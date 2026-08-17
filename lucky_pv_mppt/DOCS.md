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

**You only need to set two fields.** Everything else is a working default for
this hardware, shared across every device on the service:

- **`device_serial`** — the hex string unique to your unit (see below).
- **`source_password`** — the vendor broker password (see below).

Leave the rest as-is unless the vendor changes something. Every field has help
text under it in the UI.

The `mqtt_*` fields can usually stay blank — the add-on asks the Supervisor for
your Mosquitto details. **On some Supervisor versions that lookup is not
available** (the log says "no Supervisor token in the environment"), in which
case fill them in by hand; see Troubleshooting. Setting them is fully
supported, not a workaround.

### Finding your device serial

It is the second-to-last segment of the controller's MQTT topic —
`jgy/<module>/<SERIAL>/device_state`. If you have used the vendor app, it is
the device id shown there. Twelve hex characters, e.g. `4CEBD683EEC0`.

### Getting the vendor broker password

The vendor uses one shared password for every device, next to a generic
`device_client` username — the broker login is a shared pipe, not per-device
authentication. Two ways to get it:

- **Email paul@timmins.net** and ask for it.
- **Sniff it yourself.** It is sent in cleartext in the MQTT CONNECT packet.
  Point Wireshark at traffic to `usadev.inverteriot.com` port `1883` (from a
  phone running the vendor app, or the controller itself) and read the
  username and password straight out of the CONNECT packet. No TLS is used.

It is not derived from your serial, so there is nothing to compute — it is the
same string for everyone.

Once both fields are set, start the add-on and watch the **Log** tab. You
should see it connect to both brokers, publish discovery for seven sensors,
then a line per frame.

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
| Temperature | °C | Controller temperature. Confirmed against overnight data. |
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

**The build fails with "base name should not be blank".** `build.yaml` is
missing or its `build_from` has no entry for your architecture. The Supervisor
only passes the `BUILD_FROM` build-arg when that file supplies one; there is no
default.

**Nothing appears in Home Assistant.** Check the MQTT integration is installed
and that `discovery_prefix` matches its setting. The log will show
"published discovery for 7 sensors" if the add-on's side succeeded.

**Entities appear but stay unknown.** Discovery worked but no frame has arrived
yet. Overnight this is normal. Set `log_level: debug` to see raw frames.

**"no Supervisor token in the environment".** Some Supervisor versions do not
inject a token into add-ons, so the broker cannot be detected automatically.
This is not fatal — set the broker by hand on the Configuration tab:

```
mqtt_host     = core-mosquitto
mqtt_port     = 1883
mqtt_username = a Home Assistant username
mqtt_password = that user's password
```

The Mosquitto add-on authenticates against Home Assistant user accounts. To
avoid reusing your own login, create a dedicated one under **Settings → People
→ Add person** with *Allow person to login* enabled.

**"HA broker refused connection: not authorised" (rc=5).** The broker rejected
the credentials. Usually they are blank: `core-mosquitto` never allows
anonymous connections. Fill in `mqtt_username` and `mqtt_password` as above.

**Energy dashboard will not accept the sensor.** It only offers entities with
`device_class: energy` and `state_class: total_increasing`. Pick **Generation
Total**; if it is missing, no frame has arrived yet, so the entity has no state.
