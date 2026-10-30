"""Filling the digital twin from the outside world.

Tool catalog lookup: shops know a tool by its part number, not its 3D model. Tavily searches the
manufacturer's catalog, Nemotron extracts the dimensions, and the result is only used when the quoted
evidence is found verbatim in the fetched page and contains the numbers.

Controller screen reading: a photo of the tool offset or work offset page is read by an NVIDIA vision
model into register values, which the operator confirms before they enter the setup.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time

from .agent import FAST_MODEL, _json_from, client

VISION_MODEL = os.environ.get("CRASHPROOF_VISION_MODEL", "nvidia/Nemotron-Nano-12B-v2-VL")

EXTRACT = """You read cutting-tool catalog pages. From the sources below, find the tool "{query}".
Return one JSON object:
{{"found": true|false,
  "kind": "flat"|"ball"|"drill"|"other",
  "diameter_mm": number, "flute_length_mm": number, "shank_diameter_mm": number, "overall_length_mm": number,
  "flutes": integer|null,
  "source_url": "<url of the source you used>",
  "evidence": "<one short passage copied character-for-character from that source that contains the dimensions>"}}
Convert inches to millimetres (1 in = 25.4 mm) but copy the evidence exactly as written. If the sources do not
state the dimensions of this exact tool, return {{"found": false}}."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def tavily_search(query: str, max_results: int = 5) -> list[dict]:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    from tavily import TavilyClient
    tc = TavilyClient(api_key=key)
    r = tc.search(query=query, max_results=max_results, search_depth="advanced", include_raw_content="text")
    return r.get("results", [])


def lookup_tool(query: str) -> dict:
    t0 = time.perf_counter()
    cl = client()
    if cl is None:
        return {"status": "no_key", "message": "NEBIUS_API_KEY is not set"}
    try:
        results = tavily_search(f"{query} end mill drill catalog dimensions cutting diameter length of cut shank overall length")
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Tavily: {e}"[:300]}
    sources = []
    for r in results:
        body = (r.get("raw_content") or r.get("content") or "")[:7000]
        sources.append({"url": r.get("url"), "title": r.get("title"), "body": body})
    ctx = "\n\n".join(f"[source {i + 1}] {s['url']}\n{s['body']}" for i, s in enumerate(sources))
    try:
        resp = cl.chat.completions.create(model=FAST_MODEL, temperature=0.0, max_tokens=900,
                                          messages=[{"role": "user", "content": EXTRACT.format(query=query) + "\n\n" + ctx}])
        data = _json_from(resp.choices[0].message.content or "")
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"model: {e}"[:300], "sources": [s["url"] for s in sources]}
    out = {"status": "not_found", "query": query, "model": FAST_MODEL, "sources": [s["url"] for s in sources],
           "latency_s": round(time.perf_counter() - t0, 2), "extracted": data}
    if not data.get("found"):
        return out
    src = next((s for s in sources if s["url"] == data.get("source_url")), None)
    ev = data.get("evidence") or ""
    verified = bool(src and ev and _norm(ev) in _norm(src["body"]))
    nums_in_ev = set(re.findall(r"\d+(?:\.\d+)?", ev))
    out["status"] = "verified" if verified else "unverified"
    out["evidence_found_in_source"] = verified
    out["evidence_has_numbers"] = len(nums_in_ev) >= 2
    out["tool"] = {"kind": data.get("kind"), "diameter": data.get("diameter_mm"),
                   "flute_length": data.get("flute_length_mm"), "shank_diameter": data.get("shank_diameter_mm"),
                   "overall_length": data.get("overall_length_mm"), "flutes": data.get("flutes")}
    return out


OFFSETS = """This is a photo of a CNC control screen ({page}). Read every register you can see.
Return one JSON object:
{{"page": "tool_offsets"|"work_offsets"|"other",
  "units": "mm"|"inch"|null,
  "tool_offsets": [{{"no": 1, "length_geom": 0.0, "length_wear": 0.0, "dia_geom": 0.0, "dia_wear": 0.0}}],
  "work_offsets": [{{"name": "G54", "x": 0.0, "y": 0.0, "z": 0.0}}],
  "unreadable": ["<registers you could not read with certainty>"]}}
Only report digits you can actually read; put anything uncertain in "unreadable" instead of guessing."""


def read_screen(image_bytes: bytes, mime: str = "image/jpeg", page: str = "tool offset or work offset page") -> dict:
    cl = client()
    if cl is None:
        return {"status": "no_key"}
    b64 = base64.b64encode(image_bytes).decode()
    t0 = time.perf_counter()
    try:
        resp = cl.chat.completions.create(
            model=VISION_MODEL, temperature=0.0, max_tokens=1500,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": OFFSETS.format(page=page)},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}])
        data = _json_from(resp.choices[0].message.content or "")
        return {"status": "ok", "model": VISION_MODEL, "latency_s": round(time.perf_counter() - t0, 2), "data": data}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "model": VISION_MODEL, "message": f"{type(e).__name__}: {e}"[:300]}


def offsets_to_setup_changes(setup_dict: dict, data: dict) -> list[dict]:
    """Compare what the control holds with what the setup assumes; every change is shown to the operator."""
    changes = []
    scale = 25.4 if data.get("units") == "inch" else 1.0
    for row in data.get("tool_offsets") or []:
        try:
            n = str(int(row["no"]))
            val = (float(row.get("length_geom") or 0) + float(row.get("length_wear") or 0)) * scale
        except (KeyError, TypeError, ValueError):
            continue
        cur = (setup_dict.get("h_table") or {}).get(n)
        if cur is None and abs(val) < 1e-9:
            continue
        if cur is None or abs(float(cur) - val) > 1e-3:
            changes.append({"field": f"H{n}", "path": ["h_table", n], "from": cur, "to": round(val, 4)})
    for row in data.get("work_offsets") or []:
        name = str(row.get("name", "")).upper()
        if not re.fullmatch(r"G5[4-9]", name):
            continue
        try:
            val = [float(row["x"]) * scale, float(row["y"]) * scale, float(row["z"]) * scale]
        except (KeyError, TypeError, ValueError):
            continue
        cur = (setup_dict.get("wcs") or {}).get(name)
        if cur is None or any(abs(float(a) - b) > 1e-3 for a, b in zip(cur, val)):
            changes.append({"field": name, "path": ["wcs", name], "from": cur, "to": [round(v, 4) for v in val]})
    return changes
