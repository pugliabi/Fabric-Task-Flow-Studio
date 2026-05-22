"""
Item Type Registry loader.

All scripts that need item type metadata MUST import from here instead of
maintaining their own dictionaries.  The single source of truth is
``registry/item-type-registry.json``.

Usage::

    from registry_loader import load_registry, build_fab_type_map, build_phase_map
"""

from __future__ import annotations

import json
from typing import Any, Callable

from paths import REPO_ROOT, REGISTRY_DIR

REGISTRY_PATH = REGISTRY_DIR / "item-type-registry.json"

_cache: dict | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Core loaders
# ─────────────────────────────────────────────────────────────────────────────

def load_registry() -> dict[str, dict]:
    """Load the item type registry.  Returns the ``types`` dict.

    Applies ``$defaults`` (availability) and reconstructs derived
    fields (``fab_type`` defaults to key name, ``rest_api`` from flat booleans,
    auto-generated lowercase alias) so downstream code sees the full shape.

    The result is cached after the first successful call so repeated imports
    across scripts share a single in-memory copy.

    The cached value is a *deep copy* derived from the on-disk JSON — the
    cache is never mutated in place. Callers receive the cached dict and
    must treat it as read-only; mutations will leak to other callers but
    won't corrupt the validation invariants the loader enforces.

    Raises:
        FileNotFoundError: Registry JSON file does not exist.
        ValueError: JSON is malformed or missing the ``types`` key.
    """
    import copy

    global _cache
    if _cache is None:
        data = _read_registry_file()
        if "types" not in data or not isinstance(data["types"], dict):
            raise ValueError(
                f"Registry at {REGISTRY_PATH} is missing a valid 'types' dict"
            )
        defaults = data.get("$defaults", {})
        phase_legend = data.get("$phase", {})
        phase_keys = list(phase_legend.keys())
        # Work on a deep copy so the on-disk JSON shape is preserved for
        # validate_registry() and any other consumer that needs to inspect
        # the raw declaration (e.g. to detect missing fields).
        types = copy.deepcopy(data["types"])
        for name, item in types.items():
            # Apply availability default
            if "availability" not in item and "availability" in defaults:
                item["availability"] = defaults["availability"]
            # Default fab_type to key name
            if "fab_type" not in item:
                # Check nested api.fab_type first (v1.0.0 schema)
                if "api" in item and "fab_type" in item["api"]:
                    item["fab_type"] = item["api"]["fab_type"]
                else:
                    item["fab_type"] = name
            # Auto-generate lowercase alias
            auto_alias = name.lower()
            if "aliases" not in item:
                item["aliases"] = [auto_alias]
            elif auto_alias not in item["aliases"]:
                item["aliases"].insert(0, auto_alias)
            # Reconstruct rest_api from api object (v1.0.0 schema) or flat booleans (legacy)
            if "rest_api" not in item:
                if "api" in item:
                    api = item["api"]
                    item["rest_api"] = {
                        "creatable": api.get("creatable", False),
                        "definition": api.get("definition", False),
                    }
                    if "name" in api:
                        item["rest_api"]["api_name"] = api["name"]
                    # Preserve api_path at top level for backward compat
                    if "api_path" not in item:
                        item["api_path"] = api.get("path", "items")
                elif "api_creatable" in item:
                    item["rest_api"] = {
                        "creatable": item.pop("api_creatable", False),
                        "definition": item.pop("api_definition", False),
                    }
                    if "api_name" in item:
                        item["rest_api"]["api_name"] = item.pop("api_name")
            # Derive phase_order from $phase legend position (v1.0.0)
            if "phase_order" not in item:
                phase_name = item.get("phase", "")
                if phase_name in phase_keys:
                    item["phase_order"] = phase_keys.index(phase_name) + 1
                else:
                    item["phase_order"] = 99
        _cache = types
    return _cache


def _read_registry_file() -> dict:
    """Read and parse the registry JSON with clear error messages."""
    if not REGISTRY_PATH.exists():
        raise FileNotFoundError(
            f"Item type registry not found at {REGISTRY_PATH}. "
            f"Expected repo root: {REPO_ROOT}"
        )
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {REGISTRY_PATH}: {exc}"
        ) from exc


