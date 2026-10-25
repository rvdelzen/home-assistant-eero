"""IP table, reserve, and release services."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import voluptuous as vol

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eero.const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DRY_RUN,
    ATTR_FORCE,
    ATTR_IP,
    ATTR_MAC,
    ATTR_REFRESH,
    ATTR_SUGGEST,
    CONF_NETWORKS,
    DATA_API,
    DATA_COORDINATOR,
    DOMAIN,
    SERVICE_IP_TABLE,
    SERVICE_RELEASE_IP,
    SERVICE_RESERVE_IP,
)
from custom_components.eero.sensor import EeroIpReservationSensor
from custom_components.eero.services import (
    IP_TABLE_SCHEMA,
    RELEASE_IP_SCHEMA,
    RESERVE_IP_SCHEMA,
    async_register_services,
)
from tests.test_services import FakeAPI

MAC = "00:11:22:33:44:55"
PRIVATE = "02:11:22:33:44:55"


class InventoryAPI(FakeAPI):
    """Fake API that also serves the device list used by the IP table."""

    def __init__(self) -> None:
        super().__init__()
        self.devices: list[dict] = []

    def call(self, method, url, **kwargs):
        if url.rstrip("/").endswith("/devices"):
            self.calls.append({"method": method, "url": url, **kwargs})
            if method != "GET":
                raise AssertionError(url)
            return [dict(item) for item in self.devices]
        return super().call(method, url, **kwargs)


def _install(hass, *entry_ids: str):
    hass.data.setdefault(DOMAIN, {})
    clients = []
    networks = []
    entries = []
    for index, entry_id in enumerate(entry_ids, start=1):
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id=entry_id,
            unique_id=f"log-{index}",
            title=f"Home {index}",
            data={
                "name": f"Home {index}",
                "login": "user@example.com",
                "user_token": "token",
                CONF_NETWORKS: [f"net-{index}"],
            },
        )
        entry.add_to_hass(hass)
        client = InventoryAPI()
        network = SimpleNamespace(
            id=f"net-{index}",
            name="Ohana" if index == 1 else "Guest",
            name_unique="Ohana" if index == 1 else "Guest",
            data={
                "gateway_ip": "192.168.4.1",
                "eeros": {"data": [{"gateway": True, "ip_address": "192.168.4.1"}]},
            },
        )
        hass.data[DOMAIN][entry.entry_id] = {
            DATA_API: client,
            DATA_COORDINATOR: SimpleNamespace(data=SimpleNamespace(networks=[network])),
        }
        clients.append(client)
        networks.append(network)
        entries.append(entry)
    async_register_services(hass)
    return entries, clients, networks


async def _call(hass, service, data):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


def test_ip_service_schemas_reject_bad_input() -> None:
    """Boolean and address fields reject values the services cannot use."""
    with pytest.raises(vol.Invalid):
        IP_TABLE_SCHEMA({ATTR_REFRESH: "maybe"})
    with pytest.raises(vol.Invalid):
        RESERVE_IP_SCHEMA({ATTR_MAC: MAC, ATTR_FORCE: "maybe"})
    with pytest.raises(vol.Invalid):
        RELEASE_IP_SCHEMA({ATTR_MAC: MAC, ATTR_DRY_RUN: "maybe"})


async def test_ip_table_single_entry_and_critical_match(hass) -> None:
    """One entry can be queried without an id, and a Shelly device is critical."""
    (entry,), (client,), _networks = _install(hass, "entry-1")
    client.devices.append(
        {
            "mac": MAC,
            "nickname": "",
            "hostname": "plug",
            "ip": "192.168.4.40",
            "connected": True,
            "manufacturer": "Shelly",
        }
    )
    shelly = MockConfigEntry(domain="shelly", entry_id="shelly-1", data={})
    shelly.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id="shelly-1",
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Plug",
    )
    response = await _call(hass, SERVICE_IP_TABLE, {})
    assert response["config_entry_id"] == entry.entry_id
    row = response["devices"][0]
    assert row["status"] == "dynamic"
    assert row["critical"] is True
    assert "shelly" in row["ha_integrations"]
    assert response["summary"]["critical_unreserved"] == 1
    sensor = EeroIpReservationSensor.__new__(EeroIpReservationSensor)
    sensor.hass = hass
    sensor._network_id = "net-1"
    sensor._config_entry = entry
    sensor.coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["critical_unreserved"] == 1
    assert sensor.name == "Ohana IP Reservations"


async def test_ip_table_requires_an_entry_when_several_are_loaded(hass) -> None:
    """A missing config entry is refused once a second eero entry is loaded."""
    _install(hass, "entry-1", "entry-2")
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_IP_TABLE, {})


async def test_reserve_pins_current_ip_and_refuses_a_private_mac(hass) -> None:
    """Pin uses the current address. A private MAC needs force."""
    (entry,), (client,), _networks = _install(hass, "entry-1")
    client.devices.append(
        {"mac": MAC, "nickname": "Amp", "hostname": "amp", "ip": "192.168.4.40"}
    )
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Denon",
    )
    response = await _call(
        hass, SERVICE_RESERVE_IP, {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_MAC: MAC}
    )
    assert response["dry_run"] is False
    assert response["changed"]["ip"] == "192.168.4.40"
    assert response["changed"]["description"] == "Denon"
    assert any(call["method"] == "POST" for call in client.calls)

    client.devices.append({"mac": PRIVATE, "ip": "192.168.4.41", "nickname": "Phone"})
    with pytest.raises(ServiceValidationError, match="private"):
        await _call(
            hass,
            SERVICE_RESERVE_IP,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_MAC: PRIVATE},
        )
    forced = await _call(
        hass,
        SERVICE_RESERVE_IP,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_MAC: PRIVATE,
            ATTR_FORCE: True,
            ATTR_DRY_RUN: True,
        },
    )
    assert forced["dry_run"] is True
    assert forced["changed"]["mac"] == PRIVATE


async def test_suggest_and_release(hass) -> None:
    """Suggest picks a free address, and release removes the reservation."""
    (entry,), (client,), _networks = _install(hass, "entry-1")
    client.devices.append(
        {"mac": MAC, "nickname": "Amp", "hostname": "amp", "ip": "192.168.4.40"}
    )
    suggested = await _call(
        hass,
        SERVICE_RESERVE_IP,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_MAC: MAC,
            ATTR_SUGGEST: True,
            ATTR_IP: "192.168.4.40",
        },
    )
    assert suggested["changed"]["ip"] != "192.168.4.40"
    assert suggested["changed"]["ip"].startswith("192.168.4.")
    released = await _call(
        hass,
        SERVICE_RELEASE_IP,
        {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_MAC: MAC, ATTR_DRY_RUN: True},
    )
    assert released["dry_run"] is True
    assert client.reservations
    await _call(
        hass,
        SERVICE_RELEASE_IP,
        {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_MAC: MAC},
    )
    assert client.reservations == []
