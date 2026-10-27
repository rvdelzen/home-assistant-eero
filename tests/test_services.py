"""DHCP reservation and port-forward services against a fake Eero API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import voluptuous as vol

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.eero.api import EeroException
from custom_components.eero.const import (
    ATTR_CLIENT_PORT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DESCRIPTION,
    ATTR_DRY_RUN,
    ATTR_FORWARD_ID,
    ATTR_GATEWAY_PORT,
    ATTR_IP,
    ATTR_MAC,
    ATTR_NETWORK,
    ATTR_PROTOCOL,
    ATTR_RESERVATION_ID,
    CONF_NETWORKS,
    DATA_API,
    DATA_COORDINATOR,
    DOMAIN,
    SERVICE_CREATE_PORT_FORWARD,
    SERVICE_CREATE_RESERVATION,
    SERVICE_DELETE_PORT_FORWARD,
    SERVICE_DELETE_RESERVATION,
    SERVICE_LIST_PORT_FORWARDS,
    SERVICE_LIST_RESERVATIONS,
    SERVICE_UPDATE_RESERVATION,
)
from custom_components.eero.services import async_register_services


class FakeAPI:
    """In-memory reservations and port forwards with a call log."""

    def __init__(self) -> None:
        self.reservations: list[dict] = []
        self.forwards: list[dict] = []
        self.calls: list[dict] = []
        self.fail_methods: set[str] = set()
        self.fail_get_number: int | None = None
        self._gets = 0

    def call(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "GET":
            self._gets += 1
            if self.fail_get_number is not None and self._gets >= self.fail_get_number:
                raise EeroException(message="read failed", error="read failed")
        elif method in self.fail_methods:
            raise EeroException(message="write failed", error="write failed")
        if "/reservations" in url:
            return self._handle(self.reservations, "reservations", method, url, kwargs)
        if "/forwards" in url:
            return self._handle(self.forwards, "forwards", method, url, kwargs)
        raise AssertionError(url)

    def _handle(self, records, resource, method, url, kwargs):
        item_id = url.rstrip("/").split("/")[-1]
        collection = item_id == resource
        if method == "GET" and collection:
            return [dict(item) for item in records]
        if method == "POST" and collection:
            body = dict(kwargs["json"])
            body["url"] = f"{url}/{resource[:-1]}-{len(records) + 1}"
            records.append(body)
            return dict(body)
        if method == "PUT" and not collection:
            for item in records:
                if str(item.get("id", "")) == item_id or str(item.get("url", "")).endswith(
                    "/" + item_id
                ):
                    item.update(kwargs["json"])
                    return dict(item)
            raise AssertionError(f"missing {item_id}")
        if method == "DELETE" and not collection:
            before = len(records)
            records[:] = [
                item
                for item in records
                if str(item.get("id", "")) != item_id
                and not str(item.get("url", "")).endswith("/" + item_id)
            ]
            if len(records) == before:
                raise AssertionError(f"missing {item_id}")
            return {}
        raise AssertionError(f"{method} {url}")


def _methods(api: FakeAPI) -> list[str]:
    return [call["method"] for call in api.calls]


def _writes(api: FakeAPI) -> list[dict]:
    return [call for call in api.calls if call["method"] != "GET"]


@pytest.fixture
def api(hass):
    """Register services against one loaded entry and its fake API."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-1",
        unique_id="log-1",
        title="Home",
        data={
            "name": "Home",
            "login": "user@example.com",
            "user_token": "token-1",
            CONF_NETWORKS: ["net-1", "net-2"],
        },
    )
    entry.add_to_hass(hass)
    client = FakeAPI()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_API: client,
        DATA_COORDINATOR: SimpleNamespace(
            data=SimpleNamespace(
                networks=[
                    SimpleNamespace(id="net-1", name="Home", name_unique="Home"),
                    SimpleNamespace(id="net-2", name="Guest", name_unique="Guest"),
                ]
            )
        ),
    }
    async_register_services(hass)
    return entry, client


def _seed_reservation(api: FakeAPI, reservation_id="res-1", **overrides) -> dict:
    item = {
        "url": f"/2.2/networks/net-1/reservations/{reservation_id}",
        "ip": "192.168.4.10",
        "mac": "aa:bb:cc:dd:ee:10",
        "description": "Camera",
    }
    item.update(overrides)
    api.reservations.append(item)
    return item


