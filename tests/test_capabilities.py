"""Tests for the downloadable capability table (not part of the upstream sync)."""

import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from anthropic._models import construct_type
from anthropic.pagination import AsyncPage
from anthropic.types import ModelInfo
from freezegun.api import FrozenDateTimeFactory
import httpx2
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.claude_proxy import proxy
from custom_components.claude_proxy.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

NEW_MODEL = "claude-opus-9"


def bundled() -> dict[str, Any]:
    """Return the bundled table as published."""
    return json.loads(proxy.CAPABILITIES_FILE.read_text())


def published(**extra_models: dict[str, Any]) -> dict[str, Any]:
    """Return the bundled table plus extra models, as the repository would serve it."""
    data = bundled()
    data["models"].update(extra_models)
    return data


def new_model_entry(effort: list[str]) -> dict[str, Any]:
    """Return an entry for a model the bundled table doesn't know."""
    entry = copy.deepcopy(bundled()["models"]["claude-opus-5-5"])
    for level in ("low", "medium", "high", "xhigh", "max"):
        entry["capabilities"]["effort"][level] = {"supported": level in effort}
    return entry


def model(model_id: str) -> ModelInfo:
    """Parse a bare proxy model."""
    return construct_type(type_=ModelInfo, value={"id": model_id, "type": "model"})


def efforts(table: proxy.CapabilityTable, model_id: str) -> list[str]:
    """Return the effort levels the table gives a model."""
    known = table.get(model_id)
    assert known is not None
    effort = known.capabilities.effort
    return [
        level
        for level in ("low", "medium", "high", "xhigh", "max")
        if (support := getattr(effort, level)) and support.supported
    ]


async def test_bundled_table_only(
    hass: HomeAssistant, mock_capability_download: AsyncMock
) -> None:
    """Test that a failed download leaves the bundled table in use."""
    table = await proxy.async_get_table(hass)
    await table.async_refresh()

    mock_capability_download.assert_awaited_once()
    assert table.get("claude-opus-5-5") is not None
    assert table.get(NEW_MODEL) is None


async def test_download_adds_models_and_is_stored(
    hass: HomeAssistant,
    mock_capability_download: AsyncMock,
    hass_storage: dict[str, Any],
) -> None:
    """Test a downloaded table adds models and is saved for the next start."""
    mock_capability_download.side_effect = None
    mock_capability_download.return_value = published(
        **{NEW_MODEL: new_model_entry(["low", "high"])}
    )
    table = await proxy.async_get_table(hass)
    await table.async_refresh()

    assert efforts(table, NEW_MODEL) == ["low", "high"]
    assert efforts(table, f"{NEW_MODEL}-20270101") == ["low", "high"]
    normalized = proxy.normalize_model(model(NEW_MODEL), table)
    assert normalized.capabilities is not None
    assert normalized.line == "opus"

    stored = hass_storage[proxy.STORAGE_KEY]["data"]
    assert NEW_MODEL in stored["table"]["models"]
    assert dt.datetime.fromisoformat(stored["fetched_at"])


async def test_stored_table_used_offline(
    hass: HomeAssistant,
    mock_capability_download: AsyncMock,
    hass_storage: dict[str, Any],
) -> None:
    """Test the stored copy is used after a restart without network."""
    hass_storage[proxy.STORAGE_KEY] = {
        "version": proxy.STORAGE_VERSION,
        "key": proxy.STORAGE_KEY,
        "data": {
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "table": published(**{NEW_MODEL: new_model_entry(["medium"])}),
        },
    }
    table = await proxy.async_get_table(hass)
    await table.async_refresh()  # stale, so it tries and fails

    mock_capability_download.assert_awaited_once()
    assert efforts(table, NEW_MODEL) == ["medium"]


async def test_downloaded_entry_overrides_bundled(
    hass: HomeAssistant, mock_capability_download: AsyncMock
) -> None:
    """Test a corrected entry in the published table wins over the bundled one."""
    mock_capability_download.side_effect = None
    mock_capability_download.return_value = published(
        **{"claude-opus-5-5": new_model_entry(["low"])}
    )
    table = await proxy.async_get_table(hass)
    assert efforts(table, "claude-opus-5-5") == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    await table.async_refresh()
    assert efforts(table, "claude-opus-5-5") == ["low"]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"version": 2, "models": {}}, id="newer_format"),
        pytest.param({"version": 1}, id="no_models"),
        pytest.param(["not", "a", "table"], id="not_an_object"),
        pytest.param(ValueError("bad json"), id="invalid_json"),
        pytest.param(
            httpx2.HTTPStatusError(
                "404",
                request=httpx2.Request("GET", proxy.REMOTE_URL),
                response=httpx2.Response(404),
            ),
            id="http_error",
        ),
    ],
)
async def test_unusable_download_ignored(
    hass: HomeAssistant,
    mock_capability_download: AsyncMock,
    hass_storage: dict[str, Any],
    payload: Any,
) -> None:
    """Test downloads this release can't read leave the current table alone."""
    if isinstance(payload, Exception):
        mock_capability_download.side_effect = payload
    else:
        mock_capability_download.side_effect = None
        mock_capability_download.return_value = payload
    table = await proxy.async_get_table(hass)
    await table.async_refresh()

    assert table.get("claude-opus-5-5") is not None
    assert proxy.STORAGE_KEY not in hass_storage


