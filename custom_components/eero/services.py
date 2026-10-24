"""DHCP reservation and port-forward services.

Reservation bodies use ``description``, ``ip``, and ``mac``. Updates also send
``public_static_ip`` when the current record has one, which is the fourth field
documented by eero-api ``src/eero/api/reservations.py``. Deletes always pass
``delete_forwards=false``.

Port-forward bodies use the six fields documented by eero-api
``src/eero/api/forwards.py``: ``client_port``, ``description``, ``enabled``,
``gateway_port``, ``ip``, and ``protocol``.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import quote

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import selector

from .api import EeroAuthException, EeroException
from .api.const import METHOD_DELETE, METHOD_GET, METHOD_POST, METHOD_PUT
from .const import (
    ATTR_CLIENT_PORT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DESCRIPTION,
    ATTR_DRY_RUN,
    ATTR_ENABLED,
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

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$|^[0-9A-Fa-f]{12}$")
_PROTOCOLS = {"tcp", "udp"}
_SERVICES = (
    SERVICE_LIST_RESERVATIONS,
    SERVICE_CREATE_RESERVATION,
    SERVICE_UPDATE_RESERVATION,
    SERVICE_DELETE_RESERVATION,
    SERVICE_LIST_PORT_FORWARDS,
    SERVICE_CREATE_PORT_FORWARD,
    SERVICE_DELETE_PORT_FORWARD,
)

_ENTRY_FIELDS = {
    vol.Required(ATTR_CONFIG_ENTRY_ID): selector(
        {"config_entry": {"integration": DOMAIN}}
    ),
    vol.Optional(ATTR_NETWORK): cv.string,
}


def _schema(extra: dict[Any, Any]) -> vol.Schema:
    """Build a service schema with the shared entry and network fields."""
    fields = dict(_ENTRY_FIELDS)
    fields.update(extra)
    return vol.Schema(fields)


LIST_RESERVATIONS_SCHEMA = _schema({})
CREATE_RESERVATION_SCHEMA = _schema(
    {
        vol.Required(ATTR_MAC): cv.string,
        vol.Required(ATTR_IP): cv.string,
        vol.Required(ATTR_DESCRIPTION): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)
UPDATE_RESERVATION_SCHEMA = _schema(
    {
        vol.Optional(ATTR_RESERVATION_ID): vol.Any(cv.string, int),
        vol.Optional(ATTR_MAC): cv.string,
        vol.Optional(ATTR_IP): cv.string,
        vol.Optional(ATTR_DESCRIPTION): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)
DELETE_RESERVATION_SCHEMA = _schema(
    {
        vol.Optional(ATTR_RESERVATION_ID): vol.Any(cv.string, int),
        vol.Optional(ATTR_MAC): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)
LIST_PORT_FORWARDS_SCHEMA = _schema({})
CREATE_PORT_FORWARD_SCHEMA = _schema(
    {
        vol.Required(ATTR_IP): cv.string,
        vol.Required(ATTR_CLIENT_PORT): vol.Any(cv.string, int),
        vol.Required(ATTR_GATEWAY_PORT): vol.Any(cv.string, int),
        vol.Required(ATTR_PROTOCOL): cv.string,
        vol.Optional(ATTR_DESCRIPTION, default=""): cv.string,
        vol.Optional(ATTR_ENABLED, default=True): cv.boolean,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)
DELETE_PORT_FORWARD_SCHEMA = _schema(
    {
        vol.Optional(ATTR_FORWARD_ID): vol.Any(cv.string, int),
        vol.Optional(ATTR_IP): cv.string,
        vol.Optional(ATTR_GATEWAY_PORT): vol.Any(cv.string, int),
        vol.Optional(ATTR_PROTOCOL): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)


def normalize_mac(value: Any) -> str:
    """Validate a MAC address and return lowercase colon-separated form."""
    text = str(value).strip()
    if not _MAC_RE.match(text):
        raise ServiceValidationError("Invalid MAC address")
    hexdigits = re.sub(r"[^0-9A-Fa-f]", "", text).lower()
    return ":".join(hexdigits[index : index + 2] for index in range(0, 12, 2))


def normalize_ipv4(value: Any) -> str:
    """Validate an IPv4 address."""
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except (ipaddress.AddressValueError, ValueError) as err:
        raise ServiceValidationError("Invalid IPv4 address") from err


def _maybe_mac(value: Any) -> str:
    """Normalize a MAC from the API, keeping the original text if it is unusual."""
    if not value:
        return ""
    try:
        return normalize_mac(value)
    except ServiceValidationError:
        return str(value).lower()


def _maybe_ip(value: Any) -> str:
    """Normalize an IPv4 address from the API."""
    if not value:
        return ""
    try:
        return normalize_ipv4(value)
    except ServiceValidationError:
        return str(value)


def _port(value: Any, label: str) -> int:
    """Validate a TCP/UDP port."""
    if isinstance(value, bool):
        raise ServiceValidationError(f"Invalid {label}")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as err:
        raise ServiceValidationError(f"Invalid {label}") from err
    if port < 1 or port > 65535:
        raise ServiceValidationError(f"Invalid {label}")
    return port


def _protocol(value: Any) -> str:
    """Validate a port-forward protocol."""
    text = str(value).strip().lower()
    if text not in _PROTOCOLS:
        raise ServiceValidationError("Protocol must be tcp or udp")
    return text


def _segment(value: Any) -> str:
    """Escape one URL path segment."""
    return quote(str(value), safe="")


def _collection(network_id: str, resource: str) -> str:
    """Return a network sub-resource path."""
    return f"/2.2/networks/{_segment(network_id)}/{resource}"


def _item(network_id: str, resource: str, item_id: str) -> str:
    """Return one reservation or port-forward path."""
    return f"{_collection(network_id, resource)}/{_segment(item_id)}"


def _record_id(item: dict[str, Any], label: str) -> str:
    """Return the record id, falling back to the last segment of its URL."""
    if item.get("id") not in (None, ""):
        return str(item["id"])
    url = str(item.get("url") or "").rstrip("/")
    if not url or "/" not in url:
        raise HomeAssistantError(f"Eero {label} response is missing an id")
    return url.rsplit("/", 1)[-1]


def _reservation_view(item: dict[str, Any]) -> dict[str, Any]:
    """Return the stable reservation shape exposed by the services."""
    return {
        "id": _record_id(item, "reservation"),
        "ip": _maybe_ip(item.get("ip") or ""),
        "mac": _maybe_mac(item.get("mac") or ""),
        "description": item.get("description") or "",
    }


def _forward_view(item: dict[str, Any]) -> dict[str, Any]:
    """Return the stable port-forward shape exposed by the services."""
    return {
        "id": _record_id(item, "port forward"),
        "ip": _maybe_ip(item.get("ip") or ""),
        "client_port": item.get("client_port"),
        "gateway_port": item.get("gateway_port"),
        "protocol": str(item.get("protocol") or "").lower(),
        "description": item.get("description") or "",
        "enabled": bool(item.get("enabled", True)),
    }


def _as_int(value: Any) -> int | None:
    """Return an int when the API value can be one."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _changes(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify list differences by id."""
    before_by_id = {item["id"]: item for item in before}
    after_by_id = {item["id"]: item for item in after}
    added = [after_by_id[key] for key in after_by_id if key not in before_by_id]
    removed = [before_by_id[key] for key in before_by_id if key not in after_by_id]
    updated = [
        after_by_id[key]
        for key in before_by_id
        if key in after_by_id and after_by_id[key] != before_by_id[key]
    ]
    return added, removed, updated


