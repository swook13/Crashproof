"""Analysis, patch proof, restart blocks and the run certificate."""
from __future__ import annotations

import difflib
import hashlib
import math
import time
from datetime import datetime, timezone

import numpy as np

from .gcode import CYCLES
from .setup import Setup
from .sim import Issue, SimResult, intent_diff, simulate

CHECKS = [
    ("RAPID_INTO_MATERIAL", "No rapid move touches material"),
    ("FIXTURE_COLLISION", "Tool, holder and spindle clear every clamp and the table"),
    ("HOLDER_COLLISION", "Holder clears the part"),
    ("SHANK_COLLISION", "Depth of cut stays within flute length"),
    ("SPINDLE_COLLISION", "Spindle nose clears the part"),
    ("CUT_SPINDLE_OFF", "Spindle is running whenever material is cut"),
    ("GOUGE", "No cut goes below the finished part surface"),
    ("TLO_OFFSET_ERROR", "Active length offset matches the loaded tool"),
    ("INTENT_MISMATCH", "Machine removes exactly what the CAM program intends"),
    ("OVERTRAVEL", "All moves stay inside machine travel"),
    ("TOOL_NOT_IN_MAGAZINE", "Every called tool is in the magazine"),
    ("ARC_ERROR", "Every arc is geometrically valid"),
    ("NO_FEED", "Every cutting move has a feed rate"),
]


def _intent_issues(real: SimResult, ideal: SimResult, setup: Setup) -> list[Issue]:
    tol = setup.tolerance
    under = real.heights > ideal.heights + tol
    over = real.heights < ideal.heights - tol
    out = []
    area = real.grid.cell_area
    if under.sum() > 4:
        dev = float((real.heights - ideal.heights)[under].max())
        out.append(Issue("INTENT_MISMATCH", "scrap", 0,
                         f"{under.sum() * area:.0f} mm² that the program is meant to cut stays uncut "
                         f"(up to {dev:.2f} mm too high). The finished part will not match the CAM model.",
                         value=dev))
    if over.sum() > 4 and not any(i.code == "GOUGE" for i in real.issues):
        dev = float((ideal.heights - real.heights)[over].max())
        out.append(Issue("INTENT_MISMATCH", "scrap", 0,
                         f"{over.sum() * area:.0f} mm² is cut deeper than the program intends (up to {dev:.2f} mm).",
                         value=dev))
    return out


def analyze(text: str, setup: Setup) -> dict:
    t0 = time.perf_counter()
    real = simulate(text, setup, "real")
    ideal = simulate(text, setup, "ideal", program=None)
    issues = list(real.issues) + _intent_issues(real, ideal, setup)
    blocking = [i for i in issues if i.severity in ("crash", "scrap", "alarm")]
    return {
        "real": real,
        "ideal": ideal,
        "issues": issues,
        "blocking": blocking,
        "verdict": "STOP" if blocking else "GO",
        "elapsed": time.perf_counter() - t0,
    }


def view_payload(text: str, setup: Setup, rep: dict) -> dict:
    """Everything the browser needs to draw the setup, the toolpath and the result."""
    real: SimResult = rep["real"]
    g = real.grid
    return {
        "verdict": rep["verdict"],
        "issues": [i.to_dict() for i in rep["issues"]],
        "lines": text.splitlines(),
        "path": real.path,
        "grid": {"nx": g.nx, "ny": g.ny, "x0": g.x0, "y0": g.y0, "dx": g.dx, "dy": g.dy},
        "heights": np.round(real.heights, 2).ravel().tolist(),
        "stock": vars(setup.stock),
        "fixtures": [vars(f) for f in setup.fixtures],
        "tools": {n: {"kind": t.kind, "diameter": t.diameter, "flute_length": t.flute_length,
                      "holder_diameter": t.holder_diameter, "stickout": t.stickout, "length": t.length,
                      "description": t.description} for n, t in setup.tools.items()},
        "stats": {"moves": len(real.program.moves), "removed_mm3": round(real.removed),
                  "sim_ms": round(rep["elapsed"] * 1000), "resolution": setup.resolution},
        "checks": checklist(rep["issues"]),
    }


