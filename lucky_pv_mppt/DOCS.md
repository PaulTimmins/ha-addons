# Lucky PV MPPT Device

Bridges a JGY / inverteriot MPPT solar charge controller into Home Assistant.
It subscribes to the controller's topic on the vendor's MQTT broker, decodes
the binary frames, and republishes them to your Mosquitto broker using MQTT
Discovery — so you get real entities with long-term statistics and an Energy
dashboard PV source.

## Installation

### From the repository URL (recommended)

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Paste the repository URL and **Add**
3. *Lucky PV MPPT Device* appears in the store. Install it — the first build takes
   a few minutes, since the image is built on your machine.

Updates then arrive through the normal add-on page whenever the repository's
`version` is bumped.

### As a local add-on

Useful while developing, or if you would rather not publish the repository.

1. Get access to the `/addons` folder. Install one of the **Samba share**,
   **Advanced SSH & Web Terminal**, or **Studio Code Server** add-ons.
2. Copy the `lucky_pv_mppt` folder to `/addons/lucky_pv_mppt`. It must contain
   `config.yaml`, `Dockerfile`, `addon_bootstrap.py` and the `mppt/` folder.
3. **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**.
4. *Lucky PV MPPT Device* appears under **Local add-ons**. Install it.

Note this route has no update mechanism — you replace the files and rebuild.

## Configuration

| Option | What it is |
|---|---|
| `device_serial` | The hex string in your controller's MQTT topic. Home Assistant keys every entity's `unique_id` on this, so changing it later orphans the entities and their history. |
| `device_module_id` | The wifi module id — the topic segment before the serial. |
| `device_name` | Device name shown in Home Assistant. |
| `source_host` | The **vendor's** broker, where the controller reports. |
| `source_port` | Usually 1883. |
| `source_username` / `source_password` | Credentials for the vendor's broker. |
| `source_topic` | `{serial}` and `{module_id}` are substituted in. |
| `mqtt_host` … `mqtt_password` | **Leave blank.** The add-on asks the Supervisor for your Mosquitto details automatically. Only fill these in to use a different broker. |
| `discovery_prefix` | Must match the MQTT integration's setting. `homeassistant` unless you changed it. |
| `retain` | Keep on, so a restart restores the last reading immediately instead of waiting for the controller's next report. |
| `entity_id_charge_power` | Optional. Pins the entity_id, e.g. `solarinverterwatts` → `sensor.solarinverterwatts`. |
| `entity_id_energy_today` | Optional, same idea. |
| `log_level` | `debug` shows every raw frame. |

Start the add-on and watch the **Log** tab. You should see it connect to both
brokers, publish discovery for seven sensors, then a line per frame.

## Setting up the Energy dashboard

1. **Settings → Devices & services → MQTT** — the device and its seven sensors
   appear once the first frame arrives.
2. **Settings → Dashboards → Energy → Solar panels → Add solar production**,
   and pick **Generation Total**.

Use the *lifetime* counter, not **Generation Today**. It never resets, so the
statistics engine has nothing to misinterpret, and HA derives the daily,
monthly and yearly figures from it on its own.

## Entities

| Name | Unit | Notes |
|---|---|---|
| PV Power | W | Derived as battery voltage × charge current, the same way the vendor app does it. Not transmitted directly. |
| PV Voltage | V | |
| Battery Voltage | V | |
| Charge Current | A | |
| Temperature | °C | Tentative — see the project README. |
| Generation Today | kWh | Resets at midnight. |
| Generation Total | kWh | Lifetime. Use this for the Energy dashboard. |

## Reusing entity_ids from an older setup

`entity_id_charge_power` and `entity_id_energy_today` pin the entity_id so
existing dashboards and automations keep working. Two things to know:

- The domain will be `sensor.`, not `solar.`. Only the REST API ever allowed
  invented domains; real entities cannot use them.
- **Delete the old orphaned entities first**, or Home Assistant will append
  `_2` rather than reuse the name. Spook's `spook.delete_entity` service does
  this.

The override sets `object_id` only, never `unique_id`, so renaming an entity
never orphans its history.

## Behaviour during outages

Neither broker going away stops the add-on; both links reconnect with backoff.

- **Vendor broker down** — no frames arrive, nothing is published, and the
  entities keep their last values rather than going unavailable. A gap in
  reporting is indistinguishable from night-time. The trade-off is that a long
  vendor outage looks like a quiet night; the add-on log shows the reconnect
  attempts.
- **Mosquitto down** — frames are still decoded and logged, but only the
  newest reading is held. On reconnect the add-on republishes discovery and
  sends that one reading. Stale readings are dropped rather than queued, so an
  outage cannot flood Home Assistant with superseded values.
- **Add-on stopped or crashed** — the MQTT will marks the device unavailable.

## Troubleshooting

**Nothing appears in Home Assistant.** Check the MQTT integration is installed
and that `discovery_prefix` matches its setting. The log will show
"published discovery for 7 sensors" if the add-on's side succeeded.

**Entities appear but stay unknown.** Discovery worked but no frame has arrived
yet. Overnight this is normal. Set `log_level: debug` to see raw frames.

**"no MQTT broker" on startup.** The Supervisor did not return Mosquitto
details. Install the Mosquitto broker add-on, or fill in `mqtt_host` manually.

**Energy dashboard will not accept the sensor.** It only offers entities with
`device_class: energy` and `state_class: total_increasing`. Pick **Generation
Total**; if it is missing, no frame has arrived yet, so the entity has no state.
