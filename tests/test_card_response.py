"""The IP table card must request service responses, and registry lookups stay current."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from custom_components.eero.const import (
    DOMAIN,
    SERVICE_IP_TABLE,
    SERVICE_RELEASE_IP,
    SERVICE_RESERVE_IP,
    SERVICE_SYNC_NAMES,
)
from custom_components.eero.ip_table import (
    device_config_entry_ids,
    iter_device_entries,
    lookup_device_by_identifier,
    registry_mac_records,
)
from custom_components.eero.services import async_register_services

CARD = Path("custom_components/eero/frontend/eero-ip-table-card.js")
CARD_SERVICES = (
    SERVICE_IP_TABLE,
    SERVICE_RESERVE_IP,
    SERVICE_RELEASE_IP,
    SERVICE_SYNC_NAMES,
)
_CALL_SERVICE = re.compile(
    r"callService\(\s*\"eero\",\s*name,\s*payload,\s*undefined,\s*false,\s*true\s*\)",
    re.MULTILINE,
)


def test_card_version_is_not_read_on_the_event_loop() -> None:
    """The manifest is loaded by the integration loader, with a thread fallback."""
    source = Path("custom_components/eero/frontend.py").read_text(encoding="utf-8")
    assert "async_get_integration" in source
    assert "async_add_executor_job" in source
    register = source.split("async def async_register_frontend", 1)[1]
    assert "read_text" not in register


def test_card_requests_a_response_for_every_service_call() -> None:
    """ip_table is response-only. A call without return_response is rejected."""
    source = CARD.read_text(encoding="utf-8")
    assert "return_response: true" in source
    assert "payload, {}, true" not in source
    assert _CALL_SERVICE.search(source)
    assert source.count("callWS(") == 1
    assert source.count("callService(") == 1
    for name in CARD_SERVICES:
        assert f'"{name}"' in source


async def test_ip_table_is_response_only_and_the_others_can_return(hass) -> None:
    """Register the services and reject an ip_table call that asks for no response."""
    async_register_services(hass)
    assert (
        hass.services.supports_response(DOMAIN, SERVICE_IP_TABLE)
        is SupportsResponse.ONLY
    )
    for name in (SERVICE_RESERVE_IP, SERVICE_RELEASE_IP, SERVICE_SYNC_NAMES):
        assert hass.services.supports_response(DOMAIN, name) in (
            SupportsResponse.ONLY,
            SupportsResponse.OPTIONAL,
        )
    with pytest.raises(ServiceValidationError, match="service_lacks_response_request"):
        await hass.services.async_call(DOMAIN, SERVICE_IP_TABLE, {}, blocking=True)


def test_config_entry_id_is_used_without_reading_config_entries() -> None:
    """A 2026.8 device exposes one config entry id. The old set must not be read."""

    class Device:
        id = "dev-1"
        name = "Plug"
        name_by_user = None
        connections = [("mac", "00:11:22:33:44:55")]
        config_entry_id = "shelly-1"

        @property
        def config_entries(self):
            raise AssertionError("config_entries is deprecated")

    records = registry_mac_records([Device()], lambda entry_id: "shelly")
    assert records[0]["ha_integrations"] == ["shelly"]
    assert device_config_entry_ids(Device()) == ["shelly-1"]


def test_old_registry_mapping_and_scoped_identifier_lookup() -> None:
    """Older cores iterate a mapping and look devices up with async_get_device."""

    class Entry:
        def __init__(self, entry_id):
            self.id = entry_id

    class Registry:
        def __init__(self):
            self.devices = {"dev-1": Entry("dev-1")}
            self.scoped = None
            self.legacy = None

        def async_get_device(self, identifiers=None, connections=None):
            self.legacy = identifiers
            return self.devices["dev-1"]

    registry = Registry()
    assert [device.id for device in iter_device_entries(registry)] == ["dev-1"]
    found = lookup_device_by_identifier(registry, ("eero", "net-1"), "entry-1")
    assert found.id == "dev-1"
    assert registry.legacy == {("eero", "net-1")}

    class Scoped(Registry):
        def async_get_device_by_identifier(self, identifier, config_entry_id):
            self.scoped = (identifier, config_entry_id)
            return self.devices["dev-1"]

    scoped = Scoped()
    lookup_device_by_identifier(scoped, ("eero", "net-1"), "entry-1")
    assert scoped.scoped == (("eero", "net-1"), "entry-1")
    assert scoped.legacy is None

    class View:
        def __iter__(self):
            yield Entry("dev-2")

        def values(self):
            raise AssertionError("devices.values() is deprecated")

    class NewRegistry:
        devices = View()

    assert [device.id for device in iter_device_entries(NewRegistry())] == ["dev-2"]
