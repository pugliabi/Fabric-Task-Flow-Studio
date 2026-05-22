#!/usr/bin/env python3
"""
Fabric Task Flows — Validation Checklist Generator

Parses a deployment-handoff.md and generates a manual configuration checklist.
Trusts that deployment succeeded (fabric-cicd is deterministic) — no redundant
REST API existence checks.

Usage:
    python validate-items.py <deployment-handoff.md>
    python validate-items.py <deployment-handoff.md> > validation-checklist.yaml

No external dependencies required.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Load registries
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "_shared" / "lib"))
from paths import REPO_ROOT
from registry_loader import load_registry
from yaml_utils import parse_yaml, parse_yaml_scalar

REGISTRY_DIR = REPO_ROOT / "_shared" / "registry"


def _load_validation_checklists() -> dict:
    """Load task-flow-specific validation checklists."""
    path = REGISTRY_DIR / "validation-checklists.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _get_manual_steps(task_flow: str, checklists: dict) -> list[dict]:
    """Get manual steps for a task flow from the checklist."""
    tf_data = checklists.get("task_flows", {}).get(task_flow, {})
    return tf_data.get("manual_steps", [])


# ---------------------------------------------------------------------------
# Handoff parser
# ---------------------------------------------------------------------------

def _parse_handoff(path: str) -> tuple[str, str, str, list[dict]]:
    """Parse deployment-handoff.md → (project, task_flow, workspace, items).

    Delegates to the shared YAML parser instead of the previous hand-rolled
    state machine. The old parser silently dropped items whose keys appeared
    in unexpected order or whose values contained colons — both common with
    LLM-edited handoffs.
    """
    text = Path(path).read_text(encoding="utf-8")

    parsed = parse_yaml(text)

    # Scalars: tolerate both the shared parser output and legacy YAML
    # blocks that put metadata under the top-level mapping. Fall back to
    # the scoped helpers if the shared parser didn't surface the field.
    def _scalar(name: str) -> str:
        val = parsed.get(name) if isinstance(parsed.get(name), str) else None
        if val:
            return val
        return parse_yaml_scalar(text, name) or ""

    project = _scalar("project")
    task_flow = _scalar("task_flow")
    workspace = _scalar("workspace")

    # Items: accept either ``items`` or ``items_deployed`` as the key.
    raw_items = parsed.get("items") or parsed.get("items_deployed") or []
    if not isinstance(raw_items, list):
        raw_items = []

    items: list[dict] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("item_name") or ""
        item_type = entry.get("type") or entry.get("item_type") or ""
        wave = entry.get("wave", "")
        status = entry.get("status", "deployed")
        items.append({
            "name": str(name),
            "type": str(item_type),
            "wave": str(wave) if wave != "" else "",
            "status": str(status),
        })

    return project, task_flow, workspace, items


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

from banner import print_banner


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate post-deployment configuration checklist (no API calls)"
    )
    parser.add_argument("handoff", help="Path to deployment-handoff.md")
    args = parser.parse_args()

    if not Path(args.handoff).exists():
        print(f"Error: File not found: {args.handoff}", file=sys.stderr)
        sys.exit(2)

    # Load registries
    checklists = _load_validation_checklists()

    # Parse handoff
    project, task_flow, workspace, items = _parse_handoff(args.handoff)

    if not items:
        print("Error: No items found in the deployment handoff.", file=sys.stderr)
        sys.exit(2)

    # Show banner
    print_banner(project=project, task_flow=task_flow, mode="Config Checklist")
    print(f"  Handoff:   {args.handoff}", file=sys.stderr)
    print(f"  Workspace: {workspace}", file=sys.stderr)
    print(f"  Items:     {len(items)}", file=sys.stderr)
    print("  Method:    Trust deploy, check config only", file=sys.stderr)
    print("", file=sys.stderr)

    # Get manual steps for this task flow
    manual_steps = _get_manual_steps(task_flow, checklists)
    
    # Match deployed items to manual steps
    config_tasks: list[dict] = []
    deployed_types = {item["type"] for item in items}
    
    for step in manual_steps:
        item_type = step.get("item_type", "")
        action = step.get("action", "")
        # Check if this item type was deployed
        if any(t in deployed_types or item_type.lower() in t.lower() 
               for t in deployed_types):
            # Find the matching deployed item(s)
            for item in items:
                if item_type.lower() in item["type"].lower() or item["type"].lower() in item_type.lower():
                    config_tasks.append({
                        "item_name": item["name"],
                        "item_type": item["type"],
                        "action": action,
                        "confirmed": False
                    })

    # Summary
    print("", file=sys.stderr)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", file=sys.stderr)
    print(f"  {len(items)} items deployed, {len(config_tasks)} manual config steps", file=sys.stderr)
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", file=sys.stderr)
    print("", file=sys.stderr)

    # Output YAML to stdout
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("# Validation Report")
    print(f"# Generated: {today}")
    print("# Deployment trusted (fabric-cicd deterministic) — config checks only")
    print()
    print(f'project: "{project}"')
    print(f'task_flow: "{task_flow}"')
    print(f'workspace: "{workspace}"')
    print(f'date: "{today}"')
    print(f"status: pending  # pending | passed | failed")
    print()
    print(f"items_deployed: {len(items)}")
    print()
    print("config_checklist:")

    if config_tasks:
        for i, task in enumerate(config_tasks, 1):
            print(f"  - id: CFG-{i:02d}")
            print(f'    item: "{task["item_name"]}"')
            print(f'    type: "{task["item_type"]}"')
            print(f'    action: "{task["action"]}"')
            print(f"    confirmed: false")
    else:
        print("  []  # No manual config steps — all items are fully automated")

    print()
    print("smoke_tests:")
    print("  - test: Query returns data")
    print("    passed: false")
    print("  - test: Pipeline executes successfully")
    print("    passed: false")
    print("  - test: Report renders correctly")
    print("    passed: false")
    print()
    print("next_steps:")
    if config_tasks:
        print(f'  - "Complete {len(config_tasks)} manual configuration step(s)"')
    print('  - "Run smoke tests to verify data flow"')
    print('  - "Mark status as passed when all checks complete"')

    sys.exit(0)


if __name__ == "__main__":
    main()