async def test_bad_entry_skipped(
    hass: HomeAssistant, mock_capability_download: AsyncMock
) -> None:
    """Test one invalid entry doesn't discard the rest of the download."""
    mock_capability_download.side_effect = None
    mock_capability_download.return_value = published(
        **{
            NEW_MODEL: new_model_entry(["high"]),
            "claude-broken": {"capabilities": {"effort": "nope"}},
            "claude-no-caps": {"line": "opus"},
        }
    )
    table = await proxy.async_get_table(hass)
    await table.async_refresh()

    assert efforts(table, NEW_MODEL) == ["high"]
    assert table.get("claude-broken") is None
    assert table.get("claude-no-caps") is None


@pytest.mark.freeze_time("2026-11-25T12:00:00Z")
async def test_refresh_intervals(
    hass: HomeAssistant,
    mock_capability_download: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Test downloads happen at most daily, and retry hourly after a failure."""
    table = await proxy.async_get_table(hass)

    await table.async_refresh()  # fails
    freezer.tick(dt.timedelta(minutes=30))
    await table.async_refresh()  # too soon to retry
    assert mock_capability_download.await_count == 1

    mock_capability_download.side_effect = None
    mock_capability_download.return_value = published()
    freezer.tick(dt.timedelta(minutes=31))
    await table.async_refresh()  # retry succeeds
    assert mock_capability_download.await_count == 2

    freezer.tick(dt.timedelta(hours=23))
    await table.async_refresh()  # still fresh
    assert mock_capability_download.await_count == 2

    freezer.tick(dt.timedelta(hours=1, minutes=1))
    await table.async_refresh()
    assert mock_capability_download.await_count == 3


async def test_shared_between_entries(hass: HomeAssistant) -> None:
    """Test every config entry uses the same table, loaded once."""
    first = await proxy.async_get_table(hass)
    second = await proxy.async_get_table(hass)
    assert first is second


def test_published_file_is_readable() -> None:
    """Test the bundled file is in the format this release reads."""
    models = proxy.parse_table(bundled())
    assert "claude-opus-5-5" in models
    assert all(entry.capabilities is not None for entry in models.values())


async def test_coordinator_uses_downloaded_table(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_capability_download: AsyncMock,
) -> None:
    """Test a model only the downloaded table knows gets capabilities on setup."""
    mock_capability_download.side_effect = None
    mock_capability_download.return_value = published(
        **{NEW_MODEL: new_model_entry(["low", "max"])}
    )
    with patch(
        "anthropic.resources.models.AsyncModels.list",
        new_callable=AsyncMock,
        return_value=AsyncPage(
            data=[model(NEW_MODEL), model("claude-opus-5-5")], has_more=False
        ),
    ):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    info, found = mock_config_entry.runtime_data.get_model_info(NEW_MODEL)
    assert found
    assert info.lifecycle == "active"
    assert info.capabilities is not None
    assert info.capabilities.effort.max.supported
    assert not info.capabilities.effort.medium.supported


def test_generator_applies_claude_code() -> None:
    """Test the generator overlays Claude Code's effort levels on a template."""
    spec = importlib.util.spec_from_file_location(
        "gen_capabilities", Path(__file__).parent.parent / "scripts/gen_capabilities.py"
    )
    assert spec and spec.loader
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    template = bundled()["models"]["claude-opus-5-5"]["capabilities"]
    result = gen.apply_claude_code(template, {"effort": ["high"], "adaptive": False})
    assert gen.reported_effort(result) == ["high"]
    assert not result["thinking"]["types"]["adaptive"]["supported"]
    assert gen.reported_effort(template) == ["low", "medium", "high", "xhigh", "max"]
    assert gen.reported_effort(gen.apply_claude_code(template, {"effort": []})) == []
