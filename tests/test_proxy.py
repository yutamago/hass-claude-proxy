"""Tests for proxy-specific behaviour (not part of the upstream sync)."""

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

from anthropic import AuthenticationError
from anthropic._models import construct_type
from anthropic.pagination import AsyncPage
from anthropic.types import ModelInfo
from freezegun import freeze_time
from httpx2 import Request, Response
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.claude_proxy import proxy
from custom_components.claude_proxy.const import DOMAIN
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

# Shape of CLIProxyAPI's `GET /v1/models` answer (v8.0.73): no lifecycle,
# no capabilities, and the `lifecycle` query parameter is ignored.
PROXY_MODELS: list[dict[str, Any]] = [
    {
        "created_at": "2024-10-22T00:00:00Z",
        "display_name": "Claude 3.5 Haiku",
        "id": "claude-3-5-haiku-20241022",
        "max_input_tokens": 128000,
        "max_tokens": 8192,
        "object": "model",
        "owned_by": "anthropic",
        "type": "model",
    },
    {
        "created_at": "2025-09-29T00:00:00Z",
        "display_name": "Claude 4.5 Sonnet",
        "id": "claude-sonnet-4-5-20250929",
        "max_input_tokens": 200000,
        "max_tokens": 64000,
        "object": "model",
        "owned_by": "anthropic",
        "type": "model",
    },
    {
        "created_at": "2026-09-22T00:00:00Z",
        "display_name": "Claude Opus 5.5",
        "id": "claude-opus-5-5",
        "max_input_tokens": 1000000,
        "max_tokens": 128000,
        "object": "model",
        "owned_by": "anthropic",
        "type": "model",
    },
    {
        "created_at": "2026-09-28T00:00:00Z",
        "display_name": "Claude Haiku 5.5",
        "id": "claude-haiku-5-5",
        "max_input_tokens": 1000000,
        "max_tokens": 128000,
        "object": "model",
        "owned_by": "anthropic",
        "type": "model",
    },
    {
        "created_at": "2026-11-09T19:00:00Z",
        "display_name": "Claude Fable 5.2",
        "id": "claude-fable-5-2",
        "max_input_tokens": 1000000,
        "max_tokens": 128000,
        "object": "model",
        "owned_by": "anthropic",
        "type": "model",
    },
    {
        "created_at": "2026-01-01T00:00:00Z",
        "display_name": "Some Other Model",
        "id": "other-provider-model",
        "object": "model",
        "type": "model",
    },
]


def proxy_model(raw: dict[str, Any]) -> ModelInfo:
    """Parse a raw model the way the SDK does (lenient, missing fields are None)."""
    return construct_type(type_=ModelInfo, value=raw)


@pytest.fixture
def mock_proxy_model_list() -> Generator[AsyncMock]:
    """Mock a proxy that returns every model regardless of the lifecycle filter."""
    with patch(
        "anthropic.resources.models.AsyncModels.list",
        new_callable=AsyncMock,
        side_effect=lambda **kwargs: AsyncPage(
            data=[proxy_model(raw) for raw in PROXY_MODELS], has_more=False
        ),
    ) as mock_list:
        yield mock_list


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://proxy.test:8317", "http://proxy.test:8317"),
        ("http://proxy.test:8317/", "http://proxy.test:8317"),
        ("http://proxy.test:8317/v1", "http://proxy.test:8317"),
        (" https://proxy.test/claude/v1/ ", "https://proxy.test/claude"),
        ("https://proxy.test/v1beta", "https://proxy.test/v1beta"),
    ],
)
def test_normalize_base_url(url: str, expected: str) -> None:
    """Test base URL normalization."""
    assert proxy.normalize_base_url(url) == expected


@freeze_time("2026-11-25")
@pytest.mark.parametrize(
    ("model_id", "lifecycle", "line", "has_capabilities"),
    [
        ("claude-opus-5-5", "active", "opus", True),
        ("claude-fable-5-2", "active", "fable", True),
        ("claude-sonnet-4-5-20250929", "deprecated", "sonnet", True),
        ("claude-3-5-haiku-20241022", "retired", None, False),
        ("anthropic/claude-haiku-5-5", "active", "haiku", True),
        ("other-provider-model", "active", None, False),
    ],
)
def test_normalize_model(
    model_id: str, lifecycle: str, line: str | None, has_capabilities: bool
) -> None:
    """Test filling in lifecycle and capabilities."""
    model = proxy.normalize_model(
        proxy_model({"id": model_id, "type": "model", "display_name": model_id})
    )
    assert model.lifecycle == lifecycle
    assert model.line == line
    assert (model.capabilities is not None) is has_capabilities


