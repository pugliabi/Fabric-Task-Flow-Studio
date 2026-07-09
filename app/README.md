# Fabric Task Flows Studio

A local **chat web app** that drives the whole Fabric task-flows pipeline for you —
describe a problem, watch an AI agent map the architecture, approve at sign-off, and
get your deliverables. No copy-pasting prompts into a chat window.

It wraps the exact same pipeline library the CLI uses (`_shared/scripts/run-pipeline.py`),
so pipeline semantics and state ownership are unchanged. The app only **reads** state and
docs, calls the same `advance`/`start` functions, and streams everything into a chat UI.

## Quick start

```bash
python run-app.py            # installs fastapi/uvicorn if needed, opens http://127.0.0.1:8000
```

Options: `--port 8000`, `--host 127.0.0.1`, `--no-reload`, `--no-browser`.

## Backends — who drives the agent

Pick one on the start screen (the app auto-detects what's installed):

| Backend | How it's driven | Install |
|---------|-----------------|---------|
| **Claude Code** | `claude -p --output-format stream-json --permission-mode bypassPermissions`. A preamble tells it to read `.github/agents/fabric-advisor.agent.md` + the phase's `SKILL.md` and act as the orchestrator. | `npm i -g @anthropic-ai/claude-code` |
| **GitHub Copilot** | `copilot -p - -s --allow-all-tools --no-ask-user --agent fabric-advisor`. Loads the real `fabric-advisor` agent natively. | `npm i -g @github/copilot` |

**Claude auth:** the Claude backend uses your **Claude Code subscription login**. If
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` is set in your environment, Claude Code would
otherwise prefer that pay-as-you-go key (which can fail with *"credit balance too low"*), so
the runner strips it from the agent subprocess. To force the API key instead, set
`FTF_USE_API_KEY=1`.

Both run **headless**. The agent's only job per phase is to write that phase's doc file;
the app owns every `advance` and the deterministic fast-forward. Nothing edits
`pipeline-state.json` except the pipeline library.

## What happens in a run

1. **Start** → project is scaffolded, signal mapper runs.
2. **Discover** → the agent writes `docs/discovery-brief.md`. The app advances, which
   deterministically fast-forwards Design + Test Plan and lands on the gate.
3. **Sign-off (🛑 human gate)** → the app shows the architecture diagram + a summary and
   two buttons: **Approve · Deploy live** or **Approve · Artifacts only**. Or type feedback
   and **Revise** (max 3 cycles).
4. **Artifacts-only** → the app fast-forwards Deploy → Validate → Document deterministically
   → **complete**. **Live** → the agent runs the deploy/validate/document phases.
5. Deliverables (`discovery-brief`, `architecture-handoff`, `test-plan`,
   `deployment-handoff`, `validation-report`, `project-brief`) appear in the sidebar —
   click any to read it rendered.

## Home page, review, redo, edit, chat

- **Projects dashboard (home)** — the landing page is a live dashboard of every project:
  a card each with a mini 7-phase progress bar, status (● running / 🛑 sign-off /
  complete / idle), phase count (e.g. `3/7`), and the project's **latest activity line**
  (pulled from its saved transcript, so it updates even for background runs). A header
  strip summarizes how many are running / at sign-off / complete. It **polls every ~2.5s**
  (paused when the tab is hidden), so you watch several projects at once with no
  tab-switching. Each card has **Open/View live** and, for running ones, **⏹ Stop**.
- **Chat anytime** — the composer is always live. Type a question or a tweak and the agent
  responds in the same repo context. For **Claude Code** the session is resumed across
  turns (`--resume <session_id>`), so it remembers the conversation. Disabled only while a
  phase agent is actively running.
- **Per-phase actions** — hover any phase in the timeline:
  - 📄 **Review** its deliverable (rendered)
  - ✏️ **Edit** the deliverable inline and **Save**
  - ↻ **Redo** from that phase (resets it + all later phases, then re-runs the agent)
- **Auto-advance toggle** — the sidebar switch controls pacing. **On** (default): phases
  run back-to-back up to the sign-off gate. **Off**: the pipeline pauses after each phase so
  you can review/edit before clicking **Continue** for the next one. Human gates and
  completion always stop regardless. Flipping it on while paused resumes immediately.
- **Continue** — reopening an in-progress project (or a paused one) shows a *Continue
  pipeline* button that resumes driving from the current phase.
- **Check / Heal** — *Check* verifies every completed phase's output and state
  consistency (read-only); *Heal* runs `reconcile` to repair drift from file evidence.
- **Activity indicator** — the moment you do anything (send a message, approve, revise,
  redo, continue, check), a "*<backend> is working…*" bar appears above the composer and
  stays until the agent responds, so you always know something is being processed.
- **Stop** — a running task flow can be halted from the working bar, the sidebar, or a
  running project's card on the home page. Stop kills the agent's whole process tree
  (`taskkill /T`), cancels the drive loop, and leaves the phase re-runnable (Redo / Continue).
  Each project stops independently.
- **Persistent chat history** — every project's conversation (phase headers, messages, tool
  calls, replies) is appended to `_projects/<p>/.studio/history.jsonl`. Reopen a project —
  even after a server restart — and the full transcript replays, then the live state
  re-syncs. You pick up exactly where you left off.

### Layout

The app shell is a fixed 100vh grid: the **sidebar (phase timeline + deliverables) and the
composer/working bar stay pinned** — only the chat transcript scrolls. So scrolling back
through history never hides the phases, the deliverables, or the "working…" indicator.

## Architecture

```
run-app.py                 → launcher (ensures deps, forces UTF-8, runs uvicorn)
app/
  server.py                → FastAPI: REST + SSE, static serving, in-memory sessions
  orchestrator.py          → the drive loop: run agent per phase → advance → stop at gate
  runners.py               → AgentRunner + ClaudeCodeRunner / CopilotRunner, backend detection
  pipeline_api.py          → thin wrapper over _shared/lib (start/advance/status/prompts/docs)
  static/index.html        → self-contained chat UI (timeline, gate card, doc viewer, SSE)
```

### Concurrency — multiple projects at once

Each project is an independent `Session` with its own agent process and event
stream, so several can run **in parallel** (open each in its own browser tab).
The agent CLIs run in worker threads, and the synchronous pipeline-library calls
(`start`, `advance`, precompute — which spawn their own generator subprocesses)
are offloaded off the event loop via `asyncio.to_thread`, serialized by a lock
because that library isn't thread-safe. The result: one project's phase running
never freezes the UI for the others — loading, status, and streaming stay
responsive across all tabs.

> Browser note: HTTP/1.1 allows ~6 live connections per origin and each open
> project tab holds one SSE connection, so keep it to a handful of simultaneous
> tabs.

### How the chat stream works

Each session owns an `asyncio.Queue`. The orchestrator emits events
(`state`, `phase_start`, `agent_delta`, `tool`, `gate`, `complete`, …) onto it; the
`/api/projects/{p}/events` SSE endpoint drains the queue to the browser as
`data: {json}` frames. The queue buffers, so a browser that connects late replays the
full conversation.

## API reference

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/backends` | Which CLIs are installed |
| GET | `/api/projects` | Existing projects (to resume) |
| POST | `/api/start` | `{name, problem, backend}` → scaffold + start driving |
| POST | `/api/projects/{p}/resume` | `{backend}` → re-drive an existing project |
| POST | `/api/projects/{p}/approve` | `{deploy_mode: live|artifacts_only}` |
| POST | `/api/projects/{p}/revise` | `{feedback}` |
| GET | `/api/projects/{p}/status` | Current state + phases + docs |
| GET | `/api/projects/{p}/doc?name=` | One `docs/*.md` file |
| GET | `/api/projects/{p}/events` | SSE chat stream |

## Notes

- **UTF-8 is forced** (`PYTHONUTF8`) — Windows cp1252 otherwise corrupts the box/arrow
  glyphs in diagrams and agent output.
- The app is single-user and local; sessions live in memory and reset when the server
  restarts (pipeline state on disk is the source of truth — just **Resume**).
- Live deployment still requires `az login` in your own terminal, exactly as the CLI does.
