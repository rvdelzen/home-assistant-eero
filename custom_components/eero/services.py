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
import logging
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
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
)
from homeassistant.helpers.selector import selector

from .api import EeroAuthException, EeroException
from .api.const import METHOD_DELETE, METHOD_GET, METHOD_POST, METHOD_PUT
from .const import (
    ATTR_CLIENT_PORT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DESCRIPTION,
    ATTR_DRY_RUN,
    ATTR_ENABLED,
    ATTR_FORCE,
    ATTR_FORWARD_ID,
    ATTR_GATEWAY_PORT,
    ATTR_IP,
    ATTR_MAC,
    ATTR_MACS,
    ATTR_NETWORK,
    ATTR_PREFIX_AREA,
    ATTR_PROTOCOL,
    ATTR_REFRESH,
    ATTR_RESERVATION_ID,
    ATTR_SUGGEST,
    ATTR_TARGETS,
    CONF_NETWORKS,
    DATA_API,
    DATA_COORDINATOR,
    DOMAIN,
    SERVICE_CREATE_PORT_FORWARD,
    SERVICE_CREATE_RESERVATION,
    SERVICE_DELETE_PORT_FORWARD,
    SERVICE_DELETE_RESERVATION,
    SERVICE_IP_TABLE,
    SERVICE_LIST_PORT_FORWARDS,
    SERVICE_LIST_RESERVATIONS,
    SERVICE_RELEASE_IP,
    SERVICE_RESERVE_IP,
    SERVICE_SYNC_NAMES,
    SERVICE_UPDATE_RESERVATION,
)
from .ip_table import (
    build_ip_table,
    current_ipv4,
    default_reservation_description,
    ip_table_settings,
    is_private_mac,
    iter_device_entries,
    mac_key,
    registry_mac_records,
    suggest_free_ip,
)
from .names import (
    PREFIX_NEVER,
    PREFIX_WHEN_MISSING,
    TARGET_NICKNAME,
    TARGET_RESERVATION,
    plan_name_sync,
)

_LOGGER = logging.getLogger(__name__)

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
    SERVICE_IP_TABLE,
    SERVICE_RESERVE_IP,
    SERVICE_RELEASE_IP,
    SERVICE_SYNC_NAMES,
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
_OPTIONAL_ENTRY_FIELDS = {
    vol.Optional(ATTR_CONFIG_ENTRY_ID): selector(
        {"config_entry": {"integration": DOMAIN}}
    ),
    vol.Optional(ATTR_NETWORK): cv.string,
}


def _optional_schema(extra: dict[Any, Any]) -> vol.Schema:
    """Build a schema whose config entry is optional for a single entry."""
    fields = dict(_OPTIONAL_ENTRY_FIELDS)
    fields.update(extra)
    return vol.Schema(fields)


