# Semantic Capability Layer (additive to lexical signal mapper)

We added a deterministic-with-LLM-fallback **capability** layer on top of the existing lexical signal mapper, so problem statements like *"data rich, insight poor — get it into our data scientists' hands"* deterministically resolve to **Notebook + Lakehouse + DataAgent** rather than missing them.

## Context

The lexical `signal-mapper.py` matches keyword → category → task-flow. It works well when users use vocabulary the registry already contains (e.g., "real-time", "alerts"). It fails when users describe **intent** in plain language ("AI angle", "mountain of data we want to make use of", "chat with our data") because no individual word is in the v2 weighted-keyword registry.

Concretely, on the OutageIQ project the lexical mapper missed three architectural items in sequence (Lakehouse, Notebook, DataAgent) — each surfaced only after the user pushed back. Root cause: words → categories, not meaning → capabilities.

## Decision

Add a **second deterministic layer** plus an **optional skill-orchestrated LLM augmentation** layer:

```
Layer 1 (existing)  signal-mapper.py       words → categories → task-flow candidates
Layer 2 (new)       capability-mapper.py   intent patterns → capabilities → Fabric items
Layer 3 (optional)  skill-orchestrated LLM gap-fill when Layer 2 coverage < 0.6
```

The new layer:

- Reads a new registry at `_shared/registry/capability-registry.json` (10 intents → 10 capabilities → Fabric items).
- Extracts intents with weighted regex patterns (`strong`/`moderate`/`weak`), reusing the negation-suppression logic from `signal-mapper.py`.
- Emits `required_capabilities[]` and `required_items[]` for the handoff scaffolder to union into the architecture.
- When coverage falls below 0.6, writes a small JSON **prompt bundle** to `_projects/<p>/docs/.capability-llm-prompt.json` and exits 2. The `fabric-discover` skill (the agent itself) performs intent extraction and persists the result via `capability-writer.py`. Re-running the mapper merges LLM intents with deterministic ones.
- The LLM result is **cached** keyed by `(sha256(problem_text)[:16], registry_version)`, so the same inputs produce the same merged output indefinitely.

## Consequences

**Positive**

- OutageIQ-class problem statements now resolve to the right items on the first pass (verified by `test_capability_mapper.py`).
- The architectural design phase has a hard checklist tied to capability IDs (not free-text user phrases), so missing items are catchable mechanically.
- Layer 1 untouched. Existing task-flow scoring keeps working for all current projects.
- LLM augmentation lives in the skill orchestrator — no new dependencies, no API keys in scripts, fully reproducible via cache.

**Negative / trade-offs**

- One more registry to maintain. Mitigation: kept small (10 intents) and `capability_to_items` references are validated against `item-type-registry.json` at load time.
- LLM augmentation runs only when explicit `--emit-prompt-bundle` is passed *and* coverage is low. Healing flows (`fabric-heal`) can also use it but are out of scope for this ADR.
- Pattern registry can drift from real-world user phrasing. Mitigation: every problem statement that triggers LLM augmentation produces a cached artifact engineers can review to harvest new patterns into the registry.

## Out of scope

- Replacing the lexical mapper — both layers run.
- Embedding-based vector similarity — the pattern + skill-LLM path covers this need without adding a model dependency.
- Auto-deploying capability-required items — capabilities feed the **handoff scaffolder** only; the architect still reviews and approves.

## Verification

- 900+ pytest tests pass after change.
- New tests:
  - `test_capability_registry.py` — registry schema + cross-reference validation (18 tests)
  - `test_capability_mapper.py` — golden archetypes (OutageIQ, healthcare-RAG, manufacturing-IoT, public-sector-batch) + negation/gap routing (14 tests)
  - `test_capability_llm_layer.py` — prompt bundle, writer validation, cache merge, CLI exit-2 (15 tests)
  - `test_handoff_capability_union.py` — required items get unioned into handoff (3 tests)
