"""Built-in demo scenarios: the clean bracket program plus realistic shop-floor mistakes."""
from __future__ import annotations

import json
from pathlib import Path

from bench.defects import inject

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

STORIES = [
    ("clean", None, "Clean program", "CAM output exactly as posted, setup as planned."),
    ("g99", "g98_to_g99", "Drill cycle returns to R plane",
     "Someone changed G98 to G99 to save cycle time. The drill now travels between holes 2 mm above the part."),
    ("no_g43", "drop_tlo", "Length offset deleted at the control",
     "An operator edited the program at the machine and removed the G43 block."),
    ("wrong_h", "wrong_h_shorter", "Wrong H register",
     "G43 calls the offset of a shorter tool than the one in the spindle."),
    ("rapid", "rapid_plunge", "Rapid plunge",
     "A feed move into the part became G0 during a hand edit."),
    ("no_m3", "spindle_not_started", "Spindle never started",
     "The S/M3 block after a tool change is missing."),
    ("depth", "depth_typo", "Depth typo",
     "A cutting depth was typed 2 mm too deep."),
    ("safe_z", "low_safe_z", "Safe height typo",
     "Z25. became Z2.5 on the drill's approach, so the cycle's initial level is below the clamps."),
    ("wrong_tool", "wrong_tool_in_pocket", "Wrong end mill in the magazine",
     "Pocket 3 holds an 8 mm end mill; the program was written for 6 mm."),
    ("short_tool", "tool_too_short", "Tool loaded too short",
     "A tool was set with almost no stickout, so the holder reaches the part."),
    ("clamp", "clamp_moved", "Clamp moved",
     "The setup sheet was not followed: a clamp sits over a hole position."),
]


def base() -> tuple[str, dict]:
    text = (EXAMPLES / "bracket.nc").read_text(encoding="utf-8")
    setup = json.loads((EXAMPLES / "bracket.setup.json").read_text(encoding="utf-8"))
    return text, setup


def all_scenarios() -> list[dict]:
    text, setup = base()
    out = []
    for sid, inj, title, story in STORIES:
        if inj is None:
            prog, s = text, setup
        else:
            res = inject(inj, text, setup, seed=0)
            if res is None:
                continue
            prog, s, _exp = res
        out.append({"id": sid, "title": title, "story": story, "program": prog, "setup": s})
    return out
