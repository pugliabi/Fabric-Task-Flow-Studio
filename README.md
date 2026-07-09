<p align="center">
  <img src="app/docs/images/banner.png" alt="Fabric Task Flows Studio" width="900"/>
</p>

<p align="center">
  <b>From problem to production — chat your way through the whole Microsoft Fabric pipeline.</b><br/>
  A local web app that drives the <a href="https://github.com/microsoft/fabric-task-flows">Fabric Task Flows</a> pipeline end-to-end with a headless AI agent (<b>Claude Code</b> or <b>GitHub Copilot</b>). You describe the business problem in chat; the agent maps an architecture, tests it, and produces CI/CD-ready deployment artifacts — you just approve at the sign-off gate.
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue"/>
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-web%20app-009688"/>
  <img alt="Backends" src="https://img.shields.io/badge/backends-Claude%20Code%20%C2%B7%20GitHub%20Copilot-7c5cff"/>
  <img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg"/>
</p>

> **Fabric Task Flows Studio** is a fork of Microsoft's [**fabric-task-flows**](https://github.com/microsoft/fabric-task-flows) that adds a full chat-driven web UI on top of the same pipeline engine. The original CLI-and-Copilot workflow is untouched and [documented below](#-built-on-microsoft-fabric-task-flows).

---

## 📸 What it looks like

| Live projects dashboard | Sign-off gate with architecture diagram |
|:--:|:--:|
| ![Dashboard](app/docs/images/01-dashboard.png) | ![Sign-off gate](app/docs/images/02-signoff-gate.png) |
| **Completed run — timeline + deliverables** | **A generated deliverable (editable)** |
| ![Project view](app/docs/images/03-project-view.png) | ![Deliverable](app/docs/images/04-deliverable.png) |

---

## ✨ Highlights

- **Chat-driven** — describe your problem in plain language; the agent does the rest.
- **Two backends** — pick **Claude Code** or **GitHub Copilot** per project; the app auto-detects what's installed.
- **One human gate** — review the architecture diagram and **Approve** (deploy live or artifacts-only) or **Revise**.
- **Live multi-project dashboard** — run several projects at once as background agents and watch them all update, no tab-switching.
- **Full control** — review / edit / redo any phase, chat anytime, auto-advance toggle, and a **Stop** button that halts a run.
- **Persistent** — each project's transcript and deliverables are saved; reopen and pick up exactly where you left off.

---

## 🚀 Quick start

### Prerequisites

- **Python 3.11+**
- At least one agent backend:
  - **Claude Code** — `npm i -g @anthropic-ai/claude-code` (uses your Claude subscription login)
  - **GitHub Copilot CLI** — `npm i -g @github/copilot`

### Install

```bash
git clone https://github.com/pugliabi/Fabric-Task-Flow-Studios.git
cd Fabric-Task-Flow-Studios
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r app/requirements-app.txt
```

### Start

```bash
python run-app.py
```

That installs FastAPI/uvicorn if needed, launches the server, and opens **http://127.0.0.1:8000** in your browser.
Options: `--port 8000`, `--host 127.0.0.1`, `--no-reload`, `--no-browser`.

**Windows:** running `python run-app.py` once registers a **Start Menu** launcher (`fabric-studio.bat`) — after that just press <kbd>⊞ Win</kbd> and type *"Fabric Task Flows Studio"*, or run `fabric-studio` in any terminal.

---

## 🧭 How to use it

1. **Describe your problem.** On the home page, enter a project name and a one-paragraph problem statement, pick a backend, and click **Start pipeline**.
2. **Watch it work.** The agent streams its thinking and tool calls into the chat while it discovers signals, selects a task flow, designs the architecture, and writes a test plan. The sidebar timeline shows phase progress.
3. **Approve at the 🛑 sign-off gate.** You get a plain-language summary plus the architecture diagram, and two buttons:
   - **Approve · Deploy live** — generates and runs the deployment against a Fabric workspace (needs `az login`).
   - **Approve · Artifacts only** — generates the CI/CD-ready scripts without deploying.
   - Or type feedback and **Revise** (up to 3 cycles).
4. **Get your deliverables.** Everything lands in the sidebar — `discovery-brief`, `architecture-handoff`, `test-plan`, `deployment-handoff`, `validation-report`, and a synthesized `project-brief`. Click any to read it rendered, or **Edit** it inline.

**Along the way you can:** chat with the agent anytime · **review / edit / redo** any phase from the timeline · toggle **auto-advance** off to step phase-by-phase · **Stop** a run · and run **multiple projects in parallel** from the dashboard.

📖 **Full technical documentation:** [`app/README.md`](app/README.md) — architecture, API reference, event streaming, concurrency model, and more.

---

## 📦 What you get per project

```
_projects/your-project/          (local only — gitignored)
├── docs/                         ← discovery, architecture, test plan, validation, project brief
├── deploy/                       ← CI/CD-ready deployment scripts + workspace definitions
└── .studio/history.jsonl         ← saved chat transcript (so you can pick up where you left off)
```

---

## 🏗️ Built on Microsoft Fabric Task Flows

The Studio is a UI layer over Microsoft's **[fabric-task-flows](https://github.com/microsoft/fabric-task-flows)** — the same `@fabric-advisor` agent, skills, registries, and templates power everything under the hood. The original workflow (drive the pipeline from the CLI + Copilot chat) still works exactly as before.

| Resource | Description |
|----------|-------------|
| [**13 Task Flows**](task-flows.md) | Pre-defined architectures — batch, streaming, hybrid, ML, API, governance, and more |
| [**7 Decision Guides**](decisions/_index.md) | Opinionated guidance on the choices that matter |
| [**Deployment Diagrams**](diagrams/_index.md) | Visual maps showing how pieces connect and deploy in order |
| [**Item Registry**](_shared/registry/item-type-registry.json) | 45 Fabric item types with API paths, CI/CD strategies, and deployment order |
| [**Templates**](_shared/templates/) | Definition files for every deployable Fabric item type |
| [**Workflow Guide**](_shared/workflow-guide.md) | The full CLI pipeline reference (`run-pipeline.py`) |

### Repository structure

```
app/                    → Fabric Task Flows Studio (the web app)  ★ this fork
run-app.py              → Studio launcher
fabric-studio.bat       → Windows Start Menu launcher
.github/agents/         → The @fabric-advisor orchestrator
.github/skills/         → Composable skills for each phase
decisions/              → Decision guides (the "why" behind each choice)
diagrams/               → Deployment diagrams (how pieces connect)
_shared/registry/       → Canonical registries the agent uses
_shared/templates/      → Starting point templates
_shared/lib/            → Shared Python modules
_shared/scripts/        → Pipeline CLI tools
_shared/tests/          → Test suite
_projects/              → Your project workspaces (gitignored)
```

---

## Contributing & License

Contributions welcome — especially to the **[Item Type Registry](_shared/registry/item-type-registry.json)**. See [CONTRIBUTING.md](CONTRIBUTING.md).

Licensed [MIT](LICENSE). Original project © Microsoft. Studio additions maintained in this fork.