def _seed_forward(api: FakeAPI, forward_id="fwd-1", **overrides) -> dict:
    item = {
        "url": f"/2.2/networks/net-1/forwards/{forward_id}",
        "ip": "192.168.4.10",
        "client_port": 80,
        "gateway_port": 8080,
        "protocol": "tcp",
        "description": "Camera",
        "enabled": True,
    }
    item.update(overrides)
    api.forwards.append(item)
    return item


async def _call(hass, service, data, **kwargs):
    return await hass.services.async_call(
        DOMAIN,
        service,
        data,
        blocking=True,
        return_response=True,
        **kwargs,
    )


async def test_list_reservations_returns_normalized_records(hass, api) -> None:
    """List reads the reservations endpoint and returns id, ip, mac, and description."""
    entry, client = api
    _seed_reservation(client, mac="AA:BB:CC:DD:EE:10")

    response = await _call(
        hass,
        SERVICE_LIST_RESERVATIONS,
        {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_NETWORK: "Home"},
    )

    assert response == {
        "reservations": [
            {
                "id": "res-1",
                "ip": "192.168.4.10",
                "mac": "aa:bb:cc:dd:ee:10",
                "description": "Camera",
            }
        ]
    }
    assert client.calls[0]["url"] == "/2.2/networks/net-1/reservations"
    assert _methods(client) == ["GET"]


async def test_list_requires_return_response(hass, api) -> None:
    """The list service is SupportsResponse.ONLY."""
    entry, _client = api
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_LIST_RESERVATIONS,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id},
            blocking=True,
        )


async def test_default_network_is_the_first_configured_network(hass, api) -> None:
    """Omitting network uses the entry's first configured network."""
    entry, client = api
    await _call(hass, SERVICE_LIST_RESERVATIONS, {ATTR_CONFIG_ENTRY_ID: entry.entry_id})
    assert client.calls[0]["url"] == "/2.2/networks/net-1/reservations"


async def test_create_update_and_delete_reservation(hass, api) -> None:
    """Create, update, and delete each change one reservation and read the list back."""
    entry, client = api
    _seed_reservation(client)

    created = await _call(
        hass,
        SERVICE_CREATE_RESERVATION,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_MAC: "AA-BB-CC-DD-EE-FF",
            ATTR_IP: "192.168.4.50",
            ATTR_DESCRIPTION: "Printer",
        },
    )
    assert created["dry_run"] is False
    assert created["changed"]["id"] == "reservation-2"
    assert created["changed"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert created["changed"]["ip"] == "192.168.4.50"
    assert [item["id"] for item in created["before"]] == ["res-1"]
    assert [item["id"] for item in created["after"]] == ["res-1", "reservation-2"]
    post = _writes(client)[-1]
    assert post["method"] == "POST"
    assert post["json"] == {
        "ip": "192.168.4.50",
        "mac": "aa:bb:cc:dd:ee:ff",
        "description": "Printer",
    }
    assert post["allow_refresh"] is False

    updated = await _call(
        hass,
        SERVICE_UPDATE_RESERVATION,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_MAC: "aa:bb:cc:dd:ee:ff",
            ATTR_IP: "192.168.4.51",
        },
    )
    assert updated["changed"]["id"] == "reservation-2"
    assert updated["changed"]["ip"] == "192.168.4.51"
    assert updated["changed"]["description"] == "Printer"
    assert updated["after"][0] == created["before"][0]
    put = _writes(client)[-1]
    assert put["method"] == "PUT"
    assert put["url"] == "/2.2/networks/net-1/reservations/reservation-2"
    assert put["allow_refresh"] is False

    by_id = await _call(
        hass,
        SERVICE_UPDATE_RESERVATION,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_RESERVATION_ID: "res-1",
            ATTR_DESCRIPTION: "Doorbell",
        },
    )
    assert by_id["changed"]["description"] == "Doorbell"
    assert by_id["changed"]["ip"] == "192.168.4.10"

    deleted = await _call(
        hass,
        SERVICE_DELETE_RESERVATION,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_RESERVATION_ID: "reservation-2",
        },
    )
    assert deleted["changed"]["id"] == "reservation-2"
    assert [item["id"] for item in deleted["after"]] == ["res-1"]
    delete = _writes(client)[-1]
    assert delete["method"] == "DELETE"
    assert delete["url"] == "/2.2/networks/net-1/reservations/reservation-2"
    assert delete["params"] == {"delete_forwards": "false"}
    assert delete["allow_refresh"] is False
    assert "delete_forwards=true" not in delete["url"]


