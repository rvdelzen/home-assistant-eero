"""Name matching, area prefix, flagged rows, and the 2.3 write path."""

from __future__ import annotations

from urllib.parse import unquote

import pytest
import voluptuous as vol

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr

from custom_components.eero.const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DRY_RUN,
    ATTR_MACS,
    ATTR_PREFIX_AREA,
    ATTR_TARGETS,
    DOMAIN,
    SERVICE_SYNC_NAMES,
)
from custom_components.eero.names import (
    PREFIX_NEVER,
    PREFIX_WHEN_MISSING,
    TARGET_NICKNAME,
    TARGET_RESERVATION,
    match_ha_device,
    plan_name_sync,
    proposed_name,
)
from custom_components.eero.services import SYNC_NAMES_SCHEMA
from tests.test_services import FakeAPI

from pytest_homeassistant_custom_component.common import MockConfigEntry


MAC = "00:11:22:33:44:55"
MAC_B = "00:11:22:33:44:66"
PRIVATE = "02:11:22:33:44:77"


def _ha(**overrides):
    item = {
        "id": "dev",
        "name": "Amp",
        "name_by_user": None,
        "area_id": None,
        "area_name": None,
        "model": None,
        "connections": [],
        "identifiers": [],
    }
    item.update(overrides)
    return item


def test_match_tiers_prefer_a_named_sibling() -> None:
    """Connections win first, then an embedded identifier, then a named sibling."""
    connection = _ha(
        id="conn",
        name_by_user="Hall Amp",
        connections=[("mac", MAC)],
    )
    assert match_ha_device(MAC, [connection])["tier"] == "connection"
    assert match_ha_device(MAC, [connection])["device"]["id"] == "conn"

    embedded = _ha(
        id="oven",
        name="Oven",
        identifiers=[("home_connect_local", f"HC-{MAC_B}")],
    )
    assert match_ha_device(MAC_B, [embedded])["tier"] == "identifier"

    seed = _ha(
        id="seed",
        name="Shelly",
        connections=[("mac", "00:11:22:33:44:88")],
        identifiers=[("shelly", "plug-1")],
    )
    sibling = _ha(
        id="sib",
        name="Plug",
        name_by_user="Hall Light",
        area_id="hall",
        identifiers=[("shelly", "plug-1")],
    )
    matched = match_ha_device("00:11:22:33:44:88", [seed, sibling])
    assert matched["tier"] == "sibling"
    assert matched["device"]["id"] == "sib"


def test_prefix_area_rules() -> None:
    """Prefix the area only when it is missing, and expand a bare area with the model."""
    assert (
        proposed_name(
            ha_name="Kitchen",
            area_name="Kitchen",
            model="Denon Home Amp",
            prefix_area=PREFIX_WHEN_MISSING,
        )
        == "Kitchen Denon Home Amp"
    )
    assert (
        proposed_name(
            ha_name="Kitchen Amp",
            area_name="Kitchen",
            model="Denon Home Amp",
            prefix_area=PREFIX_WHEN_MISSING,
        )
        == "Kitchen Amp"
    )
    assert (
        proposed_name(
            ha_name="Amp",
            area_name="Kitchen",
            model="Denon",
            prefix_area=PREFIX_WHEN_MISSING,
        )
        == "Kitchen Amp"
    )
    assert (
        proposed_name(
            ha_name="Kitchen",
            area_name="Kitchen",
            model="Denon Home Amp",
            prefix_area=PREFIX_NEVER,
        )
        == "Kitchen"
    )
    assert (
        proposed_name(
            ha_name="Kitchen",
            area_name="Kitchen",
            model=None,
            prefix_area=PREFIX_WHEN_MISSING,
        )
        == "Kitchen"
    )