def test_normalize_model_keeps_api_data() -> None:
    """Test that data from a real Models API response is left alone."""
    model = proxy_model(
        {
            "id": "claude-opus-5-5",
            "type": "model",
            "display_name": "Claude Opus 5.5",
            "lifecycle": "deprecated",
            "line": "custom",
        }
    )
    normalized = proxy.normalize_model(model)
    assert normalized.lifecycle == "deprecated"
    assert normalized.line == "custom"
    assert normalized.capabilities is not None


def test_capabilities_match_documented_thinking() -> None:
    """Spot-check the generated table against documented model behaviour."""
    opus = proxy.normalize_model(proxy_model({"id": "claude-opus-5-5"}))
    assert opus.capabilities is not None
    thinking = opus.capabilities.thinking.types
    assert thinking.adaptive.supported
    assert not thinking.disabled.supported
    assert not thinking.enabled.supported
    sonnet = proxy.normalize_model(proxy_model({"id": "claude-sonnet-5-5"}))
    assert sonnet.capabilities is not None
    assert sonnet.capabilities.thinking.types.between_tools.supported


@freeze_time("2026-11-25")
async def test_setup_with_proxy_catalogue(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_proxy_model_list: AsyncMock,
) -> None:
    """Test the catalogue from a proxy that ignores the lifecycle filter."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    coordinator = mock_config_entry.runtime_data

    ids = [model.id for model in coordinator.data]
    assert sorted(ids) == sorted(raw["id"] for raw in PROXY_MODELS)
    assert len(ids) == len(set(ids))

    model, found = coordinator.get_model_info("claude-opus-5-5")
    assert found
    assert model.lifecycle == "active"
    assert model.capabilities is not None
    assert model.capabilities.structured_outputs.supported

    model, found = coordinator.get_model_info("claude-3-5-haiku")
    assert found
    assert model.lifecycle == "retired"

    assert str(coordinator.client.base_url) == "http://proxy.test:8317"


@freeze_time("2026-11-25")
async def test_ai_task_model_picker_with_proxy(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_proxy_model_list: AsyncMock,
) -> None:
    """Test the AI task picker lists known models and hides retired ones."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry.entry_id, "ai_task_data"),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    options = {
        option["value"]
        for option in result["data_schema"].schema["chat_model"].config["options"]
    }
    assert "claude-opus-5-5" in options
    assert "claude-fable-5-2" in options
    assert "claude-3-5-haiku" not in options
    # Unknown models have no capabilities, so structured output is not assumed.
    assert "other-provider-model" not in options


@pytest.mark.usefixtures("mock_setup_entry")
async def test_form_normalizes_url(hass: HomeAssistant) -> None:
    """Test the stored URL is normalized."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch("anthropic.resources.models.AsyncModels.list", new_callable=AsyncMock):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: "http://proxy.test:8317/v1/", CONF_API_KEY: "bla"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: "http://proxy.test:8317", CONF_API_KEY: "bla"}


@pytest.mark.usefixtures("mock_setup_entry")
async def test_form_invalid_url(hass: HomeAssistant) -> None:
    """Test a URL without scheme is rejected before any request."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "anthropic.resources.models.AsyncModels.list", new_callable=AsyncMock
    ) as mock_list:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "proxy.test:8317", CONF_API_KEY: "bla"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_URL: "invalid_url"}
    mock_list.assert_not_called()


@pytest.mark.usefixtures("mock_setup_entry")
async def test_reconfigure_changes_url(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test reconfigure suggests the current URL and stores a new one."""
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    url_key = next(key for key in result["data_schema"].schema if key == CONF_URL)
    assert url_key.description == {"suggested_value": "http://proxy.test:8317"}
    api_key = next(key for key in result["data_schema"].schema if key == CONF_API_KEY)
    assert not (api_key.description or {}).get("suggested_value")

    with patch("anthropic.resources.models.AsyncModels.list", new_callable=AsyncMock):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: "https://other.test/v1", CONF_API_KEY: "bla"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_URL] == "https://other.test"


@pytest.mark.usefixtures("mock_setup_entry")
async def test_form_proxy_auth_error(hass: HomeAssistant) -> None:
    """Test a 401 with CLIProxyAPI's plain error body maps to an auth error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    error = AuthenticationError(
        message="Invalid API key",
        response=Response(401, request=Request("GET", "http://proxy.test:8317")),
        body={"error": "Invalid API key"},
    )
    with patch("anthropic.resources.models.AsyncModels.list", side_effect=error):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_URL: "http://proxy.test:8317", CONF_API_KEY: "x"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "authentication_error"}
