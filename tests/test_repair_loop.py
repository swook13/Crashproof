"""The repair loop with a scripted stand-in for the model: only proved candidates may be accepted."""
import json
from pathlib import Path

from bench.defects import inject
from crashproof import agent
from crashproof.core import analyze
from crashproof.setup import Setup

ROOT = Path(__file__).resolve().parent.parent


def _base():
    text = (ROOT / "examples" / "bracket.nc").read_text(encoding="utf-8")
    setup = json.loads((ROOT / "examples" / "bracket.setup.json").read_text(encoding="utf-8"))
    return text, setup


def _run(monkeypatch, text, setup, replies):
    monkeypatch.setattr(agent, "client", lambda: object())
    queue = list(replies)

    def fake_ask(cl, model, txt, s, issues, feedback, temperature):
        c = agent.Candidate(model="scripted", temperature=temperature)
        r = queue.pop(0) if queue else {}
        c.edits, c.setup_changes = r.get("edits", []), r.get("setup_changes", [])
        c.setup_actions = r.get("setup_actions", [])
        return c

    monkeypatch.setattr(agent, "ask", fake_ask)
    s = Setup.from_dict(setup)
    issues = [i.to_dict() for i in analyze(text, s)["issues"]]
    return agent.repair(text, s, issues, rounds=1, n=3)


def test_code_fix_proved_and_minimal(monkeypatch):
    text, setup = _base()
    bad, s, _ = inject("g98_to_g99", text, setup, 0)
    ln = next(i for i, l in enumerate(bad.splitlines(), 1) if "G99" in l)
    line = bad.splitlines()[ln - 1]
    res = _run(monkeypatch, bad, s, [
        {"edits": [{"op": "delete", "line": ln}]},  # removes the holes: must be rejected
        {"edits": [{"op": "replace", "line": ln, "text": line.replace("G99", "G98")}]},
        {"edits": [{"op": "replace", "line": ln, "text": line.replace("G99", "G98").replace("R2.", "R20.")}]},
    ])
    assert res["status"] == "proved"
    assert res["chosen"]["candidate"] == 2
    assert [h["result"] for h in res["history"]] == ["rejected", "proved", "proved"]


def test_setup_fix_proved_when_code_cannot_fix(monkeypatch):
    text, setup = _base()
    prog, s, _ = inject("wrong_tool_in_pocket", text, setup, 0)
    res = _run(monkeypatch, prog, s, [
        {"edits": [{"op": "replace", "line": 5, "text": "(T3 D=8.)"}]},  # only a comment: still gouges
        {"setup_changes": [{"path": "tools.3.diameter", "value": 6.0}],
         "setup_actions": ["Load the 6 mm end mill the program was written for in pocket 3."]},
        {"setup_changes": [{"path": "fixtures.0.x0", "value": 500}]},  # clamp would not hold the part
    ])
    assert res["status"] == "proved"
    assert res["setup_changes"] and res["setup_changes"][0]["path"] == "tools.3.diameter"
    assert [h["result"] for h in res["history"]] == ["rejected", "proved", "invalid_edit"]
