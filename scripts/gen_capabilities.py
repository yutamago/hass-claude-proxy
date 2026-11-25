#!/usr/bin/env python3
"""Generate custom_components/claude_proxy/model_capabilities.json.

Proxies usually return a bare model list without `capabilities`. The upstream
test fixtures (tests/__init__.py, synced from home-assistant/core) carry the
capability objects the official Models API returns, so we reuse them as the
fallback table. Models newer than the fixtures are added through EXTRA_ALIASES,
which copies the capabilities of a model with the same API surface.

Run after every upstream sync: .venv/bin/python scripts/gen_capabilities.py
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "__init__.py"
OUT = REPO / "custom_components" / "claude_proxy" / "model_capabilities.json"

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


def main() -> None:
    """Write the table."""
    spec = importlib.util.spec_from_file_location("upstream_fixtures", FIXTURES)
    assert spec and spec.loader
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)

    table: dict[str, dict] = {}
    for model in fixtures.model_list:
        if model.capabilities is None:
            continue
        table[model_alias(model.id)] = {
            "line": model.line,
            "capabilities": model.capabilities.model_dump(mode="json"),
        }
    for alias, source in EXTRA_ALIASES.items():
        if alias not in table:
            table[alias] = table[source]

    OUT.write_text(json.dumps(dict(sorted(table.items())), indent=2) + "\n")
    print(f"Wrote {len(table)} models to {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
