"""Phase 5 — decision-resolver consumes capability mapper output.

Locks the additive enrichment matrix: capability ids → signal projections.
The resolver must enrich signals BEFORE resolving decisions, so a brief
that says "AI angle" (no skillset/use_case keywords) still routes to ML
and conversational-AI decision branches when the capability mapper has
detected python-ml-runtime and conversational-query.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "skills" / "fabric-design" / "scripts" / "decision-resolver.py"

# Import the resolver module directly for unit tests on the enrichment fn.
sys.path.insert(0, str(REPO_ROOT / "_shared" / "lib"))
sys.path.insert(0, str(SCRIPT.parent))
import importlib.util
_spec = importlib.util.spec_from_file_location("decision_resolver", SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["decision_resolver"] = _mod
_spec.loader.exec_module(_mod)
enrich = _mod.enrich_signals_with_capabilities
load_cache = _mod._load_capability_cache


class TestEnrichmentMatrix:
    def test_python_ml_runtime_sets_skillset_and_use_case(self):
        out = enrich({}, ["python-ml-runtime"])
        assert out["skillset"] == "code-first"
        assert "ml" in out["use_case"].split("+")

    def test_python_ml_runtime_does_not_override_explicit_skillset(self):
        out = enrich({"skillset": "low-code"}, ["python-ml-runtime"])
        assert out["skillset"] == "low-code"
        assert "ml" in out["use_case"].split("+")

    def test_conversational_query_adds_use_case(self):
        out = enrich({}, ["conversational-query"])
        assert "conversational-ai" in out["use_case"].split("+")

    def test_streaming_plus_batch_brief_yields_both(self):
        out = enrich({"velocity": "batch"}, ["streaming-ingest"])
        assert out["velocity"] == "both"

    def test_low_latency_on_empty_velocity_yields_real_time(self):
        out = enrich({}, ["low-latency-event-store"])
        assert out["velocity"] == "real-time"

    def test_real_time_brief_not_downgraded(self):
        out = enrich({"velocity": "real-time"}, ["streaming-ingest"])
        assert out["velocity"] == "real-time"

    def test_alerting_adds_alerts_use_case(self):
        out = enrich({}, ["alerting-trigger"])
        assert "alerts" in out["use_case"].split("+")

    def test_mobile_field_intake_sets_mixed_data_pattern(self):
        out = enrich({}, ["mobile-field-intake"])
        assert out["data_pattern"] == "mixed"

    def test_low_code_ingest_only_when_skillset_unset(self):
        out = enrich({"skillset": "code-first"}, ["low-code-ingest"])
        assert out["skillset"] == "code-first"
        out2 = enrich({}, ["low-code-ingest"])
        assert out2["skillset"] == "low-code"

    def test_semantic_self_service_sets_interactivity(self):
        out = enrich({}, ["semantic-self-service"])
        assert out["interactivity"] == "interactive"

    def test_empty_capabilities_returns_signals_unchanged(self):
        original = {"skillset": "python", "velocity": "batch"}
        out = enrich(original, [])
        assert out == original

    def test_use_case_dedup(self):
        out = enrich({"use_case": "ml"}, ["python-ml-runtime"])
        assert out["use_case"].split("+").count("ml") == 1


class TestCacheLoader:
    def test_loads_capability_ids(self, tmp_path):
        cache = {
            "required_capabilities": [
                {"capability_id": "python-ml-runtime", "satisfied_by_items": ["Notebook"]},
                {"capability_id": "conversational-query", "satisfied_by_items": ["DataAgent"]},
            ],
            "required_items": ["Notebook", "DataAgent"],
            "coverage": 0.8,
            "gaps": [],
        }
        p = tmp_path / "cache.json"
        p.write_text(json.dumps(cache), encoding="utf-8")
        ids = load_cache(str(p))
        assert "python-ml-runtime" in ids
        assert "conversational-query" in ids

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_cache(str(tmp_path / "nope.json")) == []

    def test_malformed_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        assert load_cache(str(p)) == []


class TestCLIIntegration:
    def test_capability_cache_flag_round_trips(self, tmp_path):
        cache = {"required_capabilities": [
            {"capability_id": "python-ml-runtime", "satisfied_by_items": ["Notebook"]}],
            "required_items": ["Notebook"], "coverage": 0.6, "gaps": []}
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps(cache), encoding="utf-8")

        # Use --signals (minimal) plus the cache; capability should force ML.
        r = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--signals", "{}",
             "--capability-cache", str(cache_path),
             "--format", "json"],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        # CLI accepts the flag; resolver may exit 1 if ambiguous decisions
        # remain after capability enrichment — that's fine, we just want to
        # confirm the flag is wired and produces JSON.
        assert r.returncode in (0, 1), r.stderr
        assert r.stdout.strip(), "expected JSON on stdout"
        json.loads(r.stdout)
