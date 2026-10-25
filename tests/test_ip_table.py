"""Row merge, status, private MACs, and free-address selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.eero.ip_table import (
    STATUS_DYNAMIC,
    STATUS_FIXED,
    STATUS_MISMATCH,
    STATUS_STALE,
    build_ip_table,
    is_private_mac,
    registry_mac_records,
    sensor_attributes,
    suggest_free_ip,
)


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def _device(**overrides):
    item = {
        "mac": "00:11:22:33:44:55",
        "nickname": "Amp",
        "hostname": "amp",
        "ip": "192.168.4.20",
        "connected": True,
        "last_active": "2026-10-25T12:00:00Z",
        "manufacturer": "Denon",
        "device_type": "audio",
        "connection_type": "wireless",
        "source": {"location": "Den"},
    }
    item.update(overrides)
    return item


def _reservation(**overrides):
    item = {
        "mac": "00:11:22:33:44:55",
        "ip": "192.168.4.20",
        "description": "Amp",
    }
    item.update(overrides)
    return item


def test_private_mac_uses_the_local_bit() -> None:
    """A locally administered MAC is private; a global one is not."""
    assert is_private_mac("02:11:22:33:44:55") is True
    assert is_private_mac("AA:BB:CC:DD:EE:FF") is True
    assert is_private_mac("00:11:22:33:44:55") is False


def test_status_merge_mismatch_and_stale() -> None:
    """Rows merge by MAC and stale wins over a mismatched offline lease."""
    table = build_ip_table(
        [
            _device(),
            _device(
                mac="00:11:22:33:44:66",
                nickname="Moved",
                ip="192.168.4.30",
                connected=True,
            ),
            _device(
                mac="00:11:22:33:44:77",
                nickname="Old",
                ip="192.168.4.40",
                connected=False,
                last_active="2026-08-01T00:00:00Z",
            ),
            _device(
                mac="00:11:22:33:44:88",
                nickname="",
                hostname="phone",
                ip="192.168.4.50",
                connected=True,
            ),
        ],
        [
            _reservation(),
            _reservation(mac="00:11:22:33:44:66", ip="192.168.4.31"),
            _reservation(mac="00:11:22:33:44:77", ip="192.168.4.41"),
            _reservation(mac="00:11:22:33:44:99", ip="192.168.4.99", description="Gone"),
        ],
        now=NOW,
        stale_after_days=30,
    )
    by_mac = {row["mac"]: row for row in table["devices"]}
    assert by_mac["00:11:22:33:44:55"]["status"] == STATUS_FIXED
    assert by_mac["00:11:22:33:44:66"]["status"] == STATUS_MISMATCH
    assert by_mac["00:11:22:33:44:77"]["status"] == STATUS_STALE
    assert by_mac["00:11:22:33:44:88"]["status"] == STATUS_DYNAMIC
    assert by_mac["00:11:22:33:44:88"]["name"] == "phone"
    assert by_mac["00:11:22:33:44:99"]["status"] == STATUS_STALE
    assert by_mac["00:11:22:33:44:99"]["name"] == "Gone"
    assert table["summary"]["reservations"] == 4
    assert table["summary"]["fixed"] == 1
    assert table["summary"]["fixed_mismatch"] == 1
    assert table["summary"]["stale_reservation"] == 2
    assert table["summary"]["dynamic"] == 1


def test_connected_device_is_not_stale() -> None:
    """A connected reservation is a mismatch even when last_active is old."""
    table = build_ip_table(
        [
            _device(
                connected=True,
                ip="192.168.4.21",
                last_active=(NOW - timedelta(days=90)).isoformat(),
            )
        ],
        [_reservation(ip="192.168.4.20")],
        now=NOW,
    )
    assert table["devices"][0]["status"] == STATUS_MISMATCH


def test_registry_match_is_case_insensitive_and_marks_critical() -> None:
    """Connection MACs match the device registry and merge integrations."""
    shelly = SimpleNamespace(
        id="dev-1",
        name="Plug",
        name_by_user=None,
        connections=[("MAC", "00-11-22-33-44-55")],
        config_entries=["shelly-1"],
    )
    other = SimpleNamespace(
        id="dev-2",
        name="Also",
        name_by_user="Kitchen plug",
        connections=[("mac", "00:11:22:33:44:55")],
        config_entries=["other-1"],
    )
    records = registry_mac_records(
        [shelly, other],
        lambda entry_id: {"shelly-1": "shelly", "other-1": "mqtt"}.get(entry_id),
    )
    table = build_ip_table(
        [_device(nickname="")],
        [],
        ha_devices=records,
        critical_integrations={"shelly"},
        now=NOW,
    )
    row = table["devices"][0]
    assert row["ha_integrations"] == ["shelly", "mqtt"]
    assert row["critical"] is True
    assert row["ha_device_id"] == "dev-1"
    assert row["ha_device_name"] == "Plug"
    assert table["summary"]["critical_unreserved"] == 1
    assert set(sensor_attributes(table["summary"])) == {
        "fixed",
        "dynamic",
        "mismatch",
        "stale",
        "critical_unreserved",
        "private_mac_reserved",
    }


def test_suggest_free_ip_prefers_the_largest_block() -> None:
    """Free selection skips reserved, leased, gateway, .1, and node addresses."""
    devices = [
        _device(mac="00:11:22:33:44:10", ip="192.168.4.11"),
        _device(mac="00:11:22:33:44:12", ip="192.168.4.12"),
    ]
    for last in range(13, 201):
        devices.append(
            _device(mac=f"00:11:22:33:45:{last:02x}", ip=f"192.168.4.{last}")
        )
    chosen = suggest_free_ip(
        gateway="192.168.4.1",
        devices=devices,
        reservations=[_reservation(mac="00:11:22:33:44:0a", ip="192.168.4.10")],
        node_ips=["192.168.4.2"],
    )
    assert chosen == "192.168.4.201"


def test_suggest_free_ip_rejects_an_empty_range() -> None:
    """A range with nothing left raises instead of returning the gateway."""
    with pytest.raises(ValueError):
        suggest_free_ip(
            gateway="192.168.4.1",
            reservation_range="192.168.4.1-192.168.4.1",
        )
