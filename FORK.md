# rvdelzen/home-assistant-eero

Household fork of [schmittx/home-assistant-eero](https://github.com/schmittx/home-assistant-eero),
installed on the Bentveld Green through HACS as a custom repository in place
of the upstream entry.

## Patches on top of upstream 1.8.1

| Version | Change | Upstream |
|---|---|---|
| 1.10.0 | IP reservation table: poll devices and reservations without creating client entities, `eero.ip_table`, `eero.reserve_ip`, `eero.release_ip`, a reservation-count sensor, a bundled Lovelace card, and one-way `eero.sync_names` from Home Assistant to eero nicknames and reservation descriptions. | — |
| 1.9.0 | DHCP reservation services (`list`, `create`, `update`, `delete`), port-forward services (`list`, `create`, `delete`), config-entry reconfigure (switch Eero account without a new entry id) and reauthentication, and persistence of a refreshed session token back into the config entry. | — |
| 1.8.1.1 | `EeroEntity.device_info` links child devices to the network device with `via_device_id` (device registry id) instead of the deprecated `via_device` identifier tuple. On Home Assistant 2026.10 the tuple logs a deprecation warning on every platform and raises for `image` (`Error adding entity None for domain image with platform eero`, so `image.ohana_qr_code` was never added). Breaks in 2027.8 otherwise. | [#180](https://github.com/schmittx/home-assistant-eero/issues/180) |

All platforms (binary_sensor, button, device_tracker, image, light, number,
select, sensor, switch, time, update) take their device info from
`EeroEntity`, so the one change covers them all.

`via_device_id` in entity `DeviceInfo` needs a Home Assistant release that
has it (2026.10.1 on the Green does).

## Going back to upstream

When upstream releases a version with the same fix: in HACS remove this
repository, add `schmittx/home-assistant-eero` again, download, restart.
