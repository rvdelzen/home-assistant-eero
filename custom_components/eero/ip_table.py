"""Merge Eero devices and DHCP reservations into one IP table.

The functions in this module do not talk to Home Assistant or the Eero API.
Callers pass plain dictionaries and receive plain dictionaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import itertools
import re
from typing import Any

from .const import (
    CONF_CRITICAL_INTEGRATIONS,
    CONF_IP_TABLE_EVERY,
    CONF_RESERVATION_RANGE,
    CONF_STALE_RESERVATION_DAYS,
    DEFAULT_CRITICAL_INTEGRATIONS,
    DEFAULT_IP_TABLE_EVERY,
    DEFAULT_RESERVATION_RANGE,
    DEFAULT_STALE_RESERVATION_DAYS,
    MAX_IP_TABLE_EVERY,
    MAX_STALE_RESERVATION_DAYS,
    MIN_IP_TABLE_EVERY,
    MIN_STALE_RESERVATION_DAYS,
)

STATUS_DYNAMIC = "dynamic"
STATUS_FIXED = "fixed"
STATUS_MISMATCH = "fixed_mismatch"
STATUS_STALE = "stale_reservation"

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{12}$")
_MAX_RANGE = 4096

SENSOR_ATTRIBUTES = (
    ("fixed", "fixed"),
    ("dynamic", "dynamic"),
    ("mismatch", "fixed_mismatch"),
    ("stale", "stale_reservation"),
    ("critical_unreserved", "critical_unreserved"),
    ("private_mac_reserved", "private_mac_reserved"),
)


def mac_key(value: Any) -> str:
    """Return a lowercase colon MAC, or an empty string when it is not a MAC."""
    text = str(value or "").strip()
    if not _MAC_RE.match(text):
        return ""
    hexdigits = re.sub(r"[^0-9A-Fa-f]", "", text).lower()
    if len(hexdigits) != 12:
        return ""
    return ":".join(hexdigits[index : index + 2] for index in range(0, 12, 2))


def is_private_mac(value: Any) -> bool:
    """Return True when the locally administered bit of the MAC is set."""
    key = mac_key(value)
    if not key:
        return False
    return bool(int(key.split(":", 1)[0], 16) & 0x02)


def ip_key(value: Any) -> str:
    """Return a canonical IPv4 address, or an empty string."""
    if value is None or isinstance(value, bool):
        return ""
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except (ipaddress.AddressValueError, ValueError):
        return ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "connected"}
    return False


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def critical_integration_set(value: Any) -> set[str]:
    """Return the integration domains that mark a device as critical."""
    if value is None or value == "":
        names = DEFAULT_CRITICAL_INTEGRATIONS
    elif isinstance(value, str):
        names = [item.strip() for item in value.split(",")]
    else:
        names = list(value)
    return {str(name).strip().lower() for name in names if str(name).strip()}


def critical_integrations_text(value: Any = None) -> str:
    """Return the critical-integration option as comma-separated text."""
    if value is None or value == "":
        return ", ".join(DEFAULT_CRITICAL_INTEGRATIONS)
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value)


def ip_table_settings(options: dict[str, Any] | None, data: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve IP-table options. Saved options win over config-entry data."""
    options = options or {}
    data = data or {}

    def pick(key: str, default: Any) -> Any:
        if key in options and options[key] not in (None, ""):
            return options[key]
        if key in data and data[key] not in (None, ""):
            return data[key]
        return default

    return {
        "every": _bounded_int(
            pick(CONF_IP_TABLE_EVERY, DEFAULT_IP_TABLE_EVERY),
            DEFAULT_IP_TABLE_EVERY,
            MIN_IP_TABLE_EVERY,
            MAX_IP_TABLE_EVERY,
        ),
        "stale_days": _bounded_int(
            pick(CONF_STALE_RESERVATION_DAYS, DEFAULT_STALE_RESERVATION_DAYS),
            DEFAULT_STALE_RESERVATION_DAYS,
            MIN_STALE_RESERVATION_DAYS,
            MAX_STALE_RESERVATION_DAYS,
        ),
        "reservation_range": _text(
            pick(CONF_RESERVATION_RANGE, DEFAULT_RESERVATION_RANGE)
        ),
        "critical_integrations": critical_integration_set(
            pick(CONF_CRITICAL_INTEGRATIONS, None)
        ),
    }


