"""Adapt model metadata from Anthropic-compatible proxies.

Proxies such as CLIProxyAPI answer `GET /v1/models` in Anthropic's format but
without `lifecycle` or `capabilities`, and ignore the `lifecycle` filter. The
rest of this integration (synced from Home Assistant core) relies on both, so
this module fills them in before the coordinator stores the catalogue.

Capabilities come from model_capabilities.json. The bundled copy is the
fallback; a newer copy is downloaded from the repository about once a day so
new models get their options without a release.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Literal

from anthropic.resources.messages.messages import DEPRECATED_MODELS
from anthropic.types import ModelCapabilities, ModelInfo
import httpx2

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .const import (
    CONF_EFFORT,
    CONF_MAX_TOKENS,
    CONF_THINKING_BUDGET,
    CONF_THINKING_TYPE,
)

_LOGGER = logging.getLogger(__name__)

type Lifecycle = Literal["active", "deprecated", "retired"]

CAPABILITIES_FILE = Path(__file__).with_name("model_capabilities.json")
REMOTE_URL = (
    "https://raw.githubusercontent.com/yutamago/hass-claude-proxy/main/"
    "custom_components/claude_proxy/model_capabilities.json"
)
# Bump when the file layout changes, so older releases ignore files they can't read.
TABLE_FORMAT = 1
REFRESH_INTERVAL = dt.timedelta(hours=24)
RETRY_INTERVAL = dt.timedelta(hours=1)
STORAGE_KEY = "claude_proxy.model_capabilities"
STORAGE_VERSION = 1
DATA_TABLE: HassKey[CapabilityTable] = HassKey("claude_proxy_capabilities")


# Max tokens of 0 means "choose from the model and effort". The limit only stops
# runaway answers; Claude isn't told about it and only the tokens it uses count, so
# it is generous. Claude Code always sends the model's maximum.
AUTO_MAX_TOKENS = 0
EFFORT_MAX_TOKENS: dict[str, int | None] = {
    "low": 8_000,
    "medium": 16_000,
    "high": 32_000,
    "xhigh": 64_000,
    "max": None,  # the model's maximum
}
NO_EFFORT_MAX_TOKENS = 16_000
UNKNOWN_MODEL_MAX_TOKENS = 64_000
THINKING_BUDGET_HEADROOM = 4_000


def resolve_max_tokens(options: Mapping[str, Any], model_info: ModelInfo) -> int:
    """Return the max_tokens to send, filling in the automatic value."""
    if value := options.get(CONF_MAX_TOKENS):
        return int(value)
    model_max = model_info.max_tokens or UNKNOWN_MODEL_MAX_TOKENS
    effort = options.get(CONF_EFFORT)
    supports_effort = bool(
        model_info.capabilities and model_info.capabilities.effort.supported
    )
    if supports_effort and effort in EFFORT_MAX_TOKENS:
        wanted = EFFORT_MAX_TOKENS[effort] or model_max
    else:
        wanted = NO_EFFORT_MAX_TOKENS
    if options.get(CONF_THINKING_TYPE) == "enabled":
        # A fixed thinking budget must fit below max_tokens, with room for the answer.
        wanted = max(
            wanted,
            int(options.get(CONF_THINKING_BUDGET) or 0) + THINKING_BUDGET_HEADROOM,
        )
    return min(wanted, model_max)


def normalize_base_url(url: str) -> str:
    """Return the base URL the SDK expects (without trailing `/v1`)."""
    url = url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def _bare_model_id(model_id: str) -> str:
    """Drop a routing prefix (`provider/model`) and a snapshot date suffix."""
    model_id = model_id.rpartition("/")[2]
    model, _, version = model_id.rpartition("-")
    if len(version) == 8 and version.isdecimal():
        return model
    return model_id


class KnownModel:
    """Capabilities and model line for one model alias."""

    __slots__ = ("capabilities", "line")

    def __init__(self, capabilities: ModelCapabilities, line: str | None) -> None:
        """Initialize."""
        self.capabilities = capabilities
        self.line = line


def parse_table(data: Any) -> dict[str, KnownModel]:
    """Validate a capability file and return its models.

    Raises ValueError when the file is not in a format this release reads.
    Entries that don't validate are skipped, so one bad entry can't hide the rest.
    """
    if not isinstance(data, dict) or data.get("version") != TABLE_FORMAT:
        raise ValueError("unsupported capability table format")
    models = data.get("models")
    if not isinstance(models, dict):
        raise ValueError("capability table has no models")
    result: dict[str, KnownModel] = {}
    for alias, entry in models.items():
        try:
            line = entry.get("line")
            if line is not None and not isinstance(line, str):
                raise ValueError("line must be a string")
            result[alias] = KnownModel(
                ModelCapabilities.model_validate(entry["capabilities"]), line
            )
        except (AttributeError, KeyError, TypeError, ValueError) as err:
            _LOGGER.debug("Skipping capability entry %s: %s", alias, err)
    return result


class CapabilityTable:
    """Bundled capability table, overlaid with the latest downloaded copy."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._loaded = False
        self._bundled: dict[str, KnownModel] = {}
        self._remote: dict[str, KnownModel] = {}
        self._fetched_at: dt.datetime | None = None
        self._attempted_at: dt.datetime | None = None

    def get(self, model_id: str) -> KnownModel | None:
        """Return what is known about a model, preferring the downloaded table."""
        alias = _bare_model_id(model_id)
        return self._remote.get(alias) or self._bundled.get(alias)

    async def async_load(self) -> None:
        """Read the bundled table and the last downloaded copy."""
        async with self._lock:
            if self._loaded:
                return
            raw = await self.hass.async_add_executor_job(
                CAPABILITIES_FILE.read_text, "utf-8"
            )
            self._bundled = parse_table(json.loads(raw))
            stored = await self._store.async_load()
            if stored:
                try:
                    self._remote = parse_table(stored.get("table"))
                    self._fetched_at = dt_util.parse_datetime(stored["fetched_at"])
                except (KeyError, TypeError, ValueError) as err:
                    _LOGGER.debug("Ignoring stored capability table: %s", err)
            self._loaded = True

    async def async_refresh(self) -> None:
        """Download the latest table if the current one is a day old.

        Failures are logged and leave the current table in place.
        """
        now = dt_util.utcnow()
        if self._fetched_at and now - self._fetched_at < REFRESH_INTERVAL:
            return
        if self._attempted_at and now - self._attempted_at < RETRY_INTERVAL:
            return
        self._attempted_at = now
        try:
            data = await self._async_fetch()
            models = parse_table(data)
        except (httpx2.HTTPError, ValueError) as err:
            _LOGGER.debug("Could not refresh model capabilities: %s", err)
            return
        self._remote = models
        self._fetched_at = now
        await self._store.async_save({"fetched_at": now.isoformat(), "table": data})
        _LOGGER.debug("Refreshed capabilities for %d models", len(models))

    async def _async_fetch(self) -> Any:
        """Download the published table and return the decoded JSON."""
        response = await get_async_client(self.hass).get(REMOTE_URL, timeout=10)
        response.raise_for_status()
        return response.json()


