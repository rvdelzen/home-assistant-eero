[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
# Eero Home Assistant Integration
Custom component to allow control of Eero networks in [Home Assistant](https://home-assistant.io).

## Credit
- [@343max's eero-client project](https://github.com/343max/eero-client) - Basic API auth and refresh methods
- [@jrlucier's eero_tracker project](https://github.com/jrlucier/eero_tracker) - Initial Home Assistant idea

## Install
1. Ensure Home Assistant is updated to version 2025.2.0 or newer.
2. Use HACS and add as a [custom repo](https://hacs.xyz/docs/faq/custom_repositories); or download and manually move to the `custom_components` folder.
3. Once the integration is installed follow the standard process to setup via UI and search for `eero`.
4. Follow the prompts.

## Options
- Networks, resources, and activity metrics can be updated via integration options.
- The inclusion method for clients can be toggled between whitelisting (include only selected clients) or blacklisting (exclude only selected clients).
- If `Advanced Mode` is enabled for the current profile, additional options are available (interval, timeout, and response logging).

## Notes
- This integration does not support login via Amazon account. A workaround is to create a new account without Amazon login and add that account as another network admin. Refer to this [post](https://github.com/schmittx/home-assistant-eero/issues/77#issuecomment-1960875926) for step-by-step instructions.

## Currently Working
- Multiple networks supported
- Control network properties (ex. guest network, Eero Plus features, Eero Labs features)
- Pause access for profiles and/or clients
- Control content filters for profiles
- Device tracker entities for clients and profiles
- Sensors for various metrics
- Button entities to control features that require network restarts
- Select and time entities to control nightlight features for Eero Beacon devices
- Image entities to display QR code for joining main network and guest network
- Sensors for activity data (requires Eero Plus subscription)
- Set blocked apps for profiles (requires Eero Plus subscription)
- Update entities for Eero device firmware management
- Control backup networks (requires Eero Plus subscription)
- List, create, update, and delete DHCP reservations
- List, create, and delete port forwards
- IP reservation table, pin and release actions, and a bundled Lovelace card
- One-way name sync from Home Assistant to eero nicknames and reservation descriptions
- Switch the Eero account on an existing config entry (reconfigure) and sign in again when the session expires (reauthentication)

## Services
Service responses are JSON objects. In Developer Tools, enable **return response** on the action. In a script or automation, set `response_variable`.

`config_entry_id` is the id of the Eero config entry (Settings → Devices & services → Eero → the entry menu shows it, and the config-entry selector lists the entry). `network` is optional and accepts a network id or name. When it is omitted, the entry's only network is used, or the first configured network when there are several.

Write actions read the current list, change one record, then read the list again. They return `before`, `after`, `changed`, and `dry_run`. A conflicting IP address or MAC address, or a missing target, is refused before anything is written. `dry_run: true` returns the planned change and does not call the write endpoint. A failed write is not retried. Deleting a reservation always sends `delete_forwards=false`, so port forwards are kept.

### List DHCP reservations

```yaml
action: eero.list_reservations
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
response_variable: reservations
```

`reservations.reservations` is a list of `{id, ip, mac, description}`.

### Create a reservation

```yaml
action: eero.create_reservation
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  mac: "aa:bb:cc:dd:ee:ff"
  ip: "192.168.4.50"
  description: Printer
  dry_run: true
response_variable: reservation
```

Set `dry_run` to `false` to create it. Update with `eero.update_reservation` (`reservation_id` or `mac`, plus a new `ip` and/or `description`). Delete with `eero.delete_reservation` (`reservation_id` or `mac`).

### List port forwards

```yaml
action: eero.list_port_forwards
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
response_variable: forwards
```

Each item has `id`, `ip`, `client_port`, `gateway_port`, `protocol` (`tcp` or `udp`), `description`, and `enabled`.

### Create a port forward

```yaml
action: eero.create_port_forward
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  ip: "192.168.4.50"
  client_port: 80
  gateway_port: 8080
  protocol: tcp
  description: Camera
  enabled: true
  dry_run: true
response_variable: forward
```

Delete with `eero.delete_port_forward`, passing `forward_id` or `ip`, `gateway_port`, and `protocol`.

### IP table

Each poll also requests `GET /2.2/networks/{id}/devices` (skipped when that poll already fetched devices for client entities) and `GET /2.2/networks/{id}/reservations`. This is independent of which clients are selected, and it does not create a Home Assistant entity per client. Advanced options can poll this inventory every Nth update (default every poll), choose how many days an offline reservation stays current before it is stale (default 30), limit the free-address search to a CIDR or start-end range, and list the integrations that mark a device as critical.

`eero.ip_table` returns one row per device and per reservation whose MAC is not in the device list. `config_entry_id` is optional when a single Eero entry is loaded. `refresh: true` fetches now; otherwise the last poll is returned. A failed refresh keeps the last good snapshot and is logged once per outage.

```yaml
action: eero.ip_table
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
response_variable: ip_table
```

`eero.reserve_ip` pins `mac` to `ip`, or to the device's current address when `ip` is omitted. With no current address, or with `suggest: true`, it picks the first address of the largest free block, skipping reservations, current leases, the gateway, addresses ending in `.1`, and eero nodes. The description defaults to the Home Assistant device name, then the eero nickname, then the hostname. A private or randomized MAC is refused unless `force: true`. `eero.release_ip` deletes that MAC's reservation and keeps port forwards. Both accept `dry_run`.

The sensor `sensor.<network>_ip_reservations` reports the number of reservations. Its attributes are only `fixed`, `dynamic`, `mismatch`, `stale`, `critical_unreserved`, and `private_mac_reserved`.

The card is served by the integration (no separate HACS frontend). Add it with:

```yaml
type: custom:eero-ip-table-card
config_entry_id: YOUR_CONFIG_ENTRY_ID
filters:
  critical: false
  status: ""
```

It loads the table on demand and again every 60 seconds from the cached poll. Refresh, pin, and release talk to eero and then reload. Pin uses the current address. Release asks for confirmation first. Critical devices without a reservation are marked in red. The card is served at `/eero/ip-table-card.js`. A failed call shows the error and leaves the last table on screen.

### Sync names

`eero.sync_names` copies Home Assistant names onto eero. It never writes back to Home Assistant. `dry_run` defaults to true. `targets` defaults to `nickname` and `reservation`. `prefix_area` is `when_missing` or `never` (default `when_missing`): the area is added only when the Home Assistant name does not already contain it, and a name that is exactly the area is expanded with the model (`Kitchen` and model `Denon Home Amp` becomes `Kitchen Denon Home Amp`). `macs` limits the diff to those addresses. `config_entry_id` is required.

Matching is by MAC: device-registry connections first, then a MAC embedded in an identifier (Home Connect local, for example), then another device that shares an identifier. A device with a user-given name or an area is preferred. The name is `name_by_user`, otherwise `name`.

A nickname is written with `PUT /2.3/networks/{network_id}/devices/{device_id}` and `{"nickname": ...}`. The 2.2 devices resource drops that write. A reservation description uses the existing reservation update. Each write is read back once and is not retried. Unmatched devices, private or random MACs, unnamed Home Assistant devices, and duplicate target names are returned under `flagged` and are not written. The response lists, per MAC, the current nickname, current description, proposed name, action, and result.

The card's **Sync names** button runs the dry run, shows that diff, and writes only after confirmation.

```yaml
action: eero.sync_names
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  dry_run: true
response_variable: names
```

### Reconfigure and reauthentication
Use **Reconfigure** on the config entry to sign the same entry into a different Eero account (email or phone, then the verification code). The entry id, devices, and entity ids stay the same. When a saved session is rejected and refresh fails, Home Assistant starts the reauthentication flow for that same entry. An account that is already set up on another entry is refused.

## Coming Soon
- TBD, feature requests are welcome.
