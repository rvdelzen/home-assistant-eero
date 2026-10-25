"""Serve the bundled IP table card from the integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_REGISTERED: set[int] = set()
_URL_PATH = "/eero/ip-table-card.js"
_MANIFEST = Path(__file__).parent / "manifest.json"


def _version_from_manifest() -> str:
    """Read the version from disk. Call this from an executor, not the loop."""
    try:
        return str(json.loads(_MANIFEST.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return "0"


async def _card_version(hass: HomeAssistant) -> str:
    """Return the integration version so a card upgrade is not cached."""
    try:
        integration = await async_get_integration(hass, DOMAIN)
    except Exception:  # noqa: BLE001 - a missing integration still serves the card
        integration = None
    version = getattr(integration, "version", None) if integration is not None else None
    if version:
        return str(version)
    return await hass.async_add_executor_job(_version_from_manifest)


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the card once per Home Assistant instance."""
    if id(hass) in _REGISTERED:
        return
    http = getattr(hass, "http", None)
    if http is None or not hasattr(http, "async_register_static_paths"):
        return
    version = await _card_version(hass)
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig
    except ImportError:
        _LOGGER.debug("Frontend static paths are not available")
        return
    path = Path(__file__).parent / "frontend" / "eero-ip-table-card.js"
    try:
        await http.async_register_static_paths(
            [StaticPathConfig(_URL_PATH, str(path), False)]
        )
        add_extra_js_url(hass, f"{_URL_PATH}?v={version}")
    except Exception:  # noqa: BLE001 - a missing frontend must not block setup
        _LOGGER.debug("Could not register the eero IP table card", exc_info=True)
        return
    _REGISTERED.add(id(hass))
