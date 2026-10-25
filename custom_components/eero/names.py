"""Match Home Assistant devices to Eero MACs and build one-way names.

Nothing in this module calls the Eero API or writes to Home Assistant.
"""

from __future__ import annotations

import re
from typing import Any

from .ip_table import is_private_mac, mac_key

PREFIX_NEVER = "never"
PREFIX_WHEN_MISSING = "when_missing"
TARGET_NICKNAME = "nickname"
TARGET_RESERVATION = "reservation"

REASON_UNMATCHED = "unmatched"
REASON_PRIVATE_MAC = "private_mac"
REASON_UNNAMED = "unnamed"
REASON_DUPLICATE = "duplicate_name"

_MAC_SEPARATED = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
_MAC_BARE = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{12})(?![0-9A-Fa-f])")


def macs_embedded(value: Any) -> list[str]:
    """Return MACs written inside an identifier, colon-normalised."""
    text = "" if value is None else str(value)
    found: list[str] = []
    for match in _MAC_SEPARATED.finditer(text):
        key = mac_key(match.group(0))
        if key and key not in found:
            found.append(key)
    without_separated = _MAC_SEPARATED.sub(" ", text)
    for match in _MAC_BARE.finditer(without_separated):
        key = mac_key(match.group(1))
        if key and key not in found:
            found.append(key)
    return found


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _connection_macs(device: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for connection in device.get("connections") or []:
        if not isinstance(connection, (tuple, list)) or len(connection) != 2:
            continue
        if str(connection[0]).lower() != "mac":
            continue
        key = mac_key(connection[1])
        if key and key not in found:
            found.append(key)
    return found


def _identifier_pairs(device: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for identifier in device.get("identifiers") or []:
        if not isinstance(identifier, (tuple, list)) or len(identifier) != 2:
            continue
        pairs.append((str(identifier[0]), str(identifier[1])))
    return pairs


def _identifier_macs(device: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for _domain, identifier in _identifier_pairs(device):
        for key in macs_embedded(identifier):
            if key not in found:
                found.append(key)
    return found


def _customized(device: dict[str, Any]) -> bool:
    return bool(_text(device.get("name_by_user")) or device.get("area_id"))


def match_ha_device(mac: str, devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the HA device for an Eero MAC.

    Order: registry connections, then a MAC embedded in identifiers, then a
    sibling that shares an identifier. A sibling with a user-given name or an
    area wins over a seed that has neither.
    """
    key = mac_key(mac)
    if not key:
        return None
    connection = [device for device in devices if key in _connection_macs(device)]
    embedded = [device for device in devices if key in _identifier_macs(device)]
    if connection:
        seeds = connection
        seed_tier = "connection"
    elif embedded:
        seeds = embedded
        seed_tier = "identifier"
    else:
        return None

    seed_ids = {id(device) for device in seeds}
    shared = {pair for device in seeds for pair in _identifier_pairs(device)}
    siblings: list[dict[str, Any]] = []
    if shared:
        for device in devices:
            if id(device) in seed_ids:
                continue
            if shared.intersection(_identifier_pairs(device)):
                siblings.append(device)

    def score(item: tuple[dict[str, Any], bool]) -> tuple[int, int, int]:
        device, is_seed = item
        return (
            1 if _customized(device) else 0,
            1 if _text(device.get("name_by_user")) else 0,
            1 if is_seed else 0,
        )

    pool = [(device, True) for device in seeds] + [(device, False) for device in siblings]
    best, is_seed = max(pool, key=score)
    return {"device": best, "tier": seed_tier if is_seed else "sibling"}


def ha_display_name(device: dict[str, Any] | None) -> str:
    """Return name_by_user, otherwise the device name."""
    if not device:
        return ""
    return _text(device.get("name_by_user")) or _text(device.get("name"))


def proposed_name(
    *,
    ha_name: str,
    area_name: str | None,
    model: str | None,
    prefix_area: str,
) -> str:
    """Apply the area prefix rule to an HA device name."""
    name = _text(ha_name)
    if prefix_area != PREFIX_WHEN_MISSING:
        return name
    area = _text(area_name)
    if not area:
        return name
    if name.casefold() == area.casefold():
        model_text = _text(model)
        if (
            model_text
            and model_text.casefold() != area.casefold()
            and model_text.casefold() not in name.casefold()
        ):
            return f"{area} {model_text}"
        return name
    if area.casefold() in name.casefold():
        return name
    return f"{area} {name}"


def eero_device_id(device: dict[str, Any] | None) -> str:
    """Return the id eero expects on the devices URL, or a blank string."""
    if not device:
        return ""
    url = str(device.get("url") or "")
    marker = "/devices/"
    if marker in url:
        segment = url.split(marker, 1)[1].split("?", 1)[0].strip("/")
        if segment:
            return segment.split("/")[0]
    for key in ("id", "mac"):
        if device.get(key) not in (None, ""):
            return str(device[key])
    return ""


def reservation_id(item: dict[str, Any] | None) -> str | None:
    """Return a reservation id from the record or the last URL segment."""
    if not item:
        return None
    if item.get("id") not in (None, ""):
        return str(item["id"])
    url = str(item.get("url") or "").rstrip("/")
    if "/reservations/" in url:
        return url.rsplit("/", 1)[-1] or None
    return None


def plan_name_sync(
    devices: list[dict[str, Any]] | None,
    reservations: list[dict[str, Any]] | None,
    ha_devices: list[dict[str, Any]] | None,
    *,
    targets: list[str],
    prefix_area: str,
    macs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Plan one-way HA to eero name changes. This function does not write.

    Private MACs, unmatched devices, unnamed HA devices, and duplicate
    proposed names are flagged and have no fields to write.
    """
    wanted = {target for target in targets if target in {TARGET_NICKNAME, TARGET_RESERVATION}}
    by_device: dict[str, dict[str, Any]] = {}
    for device in devices or []:
        if isinstance(device, dict):
            key = mac_key(device.get("mac"))
            if key:
                by_device[key] = device
    by_reservation: dict[str, dict[str, Any]] = {}
    for reservation in reservations or []:
        if isinstance(reservation, dict):
            key = mac_key(reservation.get("mac"))
            if key and key not in by_reservation:
                by_reservation[key] = reservation
    if macs is None:
        keys = set(by_device) | set(by_reservation)
    else:
        keys = {key for key in (mac_key(mac) for mac in macs) if key}

    home = ha_devices or []
    rows: list[dict[str, Any]] = []
    for key in sorted(keys):
        device = by_device.get(key)
        reservation = by_reservation.get(key)
        matched = match_ha_device(key, home)
        ha_device = matched["device"] if matched else None
        raw_name = ha_display_name(ha_device)
        proposed = ""
        if raw_name:
            proposed = proposed_name(
                ha_name=raw_name,
                area_name=ha_device.get("area_name") if ha_device else None,
                model=ha_device.get("model") if ha_device else None,
                prefix_area=prefix_area,
            )
        if is_private_mac(key):
            reason = REASON_PRIVATE_MAC
        elif matched is None:
            reason = REASON_UNMATCHED
        elif not proposed:
            reason = REASON_UNNAMED
        else:
            reason = None
        nickname = _text(device.get("nickname")) if device else ""
        description = _text(reservation.get("description")) if reservation else ""
        rows.append(
            {
                "mac": key,
                "current_nickname": nickname or None,
                "current_description": description or None,
                "proposed_name": proposed or None,
                "action": "flagged" if reason else "unchanged",
                "match": matched["tier"] if matched else None,
                "ha_device_id": ha_device.get("id") if ha_device else None,
                "reason": reason,
                "fields": [],
                "device_id": eero_device_id(device) or None,
                "reservation_id": reservation_id(reservation),
                "_nickname": nickname,
                "_description": description,
                "_proposed": proposed,
            }
        )

    open_rows = [row for row in rows if row["reason"] is None]
    collisions = duplicate_names(
        {row["mac"]: row["_proposed"] for row in open_rows}
    )
    for row in open_rows:
        if row["mac"] in collisions:
            row["reason"] = REASON_DUPLICATE
            row["action"] = "flagged"
            continue
        fields: list[str] = []
        if (
            TARGET_NICKNAME in wanted
            and row["device_id"]
            and row["_proposed"] != row["_nickname"]
        ):
            fields.append(TARGET_NICKNAME)
        if (
            TARGET_RESERVATION in wanted
            and row["reservation_id"]
            and row["_proposed"] != row["_description"]
        ):
            fields.append(TARGET_RESERVATION)
        row["fields"] = fields
        row["action"] = "update" if fields else "unchanged"
    for row in rows:
        row.pop("_nickname", None)
        row.pop("_description", None)
        row.pop("_proposed", None)
    return rows


def duplicate_names(names: dict[str, str]) -> set[str]:
    """Return MACs whose proposed name collides with another MAC."""
    grouped: dict[str, list[str]] = {}
    for mac, name in names.items():
        key = name.casefold()
        if not key:
            continue
        grouped.setdefault(key, []).append(mac)
    collisions: set[str] = set()
    for macs in grouped.values():
        if len(macs) > 1:
            collisions.update(macs)
    return collisions
