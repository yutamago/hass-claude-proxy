"""Adapt model metadata from Anthropic-compatible proxies.

Proxies such as CLIProxyAPI answer `GET /v1/models` in Anthropic's format but
without `lifecycle` or `capabilities`, and ignore the `lifecycle` filter. The
rest of this integration (synced from Home Assistant core) relies on both, so
this module fills them in before the coordinator stores the catalogue.
"""

from __future__ import annotations

from collections.abc import Iterable
import datetime as dt
from functools import cache
import json
from pathlib import Path
from typing import Any, Literal

from anthropic.resources.messages.messages import DEPRECATED_MODELS
from anthropic.types import ModelCapabilities, ModelInfo

from homeassistant.util import dt as dt_util

type Lifecycle = Literal["active", "deprecated", "retired"]

CAPABILITIES_FILE = Path(__file__).with_name("model_capabilities.json")


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


@cache
def _known_models() -> dict[str, dict[str, Any]]:
    """Load the bundled capability table (blocking, cached after first call)."""
    return json.loads(CAPABILITIES_FILE.read_text(encoding="utf-8"))


def preload() -> None:
    """Read the capability table; call from an executor before first use."""
    _known_models()


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


def normalize_model(model: ModelInfo, now: dt.datetime | None = None) -> ModelInfo:
    """Fill in lifecycle and capabilities a proxy left out."""
    update: dict[str, Any] = {}
    if model.lifecycle is None:
        update["lifecycle"] = _lifecycle_from_sdk(model.id, now or dt_util.utcnow())
    if model.capabilities is None and (
        known := _known_models().get(_bare_model_id(model.id))
    ):
        update["capabilities"] = ModelCapabilities.model_validate(known["capabilities"])
        if model.line is None:
            update["line"] = known["line"]
    if not update:
        return model
    return model.model_copy(update=update)


def filter_models(
    models: Iterable[ModelInfo], lifecycle: Iterable[Lifecycle]
) -> list[ModelInfo]:
    """Normalize models and apply the lifecycle filter the proxy ignored."""
    wanted = set(lifecycle)
    now = dt_util.utcnow()
    return [
        model
        for model in (normalize_model(model, now) for model in models)
        if model.lifecycle in wanted
    ]
