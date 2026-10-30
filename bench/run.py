"""Detection benchmark.

For N generated parts (different stock, pocket, hole, slot, clamp and height values):
  * the clean program must get GO       -> false alarm rate
  * each injected mistake must get STOP with an expected finding -> detection rate per mistake type
Writes bench/results.json and prints a Markdown table.

    python -m bench.run --parts 40
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from examples.make_examples import program, setup, variant
from crashproof.core import analyze
from crashproof.setup import Setup

from .defects import INJECTORS, harmful, inject

OUT = Path(__file__).parent


def run(parts: int, seed0: int = 1000) -> dict:
    rows = {name: {"n": 0, "detected": 0, "harmless": 0, "harmless_go": 0, "misses": [], "false_stops": []}
            for name in INJECTORS}
    clean = {"n": 0, "false_alarms": 0, "examples": []}
    times = []
    for k in range(parts):
        P = variant(seed0 + k)
        text, sd = program(P), setup(P)
        t0 = time.perf_counter()
        rep = analyze(text, Setup.from_dict(sd))
        times.append(time.perf_counter() - t0)
        clean["n"] += 1
        if rep["verdict"] != "GO":
            clean["false_alarms"] += 1
            clean["examples"].append({"seed": seed0 + k, "findings": [i.to_dict() for i in rep["blocking"]][:3]})
        for name in INJECTORS:
            res = inject(name, text, sd, seed=k)
            if res is None:
                continue
            t2, s2, expected = res
            r2 = analyze(t2, Setup.from_dict(s2))
            codes = {i.code for i in r2["issues"]}
            row = rows[name]
            if not harmful(name, t2, s2):
                row["harmless"] += 1
                if r2["verdict"] == "GO":
                    row["harmless_go"] += 1
                else:
                    row["false_stops"].append({"seed": seed0 + k, "codes": sorted(codes)})
                continue
            row["n"] += 1
            if r2["verdict"] == "STOP" and codes & expected:
                row["detected"] += 1
            else:
                row["misses"].append({"seed": seed0 + k, "verdict": r2["verdict"], "codes": sorted(codes)})
    total_n = sum(r["n"] for r in rows.values())
    total_d = sum(r["detected"] for r in rows.values())
    hl = sum(r["harmless"] for r in rows.values())
    hl_go = sum(r["harmless_go"] for r in rows.values())
    return {"parts": parts, "seed0": seed0, "clean": clean, "defects": rows,
            "detected": total_d, "injected": total_n, "harmless": hl, "harmless_go": hl_go,
            "analyze_ms_median": round(statistics.median(times) * 1000), "analyze_ms_max": round(max(times) * 1000)}


def markdown(res: dict) -> str:
    out = [f"Generated parts: {res['parts']} (seeds {res['seed0']}..{res['seed0'] + res['parts'] - 1}); "
           f"analysis time per program median {res['analyze_ms_median']} ms, max {res['analyze_ms_max']} ms.", "",
           "| Mistake | Harmful injections | Detected (STOP + right finding) | Rate | Harmless injections | Correct GO |",
           "|---|---:|---:|---:|---:|---:|"]
    for name, r in res["defects"].items():
        rate = f"{100 * r['detected'] / r['n']:.0f}%" if r["n"] else "-"
        out.append(f"| {name} | {r['n']} | {r['detected']} | {rate} | {r['harmless']} | {r['harmless_go']} |")
    out.append(f"| **all** | **{res['injected']}** | **{res['detected']}** | "
               f"**{100 * res['detected'] / max(res['injected'], 1):.1f}%** | **{res['harmless']}** | **{res['harmless_go']}** |")
    c = res["clean"]
    out += ["", f"Clean programs flagged STOP (false alarms): {c['false_alarms']} / {c['n']}",
            "", "Harmful vs harmless is decided by plain geometry on the setup sheet and the cycle block "
                "(bench/defects.py `harmful`), not by the simulator. Drill-travel mistakes are harmful when a travel "
                "move between holes crosses a clamp below its top; a too-short tool is harmful when the holder face "
                "goes below the stock top or one pass is deeper than the flute length (per-pass depth from the CAM "
                "setup sheet). An earlier version of this rule used total depth instead of per-pass depth and wrongly "
                "counted two-pass slots as harmful; it was corrected after inspecting those cases."]
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", type=int, default=40)
    ap.add_argument("--seed0", type=int, default=1000)
    a = ap.parse_args()
    res = run(a.parts, a.seed0)
    (OUT / "results.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    md = markdown(res)
    (OUT / "RESULTS.md").write_text(md + "\n", encoding="utf-8")
    print(md)