def checklist(issues) -> list[dict]:
    found = {}
    for i in issues:
        found.setdefault(i.code, []).append(i.line)
    return [{"code": c, "label": label, "pass": c not in found, "lines": found.get(c, [])[:5]} for c, label in CHECKS]


# --- patches ----------------------------------------------------------------
ALLOWED_OPS = {"replace", "insert_before", "insert_after", "delete"}


def apply_edits(text: str, edits: list[dict]) -> str:
    lines = text.splitlines()
    n = len(lines)
    before: dict[int, list[str]] = {}
    after: dict[int, list[str]] = {}
    replace: dict[int, str] = {}
    delete: set[int] = set()
    for e in edits:
        op = e.get("op")
        ln = int(e.get("line", 0))
        if op not in ALLOWED_OPS:
            raise ValueError(f"unknown edit op {op!r}")
        if not 1 <= ln <= n:
            raise ValueError(f"line {ln} out of range 1..{n}")
        new = [s for s in str(e.get("text", "")).splitlines() if s.strip()] if op != "delete" else []
        if op == "replace":
            if not new:
                delete.add(ln)
            else:
                replace[ln] = "\n".join(new)
        elif op == "delete":
            delete.add(ln)
        elif op == "insert_before":
            before.setdefault(ln, []).extend(new)
        else:
            after.setdefault(ln, []).extend(new)
    out = []
    for i, line in enumerate(lines, start=1):
        out.extend(before.get(i, []))
        if i in replace:
            out.append(replace[i])
        elif i not in delete:
            out.append(line)
        out.extend(after.get(i, []))
    return "\n".join(out) + "\n"


