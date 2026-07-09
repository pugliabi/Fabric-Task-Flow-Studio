"""FastAPI server: REST + Server-Sent Events, serving the chat UI.

Launch with `python run-app.py` from the repo root (it sets app_dir and forces
UTF-8). The ASGI app is `server:app` with `app/` on the path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import pipeline_api as api
from orchestrator import Session
from runners import detect_backends

APP_DIR = Path(__file__).resolve().parent
STATIC = APP_DIR / "static"

app = FastAPI(title="Fabric Task Flows Studio")

# project -> Session
_sessions: dict[str, Session] = {}


def _get_session(project: str) -> Session | None:
    return _sessions.get(project)


def _default_backend() -> str | None:
    avail = detect_backends()
    for bid, meta in avail.items():
        if meta["available"]:
            return bid
    return None


def _ensure_session(project: str, backend: str | None = None) -> Session:
    """Return the live session for a project, creating one if the server was
    restarted or the project is being acted on without an explicit open."""
    try:
        api.status(project)
    except FileNotFoundError:
        raise HTTPException(404, f"No project '{project}'")
    session = _sessions.get(project)
    if session is None:
        bid = backend or _default_backend()
        if not bid:
            raise HTTPException(400, "No backend installed")
        session = Session(project, bid)
        _sessions[project] = session
    elif backend and backend != session.backend:
        session.backend = backend
    return session


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------

@app.get("/api/backends")
def backends():
    return detect_backends()


@app.get("/api/projects")
def projects():
    lst = api.list_projects()
    for p in lst:
        s = _sessions.get(p["project"])
        p["running"] = bool(s and s.busy())
        p["awaiting_gate"] = bool(s and s.awaiting_gate)
    return lst


# --------------------------------------------------------------------------
# Start / resume a run
# --------------------------------------------------------------------------

@app.post("/api/start")
async def start(req: Request):
    body = await req.json()
    name = (body.get("name") or "").strip()
    problem = (body.get("problem") or "").strip()
    backend = body.get("backend") or "claude"
    if not name:
        raise HTTPException(400, "Project name is required")
    if not problem:
        raise HTTPException(400, "Problem statement is required")

    avail = detect_backends()
    if not avail.get(backend, {}).get("available"):
        raise HTTPException(400, f"Backend '{backend}' is not installed")

    try:
        state, report = await asyncio.to_thread(api.start, name, problem)
    except Exception as exc:
        raise HTTPException(400, str(exc))

    project = state["project"]
    session = Session(project, backend)
    _sessions[project] = session
    # Kick the drive loop after the client has time to open the SSE stream.
    session.start()
    return {"project": project, "display_name": state.get("display_name", project),
            "report": report}


@app.post("/api/projects/{project}/resume")
async def resume(project: str, req: Request):
    body = await req.json()
    backend = body.get("backend") or "claude"
    avail = detect_backends()
    if not avail.get(backend, {}).get("available"):
        raise HTTPException(400, f"Backend '{backend}' is not installed")
    try:
        api.status(project)
    except FileNotFoundError:
        raise HTTPException(404, f"No project '{project}'")
    session = _sessions.get(project)
    if session is None or session.backend != backend:
        session = Session(project, backend)
        _sessions[project] = session
    session.start()
    return {"project": project}


# --------------------------------------------------------------------------
# Gate actions
# --------------------------------------------------------------------------

@app.post("/api/projects/{project}/approve")
async def approve(project: str, req: Request):
    body = await req.json()
    deploy_mode = body.get("deploy_mode") or "artifacts_only"
    if deploy_mode not in ("live", "artifacts_only"):
        raise HTTPException(400, "deploy_mode must be 'live' or 'artifacts_only'")
    session = _get_session(project)
    if not session:
        raise HTTPException(404, "No active session — resume the project first")
    session.approve(deploy_mode)
    return {"ok": True}


@app.post("/api/projects/{project}/revise")
async def revise(project: str, req: Request):
    body = await req.json()
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(400, "Feedback is required to revise")
    session = _ensure_session(project, body.get("backend"))
    session.revise(feedback)
    return {"ok": True}


# --------------------------------------------------------------------------
# Chat anytime / go-back / edit / error-check
# --------------------------------------------------------------------------

@app.post("/api/projects/{project}/chat")
async def chat(project: str, req: Request):
    body = await req.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "Message is required")
    session = _ensure_session(project, body.get("backend"))
    if session.busy():
        raise HTTPException(409, "The agent is busy — wait for the current step to finish")
    session.chat(message)
    return {"ok": True}


@app.post("/api/projects/{project}/reset")
async def reset(project: str, req: Request):
    body = await req.json()
    phase = body.get("phase")
    rerun = bool(body.get("rerun", True))
    if phase not in api.phase_order():
        raise HTTPException(400, f"Unknown phase '{phase}'")
    session = _ensure_session(project, body.get("backend"))
    if session.busy():
        raise HTTPException(409, "The agent is busy — wait for the current step to finish")
    session.reset_to(phase, rerun)
    return {"ok": True}


@app.post("/api/projects/{project}/stop")
async def stop(project: str):
    session = _get_session(project)
    if not session:
        return {"ok": True, "was_running": False}
    was = session.stop()
    return {"ok": True, "was_running": was}


@app.post("/api/projects/{project}/mode")
async def mode(project: str, req: Request):
    body = await req.json()
    auto = bool(body.get("auto_advance", True))
    session = _ensure_session(project, body.get("backend"))
    session.set_auto(auto, resume=True)
    return {"ok": True, "auto_advance": auto}


@app.post("/api/projects/{project}/check")
async def check(project: str, req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    heal = bool(body.get("heal", False))
    session = _ensure_session(project, body.get("backend"))
    session.check(heal)
    return {"ok": True}


@app.post("/api/projects/{project}/doc")
async def save_doc(project: str, req: Request):
    body = await req.json()
    name = body.get("name") or ""
    content = body.get("content")
    if content is None:
        raise HTTPException(400, "content is required")
    if not api.write_doc(project, name, content):
        raise HTTPException(400, "Invalid doc name or project")
    session = _get_session(project)
    if session:
        await session.emit("agent", role="system", text=f"✏️ Saved edits to `docs/{name}`.")
        await session.emit_state()
    return {"ok": True}


# --------------------------------------------------------------------------
# State + docs
# --------------------------------------------------------------------------

@app.get("/api/projects/{project}/status")
def status(project: str):
    try:
        state = api.status(project)
    except FileNotFoundError:
        raise HTTPException(404, "No such project")
    return {
        "project": project,
        "display_name": state.get("display_name", project),
        "task_flow": state.get("task_flow") or "TBD",
        "deploy_mode": state.get("deploy_mode"),
        "current_phase": state.get("current_phase"),
        "complete": api.is_complete(state),
        "phases": api.phase_view(state),
        "docs": api.list_docs(project),
        "awaiting_gate": bool(_get_session(project) and _get_session(project).awaiting_gate),
        "auto_advance": bool(_get_session(project).auto_advance) if _get_session(project) else True,
        "busy": bool(_get_session(project) and _get_session(project).running),
    }


@app.get("/api/projects/{project}/doc")
def doc(project: str, name: str):
    content = api.read_doc(project, name)
    if content is None:
        raise HTTPException(404, "Doc not found")
    return {"name": name, "content": content}


# --------------------------------------------------------------------------
# SSE event stream
# --------------------------------------------------------------------------

@app.get("/api/projects/{project}/events")
async def events(project: str, request: Request):
    backend = request.query_params.get("backend")
    session = _ensure_session(project, backend)

    # Resume support: the browser sends Last-Event-ID on auto-reconnect so we
    # replay only what it missed (no duplicates). A fresh connection (id 0)
    # replays the bounded history so the conversation-so-far shows up.
    try:
        last_id = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        last_id = 0

    queue = session.subscribe()

    async def gen():
        try:
            for event in session.replay_since(last_id):
                yield _frame(event)
            # Prime current state (+ gate/complete) after any replay.
            try:
                await session.prime()
            except Exception:
                pass
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _frame(event)
        finally:
            session.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _frame(event: dict) -> str:
    eid = event.get("_id")
    prefix = f"id: {eid}\n" if eid is not None else ""
    return f"{prefix}data: {json.dumps(event)}\n\n"


# --------------------------------------------------------------------------
# Static UI
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.exception_handler(FileNotFoundError)
def _fnf(_req, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})
