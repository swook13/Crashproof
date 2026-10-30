"""Run the Nemotron repair loop on every built-in scenario and record the event stream for the demo.

    python -m bench.record_runs            # all scenarios
    python -m bench.record_runs g99 no_g43 # selected ones

Each run is saved to cache/repair_<id>.jsonl exactly as the web app streams it, and a summary row
(status, rounds, candidates, accepted candidate, latency, tokens) is appended to bench/repair_runs.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from crashproof import agent  # noqa: E402
from crashproof.core import analyze, view_payload  # noqa: E402
from crashproof.scenarios import all_scenarios  # noqa: E402
from crashproof.setup import Setup  # noqa: E402


def record(sc: dict) -> dict:
    s = Setup.from_dict(sc["setup"])
    rep = analyze(sc["program"], s)
    issues = [i.to_dict() for i in rep["issues"]]
    events = [{"type": "meta", "recorded_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
               "model": agent.FIX_MODEL, "endpoint": agent.BASE_URL}]
    t0 = time.perf_counter()
    result = agent.repair(sc["program"], s, issues, emit=events.append)
    wall = round(time.perf_counter() - t0, 1)
    if result.get("status") == "proved":
        s2 = Setup.from_dict(result["setup"]) if result.get("setup") else s
        result["view"] = view_payload(result["program"], s2, analyze(result["program"], s2))
    events.append({"type": "done", **result})
    if result.get("status") in ("proved", "not_proved"):
        (ROOT / "cache").mkdir(exist_ok=True)
        (ROOT / "cache" / f"repair_{sc['id']}.jsonl").write_text(
            "\n".join(json.dumps(e, default=str) for e in events), encoding="utf-8")
    hist = result.get("history", [])
    return {"scenario": sc["id"], "status": result.get("status"), "wall_s": wall,
            "rounds": max([h["round"] for h in hist], default=0), "candidates": len(hist),
            "proved_candidates": sum(h.get("result") == "proved" for h in hist),
            "accepted": (result.get("chosen") or {}).get("candidate"),
            "kind": ("setup" if result.get("setup_changes") and not (result.get("proof") or {}).get("changed_lines")
                     else "program" if result.get("status") == "proved" else None),
            "tokens_in": sum((h.get("tokens") or {}).get("in", 0) for h in hist),
            "tokens_out": sum((h.get("tokens") or {}).get("out", 0) for h in hist),
            "model": agent.FIX_MODEL, "message": result.get("message")}


if __name__ == "__main__":
    want = set(sys.argv[1:])
    rows = []
    for sc in all_scenarios():
        if sc["id"] == "clean" or (want and sc["id"] not in want):
            continue
        row = record(sc)
        rows.append(row)
        print(json.dumps(row))
    out = ROOT / "bench" / "repair_runs.json"
    prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
    out.write_text(json.dumps(prev + rows, indent=2), encoding="utf-8")
