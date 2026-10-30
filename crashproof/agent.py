"""Nemotron on Nebius Token Factory: proposes G-code repairs; the simulator decides whether they are accepted.

The model never certifies anything. Each candidate edit is applied, re-simulated
against the real setup and accepted only when every blocking problem is gone
and the machined shape still matches what the original program intended.
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .core import apply_edits, prove
from .setup import Setup
from .sim import simulate

BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
FIX_MODEL = os.environ.get("CRASHPROOF_FIX_MODEL", "nvidia/nemotron-3-super-120b-a12b")
FAST_MODEL = os.environ.get("CRASHPROOF_FAST_MODEL", "nvidia/Nemotron-3_5-Lightning")

SYSTEM = """You are a senior CNC programmer doing a prove-out before Cycle Start on a 3-axis vertical machining centre with a Fanuc-style control.
A physics simulator has run the program against the REAL machine setup (tools actually in the magazine, offsets actually entered, clamps actually on the table) and reported problems.
Propose the smallest edit to the G-code that removes every blocking problem.

Rules
1. Do not change the cutting geometry the CAM system produced (XY paths and cutting depths). The only exception is a GOUGE caused by a wrong value in the program itself; then correct only that value.
2. Fix the root cause. Typical root causes: missing or wrong G43 H word (H must match the loaded tool), G0 where a G1 with feed is needed, missing M3 with S after a tool change, a canned-cycle return plane (G98/G99) or safe height below a clamp, a missing retract before a rapid move.
3. If the cause is in the setup (wrong tool in a pocket, clamp in the path, stickout too short, wrong value in an offset register) and the program is right, do not bend the program around it: leave "edits" empty and correct the setup with "setup_changes", plus a plain instruction for the operator in "setup_actions".
   setup_changes paths: tools.<T>.diameter|kind|flute_length|shank_diameter|stickout|length|holder_diameter, h_table.<H>, fixtures.<index>.x0|x1|y0|y1. A clamp must still hold the stock after it is moved. A re-loaded tool must be re-measured, so update its h_table value too.
4. Line numbers refer to the numbered listing. Ops: replace, insert_before, insert_after, delete. Keep the shop's style (N numbers, trailing decimal points).
5. Your fix will be re-simulated. It is accepted only if the simulator finds no blocking problem and the machined shape still matches the program's intent.

