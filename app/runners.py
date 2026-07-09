"""Pluggable agent backends that drive one pipeline phase or a chat turn.

Two implementations, same contract: run an AI coding agent non-interactively in
the repo and stream progress events back.

- ClaudeCodeRunner  -> `claude -p --output-format stream-json`  (supports --resume)
- CopilotRunner     -> `copilot -p - --agent fabric-advisor`

Both are headless. For pipeline phases the agent writes the phase's doc file; the
app owns state transitions. For chat turns the agent just answers (and may read
or edit files if asked).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

REPO_ROOT = Path(__file__).resolve().parent.parent

# Preamble that turns a generic `claude` session into the fabric-advisor
# orchestrator for a pipeline phase. Copilot loads the real agent via --agent.
_CLAUDE_PHASE_PREAMBLE = (
    "You are the Fabric Advisor orchestrator for this repository. "
    "Before doing anything, read `.github/agents/fabric-advisor.agent.md` and the "
    "SKILL.md of the skill named in the task below (under `.github/skills/`), and follow them. "
    "Honor `.github/copilot-instructions.md` (registry-first, templates-first, edit pre-generated "
    "files in place, always encoding='utf-8'). Do NOT modify pipeline-state.json. "
    "Complete ONLY the single phase described below, then stop.\n\n"
    "=== TASK ===\n"
)

# Preamble for a free-form chat turn (only used when there is no prior session
# to resume — otherwise the agent already has the conversation context).
_CLAUDE_CHAT_PREAMBLE = (
    "You are the Fabric Advisor assisting the user with an in-progress project in this "
    "repository. Read the project's docs under `_projects/<folder>/docs/` as needed to answer. "
    "You may read and edit files if the user asks, but do NOT modify pipeline-state.json. "
    "Answer the user's message directly and concisely.\n\n"
)


@dataclass
class RunEvent:
    kind: str          # "text" | "tool" | "session" | "error" | "done"
    text: str = ""
    meta: dict = field(default_factory=dict)


class BackendUnavailable(RuntimeError):
    pass


def _which(name: str) -> str | None:
    for candidate in (name, f"{name}.cmd", f"{name}.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def detect_backends() -> dict[str, dict]:
    claude = _which("claude")
    copilot = _which("copilot")
    return {
        "claude": {
            "id": "claude", "label": "Claude Code", "available": bool(claude),
            "path": claude, "install": "npm i -g @anthropic-ai/claude-code",
        },
        "copilot": {
            "id": "copilot", "label": "GitHub Copilot", "available": bool(copilot),
            "path": copilot, "install": "npm i -g @github/copilot",
        },
    }


class AgentRunner:
    id = "base"

    def __init__(self, repo_root: Path = REPO_ROOT):
        self.repo_root = repo_root
        self._proc: subprocess.Popen | None = None
        self._stopped = False

    def terminate(self) -> None:
        """Stop the running CLI and its children. Safe to call from any thread."""
        self._stopped = True
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        # Kill the whole tree — the CLI spawns node/child processes that
        # proc.terminate() alone would orphan.
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass

    # -- overridable ------------------------------------------------------

    def _argv(self, resume_session: str | None) -> list[str]:
        raise NotImplementedError

    def _prompt_text(self, prompt: str, *, mode: str, resume_session: str | None,
                     context: str) -> str:
        head = f"{context}\n\n" if context else ""
        return head + prompt

    def _parse_line(self, line: str) -> list[RunEvent]:
        line = line.rstrip("\n")
        return [RunEvent("text", line)] if line.strip() else []

    def _prepare_env(self, env: dict) -> None:
        """Hook for subclasses to adjust the subprocess environment."""

    # -- driver -----------------------------------------------------------

    async def run(self, prompt: str, scratch_dir: Path, *, mode: str = "phase",
                  resume_session: str | None = None,
                  context: str = "") -> AsyncIterator[RunEvent]:
        """Run the backend CLI and stream events.

        Uses a plain blocking subprocess on a worker thread rather than
        asyncio.create_subprocess_exec. On Windows the asyncio subprocess
        transport only works on a ProactorEventLoop; under uvicorn (esp.
        --reload) the loop may not support it and create_subprocess_exec raises
        an empty-message NotImplementedError. The threaded approach works on any
        event loop.
        """
        scratch_dir.mkdir(parents=True, exist_ok=True)
        text = self._prompt_text(prompt, mode=mode, resume_session=resume_session,
                                 context=context)
        (scratch_dir / f"prompt-{self.id}.txt").write_text(text, encoding="utf-8")

        argv = self._argv(resume_session)
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        self._prepare_env(env)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def worker() -> None:
            try:
                proc = subprocess.Popen(
                    argv, cwd=str(self.repo_root),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, bufsize=0,
                )
            except FileNotFoundError as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    RunEvent("error", f"Backend executable not found: {argv[0]} ({exc})"))
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)
                return

            self._proc = proc
            if self._stopped:  # stop requested before the process came up
                self.terminate()

            try:
                if proc.stdin:
                    proc.stdin.write(text.encode("utf-8"))
                    proc.stdin.close()
                buf = b""
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        for ev in self._parse_line(raw.decode("utf-8", errors="replace")):
                            loop.call_soon_threadsafe(queue.put_nowait, ev)
                if buf.strip():
                    for ev in self._parse_line(buf.decode("utf-8", errors="replace")):
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                rc = proc.wait()
                if rc != 0:
                    loop.call_soon_threadsafe(
                        queue.put_nowait, RunEvent("error", f"{self.id} exited with code {rc}"))
                else:
                    loop.call_soon_threadsafe(
                        queue.put_nowait, RunEvent("done", "", {"returncode": rc}))
            except Exception as exc:  # never let the worker die silently
                loop.call_soon_threadsafe(
                    queue.put_nowait, RunEvent("error", f"{self.id} runner error: {exc!r}"))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        threading.Thread(target=worker, name=f"{self.id}-runner", daemon=True).start()

        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            yield item


class ClaudeCodeRunner(AgentRunner):
    id = "claude"

    def __init__(self, repo_root: Path = REPO_ROOT):
        super().__init__(repo_root)
        self.exe = _which("claude")
        if not self.exe:
            raise BackendUnavailable("claude CLI not found on PATH")

    def _prepare_env(self, env: dict) -> None:
        # Use the user's Claude Code subscription login, not a pay-as-you-go API
        # key. A set ANTHROPIC_API_KEY/AUTH_TOKEN overrides the OAuth login and
        # can fail with "credit balance too low"; drop it so the logged-in
        # account is used. Set FTF_USE_API_KEY=1 to keep the key instead.
        if os.environ.get("FTF_USE_API_KEY") not in ("1", "true", "yes"):
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)

    def _prompt_text(self, prompt: str, *, mode: str, resume_session: str | None,
                     context: str) -> str:
        if mode == "phase":
            head = _CLAUDE_PHASE_PREAMBLE
        elif resume_session:
            head = ""  # conversation context already present in the resumed session
        else:
            head = _CLAUDE_CHAT_PREAMBLE
        ctx = f"{context}\n\n" if (context and not resume_session) else ""
        return head + ctx + prompt

    def _argv(self, resume_session: str | None) -> list[str]:
        # bypassPermissions (not acceptEdits): the Fabric skills instruct the
        # agent to RUN repo scripts (validate-items.py, diagram-gen.py,
        # discovery-summary, …). acceptEdits only auto-approves file edits, so
        # those Bash calls would be blocked in headless mode. This app is a
        # local, user-initiated automation of the user's own repo, so full tool
        # access is intended; the policy hook still blocks pipeline-state.json.
        argv = [
            self.exe, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(self.repo_root),
        ]
        if resume_session:
            argv += ["--resume", resume_session]
        return argv

    def _parse_line(self, line: str) -> list[RunEvent]:
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # stream-json emits only JSON on stdout; a non-JSON line is a CLI
            # notice merged from stderr (e.g. the connectors warning). Drop it.
            return []

        events: list[RunEvent] = []
        typ = obj.get("type")

        if typ == "system" and obj.get("subtype") == "init":
            sid = obj.get("session_id")
            if sid:
                events.append(RunEvent("session", "", {"session_id": sid}))
        elif typ == "assistant":
            for block in obj.get("message", {}).get("content", []):
                bt = block.get("type")
                if bt == "text" and block.get("text"):
                    events.append(RunEvent("text", block["text"]))
                elif bt == "tool_use":
                    name = block.get("name", "tool")
                    tgt = _tool_target(block.get("input", {}))
                    events.append(RunEvent("tool", f"{name} {tgt}".strip(), {"tool": name}))
        elif typ == "result":
            if obj.get("is_error"):
                events.append(RunEvent("error", obj.get("result", "agent error")))
            sid = obj.get("session_id")
            events.append(RunEvent("done", "", {"result": obj.get("result", ""),
                                                "session_id": sid}))
        return events


class CopilotRunner(AgentRunner):
    id = "copilot"

    def __init__(self, repo_root: Path = REPO_ROOT):
        super().__init__(repo_root)
        self.exe = _which("copilot")
        if not self.exe:
            raise BackendUnavailable("copilot CLI not found on PATH")

    def _argv(self, resume_session: str | None) -> list[str]:
        argv = [
            self.exe, "-p", "-",
            "-s",
            "--allow-all-tools",
            "--allow-all-paths",
            "--no-ask-user",
            "--add-dir", str(self.repo_root),
            "--agent", "fabric-advisor",
        ]
        if resume_session:
            argv += ["--resume", resume_session]
        return argv


def _tool_target(inp: dict) -> str:
    for key in ("file_path", "path", "command", "pattern", "url"):
        if key in inp and isinstance(inp[key], str):
            val = inp[key]
            return val if len(val) <= 80 else val[:77] + "…"
    return ""


def make_runner(backend_id: str, repo_root: Path = REPO_ROOT) -> AgentRunner:
    if backend_id == "claude":
        return ClaudeCodeRunner(repo_root)
    if backend_id == "copilot":
        return CopilotRunner(repo_root)
    raise BackendUnavailable(f"Unknown backend '{backend_id}'")
