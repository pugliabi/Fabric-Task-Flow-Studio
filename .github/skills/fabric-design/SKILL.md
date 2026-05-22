---
name: fabric-design
description: >
  The Architect skill — designs Microsoft Fabric architectures and produces
  the FINAL architecture handoff. Use when user says "design architecture",
  "which task flow", "medallion vs lambda", "create architecture", or asks
  about Fabric architecture patterns. Do NOT use for deployment (use
  fabric-deploy), testing (use fabric-test), or discovery (use
  fabric-discover).
---

# Fabric Architecture Design (Architect Role)

## Phase 1-design: Architecture Design

### Step 1: Load Discovery Brief

Read `_projects/[name]/docs/discovery-brief.md` for inferred signals, 4V's, and task flow candidates.

### Step 2: Select Task Flow

Reference `task-flows.md`. For complex multi-pattern requirements, compose a **hybrid** using a base flow + overlays (document rationale inline in the handoff).

### Step 3: Resolve Architectural Decisions

```bash
python .github/skills/fabric-design/scripts/decision-resolver.py --discovery-brief _projects/[name]/docs/discovery-brief.md --format yaml
```

- **High confidence** → accept the choice
- **Ambiguous** → read the referenced guide to resolve, present trade-offs to user

Run `decision-resolver.py --help` for signal keys and fallback options.

### Step 4: Produce FINAL Architecture Handoff

Write to `_projects/[name]/docs/architecture-handoff.md`.

The runner pre-generates `architecture-handoff.md` (items, waves, ACs, decisions, diagram) before this skill is invoked. Your job: (1) verify accuracy against the discovery brief and decision-resolver output, (2) add project-specific rationale, trade-offs, and deployment strategy, (3) replace the `<!-- AGENT: FILL -->` marker in the `## Summary` line (and anywhere else it appears) with a ≤20-word summary of the problem statement.

#### Step 4a: Pre-Handoff Verification Checklist (MUST run before showing user)

Before presenting the architecture for sign-off, verify EACH item below against the discovery brief AND the capability-mapper cache (`_projects/<project>/docs/.capability-mapper-cache.json`). If any check fails, revise the YAML `items:` and `waves:` blocks and regenerate the diagram via `diagram-gen.py` before user review.

**Hard block — Capability coverage**

Open `.capability-mapper-cache.json`. Every entry in `required_items[]` MUST appear in the handoff's `items:` block. The pre-generated handoff already unions them in (via `--capability-cache`); your job is to confirm they survived any edits.

- [ ] Every `required_items[*]` from the cache has a matching item in the handoff `items:` block
- [ ] Every `required_capabilities[*]` is satisfied by at least one item from its `satisfied_by_items` list
- [ ] If a required item was deliberately removed, the handoff's "Alternatives Considered" explains why

**Velocity consistency**
- [ ] If brief says "real-time" / "seconds latency" / "streaming" → handoff includes Eventstream + Eventhouse (or equivalent hot path)
- [ ] If brief says "batch" / "scheduled" / "nightly" → handoff includes Pipeline/CopyJob/Dataflow
- [ ] If brief says both → handoff includes both paths (hybrid)
- [ ] Trade-off rationale text does NOT contradict the brief's velocity (e.g., do not say "batch velocity" when brief is real-time)

**Capability-driven signals** (cross-check against `.capability-mapper-cache.json`)
- [ ] `python-ml-runtime` required → handoff includes a **Notebook**
- [ ] `historical-medallion-store` required → handoff includes a **Lakehouse** with medallion structure
- [ ] `conversational-query` required → handoff includes a **DataAgent**
- [ ] `alerting-trigger` required → handoff includes a **Reflex** (or OperationsAgent)
- [ ] `mobile-field-intake` required → handoff includes a CopyJob or DataPipeline path

**Variety coverage**
- [ ] Every source named in the brief (SQL DBs, sensors/SCADA, mobile forms, APIs, files) has an ingestion item routed to it
- [ ] Mobile/field-form intake → CopyJob or DataPipeline (not Eventstream alone)
- [ ] Historical SQL backfill → CopyJob or DataPipeline

**Versatility alignment**
- [ ] If brief says "low-code" preferred → ingestion/transforms use CopyJob/DataPipeline/Dataflow before Notebook/SparkJobDefinition
- [ ] If brief says "code-first for ML" → Notebook is present for ML workloads

If the runner logged `⚠️ Decision resolver returned exit N` or `⛔ Refusing to fast-forward`, the pre-generated handoff is **boilerplate** — do not trust its trade-offs or item list. Rebuild items/waves from the discovery brief + Step 3 resolver output manually.

## Constraints

- **Preserve YAML code blocks** — `diagram-gen.py` parses `items:` and `waves:` blocks to generate the architecture diagram. Do NOT convert them to markdown tables.
- Never deploy or create Fabric items
- Never skip "Alternatives Considered" or "Trade-offs"
