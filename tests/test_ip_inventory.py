"""Device and reservation polling that does not create client entities."""

from __future__ import annotations

import json
import logging

from custom_components.eero.api import EeroAPI, EeroUpdateConfig
from tests.test_api_session import FakeResponse


class RoutingSession:
    """Returns scripted JSON for the account poll and the IP inventory."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.devices = [
            {"mac": "00:11:22:33:44:55", "nickname": "Amp", "ip": "192.168.4.20"}
        ]
        self.reservations = [
            {"mac": "00:11:22:33:44:55", "ip": "192.168.4.20", "description": "Amp"}
        ]
        self.fail_devices = False

    def _payload(self, url: str):
        path = url.split(".com", 1)[-1]
        self.calls.append(path)
        if path.endswith("/account"):
            data = {"networks": {"data": [{"url": "/2.2/networks/net-1"}]}}
        elif path.endswith("/thread"):
            data = {}
        elif path.rstrip("/").endswith("/devices"):
            if self.fail_devices:
                return FakeResponse(
                    500,
                    {
                        "meta": {"code": 500, "error": "error"},
                        "data": {
                            "user_token": "super-secret-token",
                            "note": "INVENTORY-BODY-MARKER",
                        },
                    },
                )
            data = self.devices
        elif path.rstrip("/").endswith("/reservations"):
            data = self.reservations
        elif path.rstrip("/").endswith("/net-1"):
            data = {
                "url": "/2.2/networks/net-1",
                "name": "Home",
                "resources": {
                    "thread": "/2.2/networks/net-1/thread",
                    "devices": "/2.2/networks/net-1/devices",
                },
                "updates": {},
                "capabilities": {
                    "backup_access_point": {"capable": False, "requirements": []},
                    "premium": {"capable": False},
                },
                "premium_status": "disabled",
            }
        else:
            raise AssertionError(path)
        return FakeResponse(200, {"meta": {"code": 200}, "data": data})

    def get(self, url, **kwargs):
        return self._payload(url)

    def post(self, url, **kwargs):
        raise AssertionError(url)

    def put(self, url, **kwargs):
        raise AssertionError(url)

    def delete(self, url, **kwargs):
        raise AssertionError(url)


def _api() -> tuple[EeroAPI, RoutingSession]:
    api = EeroAPI(user_token="super-secret-token")
    session = RoutingSession()
    api.session = session
    return api, session


def test_inventory_is_polled_when_no_clients_are_selected() -> None:
    """Devices and reservations are fetched even when client entities are off."""
    api, session = _api()
    account = api.update({"net-1": EeroUpdateConfig()})
    network = account.networks[0]
    assert network.data["ip_devices"][0]["nickname"] == "Amp"
    assert network.data["ip_reservations"][0]["description"] == "Amp"
    assert "devices" not in network.data
    assert [path for path in session.calls if path.endswith("/devices")]
    assert [path for path in session.calls if path.endswith("/reservations")]


def test_devices_are_not_fetched_twice_when_clients_are_enabled() -> None:
    """A client poll's device list is reused for the inventory."""
    api, session = _api()
    account = api.update({"net-1": EeroUpdateConfig(get_devices=True)})
    network = account.networks[0]
    device_calls = [path for path in session.calls if path.endswith("/devices")]
    assert device_calls == ["/2.2/networks/net-1/devices"]
    assert network.data["ip_devices"] == network.data["devices"]["data"]


def test_every_second_poll_reuses_the_snapshot() -> None:
    """ip_table_every skips later polls until the interval elapses."""
    api, session = _api()
    config = {"net-1": EeroUpdateConfig()}
    api.update(config, ip_table_every=2)
    session.calls.clear()
    api.update(config, ip_table_every=2)
    assert not any(path.endswith("/devices") for path in session.calls)
    assert api.data.networks[0].data["ip_devices"][0]["mac"] == "00:11:22:33:44:55"


def test_a_skipped_poll_fetches_when_nothing_is_cached() -> None:
    """The first available poll fetches even when the interval would skip."""
    api, session = _api()
    api._ip_poll_index = 1
    api.update({"net-1": EeroUpdateConfig()}, ip_table_every=2)
    assert any(path.endswith("/devices") for path in session.calls)


def test_failed_inventory_keeps_the_last_good_data_and_logs_once(caplog) -> None:
    """A later failure does not drop the snapshot or log the response body."""
    api, session = _api()
    config = {"net-1": EeroUpdateConfig()}
    api.update(config)
    session.fail_devices = True
    session.calls.clear()
    with caplog.at_level(logging.WARNING):
        account = api.update(config)
        api.update(config)
    assert account.networks[0].data["ip_devices"][0]["nickname"] == "Amp"
    assert account.networks[0].data["ip_inventory_stale"] is True
    assert caplog.text.count("keeping the last good data") == 1
    assert "super-secret-token" not in caplog.text
    assert "INVENTORY-BODY-MARKER" not in caplog.text
    assert "super-secret-token" not in json.dumps(account.networks[0].data)
