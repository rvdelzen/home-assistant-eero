"""Serve the bundled IP table card from the integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_REGISTERED: set[int] = set()
_URL_PATH = "/eero/ip-table-card.js"


def _card_version() -> str:
    """Return the integration version so a card upgrade is not cached."""
    manifest = Path(__file__).parent / "manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return "0"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the card once per Home Assistant instance."""
    if id(hass) in _REGISTERED:
        return
    http = getattr(hass, "http", None)
    if http is None or not hasattr(http, "async_register_static_paths"):
        return
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
        add_extra_js_url(hass, f"{_URL_PATH}?v={_card_version()}")
    except Exception:  # noqa: BLE001 - a missing frontend must not block setup
        _LOGGER.debug("Could not register the eero IP table card", exc_info=True)
        return
    _REGISTERED.add(id(hass))
