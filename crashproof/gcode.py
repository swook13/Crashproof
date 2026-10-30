"""Fanuc-style 3-axis milling G-code interpreter.

Turns program text into straight-line moves of the spindle gauge line in
machine coordinates, the way the controller would execute them once its work
offsets (G54-G59) and tool length offsets (G43 H) are applied.

Each move also records the length offset value that was active, so the
simulator can recover two tool tips from the same move:
  intended tip = gauge - H value      (what the CAM programmer assumed)
  real tip     = gauge - tool length  (where the loaded tool actually is)
They only coincide when the right offset is active for the loaded tool.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
PAREN_COMMENT = re.compile(r"\(([^)]*)\)")

CYCLES = {73, 81, 82, 83, 84, 85, 86, 89}
PECK_CLEARANCE = 0.5  # mm, Fanuc parameter 5114/5115 default region
ARC_STEP = math.radians(5.0)


@dataclass
class Move:
    line: int
    kind: str  # "rapid" | "feed"
    g0: tuple[float, float, float]  # gauge line, machine frame
    g1: tuple[float, float, float]
    hv: float  # active tool length offset value (0 when G43 is off)
    tool: int | None
    h: int | None
    feed: float  # mm/min
    rpm: float  # 0 when the spindle is stopped
    source: str = ""  # "", "arc", "cycle", "toolchange", "home", "machine"


@dataclass
class Note:
    line: int
    code: str
    severity: str  # "alarm" | "warning" | "info"
    message: str


@dataclass
class LineState:
    """Modal state when a line starts executing (used to build restart blocks)."""

    line: int
    tool: int | None
    h: int | None
    wcs: str
    units: str
    absolute: bool
    motion: int
    feed: float
    rpm: float
    spindle_on: bool
    coolant: bool
    prog: tuple[float, float, float]
    gauge: tuple[float, float, float]
    return_mode: int


@dataclass
class Program:
    lines: list[str]
    moves: list[Move] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    states: dict[int, LineState] = field(default_factory=dict)
    comments: dict[int, list[str]] = field(default_factory=dict)
    end_line: int | None = None


def _split(text: str) -> tuple[str, list[str]]:
    comments = PAREN_COMMENT.findall(text)
    code = PAREN_COMMENT.sub(" ", text)
    if ";" in code:
        code, tail = code.split(";", 1)
        comments.append(tail.strip())
    return code.strip().upper(), [c.strip() for c in comments if c.strip()]


class Interpreter:
    def __init__(self, wcs: dict[str, tuple], h_table: dict[int, float], home=(0.0, 0.0, 0.0),
                 tool_in_spindle: int | None = None):
        self.wcs_table = {k: tuple(map(float, v)) for k, v in wcs.items()}
        self.h_table = {int(k): float(v) for k, v in h_table.items()}
        self.home = tuple(map(float, home))
        self.gauge = list(self.home)
        self.wcs = "G54"
        self.h: int | None = None
        self.tool = tool_in_spindle
        self.next_tool: int | None = None
        self.scale = 1.0  # 25.4 under G20
        self.absolute = True
        self.motion = 0
        self.feed = 0.0
        self.rpm = 0.0
        self.spindle_on = False
        self.coolant = False
        self.return_mode = 98
        self.cycle: dict | None = None
        self.prog_obj: Program | None = None
        self.line = 0

    # --- coordinate helpers -------------------------------------------------
    def hv(self) -> float:
        if self.h is None:
            return 0.0
        return self.h_table.get(self.h, 0.0)

    def offset(self) -> tuple[float, float, float]:
        w = self.wcs_table.get(self.wcs, (0.0, 0.0, 0.0))
        return (w[0], w[1], w[2] + self.hv())

    def prog(self) -> list[float]:
        o = self.offset()
        return [self.gauge[i] - o[i] for i in range(3)]

    def note(self, code: str, severity: str, message: str) -> None:
        self.prog_obj.notes.append(Note(self.line, code, severity, message))

    def _emit(self, g1: list[float], kind: str, source: str = "") -> None:
        g0 = tuple(self.gauge)
        g1 = tuple(float(v) for v in g1)
        if max(abs(g0[i] - g1[i]) for i in range(3)) < 1e-9:
            return
        self.prog_obj.moves.append(Move(
            line=self.line, kind=kind, g0=g0, g1=g1, hv=self.hv(), tool=self.tool, h=self.h,
            feed=self.feed, rpm=self.rpm if self.spindle_on else 0.0, source=source))
        self.gauge = list(g1)

    def move_prog(self, target: list[float], kind: str, source: str = "") -> None:
        o = self.offset()
        self._emit([target[i] + o[i] for i in range(3)], kind, source)

    # --- main loop ----------------------------------------------------------
    def run(self, text: str) -> Program:
        lines = text.splitlines()
        self.prog_obj = Program(lines=lines)
        for idx, raw in enumerate(lines, start=1):
            self.line = idx
            self.prog_obj.states[idx] = LineState(
                line=idx, tool=self.tool, h=self.h, wcs=self.wcs,
                units="inch" if self.scale != 1.0 else "mm", absolute=self.absolute,
                motion=self.motion, feed=self.feed, rpm=self.rpm, spindle_on=self.spindle_on,
                coolant=self.coolant, prog=tuple(self.prog()), gauge=tuple(self.gauge),
                return_mode=self.return_mode)
            code, comments = _split(raw.lstrip("/"))
            if comments:
                self.prog_obj.comments[idx] = comments
            if not code or code.startswith("%") or code.startswith("O"):
                continue
            if any(tok in code for tok in ("#", "IF", "GOTO", "WHILE")):
                self.note("UNSUPPORTED", "warning", "Macro statements are not simulated; this line was skipped.")
                continue
            words = [(m.group(1), float(m.group(2))) for m in WORD.finditer(code)]
            if self._block(words):
                self.prog_obj.end_line = idx
                break
        return self.prog_obj

    def _block(self, words: list[tuple[str, float]]) -> bool:
        g = [v for k, v in words if k == "G"]
        m = [int(v) for k, v in words if k == "M"]
        val = {k: v for k, v in words if k not in ("G", "M", "N")}

        if "F" in val:
            self.feed = val["F"] * self.scale
        if "S" in val:
            self.rpm = val["S"]
        if "T" in val:
            self.next_tool = int(val["T"])

        for code in m:
            if code in (3, 4):
                self.spindle_on = True
            elif code == 8:
                self.coolant = True
            elif code == 9:
                self.coolant = False
            elif code == 6:
                self._tool_change()
            elif code in (98, 99):
                self.note("UNSUPPORTED", "warning", f"M{code} subprogram calls are not simulated.")

        motion_word = None
        nonmodal = None
        for code in g:
            c = round(code, 1)
            if c in (0, 1, 2, 3):
                motion_word = int(c)
            elif c == 80:
                self.motion, self.cycle = 0 if self.motion in CYCLES else self.motion, None
                motion_word = motion_word
            elif c in CYCLES:
                motion_word = int(c)
            elif c == 17:
                pass
            elif c in (18, 19):
                self.note("UNSUPPORTED", "warning", f"G{int(c)} plane arcs are not simulated (only G17).")
            elif c == 20:
                self.scale = 25.4
            elif c == 21:
                self.scale = 1.0
            elif c == 90:
                self.absolute = True
            elif c == 91:
                self.absolute = False
            elif c in (54, 55, 56, 57, 58, 59):
                name = f"G{int(c)}"
                if name not in self.wcs_table:
                    self.note("WCS_NOT_SET", "alarm", f"{name} is used but its offset is not in the setup.")
                self.wcs = name
            elif c in (43, 44):
                h = int(val["H"]) if "H" in val else None
                if h is None:
                    self.note("NO_H_WORD", "alarm", "G43 without an H word.")
                else:
                    if h not in self.h_table:
                        self.note("H_NOT_SET", "alarm", f"H{h} has no length offset value in the setup.")
                    self.h = h
                    if self.tool is not None and h != self.tool:
                        self.note("H_MISMATCH", "warning",
                                  f"G43 H{h} while tool T{self.tool} is loaded; offsets usually follow H = T.")
            elif c == 49:
                self.h = None
            elif c == 40:
                pass
            elif c in (41, 42):
                self.note("UNSUPPORTED", "warning",
                          "Cutter radius compensation is not simulated; the path is treated as the tool centre.")
            elif c in (98, 99):
                self.return_mode = int(c)
            elif c in (28, 53):
                nonmodal = int(c)
            elif c in (4, 94, 15, 69, 64, 61, 50, 67):
                pass
            elif c in (93, 95):
                self.note("UNSUPPORTED", "warning", f"G{int(c)} feed mode is treated as mm/min.")
            elif c in (10, 92, 52, 68, 51):
                self.note("UNSUPPORTED", "warning", f"G{int(c)} changes coordinates and is not simulated.")
            else:
                self.note("UNSUPPORTED", "info", f"G{c:g} is ignored by the simulator.")

        axes = {k: val[k] * self.scale for k in ("X", "Y", "Z") if k in val}

        if nonmodal == 53:
            target = list(self.gauge)
            for i, k in enumerate("XYZ"):
                if k in axes:
                    target[i] = axes[k]
            self._emit(target, "rapid", "machine")
        elif nonmodal == 28:
            cur = self.prog()
            mid = list(cur)
            for i, k in enumerate("XYZ"):
                if k in axes:
                    mid[i] = axes[k] if self.absolute else cur[i] + axes[k]
            self.move_prog(mid, "rapid", "home")
            target = list(self.gauge)
            for i, k in enumerate("XYZ"):
                if k in axes:
                    target[i] = self.home[i]
            self._emit(target, "rapid", "home")
        else:
            if motion_word is not None:
                if motion_word in CYCLES and self.motion not in CYCLES:
                    self.cycle = {"initial": self.prog()[2], "z": None, "r": None, "q": None}
                elif motion_word not in CYCLES:
                    self.cycle = None
                self.motion = motion_word
            if self.motion in CYCLES:
                if axes or "R" in val or motion_word in CYCLES:
                    self._cycle(axes, val)
            elif axes or (self.motion in (2, 3) and ("I" in val or "J" in val)):
                self._linear_or_arc(axes, val)

        for code in m:
            if code == 5:
                self.spindle_on = False
            elif code in (2, 30):
                return True
        return False

    # --- motions ------------------------------------------------------------
    def _target(self, axes: dict[str, float]) -> list[float]:
        cur = self.prog()
        out = list(cur)
        for i, k in enumerate("XYZ"):
            if k in axes:
                out[i] = axes[k] if self.absolute else cur[i] + axes[k]
        return out

    def _linear_or_arc(self, axes: dict[str, float], val: dict[str, float]) -> None:
        target = self._target(axes)
        if self.motion == 0:
            self.move_prog(target, "rapid")
            return
        if self.feed <= 0:
            self.note("NO_FEED", "alarm", "Feed move with no feed rate programmed.")
        if self.motion == 1:
            self.move_prog(target, "feed")
            return
        self._arc(target, val, clockwise=self.motion == 2)

    def _arc(self, end: list[float], val: dict[str, float], clockwise: bool) -> None:
        start = self.prog()
        sx, sy = start[0], start[1]
        ex, ey = end[0], end[1]
        if "R" in val:
            r = val["R"] * self.scale
            dx, dy = ex - sx, ey - sy
            d = math.hypot(dx, dy)
            if d < 1e-9:
                self.note("ARC_ERROR", "alarm", "R-format arc with identical start and end point.")
                self.move_prog(end, "feed", "arc")
                return
            half = d / 2
            if abs(r) < half - 1e-3:
                self.note("ARC_ERROR", "alarm", f"Arc radius {abs(r):.3f} is smaller than half the chord {half:.3f}.")
            h = math.sqrt(max(r * r - half * half, 0.0))
            lx, ly = -dy / d, dx / d
            side = 1.0 if (not clockwise) else -1.0
            if r < 0:
                side = -side
            cx, cy = sx + dx / 2 + side * h * lx, sy + dy / 2 + side * h * ly
        else:
            i = val.get("I", 0.0) * self.scale
            j = val.get("J", 0.0) * self.scale
            cx, cy = sx + i, sy + j
        r0 = math.hypot(sx - cx, sy - cy)
        r1 = math.hypot(ex - cx, ey - cy)
        if abs(r0 - r1) > 0.01:
            self.note("ARC_ERROR", "alarm",
                      f"Arc end point is off the circle: start radius {r0:.3f}, end radius {r1:.3f}.")
        a0 = math.atan2(sy - cy, sx - cx)
        a1 = math.atan2(ey - cy, ex - cx)
        sweep = a1 - a0
        full = math.hypot(ex - sx, ey - sy) < 1e-6
        if clockwise:
            if sweep >= 0 or full:
                sweep -= 2 * math.pi
            if full:
                sweep = -2 * math.pi
        else:
            if sweep <= 0 or full:
                sweep += 2 * math.pi
            if full:
                sweep = 2 * math.pi
        n = max(2, math.ceil(abs(sweep) / ARC_STEP))
        for k in range(1, n + 1):
            t = k / n
            a = a0 + sweep * t
            rr = r0 + (r1 - r0) * t
            p = [cx + rr * math.cos(a), cy + rr * math.sin(a), start[2] + (end[2] - start[2]) * t]
            if k == n:
                p = list(end)
            self.move_prog(p, "feed", "arc")

    def _cycle(self, axes: dict[str, float], val: dict[str, float]) -> None:
        cyc = self.cycle
        cur = self.prog()
        initial = cyc["initial"]
        if self.absolute:
            x = axes.get("X", cur[0])
            y = axes.get("Y", cur[1])
            if "R" in val:
                cyc["r"] = val["R"] * self.scale
            if "Z" in axes:
                cyc["z"] = axes["Z"]
        else:
            x = cur[0] + axes.get("X", 0.0)
            y = cur[1] + axes.get("Y", 0.0)
            if "R" in val:
                cyc["r"] = initial + val["R"] * self.scale
            if "Z" in axes:
                cyc["z"] = (cyc["r"] if cyc["r"] is not None else initial) + axes["Z"]
        if "Q" in val:
            cyc["q"] = abs(val["Q"]) * self.scale
        r, z, q = cyc["r"], cyc["z"], cyc["q"]
        if r is None or z is None:
            self.note("CYCLE_ERROR", "alarm", "Canned cycle without R or Z.")
            return
        if self.feed <= 0:
            self.note("NO_FEED", "alarm", "Canned cycle with no feed rate programmed.")
        self.move_prog([x, y, cur[2]], "rapid", "cycle")
        self.move_prog([x, y, r], "rapid", "cycle")
        kind = self.motion
        if kind in (83, 73) and q:
            depth = r
            first = True
            while depth > z + 1e-9:
                nxt = max(depth - q, z)
                if kind == 83 and not first:
                    self.move_prog([x, y, depth + PECK_CLEARANCE], "rapid", "cycle")
                self.move_prog([x, y, nxt], "feed", "cycle")
                if nxt > z + 1e-9:
                    if kind == 83:
                        self.move_prog([x, y, r], "rapid", "cycle")
                    else:
                        self.move_prog([x, y, nxt + PECK_CLEARANCE], "rapid", "cycle")
                depth = nxt
                first = False
        else:
            if kind in (83, 73):
                self.note("CYCLE_ERROR", "warning", f"G{kind} without Q; treated as a single plunge.")
            self.move_prog([x, y, z], "feed", "cycle")
        back = r if self.return_mode == 99 else initial
        if kind in (84, 85, 89):
            self.move_prog([x, y, r], "feed", "cycle")
        self.move_prog([x, y, back], "rapid", "cycle")

    def _tool_change(self) -> None:
        if self.next_tool is None:
            self.note("TOOL_CHANGE", "warning", "M6 with no tool selected.")
            return
        target = list(self.gauge)
        target[2] = self.home[2]
        self._emit(target, "rapid", "toolchange")
        self.tool = self.next_tool


def interpret(text: str, wcs: dict, h_table: dict, home=(0.0, 0.0, 0.0), tool_in_spindle=None) -> Program:
    return Interpreter(wcs, h_table, home, tool_in_spindle).run(text)