# ─────────────────────────────────────────────────────────────────────────────
# Generic builder factory — eliminates copy-paste across 7+ builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_variant_map(
    value_fn: Callable[[str, dict], Any],
    *,
    include_capitalized_aliases: bool = False,
) -> dict:
    """Build a dict mapping type-name variants → derived value.

    For each registry entry the map includes keys for the canonical name,
    ``fab_type``, ``display_name``, and every alias.  When
    *include_capitalized_aliases* is ``True``, multi-word aliases also get
    a ``" ".join(w.capitalize())`` form for fuzzy handoff matching.

    *value_fn(canonical, data)* returns the value to store, or ``None`` to
    skip the entry entirely.
    """
    registry = load_registry()
    result: dict = {}

    for canonical, data in registry.items():
        value = value_fn(canonical, data)
        if value is None:
            continue

        result[canonical] = value
        result[data.get("fab_type", canonical)] = value
        result[data.get("display_name", canonical)] = value

        for alias in data.get("aliases", []):
            result[alias] = value
            if include_capitalized_aliases:
                parts = alias.split()
                if len(parts) > 1:
                    result[" ".join(w.capitalize() for w in parts)] = value

    return result


def build_display_names() -> dict[str, str]:
    """Map lowercase type variants → display name."""
    registry = load_registry()
    result: dict[str, str] = {}
    for canonical, data in registry.items():
        display = data.get("display_name", canonical)
        result[canonical.lower()] = display
        for alias in data.get("aliases", []):
            result[alias.lower()] = display
    return result


def build_phase_map() -> dict[str, tuple[str, int]]:
    """Map type-name variants → ``(phase_name, phase_order)``.

    Entries with ``phase == "TBD"`` are skipped.
    """
    def _value(canonical: str, data: dict) -> tuple[str, int] | None:
        phase = data.get("phase", "")
        if not phase or phase == "TBD":
            return None
        return (phase, data.get("phase_order", 99))

    return _build_variant_map(_value, include_capitalized_aliases=True)


def build_task_type_map() -> dict[str, str]:
    """Map type-name variants → Fabric task type string.

    Entries with empty or ``"TBD"`` task_type are skipped.
    """
    def _value(_c: str, data: dict) -> str | None:
        tt = data.get("task_type", "")
        return tt if tt and tt != "TBD" else None

    return _build_variant_map(_value, include_capitalized_aliases=True)


def build_fab_type_map() -> dict[str, str]:
    """Map display-name variants → ``fab_type``.

    Includes canonical key, ``display_name``, ``fab_type`` (identity), and
    every alias plus title-cased forms so any LLM-generated string resolves
    to the correct ``fab_type``.
    """
    registry = load_registry()
    result: dict[str, str] = {}
    for canonical, data in registry.items():
        fab_type = data.get("fab_type", canonical)
        result[canonical] = fab_type
        result[fab_type] = fab_type
        result[data.get("display_name", canonical)] = fab_type
        for alias in data.get("aliases", []):
            result[alias] = fab_type
            result[alias.title()] = fab_type
            parts = alias.split()
            if len(parts) > 1:
                result[" ".join(w.capitalize() for w in parts)] = fab_type
    return result


def build_cicd_type_set() -> set[str]:
    """Return ``fab_type`` values deployable via fabric-cicd.

    Derived from ``cicd.strategy`` — types with ``platform_only`` or
    ``content`` strategy are supported.
    """
    registry = load_registry()
    return {
        data.get("fab_type", name)
        for name, data in registry.items()
        if data.get("cicd", {}).get("strategy") in ("platform_only", "content")
    }


def build_type_remap() -> dict[str, str]:
    """Map ``display_name`` / alias → ``fab_type`` where they differ.

    Only entries where the display-name or title-cased alias is not equal
    to ``fab_type`` are included.
    """
    registry = load_registry()
    result: dict[str, str] = {}
    for _canonical, data in registry.items():
        fab_type = data.get("fab_type", _canonical)
        display = data.get("display_name", _canonical)
        if display != fab_type:
            result[display] = fab_type
        for alias in data.get("aliases", []):
            titled = alias.title()
            if titled != fab_type:
                result[titled] = fab_type
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Review & test plan pre-computation builders
# ─────────────────────────────────────────────────────────────────────────────

def build_deploy_method_map() -> dict[str, dict]:
    """Map type-name variants → deployment metadata dict.

    Each value contains ``method``, ``strategy``, ``verified``,
    ``creatable``, ``has_definition``, and ``availability`` keys.
    """
    def _value(_c: str, data: dict) -> dict:
        creatable = data.get("rest_api", {}).get("creatable", False)
        has_def = data.get("rest_api", {}).get("definition", False)
        cicd = data.get("cicd", {})
        strategy = cicd.get("strategy")
        verified = cicd.get("verified", False)
        availability = data.get("availability", "ga")

        if strategy in ("content", "platform_only"):
            method = "cicd"
        elif creatable:
            method = "rest_api"
        else:
            method = "portal"

        return {
            "method": method,
            "strategy": strategy,
            "verified": verified,
            "creatable": creatable,
            "has_definition": has_def,
            "availability": availability,
        }

    return _build_variant_map(_value)