def validate_reservation_range(value: Any) -> str:
    """Return a cleaned range, or raise ValueError when the text is unusable."""
    text = _text(value)
    if not text:
        return ""
    _range_bounds(text)
    return text


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an Eero timestamp into an aware UTC datetime."""
    if isinstance(value, datetime):
        parsed = value
    elif not value:
        return None
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def current_ipv4(device: dict[str, Any] | None) -> str:
    """Return the device's current IPv4 address."""
    if not device:
        return ""
    for key in ("ip", "ipv4"):
        parsed = ip_key(device.get(key))
        if parsed:
            return parsed
    for item in device.get("ips") or []:
        if isinstance(item, str):
            parsed = ip_key(item)
        elif isinstance(item, dict):
            parsed = ip_key(item.get("ip"))
        else:
            parsed = ""
        if parsed:
            return parsed
    lease = device.get("lease")
    if isinstance(lease, dict):
        parsed = ip_key(lease.get("ip"))
        if parsed:
            return parsed
    return ""


def device_ips(device: dict[str, Any]) -> set[str]:
    """Return every IPv4 address seen on a device record."""
    found = set()
    primary = current_ipv4(device)
    if primary:
        found.add(primary)
    for item in device.get("ips") or []:
        if isinstance(item, str):
            parsed = ip_key(item)
        elif isinstance(item, dict):
            parsed = ip_key(item.get("ip"))
        else:
            parsed = ""
        if parsed:
            found.add(parsed)
    lease = device.get("lease")
    if isinstance(lease, dict):
        parsed = ip_key(lease.get("ip"))
        if parsed:
            found.add(parsed)
    return found


def default_reservation_description(
    device: dict[str, Any] | None, ha_name: str | None, mac: str
) -> str:
    """Choose a reservation label: HA name, then nickname, then hostname."""
    nickname = _text(device.get("nickname")) if device else ""
    hostname = _text(device.get("hostname")) if device else ""
    return _text(ha_name) or nickname or hostname or mac


_MISSING = object()


def iter_device_entries(registry: Any) -> Any:
    """Yield device entries.

    Home Assistant 2026.8+ makes ``registry.devices`` iterate entries, and
    using it as a mapping (``.values()``, ``.get()``) warns until 2027.9.
    Older cores store a mapping whose iteration yields device ids.
    """
    devices = registry.devices
    if isinstance(devices, dict):
        return iter(devices.values())
    iterator = iter(devices)
    try:
        first = next(iterator)
    except StopIteration:
        return iter(())
    if isinstance(first, str):
        return iter(devices.values())
    return itertools.chain((first,), iterator)


def device_config_entry_ids(device: Any) -> list[str]:
    """Return the config entry ids that own a device.

    ``config_entry_id`` is the single owner from Home Assistant 2026.8.
    Reading ``config_entries`` warns on 2026.10 and stops working in 2027.10,
    so it is only used when the new attribute is absent.
    """
    single = getattr(device, "config_entry_id", _MISSING)
    if single is not _MISSING:
        return [str(single)] if single else []
    entries = getattr(device, "config_entries", ()) or ()
    return [str(entry_id) for entry_id in entries if entry_id]


def lookup_device_by_identifier(
    registry: Any, identifier: tuple[str, str], config_entry_id: str | None
) -> Any:
    """Find one device by identifier, scoped to its config entry when possible.

    ``async_get_device`` is ambiguous once identifiers are per config entry
    and is removed in Home Assistant 2027.8.
    """
    scoped = getattr(registry, "async_get_device_by_identifier", None)
    if scoped is not None and config_entry_id:
        return scoped(identifier, config_entry_id)
    return registry.async_get_device(identifiers={identifier})


def registry_mac_records(devices: Any, domain_for: Any) -> list[dict[str, Any]]:
    """Index device-registry entries by connection MAC.

    ``domain_for(entry_id)`` returns the integration domain that owns that
    config entry. Matching is case-insensitive and colon-normalised.
    """
    records: list[dict[str, Any]] = []
    for device in devices:
        macs: list[str] = []
        for connection in getattr(device, "connections", ()) or ():
            if not isinstance(connection, (tuple, list)) or len(connection) != 2:
                continue
            conn_type, value = connection
            if str(conn_type).lower() != "mac":
                continue
            key = mac_key(value)
            if key and key not in macs:
                macs.append(key)
        if not macs:
            continue
        integrations: list[str] = []
        for entry_id in device_config_entry_ids(device):
            domain = domain_for(entry_id)
            if domain and domain not in integrations:
                integrations.append(domain)
        name = _text(getattr(device, "name_by_user", None)) or _text(
            getattr(device, "name", None)
        )
        for mac in macs:
            records.append(
                {
                    "mac": mac,
                    "ha_device_id": getattr(device, "id", None),
                    "ha_device_name": name or None,
                    "ha_integrations": list(integrations),
                }
            )
    return records


