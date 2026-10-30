"""Generate CAM-style Fanuc programs and matching machine setups.

The default part is an actuator mount bracket, OP1: a 120 x 80 x 30 mm 7075-T6 aluminium block held by
two strap clamps:
  T1 10 mm flat end mill  pocket 50 x 40 x 8 mm, helical entry, zig-zag rough, finish contour
  T2 6.8 mm drill         four holes 20 mm deep, peck drilling (G83), return to initial level (G98)
  T3 6 mm flat end mill   slot 40 mm long, 3 mm deep, two passes
Work zero (G54) is the top, front, left corner of the stock.

`variant(seed)` draws a different part of the same family (stock size, pocket, holes, slot and clamp
positions, depths, safe height) for the benchmark.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

HERE = Path(__file__).parent

TOOLS = {
    1: dict(kind="flat", diameter=10.0, flute_length=22.0, shank_diameter=10.0, holder_diameter=42.0,
            stickout=32.0, length=98.214, flutes=3, description="10 mm 3-flute carbide end mill"),
    2: dict(kind="drill", diameter=6.8, flute_length=45.0, shank_diameter=6.8, holder_diameter=30.0,
            stickout=60.0, length=131.552, flutes=2, description="6.8 mm carbide drill (M8 tap drill)"),
    3: dict(kind="flat", diameter=6.0, flute_length=13.0, shank_diameter=6.0, holder_diameter=32.0,
            stickout=26.0, length=89.807, flutes=3, description="6 mm 3-flute carbide end mill"),
    4: dict(kind="ball", diameter=8.0, flute_length=16.0, shank_diameter=8.0, holder_diameter=32.0,
            stickout=30.0, length=92.415, flutes=2, description="8 mm ball end mill"),
    5: dict(kind="flat", diameter=8.0, flute_length=19.0, shank_diameter=8.0, holder_diameter=32.0,
            stickout=30.0, length=94.120, flutes=3, description="8 mm 3-flute carbide end mill"),
}

DEFAULT = dict(
    name="Actuator mount bracket OP1 (7075-T6) - 3-axis VMC, two strap clamps",
    stock=(120.0, 80.0, 30.0),
    pocket=dict(x0=35.0, x1=85.0, y0=20.0, y1=60.0, depth=-8.0),
    holes=[(20.0, 12.0), (100.0, 12.0), (100.0, 68.0), (20.0, 68.0)],
    hole_depth=-20.0,
    slot=dict(x0=40.0, x1=80.0, y=8.0, depth=-3.0),
    clamps=[(-5.0, 28.0, 32.0, 48.0), (92.0, 125.0, 32.0, 48.0)],
    clamp_top=14.0,
    safe_z=25.0,
    clear_z=2.0,
    stepdown=4.0,
)


def f(v: float) -> str:
    s = f"{v:.3f}".rstrip("0")
    if s in ("-0.", "-0"):
        s = "0."
    return s


class Post:
    def __init__(self):
        self.out: list[str] = []
        self.n = 0

    def raw(self, s: str) -> None:
        self.out.append(s)

    def b(self, s: str) -> None:
        self.n += 5
        self.out.append(f"N{self.n} {s}")


def pocket_ops(p: Post, P: dict) -> None:
    pk = P["pocket"]
    r = TOOLS[1]["diameter"] / 2
    cx, cy = (pk["x0"] + pk["x1"]) / 2, (pk["y0"] + pk["y1"]) / 2
    fx0, fx1, fy0, fy1 = pk["x0"] + r, pk["x1"] - r, pk["y0"] + r, pk["y1"] - r
    leave = 0.2
    rx0, rx1, ry0, ry1 = fx0 + leave, fx1 - leave, fy0 + leave, fy1 - leave
    levels = []
    z = 0.0
    while z > pk["depth"] + 1e-9:
        z = max(z - P["stepdown"], pk["depth"])
        levels.append(z)
    p.raw("(POCKET1 - ROUGH)")
    p.b("T1 M6")
    p.b("S9500 M3")
    p.b(f"G54 G0 X{f(cx + 3)} Y{f(cy)}")
    p.b(f"G43 Z{f(P['safe_z'])} H1 M8")
    p.b(f"G0 Z{f(P['clear_z'])}")
    z = 0.5
    p.b(f"G1 Z{f(z)} F300.")
    for level in levels:
        while z > level + 1e-9:
            z = max(z - 1.0, level)
            p.b(f"G3 X{f(cx + 3)} Y{f(cy)} Z{f(z)} I-3. J0. F600.")
        p.b(f"G3 X{f(cx + 3)} Y{f(cy)} I-3. J0.")
        p.b(f"G1 X{f(rx1)} Y{f(cy)} F1500.")
        ys, y = [], ry0
        while y < ry1 - 1e-9:
            ys.append(y)
            y += 6.0
        ys.append(ry1)
        p.b(f"G1 Y{f(ys[0])}")
        left = False
        for i, yy in enumerate(ys):
            if i:
                p.b(f"G1 Y{f(yy)}")
            p.b(f"G1 X{f(rx0 if not left else rx1)}")
            left = not left
        p.b(f"G1 X{f(rx0)} Y{f(ry0)}")
        p.b(f"G1 X{f(rx1)}")
        p.b(f"G1 Y{f(ry1)}")
        p.b(f"G1 X{f(rx0)}")
        p.b(f"G1 Y{f(ry0)}")
        p.b(f"G1 X{f(cx + 3)} Y{f(cy)}")
    p.raw("(POCKET1 - FINISH WALLS)")
    p.b(f"G1 X{f(cx)} Y{f(fy0 + 2)} F800.")
    p.b(f"G1 Y{f(fy0)}")
    p.b(f"G1 X{f(fx0)}")
    p.b(f"G1 Y{f(fy1)}")
    p.b(f"G1 X{f(fx1)}")
    p.b(f"G1 Y{f(fy0)}")
    p.b(f"G1 X{f(cx)}")
    p.b(f"G1 Y{f(fy0 + 2)}")
    p.b(f"G0 Z{f(P['safe_z'])}")
    p.b("M9")


def drill_ops(p: Post, P: dict) -> None:
    holes = P["holes"]
    p.raw("(DRILL1 - M8 TAP DRILL)")
    p.b("M5")
    p.b("T2 M6")
    p.b("S4200 M3")
    p.b(f"G54 G0 X{f(holes[0][0])} Y{f(holes[0][1])}")
    p.b(f"G43 Z{f(P['safe_z'])} H2 M8")
    x, y = holes[0]
    p.b(f"G98 G83 X{f(x)} Y{f(y)} Z{f(P['hole_depth'])} R{f(P['clear_z'])} Q4. F350.")
    for x, y in holes[1:]:
        p.b(f"X{f(x)} Y{f(y)}")
    p.b("G80")
    p.b(f"G0 Z{f(P['safe_z'])}")
    p.b("M9")


def slot_ops(p: Post, P: dict) -> None:
    sl = P["slot"]
    p.raw("(SLOT1)")
    p.b("M5")
    p.b("T3 M6")
    p.b("S10000 M3")
    p.b(f"G54 G0 X{f(sl['x0'])} Y{f(sl['y'])}")
    p.b(f"G43 Z{f(P['safe_z'])} H3 M8")
    p.b(f"G0 Z{f(P['clear_z'])}")
    zs = [-1.5 * (k + 1) for k in range(int(round(-sl["depth"] / 1.5)))]
    zs[-1] = sl["depth"]
    for i, z in enumerate(zs):
        end = sl["x1"] if i % 2 == 0 else sl["x0"]
        p.b(f"G1 Z{f(z)} F250.")
        p.b(f"G1 X{f(end)} F900.")
    p.b(f"G0 Z{f(P['safe_z'])}")
    p.b("M9")


def program(P: dict = DEFAULT) -> str:
    sx, sy, sz = P["stock"]
    p = Post()
    p.raw("%")
    p.raw("O1001 (ACTUATOR MOUNT BRACKET OP1)")
    p.raw("(MACHINE: 3-AXIS VMC - CONTROL: FANUC 0I-MF)")
    p.raw(f"(STOCK: {f(sx)} X {f(sy)} X {f(sz)} AL7075-T6 - WCS G54 TOP FRONT LEFT CORNER)")
    for n in (1, 2, 3):
        t = TOOLS[n]
        p.raw(f"(T{n} D={f(t['diameter'])} {t['kind'].upper()} - LOC {f(t['flute_length'])} - {t['description'].upper()})")
    p.b("G90 G94 G17 G49 G40 G80")
    p.b("G21")
    p.b("G28 G91 Z0.")
    p.b("G90")
    pocket_ops(p, P)
    drill_ops(p, P)
    slot_ops(p, P)
    p.b("M5")
    p.b("G28 G91 Z0.")
    p.b("G90")
    p.b("G53 G0 Y0.")
    p.b("M30")
    p.raw("%")
    return "\n".join(p.out) + "\n"


def setup(P: dict = DEFAULT) -> dict:
    sx, sy, sz = P["stock"]
    pk, sl = P["pocket"], P["slot"]
    return {
        "name": P["name"],
        "stock": {"name": "stock", "x0": 0.0, "x1": sx, "y0": 0.0, "y1": sy, "z0": -sz, "z1": 0.0},
        "tools": {str(k): v for k, v in TOOLS.items()},
        "cam_tools": {str(k): TOOLS[k] for k in (1, 2, 3)},
        "wcs": {"G54": [-412.37, -203.55, -361.8]},
        "fixtures": [
            *[{"name": name, "x0": c[0], "x1": c[1], "y0": c[2], "y1": c[3], "z0": 0.0, "z1": P["clamp_top"]}
              for name, c in zip(("left strap clamp", "right strap clamp"), P["clamps"])],
            {"name": "machine table", "x0": -150.0, "x1": sx + 150.0, "y0": -80.0, "y1": sy + 80.0,
             "z0": -sz - 50.0, "z1": -sz},
        ],
        "features": [
            {"type": "pocket", "x0": pk["x0"], "x1": pk["x1"], "y0": pk["y0"], "y1": pk["y1"],
             "z": pk["depth"], "corner_r": TOOLS[1]["diameter"] / 2},
            *[{"type": "hole", "x": x, "y": y, "d": TOOLS[2]["diameter"], "z": P["hole_depth"]} for x, y in P["holes"]],
            {"type": "slot", "x0": sl["x0"], "y0": sl["y"], "x1": sl["x1"], "y1": sl["y"],
             "w": TOOLS[3]["diameter"], "z": sl["depth"]},
        ],
        "operations": [
            {"tool": 1, "name": "POCKET1", "max_depth": -pk["depth"], "max_step": P["stepdown"]},
            {"tool": 2, "name": "DRILL1", "max_depth": -P["hole_depth"], "max_step": 4.0},
            {"tool": 3, "name": "SLOT1", "max_depth": -sl["depth"], "max_step": 1.5},
        ],
        "travel": {"x": [-760.0, 0.0], "y": [-410.0, 0.0], "z": [-510.0, 0.0]},
        "home": [0.0, 0.0, 0.0],
        "spindle_diameter": 90.0,
        "tool_in_spindle": None,
        "primary_wcs": "G54",
        "tolerance": 0.05,
        "resolution": 0.5,
    }


def variant(seed: int) -> dict:
    """A different part of the same family; every variant is a valid, collision-free job by construction."""
    rng = random.Random(seed)
    sx = rng.choice([100.0, 110.0, 120.0, 130.0, 140.0, 150.0])
    sy = rng.choice([70.0, 80.0, 90.0, 100.0])
    sz = rng.choice([25.0, 30.0, 35.0, 40.0])
    ch = rng.choice([14.0, 16.0, 20.0])  # clamp width in Y
    cy0 = sy / 2 - ch / 2
    pw = rng.uniform(0.32, 0.45) * sx
    ph = rng.uniform(0.38, 0.52) * sy
    px0 = sx / 2 - pw / 2 + rng.uniform(-3, 3)
    py0 = sy / 2 - ph / 2 + rng.uniform(-2, 2)
    reach = min(rng.choice([19.0, 23.0, 28.0]), px0 - 6.0, sx - (px0 + pw) - 6.0)  # clamp overlap onto the stock
    cw = reach + 5.0
    inset_x = reach - rng.uniform(4, 7)
    inset_y = rng.uniform(9, 12)
    holes = [(inset_x, inset_y), (sx - inset_x, inset_y), (sx - inset_x, sy - inset_y), (inset_x, sy - inset_y)]
    rng.shuffle(holes)
    slot_y = rng.uniform(5.5, py0 - 8.0) if py0 > 16 else py0 - 6.0
    clamp_top = rng.choice([12.0, 14.0, 18.0])
    return dict(
        name=f"Variant {seed} - {sx:g} x {sy:g} x {sz:g} block, two strap clamps",
        stock=(sx, sy, sz),
        pocket=dict(x0=round(px0, 1), x1=round(px0 + pw, 1), y0=round(py0, 1), y1=round(py0 + ph, 1),
                    depth=-rng.choice([5.0, 6.0, 8.0, 10.0])),
        holes=[(round(x, 1), round(y, 1)) for x, y in holes],
        hole_depth=-rng.choice([12.0, 15.0, 18.0, min(20.0, sz - 6)]),
        slot=dict(x0=round(px0 + 3, 1), x1=round(px0 + pw - 3, 1), y=round(slot_y, 1), depth=-rng.choice([1.5, 3.0])),
        clamps=[(-5.0, cw - 5.0, cy0, cy0 + ch), (sx - cw + 5.0, sx + 5.0, cy0, cy0 + ch)],
        clamp_top=clamp_top,
        safe_z=clamp_top + rng.choice([8.0, 11.0, 16.0]),
        clear_z=rng.choice([1.0, 2.0, 3.0]),
        stepdown=rng.choice([3.0, 4.0, 5.0]),
    )


if __name__ == "__main__":
    (HERE / "bracket.nc").write_text(program(), encoding="utf-8")
    (HERE / "bracket.setup.json").write_text(json.dumps(setup(), indent=2), encoding="utf-8")
    print("wrote", HERE / "bracket.nc", HERE / "bracket.setup.json")