def build_test_method_map() -> dict[str, dict]:
    """Map type-name variants → test method metadata dict.

    Each value contains ``verify_method``, ``definition_check``,
    ``manual_fallback``, ``api_path``, ``supports_definition``,
    ``is_portal_only``, ``phase``, and ``phase_order``.
    """
    def _value(_c: str, data: dict) -> dict:
        creatable = data.get("rest_api", {}).get("creatable", False)
        has_def = data.get("rest_api", {}).get("definition", False)
        api_path = data.get("api_path", "items")
        phase = data.get("phase", "Other")
        phase_order = data.get("phase_order", 99)

        if creatable and has_def:
            verify = f"REST API GET /{api_path} | verify {{item}} exists"
            def_check = f"REST API GET /{api_path}/{{item}} | check definition"
        elif creatable:
            verify = f"REST API GET /{api_path} | verify {{item}} exists"
            def_check = None
        else:
            verify = "Verify {item} exists in Fabric portal"
            def_check = None

        return {
            "verify_method": verify,
            "definition_check": def_check,
            "manual_fallback": "Verify {item} exists in Fabric portal",
            "api_path": api_path,
            "supports_definition": has_def,
            "is_portal_only": not creatable,
            "phase": phase,
            "phase_order": phase_order,
        }

    return _build_variant_map(_value)


_PHASE_TO_LAYER: dict[str, tuple[str, str]] = {
    "Foundation":      ("Store",     "🗄️"),
    "Ingestion":       ("Ingest",    "📥"),
    "Transformation":  ("Process",   "⚙️"),
    "Visualization":   ("Visualize", "📊"),
    "ML":              ("AI / ML",   "🤖"),
    "IQ":              ("AI / ML",   "🤖"),
    "Monitoring":      ("Alert",     "🔔"),
    "Environment":     ("Config",    "🔧"),
}

_LAYER_DEFAULT: tuple[str, str] = ("Other", "📦")


def build_layer_map() -> dict[str, tuple[str, str]]:
    """Map type-name variants → ``(layer_label, emoji)``.

    Derived from each registry entry's ``phase`` field.  Entries with
    unknown or TBD phases map to ``("Other", "📦")``.
    """
    def _value(_c: str, data: dict) -> tuple[str, str]:
        phase = data.get("phase", "")
        return _PHASE_TO_LAYER.get(phase, _LAYER_DEFAULT)

    return _build_variant_map(_value)


_PHASE_TO_DECISION: dict[str, str] = {
    "Foundation":     "storage",
    "Ingestion":      "ingestion",
    "Transformation": "processing",
    "Visualization":  "visualization",
}


def build_type_to_decision_map() -> dict[str, str]:
    """Map type-name variants (lowercased) → decision category string.

    Only entries whose ``phase`` maps to a known decision category are
    included.
    """
    def _value(_c: str, data: dict) -> str | None:
        phase = data.get("phase", "")
        return _PHASE_TO_DECISION.get(phase)

    raw = _build_variant_map(_value, include_capitalized_aliases=True)
    return {k.lower(): v for k, v in raw.items()}


