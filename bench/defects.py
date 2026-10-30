"""Defect injectors: realistic shop-floor mistakes applied to a clean program or setup.

Each injector returns (program, setup_dict, expected_codes) or None when the
program has no place to apply it. Expected codes are what a correct checker
must report (any one of them counts as a detection).
"""
from __future__ import annotations

import copy
import random
import re

CRASH = {"RAPID_INTO_MATERIAL", "FIXTURE_COLLISION", "HOLDER_COLLISION", "SHANK_COLLISION",
         "SPINDLE_COLLISION", "CUT_SPINDLE_OFF"}


def _lines(text):
    return text.splitlines()


def _join(lines):
    return "\n".join(lines) + "\n"


def _pick(rng, idxs):
    return rng.choice(idxs) if idxs else None


def g98_to_g99(text, setup, rng):
    """Drill cycle returns to the R plane instead of the initial level and crosses a clamp."""
    ls = _lines(text)
    idx = [i for i, l in enumerate(ls) if "G98" in l and re.search(r"G8[1-3]|G73", l)]
    i = _pick(rng, idx)
    if i is None:
        return None
    ls[i] = ls[i].replace("G98", "G99")
    return _join(ls), setup, {"FIXTURE_COLLISION"}


def drop_tlo(text, setup, rng):
    """Operator deletes the G43 line by hand; the tool comes down a full tool length too low."""
    ls = _lines(text)
    idx = [i for i, l in enumerate(ls) if re.search(r"G43\b", l)]
    i = _pick(rng, idx)
    if i is None:
        return None
    ls[i] = re.sub(r"\s*G43\s*", " ", ls[i])
    ls[i] = re.sub(r"\s*H\d+", "", ls[i])
    return _join(ls), setup, CRASH | {"NO_TLO", "TLO_OFFSET_ERROR", "INTENT_MISMATCH", "GOUGE"}


def wrong_h_shorter(text, setup, rng):
    """G43 calls the offset of a shorter tool: the loaded tool is driven too deep."""
    ls = _lines(text)
    tools = setup["tools"]
    cands = []
    for i, l in enumerate(ls):
        m = re.search(r"H(\d+)", l)
        if m and "G43" in l:
            h = int(m.group(1))
            shorter = [int(k) for k, t in tools.items() if t["length"] < tools[str(h)]["length"] - 5]
            if shorter:
                cands.append((i, h, shorter))
    if not cands:
        return None
    i, h, shorter = rng.choice(cands)
    ls[i] = re.sub(r"H\d+", f"H{rng.choice(shorter)}", ls[i])
    return _join(ls), setup, CRASH | {"GOUGE", "H_MISMATCH"}


def rapid_plunge(text, setup, rng):
    """A feed move into material is changed to a rapid (G1 -> G0)."""
    ls = _lines(text)
    idx = [i for i, l in enumerate(ls) if re.search(r"G1 Z-", l)]
    i = _pick(rng, idx)
    if i is None:
        return None
    ls[i] = ls[i].replace("G1 Z-", "G0 Z-", 1)
    return _join(ls), setup, {"RAPID_INTO_MATERIAL"}


def spindle_not_started(text, setup, rng):
    """The M3 after a tool change is missing."""
    ls = _lines(text)
    idx = [i for i, l in enumerate(ls) if re.search(r"\bM0?3\b", l)]
    i = _pick(rng, idx)
    if i is None:
        return None
    ls[i] = re.sub(r"\s*\bM0?3\b", "", ls[i])
    return _join(ls), setup, {"CUT_SPINDLE_OFF"}


def depth_typo(text, setup, rng):
    """A cutting depth is mistyped 2 mm deeper than the part allows."""
    ls = _lines(text)
    idx = [i for i, l in enumerate(ls) if re.search(r"G1 Z-\d", l)]
    i = _pick(rng, idx)
    if i is None:
        return None

    def deeper(m):
        return f"Z{float(m.group(1)) - 2.0:.3f}".rstrip("0")

    ls[i] = re.sub(r"Z(-\d+\.?\d*)", deeper, ls[i], count=1)
    return _join(ls), setup, {"GOUGE"}


def low_safe_z(text, setup, rng):
    """The safe height before a drill cycle is mistyped (25. -> 2.5) so the cycle's initial level is below the clamps."""
    ls = _lines(text)
    idx = [i for i, l in enumerate(ls) if re.search(r"G43 Z\d+\.\d* H", l)]
    drill_idx = [i for i in idx if any("G83" in ls[j] or "G81" in ls[j] for j in range(i + 1, min(i + 3, len(ls))))]
    i = _pick(rng, drill_idx)
    if i is None:
        return None
    m = re.search(r"G43 Z(\d+\.\d*)", ls[i])
    slipped = float(m.group(1)) / 10  # decimal point typed one place too far left
    ls[i] = ls[i].replace(f"Z{m.group(1)}", f"Z{slipped:g}" + ("" if "." in f"{slipped:g}" else "."), 1)
    return _join(ls), setup, {"FIXTURE_COLLISION"}