async def test_reservation_conflicts_and_missing_targets_do_not_write(hass, api) -> None:
    """IP and MAC conflicts, and a missing reservation, are refused before a write."""
    entry, client = api
    _seed_reservation(client)
    _seed_reservation(
        client,
        reservation_id="res-2",
        ip="192.168.4.20",
        mac="aa:bb:cc:dd:ee:20",
        description="Phone",
    )

    with pytest.raises(ServiceValidationError, match="MAC address"):
        await _call(
            hass,
            SERVICE_CREATE_RESERVATION,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MAC: "aa:bb:cc:dd:ee:10",
                ATTR_IP: "192.168.4.80",
                ATTR_DESCRIPTION: "Other",
            },
        )
    with pytest.raises(ServiceValidationError, match="IP address"):
        await _call(
            hass,
            SERVICE_UPDATE_RESERVATION,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_RESERVATION_ID: "res-2",
                ATTR_IP: "192.168.4.10",
            },
        )
    with pytest.raises(ServiceValidationError, match="not found"):
        await _call(
            hass,
            SERVICE_DELETE_RESERVATION,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MAC: "aa:bb:cc:dd:ee:99",
            },
        )

    assert _methods(client) == ["GET", "GET", "GET"]
    assert len(client.reservations) == 2


async def test_dry_run_does_not_call_the_write_endpoint(hass, api) -> None:
    """Dry run returns the planned reservation and leaves the API list unchanged."""
    entry, client = api
    _seed_reservation(client)
    before = [dict(item) for item in client.reservations]

    response = await _call(
        hass,
        SERVICE_CREATE_RESERVATION,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_MAC: "aa:bb:cc:dd:ee:ff",
            ATTR_IP: "192.168.4.50",
            ATTR_DESCRIPTION: "Printer",
            ATTR_DRY_RUN: True,
        },
    )

    assert response["dry_run"] is True
    assert response["changed"] == {
        "id": None,
        "ip": "192.168.4.50",
        "mac": "aa:bb:cc:dd:ee:ff",
        "description": "Printer",
    }
    assert response["after"][-1] == response["changed"]
    assert _methods(client) == ["GET"]
    assert client.reservations == before


async def test_invalid_mac_and_ipv4_are_rejected_before_the_api(hass, api) -> None:
    """MAC and IPv4 addresses are validated locally."""
    entry, client = api
    with pytest.raises(ServiceValidationError, match="Invalid MAC"):
        await _call(
            hass,
            SERVICE_CREATE_RESERVATION,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MAC: "not-a-mac",
                ATTR_IP: "192.168.4.50",
                ATTR_DESCRIPTION: "Printer",
            },
        )
    with pytest.raises(ServiceValidationError, match="Invalid IPv4"):
        await _call(
            hass,
            SERVICE_CREATE_RESERVATION,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MAC: "aa:bb:cc:dd:ee:ff",
                ATTR_IP: "2001:db8::1",
                ATTR_DESCRIPTION: "Printer",
            },
        )
    assert client.calls == []


async def test_write_error_is_not_retried_and_skips_read_back(hass, api) -> None:
    """An API error on the write stops the service after that single write."""
    entry, client = api
    client.fail_methods.add("POST")

    with pytest.raises(HomeAssistantError, match="write failed") as caught:
        await _call(
            hass,
            SERVICE_CREATE_RESERVATION,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MAC: "aa:bb:cc:dd:ee:ff",
                ATTR_IP: "192.168.4.50",
                ATTR_DESCRIPTION: "Printer",
            },
        )

    assert type(caught.value) is HomeAssistantError
    assert _methods(client) == ["GET", "POST"]
    assert client.reservations == []


async def test_read_back_error_does_not_retry(hass, api) -> None:
    """A failed read-back is surfaced and the GET is not repeated."""
    entry, client = api
    client.fail_get_number = 2

    with pytest.raises(HomeAssistantError, match="read failed"):
        await _call(
            hass,
            SERVICE_CREATE_RESERVATION,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MAC: "aa:bb:cc:dd:ee:ff",
                ATTR_IP: "192.168.4.50",
                ATTR_DESCRIPTION: "Printer",
            },
        )

    assert _methods(client) == ["GET", "POST", "GET"]


