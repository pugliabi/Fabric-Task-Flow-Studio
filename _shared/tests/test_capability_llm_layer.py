"""Phase 2 tests — LLM augmentation surface (no live LLM calls).

Covers:
- Prompt bundle schema
- capability-writer.py validation + cache persistence
- capability-mapper.py cache merge behavior
- --emit-prompt-bundle CLI exit code 2
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPPER = REPO_ROOT / ".github" / "skills" / "fabric-discover" / "scripts" / "capability-mapper.py"
WRITER = REPO_ROOT / ".github" / "skills" / "fabric-discover" / "scripts" / "capability-writer.py"

# Load both scripts as modules
def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


capability_mapper = _load("capability_mapper_phase2", MAPPER)
capability_writer = _load("capability_writer_phase2", WRITER)
sys.path.insert(0, str(REPO_ROOT / "_shared" / "lib"))
from registry_loader import load_capability_registry  # noqa: E402


# A deliberately vague problem statement — won't trip many deterministic patterns
VAGUE_TEXT = (
    "We're a regional cooperative and we want our analytics to be smarter. "
    "Our people in the field have been doing things the old way for years."
)


# ---------------------------------------------------------------------------
# Prompt bundle
# ---------------------------------------------------------------------------


def test_prompt_bundle_has_required_fields():
    registry = load_capability_registry()
    det = capability_mapper.map_capabilities(VAGUE_TEXT, registry)
    bundle = capability_mapper.build_prompt_bundle(VAGUE_TEXT, det, registry)

    for key in ("schema_version", "registry_version", "problem_hash",
                "problem_text", "candidate_intents", "candidates_to_consider",
                "instructions", "response_schema"):
        assert key in bundle, f"missing {key}"
    assert bundle["schema_version"] == "1.0.0"
    assert bundle["registry_version"] == registry["registry_version"]


def test_prompt_bundle_candidate_intents_cover_registry():
    registry = load_capability_registry()
    det = capability_mapper.map_capabilities(VAGUE_TEXT, registry)
    bundle = capability_mapper.build_prompt_bundle(VAGUE_TEXT, det, registry)
    bundle_ids = {c["intent_id"] for c in bundle["candidate_intents"]}
    registry_ids = {i["id"] for i in registry["intents"]}
    assert bundle_ids == registry_ids


def test_prompt_bundle_size_under_budget():
    registry = load_capability_registry()
    det = capability_mapper.map_capabilities(VAGUE_TEXT, registry)
    bundle = capability_mapper.build_prompt_bundle(VAGUE_TEXT, det, registry)
    size = len(json.dumps(bundle))
    assert size < 8192, f"prompt bundle too large ({size} bytes)"


# ---------------------------------------------------------------------------
# capability-writer validation
# ---------------------------------------------------------------------------


def _valid_payload(registry):
    return {
        "schema_version": "1.0.0",
        "problem_hash": "deadbeefdeadbeef",
        "registry_version": registry["registry_version"],
        "intents": [
            {
                "intent_id": "expose-data-to-data-scientists",
                "confidence": "high",
                "rationale": "User mentions data scientists explicitly.",
            }
        ],
    }


def test_writer_accepts_valid_payload():
    registry = load_capability_registry()
    errors = capability_writer.validate_response(_valid_payload(registry), registry)
    assert errors == []


def test_writer_rejects_unknown_intent_id():
    registry = load_capability_registry()
    payload = _valid_payload(registry)
    payload["intents"][0]["intent_id"] = "totally-made-up"
    errors = capability_writer.validate_response(payload, registry)
    assert any("not in registry" in e for e in errors)


def test_writer_rejects_bad_confidence():
    registry = load_capability_registry()
    payload = _valid_payload(registry)
    payload["intents"][0]["confidence"] = "extra-spicy"
    errors = capability_writer.validate_response(payload, registry)
    assert any("confidence must be one of" in e for e in errors)


def test_writer_rejects_stale_registry_version():
    registry = load_capability_registry()
    payload = _valid_payload(registry)
    payload["registry_version"] = "1999.01.01-1"
    errors = capability_writer.validate_response(payload, registry)
    assert any("registry_version" in e for e in errors)


def test_writer_rejects_empty_rationale():
    registry = load_capability_registry()
    payload = _valid_payload(registry)
    payload["intents"][0]["rationale"] = "   "
    errors = capability_writer.validate_response(payload, registry)
    assert any("rationale" in e for e in errors)


def test_writer_rejects_duplicate_intents():
    registry = load_capability_registry()
    payload = _valid_payload(registry)
    payload["intents"].append(dict(payload["intents"][0]))
    errors = capability_writer.validate_response(payload, registry)
    assert any("duplicated" in e for e in errors)


def test_writer_round_trip_cache(tmp_path):
    registry = load_capability_registry()
    # Stand up a temp project layout
    project_root = tmp_path / "_projects" / "tmpproj"
    (project_root / "docs").mkdir(parents=True)
    payload = _valid_payload(registry)
    cache_path = capability_writer.write_cache("tmpproj", payload, repo_root=tmp_path)
    assert cache_path.exists()
    with open(cache_path, encoding="utf-8") as f:
        round_trip = json.load(f)
    assert round_trip == payload


# ---------------------------------------------------------------------------
# Mapper-side cache merge
# ---------------------------------------------------------------------------


def test_mapper_merges_llm_intents_into_capabilities():
    registry = load_capability_registry()
    llm_intents = [
        {
            "intent_id": "expose-data-to-data-scientists",
            "confidence": "high",
            "rationale": "Quote: 'analytics to be smarter'",
        },
        {
            "intent_id": "modernize-field-intake",
            "confidence": "medium",
            "rationale": "Quote: 'people in the field doing things the old way'",
        },
    ]
    result = capability_mapper.map_capabilities(
        VAGUE_TEXT, registry, llm_intents=llm_intents
    )
    cap_ids = [c["capability_id"] for c in result["required_capabilities"]]
    assert "python-ml-runtime" in cap_ids
    assert "historical-medallion-store" in cap_ids
    assert "mobile-field-intake" in cap_ids

    # Source labels reflect provenance
    src_for_python_ml = next(c["source"] for c in result["required_capabilities"]
                              if c["capability_id"] == "python-ml-runtime")
    assert src_for_python_ml in {"llm", "merged"}

    # And needs_llm flips OFF once cache is present
    assert result["needs_llm_augmentation"] is False


def test_load_cached_llm_response_rejects_stale_hash(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "problem_hash": "AAAA",
        "registry_version": "x",
        "intents": [],
    }), encoding="utf-8")
    assert capability_mapper.load_cached_llm_response(
        cache_path, "BBBB", "x") is None


def test_load_cached_llm_response_rejects_stale_registry_version(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "problem_hash": "AAAA",
        "registry_version": "old",
        "intents": [],
    }), encoding="utf-8")
    assert capability_mapper.load_cached_llm_response(
        cache_path, "AAAA", "new") is None


def test_load_cached_llm_response_returns_intents_on_match(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "problem_hash": "AAAA",
        "registry_version": "v",
        "intents": [{"intent_id": "x", "confidence": "high", "rationale": "r"}],
    }), encoding="utf-8")
    res = capability_mapper.load_cached_llm_response(cache_path, "AAAA", "v")
    assert res == [{"intent_id": "x", "confidence": "high", "rationale": "r"}]


# ---------------------------------------------------------------------------
# CLI surface: --emit-prompt-bundle exits 2
# ---------------------------------------------------------------------------


def test_cli_emit_prompt_bundle_exits_2(tmp_path, monkeypatch):
    # Need a scaffolded project — make one under the real repo to satisfy
    # the project-validator, but write into a tmp docs/ dir we then clean up.
    project_name = "_tmp_phase2_test"
    project_dir = REPO_ROOT / "_projects" / project_name
    (project_dir / "docs").mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [sys.executable, str(MAPPER),
             "--project", project_name,
             "--text", VAGUE_TEXT,
             "--emit-prompt-bundle",
             "--coverage-threshold", "0.9"],
            capture_output=True, text=True, encoding="utf-8",
        )
        assert proc.returncode == 2, (
            f"expected exit 2, got {proc.returncode}\nstdout={proc.stdout}\n"
            f"stderr={proc.stderr}"
        )
        bundle_path = project_dir / "docs" / ".capability-llm-prompt.json"
        assert bundle_path.exists()
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        assert bundle["schema_version"] == "1.0.0"
        assert "candidates_to_consider" in bundle
    finally:
        # Clean up the temp project
        import shutil
        if project_dir.exists():
            shutil.rmtree(project_dir)
