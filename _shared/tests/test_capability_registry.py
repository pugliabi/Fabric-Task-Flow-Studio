"""Tests for the capability registry — schema validity, cross-references, and loader behavior."""

import sys
from pathlib import Path

import pytest

SHARED_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SHARED_DIR / "lib"))

from registry_loader import (
    load_capability_registry,
    load_registry,
    _validate_capability_registry,
)


# ── Module-level sanity ──────────────────────────────────────────────────


def test_registry_loads_successfully():
    """Capability registry loads without errors."""
    registry = load_capability_registry()
    assert isinstance(registry, dict)
    assert "intents" in registry
    assert "capabilities" in registry


def test_registry_has_required_top_level_fields():
    """Schema metadata fields are present."""
    registry = load_capability_registry()
    assert "schema" in registry
    assert "version" in registry
    assert "registry_version" in registry, (
        "registry_version is required for LLM cache invalidation"
    )


def test_intents_are_non_empty():
    """At least the 10 seeded intents are present."""
    registry = load_capability_registry()
    assert len(registry["intents"]) >= 10


def test_capabilities_are_non_empty():
    """At least the 11 seeded capabilities are present."""
    registry = load_capability_registry()
    assert len(registry["capabilities"]) >= 10


# ── Intent schema ────────────────────────────────────────────────────────


def test_each_intent_has_required_fields():
    """Every intent must have id, summary, patterns, llm_hint, requires_capabilities."""
    registry = load_capability_registry()
    required = {"id", "summary", "patterns", "llm_hint", "requires_capabilities"}
    for intent in registry["intents"]:
        missing = required - set(intent.keys())
        assert not missing, f"Intent {intent.get('id')} missing fields: {missing}"


def test_intent_ids_are_unique():
    """No duplicate intent ids."""
    registry = load_capability_registry()
    ids = [i["id"] for i in registry["intents"]]
    assert len(ids) == len(set(ids)), f"Duplicate intent ids: {ids}"


def test_intent_patterns_use_weight_levels():
    """Patterns must use strong/moderate/weak keys (matching signal-categories v2 convention)."""
    registry = load_capability_registry()
    allowed = {"strong", "moderate", "weak"}
    for intent in registry["intents"]:
        levels = set(intent["patterns"].keys())
        assert levels.issubset(allowed), (
            f"Intent {intent['id']} has unknown pattern levels: {levels - allowed}"
        )


# ── Capability schema ────────────────────────────────────────────────────


def test_each_capability_has_required_fields():
    """Every capability must have id, summary, satisfied_by_items."""
    registry = load_capability_registry()
    required = {"id", "summary", "satisfied_by_items"}
    for cap in registry["capabilities"]:
        missing = required - set(cap.keys())
        assert not missing, f"Capability {cap.get('id')} missing fields: {missing}"


def test_capability_ids_are_unique():
    """No duplicate capability ids."""
    registry = load_capability_registry()
    ids = [c["id"] for c in registry["capabilities"]]
    assert len(ids) == len(set(ids)), f"Duplicate capability ids: {ids}"


def test_every_capability_satisfies_at_least_one_item():
    """A capability with no satisfying items is useless."""
    registry = load_capability_registry()
    for cap in registry["capabilities"]:
        assert len(cap["satisfied_by_items"]) >= 1, (
            f"Capability {cap['id']} has no satisfied_by_items"
        )


# ── Cross-references ─────────────────────────────────────────────────────


def test_every_intent_references_real_capabilities():
    """Every intent.requires_capabilities entry must resolve to a real capability."""
    registry = load_capability_registry()
    cap_ids = {c["id"] for c in registry["capabilities"]}
    for intent in registry["intents"]:
        for cap_ref in intent["requires_capabilities"]:
            assert cap_ref in cap_ids, (
                f"Intent {intent['id']} references unknown capability '{cap_ref}'"
            )


def test_every_capability_item_exists_in_item_registry():
    """Every Fabric item referenced by a capability must exist in item-type-registry."""
    registry = load_capability_registry()
    item_registry = load_registry()
    item_keys = set(item_registry.keys())

    for cap in registry["capabilities"]:
        for item in cap["satisfied_by_items"]:
            assert item in item_keys, (
                f"Capability {cap['id']} satisfied_by_items references unknown "
                f"Fabric item '{item}' (not in item-type-registry.json)"
            )
        for item in cap.get("supporting_items", []):
            assert item in item_keys, (
                f"Capability {cap['id']} supporting_items references unknown "
                f"Fabric item '{item}'"
            )


def test_every_capability_is_referenced_by_some_intent():
    """Capabilities not referenced by any intent are dead weight — warn loudly."""
    registry = load_capability_registry()
    referenced = set()
    for intent in registry["intents"]:
        referenced.update(intent["requires_capabilities"])

    unreferenced = [
        c["id"] for c in registry["capabilities"] if c["id"] not in referenced
    ]
    assert not unreferenced, (
        f"Capabilities not referenced by any intent: {unreferenced}. "
        "Either add an intent that requires them or remove them."
    )


# ── Validator behavior ───────────────────────────────────────────────────


def test_validator_catches_duplicate_intent_ids():
    """Validator must flag duplicate intent ids."""
    bad = {
        "intents": [
            {"id": "x", "requires_capabilities": []},
            {"id": "x", "requires_capabilities": []},
        ],
        "capabilities": [],
    }
    errors = _validate_capability_registry(bad)
    assert any("Duplicate intent ids" in e for e in errors)


def test_validator_catches_unknown_capability_reference():
    """Validator must flag an intent referencing a missing capability."""
    bad = {
        "intents": [{"id": "x", "requires_capabilities": ["does-not-exist"]}],
        "capabilities": [],
    }
    errors = _validate_capability_registry(bad)
    assert any("unknown capability" in e for e in errors)


def test_validator_catches_unknown_item_reference():
    """Validator must flag a capability referencing a non-existent Fabric item type."""
    bad = {
        "intents": [],
        "capabilities": [
            {"id": "c", "satisfied_by_items": ["NotARealFabricItem"]}
        ],
    }
    errors = _validate_capability_registry(bad)
    assert any("NotARealFabricItem" in e for e in errors)


def test_validator_passes_for_loaded_registry():
    """The actual on-disk registry must validate cleanly."""
    registry = load_capability_registry()
    errors = _validate_capability_registry(registry)
    assert errors == [], f"On-disk registry has validation errors: {errors}"


# ── Caching ──────────────────────────────────────────────────────────────


def test_load_is_cached():
    """Repeated calls return the same dict instance."""
    a = load_capability_registry()
    b = load_capability_registry()
    assert a is b, "Capability registry load should be cached"
