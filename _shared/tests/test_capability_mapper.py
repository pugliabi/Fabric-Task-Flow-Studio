"""Tests for the deterministic capability-mapper layer.

Covers golden fixtures, negation handling, low-confidence gap routing, and
the contract surface consumed by downstream artifacts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "skills" / "fabric-discover" / "scripts" / "capability-mapper.py"

sys.path.insert(0, str(REPO_ROOT / ".github" / "skills" / "fabric-discover" / "scripts"))
import importlib.util
spec = importlib.util.spec_from_file_location("capability_mapper", SCRIPT)
capability_mapper = importlib.util.module_from_spec(spec)
sys.modules["capability_mapper"] = capability_mapper
spec.loader.exec_module(capability_mapper)  # type: ignore


# ---------------------------------------------------------------------------
# Golden archetype fixtures — these MUST pass deterministically (no LLM)
# ---------------------------------------------------------------------------

OUTAGEIQ_TEXT = """
As a utility company we have pen and paper processes for storm response.
We need real-time box signals to flow to analyst reports.
We're a SQL shop with batch jobs but we want to learn modern patterns.
We have a mountain of data from 20 years that we want to put in our
data scientists hands. If there's an AI angle leadership will cut red
tape. We want to chat with our data.
""".strip()

HEALTHCARE_RAG_TEXT = """
We're a hospital network. Clinicians need to ask questions about
patient data using natural language. We have HIPAA compliance
requirements and sensitive PHI to protect. Our analysts use Power BI
self-service. We want a conversational AI experience over our
historical records.
""".strip()

MANUFACTURING_IOT_TEXT = """
Our factory floor has thousands of IoT sensors emitting telemetry every
second. We need real-time alerts when temperature thresholds are
breached. The data goes to a streaming pipeline and we want to trigger
notifications immediately.
""".strip()

PUBLIC_SECTOR_BATCH_TEXT = """
We are a government agency consolidating data from many SQL databases.
We need scheduled nightly batch loads — no streaming required. Our team
prefers low-code drag-and-drop tools. We want a semantic model for
self-service BI reporting.
""".strip()


@pytest.fixture(scope="module")
def registry():
    from registry_loader import load_capability_registry
    return load_capability_registry()


def _ids(items): return [i["capability_id"] for i in items]


def test_outageiq_surfaces_ml_lakehouse_and_dataagent():
    result = capability_mapper.map_capabilities(OUTAGEIQ_TEXT)
    cap_ids = _ids(result["required_capabilities"])
    # These are the items the original lexical mapper missed
    assert "python-ml-runtime" in cap_ids
    assert "historical-medallion-store" in cap_ids
    assert "conversational-query" in cap_ids
    # And the resolved Fabric items
    assert "Notebook" in result["required_items"]
    assert "Lakehouse" in result["required_items"]
    assert "DataAgent" in result["required_items"]


def test_outageiq_also_keeps_streaming_signals():
    result = capability_mapper.map_capabilities(OUTAGEIQ_TEXT)
    cap_ids = _ids(result["required_capabilities"])
    assert "low-latency-event-store" in cap_ids
    assert "streaming-ingest" in cap_ids
    assert "Eventhouse" in result["required_items"]
    assert "Eventstream" in result["required_items"]


def test_healthcare_rag_detects_conversational_and_compliance():
    result = capability_mapper.map_capabilities(HEALTHCARE_RAG_TEXT)
    cap_ids = _ids(result["required_capabilities"])
    assert "conversational-query" in cap_ids
    assert "secrets-governance" in cap_ids
    assert "DataAgent" in result["required_items"]


def test_manufacturing_iot_detects_streaming_and_alerts():
    result = capability_mapper.map_capabilities(MANUFACTURING_IOT_TEXT)
    cap_ids = _ids(result["required_capabilities"])
    assert "streaming-ingest" in cap_ids
    assert "low-latency-event-store" in cap_ids
    assert "alerting-trigger" in cap_ids


def test_public_sector_batch_prefers_low_code_no_streaming():
    result = capability_mapper.map_capabilities(PUBLIC_SECTOR_BATCH_TEXT)
    cap_ids = _ids(result["required_capabilities"])
    assert "low-code-ingest" in cap_ids
    assert "semantic-self-service" in cap_ids
    # Should NOT trip streaming when explicitly negated
    assert "streaming-ingest" not in cap_ids


# ---------------------------------------------------------------------------
# Negation / gap routing
# ---------------------------------------------------------------------------


def test_negation_suppresses_intent():
    text = "We do not need real-time processing. Batch loads at night are fine."
    result = capability_mapper.map_capabilities(text)
    cap_ids = _ids(result["required_capabilities"])
    assert "streaming-ingest" not in cap_ids
    assert "low-latency-event-store" not in cap_ids
    assert any(g["type"] == "negated_phrases" for g in result["gaps"])


def test_low_confidence_intent_lands_in_gaps_not_capabilities():
    # Single weak-only phrase → low confidence → gap, not capability
    text = "We worry about data silos but have no other concerns."
    result = capability_mapper.map_capabilities(text)
    cap_ids = _ids(result["required_capabilities"])
    assert "python-ml-runtime" not in cap_ids
    assert any(g["type"] == "low_confidence_intent" for g in result["gaps"])


def test_empty_text_yields_empty_capabilities():
    result = capability_mapper.map_capabilities("")
    assert result["required_capabilities"] == []
    assert result["required_items"] == []
    assert result["needs_llm_augmentation"] is True


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


def test_output_includes_registry_version_and_problem_hash(registry):
    result = capability_mapper.map_capabilities(OUTAGEIQ_TEXT, registry)
    assert result["registry_version"] == registry["registry_version"]
    assert len(result["problem_hash"]) == 16


def test_all_required_items_are_in_item_type_registry():
    from registry_loader import load_registry
    item_registry = load_registry()
    valid_keys = set(item_registry.keys())
    result = capability_mapper.map_capabilities(OUTAGEIQ_TEXT)
    for item in result["required_items"]:
        assert item in valid_keys, f"{item} not in item-type-registry.json"


def test_capabilities_are_sorted_for_stable_output():
    result = capability_mapper.map_capabilities(OUTAGEIQ_TEXT)
    cap_ids = _ids(result["required_capabilities"])
    assert cap_ids == sorted(cap_ids)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_intake_mode_returns_json():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--intake", "--text", OUTAGEIQ_TEXT,
         "--format", "json"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "required_items" in payload
    assert "Notebook" in payload["required_items"]


def test_cli_requires_project_or_intake():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--text", "anything"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode != 0
    assert "project" in proc.stderr.lower()


def test_cli_yaml_format():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--intake", "--text", OUTAGEIQ_TEXT,
         "--format", "yaml"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0
    assert "required_capabilities:" in proc.stdout
    assert "required_items:" in proc.stdout