def build_alternatives_map() -> dict[str, list[str]]:
    """Map canonical type name → list of alternative item type names.

    Reads the ``alternatives`` field from each item type in the registry.
    Returns only items that have alternatives defined.
    """
    registry = load_registry()
    result: dict[str, list[str]] = {}
    for canonical, data in registry.items():
        alts = data.get("alternatives")
        if alts and isinstance(alts, list):
            result[canonical] = alts
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Registry validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_registry() -> list[str]:
    """Check the registry for structural issues.

    Returns a list of error messages (empty if valid).  Useful for CI
    checks and maintenance scripts.
    """
    errors: list[str] = []
    registry = load_registry()

    _REQUIRED_FIELDS = {"fab_type", "display_name", "phase", "task_type", "aliases"}
    _VALID_PHASES = {
        "Environment", "Foundation", "IQ", "Ingestion",
        "ML", "Monitoring", "Transformation", "Visualization", "TBD",
    }
    _VALID_TASK_TYPES = {
        "get data", "mirror data", "store data", "prepare data",
        "analyze and train data", "track data", "visualize",
        "distribute data", "develop data", "general", "TBD", "",
    }

    for name, data in registry.items():
        # Guard against type confusion: a corrupted registry entry could be
        # a string, list, or null rather than a dict. Report and skip so
        # downstream checks don't crash with AttributeError.
        if not isinstance(data, dict):
            errors.append(
                f"{name}: expected object, got {type(data).__name__}"
            )
            continue

        # Check required fields
        missing = _REQUIRED_FIELDS - set(data.keys())
        if missing:
            errors.append(f"{name}: missing fields {sorted(missing)}")

        # Check phase validity
        phase = data.get("phase", "")
        if phase and phase not in _VALID_PHASES:
            errors.append(f"{name}: invalid phase '{phase}'")

        # Check task_type validity
        task_type = data.get("task_type", "")
        if task_type and task_type not in _VALID_TASK_TYPES:
            errors.append(f"{name}: invalid task_type '{task_type}'")

        # Check aliases are lowercase
        aliases = data.get("aliases", [])
        if not isinstance(aliases, list):
            errors.append(
                f"{name}: aliases must be a list, got {type(aliases).__name__}"
            )
        else:
            for alias in aliases:
                if not isinstance(alias, str):
                    errors.append(
                        f"{name}: alias entries must be strings, got {type(alias).__name__}"
                    )
                    continue
                if alias != alias.lower():
                    errors.append(f"{name}: alias '{alias}' is not lowercase")

        # Check fab_type is present and non-empty
        if not data.get("fab_type"):
            errors.append(f"{name}: empty or missing fab_type")

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Stop words loader
# ─────────────────────────────────────────────────────────────────────────────

_stop_words_cache: frozenset[str] | None = None


def load_stop_words() -> frozenset[str]:
    """Load English stop words from registry/stop-words.json.

    Stop words are excluded from keyword coverage calculations so metrics
    reflect actual tech-content coverage, not natural-language filler.

    Result is cached after first call.
    """
    global _stop_words_cache
    if _stop_words_cache is None:
        path = REGISTRY_DIR / "stop-words.json"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _stop_words_cache = frozenset(data.get("words", []))
    return _stop_words_cache


# ─────────────────────────────────────────────────────────────────────────────
# Capability registry loader
# ─────────────────────────────────────────────────────────────────────────────

_capability_registry_cache: dict | None = None
_CAPABILITY_REGISTRY_PATH = REGISTRY_DIR / "capability-registry.json"


def load_capability_registry() -> dict:
    """Load the semantic capability registry from registry/capability-registry.json.

    The registry has three sections:
      - intents[]       — semantic patterns extracted from problem text
      - capabilities[]  — named abilities required to satisfy intents
      - (capabilities reference Fabric item types from item-type-registry.json)

    Validates on load:
      - every intent's `requires_capabilities` references a real capability id
      - every capability's `satisfied_by_items` references a real Fabric item
        type key in item-type-registry.json

    Returns the full registry dict (cached after first successful load).

    Raises:
        FileNotFoundError: Registry file missing.
        ValueError: Schema invalid or cross-references broken.
    """
    global _capability_registry_cache
    if _capability_registry_cache is not None:
        return _capability_registry_cache

    if not _CAPABILITY_REGISTRY_PATH.exists():
        raise FileNotFoundError(
            f"Capability registry not found at {_CAPABILITY_REGISTRY_PATH}"
        )

    with open(_CAPABILITY_REGISTRY_PATH, encoding="utf-8") as f:
        data = json.load(f)

    errors = _validate_capability_registry(data)
    if errors:
        raise ValueError(
            "Capability registry validation failed:\n  - "
            + "\n  - ".join(errors)
        )

    _capability_registry_cache = data
    return _capability_registry_cache


def _validate_capability_registry(data: dict) -> list[str]:
    """Return a list of error messages; empty list = valid."""
    errors: list[str] = []

    for key in ("intents", "capabilities"):
        if key not in data or not isinstance(data[key], list):
            errors.append(f"Missing or non-list section '{key}'")
            return errors  # cannot continue without these

    intent_ids = [i.get("id") for i in data["intents"]]
    if len(intent_ids) != len(set(intent_ids)):
        dups = [i for i in intent_ids if intent_ids.count(i) > 1]
        errors.append(f"Duplicate intent ids: {sorted(set(dups))}")

    capability_ids = [c.get("id") for c in data["capabilities"]]
    if len(capability_ids) != len(set(capability_ids)):
        dups = [c for c in capability_ids if capability_ids.count(c) > 1]
        errors.append(f"Duplicate capability ids: {sorted(set(dups))}")

    capability_id_set = set(capability_ids)
    for intent in data["intents"]:
        for cap_ref in intent.get("requires_capabilities", []):
            if cap_ref not in capability_id_set:
                errors.append(
                    f"Intent '{intent.get('id')}' requires unknown capability "
                    f"'{cap_ref}'"
                )

    try:
        item_registry = load_registry()
        item_keys = set(item_registry.keys())
    except Exception as exc:  # pragma: no cover — defensive
        errors.append(f"Could not load item-type-registry for cross-check: {exc}")
        return errors

    for cap in data["capabilities"]:
        for item_ref in cap.get("satisfied_by_items", []):
            if item_ref not in item_keys:
                errors.append(
                    f"Capability '{cap.get('id')}' references unknown Fabric "
                    f"item type '{item_ref}' (not in item-type-registry.json)"
                )
        for item_ref in cap.get("supporting_items", []):
            if item_ref not in item_keys:
                errors.append(
                    f"Capability '{cap.get('id')}' supporting_items references "
                    f"unknown Fabric item type '{item_ref}'"
                )

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Deployment order loader
# ─────────────────────────────────────────────────────────────────────────────