async def async_get_table(hass: HomeAssistant) -> CapabilityTable:
    """Return the shared capability table, loading it on first use."""
    if (table := hass.data.get(DATA_TABLE)) is None:
        table = hass.data[DATA_TABLE] = CapabilityTable(hass)
    await table.async_load()
    return table


def _lifecycle_from_sdk(model_id: str, now: dt.datetime) -> Lifecycle:
    """Derive a lifecycle from the SDK's deprecation list."""
    # Imported here to avoid a circular import with the coordinator module.
    from .coordinator import _parse_sdk_retirement_date  # noqa: PLC0415

    retirement = DEPRECATED_MODELS.get(model_id) or DEPRECATED_MODELS.get(
        _bare_model_id(model_id)
    )
    if retirement is None:
        return "active"
    retires_at = _parse_sdk_retirement_date(retirement)
    if retires_at is not None and retires_at <= now:
        return "retired"
    return "deprecated"


def normalize_model(
    model: ModelInfo, table: CapabilityTable, now: dt.datetime | None = None
) -> ModelInfo:
    """Fill in lifecycle and capabilities a proxy left out."""
    update: dict[str, Any] = {}
    if model.lifecycle is None:
        update["lifecycle"] = _lifecycle_from_sdk(model.id, now or dt_util.utcnow())
    if model.capabilities is None and (known := table.get(model.id)):
        update["capabilities"] = known.capabilities
        if model.line is None:
            update["line"] = known.line
    if not update:
        return model
    return model.model_copy(update=update)


def filter_models(
    models: Iterable[ModelInfo],
    lifecycle: Iterable[Lifecycle],
    table: CapabilityTable,
) -> list[ModelInfo]:
    """Normalize models and apply the lifecycle filter the proxy ignored."""
    wanted = set(lifecycle)
    now = dt_util.utcnow()
    return [
        model
        for model in (normalize_model(model, table, now) for model in models)
        if model.lifecycle in wanted
    ]