Reply with one JSON object and nothing else:
{"diagnosis": "<one or two sentences: what will physically happen and why>",
 "edits": [{"op": "replace", "line": 12, "text": "N60 G43 Z25. H3 M8"}],
 "setup_changes": [],
 "setup_actions": [],
 "confidence": 0.0}"""

SETUP_PATH = re.compile(r"^(tools\.\d+\.(diameter|kind|flute_length|shank_diameter|stickout|length|holder_diameter)"
                        r"|h_table\.\d+|fixtures\.\d+\.(x0|x1|y0|y1))$")


def apply_setup_changes(setup: Setup, changes: list[dict]) -> Setup:
    d = setup.to_dict()
    for c in changes:
        path = str(c.get("path", ""))
        if not SETUP_PATH.match(path):
            raise ValueError(f"setup path not allowed: {path}")
        parts = path.split(".")
        if parts[0] == "h_table":
            d["h_table"][parts[1]] = float(c["value"])
        elif parts[0] == "tools":
            if parts[1] not in d["tools"]:
                raise ValueError(f"no tool T{parts[1]} in the magazine")
            d["tools"][parts[1]][parts[2]] = str(c["value"]) if parts[2] == "kind" else float(c["value"])
        else:
            i = int(parts[1])
            if not 0 <= i < len(d["fixtures"]):
                raise ValueError(f"no fixture {i}")
            d["fixtures"][i][parts[2]] = float(c["value"])
    st = d["stock"]
    for fx in d["fixtures"]:
        if "clamp" in fx["name"].lower():
            holds = fx["x0"] < st["x1"] and fx["x1"] > st["x0"] and fx["y0"] < st["y1"] and fx["y1"] > st["y0"]
            if not holds or fx["x1"] <= fx["x0"] or fx["y1"] <= fx["y0"]:
                raise ValueError(f"{fx['name']} would no longer hold the stock")
    return Setup.from_dict(d)


@dataclass
class Candidate:
    model: str
    temperature: float
    diagnosis: str = ""
    edits: list = field(default_factory=list)
    setup_changes: list = field(default_factory=list)
    setup_actions: list = field(default_factory=list)
    confidence: float | None = None
    raw: str = ""
    error: str | None = None
    latency_s: float = 0.0
    tokens: dict = field(default_factory=dict)


def client():
    key = os.environ.get("NEBIUS_API_KEY")
    if not key:
        return None
    from openai import OpenAI
    return OpenAI(base_url=BASE_URL, api_key=key, timeout=120)


def _json_from(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if m:
        return json.loads(m.group(1))
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for s in starts:
        depth = 0
        for j in range(s, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[s:j + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError("no JSON object in model reply")


def setup_brief(setup: Setup) -> str:
    out = [f"Setup: {setup.name}",
           f"Stock (work frame {setup.primary_wcs}, mm): X {setup.stock.x0}..{setup.stock.x1}, "
           f"Y {setup.stock.y0}..{setup.stock.y1}, Z {setup.stock.z0}..{setup.stock.z1}",
           "Tools in the magazine (measured length = what H should hold):"]
    for n, t in sorted(setup.tools.items()):
        cam = setup.cam_tools.get(n)
        note = ""
        if cam and (abs(cam.diameter - t.diameter) > 0.01 or cam.kind != t.kind):
            note = f"   <-- CAM program assumes {cam.kind} D{cam.diameter:g}"
        out.append(f"  T{n}: {t.description or t.kind} D{t.diameter:g} {t.kind}, flute {t.flute_length:g}, "
                   f"stickout {t.stickout:g}, holder D{t.holder_diameter:g}, length {t.length:g}{note}")
    out.append("Length offset registers: " + ", ".join(f"H{k}={v:g}" for k, v in sorted(setup.h_table.items())))
    out.append("Work offsets (machine): " + ", ".join(f"{k}={v}" for k, v in setup.wcs.items()))
    out.append("Fixtures (work frame, index for setup_changes):")
    for i, f in enumerate(setup.fixtures):
        out.append(f"  fixtures.{i} {f.name}: X {f.x0}..{f.x1}, Y {f.y0}..{f.y1}, Z {f.z0}..{f.z1}")
    if setup.features:
        out.append("Finished part features (from the CAM setup sheet):")
        for f in setup.features:
            out.append("  " + ", ".join(f"{k}={v}" for k, v in f.items()))
    return "\n".join(out)


def listing(text: str, focus: list[int], full_limit: int = 320) -> str:
    lines = text.splitlines()
    if len(lines) <= full_limit:
        keep = range(1, len(lines) + 1)
    else:
        keep_set = set(range(1, 16))
        for ln in focus:
            keep_set.update(range(max(1, ln - 30), min(len(lines), ln + 15) + 1))
        keep = sorted(keep_set)
    out, prev = [], 0
    for i in keep:
        if prev and i != prev + 1:
            out.append("   ...")
        out.append(f"{i:4d} | {lines[i - 1]}")
        prev = i
    return "\n".join(out)


def issues_brief(issues: list[dict]) -> str:
    rows = []
    for i in issues:
        at = f" at X{i['at'][0]} Y{i['at'][1]} Z{i['at'][2]}" if i.get("at") else ""
        rows.append(f"- line {i['line']}: {i['severity'].upper()} {i['code']}: {i['message']}{at}")
    return "\n".join(rows)


def ask(cl, model: str, text: str, setup: Setup, issues: list[dict], feedback: list[str], temperature: float) -> Candidate:
    cand = Candidate(model=model, temperature=temperature)
    focus = [i["line"] for i in issues if i.get("line")]
    user = (setup_brief(setup) + "\n\nSimulator findings on the real setup:\n" + issues_brief(issues)
            + "\n\nProgram:\n" + listing(text, focus))
    if feedback:
        user += "\n\nEarlier attempts that the simulator rejected:\n" + "\n".join(feedback)
    t0 = time.perf_counter()
    try:
        resp = cl.chat.completions.create(
            model=model, temperature=temperature, max_tokens=4000,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
        cand.latency_s = round(time.perf_counter() - t0, 2)
        msg = resp.choices[0].message
        cand.raw = msg.content or ""
        if resp.usage:
            cand.tokens = {"in": resp.usage.prompt_tokens, "out": resp.usage.completion_tokens}
        data = _json_from(cand.raw)
        cand.diagnosis = str(data.get("diagnosis", ""))
        cand.edits = list(data.get("edits") or [])
        cand.setup_changes = list(data.get("setup_changes") or [])
        cand.setup_actions = [str(a) for a in data.get("setup_actions") or []]
        cand.confidence = data.get("confidence")
    except Exception as e:  # noqa: BLE001 - surfaced to the UI
        cand.latency_s = round(time.perf_counter() - t0, 2)
        cand.error = f"{type(e).__name__}: {e}"[:300]
    return cand


def repair(text: str, setup: Setup, issues: list[dict], rounds: int = 3, n: int = 3, emit=lambda e: None,
           model: str | None = None) -> dict:
    cl = client()
    if cl is None:
        return {"status": "no_key", "message": "Set NEBIUS_API_KEY to enable the Nemotron repair agent."}
    model = model or FIX_MODEL
    ideal_original = simulate(text, setup, "ideal")
    current, current_setup = text, setup
    current_issues = [i for i in issues if i["severity"] in ("crash", "scrap", "alarm")]
    feedback: list[str] = []
    history = []
    temps = [0.2, 0.6, 0.9][:n]
    for rnd in range(1, rounds + 1):
        emit({"type": "round", "round": rnd, "issues": len(current_issues)})
        with ThreadPoolExecutor(max_workers=len(temps)) as ex:
            cands = list(ex.map(lambda t: ask(cl, model, current, current_setup, current_issues, feedback, t), temps))
        scored = []
        for k, c in enumerate(cands):
            entry = {"round": rnd, "candidate": k + 1, "model": c.model, "temperature": c.temperature,
                     "diagnosis": c.diagnosis, "edits": c.edits, "setup_changes": c.setup_changes,
                     "setup_actions": c.setup_actions, "confidence": c.confidence, "latency_s": c.latency_s,
                     "tokens": c.tokens, "error": c.error}
            if c.error or not (c.edits or c.setup_changes):
                entry["result"] = "error" if c.error else "no_edit"
                emit({"type": "candidate", **entry})
                history.append(entry)
                continue
            try:
                cand_text = apply_edits(current, c.edits) if c.edits else current
                cand_setup = apply_setup_changes(current_setup, c.setup_changes) if c.setup_changes else current_setup
            except Exception as e:  # noqa: BLE001
                entry["result"] = "invalid_edit"
                entry["error"] = str(e)
                emit({"type": "candidate", **entry})
                history.append(entry)
                continue
            proof = prove(text, cand_text, cand_setup, ideal_original)
            entry.update({"result": "proved" if proof["accepted"] else "rejected",
                          "remaining": proof["blocking"], "intent": proof["intent"],
                          "changed_lines": proof["changed_lines"]})
            emit({"type": "candidate", **entry})
            history.append(entry)
            scored.append((proof, cand_text, cand_setup, entry))
        accepted = [s for s in scored if s[0]["accepted"]]
        if accepted:
            proof, cand_text, cand_setup, entry = min(
                accepted, key=lambda s: s[0]["changed_lines"] + 2 * len(s[3]["setup_changes"]))
            emit({"type": "accepted", "round": rnd, "candidate": entry["candidate"]})
            return {"status": "proved", "program": cand_text, "diff": proof["diff"], "proof": _slim(proof),
                    "setup": cand_setup.to_dict() if cand_setup is not setup else None,
                    "setup_changes": _setup_diff(setup, cand_setup) if cand_setup is not setup else [],
                    "setup_actions": entry["setup_actions"],
                    "chosen": entry, "history": history, "model": model}
        for proof, _t, _s, entry in scored:
            rem = "; ".join(f"line {i['line']} {i['code']}" for i in proof["blocking"][:4]) or "none"
            why = "changed the machined shape" if not proof["intent"]["preserved"] else f"still blocked: {rem}"
            feedback.append(f"- edits {json.dumps(entry['edits'])}, setup_changes {json.dumps(entry['setup_changes'])}"
                            f" -> rejected ({why})")
        if scored:
            best = min(scored, key=lambda s: (len(s[0]["blocking"]), s[0]["changed_lines"]))
            if len(best[0]["blocking"]) < len(current_issues):
                current, current_setup, current_issues = best[1], best[2], best[0]["blocking"]
                emit({"type": "progress", "round": rnd, "remaining": len(current_issues)})
    actions = sorted({a for h in history for a in h.get("setup_actions") or []})
    return {"status": "not_proved", "history": history, "setup_actions": actions, "model": model,
            "message": "No candidate passed the simulator. The program must not run as is."}


def _slim(proof: dict) -> dict:
    return {k: v for k, v in proof.items() if k != "report"}


def _setup_diff(a: Setup, b: Setup) -> list[dict]:
    da, db = a.to_dict(), b.to_dict()
    out = []
    for n, t in db["tools"].items():
        for k, v in t.items():
            if da["tools"].get(n, {}).get(k) != v:
                out.append({"path": f"tools.{n}.{k}", "from": da["tools"].get(n, {}).get(k), "to": v})
    for n, v in db["h_table"].items():
        if da["h_table"].get(n) != v:
            out.append({"path": f"h_table.{n}", "from": da["h_table"].get(n), "to": v})
    for i, (fa, fb) in enumerate(zip(da["fixtures"], db["fixtures"])):
        for k in ("x0", "x1", "y0", "y1"):
            if fa[k] != fb[k]:
                out.append({"path": f"fixtures.{i}.{k}", "from": fa[k], "to": fb[k], "name": fb["name"]})
    return out


def explain(text: str, setup: Setup, verdict: str, issues: list[dict], diff: str | None = None,
            model: str | None = None) -> dict:
    """Short operator briefing from the fast model, grounded only in simulator findings."""
    cl = client()
    if cl is None:
        return {"status": "no_key"}
    model = model or FAST_MODEL
    prompt = (f"Verdict from the simulator: {verdict}\nFindings:\n{issues_brief(issues[:8]) or '- none'}\n"
              + (f"\nProved fix (unified diff):\n{diff[:3000]}\n" if diff else "")
              + "\nWrite a briefing for the machine operator standing at the control: at most 4 short bullet "
                "lines, plain words, what would physically happen, and what to check before pressing Cycle Start. "
                "Use only the facts above. No preamble.")
    t0 = time.perf_counter()
    try:
        r = cl.chat.completions.create(model=model, temperature=0.2, max_tokens=600,
                                       messages=[{"role": "user", "content": prompt}])
        txt = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
        return {"status": "ok", "model": model, "text": txt, "latency_s": round(time.perf_counter() - t0, 2)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "model": model, "error": f"{type(e).__name__}: {e}"[:300]}


def list_models() -> list[str]:
    cl = client()
    if cl is None:
        return []
    return sorted(m.id for m in cl.models.list().data)
