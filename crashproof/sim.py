"""Height-map (Z-map) material removal and collision simulation for 3-axis milling.

The stock is a grid of material heights. Every move sweeps the cutter over the
grid and lowers the heights it passes through. Before each sweep the tool
assembly (cutter, shank, holder, spindle nose) is tested against the material
that is still there and against the fixtures, so a collision is reported at
the exact line and position where it would happen on the machine.

Two modes run the same program:
  real   tool tip = gauge - loaded tool length   (what the machine will do)
  ideal  tool tip = gauge - active H value       (what the CAM programmer meant)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from .gcode import Program, interpret
from .setup import Box, Setup, Tool

EPS = 0.02  # mm of material that counts as contact
MAX_DZ = 0.1  # vertical step when sweeping plunges and ramps
DRILL_COT = 1 / math.tan(math.radians(59))  # 118 degree drill point
SPINDLE_NOSE_HEIGHT = 200.0

SEVERITY_RANK = {"crash": 0, "scrap": 1, "alarm": 2, "warning": 3, "info": 4}


@dataclass
class Issue:
    code: str
    severity: str
    line: int
    message: str
    at: tuple[float, float, float] | None = None
    value: float | None = None
    tool: int | None = None

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "line": self.line, "message": self.message,
                "at": [round(v, 3) for v in self.at] if self.at else None,
                "value": None if self.value is None else round(self.value, 3), "tool": self.tool}


class Grid:
    def __init__(self, stock: Box, res: float):
        self.nx = max(1, int(round((stock.x1 - stock.x0) / res)))
        self.ny = max(1, int(round((stock.y1 - stock.y0) / res)))
        self.dx = (stock.x1 - stock.x0) / self.nx
        self.dy = (stock.y1 - stock.y0) / self.ny
        self.x0, self.y0 = stock.x0, stock.y0
        self.xs = self.x0 + (np.arange(self.nx) + 0.5) * self.dx
        self.ys = self.y0 + (np.arange(self.ny) + 0.5) * self.dy
        self.cell_area = self.dx * self.dy

    def window(self, xmin, xmax, ymin, ymax):
        i0 = max(0, int(math.floor((xmin - self.x0) / self.dx)))
        i1 = min(self.nx, int(math.ceil((xmax - self.x0) / self.dx)) + 1)
        j0 = max(0, int(math.floor((ymin - self.y0) / self.dy)))
        j1 = min(self.ny, int(math.ceil((ymax - self.y0) / self.dy)) + 1)
        if i0 >= i1 or j0 >= j1:
            return None
        return slice(j0, j1), slice(i0, i1)


def target_heights(setup: Setup, grid: Grid) -> np.ndarray:
    """Lowest height the finished part allows at every cell (stock top where nothing is machined)."""
    X, Y = np.meshgrid(grid.xs, grid.ys)
    t = np.full((grid.ny, grid.nx), setup.stock.z1, dtype=float)
    dil = 0.75 * max(grid.dx, grid.dy)
    for f in setup.features:
        kind = f["type"]
        if kind == "face":
            t = np.minimum(t, f["z"])
            continue
        if kind == "pocket":
            cx, cy = (f["x0"] + f["x1"]) / 2, (f["y0"] + f["y1"]) / 2
            hw, hh, r = (f["x1"] - f["x0"]) / 2, (f["y1"] - f["y0"]) / 2, f.get("corner_r", 0.0)
            qx = np.maximum(np.abs(X - cx) - (hw - r), 0)
            qy = np.maximum(np.abs(Y - cy) - (hh - r), 0)
            mask = np.hypot(qx, qy) <= r + dil
        elif kind == "hole":
            mask = np.hypot(X - f["x"], Y - f["y"]) <= f["d"] / 2 + dil
        elif kind == "slot":
            mask = _seg_dist(X, Y, f["x0"], f["y0"], f["x1"], f["y1"]) <= f["w"] / 2 + dil
        else:
            continue
        t = np.where(mask, np.minimum(t, f["z"]), t)
    return t


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-12:
        return np.hypot(px - ax, py - ay)
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _seg_hits_rect(ax, ay, bx, by, x0, x1, y0, y1) -> bool:
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
        else:
            r = q / p
            if p < 0:
                if r > t1:
                    return False
                t0 = max(t0, r)
            else:
                if r < t0:
                    return False
                t1 = min(t1, r)
    return True


def _seg_rect_dist(ax, ay, bx, by, b: Box) -> float:
    if _seg_hits_rect(ax, ay, bx, by, b.x0, b.x1, b.y0, b.y1):
        return 0.0

    def pt(px, py):
        return math.hypot(max(b.x0 - px, 0, px - b.x1), max(b.y0 - py, 0, py - b.y1))

    d = min(pt(ax, ay), pt(bx, by))
    for cx, cy in ((b.x0, b.y0), (b.x0, b.y1), (b.x1, b.y0), (b.x1, b.y1)):
        d = min(d, float(_seg_dist(np.array(cx), np.array(cy), ax, ay, bx, by)))
    return d


def _profile(tool: Tool, d: np.ndarray) -> np.ndarray:
    r = tool.radius
    if tool.kind == "ball":
        return r - np.sqrt(np.maximum(r * r - d * d, 0.0))
    if tool.kind == "drill":
        return d * DRILL_COT
    return np.zeros_like(d)


def _components(tool: Tool, spindle_r: float):
    """Tool assembly as stacked cylinders: (name, radius, z from tip, z to tip)."""
    return [
        ("cutter", tool.radius, 0.0, tool.flute_length),
        ("shank", tool.shank_radius, tool.flute_length, tool.stickout),
        ("holder", tool.holder_diameter / 2, tool.stickout, tool.length),
        ("spindle", spindle_r, tool.length, tool.length + SPINDLE_NOSE_HEIGHT),
    ]


@dataclass
class SimResult:
    program: Program
    issues: list[Issue]
    heights: np.ndarray
    target: np.ndarray
    grid: Grid
    path: list[dict]
    removed: float
    snapshots: dict[int, np.ndarray]
    elapsed: float
    mode: str

    @property
    def blocking(self) -> list[Issue]:
        return [i for i in self.issues if i.severity in ("crash", "scrap", "alarm")]

    @property
    def verdict(self) -> str:
        return "STOP" if self.blocking else "GO"


class _Collector:
    def __init__(self):
        self.items: dict[tuple, Issue] = {}

    def add(self, code, severity, line, message, at=None, value=None, tool=None, key_extra="", once=False):
        key = (code, None if once else line, key_extra)
        cur = self.items.get(key)
        if cur is None:
            self.items[key] = Issue(code, severity, line, message, at, value, tool)
        elif value is not None and (cur.value is None or value > cur.value):
            cur.value = value

    def sorted(self) -> list[Issue]:
        for i in self.items.values():
            if "{v}" in i.message:
                i.message = i.message.replace("{v}", f"{i.value or 0:.2f}")
        return sorted(self.items.values(), key=lambda i: (i.line, SEVERITY_RANK.get(i.severity, 9), i.code))


def simulate(text: str, setup: Setup, mode: str = "real", snapshot_lines=(), start_heights=None,
             program: Program | None = None) -> SimResult:
    t_start = time.perf_counter()
    if program is None:
        program = interpret(text, setup.wcs, setup.h_table, setup.home, setup.tool_in_spindle)
    real = mode == "real"
    grid = Grid(setup.stock, setup.resolution)
    target = target_heights(setup, grid)
    h = np.full((grid.ny, grid.nx), setup.stock.z1, dtype=float) if start_heights is None else start_heights.copy()
    X, Y = np.meshgrid(grid.xs, grid.ys)
    w = setup.wcs.get(setup.primary_wcs, [0.0, 0.0, 0.0])
    top_all = max([setup.stock.z1] + [f.z1 for f in setup.fixtures])
    spindle_r = setup.spindle_diameter / 2
    tol = setup.tolerance
    out = _Collector()
    snapshots: dict[int, np.ndarray] = {}
    pending_snaps = sorted(set(snapshot_lines))
    path: list[dict] = []
    removed = 0.0
    warned_tools: set = set()

    for n in program.notes:
        out.add(n.code, n.severity, n.line, n.message)

    if real:
        _tool_checks(program, setup, out)

    for mv in program.moves:
        while pending_snaps and mv.line >= pending_snaps[0]:
            snapshots[pending_snaps.pop(0)] = h.copy()
        tool = setup.tools.get(mv.tool) if real else setup.cam_tool(mv.tool)
        if tool is None:
            if mv.tool is not None and mv.tool not in warned_tools:
                warned_tools.add(mv.tool)
                out.add("TOOL_NOT_IN_MAGAZINE", "alarm", mv.line, f"T{mv.tool} is not in the machine's tool table.")
            continue
        drop = tool.length if real else mv.hv
        a = np.array([mv.g0[0] - w[0], mv.g0[1] - w[1], mv.g0[2] - w[2] - drop])
        b = np.array([mv.g1[0] - w[0], mv.g1[1] - w[1], mv.g1[2] - w[2] - drop])
        path.append({"line": mv.line, "kind": mv.kind, "tool": mv.tool,
                     "a": [round(float(v), 3) for v in a], "b": [round(float(v), 3) for v in b]})

        if real:
            for i, ax in enumerate("xyz"):
                lo, hi = setup.travel[ax]
                v = mv.g1[i]
                if v < lo - 1e-6 or v > hi + 1e-6:
                    out.add("OVERTRAVEL", "alarm", mv.line,
                            f"{ax.upper()} axis would travel to {v:.2f} (machine), outside [{lo}, {hi}].",
                            tuple(b), abs(v - (lo if v < lo else hi)))
            if mv.kind == "feed" and mv.h is None and min(a[2], b[2]) < top_all + 5:
                out.add("NO_TLO", "warning", mv.line,
                        "Cutting with no tool length offset active (G43 H missing or cancelled).", tuple(b))
            if mv.h is not None and mv.kind == "feed" and abs(mv.hv - tool.length) > tol:
                err = mv.hv - tool.length
                where = "above" if err > 0 else "below"
                out.add("TLO_OFFSET_ERROR", "scrap", mv.line,
                        f"H{mv.h} = {mv.hv:g} mm but T{mv.tool} measures {tool.length:g} mm: the tool tip runs "
                        f"{abs(err):.2f} mm {where} the programmed path.", tuple(b), abs(err), mv.tool,
                        key_extra=f"H{mv.h}T{mv.tool}", once=True)

        if min(a[2], b[2]) > top_all + EPS:
            continue

        comps = _components(tool, spindle_r)
        n_sub = max(1, math.ceil(abs(b[2] - a[2]) / MAX_DZ))
        contact_top = None  # material height where this move first touched, for total penetration
        for k in range(n_sub):
            pa = a + (b - a) * (k / n_sub)
            pb = a + (b - a) * ((k + 1) / n_sub)
            zlow, zhigh = min(pa[2], pb[2]), max(pa[2], pb[2])
            if zlow > top_all + EPS:
                continue

            # assembly above the cutter against remaining material
            for name, r, lo, _hi in comps[1:]:
                if zlow + lo > setup.stock.z1 + EPS or (name == "spindle" and not real):
                    continue
                win = grid.window(min(pa[0], pb[0]) - r, max(pa[0], pb[0]) + r,
                                  min(pa[1], pb[1]) - r, max(pa[1], pb[1]) + r)
                if win is None or not real:
                    continue
                d = _seg_dist(X[win], Y[win], pa[0], pa[1], pb[0], pb[1])
                hit = (d <= r) & (h[win] > zlow + lo + EPS)
                if hit.any():
                    depth = float((h[win][hit] - (zlow + lo)).max())
                    code = {"shank": "SHANK_COLLISION", "holder": "HOLDER_COLLISION",
                            "spindle": "SPINDLE_COLLISION"}[name]
                    msg = {"shank": f"Material reaches above the {tool.flute_length:g} mm flute length of T{mv.tool}; the shank would rub.",
                           "holder": f"Tool holder of T{mv.tool} hits the part (stickout {tool.stickout:g} mm is too short here).",
                           "spindle": "Spindle nose hits the part."}[name]
                    out.add(code, "crash", mv.line, msg, tuple(pb), depth, mv.tool)

            # cutter against material: removal, rapid crash, spindle stopped, gouge
            r = tool.radius
            win = grid.window(min(pa[0], pb[0]) - r, max(pa[0], pb[0]) + r,
                              min(pa[1], pb[1]) - r, max(pa[1], pb[1]) + r)
            if win is not None:
                d = _seg_dist(X[win], Y[win], pa[0], pa[1], pb[0], pb[1])
                mask = d <= r
                if mask.any():
                    bottom = zlow + _profile(tool, d)
                    pre = h[win]
                    over = np.where(mask, pre - bottom, 0.0)
                    cut = over > EPS
                    if cut.any():
                        if contact_top is None:
                            contact_top = float(pre[cut].max())
                        depth = max(float(over.max()), contact_top - zlow)
                        if real and mv.kind == "rapid":
                            out.add("RAPID_INTO_MATERIAL", "crash", mv.line,
                                    f"Rapid move drives T{mv.tool} up to {{v}} mm into material.",
                                    tuple(pb), depth, mv.tool)
                        elif real and mv.rpm <= 0:
                            out.add("CUT_SPINDLE_OFF", "crash", mv.line,
                                    f"T{mv.tool} cuts up to {{v}} mm of material with the spindle stopped.",
                                    tuple(pb), depth, mv.tool)
                        removed += float(over[cut].sum()) * grid.cell_area
                        new = np.where(cut, bottom, pre)
                        tgt = target[win]
                        gouge = cut & (new < tgt - tol) & (pre >= tgt - tol)
                        if gouge.any():
                            gd = float((tgt - new)[gouge].max())
                            out.add("GOUGE", "scrap", mv.line,
                                    "Cuts up to {v} mm below the finished part surface.", tuple(pb), gd, mv.tool)
                        h[win] = new

            # fixtures
            if real:
                for fx in setup.fixtures:
                    if zlow > fx.z1 + EPS:
                        continue
                    dist = None
                    for name, r_c, lo, hi in comps:
                        if zlow + lo < fx.z1 and zhigh + hi > fx.z0:
                            if dist is None:
                                dist = _seg_rect_dist(pa[0], pa[1], pb[0], pb[1], fx)
                            if dist <= r_c:
                                out.add("FIXTURE_COLLISION", "crash", mv.line,
                                        f"T{mv.tool} {name} hits {fx.name}.", tuple(pb),
                                        float(fx.z1 - (zlow + lo)), mv.tool, key_extra=fx.name)
                                break

    for line in pending_snaps:
        snapshots[line] = h.copy()
    return SimResult(program=program, issues=out.sorted(), heights=h, target=target, grid=grid, path=path,
                     removed=removed, snapshots=snapshots, elapsed=time.perf_counter() - t_start, mode=mode)


def _tool_checks(program: Program, setup: Setup, out: _Collector) -> None:
    """Compare what the CAM post says each tool is with what is actually in the magazine."""
    lines = sorted(program.states)
    seen = set()
    for i, ln in enumerate(lines[:-1]):
        t_now = program.states[lines[i + 1]].tool
        if t_now is None or t_now == program.states[ln].tool or t_now in seen:
            continue
        seen.add(t_now)
        cam = setup.cam_tools.get(t_now)
        real = setup.tools.get(t_now)
        if cam is None or real is None:
            continue
        diffs = []
        if real.kind != cam.kind:
            diffs.append(f"type {cam.kind} -> {real.kind}")
        if abs(real.diameter - cam.diameter) > 0.01:
            diffs.append(f"diameter {cam.diameter:g} -> {real.diameter:g} mm")
        if abs(real.flute_length - cam.flute_length) > 0.5:
            diffs.append(f"flute length {cam.flute_length:g} -> {real.flute_length:g} mm")
        if diffs:
            out.add("TOOL_MISMATCH", "warning", ln,
                    f"T{t_now} in the magazine differs from the CAM tool: " + ", ".join(diffs) + ".", tool=t_now)


def intent_diff(a: np.ndarray, b: np.ndarray, tol: float) -> dict:
    d = np.abs(a - b)
    return {"max_mm": round(float(d.max()), 4), "cells_over_tol": int((d > tol).sum()), "cells": int(d.size)}
