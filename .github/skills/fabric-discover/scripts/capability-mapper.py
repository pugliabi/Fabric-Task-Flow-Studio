#!/usr/bin/env python3
"""
Deterministic capability mapper — Layer 2 of the semantic signal pipeline.

Reads problem text + the capability registry, extracts intent matches via
weighted regex patterns, resolves intents → required capabilities → required
Fabric item types, and emits a structured output the design phase consumes.

This is the deterministic baseline. When coverage is low or ambiguity is
flagged, the script can emit an LLM prompt bundle for Layer 3 (skill-
orchestrated) augmentation — see ``--gap-mode``.

Output shape (JSON)::

    {
      "project": "...",
      "intent_matches": [
        {"intent_id": "...", "confidence": "high|medium|low",
         "matched_phrases": [...], "weighted_score": int, "source": "deterministic"}
      ],
      "required_capabilities": [
        {"capability_id": "...", "satisfied_by_items": [...],
         "supporting_items": [...], "triggered_by_intents": [...],
         "source": "deterministic"}
      ],
      "required_items": ["Notebook", "Lakehouse", ...],
      "coverage": 0.0..1.0,
      "gaps": [...],
      "needs_llm_augmentation": bool,
      "registry_version": "..."
    }

Usage::

    python capability-mapper.py --project outageiq --text "..."
    python capability-mapper.py --intake --text-file problem.txt --format json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Add _shared/lib so we can import registry_loader (matches signal-mapper.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_shared" / "lib"))
from registry_loader import load_capability_registry, load_stop_words

# Pattern weight levels mirror signal-categories v2 convention
PATTERN_WEIGHTS = {"strong": 3, "moderate": 2, "weak": 1}

# Confidence thresholds (weighted score)
HIGH_CONFIDENCE_SCORE = 5
MEDIUM_CONFIDENCE_SCORE = 3

# Coverage below this triggers LLM augmentation
DEFAULT_COVERAGE_THRESHOLD = 0.6

# Exit code signaling "LLM needed" — fabric-discover SKILL watches for this
EXIT_LLM_NEEDED = 2

# Prompt bundle / cache schema version — bump on breaking changes
PROMPT_BUNDLE_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentPattern:
    intent_id: str
    pattern: re.Pattern[str]
    raw: str
    weight: int


def _compile_patterns(registry: dict) -> list[IntentPattern]:
    compiled: list[IntentPattern] = []
    for intent in registry["intents"]:
        for level, phrases in intent.get("patterns", {}).items():
            weight = PATTERN_WEIGHTS.get(level, 1)
            for raw in phrases:
                try:
                    regex = re.compile(rf"\b{raw}\b", re.IGNORECASE)
                except re.error:
                    # Phrases may already contain regex constructs; bare-compile.
                    try:
                        regex = re.compile(raw, re.IGNORECASE)
                    except re.error as exc:
                        print(
                            f"⚠️  capability-mapper: invalid pattern in intent "
                            f"{intent['id']!r}: {raw!r} ({exc})",
                            file=sys.stderr,
                        )
                        continue
                compiled.append(IntentPattern(
                    intent_id=intent["id"],
                    pattern=regex,
                    raw=raw,
                    weight=weight,
                ))
    return compiled


# ---------------------------------------------------------------------------
# Negation suppression — reuse the same cues as signal-mapper for consistency
# ---------------------------------------------------------------------------

_NEGATION_WINDOW = 60
_NEGATION_CUES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bno\b", r"\bnot\b", r"\bnor\b", r"\bnever\b",
        r"\bdon['\u2019]?t\b", r"\bdo\s+not\b", r"\bdoesn['\u2019]?t\b",
        r"\bwon['\u2019]?t\b", r"\bwithout\b",
        r"\bno\s+need\s+for\b", r"\bnot\s+looking\s+for\b",
        r"\bnot\s+interested\s+in\b",
        r"\beliminate\b", r"\bexclude\b", r"\bavoid\b", r"\bskip\b",
    )
]
_AFFIRMATION_OVERRIDES = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bbut\s+also\b", r"\bbut\s+rather\b", r"\binstead\b", r"\bhowever\b",
    )
]


def _is_negated(text: str, match_start: int) -> bool:
    window_start = max(0, match_start - _NEGATION_WINDOW)
    window = text[window_start:match_start]
    for sep in (".  ", ". ", "! ", "? ", ";\n", ".\n"):
        last_boundary = window.rfind(sep)
        if last_boundary != -1:
            window = window[last_boundary + len(sep):]
    for cue in _NEGATION_CUES:
        m = cue.search(window)
        if m:
            between = window[m.end():]
            if any(aff.search(between) for aff in _AFFIRMATION_OVERRIDES):
                continue
            return True
    return False


# ---------------------------------------------------------------------------
# Core mapping
# ---------------------------------------------------------------------------


@dataclass
class IntentMatch:
    intent_id: str
    matched_phrases: list[tuple[str, int]] = field(default_factory=list)  # (phrase, weight)
    negated_phrases: list[str] = field(default_factory=list)

    @property
    def weighted_score(self) -> int:
        return sum(w for _, w in self.matched_phrases)

    @property
    def confidence(self) -> str:
        s = self.weighted_score
        if s >= HIGH_CONFIDENCE_SCORE:
            return "high"
        if s >= MEDIUM_CONFIDENCE_SCORE:
            return "medium"
        return "low"

    @property
    def unique_phrases(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for phrase, _w in self.matched_phrases:
            if phrase not in seen:
                seen.add(phrase)
                out.append(phrase)
        return out


def map_capabilities(
    text: str,
    registry: dict | None = None,
    llm_intents: list[dict] | None = None,
) -> dict:
    """Map problem text to required Fabric capabilities and items.

    Deterministic by default. If ``llm_intents`` is provided (a list of
    ``{"intent_id": str, "confidence": str, "rationale": str}`` dicts from
    a cached LLM response), they are merged with deterministic matches and
    tagged ``source: llm`` for transparency.
    """
    if registry is None:
        registry = load_capability_registry()

    text = (
        text.replace("\u2019", "'").replace("\u2018", "'")
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2014", "--").replace("\u2013", "-")
    )

    patterns = _compile_patterns(registry)
    intent_matches: dict[str, IntentMatch] = {
        intent["id"]: IntentMatch(intent_id=intent["id"])
        for intent in registry["intents"]
    }

    matched_spans: list[tuple[int, int]] = []
    for ip in sorted(patterns, key=lambda p: -len(p.raw)):
        for m in ip.pattern.finditer(text):
            overlap = any(
                m.start() < ee and m.end() > es
                for es, ee in matched_spans
            )
            if overlap:
                continue
            if _is_negated(text, m.start()):
                intent_matches[ip.intent_id].negated_phrases.append(ip.raw)
                continue
            matched_spans.append((m.start(), m.end()))
            intent_matches[ip.intent_id].matched_phrases.append((ip.raw, ip.weight))

    # Active intents = those with at least one (non-negated) hit
    active_intents = {iid: im for iid, im in intent_matches.items()
                      if im.matched_phrases}

    # ---- LLM augmentation merge -------------------------------------------
    # Treat cached LLM intents as authoritative for their reported confidence.
    # They're tagged source=llm so the architect can audit them.
    _known_intent_ids = {i["id"] for i in registry["intents"]}
    llm_intent_index: dict[str, dict] = {}
    for li in llm_intents or []:
        iid = li.get("intent_id")
        if not iid or iid not in _known_intent_ids:
            continue
        # Normalize confidence; default to medium if missing
        conf = (li.get("confidence") or "medium").lower()
        if conf not in {"high", "medium", "low"}:
            conf = "medium"
        llm_intent_index[iid] = {
            "confidence": conf,
            "rationale": li.get("rationale", ""),
        }

    # Resolve required capabilities + which intents triggered each
    intent_by_id = {i["id"]: i for i in registry["intents"]}
    cap_by_id = {c["id"]: c for c in registry["capabilities"]}
    capability_triggers: dict[str, list[str]] = {}
    capability_sources: dict[str, set[str]] = {}

    for intent_id, im in active_intents.items():
        # Only "medium" or "high" confidence intents trigger capabilities.
        # Low-confidence intents go into the gaps list for review.
        if im.confidence == "low":
            continue
        intent_def = intent_by_id[intent_id]
        for cap_id in intent_def.get("requires_capabilities", []):
            capability_triggers.setdefault(cap_id, []).append(intent_id)
            capability_sources.setdefault(cap_id, set()).add("deterministic")

    # LLM intents promote capabilities too (only medium+ confidence)
    for intent_id, meta in llm_intent_index.items():
        if meta["confidence"] == "low":
            continue
        intent_def = intent_by_id[intent_id]
        for cap_id in intent_def.get("requires_capabilities", []):
            if intent_id not in capability_triggers.setdefault(cap_id, []):
                capability_triggers[cap_id].append(intent_id)
            capability_sources.setdefault(cap_id, set()).add("llm")

    required_capabilities = []
    required_items_ordered: list[str] = []
    seen_items: set[str] = set()
    for cap_id, triggered_by in capability_triggers.items():
        cap = cap_by_id[cap_id]
        sources = sorted(capability_sources.get(cap_id, {"deterministic"}))
        if sources == ["deterministic", "llm"]:
            src_label = "merged"
        elif sources == ["llm"]:
            src_label = "llm"
        else:
            src_label = "deterministic"
        required_capabilities.append({
            "capability_id": cap_id,
            "summary": cap["summary"],
            "satisfied_by_items": cap["satisfied_by_items"],
            "supporting_items": cap.get("supporting_items", []),
            "triggered_by_intents": triggered_by,
            "source": src_label,
        })
        # Primary required item is the FIRST satisfied_by_items entry
        # (registry order is intentional — most-canonical satisfier first)
        if cap["satisfied_by_items"]:
            primary = cap["satisfied_by_items"][0]
            if primary not in seen_items:
                seen_items.add(primary)
                required_items_ordered.append(primary)

    # Coverage = fraction of intents (low+ confidence, det OR llm) over total intents
    total_intents = len(registry["intents"])
    triggered_intent_ids = {
        iid for iid, im in active_intents.items() if im.confidence != "low"
    } | {
        iid for iid, meta in llm_intent_index.items() if meta["confidence"] != "low"
    }
    coverage = (
        round(len(triggered_intent_ids) / total_intents, 2)
        if total_intents else 0.0
    )

    # Gap detection — surface low-confidence hits + negations for review
    gaps: list[dict] = []
    for intent_id, im in intent_matches.items():
        if im.confidence == "low" and im.matched_phrases:
            gaps.append({
                "type": "low_confidence_intent",
                "intent_id": intent_id,
                "matched_phrases": im.unique_phrases,
                "reason": "Weak signal — may need confirmation",
            })
        if im.negated_phrases:
            gaps.append({
                "type": "negated_phrases",
                "intent_id": intent_id,
                "negated_phrases": list(set(im.negated_phrases)),
                "reason": "User negated these phrases — suppressed from active intents",
            })

    # When LLM augmentation has already been applied, don't ask for more.
    needs_llm = coverage < DEFAULT_COVERAGE_THRESHOLD and not llm_intent_index

    # Build intent_matches output — include LLM-only intents alongside deterministic
    intent_match_out = []
    for iid, im in sorted(active_intents.items()):
        entry = {
            "intent_id": iid,
            "confidence": im.confidence,
            "matched_phrases": im.unique_phrases,
            "weighted_score": im.weighted_score,
            "source": "merged" if iid in llm_intent_index else "deterministic",
        }
        if iid in llm_intent_index:
            entry["llm_rationale"] = llm_intent_index[iid]["rationale"]
        intent_match_out.append(entry)
    # LLM-only intents (no deterministic hit)
    for iid in sorted(llm_intent_index.keys() - set(active_intents.keys())):
        meta = llm_intent_index[iid]
        intent_match_out.append({
            "intent_id": iid,
            "confidence": meta["confidence"],
            "matched_phrases": [],
            "weighted_score": 0,
            "source": "llm",
            "llm_rationale": meta["rationale"],
        })

    return {
        "intent_matches": intent_match_out,
        "required_capabilities": sorted(required_capabilities,
                                        key=lambda c: c["capability_id"]),
        "required_items": required_items_ordered,
        "coverage": coverage,
        "gaps": gaps,
        "needs_llm_augmentation": needs_llm,
        "registry_version": registry.get("registry_version", "unknown"),
        "problem_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }


# ---------------------------------------------------------------------------
# CLI (mirrors signal-mapper.py conventions)
# ---------------------------------------------------------------------------


def build_prompt_bundle(
    text: str,
    deterministic_result: dict,
    registry: dict,
) -> dict:
    """Build the LLM prompt bundle written when deterministic coverage is low.

    Skill orchestrator (the agent) reads this bundle, performs intent
    extraction against the listed registry intents, and writes a response
    back via capability-writer.py. Strict JSON-only response schema below.
    """
    # Slim registry view — only what's needed for extraction
    registry_view = []
    for intent in registry["intents"]:
        registry_view.append({
            "intent_id": intent["id"],
            "summary": intent["summary"],
            "llm_hint": intent.get("llm_hint", ""),
        })

    missed_intents = sorted(
        i["intent_id"] for i in registry_view
        if i["intent_id"] not in {m["intent_id"]
                                  for m in deterministic_result["intent_matches"]
                                  if m["confidence"] != "low"}
    )

    return {
        "schema_version": PROMPT_BUNDLE_SCHEMA_VERSION,
        "registry_version": registry.get("registry_version", "unknown"),
        "problem_hash": deterministic_result["problem_hash"],
        "problem_text": text,
        "deterministic_coverage": deterministic_result["coverage"],
        "deterministic_intents": [
            {"intent_id": m["intent_id"], "confidence": m["confidence"]}
            for m in deterministic_result["intent_matches"]
        ],
        "candidate_intents": registry_view,
        "candidates_to_consider": missed_intents,
        "instructions": (
            "Read the problem_text. For each intent_id under candidates_to_consider, "
            "decide whether the problem genuinely expresses that intent — even when "
            "the user does not use exact keywords. Return only intents with "
            "confidence 'high' or 'medium'. Be conservative: if the signal is weak, "
            "omit the intent. Do NOT invent new intent_ids. Cite a short quote from "
            "the problem_text in 'rationale' for every returned intent."
        ),
        "response_schema": {
            "type": "object",
            "required": ["schema_version", "problem_hash", "registry_version",
                         "intents"],
            "properties": {
                "schema_version": {"const": PROMPT_BUNDLE_SCHEMA_VERSION},
                "problem_hash": {"type": "string"},
                "registry_version": {"type": "string"},
                "intents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["intent_id", "confidence", "rationale"],
                        "properties": {
                            "intent_id": {"type": "string"},
                            "confidence": {"enum": ["high", "medium", "low"]},
                            "rationale": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def load_cached_llm_response(cache_path: Path, expected_hash: str,
                             expected_version: str) -> list[dict] | None:
    """Load and validate an LLM response cache file.

    Returns the list of LLM intents if cache is valid for the given hash +
    registry version, else None.
    """
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("problem_hash") != expected_hash:
        return None
    if data.get("registry_version") != expected_version:
        return None
    intents = data.get("intents")
    if not isinstance(intents, list):
        return None
    return intents


def _resolve_repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    for _ in range(10):
        if (candidate / "_projects").is_dir():
            return candidate
        candidate = candidate.parent
    return Path(__file__).resolve().parent


def _validate_project_context(project: str | None, intake: bool) -> None:
    if intake:
        return
    if not project:
        print("ERROR: --project required (or pass --intake for standalone analysis)",
              file=sys.stderr)
        sys.exit(1)
    repo_root = _resolve_repo_root()
    if not (repo_root / "_projects" / project).is_dir():
        print(f"ERROR: project '{project}' not scaffolded under _projects/",
              file=sys.stderr)
        sys.exit(1)


def _read_input(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            return f.read().strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print("ERROR: provide --text, --text-file, or stdin", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Map problem text to required Fabric capabilities (deterministic + skill-orchestrated LLM layer)"
    )
    parser.add_argument("--text", type=str)
    parser.add_argument("--text-file", type=str)
    parser.add_argument("--project", type=str)
    parser.add_argument("--intake", action="store_true",
                        help="Standalone mode — bypasses --project requirement")
    parser.add_argument("--format", choices=["json", "yaml"], default="json")
    parser.add_argument(
        "--emit-prompt-bundle", action="store_true",
        help="If deterministic coverage is below threshold, write an LLM "
             "prompt bundle to <project>/docs/.capability-llm-prompt.json "
             "and exit with code 2 (signaling LLM is needed).",
    )
    parser.add_argument(
        "--cache-file", type=str, default=None,
        help="Path to an LLM response cache file. When present and the "
             "problem_hash + registry_version match, its intents are merged.",
    )
    parser.add_argument(
        "--coverage-threshold", type=float, default=DEFAULT_COVERAGE_THRESHOLD,
        help=f"Minimum deterministic coverage before LLM is requested "
             f"(default: {DEFAULT_COVERAGE_THRESHOLD}).",
    )
    args = parser.parse_args()

    _validate_project_context(args.project, args.intake)
    text = _read_input(args)
    registry = load_capability_registry()

    # Load cached LLM response if present (auto-discover when --project given)
    cache_path: Path | None = None
    if args.cache_file:
        cache_path = Path(args.cache_file)
    elif args.project:
        cache_path = (_resolve_repo_root() / "_projects" / args.project /
                      "docs" / ".capability-llm-cache.json")

    llm_intents = None
    if cache_path:
        # Need problem_hash to validate — compute via a quick deterministic pass
        seed = map_capabilities(text, registry)
        llm_intents = load_cached_llm_response(
            cache_path, seed["problem_hash"], seed["registry_version"],
        )

    result = map_capabilities(text, registry, llm_intents=llm_intents)
    if args.project:
        result["project"] = args.project

    # If still below threshold AND no LLM cache yet AND prompt-bundle requested,
    # write the bundle and exit 2.
    if (args.emit_prompt_bundle
            and not llm_intents
            and result["coverage"] < args.coverage_threshold):
        if not args.project:
            print("ERROR: --emit-prompt-bundle requires --project to know "
                  "where to write the bundle.", file=sys.stderr)
            return 1
        bundle = build_prompt_bundle(text, result, registry)
        bundle_path = (_resolve_repo_root() / "_projects" / args.project /
                       "docs" / ".capability-llm-prompt.json")
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        with open(bundle_path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2)
        print(f"🟡 capability-mapper: coverage {result['coverage']:.2f} < "
              f"{args.coverage_threshold:.2f}. "
              f"Prompt bundle written to {bundle_path}", file=sys.stderr)
        print("   Next: skill runs intent extraction, then capability-writer.py "
              "persists the response.", file=sys.stderr)
        # Still emit the deterministic result on stdout for inspection
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return EXIT_LLM_NEEDED

    if args.format == "yaml":
        sys.stdout.write(_to_yaml(result))
    else:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")

    return 0


def _to_yaml(data: dict) -> str:
    lines: list[str] = []
    if "project" in data:
        lines.append(f"project: {data['project']}")
    lines.append(f"registry_version: {data['registry_version']}")
    lines.append(f"coverage: {data['coverage']}")
    lines.append(f"needs_llm_augmentation: "
                 f"{'true' if data['needs_llm_augmentation'] else 'false'}")
    lines.append("")
    lines.append("intent_matches:")
    for im in data["intent_matches"]:
        lines.append(f"  - intent_id: {im['intent_id']}")
        lines.append(f"    confidence: {im['confidence']}")
        lines.append(f"    weighted_score: {im['weighted_score']}")
        lines.append(f"    matched_phrases: {json.dumps(im['matched_phrases'])}")
    lines.append("")
    lines.append("required_capabilities:")
    for cap in data["required_capabilities"]:
        lines.append(f"  - capability_id: {cap['capability_id']}")
        lines.append(f"    satisfied_by_items: "
                     f"{json.dumps(cap['satisfied_by_items'])}")
        lines.append(f"    triggered_by_intents: "
                     f"{json.dumps(cap['triggered_by_intents'])}")
    lines.append("")
    lines.append(f"required_items: {json.dumps(data['required_items'])}")
    if data["gaps"]:
        lines.append("")
        lines.append("gaps:")
        for g in data["gaps"]:
            lines.append(f"  - type: {g['type']}")
            lines.append(f"    intent_id: {g['intent_id']}")
            if "matched_phrases" in g:
                lines.append(f"    matched_phrases: "
                             f"{json.dumps(g['matched_phrases'])}")
            if "negated_phrases" in g:
                lines.append(f"    negated_phrases: "
                             f"{json.dumps(g['negated_phrases'])}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