_deployment_order_cache: dict | None = None
_DEPLOYMENT_ORDER_PATH = REGISTRY_DIR / "deployment-order.json"


def _compute_waves(items: list[dict]) -> list[dict]:
    """Compute deployment wave order from dependsOn via topological sort.

    Items with no dependencies get wave 1. Each subsequent item gets
    max(dependency waves) + 1. Items within the same wave get letter
    suffixes (a, b, c...) for parallel deployment.
    """
    from collections import defaultdict, deque

    item_names = {item["itemType"] for item in items}
    # Build dep graph (only consider deps within this flow)
    deps_map: dict[str, set[str]] = {}
    for item in items:
        itype = item["itemType"]
        deps_map[itype] = {d for d in item.get("dependsOn", []) if d in item_names}

    # Compute wave via BFS (longest path from roots)
    waves: dict[str, int] = {}
    in_degree = {k: len(v) for k, v in deps_map.items()}
    queue = deque()
    for name, degree in in_degree.items():
        if degree == 0:
            waves[name] = 1
            queue.append(name)

    dependents: dict[str, set[str]] = defaultdict(set)
    for name, item_deps in deps_map.items():
        for d in item_deps:
            dependents[d].add(name)

    while queue:
        current = queue.popleft()
        for dependent in dependents[current]:
            dep_waves = [waves.get(d, 0) for d in deps_map[dependent]]
            if all(w > 0 for w in dep_waves):
                waves[dependent] = max(dep_waves) + 1
                queue.append(dependent)

    # Assign order strings: wave number + letter suffix for parallel items
    wave_groups: dict[int, list[str]] = defaultdict(list)
    for name, wave in sorted(waves.items(), key=lambda x: x[1]):
        wave_groups[wave].append(name)

    order_map: dict[str, str] = {}
    for wave, names in sorted(wave_groups.items()):
        if len(names) == 1:
            order_map[names[0]] = str(wave)
        else:
            for i, name in enumerate(names):
                order_map[name] = f"{wave}{chr(97 + i)}"  # 1a, 1b, 1c...

    # Inject computed order into items. Items still missing from order_map
    # are unreachable from any root — i.e. there is a dependency cycle. Raise
    # rather than silently emitting order="0" because a silent fallback
    # produces an invalid deployment plan downstream.
    unresolved = [item["itemType"] for item in items if item["itemType"] not in order_map]
    if unresolved:
        raise ValueError(
            f"deployment-order: cannot compute waves for {unresolved!r} — "
            f"likely a circular dependsOn cycle or unresolved external dependency."
        )
    result = []
    for item in items:
        enriched = dict(item)
        enriched["order"] = order_map[item["itemType"]]
        result.append(enriched)
    return result


def get_deployment_items(task_flow: str) -> list[dict]:
    """Get deployment items for a task flow from the deployment-order registry.

    The ``order`` field is computed at runtime via topological sort of
    ``dependsOn`` edges — it is not stored in the JSON file.

    Args:
        task_flow: Task flow ID (e.g., 'medallion', 'lambda').
                   Case-insensitive — normalized to lowercase internally.

    Returns:
        List of deployment items with order, itemType, dependsOn, etc.
    """
    global _deployment_order_cache
    if _deployment_order_cache is None:
        if not _DEPLOYMENT_ORDER_PATH.exists():
            _deployment_order_cache = {}
        else:
            data = json.loads(_DEPLOYMENT_ORDER_PATH.read_text(encoding="utf-8"))
            _deployment_order_cache = data.get("taskFlows", {})
    flow_data = _deployment_order_cache.get(task_flow.lower(), {})
    items = flow_data.get("items", [])
    if not items:
        return []
    return _compute_waves(items)