def unified_diff(a: str, b: str) -> tuple[str, int]:
    d = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "original.nc", "proved.nc", lineterm="", n=1))
    changed = sum(1 for l in d if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---")))
    return "\n".join(d), changed


def prove(original: str, candidate: str, setup: Setup, ideal_original: SimResult | None = None) -> dict:
    """A candidate is proved when the real machine run is collision-free and cuts what the original program
    intended, except where the original itself cut below the finished part (those cells may only get better)."""
    if ideal_original is None:
        ideal_original = simulate(original, setup, "ideal")
    rep = analyze(candidate, setup)
    real = rep["real"]
    tol = setup.tolerance
    valid = ideal_original.heights >= real.target - tol
    diff = np.abs(real.heights - ideal_original.heights)
    over = valid & (diff > tol)
    corrected = int((~valid & (real.heights >= real.target - tol)).sum())
    patch, changed = unified_diff(original, candidate)
    intent_ok = int(over.sum()) == 0
    return {
        "accepted": not rep["blocking"] and intent_ok,
        "verdict": rep["verdict"],
        "blocking": [i.to_dict() for i in rep["blocking"]],
        "issues": [i.to_dict() for i in rep["issues"]],
        "intent": {"preserved": intent_ok, "max_dev_mm": round(float(diff[valid].max()) if valid.any() else 0.0, 4),
                   "cells_changed": int(over.sum()), "cells_corrected": corrected,
                   "geometry_change": corrected > 0},
        "diff": patch,
        "changed_lines": changed,
        "report": rep,
    }


# --- restart ----------------------------------------------------------------
def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0")
    return "0." if s in ("-0.", "-0") else s


def restart_block(text: str, setup: Setup, line: int) -> dict:
    """Safe mid-program restart at `line`: rebuild the modal state, approach from a safe height, then prove the
    restarted run against the material that is really left on the part at that point."""
    full = simulate(text, setup, "real", snapshot_lines=[line])
    prog = full.program
    st = prog.states.get(line)
    lines = text.splitlines()
    if st is None or line > len(lines):
        raise ValueError(f"line {line} is outside the program")
    if st.motion in CYCLES:
        raise ValueError("This line is inside a canned cycle. Restart at the line that starts the cycle.")
    if st.tool is None:
        raise ValueError("No tool is loaded at this line yet. Restart at or after the first tool change.")
    w_cur = setup.wcs.get(st.wcs, [0, 0, 0])
    w_pri = setup.wcs.get(setup.primary_wcs, [0, 0, 0])
    safe_work = max([setup.stock.z1] + [f.z1 for f in setup.fixtures if f.z1 < setup.stock.z1 + 100]) + 10.0
    safe = safe_work + w_pri[2] - w_cur[2]
    x, y, z = st.prog
    heights = full.snapshots.get(line)
    top = setup.stock.z1
    tool = setup.tools.get(st.tool)
    if heights is not None and tool is not None:
        g = full.grid
        X, Y = np.meshgrid(g.xs, g.ys)
        wx, wy = x + w_cur[0] - w_pri[0], y + w_cur[1] - w_pri[1]
        under = np.hypot(X - wx, Y - wy) <= tool.radius + 1.0
        if under.any():
            top = float(heights[under].max()) + w_pri[2] - w_cur[2]
    approach = max(z, top + 1.0)
    unit = "G20" if st.units == "inch" else "G21"
    s = 25.4 if st.units == "inch" else 1.0
    head = [
        f"(RESTART AT LINE {line} - GENERATED AND PROVED BY CRASHPROOF)",
        "G90 G94 G17 G40 G49 G80",
        unit,
        "G28 G91 Z0.",
        "G90",
        f"T{st.tool} M6",
    ]
    if st.spindle_on and st.rpm > 0:
        head.append(f"S{int(st.rpm)} M3")
    if st.coolant:
        head.append("M8")
    head += [
        f"{st.wcs} G0 X{_fmt(x / s)} Y{_fmt(y / s)}",
        f"G43 Z{_fmt(safe / s)} H{st.h if st.h is not None else st.tool}",
        f"G0 Z{_fmt(approach / s)}",
    ]
    if approach > z + 1e-6:
        head.append(f"G1 Z{_fmt(z / s)} F{_fmt(min(st.feed, 300.0 * s) / s if st.feed else 300.0)}")
    if st.feed:
        head.append(f"F{_fmt(st.feed / s)}")
    if not st.absolute:
        head.append("G91")
    if st.motion in (0, 1, 2, 3):
        head.append(f"G{st.motion}")
    body = lines[line - 1:]
    restart_text = "\n".join(head + body) + "\n"
    proof = simulate(restart_text, setup, "real", start_heights=heights)
    blocking = [i for i in proof.blocking if i.code not in ("NO_TLO",)]
    return {
        "line": line,
        "program": restart_text,
        "header_lines": len(head),
        "state": {"tool": st.tool, "h": st.h, "wcs": st.wcs, "rpm": st.rpm, "spindle_on": st.spindle_on,
                  "feed": st.feed, "position": [round(v, 3) for v in st.prog]},
        "approach_z": round(approach, 3),
        "proved": not blocking,
        "issues": [i.to_dict() for i in proof.issues],
    }


# --- certificate --------------------------------------------------------------
def certificate(text: str, setup: Setup, rep: dict, models: dict | None = None) -> dict:
    return {
        "verdict": rep["verdict"],
        "program_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "setup_sha256": setup.fingerprint(),
        "setup_name": setup.name,
        "issued_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "checks": checklist(rep["issues"]),
        "simulation": {"method": "Z-map material removal, 3-axis", "resolution_mm": setup.resolution,
                       "tolerance_mm": setup.tolerance, "moves": len(rep["real"].program.moves)},
        "tools": {n: f"{t.description} (D{t.diameter:g}, L{t.length:g})" for n, t in setup.tools.items()},
        "wcs": setup.wcs,
        "models": models or {},
        "limits": [
            "3-axis milling only; G17 arcs; cutter compensation (G41/G42), macros and subprograms are not simulated.",
            "Fixtures and tool holders are modelled as boxes and cylinders.",
            "The verdict is only valid for the setup values listed here; re-check after any offset or tool change.",
        ],
    }