async def test_update_preserves_public_static_ip(hass, api) -> None:
    """An update sends public_static_ip through when the current record has one."""
    entry, client = api
    _seed_reservation(client, public_static_ip="203.0.113.10")

    await _call(
        hass,
        SERVICE_UPDATE_RESERVATION,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_RESERVATION_ID: "res-1",
            ATTR_DESCRIPTION: "Camera 2",
        },
    )

    assert _writes(client)[-1]["json"]["public_static_ip"] == "203.0.113.10"
    assert _writes(client)[-1]["json"]["mac"] == "aa:bb:cc:dd:ee:10"


async def test_unknown_entry_and_network_are_rejected(hass, api) -> None:
    """The service only runs for a loaded eero entry and one of its networks."""
    entry, client = api
    with pytest.raises(ServiceValidationError, match="not an eero entry"):
        await _call(
            hass,
            SERVICE_LIST_RESERVATIONS,
            {ATTR_CONFIG_ENTRY_ID: "missing"},
        )
    with pytest.raises(ServiceValidationError, match="not found"):
        await _call(
            hass,
            SERVICE_LIST_RESERVATIONS,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_NETWORK: "Missing"},
        )
    with pytest.raises(vol.Invalid):
        await _call(hass, SERVICE_LIST_RESERVATIONS, {})
    assert client.calls == []


async def test_port_forward_list_create_conflict_and_delete(hass, api) -> None:
    """Port forwards use the eero-api field names and the same write safeguards."""
    entry, client = api
    _seed_forward(client)

    listed = await _call(
        hass, SERVICE_LIST_PORT_FORWARDS, {ATTR_CONFIG_ENTRY_ID: entry.entry_id}
    )
    assert listed["forwards"][0]["gateway_port"] == 8080
    assert listed["forwards"][0]["protocol"] == "tcp"

    with pytest.raises(ServiceValidationError, match="already uses"):
        await _call(
            hass,
            SERVICE_CREATE_PORT_FORWARD,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_IP: "192.168.4.60",
                ATTR_CLIENT_PORT: 81,
                ATTR_GATEWAY_PORT: 8080,
                ATTR_PROTOCOL: "TCP",
                ATTR_DESCRIPTION: "Other",
            },
        )
    assert _methods(client) == ["GET", "GET"]

    created = await _call(
        hass,
        SERVICE_CREATE_PORT_FORWARD,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_IP: "192.168.4.60",
            ATTR_CLIENT_PORT: "81",
            ATTR_GATEWAY_PORT: 9090,
            ATTR_PROTOCOL: "udp",
            ATTR_DESCRIPTION: "Phone",
            ATTR_DRY_RUN: True,
        },
    )
    assert created["dry_run"] is True
    assert created["changed"]["protocol"] == "udp"
    assert _methods(client) == ["GET", "GET", "GET"]

    created = await _call(
        hass,
        SERVICE_CREATE_PORT_FORWARD,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_IP: "192.168.4.60",
            ATTR_CLIENT_PORT: 81,
            ATTR_GATEWAY_PORT: 9090,
            ATTR_PROTOCOL: "udp",
            ATTR_DESCRIPTION: "Phone",
        },
    )
    assert created["changed"]["id"] == "forward-2"
    assert _writes(client)[-1]["json"] == {
        "ip": "192.168.4.60",
        "client_port": 81,
        "gateway_port": 9090,
        "protocol": "udp",
        "description": "Phone",
        "enabled": True,
    }

    deleted = await _call(
        hass,
        SERVICE_DELETE_PORT_FORWARD,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_IP: "192.168.4.60",
            ATTR_GATEWAY_PORT: 9090,
            ATTR_PROTOCOL: "udp",
        },
    )
    assert deleted["changed"]["id"] == "forward-2"
    assert [item["id"] for item in deleted["after"]] == ["fwd-1"]
    assert _writes(client)[-1]["method"] == "DELETE"
    assert "params" not in _writes(client)[-1]
    assert _writes(client)[-1]["allow_refresh"] is False

    client.fail_methods.add("DELETE")
    with pytest.raises(HomeAssistantError, match="write failed") as caught:
        await _call(
            hass,
            SERVICE_DELETE_PORT_FORWARD,
            {ATTR_CONFIG_ENTRY_ID: entry.entry_id, ATTR_FORWARD_ID: "fwd-1"},
        )
    assert type(caught.value) is HomeAssistantError
    assert _methods(client)[-2:] == ["GET", "DELETE"]
