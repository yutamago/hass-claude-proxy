#!/usr/bin/env python3
"""End-to-end smoke test against a real Anthropic-compatible proxy.

Boots an in-memory Home Assistant, runs the config flow, then sends a
conversation turn that must call an Assist tool and an AI task that must
return structured data. Each step uses a small Claude request.

Usage:
  CLAUDE_PROXY_URL=http://host:8317 CLAUDE_PROXY_KEY_FILE=~/.config/claude_proxy/key \\
      .venv/bin/python scripts/live_smoke.py [model] [effort]
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import probatio  # noqa: E402
from pytest_homeassistant.common import async_test_home_assistant  # noqa: E402

from homeassistant import config_entries, loader  # noqa: E402
from homeassistant.components import ai_task, conversation  # noqa: E402
from homeassistant.components.homeassistant.exposed_entities import (  # noqa: E402
    async_expose_entity,
)
from homeassistant.const import CONF_API_KEY, CONF_URL  # noqa: E402
from homeassistant.core import Context  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

DOMAIN = "claude_proxy"
LIGHT = "light.kitchen_lights"


def check(ok: bool, message: str) -> None:
    """Print a result line and stop on failure."""
    print(("PASS " if ok else "FAIL ") + message)
    if not ok:
        raise SystemExit(1)


async def main(model: str | None, effort: str | None) -> None:
    """Run the smoke test."""
    url = os.environ["CLAUDE_PROXY_URL"]
    key = Path(os.environ["CLAUDE_PROXY_KEY_FILE"]).expanduser().read_text().strip()

    with tempfile.TemporaryDirectory() as config_dir:
        (Path(config_dir) / "custom_components").symlink_to(REPO / "custom_components")
        async with async_test_home_assistant(config_dir=config_dir) as hass:
            hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
            hass.config.country = "DE"
            assert await async_setup_component(hass, "homeassistant", {})
            assert await async_setup_component(
                hass, "light", {"light": {"platform": "demo"}}
            )
            await hass.async_block_till_done()
            await hass.services.async_call(
                "light", "turn_off", {"entity_id": LIGHT}, blocking=True
            )
            async_expose_entity(hass, conversation.DOMAIN, LIGHT, True)

            # Config flow, including the URL normalization.
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_URL: url + "/v1/", CONF_API_KEY: key}
            )
            check(result["type"] == "create_entry", f"config flow: {result['type']}")
            await hass.async_block_till_done()
            entry = hass.config_entries.async_entries(DOMAIN)[0]
            check(entry.state is config_entries.ConfigEntryState.LOADED, "entry loaded")

            coordinator = entry.runtime_data
            active = [m.id for m in coordinator.data if m.lifecycle == "active"]
            with_caps = [m.id for m in coordinator.data if m.capabilities]
            check(bool(active), f"{len(active)} active models: {', '.join(active)}")
            check(bool(with_caps), f"{len(with_caps)} models with capabilities")

            if model:
                for subentry in entry.subentries.values():
                    hass.config_entries.async_update_subentry(
                        entry,
                        subentry,
                        data={
                            **subentry.data,
                            "recommended": False,
                            "chat_model": model,
                            "max_tokens": 0,  # automatic
                            **({"effort": effort} if effort else {}),
                        },
                    )
                await hass.config_entries.async_reload(entry.entry_id)
                await hass.async_block_till_done()

            registry = er.async_get(hass)
            agent_id = next(
                e.entity_id
                for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                if e.domain == "conversation"
            )
            task_entity = next(
                e.entity_id
                for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                if e.domain == "ai_task"
            )

            # Conversation with an Assist tool call.
            response = await conversation.async_converse(
                hass, "Turn on the kitchen lights.", None, Context(), agent_id=agent_id
            )
            speech = response.response.speech.get("plain", {}).get("speech", "")
            check(
                response.response.response_type.value != "error",
                f"conversation reply: {speech!r}",
            )
            check(
                hass.states.get(LIGHT).state == "on",
                f"tool call turned on {LIGHT}",
            )

            # Structured AI task.
            data = await ai_task.async_generate_data(
                hass,
                task_name="smoke",
                entity_id=task_entity,
                instructions="Name one primary colour and how many letters it has.",
                structure=probatio.Schema(
                    {
                        probatio.Required("colour"): str,
                        probatio.Required("letters"): int,
                    }
                ),
            )
            check(
                isinstance(data.data, dict)
                and isinstance(data.data.get("letters"), int),
                f"ai_task structured data: {data.data}",
            )
            await hass.config_entries.async_unload(entry.entry_id)


if __name__ == "__main__":
    args = sys.argv[1:] + [None, None]
    asyncio.run(main(args[0], args[1]))
