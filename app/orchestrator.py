"""The drive loop: turns the pipeline state machine into a chat conversation.

For each agent-driven phase it runs the chosen backend (which writes the phase
doc), then calls `advance`. It stops at the 2b Sign-Off human gate and waits for
the user to approve (live / artifacts-only) or revise. Everything it does is
emitted as chat events over an asyncio.Queue the SSE endpoint drains.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pipeline_api as api
from runners import make_runner

SCRATCH = Path(__file__).resolve().parent.parent / "_projects"

# Event kinds worth persisting as the durable conversation transcript. Transient
# UI signals (state / agent_delta / chat_start / gate / paused / complete) are
# re-derived from live pipeline state by prime() on reconnect, so we don't store
# them — that keeps the on-disk history a clean chat log with no stale cards.
_PERSIST_KINDS = frozenset({"phase_start", "agent", "agent_final", "tool"})
_HISTORY_LOAD_MAX = 4000


class Session:
    """One project being driven by one backend, with a chat event queue."""

    _HISTORY_MAX = 2000

    def __init__(self, project: str, backend: str):
        self.project = project
        self.backend = backend
        # Fan-out eventing: each SSE connection subscribes its own queue, and
        # a bounded event log lets a (re)connecting client replay only what it
        # has not seen yet (via SSE Last-Event-ID). This avoids the single-queue
        # "two readers steal each other's events" bug on reconnect.
        self.subscribers: set[asyncio.Queue] = set()
        self.history: list[dict] = []
        self._seq = 0
        self._hist_path = SCRATCH / project / ".studio" / "history.jsonl"
        self._load_history()
        self.task: asyncio.Task | None = None
        self.awaiting_gate = False   # True while parked at 2b sign-off
        self.running = False         # True while an agent subprocess is active
        self.session_id: str | None = None  # last backend session, for chat resume
        self.auto_advance = True     # False = pause after each phase for review
        self.active_runner = None    # the AgentRunner currently executing, for stop()

    def _load_history(self):
        """Preload the persisted conversation so a reopened project shows the
        transcript and Last-Event-ID resume continues from the right id."""
        try:
            if not self._hist_path.exists():
                return
            lines = self._hist_path.read_text(encoding="utf-8").splitlines()
            events = []
            for line in lines[-_HISTORY_LOAD_MAX:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            self.history = events
            if events:
                self._seq = max(ev.get("_id", 0) for ev in events)
        except OSError:
            pass

    def _persist(self, ev: dict):
        try:
            self._hist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._hist_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass

    async def emit(self, kind: str, **data):
        self._seq += 1
        ev = {"_id": self._seq, "kind": kind, **data}
        self.history.append(ev)
        if len(self.history) > self._HISTORY_MAX:
            self.history = self.history[-self._HISTORY_MAX:]
        if kind in _PERSIST_KINDS:
            self._persist(ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def replay_since(self, last_id: int) -> list[dict]:
        return [ev for ev in self.history if ev["_id"] > last_id]

    async def emit_state(self):
        state = api.status(self.project)
        await self.emit(
            "state",
            phases=api.phase_view(state),
            current_phase=state.get("current_phase"),
            task_flow=state.get("task_flow") or "TBD",
            deploy_mode=state.get("deploy_mode"),
            complete=api.is_complete(state),
            auto_advance=self.auto_advance,
            busy=self.running,
        )

    async def prime(self):
        """Emit current state when a client (re)connects. If the project is
        parked at the sign-off gate, re-present the gate so it stays actionable
        even after a server restart."""
        await self.emit_state()
        try:
            state = api.status(self.project)
        except Exception:
            return
        cur = state.get("current_phase")
        if cur == "2b-sign-off" and state["phases"][cur]["status"] == "in_progress" \
                and not api.is_complete(state):
            self.awaiting_gate = True
            await self.emit("gate", **await asyncio.to_thread(api.signoff_data, self.project))
        elif api.is_complete(state):
            docs = api.list_docs(self.project)
            brief = "project-brief.md" if "project-brief.md" in docs else None
            await self.emit("complete", brief=brief, docs=docs)

    # -- lifecycle ---------------------------------------------------------

    def _launch(self, factory) -> bool:
        """Start a single foreground action. Refuse (and note) if one is already
        in flight — this prevents two drive loops from colliding on the same
        files, which is what produced spurious empty errors before."""
        if self.task and not self.task.done():
            asyncio.create_task(self._guarded(self._busy_note()))
            return False
        self.task = asyncio.create_task(self._guarded(factory()))
        return True

    async def _busy_note(self):
        await self.emit("agent", role="system",
                        text="⏳ Busy with the current step — try again once it finishes.")

    def start(self):
        self._launch(lambda: self._drive())

    def approve(self, deploy_mode: str):
        self._launch(lambda: self._approve(deploy_mode))

    def revise(self, feedback: str):
        self._launch(lambda: self._revise(feedback))

    def chat(self, message: str):
        self._launch(lambda: self._chat(message))

    def reset_to(self, phase: str, rerun: bool):
        self._launch(lambda: self._reset_to(phase, rerun))

    def check(self, heal: bool):
        self._launch(lambda: self._check(heal))

    def busy(self) -> bool:
        return self.running or bool(self.task and not self.task.done())

    def stop(self) -> bool:
        """Halt the running task flow: kill the agent process tree and cancel
        the drive loop. The current phase is left re-runnable."""
        was_busy = self.busy()
        runner = self.active_runner
        if runner is not None:
            runner.terminate()
        self.active_runner = None
        self.running = False
        self.awaiting_gate = False
        t = self.task
        if t and not t.done():
            t.cancel()
        # Announce on a fresh task (the old one is being cancelled).
        self.task = asyncio.create_task(self._guarded(self._after_stop(was_busy)))
        return was_busy

    async def _after_stop(self, was_busy: bool):
        if was_busy:
            await self.emit("phase_end", phase="", status="stopped")
            await self.emit("agent", role="system",
                            text="⏹ **Stopped.** The current phase was halted. You can "
                                 "**Redo** it, edit a deliverable, or **Continue**.")
        else:
            await self.emit("agent", role="system", text="Nothing was running.")
        await self.emit_state()

    async def _guarded(self, coro):
        try:
            await coro
        except Exception as exc:  # surface any failure into the chat
            await self.emit("agent", role="system", text=f"⚠️ Error: {exc}")

    # -- the loop ----------------------------------------------------------

    async def _drive(self):
        """Run agent phases until we hit the sign-off gate or completion.

        When auto_advance is False, run at most ONE agent phase per call and
        then pause so the user can review and click Continue for the next one.
        Human gates and completion always stop regardless of the toggle.
        """
        await self.emit_state()
        ran_one = False
        while True:
            state = api.status(self.project)
            if api.is_complete(state):
                await self._announce_complete()
                return

            prompt, agent, phase, is_gate = await asyncio.to_thread(api.next_prompt, self.project)

            if phase == "complete" or agent is None and phase == "complete":
                await self._announce_complete()
                return

            if is_gate:
                await self._present_gate()
                return

            # Step mode: we already ran one phase this call — pause before the next.
            if ran_one and not self.auto_advance:
                label = api.PHASE_LABELS.get(phase, phase)
                await self.emit(
                    "agent", role="system",
                    text=f"⏸ Paused (auto-advance off). Click **Continue** to run **{label}**.",
                )
                await self.emit("paused", next_phase=phase)
                return

            # Agent-driven phase.
            await self._run_phase(phase, agent, prompt)
            ran_one = True

            # Verify the doc exists, then advance.
            ok, msg = api.verify_output(phase, self.project)
            if not ok:
                await self.emit(
                    "agent", role="system",
                    text=f"⚠️ {api.PHASE_LABELS.get(phase, phase)} didn't produce its "
                         f"output ({msg}). Re-running may help, or check the agent log above.",
                )
                await self.emit("phase_end", phase=phase, status="stalled")
                return

            _, report = await asyncio.to_thread(api.advance, self.project)
            await self.emit("phase_end", phase=phase, status="complete")
            if report.strip():
                await self.emit("agent", role="system", text=report.strip())
            await self.emit_state()

    def set_auto(self, value: bool, resume: bool):
        # The flag takes effect immediately; a running drive loop reads it at its
        # next phase boundary. Only launch a (possibly resuming) task when idle.
        self.auto_advance = value
        if self.task and not self.task.done():
            asyncio.create_task(self._guarded(self._note_auto()))
        else:
            self.task = asyncio.create_task(self._guarded(self._after_set_auto(resume)))

    async def _note_auto(self):
        await self.emit("agent", role="system",
                        text=f"⚙️ Auto-advance **{'on' if self.auto_advance else 'off'}** "
                             f"(applies at the next phase boundary).")
        await self.emit_state()

    async def _after_set_auto(self, resume: bool):
        await self.emit("agent", role="system",
                        text=f"⚙️ Auto-advance **{'on' if self.auto_advance else 'off'}**.")
        await self.emit_state()
        # If turned on while idle mid-pipeline, resume driving immediately.
        if self.auto_advance and resume and not self.running and not self.awaiting_gate:
            state = api.status(self.project)
            if not api.is_complete(state) and state.get("current_phase") != "2b-sign-off":
                await self._drive()

    async def _run_phase(self, phase: str, agent: str | None, prompt: str):
        label = api.PHASE_LABELS.get(phase, phase)
        await self.emit("phase_start", phase=phase, label=label, skill=agent)
        await self.emit(
            "agent", role="system",
            text=f"**Phase {label}** — running `{agent or 'orchestrator'}` "
                 f"via **{self.backend}**…",
        )
        runner = make_runner(self.backend)
        self.active_runner = runner
        scratch = SCRATCH / self.project / ".agent-runs"
        buffer: list[str] = []
        self.running = True
        try:
            async for ev in runner.run(prompt, scratch, mode="phase"):
                if ev.kind == "text":
                    buffer.append(ev.text)
                    await self.emit("agent_delta", phase=phase, text=ev.text)
                elif ev.kind == "tool":
                    await self.emit("tool", phase=phase, text=ev.text,
                                    tool=(ev.meta or {}).get("tool"))
                elif ev.kind == "session":
                    self.session_id = ev.meta.get("session_id") or self.session_id
                elif ev.kind == "error":
                    await self.emit("agent", role="system", text=f"⚠️ {ev.text}")
                elif ev.kind == "done":
                    self.session_id = ev.meta.get("session_id") or self.session_id
        finally:
            self.running = False
            self.active_runner = None
        # Flush the streamed text as one finalized assistant message.
        if buffer:
            await self.emit("agent_final", phase=phase, role="assistant",
                            text="".join(buffer))

    # -- sign-off gate -----------------------------------------------------

    async def _present_gate(self):
        self.awaiting_gate = True
        data = await asyncio.to_thread(api.signoff_data, self.project)
        await self.emit_state()
        await self.emit("gate", **data)

    async def _approve(self, deploy_mode: str):
        if not self.awaiting_gate:
            await self.emit("agent", role="system",
                            text="Nothing to approve right now.")
            return
        self.awaiting_gate = False
        await self.emit("agent", role="user",
                        text=f"Approved — **{deploy_mode.replace('_', ' ')}**.")
        _, report = await asyncio.to_thread(
            api.advance, self.project, approved=True, deploy_mode=deploy_mode)
        if report.strip():
            await self.emit("agent", role="system", text=report.strip())
        await self.emit_state()
        # Continue the loop (artifacts-only fast-forwards to done; live runs 2c+).
        await self._drive()

    async def _revise(self, feedback: str):
        if not self.awaiting_gate:
            await self.emit("agent", role="system",
                            text="Revisions can only be requested at sign-off.")
            return
        self.awaiting_gate = False
        await self.emit("agent", role="user", text=f"Requested changes: {feedback}")
        _, report = await asyncio.to_thread(
            api.advance, self.project, revise=True, feedback=feedback)
        if report.strip():
            await self.emit("agent", role="system", text=report.strip())
        await self.emit_state()
        await self._drive()

    # -- completion --------------------------------------------------------

    # -- free-form chat (any time) ----------------------------------------

    async def _chat(self, message: str):
        if self.running:
            await self.emit("agent", role="system",
                            text="⏳ The agent is mid-phase — please resend once it finishes.")
            return
        # The client renders the user's message optimistically, so we don't echo
        # role=user here (avoids a duplicate bubble).
        state = api.status(self.project)
        context = f"Project: {state.get('display_name', self.project)} (folder: _projects/{self.project})."
        runner = make_runner(self.backend)
        self.active_runner = runner
        scratch = SCRATCH / self.project / ".agent-runs"
        buffer: list[str] = []
        self.running = True
        await self.emit("chat_start")
        try:
            async for ev in runner.run(message, scratch, mode="chat",
                                       resume_session=self.session_id, context=context):
                if ev.kind == "text":
                    buffer.append(ev.text)
                    await self.emit("agent_delta", text=ev.text)
                elif ev.kind == "tool":
                    await self.emit("tool", text=ev.text, tool=ev.meta.get("tool"))
                elif ev.kind == "session":
                    self.session_id = ev.meta.get("session_id") or self.session_id
                elif ev.kind == "error":
                    await self.emit("agent", role="system", text=f"⚠️ {ev.text}")
                elif ev.kind == "done":
                    self.session_id = ev.meta.get("session_id") or self.session_id
        finally:
            self.running = False
            self.active_runner = None
        if buffer:
            await self.emit("agent_final", role="assistant", text="".join(buffer))
        await self.emit_state()  # the agent may have edited files

    # -- go back / redo a stage -------------------------------------------

    async def _reset_to(self, phase: str, rerun: bool):
        if self.running:
            await self.emit("agent", role="system",
                            text="⏳ Can't jump phases while the agent is running — hold on.")
            return
        _, report = await asyncio.to_thread(api.reset, self.project, phase)
        label = api.PHASE_LABELS.get(phase, phase)
        await self.emit("agent", role="system",
                        text=f"↩️ Reset to **{label}**. Later phases are cleared.")
        if report.strip():
            await self.emit("agent", role="system", text=report.strip())
        await self.emit_state()
        if rerun:
            await self._drive()

    # -- error check / heal ------------------------------------------------

    async def _check(self, heal: bool):
        report = await asyncio.to_thread(api.check_errors, self.project)
        issues = report["issues"]
        if not issues:
            await self.emit("agent", role="system",
                            text="✅ **Health check passed** — every completed phase has valid output and state is consistent.")
        else:
            lines = ["🔎 **Health check found:**"]
            for i in issues:
                icon = "❌" if i["severity"] == "error" else "⚠️"
                lines.append(f"- {icon} **{i['label']}** — {i['detail']}")
            await self.emit("agent", role="system", text="\n".join(lines))
        if heal:
            _, rec = await asyncio.to_thread(api.reconcile, self.project)
            await self.emit("agent", role="system",
                            text="🩹 **Reconcile:**\n" + (rec.strip() or "no changes"))
            await self.emit_state()
        await self.emit("checked", issues=issues, ok=report["ok"])

    async def _announce_complete(self):
        self.awaiting_gate = False
        docs = api.list_docs(self.project)
        brief = "docs/project-brief.md" if "project-brief.md" in docs else None
        await self.emit_state()
        msg = "✅ **Pipeline complete.** "
        if brief:
            msg += f"Your project brief is ready — open `{brief}` from the sidebar."
        await self.emit("agent", role="assistant", text=msg)
        await self.emit("complete", brief=brief, docs=docs)
