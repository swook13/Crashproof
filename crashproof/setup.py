"""Machine setup: what is physically on the machine when the operator presses Cycle Start."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass
class Tool:
    number: int
    kind: str = "flat"  # flat | ball | drill
    diameter: float = 10.0
    flute_length: float = 20.0
    shank_diameter: float | None = None
    holder_diameter: float = 40.0
    stickout: float = 35.0  # tip to holder face
    length: float = 100.0  # gauge line to tip, as measured by the tool setter
    flutes: int = 2
    description: str = ""

    @property
    def radius(self) -> float:
        return self.diameter / 2

    @property
    def shank_radius(self) -> float:
        return (self.shank_diameter or self.diameter) / 2


@dataclass
class Box:
    name: str
    x0: float
    x1: float
    y0: float
    y1: float
    z0: float
    z1: float


@dataclass
class Setup:
    name: str
    stock: Box
    tools: dict[int, Tool]
    wcs: dict[str, list[float]]
    h_table: dict[int, float] = field(default_factory=dict)
    cam_tools: dict[int, Tool] = field(default_factory=dict)
    fixtures: list[Box] = field(default_factory=list)
    features: list[dict] = field(default_factory=list)
    operations: list[dict] = field(default_factory=list)  # CAM setup sheet: per-tool max depth and step
    travel: dict[str, list[float]] = field(default_factory=lambda: {"x": [-760, 0], "y": [-410, 0], "z": [-510, 0]})
    home: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    spindle_diameter: float = 90.0
    tool_in_spindle: int | None = None
    primary_wcs: str = "G54"
    tolerance: float = 0.05
    resolution: float = 0.5

    def __post_init__(self):
        if not self.h_table:
            self.h_table = {n: t.length for n, t in self.tools.items()}

    def cam_tool(self, n: int | None) -> Tool | None:
        if n is None:
            return None
        return self.cam_tools.get(n) or self.tools.get(n)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tools"] = {str(k): v for k, v in d["tools"].items()}
        d["cam_tools"] = {str(k): v for k, v in d["cam_tools"].items()}
        d["h_table"] = {str(k): v for k, v in d["h_table"].items()}
        return d

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_dict(cls, d: dict) -> "Setup":
        d = dict(d)
        tools = {int(k): Tool(**{**v, "number": int(k)}) for k, v in d.pop("tools").items()}
        cam_tools = {int(k): Tool(**{**v, "number": int(k)}) for k, v in d.pop("cam_tools", {}).items()}
        h_table = {int(k): float(v) for k, v in d.pop("h_table", {}).items()}
        stock = Box(**d.pop("stock"))
        fixtures = [Box(**f) for f in d.pop("fixtures", [])]
        return cls(stock=stock, tools=tools, cam_tools=cam_tools, h_table=h_table, fixtures=fixtures, **d)

    @classmethod
    def load(cls, path) -> "Setup":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
