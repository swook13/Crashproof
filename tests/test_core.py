import math

import numpy as np

from examples.make_examples import program, setup
from crashproof.core import analyze, apply_edits, prove
from crashproof.gcode import interpret
from crashproof.setup import Box, Setup, Tool
from crashproof.sim import simulate

WCS = {"G54": [-400.0, -200.0, -350.0]}


def tip_path(prog, length=100.0):
    w = WCS["G54"]
    return [(m.g1[0] - w[0], m.g1[1] - w[1], m.g1[2] - w[2] - length) for m in prog.moves]


def test_full_circle_returns_to_start():
    p = interpret("G90 G54 G43 H1 G0 X10 Y0 Z5\nG1 F100\nG2 X10 Y0 I-10 J0\n", WCS, {1: 100.0})
    pts = tip_path(p)
    assert math.isclose(pts[-1][0], 10, abs_tol=1e-9) and math.isclose(pts[-1][1], 0, abs_tol=1e-9)
    radii = [math.hypot(x, y) for x, y, _ in pts[1:]]
    assert max(abs(r - 10) for r in radii) < 1e-6


def test_r_format_center_side():
    # G2 (clockwise) short arc from (0,0) to (10,0), R10: centre below the chord, arc bulges upward
    p = interpret("G90 G54 G43 H1 G0 X0 Y0 Z5\nG2 X10 Y0 R10 F100\n", WCS, {1: 100.0})
    ys = [y for _, y, _ in tip_path(p)[1:]]
    assert max(ys) > 1.3


def test_peck_cycle_expansion():
    p = interpret("G90 G54 G43 H1 G0 X0 Y0 Z25\nG98 G83 X0 Y0 Z-10 R2 Q4 F100\nG80\n", WCS, {1: 100.0})
    feeds = [m for m in p.moves if m.kind == "feed"]
    assert len(feeds) == 3  # 2 -> -2 -> -6 -> -10
    assert math.isclose(tip_path(p)[-1][2], 25.0, abs_tol=1e-9)  # G98 returns to the initial level


def test_length_offset_physics():
    text = "G90 G54 T1 M6\nG43 H2 G0 X0 Y0 Z10\n"
    p = interpret(text, WCS, {1: 100.0, 2: 90.0})
    w = WCS["G54"]
    gz = p.moves[-1].g1[2]
    assert math.isclose(gz - w[2] - 100.0, 10 - 10.0, abs_tol=1e-9)  # loaded tool is 10 mm longer than H2 says


def _block_setup():
    tool = Tool(1, "flat", 10.0, 30.0, 10.0, 40.0, 40.0, 100.0)
    return Setup(name="block", stock=Box("stock", 0, 100, 0, 100, -30, 0), tools={1: tool}, wcs=WCS,
                 resolution=0.25, features=[{"type": "pocket", "x0": 20, "x1": 80, "y0": 20, "y1": 80, "z": -5,
                                             "corner_r": 5}])


def test_removed_volume_matches_geometry():
    lines = ["G90 G54 T1 M6", "S8000 M3", "G43 H1 G0 X25 Y25 Z5", "G1 Z-5 F300"]
    y = 25.0
    while y <= 75.0 + 1e-9:
        lines.append(f"G1 X75 Y{y:.3f}" if int((y - 25) / 5) % 2 == 0 else f"G1 X25 Y{y:.3f}")
        y += 5.0
        if y <= 75.0 + 1e-9:
            lines.append(f"G1 Y{y:.3f}")
    lines += ["G1 X25 Y25", "G1 X75", "G1 Y75", "G1 X25", "G1 Y25", "G0 Z5", "M30"]
    s = _block_setup()
    r = simulate("\n".join(lines), s, "real")
    exact = (60 * 60 - (4 - math.pi) * 25) * 5  # 60 x 60 pocket with R5 corners, 5 deep
    assert abs(r.removed - exact) / exact < 0.01
    assert r.verdict == "GO"


def test_clean_example_is_go_and_real_equals_intent():
    rep = analyze(program(), Setup.from_dict(setup()))
    assert rep["verdict"] == "GO"
    assert np.abs(rep["real"].heights - rep["ideal"].heights).max() == 0.0


def test_proof_rejects_a_fix_that_changes_the_part():
    text = program().replace("G98 G83", "G99 G83")
    s = Setup.from_dict(setup())
    ln = next(i for i, l in enumerate(text.splitlines(), 1) if "G99 G83" in l)
    good = apply_edits(text, [{"op": "replace", "line": ln, "text": text.splitlines()[ln - 1].replace("G99", "G98")}])
    bad = apply_edits(text, [{"op": "delete", "line": ln}])
    assert prove(text, good, s)["accepted"]
    assert not prove(text, bad, s)["accepted"]
