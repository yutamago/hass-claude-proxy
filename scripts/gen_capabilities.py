#!/usr/bin/env python3
"""Generate custom_components/claude_proxy/model_capabilities.json.

Proxies usually return a bare model list without `capabilities`. The table is
built from three sources, in this order of precedence:

1. The upstream test fixtures (tests/__init__.py, synced from home-assistant/core).
   They carry the capability objects the official Models API returns.
2. Claude Code's model list (scripts/claude_code_models.json, written by
   scripts/claude-code/probe.mjs). For a model the fixtures don't have yet, the
   capabilities of the newest fixture model in the same line are copied, then the
   effort levels and adaptive thinking flag Claude Code reports are applied.
3. EXTRA_ALIASES, for models neither source lists yet.

Run after every upstream sync and after refreshing the Claude Code snapshot:
  .venv/bin/python scripts/gen_capabilities.py
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "__init__.py"
CLAUDE_CODE = REPO / "scripts" / "claude_code_models.json"
OUT = REPO / "custom_components" / "claude_proxy" / "model_capabilities.json"
TABLE_FORMAT = 1  # keep in sync with proxy.TABLE_FORMAT
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODEL_LINE = re.compile(r"^claude-([a-z]+)-")

# alias -> alias whose capabilities it shares
EXTRA_ALIASES = {
    # Same request surface as Fable 5.1 (adaptive thinking only, all effort levels).
    "claude-fable-5-2": "claude-fable-5-1",
    "claude-mythos-5-2": "claude-fable-5-1",
    "claude-mythos-5-1": "claude-fable-5-1",
    "claude-mythos-5": "claude-fable-5",
}


def model_alias(model_id: str) -> str:
    """Drop a trailing -YYYYMMDD snapshot date (same rule as the coordinator)."""
    model, _, version = model_id.rpartition("-")
    if len(version) == 8 and version.isdecimal():
        return model
    return model_id


def apply_claude_code(capabilities: dict, reported: dict) -> dict:
    """Overlay what Claude Code reports onto a copy of a capability object."""
    capabilities = copy.deepcopy(capabilities)
    levels = set(reported.get("effort") or [])
    effort = capabilities["effort"]
    effort["supported"] = bool(levels)
    for level in EFFORT_LEVELS:
        effort[level] = {"supported": level in levels}
    if "adaptive" in reported:
        capabilities["thinking"]["types"]["adaptive"]["supported"] = bool(
            reported["adaptive"]
        )
    return capabilities


def reported_effort(capabilities: dict) -> list[str]:
    """Return the effort levels a capability object allows."""
    effort = capabilities["effort"]
    if not effort.get("supported"):
        return []
    return [lvl for lvl in EFFORT_LEVELS if (effort.get(lvl) or {}).get("supported")]


def main() -> int:
    """Write the table."""
    spec = importlib.util.spec_from_file_location("upstream_fixtures", FIXTURES)
    assert spec and spec.loader
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)

    table: dict[str, dict] = {}
    newest_in_line: dict[str, str] = {}
    for model in fixtures.model_list:  # newest first within each line
        if model.capabilities is None:
            continue
        alias = model_alias(model.id)
        table[alias] = {
            "line": model.line,
            "source": "home-assistant",
            "capabilities": model.capabilities.model_dump(mode="json"),
        }
        if model.line:
            newest_in_line.setdefault(model.line, alias)

    if CLAUDE_CODE.exists():
        snapshot = json.loads(CLAUDE_CODE.read_text())
        for reported in snapshot["models"]:
            alias = model_alias(reported["id"])
            if alias in table:
                if reported_effort(table[alias]["capabilities"]) != list(
                    reported.get("effort") or []
                ):
                    print(
                        f"warning: Claude Code reports effort {reported.get('effort')}"
                        f" for {alias}, fixtures say"
                        f" {reported_effort(table[alias]['capabilities'])}",
                        file=sys.stderr,
                    )
                continue
            match = MODEL_LINE.match(alias)
            template = newest_in_line.get(match[1]) if match else None
            if template is None:
                print(f"warning: no template for {alias}, skipped", file=sys.stderr)
                continue
            table[alias] = {
                "line": table[template]["line"],
                "source": f"claude-code (template {template})",
                "capabilities": apply_claude_code(
                    table[template]["capabilities"], reported
                ),
            }

    for alias, source in EXTRA_ALIASES.items():
        if alias not in table:
            table[alias] = {**table[source], "source": f"alias of {source}"}

    OUT.write_text(
        json.dumps(
            {"version": TABLE_FORMAT, "models": dict(sorted(table.items()))}, indent=2
        )
        + "\n"
    )
    print(f"Wrote {len(table)} models to {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