def _expect_one(
    kind: str, before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> dict[str, Any]:
    """Require the read-back to show exactly one create, update, or delete."""
    added, removed, updated = _changes(before, after)
    if kind == "create" and len(added) == 1 and not removed and not updated:
        return added[0]
    if kind == "delete" and len(removed) == 1 and not added and not updated:
        return removed[0]
    if kind == "update" and len(updated) == 1 and not added and not removed:
        return updated[0]
    raise HomeAssistantError(
        "Eero read-back did not show exactly one change; no further requests were made"
    )


def _result(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    changed: dict[str, Any],
    dry_run: bool,
) -> ServiceResponse:
    """Return the write-service payload."""
    return {
        "before": before,
        "after": after,
        "changed": changed,
        "dry_run": dry_run,
    }


def _runtime(hass: HomeAssistant, entry_id: str) -> tuple[Any, dict[str, Any]]:
    """Return a loaded eero config entry and its runtime data."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError("Config entry is not an eero entry")
    runtime = hass.data.get(DOMAIN, {}).get(entry_id)
    if not runtime or DATA_API not in runtime:
        raise ServiceValidationError("Eero config entry is not loaded")
    return entry, runtime


def _network_id(entry: Any, runtime: dict[str, Any], requested: str | None) -> str:
    """Resolve a network id, defaulting to the entry's first configured network."""
    configured = list(entry.options.get(CONF_NETWORKS, entry.data.get(CONF_NETWORKS, [])))
    if not configured:
        raise ServiceValidationError("No Eero network is configured for this entry")
    coordinator = runtime.get(DATA_COORDINATOR)
    data = getattr(coordinator, "data", None)
    networks = []
    if data is not None:
        networks = [
            network
            for network in getattr(data, "networks", [])
            if network is not None and getattr(network, "id", None) in configured
        ]
    by_id = {network.id: network for network in networks}
    if not requested:
        for network_id in configured:
            if network_id in by_id:
                return network_id
        return str(configured[0])
    requested = str(requested).strip()
    for network in networks:
        names = {
            network.id,
            getattr(network, "name", None),
            getattr(network, "name_unique", None),
        }
        if requested in names:
            return network.id
    if requested in configured or requested in {str(network_id) for network_id in configured}:
        return requested
    raise ServiceValidationError("Network was not found on this Eero entry")


def _context(hass: HomeAssistant, call: ServiceCall) -> tuple[Any, Any, str]:
    """Return the entry, API client, and network id for a service call."""
    entry, runtime = _runtime(hass, call.data[ATTR_CONFIG_ENTRY_ID])
    return entry, runtime[DATA_API], _network_id(entry, runtime, call.data.get(ATTR_NETWORK))


def _api_error(err: EeroException) -> HomeAssistantError:
    """Convert an API error into a Home Assistant error without retrying."""
    if isinstance(err, EeroAuthException):
        return HomeAssistantError("Eero session was rejected")
    detail = err.error or err.message or "request failed"
    return HomeAssistantError(f"Eero API request failed: {detail}")


async def _call(
    hass: HomeAssistant, api: Any, method: str, url: str, **kwargs: Any
) -> Any:
    """Perform one API call. The caller decides whether refresh is allowed."""

    def _invoke() -> Any:
        return api.call(method, url, **kwargs)

    try:
        return await hass.async_add_executor_job(_invoke)
    except EeroException as err:
        raise _api_error(err) from err


async def _read(hass: HomeAssistant, api: Any, url: str) -> list[dict[str, Any]]:
    """GET a collection once."""
    payload = await _call(hass, api, METHOD_GET, url)
    if payload is None:
        return []
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise HomeAssistantError("Unexpected list response from Eero")
    return payload


async def _write(
    hass: HomeAssistant, api: Any, method: str, url: str, **kwargs: Any
) -> None:
    """Perform one write. A failure is returned immediately and is not retried."""
    kwargs["allow_refresh"] = False
    await _call(hass, api, method, url, **kwargs)


def _assert_reservation_free(
    views: list[dict[str, Any]], *, ip: str, mac: str, ignore_id: str | None
) -> None:
    """Refuse when another reservation already uses the IP address or MAC."""
    for view in views:
        if ignore_id is not None and view["id"] == ignore_id:
            continue
        if ip and view["ip"] == ip:
            raise ServiceValidationError(
                f"IP address {ip} is already used by another reservation"
            )
        if mac and view["mac"] == mac:
            raise ServiceValidationError(
                f"MAC address {mac} is already used by another reservation"
            )


def _match_reservation(
    records: list[dict[str, Any]], *, reservation_id: str | None, mac: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Find exactly one reservation by id, MAC, or both."""
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in records:
        view = _reservation_view(item)
        id_match = reservation_id is not None and view["id"] == reservation_id
        mac_match = mac is not None and view["mac"] == mac
        if reservation_id is not None and mac is not None:
            if id_match and mac_match:
                matches.append((item, view))
            elif id_match or mac_match:
                raise ServiceValidationError(
                    "Reservation id and MAC address refer to different reservations"
                )
        elif id_match or mac_match:
            matches.append((item, view))
    if not matches:
        raise ServiceValidationError("Reservation was not found")
    if len(matches) > 1:
        raise ServiceValidationError("More than one reservation matches")
    return matches[0]


def _assert_forward_free(
    views: list[dict[str, Any]],
    *,
    ip: str,
    client_port: int,
    gateway_port: int,
    protocol: str,
) -> None:
    """Refuse when the external or internal port binding is already taken."""
    for view in views:
        same_external = (
            _as_int(view.get("gateway_port")) == gateway_port
            and view.get("protocol") == protocol
        )
        same_internal = (
            view.get("ip") == ip
            and _as_int(view.get("client_port")) == client_port
            and view.get("protocol") == protocol
        )
        if same_external or same_internal:
            raise ServiceValidationError(
                "A port forward already uses that address, port, and protocol"
            )


def _match_forward(
    records: list[dict[str, Any]],
    *,
    forward_id: str | None,
    ip: str | None,
    gateway_port: int | None,
    protocol: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Find exactly one port forward by id or by ip, gateway port, and protocol."""
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in records:
        view = _forward_view(item)
        if forward_id is not None:
            if view["id"] != forward_id:
                continue
            if ip is not None and view["ip"] != ip:
                raise ServiceValidationError(
                    "Port forward id does not match the IP address"
                )
            if gateway_port is not None and _as_int(view.get("gateway_port")) != gateway_port:
                raise ServiceValidationError(
                    "Port forward id does not match the gateway port"
                )
            if protocol is not None and view.get("protocol") != protocol:
                raise ServiceValidationError(
                    "Port forward id does not match the protocol"
                )
            matches.append((item, view))
            continue
        if (
            view["ip"] == ip
            and _as_int(view.get("gateway_port")) == gateway_port
            and view.get("protocol") == protocol
        ):
            matches.append((item, view))
    if not matches:
        raise ServiceValidationError("Port forward was not found")
    if len(matches) > 1:
        raise ServiceValidationError("More than one port forward matches")
    return matches[0]


def _optional_id(call: ServiceCall, key: str) -> str | None:
    """Return a stripped id when the service field was provided."""
    if key not in call.data or call.data[key] in (None, ""):
        return None
    return str(call.data[key]).strip() or None


def async_register_services(hass: HomeAssistant) -> None:
    """Register eero services once. Handlers select the entry from the call."""
    if hass.services.has_service(DOMAIN, SERVICE_LIST_RESERVATIONS):
        return

    async def async_list_reservations(call: ServiceCall) -> ServiceResponse:
        """Return DHCP reservations for one network."""
        _entry, api, network_id = _context(hass, call)
        records = await _read(hass, api, _collection(network_id, "reservations"))
        return {"reservations": [_reservation_view(item) for item in records]}

    async def async_create_reservation(call: ServiceCall) -> ServiceResponse:
        """Create one DHCP reservation, or preview it."""
        _entry, api, network_id = _context(hass, call)
        mac = normalize_mac(call.data[ATTR_MAC])
        ip = normalize_ipv4(call.data[ATTR_IP])
        description = call.data[ATTR_DESCRIPTION]
        dry_run = bool(call.data[ATTR_DRY_RUN])
        url = _collection(network_id, "reservations")
        records = await _read(hass, api, url)
        before = [_reservation_view(item) for item in records]
        _assert_reservation_free(before, ip=ip, mac=mac, ignore_id=None)
        planned = {"id": None, "ip": ip, "mac": mac, "description": description}
        if dry_run:
            return _result(before, [*before, planned], planned, True)
        await _write(
            hass,
            api,
            METHOD_POST,
            url,
            json={"ip": ip, "mac": mac, "description": description},
        )
        after = [
            _reservation_view(item) for item in await _read(hass, api, url)
        ]
        return _result(before, after, _expect_one("create", before, after), False)

    async def async_update_reservation(call: ServiceCall) -> ServiceResponse:
        """Update one DHCP reservation, or preview it."""
        _entry, api, network_id = _context(hass, call)
        reservation_id = _optional_id(call, ATTR_RESERVATION_ID)
        mac = normalize_mac(call.data[ATTR_MAC]) if call.data.get(ATTR_MAC) else None
        if reservation_id is None and mac is None:
            raise ServiceValidationError("Reservation id or MAC address is required")
        has_ip = ATTR_IP in call.data
        has_description = ATTR_DESCRIPTION in call.data
        if not has_ip and not has_description:
            raise ServiceValidationError("A new IP address or description is required")
        new_ip = normalize_ipv4(call.data[ATTR_IP]) if has_ip else None
        url = _collection(network_id, "reservations")
        records = await _read(hass, api, url)
        item, view = _match_reservation(
            records, reservation_id=reservation_id, mac=mac
        )
        ip = new_ip if new_ip is not None else view["ip"]
        description = (
            call.data[ATTR_DESCRIPTION] if has_description else view["description"]
        )
        if ip == view["ip"] and description == view["description"]:
            raise ServiceValidationError(
                "Reservation already has that IP address and description"
            )
        before = [_reservation_view(record) for record in records]
        _assert_reservation_free(before, ip=ip, mac=view["mac"], ignore_id=view["id"])
        planned = {**view, "ip": ip, "description": description}
        dry_run = bool(call.data[ATTR_DRY_RUN])
        if dry_run:
            after = [
                planned if current["id"] == view["id"] else current for current in before
            ]
            return _result(before, after, planned, True)
        body: dict[str, Any] = {
            "ip": ip,
            "mac": view["mac"],
            "description": description,
        }
        if item.get("public_static_ip") is not None:
            body["public_static_ip"] = item["public_static_ip"]
        await _write(hass, api, METHOD_PUT, _item(network_id, "reservations", view["id"]), json=body)
        after = [_reservation_view(record) for record in await _read(hass, api, url)]
        return _result(before, after, _expect_one("update", before, after), False)

    async def async_delete_reservation(call: ServiceCall) -> ServiceResponse:
        """Delete one DHCP reservation without deleting its port forwards."""
        _entry, api, network_id = _context(hass, call)
        reservation_id = _optional_id(call, ATTR_RESERVATION_ID)
        mac = normalize_mac(call.data[ATTR_MAC]) if call.data.get(ATTR_MAC) else None
        if reservation_id is None and mac is None:
            raise ServiceValidationError("Reservation id or MAC address is required")
        url = _collection(network_id, "reservations")
        records = await _read(hass, api, url)
        _item_raw, view = _match_reservation(
            records, reservation_id=reservation_id, mac=mac
        )
        before = [_reservation_view(record) for record in records]
        dry_run = bool(call.data[ATTR_DRY_RUN])
        if dry_run:
            after = [current for current in before if current["id"] != view["id"]]
            return _result(before, after, view, True)
        await _write(
            hass,
            api,
            METHOD_DELETE,
            _item(network_id, "reservations", view["id"]),
            params={"delete_forwards": "false"},
        )
        after = [_reservation_view(record) for record in await _read(hass, api, url)]
        return _result(before, after, _expect_one("delete", before, after), False)

    async def async_list_port_forwards(call: ServiceCall) -> ServiceResponse:
        """Return port forwards for one network."""
        _entry, api, network_id = _context(hass, call)
        records = await _read(hass, api, _collection(network_id, "forwards"))
        return {"forwards": [_forward_view(item) for item in records]}

    async def async_create_port_forward(call: ServiceCall) -> ServiceResponse:
        """Create one port forward, or preview it."""
        _entry, api, network_id = _context(hass, call)
        ip = normalize_ipv4(call.data[ATTR_IP])
        client_port = _port(call.data[ATTR_CLIENT_PORT], "client port")
        gateway_port = _port(call.data[ATTR_GATEWAY_PORT], "gateway port")
        protocol = _protocol(call.data[ATTR_PROTOCOL])
        description = call.data.get(ATTR_DESCRIPTION, "")
        enabled = bool(call.data.get(ATTR_ENABLED, True))
        dry_run = bool(call.data[ATTR_DRY_RUN])
        url = _collection(network_id, "forwards")
        records = await _read(hass, api, url)
        before = [_forward_view(item) for item in records]
        _assert_forward_free(
            before,
            ip=ip,
            client_port=client_port,
            gateway_port=gateway_port,
            protocol=protocol,
        )
        planned = {
            "id": None,
            "ip": ip,
            "client_port": client_port,
            "gateway_port": gateway_port,
            "protocol": protocol,
            "description": description,
            "enabled": enabled,
        }
        if dry_run:
            return _result(before, [*before, planned], planned, True)
        await _write(
            hass,
            api,
            METHOD_POST,
            url,
            json={
                "ip": ip,
                "client_port": client_port,
                "gateway_port": gateway_port,
                "protocol": protocol,
                "description": description,
                "enabled": enabled,
            },
        )
        after = [_forward_view(item) for item in await _read(hass, api, url)]
        return _result(before, after, _expect_one("create", before, after), False)

    async def async_delete_port_forward(call: ServiceCall) -> ServiceResponse:
        """Delete one port forward, or preview it."""
        _entry, api, network_id = _context(hass, call)
        forward_id = _optional_id(call, ATTR_FORWARD_ID)
        ip = normalize_ipv4(call.data[ATTR_IP]) if call.data.get(ATTR_IP) else None
        gateway_port = (
            _port(call.data[ATTR_GATEWAY_PORT], "gateway port")
            if ATTR_GATEWAY_PORT in call.data and call.data[ATTR_GATEWAY_PORT] not in (None, "")
            else None
        )
        protocol = (
            _protocol(call.data[ATTR_PROTOCOL]) if call.data.get(ATTR_PROTOCOL) else None
        )
        if forward_id is None and not (ip and gateway_port and protocol):
            raise ServiceValidationError(
                "Port forward id, or IP address, gateway port, and protocol, is required"
            )
        url = _collection(network_id, "forwards")
        records = await _read(hass, api, url)
        _item_raw, view = _match_forward(
            records,
            forward_id=forward_id,
            ip=ip,
            gateway_port=gateway_port,
            protocol=protocol,
        )
        before = [_forward_view(item) for item in records]
        dry_run = bool(call.data[ATTR_DRY_RUN])
        if dry_run:
            after = [current for current in before if current["id"] != view["id"]]
            return _result(before, after, view, True)
        await _write(
            hass, api, METHOD_DELETE, _item(network_id, "forwards", view["id"])
        )
        after = [_forward_view(item) for item in await _read(hass, api, url)]
        return _result(before, after, _expect_one("delete", before, after), False)

    registrations = (
        (
            SERVICE_LIST_RESERVATIONS,
            async_list_reservations,
            LIST_RESERVATIONS_SCHEMA,
            SupportsResponse.ONLY,
        ),
        (
            SERVICE_CREATE_RESERVATION,
            async_create_reservation,
            CREATE_RESERVATION_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (
            SERVICE_UPDATE_RESERVATION,
            async_update_reservation,
            UPDATE_RESERVATION_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (
            SERVICE_DELETE_RESERVATION,
            async_delete_reservation,
            DELETE_RESERVATION_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (
            SERVICE_LIST_PORT_FORWARDS,
            async_list_port_forwards,
            LIST_PORT_FORWARDS_SCHEMA,
            SupportsResponse.ONLY,
        ),
        (
            SERVICE_CREATE_PORT_FORWARD,
            async_create_port_forward,
            CREATE_PORT_FORWARD_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (
            SERVICE_DELETE_PORT_FORWARD,
            async_delete_port_forward,
            DELETE_PORT_FORWARD_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
    )
    for name, handler, schema, response in registrations:
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=schema,
            supports_response=response,
        )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove eero services after the last config entry unloads."""
    for name in _SERVICES:
        if hass.services.has_service(DOMAIN, name):
            hass.services.async_remove(DOMAIN, name)