def test_plan_flags_unmatched_private_unnamed_and_duplicates() -> None:
    """Those rows are flagged and have nothing to write."""
    devices = [
        {"mac": MAC, "nickname": "Old", "url": f"/2.2/networks/net/devices/{MAC}"},
        {"mac": MAC_B, "nickname": "Other", "url": f"/2.2/networks/net/devices/{MAC_B}"},
        {"mac": PRIVATE, "nickname": "Phone", "url": f"/2.2/networks/net/devices/{PRIVATE}"},
        {
            "mac": "00:11:22:33:44:99",
            "nickname": "Blank",
            "url": "/2.2/networks/net/devices/00:11:22:33:44:99",
        },
        {
            "mac": "00:11:22:33:44:aa",
            "nickname": "Lost",
            "url": "/2.2/networks/net/devices/00:11:22:33:44:aa",
        },
    ]
    reservations = [
        {"mac": MAC, "description": "Old", "url": "/2.2/networks/net/reservations/r1"},
        {"mac": MAC_B, "description": "Other", "url": "/2.2/networks/net/reservations/r2"},
    ]
    home = [
        _ha(id="a", name_by_user="Printer", connections=[("mac", MAC)]),
        _ha(id="b", name="printer", connections=[("mac", MAC_B)]),
        _ha(id="p", name_by_user="Phone", connections=[("mac", PRIVATE)]),
        _ha(id="u", name="  ", name_by_user=None, connections=[("mac", "00:11:22:33:44:99")]),
    ]
    rows = {
        row["mac"]: row
        for row in plan_name_sync(
            devices,
            reservations,
            home,
            targets=[TARGET_NICKNAME, TARGET_RESERVATION],
            prefix_area=PREFIX_WHEN_MISSING,
        )
    }
    assert rows[MAC]["reason"] == "duplicate_name"
    assert rows[MAC_B]["reason"] == "duplicate_name"
    assert rows[MAC]["fields"] == []
    assert rows[PRIVATE]["reason"] == "private_mac"
    assert rows[PRIVATE]["proposed_name"] == "Phone"
    assert rows["00:11:22:33:44:99"]["reason"] == "unnamed"
    assert rows["00:11:22:33:44:aa"]["reason"] == "unmatched"
    assert all(row["action"] == "flagged" for row in rows.values())


def test_sync_schema_rejects_bad_input() -> None:
    """The sync schema requires an entry and known targets."""
    with pytest.raises(vol.Invalid):
        SYNC_NAMES_SCHEMA({})
    with pytest.raises(vol.Invalid):
        SYNC_NAMES_SCHEMA(
            {ATTR_CONFIG_ENTRY_ID: "entry-1", ATTR_PREFIX_AREA: "always"}
        )
    with pytest.raises(vol.Invalid):
        SYNC_NAMES_SCHEMA({ATTR_CONFIG_ENTRY_ID: "entry-1", ATTR_TARGETS: []})
    with pytest.raises(vol.Invalid):
        SYNC_NAMES_SCHEMA({ATTR_CONFIG_ENTRY_ID: "entry-1", ATTR_DRY_RUN: "maybe"})


class NameAPI(FakeAPI):
    """Reservations plus a device list and a 2.3 nickname resource."""

    def __init__(self) -> None:
        super().__init__()
        self.devices: list[dict] = []
        self.nicknames: dict[str, str] = {}
        self.reject_nickname = False

    def call(self, method, url, **kwargs):
        if "/devices" in url and "/reservations" not in url:
            self.calls.append({"method": method, "url": url, **kwargs})
            if method in self.fail_methods:
                from custom_components.eero.api import EeroException

                raise EeroException(message="write failed", error="write failed")
            device_id = unquote(url.rstrip("/").split("/")[-1])
            if method == "GET" and device_id == "devices":
                return [dict(item) for item in self.devices]
            if method == "PUT":
                self.nicknames[device_id] = kwargs["json"]["nickname"]
                return {}
            if method == "GET":
                nickname = self.nicknames.get(device_id, "")
                if self.reject_nickname:
                    nickname = "not-written"
                return {"nickname": nickname, "mac": device_id}
            raise AssertionError(f"{method} {url}")
        return super().call(method, url, **kwargs)


@pytest.fixture
def names(hass):
    """One loaded entry whose API records nickname writes."""
    from types import SimpleNamespace

    from custom_components.eero.const import CONF_NETWORKS, DATA_API, DATA_COORDINATOR
    from custom_components.eero.services import async_register_services

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-1",
        unique_id="log-1",
        title="Home",
        data={
            "name": "Home",
            "login": "user@example.com",
            "user_token": "token",
            CONF_NETWORKS: ["net-1"],
        },
    )
    entry.add_to_hass(hass)
    client = NameAPI()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_API: client,
        DATA_COORDINATOR: SimpleNamespace(
            data=SimpleNamespace(
                networks=[SimpleNamespace(id="net-1", name="Home", name_unique="Home")]
            )
        ),
    }
    async_register_services(hass)
    return entry, client


def _seed(client: NameAPI, mac: str = MAC, nickname: str = "Old", description: str = "Old") -> None:
    client.devices.append(
        {
            "mac": mac,
            "nickname": nickname,
            "url": f"/2.2/networks/net-1/devices/{mac}",
            "ip": "192.168.4.20",
        }
    )
    client.nicknames[mac] = nickname
    client.reservations.append(
        {
            "url": f"/2.2/networks/net-1/reservations/{mac.replace(':', '')}",
            "ip": "192.168.4.20",
            "mac": mac,
            "description": description,
        }
    )


