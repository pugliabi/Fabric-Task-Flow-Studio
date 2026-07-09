"""Thin wrapper around the existing run-pipeline library.

The web app never re-implements pipeline logic. It imports the same functions
that `_shared/scripts/run-pipeline.py` uses (start, advance, status, prompts)
and captures their stdout so the reports the CLI prints become chat messages.

State ownership is unchanged: `pipeline-state.json` is written exclusively by
these library functions. The app only reads state and doc files, and calls the
same mutation functions the CLI exposes.
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
from pathlib import Path

# The run-pipeline library is not concurrency-safe: it mutates module globals,
# uses redirect_stdout, and shells out to generator scripts. When multiple
# projects run at once we call these from a thread pool (asyncio.to_thread) so
# they never block the event loop, and serialize them with this lock so their
# global-state mutations don't interleave.
_LOCK = threading.RLock()

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = REPO_ROOT / "_shared" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

# Imported from the same library the CLI uses. These calls carry all the
# precompute / fast-forward side effects (signal mapper, deploy artifacts,
# artifacts-only fast path, etc.).
from paths import REPO_ROOT as _LIB_REPO_ROOT  # noqa: E402,F401
from pipeline_prompts import get_next_prompt  # noqa: E402
from pipeline_state import (  # noqa: E402
    _load_state,
    _phase_is_gate,
    _phase_order,
    _phase_output_files,
    _phase_skill,
    _verify_output,
    advance as _advance,
    reconcile as _reconcile,
    reset_phase as _reset_phase,
    start_pipeline as _start_pipeline,
)

PROJECTS_DIR = REPO_ROOT / "_projects"

PHASE_LABELS = {
    "0a-discovery": "Discover",
    "1-design": "Design",
    "2a-test-plan": "Test Plan",
    "2b-sign-off": "Sign-Off",
    "2c-deploy": "Deploy",
    "3-validate": "Validate",
    "4-document": "Document",
}


def _capture(fn, *args, **kwargs) -> tuple[object, str]:
    """Run a library function under the lock, returning (result, stdout)."""
    buf = io.StringIO()
    with _LOCK:
        with contextlib.redirect_stdout(buf):
            result = fn(*args, **kwargs)
    return result, buf.getvalue()


def start(name: str, problem: str | None) -> tuple[dict, str]:
    return _capture(_start_pipeline, name, problem)  # type: ignore[return-value]


def status(project: str) -> dict:
    return _load_state(project)


def next_prompt(project: str):
    """Returns (prompt, agent, phase, is_gate). Precompute may shell out, so
    hold the lock (callers run this via asyncio.to_thread)."""
    with _LOCK:
        return get_next_prompt(project)


def advance(project: str, *, approved: bool = False, revise: bool = False,
            feedback: str | None = None, deploy_mode: str | None = None) -> tuple[dict, str]:
    """Advance one transition. For a live approve, record deploy_mode first
    (mirrors run-pipeline.py main, which sets it before calling advance)."""
    if deploy_mode is not None and approved:
        state = _load_state(project)
        if state.get("current_phase") == "2b-sign-off":
            from pipeline_state import _save_state
            state["deploy_mode"] = deploy_mode
            _save_state(project, state)
    return _capture(_advance, project, approved=approved, revise=revise, feedback=feedback)  # type: ignore[return-value]


def reconcile(project: str) -> tuple[tuple, str]:
    return _capture(_reconcile, project)  # type: ignore[return-value]


def reset(project: str, phase: str) -> tuple[dict, str]:
    """Reset the pipeline back to `phase` (marks it + all later phases pending)."""
    return _capture(_reset_phase, project, phase)  # type: ignore[return-value]


def check_errors(project: str) -> dict:
    """Static health check: per-phase output verification + state consistency.

    Returns a structured report the UI can render, without mutating state.
    """
    with _LOCK:
        state = _load_state(project)
    issues: list[dict] = []
    order = _phase_order()

    for phase_id in order:
        status = state["phases"][phase_id]["status"]
        expected = _phase_output_files(phase_id)
        if not expected:
            continue
        ok, msg = _verify_output(phase_id, project)
        if status == "complete" and not ok:
            issues.append({"phase": phase_id, "label": PHASE_LABELS.get(phase_id, phase_id),
                           "severity": "error",
                           "detail": f"marked complete but {msg.lower()}"})
        elif ok and status not in ("complete", "in_progress"):
            issues.append({"phase": phase_id, "label": PHASE_LABELS.get(phase_id, phase_id),
                           "severity": "warning",
                           "detail": "output exists but phase not marked complete — reconcile can heal"})

    # current_phase should be the first non-complete phase
    last_complete = None
    for phase_id in order:
        if state["phases"][phase_id]["status"] == "complete":
            last_complete = phase_id
        else:
            break
    from pipeline_state import _next_phase
    expected_current = _next_phase(last_complete) if last_complete else order[0]
    if expected_current and state.get("current_phase") != expected_current \
            and not is_complete(state):
        issues.append({"phase": state.get("current_phase"), "label": "State",
                       "severity": "warning",
                       "detail": f"current_phase is '{state.get('current_phase')}' but file "
                                 f"evidence points to '{expected_current}' — reconcile can heal"})

    return {
        "ok": not any(i["severity"] == "error" for i in issues),
        "issues": issues,
        "complete": is_complete(state),
    }


def verify_output(phase: str, project: str) -> tuple[bool, str]:
    return _verify_output(phase, project)


def phase_order() -> list[str]:
    return _phase_order()


def is_gate(phase: str) -> bool:
    return _phase_is_gate(phase)


def phase_skill(phase: str) -> str | None:
    return _phase_skill(phase)


def is_complete(state: dict) -> bool:
    return all(p.get("status") == "complete" for p in state["phases"].values())


def phase_view(state: dict) -> list[dict]:
    """A UI-friendly list of phases with status + label + skill + output file."""
    project = state["project"]
    project_dir = PROJECTS_DIR / project
    view = []
    for phase_id in _phase_order():
        phase = state["phases"][phase_id]
        output = phase.get("output", "")
        out_exists = bool(output) and (project_dir / output).exists()
        view.append({
            "id": phase_id,
            "label": PHASE_LABELS.get(phase_id, phase_id),
            "status": phase["status"],
            "skill": _phase_skill(phase_id),
            "gate": _phase_is_gate(phase_id),
            "output": output if out_exists else None,
        })
    return view


def last_activity(project: str) -> str | None:
    """The most recent human-readable line from the persisted transcript, so the
    dashboard can show live activity without opening a stream per project."""
    import json
    path = PROJECTS_DIR / project / ".studio" / "history.jsonl"
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-40:]):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("kind")
        if kind == "phase_start":
            return "▶ " + (ev.get("label") or "phase")
        if kind in ("agent", "agent_final"):
            text = (ev.get("text") or "").strip().replace("\n", " ")
            if text:
                return text[:110]
        if kind == "tool":
            return "⚙ " + (ev.get("text") or "")[:70]
    return None


def list_projects() -> list[dict]:
    """Summarize every project that has a pipeline-state.json."""
    out = []
    if not PROJECTS_DIR.exists():
        return out
    order = _phase_order()
    total = len(order)
    for child in sorted(PROJECTS_DIR.iterdir()):
        state_path = child / "pipeline-state.json"
        if not state_path.exists():
            continue
        try:
            state = _load_state(child.name)
        except Exception:
            continue
        done = sum(1 for p in order if state["phases"][p]["status"] == "complete")
        out.append({
            "project": child.name,
            "display_name": state.get("display_name", child.name),
            "task_flow": state.get("task_flow") or "TBD",
            "current_phase": state.get("current_phase"),
            "current_label": PHASE_LABELS.get(state.get("current_phase", ""), ""),
            "complete": is_complete(state),
            "deploy_mode": state.get("deploy_mode"),
            "phase_done": done,
            "phase_total": total,
            "phases": [state["phases"][p]["status"] for p in order],
            "last_activity": last_activity(child.name),
        })
    return out


def read_doc(project: str, name: str) -> str | None:
    """Read a docs/<name> file for a project. Path-traversal safe."""
    docs = (PROJECTS_DIR / project / "docs").resolve()
    target = (docs / name).resolve()
    if docs not in target.parents and target != docs:
        return None
    if not target.exists() or not target.is_file():
        return None
    return target.read_text(encoding="utf-8", errors="ignore")


def write_doc(project: str, name: str, content: str) -> bool:
    """Overwrite a docs/<name> file. Path-traversal safe; .md only."""
    if not name.endswith(".md") or "/" in name or "\\" in name:
        return False
    docs = (PROJECTS_DIR / project / "docs").resolve()
    target = (docs / name).resolve()
    if docs not in target.parents and target.parent != docs:
        return False
    if not docs.exists():
        return False
    target.write_text(content, encoding="utf-8", newline="\n")
    return True


def list_docs(project: str) -> list[str]:
    docs = PROJECTS_DIR / project / "docs"
    if not docs.exists():
        return []
    return sorted(p.name for p in docs.iterdir()
                  if p.is_file() and p.suffix == ".md" and not p.name.startswith("."))


def signoff_data(project: str) -> dict:
    """Diagram + plain-language facts for the 2b sign-off gate card."""
    from pipeline_prompts import _extract_diagram
    docs = PROJECTS_DIR / project / "docs"
    handoff = docs / "architecture-handoff.md"
    diagram = _extract_diagram(handoff) if handoff.exists() else None

    import json
    cache = docs / ".architecture-cache.json"
    summary = {}
    if cache.exists():
        try:
            summary = json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            summary = {}

    state = _load_state(project)
    return {
        "display_name": state.get("display_name", project),
        "task_flow": summary.get("task_flow") or state.get("task_flow") or "TBD",
        "item_count": summary.get("item_count", 0),
        "wave_count": summary.get("wave_count", 0),
        "diagram": diagram,
        "revisions": state.get("sign_off_revisions", 0),
    }