def wrong_tool_in_pocket(text, setup, rng):
    """Setup error: the magazine pocket holds a larger end mill than the CAM program assumes."""
    s = copy.deepcopy(setup)
    flats = [k for k, t in s["cam_tools"].items() if t["kind"] == "flat"]
    if not flats:
        return None
    k = rng.choice(flats)
    s["tools"][k]["diameter"] = s["cam_tools"][k]["diameter"] + 2.0
    return text, s, {"GOUGE", "TOOL_MISMATCH"}


def tool_too_short(text, setup, rng):
    """Setup error: a tool was loaded with too little stickout, so the holder reaches the part."""
    s = copy.deepcopy(setup)
    deep = {k: t for k, t in s["tools"].items() if k in s["cam_tools"]}
    if not deep:
        return None
    k = rng.choice(sorted(deep))
    s["tools"][k]["stickout"] = 3.0
    s["tools"][k]["flute_length"] = min(s["tools"][k]["flute_length"], 2.5)
    return text, s, {"HOLDER_COLLISION", "SHANK_COLLISION", "TOOL_MISMATCH"}


def clamp_moved(text, setup, rng):
    """Setup error: a clamp is placed over a drilled hole position."""
    s = copy.deepcopy(setup)
    holes = [f for f in s["features"] if f["type"] == "hole"]
    clamps = [i for i, f in enumerate(s["fixtures"]) if "clamp" in f["name"]]
    if not holes or not clamps:
        return None
    h = rng.choice(holes)
    c = s["fixtures"][rng.choice(clamps)]
    w, d = c["x1"] - c["x0"], c["y1"] - c["y0"]
    c["x0"], c["x1"] = h["x"] - w / 2, h["x"] + w / 2
    c["y0"], c["y1"] = h["y"] - d / 2, h["y"] + d / 2
    return text, s, {"FIXTURE_COLLISION"}


# --- ground truth ---------------------------------------------------------------
# Independent of the simulator: plain geometry on the setup sheet and the cycle block, so the benchmark does not
# grade the simulator against itself. An injected mistake that cannot hurt this particular part is "harmless" and
# the correct answer for it is GO.

def _cycle_travel(text):
    """Hole order, return height and drill radius taken directly from the drilling block."""
    ls = _lines(text)
    for i, l in enumerate(ls):
        m = re.search(r"G9([89]) G8[13] X(-?[\d.]+) Y(-?[\d.]+) Z(-?[\d.]+) R(-?[\d.]+)", l)
        if not m:
            continue
        mode, r = m.group(1), float(m.group(5))
        init = None
        for j in range(i - 1, -1, -1):
            z = re.search(r"Z(-?[\d.]+)", ls[j])
            if z:
                init = float(z.group(1))
                break
        holes = [(float(m.group(2)), float(m.group(3)))]
        for l2 in ls[i + 1:]:
            m2 = re.search(r"^N\d+ X(-?[\d.]+) Y(-?[\d.]+)\s*$", l2.strip())
            if not m2:
                break
            holes.append((float(m2.group(1)), float(m2.group(2))))
        return holes, (r if mode == "9" else init)
    return [], None


def _segment_near_box(a, b, box, pad):
    x0, x1, y0, y1 = box["x0"] - pad, box["x1"] + pad, box["y0"] - pad, box["y1"] + pad
    for k in range(201):
        t = k / 200
        x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        if x0 <= x <= x1 and y0 <= y <= y1:
            return True
    return False


def drill_travel_hits_clamp(text, setup):
    holes, z_ret = _cycle_travel(text)
    drill = next(t for t in setup["tools"].values() if t["kind"] == "drill")
    for fx in setup["fixtures"]:
        if "clamp" not in fx["name"] or z_ret is None or z_ret >= fx["z1"]:
            continue
        for a, b in zip(holes, holes[1:]):
            if _segment_near_box(a, b, fx, drill["diameter"] / 2):
                return True
    return False


def harmful(name, text, setup):
    if name in ("g98_to_g99", "low_safe_z"):
        return drill_travel_hits_clamp(text, setup)
    if name == "tool_too_short":
        # from the CAM setup sheet: the holder face goes below the stock top, or one pass is deeper than the flutes
        k = next(k for k, t in setup["tools"].items() if t["stickout"] == 3.0)
        t = setup["tools"][k]
        op = next(o for o in setup["operations"] if str(o["tool"]) == str(k))
        return op["max_depth"] > t["stickout"] or op["max_step"] > t["flute_length"]
    return True


INJECTORS = {
    "g98_to_g99": g98_to_g99,
    "drop_tlo": drop_tlo,
    "wrong_h_shorter": wrong_h_shorter,
    "rapid_plunge": rapid_plunge,
    "spindle_not_started": spindle_not_started,
    "depth_typo": depth_typo,
    "low_safe_z": low_safe_z,
    "wrong_tool_in_pocket": wrong_tool_in_pocket,
    "tool_too_short": tool_too_short,
    "clamp_moved": clamp_moved,
}


def inject(name, text, setup, seed=0):
    return INJECTORS[name](text, setup, random.Random(seed))