def _ha_by_mac(
    records: list[dict[str, Any]] | None, critical: set[str]
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records or []:
        key = mac_key(record.get("mac"))
        if not key:
            continue
        grouped.setdefault(key, []).append(record)
    chosen: dict[str, dict[str, Any]] = {}
    for key, matches in grouped.items():

        def rank(record: dict[str, Any]) -> tuple[int, str]:
            domains = {str(domain).lower() for domain in record.get("ha_integrations") or []}
            return (1 if domains & critical else 0, _text(record.get("ha_device_name")))

        best = max(matches, key=rank)
        integrations: list[str] = []
        for record in matches:
            for domain in record.get("ha_integrations") or []:
                if domain not in integrations:
                    integrations.append(domain)
        chosen[key] = {
            "ha_device_id": best.get("ha_device_id"),
            "ha_device_name": best.get("ha_device_name") or None,
            "ha_integrations": integrations,
        }
    return chosen


def _device_node(device: dict[str, Any]) -> str | None:
    source = device.get("source")
    if isinstance(source, dict):
        location = _text(source.get("location") or source.get("display_name"))
        if location:
            return location
    for key in ("connected_to", "source_location", "node"):
        value = device.get(key)
        if value and not isinstance(value, (dict, list)):
            return str(value)
    return None


def _connection_type(device: dict[str, Any]) -> str | None:
    if device.get("connection_type"):
        return str(device["connection_type"])
    if device.get("wireless") is True:
        return "wifi"
    if device.get("wireless") is False:
        return "wired"
    return None


def _prefer_device(
    current: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    current_up = _as_bool(current.get("connected"))
    candidate_up = _as_bool(candidate.get("connected"))
    if candidate_up and not current_up:
        return candidate
    if current_up and not candidate_up:
        return current
    current_seen = parse_timestamp(current.get("last_active"))
    candidate_seen = parse_timestamp(candidate.get("last_active"))
    if candidate_seen and (current_seen is None or candidate_seen > current_seen):
        return candidate
    return current


def _is_stale(
    *,
    connected: bool,
    seen: datetime | None,
    now: datetime,
    stale_after: timedelta,
) -> bool:
    if connected:
        return False
    if seen is None:
        return True
    return seen <= now - stale_after


def _status(
    *,
    reserved_ip: str,
    current_ip: str,
    connected: bool,
    seen: datetime | None,
    now: datetime,
    stale_after: timedelta,
) -> str:
    if not reserved_ip:
        return STATUS_DYNAMIC
    if _is_stale(connected=connected, seen=seen, now=now, stale_after=stale_after):
        return STATUS_STALE
    if current_ip and current_ip != reserved_ip:
        return STATUS_MISMATCH
    return STATUS_FIXED


def _empty_summary(stale_days: int, inventory_stale: bool) -> dict[str, Any]:
    return {
        "devices": 0,
        "reservations": 0,
        "fixed": 0,
        "dynamic": 0,
        "fixed_mismatch": 0,
        "stale_reservation": 0,
        "critical": 0,
        "critical_unreserved": 0,
        "private_mac": 0,
        "private_mac_reserved": 0,
        "online": 0,
        "stale_after_days": stale_days,
        "inventory_stale": inventory_stale,
    }


def sensor_attributes(summary: dict[str, Any]) -> dict[str, int]:
    """Return the small attribute set stored on the reservation sensor."""
    return {name: int(summary.get(source, 0) or 0) for name, source in SENSOR_ATTRIBUTES}


def build_ip_table(
    devices: list[dict[str, Any]] | None,
    reservations: list[dict[str, Any]] | None,
    *,
    ha_devices: list[dict[str, Any]] | None = None,
    stale_after_days: int = DEFAULT_STALE_RESERVATION_DAYS,
    critical_integrations: set[str] | list[str] | None = None,
    now: datetime | None = None,
    inventory_stale: bool = False,
) -> dict[str, Any]:
    """Return one row per device and per reservation with no matching device."""
    stale_days = _bounded_int(
        stale_after_days,
        DEFAULT_STALE_RESERVATION_DAYS,
        MIN_STALE_RESERVATION_DAYS,
        MAX_STALE_RESERVATION_DAYS,
    )
    critical = (
        critical_integration_set(critical_integrations)
        if not isinstance(critical_integrations, set)
        else {item.lower() for item in critical_integrations}
    )
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stale_after = timedelta(days=stale_days)
    ha = _ha_by_mac(ha_devices, critical)

    by_mac: dict[str, dict[str, Any]] = {}
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        key = mac_key(device.get("mac"))
        if not key:
            continue
        current = by_mac.get(key)
        by_mac[key] = device if current is None else _prefer_device(current, device)

    reserved_by_mac: dict[str, dict[str, Any]] = {}
    for reservation in reservations or []:
        if not isinstance(reservation, dict):
            continue
        key = mac_key(reservation.get("mac"))
        if not key or key in reserved_by_mac:
            continue
        reserved_by_mac[key] = reservation

    rows: list[dict[str, Any]] = []
    for key in sorted(set(by_mac) | set(reserved_by_mac)):
        device = by_mac.get(key) or {}
        reservation = reserved_by_mac.get(key) or {}
        home = ha.get(key) or {}
        reserved_ip = ip_key(reservation.get("ip"))
        current_ip = current_ipv4(device) if device else ""
        connected = _as_bool(device.get("connected")) if device else False
        seen = parse_timestamp(device.get("last_active")) if device else None
        integrations = list(home.get("ha_integrations") or [])
        is_critical = any(str(domain).lower() in critical for domain in integrations)
        private = is_private_mac(key)
        nickname = _text(device.get("nickname")) if device else ""
        hostname = _text(device.get("hostname")) if device else ""
        ha_name = home.get("ha_device_name") or None
        description = _text(reservation.get("description"))
        name = nickname or _text(ha_name) or hostname or description or key
        rows.append(
            {
                "mac": key,
                "name": name,
                "hostname": hostname or None,
                "ip": current_ip or None,
                "reserved_ip": reserved_ip or None,
                "reservation_description": description or None,
                "status": _status(
                    reserved_ip=reserved_ip,
                    current_ip=current_ip,
                    connected=connected,
                    seen=seen,
                    now=moment,
                    stale_after=stale_after,
                ),
                "connected": connected,
                "last_active": seen.isoformat() if seen else None,
                "node": _device_node(device) if device else None,
                "connection_type": _connection_type(device) if device else None,
                "manufacturer": (_text(device.get("manufacturer")) or None) if device else None,
                "device_type": (_text(device.get("device_type")) or None) if device else None,
                "is_private_mac": private,
                "ha_device_id": home.get("ha_device_id"),
                "ha_device_name": ha_name,
                "ha_integrations": integrations,
                "critical": is_critical,
            }
        )

    rows.sort(key=lambda row: (row["name"].casefold(), row["mac"]))
    summary = _empty_summary(stale_days, inventory_stale)
    summary["devices"] = len(rows)
    for row in rows:
        if row["reserved_ip"]:
            summary["reservations"] += 1
        summary[row["status"]] += 1
        if row["critical"]:
            summary["critical"] += 1
            if not row["reserved_ip"]:
                summary["critical_unreserved"] += 1
        if row["is_private_mac"]:
            summary["private_mac"] += 1
            if row["reserved_ip"]:
                summary["private_mac_reserved"] += 1
        if row["connected"]:
            summary["online"] += 1
    return {"devices": rows, "summary": summary}


def _range_bounds(text: str) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address, ipaddress.IPv4Network | None]:
    if "/" in text:
        try:
            network = ipaddress.ip_network(text, strict=False)
        except (ipaddress.NetmaskValueError, ValueError) as err:
            raise ValueError(
                "Reservation range must be an IPv4 CIDR or a start-end range"
            ) from err
        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError("Reservation range must be IPv4")
        if network.num_addresses > _MAX_RANGE:
            raise ValueError(
                "Reservation range is too large; narrow it to at most 4096 addresses"
            )
        return network.network_address, network.broadcast_address, network
    if "-" not in text:
        raise ValueError("Reservation range must be an IPv4 CIDR or a start-end range")
    left, right = text.split("-", 1)
    try:
        start = ipaddress.IPv4Address(left.strip())
    except (ipaddress.AddressValueError, ValueError) as err:
        raise ValueError("Reservation range must be an IPv4 CIDR or a start-end range") from err
    end_text = right.strip()
    try:
        if end_text.isdigit() and "." not in end_text:
            octets = str(start).split(".")
            end = ipaddress.IPv4Address(".".join([*octets[:3], end_text]))
        else:
            end = ipaddress.IPv4Address(end_text)
    except (ipaddress.AddressValueError, ValueError) as err:
        raise ValueError("Reservation range must be an IPv4 CIDR or a start-end range") from err
    if int(end) < int(start):
        raise ValueError("Reservation range is reversed")
    if int(end) - int(start) + 1 > _MAX_RANGE:
        raise ValueError(
            "Reservation range is too large; narrow it to at most 4096 addresses"
        )
    return start, end, None


def infer_lan_subnet(gateway: str | None, extra_ips: list[str]) -> ipaddress.IPv4Network:
    """Return the /24 that contains the gateway, or the busiest private /24."""
    gateway_ip = ip_key(gateway)
    if gateway_ip:
        address = ipaddress.IPv4Address(gateway_ip)
        if address.is_private:
            return ipaddress.ip_network(f"{address}/24", strict=False)
    counts: dict[ipaddress.IPv4Network, int] = {}
    for value in extra_ips:
        parsed = ip_key(value)
        if not parsed:
            continue
        address = ipaddress.IPv4Address(parsed)
        if not address.is_private:
            continue
        network = ipaddress.ip_network(f"{address}/24", strict=False)
        counts[network] = counts.get(network, 0) + 1
    if not counts:
        raise ValueError(
            "Cannot determine the LAN subnet; set a reservation range in the options"
        )
    return max(counts, key=lambda network: (counts[network], str(network)))


def _largest_block(addresses: list[ipaddress.IPv4Address]) -> ipaddress.IPv4Address:
    ordered = sorted(addresses, key=int)
    best_start = ordered[0]
    best_length = 1
    run_start = ordered[0]
    run_length = 1
    for previous, current in zip(ordered, ordered[1:]):
        if int(current) == int(previous) + 1:
            run_length += 1
            continue
        if run_length > best_length:
            best_length = run_length
            best_start = run_start
        run_start = current
        run_length = 1
    if run_length > best_length:
        best_start = run_start
    return best_start


def suggest_free_ip(
    *,
    gateway: str | None = None,
    devices: list[dict[str, Any]] | None = None,
    reservations: list[dict[str, Any]] | None = None,
    node_ips: list[str] | None = None,
    reservation_range: str | None = None,
) -> str:
    """Pick a free IPv4 address from the largest contiguous free block.

    A candidate is rejected when it is reserved, currently leased or otherwise
    seen on a device, the gateway, an address ending in .1, or an eero node.
    """
    used: set[str] = set()
    observed: list[str] = []
    for device in devices or []:
        if isinstance(device, dict):
            seen = device_ips(device)
            used.update(seen)
            observed.extend(seen)
    for reservation in reservations or []:
        if isinstance(reservation, dict):
            parsed = ip_key(reservation.get("ip"))
            if parsed:
                used.add(parsed)
                observed.append(parsed)
    for value in node_ips or []:
        parsed = ip_key(value)
        if parsed:
            used.add(parsed)
            observed.append(parsed)
    gateway_ip = ip_key(gateway)
    if gateway_ip:
        used.add(gateway_ip)
        observed.append(gateway_ip)

    text = _text(reservation_range)
    if text:
        start, end, subnet = _range_bounds(text)
    else:
        subnet = infer_lan_subnet(gateway_ip, observed)
        start, end = subnet.network_address, subnet.broadcast_address

    free: list[ipaddress.IPv4Address] = []
    for number in range(int(start), int(end) + 1):
        address = ipaddress.IPv4Address(number)
        if subnet is not None and address in {
            subnet.network_address,
            subnet.broadcast_address,
        }:
            continue
        if address.packed[-1] == 1:
            continue
        if str(address) in used:
            continue
        free.append(address)
    if not free:
        raise ValueError("No free IP address is available in the reservation range")
    return str(_largest_block(free))
