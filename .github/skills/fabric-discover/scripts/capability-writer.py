#!/usr/bin/env python3
"""
capability-writer.py — persist LLM intent-extraction response to project cache.

Workflow (skill-orchestrated, no API keys needed):

  1. capability-mapper.py --emit-prompt-bundle ...
       → writes `_projects/<p>/docs/.capability-llm-prompt.json`, exits 2
  2. fabric-discover SKILL (the agent) reads the prompt bundle,
     performs intent extraction, then invokes:
       capability-writer.py --project <p> --response-json '<...>'
     (or --response-file <path>) to persist the result.
  3. Re-run capability-mapper.py — it now finds the cache and merges
     LLM intents with deterministic ones.

The cache is the source of reproducibility: same problem_hash +
registry_version → same merged output forever.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Schema version must match capability-mapper.py's PROMPT_BUNDLE_SCHEMA_VERSION
EXPECTED_SCHEMA_VERSION = "1.0.0"
VALID_CONFIDENCES = {"high", "medium", "low"}


def _resolve_repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    for _ in range(10):
        if (candidate / "_projects").is_dir():
            return candidate
        candidate = candidate.parent
    return Path(__file__).resolve().parent


def validate_response(payload: dict, registry: dict) -> list[str]:
    """Return a list of validation errors (empty if valid)."""
    errors: list[str] = []

    if not isinstance(payload, dict):
        return ["response must be a JSON object"]

    for required in ("schema_version", "problem_hash", "registry_version",
                     "intents"):
        if required not in payload:
            errors.append(f"missing required field: {required}")

    if payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {EXPECTED_SCHEMA_VERSION!r}, "
            f"got {payload.get('schema_version')!r}"
        )

    if payload.get("registry_version") != registry.get("registry_version"):
        errors.append(
            f"registry_version {payload.get('registry_version')!r} does not "
            f"match current registry {registry.get('registry_version')!r}. "
            "Re-run the prompt bundle."
        )

    known_intent_ids = {i["id"] for i in registry["intents"]}
    intents = payload.get("intents", [])
    if not isinstance(intents, list):
        errors.append("intents must be an array")
        intents = []

    seen_ids: set[str] = set()
    for idx, intent in enumerate(intents):
        if not isinstance(intent, dict):
            errors.append(f"intents[{idx}] must be an object")
            continue
        iid = intent.get("intent_id")
        conf = intent.get("confidence")
        rationale = intent.get("rationale")
        if iid not in known_intent_ids:
            errors.append(
                f"intents[{idx}].intent_id {iid!r} not in registry"
            )
        if iid in seen_ids:
            errors.append(f"intents[{idx}].intent_id {iid!r} duplicated")
        seen_ids.add(iid)
        if conf not in VALID_CONFIDENCES:
            errors.append(
                f"intents[{idx}].confidence must be one of {VALID_CONFIDENCES}, "
                f"got {conf!r}"
            )
        if not isinstance(rationale, str) or not rationale.strip():
            errors.append(
                f"intents[{idx}].rationale must be a non-empty string"
            )

    return errors


def write_cache(
    project: str,
    payload: dict,
    repo_root: Path | None = None,
) -> Path:
    repo_root = repo_root or _resolve_repo_root()
    cache_path = (repo_root / "_projects" / project / "docs"
                  / ".capability-llm-cache.json")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return cache_path


def _read_response(args: argparse.Namespace) -> dict:
    if args.response_json:
        return json.loads(args.response_json)
    if args.response_file:
        with open(args.response_file, encoding="utf-8") as f:
            return json.load(f)
    if not sys.stdin.isatty():
        return json.loads(sys.stdin.read())
    raise SystemExit("ERROR: provide --response-json, --response-file, or stdin")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Persist an LLM intent-extraction response for a project"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--response-json", type=str,
                        help="Inline JSON payload")
    parser.add_argument("--response-file", type=str,
                        help="Path to a JSON file containing the payload")
    args = parser.parse_args()

    repo_root = _resolve_repo_root()
    if not (repo_root / "_projects" / args.project).is_dir():
        print(f"ERROR: project '{args.project}' not scaffolded under _projects/",
              file=sys.stderr)
        return 1

    sys.path.insert(0, str(repo_root / "_shared" / "lib"))
    from registry_loader import load_capability_registry  # noqa: WPS433

    registry = load_capability_registry()
    try:
        payload = _read_response(args)
    except json.JSONDecodeError as exc:
        print(f"ERROR: response is not valid JSON: {exc}", file=sys.stderr)
        return 1

    errors = validate_response(payload, registry)
    if errors:
        print("❌ capability-writer: validation failed:", file=sys.stderr)
        for e in errors:
            print(f"   - {e}", file=sys.stderr)
        return 1

    cache_path = write_cache(args.project, payload, repo_root=repo_root)
    print(f"✅ capability-writer: cached {len(payload['intents'])} LLM intent(s) "
          f"→ {cache_path}", file=sys.stderr)
    print(f"   Re-run capability-mapper.py to see merged output.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
