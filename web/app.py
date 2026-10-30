"""CrashProof web app: FastAPI backend serving the operator UI."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from crashproof import agent  # noqa: E402
from crashproof.core import analyze, certificate, restart_block, view_payload  # noqa: E402
from crashproof.scenarios import all_scenarios  # noqa: E402
from crashproof.setup import Setup  # noqa: E402

app = FastAPI(title="CrashProof")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

MAX_PROGRAM_BYTES = 400_000
LIVE_LIMIT = int(os.environ.get("CRASHPROOF_LIVE_PER_HOUR", "20"))
_calls: dict[str, deque] = defaultdict(deque)
_cache_dir = ROOT / "cache"


class Job(BaseModel):
    program: str
    setup: dict
    line: int | None = None
    scenario: str | None = None


def _setup(d: dict) -> Setup:
    try:
        return Setup.from_dict(d)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Invalid setup: {e}") from e


def _check_size(job: Job) -> None:
    if len(job.program.encode()) > MAX_PROGRAM_BYTES:
        raise HTTPException(413, "Program is larger than the demo limit (400 kB).")


def _rate_ok(ip: str) -> bool:
    now = time.time()
    q = _calls[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= LIVE_LIMIT:
        return False
    q.append(now)
    return True


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {"llm": agent.client() is not None, "fix_model": agent.FIX_MODEL, "fast_model": agent.FAST_MODEL,
            "base_url": agent.BASE_URL}


@app.get("/api/scenarios")
def scenarios():
    return all_scenarios()


_views: OrderedDict = OrderedDict()
_views_lock = threading.Lock()


def cached_view(program: str, setup: dict) -> dict:
    """Analysis is deterministic, so identical program + setup pairs are served from memory."""
    key = hashlib.sha256((program + json.dumps(setup, sort_keys=True)).encode()).hexdigest()
    with _views_lock:
        if key in _views:
            _views.move_to_end(key)
            return _views[key]
    s = _setup(setup)
    v = view_payload(program, s, analyze(program, s))
    with _views_lock:
        _views[key] = v
        while len(_views) > 48:
            _views.popitem(last=False)
    return v


@app.on_event("startup")
def _warm():
    def run():
        for sc in all_scenarios():
            try:
                cached_view(sc["program"], sc["setup"])
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=run, daemon=True).start()


@app.post("/api/analyze")
def api_analyze(job: Job):
    _check_size(job)
    return cached_view(job.program, job.setup)


@app.post("/api/restart")
def api_restart(job: Job):
    _check_size(job)
    s = _setup(job.setup)
    if not job.line:
        raise HTTPException(400, "line is required")
    try:
        return restart_block(job.program, s, job.line)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/certificate")
def api_certificate(job: Job):
    s = _setup(job.setup)
    rep = analyze(job.program, s)
    return certificate(job.program, s, rep, {"repair": agent.FIX_MODEL, "briefing": agent.FAST_MODEL})


@app.post("/api/explain")
def api_explain(job: Job, request: Request):
    s = _setup(job.setup)
    rep = analyze(job.program, s)
    if not _rate_ok(request.client.host if request.client else "?"):
        raise HTTPException(429, "Live model calls are rate limited on the public demo. Try again later.")
    issues = [i.to_dict() for i in rep["blocking"]] or [i.to_dict() for i in rep["issues"]]
    return agent.explain(job.program, s, rep["verdict"], issues)


class LookupReq(BaseModel):
    query: str


@app.post("/api/tool-lookup")
def api_tool_lookup(req: LookupReq, request: Request):
    if not _rate_ok(request.client.host if request.client else "?"):
        raise HTTPException(429, "Live model calls are rate limited on the public demo. Try again later.")
    from crashproof.research import lookup_tool
    return lookup_tool(req.query.strip()[:120])


class ScreenReq(BaseModel):
    image_b64: str
    mime: str = "image/jpeg"
    setup: dict


@app.post("/api/read-screen")
def api_read_screen(req: ScreenReq, request: Request):
    if len(req.image_b64) > 8_000_000:
        raise HTTPException(413, "Image is too large (max ~6 MB).")
    if not _rate_ok(request.client.host if request.client else "?"):
        raise HTTPException(429, "Live model calls are rate limited on the public demo. Try again later.")
    import base64
    from crashproof.research import offsets_to_setup_changes, read_screen
    res = read_screen(base64.b64decode(req.image_b64), req.mime)
    if res.get("status") == "ok":
        res["changes"] = offsets_to_setup_changes(req.setup, res["data"])
    return res


@app.get("/api/sample-screen")
def sample_screen():
    return FileResponse(ROOT / "examples" / "offset_screen_example.png")


def _cache_path(job: Job) -> Path | None:
    if not job.scenario:
        return None
    return _cache_dir / f"repair_{job.scenario}.jsonl"


@app.post("/api/repair")
def api_repair(job: Job, request: Request, live: bool = False):
    """Server-sent events: each Nemotron candidate and its simulator verdict as they happen."""
    _check_size(job)
    s = _setup(job.setup)
    cache = _cache_path(job)
    if cache and cache.exists() and not live:
        def replay():
            for line in cache.read_text(encoding="utf-8").splitlines():
                yield f"data: {line}\n\n"
                time.sleep(0.35)
        return StreamingResponse(replay(), media_type="text/event-stream")
    if agent.client() is None:
        def nokey():
            yield "data: " + json.dumps({"type": "done", "status": "no_key",
                                         "message": "Live repair needs NEBIUS_API_KEY on the server."}) + "\n\n"
        return StreamingResponse(nokey(), media_type="text/event-stream")
    if not _rate_ok(request.client.host if request.client else "?"):
        raise HTTPException(429, "Live model calls are rate limited on the public demo. Try again later.")

    rep = analyze(job.program, s)
    issues = [i.to_dict() for i in rep["issues"]]
    q: queue.Queue = queue.Queue()

    def work():
        try:
            result = agent.repair(job.program, s, issues, emit=q.put)
            if result.get("status") == "proved":
                s2 = Setup.from_dict(result["setup"]) if result.get("setup") else s
                result["view"] = view_payload(result["program"], s2, analyze(result["program"], s2))
            q.put({"type": "done", **result})
        except Exception as e:  # noqa: BLE001
            q.put({"type": "done", "status": "error", "message": f"{type(e).__name__}: {e}"})

    threading.Thread(target=work, daemon=True).start()

    def stream():
        meta = json.dumps({"type": "meta", "recorded_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                           "model": agent.FIX_MODEL, "endpoint": agent.BASE_URL})
        recorded = [meta]
        yield f"data: {meta}\n\n"
        while True:
            ev = q.get()
            line = json.dumps(ev, default=str)
            recorded.append(line)
            yield f"data: {line}\n\n"
            if ev.get("type") == "done":
                if cache and ev.get("status") in ("proved", "not_proved"):
                    _cache_dir.mkdir(exist_ok=True)
                    cache.write_text("\n".join(recorded), encoding="utf-8")
                break

    return StreamingResponse(stream(), media_type="text/event-stream")
