"""Phase 3 — verifies capability-required items get unioned into the handoff."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAFFOLDER = REPO_ROOT / ".github" / "skills" / "fabric-design" / "scripts" / "handoff-scaffolder.py"

spec = importlib.util.spec_from_file_location("handoff_scaffolder_phase3", SCAFFOLDER)
mod = importlib.util.module_from_spec(spec)
sys.modules["handoff_scaffolder_phase3"] = mod
spec.loader.exec_module(mod)


def test_scaffold_unions_capability_items_into_event_medallion():
    # event-medallion default has no Notebook / Lakehouse — they should be
    # added when capability mapper says they're required.
    md = mod.scaffold(
        task_flow="event-medallion",
        project="TestProject",
        decisions=None,
        required_items=["Notebook", "Lakehouse", "DataAgent"],
    )
    assert "Notebook" in md
    assert "Lakehouse" in md
    assert "DataAgent" in md
    assert "Required by capability mapper" in md


def test_scaffold_does_not_duplicate_existing_items():
    # Eventhouse IS in event-medallion default; passing it again must not
    # add a second copy.
    md = mod.scaffold(
        task_flow="event-medallion",
        project="TestProject",
        decisions=None,
        required_items=["Eventhouse"],
    )
    # Eventhouse should appear in items list at most a small number of times
    # (once in items YAML, once in waves YAML, once in ACs). No "Required by
    # capability mapper" tag for it because it was already present.
    assert md.count("Required by capability mapper") == 0


def test_scaffold_without_required_items_unchanged():
    baseline = mod.scaffold("event-medallion", "P", decisions=None)
    augmented = mod.scaffold("event-medallion", "P", decisions=None,
                             required_items=None)
    assert baseline == augmented
