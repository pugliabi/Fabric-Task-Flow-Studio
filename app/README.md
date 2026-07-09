<p align="center">
  <img src="docs/images/banner.png" alt="Fabric Task Flows Studio" width="820"/>
</p>

# Fabric Task Flows Studio

A local **chat web app** that drives the whole Fabric task-flows pipeline for you — describe a
problem, watch an AI agent map the architecture, approve at sign-off, and get your deliverables.

📖 **Full documentation — install, usage, features, architecture, and API reference — lives in the
[repository root README](../README.md).**

### This folder

```
server.py         → FastAPI: REST + SSE, static serving, in-memory sessions
orchestrator.py   → the drive loop: run agent per phase → advance → stop at gate
runners.py        → AgentRunner + ClaudeCodeRunner / CopilotRunner, backend detection
pipeline_api.py   → thin wrapper over _shared/lib (start/advance/status/prompts/docs)
static/           → self-contained chat UI + favicon
docs/images/      → screenshots + brand assets
```

Run it from the repo root: **`start.bat`** (Windows) or **`./start.sh`** (macOS/Linux).