async def _call(hass, data):
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_SYNC_NAMES,
        data,
        blocking=True,
        return_response=True,
    )


async def test_dry_run_makes_no_write_calls(hass, names) -> None:
    """A dry run reads the inventory and does not PUT, POST, or DELETE."""
    entry, client = names
    _seed(client)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Kitchen Amp",
    )
    response = await _call(hass, {ATTR_CONFIG_ENTRY_ID: entry.entry_id})
    assert response["dry_run"] is True
    assert response["changes"][0]["action"] == "update"
    assert response["changes"][0]["result"] == "dry_run"
    assert response["changes"][0]["proposed_name"] == "Kitchen Amp"
    assert response["changes"][0]["ha_device_id"] == device.id
    assert response["changes"][0]["match"] == "connection"
    assert [call["method"] for call in client.calls] == ["GET", "GET"]


async def test_sync_writes_nickname_on_v23_and_updates_the_reservation(hass, names) -> None:
    """Nickname uses /2.3 and the description uses the reservation update."""
    entry, client = names
    _seed(client)
    area = ar.async_get(hass).async_create("Kitchen")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Kitchen",
        model="Denon Home Amp",
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    response = await _call(
        hass,
        {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_DRY_RUN: False},
    )
    row = response["changes"][0]
    assert row["result"] == "updated"
    assert row["proposed_name"] == "Kitchen Denon Home Amp"
    assert row["fields"] == ["nickname", "reservation"]
    nickname_writes = [
        call
        for call in client.calls
        if call["method"] == "PUT" and "/devices/" in call["url"]
    ]
    assert len(nickname_writes) == 1
    assert nickname_writes[0]["url"].startswith("/2.3/networks/net-1/devices/")
    assert nickname_writes[0]["json"] == {"nickname": "Kitchen Denon Home Amp"}
    assert nickname_writes[0]["allow_refresh"] is False
    reservation_writes = [
        call
        for call in client.calls
        if call["method"] == "PUT" and "/reservations/" in call["url"]
    ]
    assert len(reservation_writes) == 1
    assert reservation_writes[0]["json"]["description"] == "Kitchen Denon Home Amp"
    assert "/2.2/networks/" in reservation_writes[0]["url"]
    assert not any("/2.2/" in call["url"] and call["method"] == "PUT" and "/devices/" in call["url"] for call in client.calls)


async def test_rejected_nickname_is_not_retried(hass, names) -> None:
    """A read-back mismatch stops the sync and does not update the reservation."""
    entry, client = names
    client.reject_nickname = True
    _seed(client, description="Keep")
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Kitchen Amp",
    )
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await _call(
            hass,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_DRY_RUN: False},
        )
    nickname_writes = [
        call for call in client.calls if call["method"] == "PUT" and "/devices/" in call["url"]
    ]
    assert len(nickname_writes) == 1
    assert not any(call["method"] == "PUT" and "/reservations/" in call["url"] for call in client.calls)


async def test_identifier_match_and_mac_filter(hass, names) -> None:
    """An identifier MAC is used, and the macs filter drops other devices."""
    entry, client = names
    _seed(client, mac=MAC_B, nickname="Oven", description="Oven")
    _seed(client, mac=MAC, nickname="Other", description="Other")
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("home_connect_local", f"local-{MAC_B}")},
        name="Wall Oven",
    )
    response = await _call(
        hass,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_MACS: [MAC_B],
            ATTR_TARGETS: [TARGET_NICKNAME],
        },
    )
    assert [row["mac"] for row in response["changes"]] == [MAC_B]
    assert response["changes"][0]["match"] == "identifier"
    assert response["changes"][0]["proposed_name"] == "Wall Oven"
    assert response["changes"][0]["fields"] == ["nickname"]


async def test_service_flags_a_private_mac(hass, names) -> None:
    """A matched private MAC is returned under flagged and is not written."""
    entry, client = names
    _seed(client, mac=PRIVATE, nickname="Phone", description="Phone")
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, PRIVATE)},
        name="Pixel",
    )
    response = await _call(
        hass,
        {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_DRY_RUN: False},
    )
    assert response["flagged"][0]["reason"] == "private_mac"
    assert response["flagged"][0]["action"] == "flagged"
    assert not any(call["method"] != "GET" for call in client.calls)
    with pytest.raises((ServiceValidationError, vol.Invalid)):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SYNC_NAMES,
            {ATTR_PREFIX_AREA: "always"},
            blocking=True,
            return_response=True,
        )