IP_TABLE_SCHEMA = _optional_schema(
    {vol.Optional(ATTR_REFRESH, default=False): cv.boolean}
)
RESERVE_IP_SCHEMA = _optional_schema(
    {
        vol.Required(ATTR_MAC): cv.string,
        vol.Optional(ATTR_IP): cv.string,
        vol.Optional(ATTR_DESCRIPTION): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
        vol.Optional(ATTR_SUGGEST, default=False): cv.boolean,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)
RELEASE_IP_SCHEMA = _optional_schema(
    {
        vol.Required(ATTR_MAC): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)
SYNC_NAMES_SCHEMA = _schema(
    {
        vol.Optional(ATTR_DRY_RUN, default=True): cv.boolean,
        vol.Optional(
            ATTR_TARGETS, default=[TARGET_NICKNAME, TARGET_RESERVATION]
        ): vol.All(
            cv.ensure_list,
            [vol.In((TARGET_NICKNAME, TARGET_RESERVATION))],
            vol.Length(min=1),
        ),
        vol.Optional(ATTR_PREFIX_AREA, default=PREFIX_WHEN_MISSING): vol.In(
            (PREFIX_NEVER, PREFIX_WHEN_MISSING)
        ),
        vol.Optional(ATTR_MACS): vol.All(cv.ensure_list, [cv.string]),
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


def collect_ha_records(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return device-registry records keyed by a normalised connection MAC."""
    registry = dr.async_get(hass)

    def domain_for(entry_id: str) -> str | None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return None
        return entry.domain

    return registry_mac_records(iter_device_entries(registry), domain_for)


def _loaded_entry_ids(hass: HomeAssistant) -> list[str]:
    """Return ids of loaded eero config entries."""
    return [
        key
        for key, value in hass.data.get(DOMAIN, {}).items()
        if isinstance(value, dict) and DATA_API in value
    ]


def _entry_id_from_call(hass: HomeAssistant, call: ServiceCall) -> str:
    """Use the requested entry, or the only loaded entry."""
    requested = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if requested:
        return str(requested)
    loaded = _loaded_entry_ids(hass)
    if len(loaded) == 1:
        return loaded[0]
    if not loaded:
        raise ServiceValidationError("No Eero config entry is loaded")
    raise ServiceValidationError(
        "config_entry_id is required when more than one Eero entry is loaded"
    )


def _context_for(hass: HomeAssistant, call: ServiceCall) -> tuple[Any, dict[str, Any], Any, str]:
    """Return the entry, runtime, API client, and network id."""
    entry, runtime = _runtime(hass, _entry_id_from_call(hass, call))
    return entry, runtime, runtime[DATA_API], _network_id(entry, runtime, call.data.get(ATTR_NETWORK))


def _network_obj(runtime: dict[str, Any], network_id: str) -> Any:
    """Return the coordinator network object, if the poll has produced one."""
    coordinator = runtime.get(DATA_COORDINATOR)
    data = getattr(coordinator, "data", None)
    for network in getattr(data, "networks", []) or []:
        if getattr(network, "id", None) == network_id:
            return network
    return None


def _network_data(network: Any) -> dict[str, Any]:
    data = getattr(network, "data", None)
    return data if isinstance(data, dict) else {}


def _cached_inventory(network: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    data = _network_data(network)
    if "ip_devices" not in data or "ip_reservations" not in data:
        return None
    devices = data.get("ip_devices") or []
    reservations = data.get("ip_reservations") or []
    if not isinstance(devices, list) or not isinstance(reservations, list):
        return None
    return devices, reservations


def _devices_url(network: Any, network_id: str) -> str:
    resources = _network_data(network).get("resources") or {}
    url = resources.get("devices") if isinstance(resources, dict) else None
    if isinstance(url, str) and url.startswith("/"):
        return url
    return f"/2.2/networks/{_segment(network_id)}/devices"


def _node_ips(network: Any) -> list[str]:
    ips: list[str] = []
    for eero in (_network_data(network).get("eeros") or {}).get("data") or []:
        if not isinstance(eero, dict):
            continue
        for key in ("ip_address", "ip"):
            if eero.get(key):
                ips.append(str(eero[key]))
    return ips


def _gateway_ip(network: Any) -> str | None:
    data = _network_data(network)
    if data.get("gateway_ip"):
        return str(data["gateway_ip"])
    for eero in (data.get("eeros") or {}).get("data") or []:
        if isinstance(eero, dict) and eero.get("gateway") and eero.get("ip_address"):
            return str(eero["ip_address"])
    return None


def _store_inventory(
    runtime: dict[str, Any],
    network_id: str,
    devices: list[dict[str, Any]],
    reservations: list[dict[str, Any]],
) -> None:
    """Publish a snapshot on the coordinator network and the API cache."""
    network = _network_obj(runtime, network_id)
    if network is not None:
        if not isinstance(getattr(network, "data", None), dict):
            network.data = {}
        network.data["ip_devices"] = devices
        network.data["ip_reservations"] = reservations
    api = runtime.get(DATA_API)
    remember = getattr(api, "remember_ip_inventory", None)
    if remember is not None:
        remember(network_id, devices, reservations)
    coordinator = runtime.get(DATA_COORDINATOR)
    if (
        coordinator is not None
        and hasattr(coordinator, "async_set_updated_data")
        and getattr(coordinator, "data", None) is not None
    ):
        coordinator.async_set_updated_data(coordinator.data)


def _log_inventory_once(runtime: dict[str, Any], network_id: str, err: Exception) -> None:
    """Log one inventory failure per outage. The message has no session material."""
    logged = runtime.setdefault("_ip_inventory_logged", set())
    if network_id in logged:
        return
    logged.add(network_id)
    _LOGGER.warning(
        "Could not refresh devices and reservations for network %s; keeping the last good data (%s)",
        network_id,
        err,
    )


async def _fetch_inventory(
    hass: HomeAssistant,
    api: Any,
    network_id: str,
    devices_url: str,
    *,
    required: bool,
) -> dict[str, Any]:
    """Load devices and reservations. EeroAPI keeps the last good snapshot."""
    if hasattr(api, "fetch_ip_inventory"):

        def _run() -> dict[str, Any]:
            return api.fetch_ip_inventory(network_id, devices_url, required=required)

        try:
            return await hass.async_add_executor_job(_run)
        except EeroException as err:
            raise _api_error(err) from err
    devices = await _read(hass, api, devices_url)
    reservations = await _read(
        hass, api, _collection(network_id, "reservations")
    )
    return {"devices": devices, "reservations": reservations, "stale": False}


def _find_device(devices: list[dict[str, Any]], mac: str) -> dict[str, Any] | None:
    found = None
    for device in devices:
        if isinstance(device, dict) and mac_key(device.get("mac")) == mac:
            found = device
    return found


async def _async_create_reservation(
    hass: HomeAssistant,
    api: Any,
    network_id: str,
    *,
    mac: str,
    ip: str,
    description: str,
    dry_run: bool,
) -> ServiceResponse:
    """Create one reservation. Callers pass the same checks as the service."""
    mac = normalize_mac(mac)
    ip = normalize_ipv4(ip)
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
    after = [_reservation_view(item) for item in await _read(hass, api, url)]
    return _result(before, after, _expect_one("create", before, after), False)


async def _async_delete_reservation(
    hass: HomeAssistant,
    api: Any,
    network_id: str,
    *,
    reservation_id: str | None,
    mac: str | None,
    dry_run: bool,
) -> ServiceResponse:
    """Delete one reservation without deleting its port forwards."""
    if mac:
        mac = normalize_mac(mac)
    if reservation_id is None and mac is None:
        raise ServiceValidationError("Reservation id or MAC address is required")
    url = _collection(network_id, "reservations")
    records = await _read(hass, api, url)
    _item_raw, view = _match_reservation(
        records, reservation_id=reservation_id, mac=mac
    )
    before = [_reservation_view(record) for record in records]
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


async def _async_update_reservation(
    hass: HomeAssistant,
    api: Any,
    network_id: str,
    *,
    reservation_id: str | None,
    mac: str | None,
    ip: str | None,
    description: str | None,
    dry_run: bool,
) -> ServiceResponse:
    """Update one reservation. A missing IP or description is left unchanged."""
    if mac:
        mac = normalize_mac(mac)
    if reservation_id is None and mac is None:
        raise ServiceValidationError("Reservation id or MAC address is required")
    if ip is None and description is None:
        raise ServiceValidationError("A new IP address or description is required")
    new_ip = normalize_ipv4(ip) if ip is not None else None
    url = _collection(network_id, "reservations")
    records = await _read(hass, api, url)
    item, view = _match_reservation(records, reservation_id=reservation_id, mac=mac)
    resolved_ip = new_ip if new_ip is not None else view["ip"]
    resolved_description = description if description is not None else view["description"]
    if resolved_ip == view["ip"] and resolved_description == view["description"]:
        raise ServiceValidationError(
            "Reservation already has that IP address and description"
        )
    before = [_reservation_view(record) for record in records]
    _assert_reservation_free(
        before, ip=resolved_ip, mac=view["mac"], ignore_id=view["id"]
    )
    planned = {**view, "ip": resolved_ip, "description": resolved_description}
    if dry_run:
        after = [
            planned if current["id"] == view["id"] else current for current in before
        ]
        return _result(before, after, planned, True)
    body: dict[str, Any] = {
        "ip": resolved_ip,
        "mac": view["mac"],
        "description": resolved_description,
    }
    if item.get("public_static_ip") is not None:
        body["public_static_ip"] = item["public_static_ip"]
    await _write(
        hass,
        api,
        METHOD_PUT,
        _item(network_id, "reservations", view["id"]),
        json=body,
    )
    after = [_reservation_view(record) for record in await _read(hass, api, url)]
    return _result(before, after, _expect_one("update", before, after), False)


def collect_name_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return registry devices with the fields name matching needs."""
    registry = dr.async_get(hass)
    areas = ar.async_get(hass)
    devices: list[dict[str, Any]] = []
    for device in iter_device_entries(registry):
        area = areas.async_get_area(device.area_id) if device.area_id else None
        devices.append(
            {
                "id": device.id,
                "name": device.name,
                "name_by_user": device.name_by_user,
                "area_id": device.area_id,
                "area_name": area.name if area else None,
                "model": device.model,
                "connections": [tuple(item) for item in (device.connections or ())],
                "identifiers": [tuple(item) for item in (device.identifiers or ())],
            }
        )
    return devices


def _nickname_url(network_id: str, device_id: str) -> str:
    """Nickname writes only stick on the 2.3 devices resource."""
    return f"/2.3/networks/{_segment(network_id)}/devices/{_segment(device_id)}"


def _nickname_text(payload: Any) -> str | None:
    """Return a nickname from a device read-back, if the payload has one."""
    if isinstance(payload, list):
        for item in payload:
            found = _nickname_text(item)
            if found is not None:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    if "nickname" in payload:
        value = payload.get("nickname")
        return "" if value is None else str(value)
    data = payload.get("data")
    if isinstance(data, (dict, list)):
        return _nickname_text(data)
    return None


def _ha_name_for_mac(records: list[dict[str, Any]], mac: str) -> str | None:
    for record in records:
        if record.get("mac") == mac and record.get("ha_device_name"):
            return record["ha_device_name"]
    return None


async def _refresh_inventory(
    hass: HomeAssistant,
    runtime: dict[str, Any],
    api: Any,
    network_id: str,
    devices_url: str,
    *,
    fallback_devices: list[dict[str, Any]],
    fallback_reservations: list[dict[str, Any]] | None,
) -> None:
    """Refresh the inventory after a write. A failed refresh keeps the result."""
    try:
        fresh = await _fetch_inventory(
            hass, api, network_id, devices_url, required=False
        )
        _store_inventory(runtime, network_id, fresh["devices"], fresh["reservations"])
    except HomeAssistantError as err:
        _log_inventory_once(runtime, network_id, err)
        if fallback_reservations is not None:
            _store_inventory(
                runtime, network_id, fallback_devices, fallback_reservations
            )


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
        return await _async_create_reservation(
            hass,
            api,
            network_id,
            mac=call.data[ATTR_MAC],
            ip=call.data[ATTR_IP],
            description=call.data[ATTR_DESCRIPTION],
            dry_run=bool(call.data[ATTR_DRY_RUN]),
        )

    async def async_update_reservation(call: ServiceCall) -> ServiceResponse:
        """Update one DHCP reservation, or preview it."""
        _entry, api, network_id = _context(hass, call)
        return await _async_update_reservation(
            hass,
            api,
            network_id,
            reservation_id=_optional_id(call, ATTR_RESERVATION_ID),
            mac=call.data.get(ATTR_MAC),
            ip=call.data[ATTR_IP] if ATTR_IP in call.data else None,
            description=(
                call.data[ATTR_DESCRIPTION] if ATTR_DESCRIPTION in call.data else None
            ),
            dry_run=bool(call.data[ATTR_DRY_RUN]),
        )

    async def _inventory_for(
        call: ServiceCall, *, required: bool
    ) -> tuple[Any, dict[str, Any], Any, str, Any, dict[str, Any]]:
        entry, runtime, api, network_id = _context_for(hass, call)
        network = _network_obj(runtime, network_id)
        snapshot = await _fetch_inventory(
            hass,
            api,
            network_id,
            _devices_url(network, network_id),
            required=required,
        )
        return entry, runtime, api, network_id, network, snapshot

    async def async_ip_table(call: ServiceCall) -> ServiceResponse:
        """Return the merged device and reservation table."""
        entry, runtime, api, network_id = _context_for(hass, call)
        network = _network_obj(runtime, network_id)
        cached = _cached_inventory(network)
        refresh = bool(call.data.get(ATTR_REFRESH))
        if refresh or cached is None:
            snapshot = await _fetch_inventory(
                hass,
                api,
                network_id,
                _devices_url(network, network_id),
                required=refresh or cached is None,
            )
            devices = snapshot["devices"]
            reservations = snapshot["reservations"]
            stale = bool(snapshot.get("stale"))
            if not stale:
                _store_inventory(runtime, network_id, devices, reservations)
        else:
            devices, reservations = cached
            stale = bool(_network_data(network).get("ip_inventory_stale"))
        settings = ip_table_settings(entry.options, entry.data)
        table = build_ip_table(
            devices,
            reservations,
            ha_devices=collect_ha_records(hass),
            stale_after_days=settings["stale_days"],
            critical_integrations=settings["critical_integrations"],
            inventory_stale=stale,
        )
        return {
            **table,
            "network_id": network_id,
            "network_name": getattr(network, "name", None),
            "config_entry_id": entry.entry_id,
        }

    async def async_reserve_ip(call: ServiceCall) -> ServiceResponse:
        """Pin a device to an IP, or preview the reservation."""
        entry, runtime, api, network_id, network, snapshot = await _inventory_for(
            call, required=True
        )
        mac = normalize_mac(call.data[ATTR_MAC])
        if is_private_mac(mac) and not bool(call.data.get(ATTR_FORCE)):
            raise ServiceValidationError(
                "Refusing to reserve a private or randomized MAC address; pass force to override"
            )
        devices = snapshot["devices"]
        reservations = snapshot["reservations"]
        if not snapshot.get("stale"):
            _store_inventory(runtime, network_id, devices, reservations)
        device = _find_device(devices, mac)
        settings = ip_table_settings(entry.options, entry.data)
        provided = call.data.get(ATTR_IP) if ATTR_IP in call.data else None
        suggest = bool(call.data.get(ATTR_SUGGEST))
        current = current_ipv4(device) if device else ""
        if suggest or (not provided and not current):
            try:
                ip = suggest_free_ip(
                    gateway=_gateway_ip(network),
                    devices=devices,
                    reservations=reservations,
                    node_ips=_node_ips(network),
                    reservation_range=settings["reservation_range"],
                )
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
        elif provided:
            ip = normalize_ipv4(provided)
        else:
            ip = current
        if ATTR_DESCRIPTION in call.data and str(call.data[ATTR_DESCRIPTION]).strip():
            description = call.data[ATTR_DESCRIPTION]
        else:
            description = default_reservation_description(
                device, _ha_name_for_mac(collect_ha_records(hass), mac), mac
            )
        dry_run = bool(call.data.get(ATTR_DRY_RUN))
        result = await _async_create_reservation(
            hass,
            api,
            network_id,
            mac=mac,
            ip=ip,
            description=description,
            dry_run=dry_run,
        )
        if not dry_run and isinstance(result, dict):
            await _refresh_inventory(
                hass,
                runtime,
                api,
                network_id,
                _devices_url(network, network_id),
                fallback_devices=devices,
                fallback_reservations=result.get("after"),
            )
        return result

    async def async_release_ip(call: ServiceCall) -> ServiceResponse:
        """Delete the reservation for a MAC, or preview it."""
        _entry, runtime, api, network_id, network, snapshot = await _inventory_for(
            call, required=True
        )
        mac = normalize_mac(call.data[ATTR_MAC])
        dry_run = bool(call.data.get(ATTR_DRY_RUN))
        if not snapshot.get("stale"):
            _store_inventory(
                runtime, network_id, snapshot["devices"], snapshot["reservations"]
            )
        result = await _async_delete_reservation(
            hass,
            api,
            network_id,
            reservation_id=None,
            mac=mac,
            dry_run=dry_run,
        )
        if not dry_run and isinstance(result, dict):
            await _refresh_inventory(
                hass,
                runtime,
                api,
                network_id,
                _devices_url(network, network_id),
                fallback_devices=snapshot["devices"],
                fallback_reservations=result.get("after"),
            )
        return result

    async def _write_nickname(
        api: Any, network_id: str, device_id: str, nickname: str, mac: str
    ) -> None:
        """PUT a nickname on API 2.3 and confirm the read-back. Never retries."""
        url = _nickname_url(network_id, device_id)
        await _write(hass, api, METHOD_PUT, url, json={"nickname": nickname})
        payload = await _call(hass, api, METHOD_GET, url)
        confirmed = _nickname_text(payload)
        if confirmed != nickname:
            raise HomeAssistantError(
                f"Eero did not confirm the nickname for {mac}; no further requests were made"
            )

    async def async_sync_names(call: ServiceCall) -> ServiceResponse:
        """Copy Home Assistant device names onto eero. Writes are one-way."""
        entry, runtime, api, network_id, network, snapshot = await _inventory_for(
            call, required=True
        )
        if not snapshot.get("stale"):
            _store_inventory(
                runtime, network_id, snapshot["devices"], snapshot["reservations"]
            )
        dry_run = bool(call.data.get(ATTR_DRY_RUN, True))
        targets = list(
            call.data.get(ATTR_TARGETS) or [TARGET_NICKNAME, TARGET_RESERVATION]
        )
        prefix_area = call.data.get(ATTR_PREFIX_AREA, PREFIX_WHEN_MISSING)
        selected = None
        if ATTR_MACS in call.data and call.data[ATTR_MACS] is not None:
            selected = {normalize_mac(mac) for mac in call.data[ATTR_MACS]}
        planned = plan_name_sync(
            snapshot["devices"],
            snapshot["reservations"],
            collect_name_devices(hass),
            targets=targets,
            prefix_area=prefix_area,
            macs=selected,
        )
        changes: list[dict[str, Any]] = []
        for row in planned:
            public = {
                "mac": row["mac"],
                "current_nickname": row["current_nickname"],
                "current_description": row["current_description"],
                "proposed_name": row["proposed_name"],
                "action": row["action"],
                "result": (
                    "flagged"
                    if row["action"] == "flagged"
                    else "unchanged"
                    if row["action"] != "update"
                    else "dry_run"
                    if dry_run
                    else "updated"
                ),
                "match": row["match"],
                "ha_device_id": row["ha_device_id"],
                "reason": row["reason"],
                "fields": list(row["fields"]),
            }
            changes.append(public)
            if dry_run or row["action"] != "update":
                continue
            try:
                if TARGET_NICKNAME in row["fields"] and row["device_id"]:
                    await _write_nickname(
                        api,
                        network_id,
                        row["device_id"],
                        row["proposed_name"],
                        row["mac"],
                    )
                if TARGET_RESERVATION in row["fields"] and row["reservation_id"]:
                    await _async_update_reservation(
                        hass,
                        api,
                        network_id,
                        reservation_id=row["reservation_id"],
                        mac=row["mac"],
                        ip=None,
                        description=row["proposed_name"],
                        dry_run=False,
                    )
            except HomeAssistantError:
                public["result"] = "rejected"
                raise
        if not dry_run and any(row["action"] == "update" for row in planned):
            await _refresh_inventory(
                hass,
                runtime,
                api,
                network_id,
                _devices_url(network, network_id),
                fallback_devices=snapshot["devices"],
                fallback_reservations=None,
            )
        return {
            "dry_run": dry_run,
            "network_id": network_id,
            "config_entry_id": entry.entry_id,
            "changes": changes,
            "flagged": [row for row in changes if row["action"] == "flagged"],
        }

    async def async_delete_reservation(call: ServiceCall) -> ServiceResponse:
        """Delete one DHCP reservation without deleting its port forwards."""
        _entry, api, network_id = _context(hass, call)
        return await _async_delete_reservation(
            hass,
            api,
            network_id,
            reservation_id=_optional_id(call, ATTR_RESERVATION_ID),
            mac=call.data.get(ATTR_MAC),
            dry_run=bool(call.data[ATTR_DRY_RUN]),
        )

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
        (
            SERVICE_IP_TABLE,
            async_ip_table,
            IP_TABLE_SCHEMA,
            SupportsResponse.ONLY,
        ),
        (
            SERVICE_RESERVE_IP,
            async_reserve_ip,
            RESERVE_IP_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (
            SERVICE_RELEASE_IP,
            async_release_ip,
            RELEASE_IP_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        (
            SERVICE_SYNC_NAMES,
            async_sync_names,
            SYNC_NAMES_SCHEMA,
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
