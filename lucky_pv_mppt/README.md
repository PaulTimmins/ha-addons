# Lucky PV MPPT Device

Bridges a JGY / inverteriot MPPT solar charge controller into Home Assistant.
It subscribes to the controller's `device_state` topic on the vendor's MQTT
broker, decodes the binary frames, and republishes them to your own broker using
**Home Assistant MQTT Discovery** — so HA creates real registry entities with
long-term statistics and an Energy dashboard PV source.

Runs as an external long-running daemon. It does not need to run inside HA.

```
vendor broker  ──subscribe──▶  solar-mppt  ──publish──▶  your broker  ──▶  HA
(inverteriot)                    daemon                  (mosquitto)
```

The parser, config loader and payload builder are pure standard library;
only the MQTT layer needs `paho-mqtt`.

## Why not the REST API

An earlier version of this used `POST /api/states/...`. That only pushes a value
into the state machine. The result has **no `unique_id`, no device registry
entry and no config entry**, so it cannot be renamed or assigned to an area, it
vanishes on restart, and — the reason the Energy dashboard never worked — it
gets **no long-term statistics**. It also lets you invent domains HA has no
concept of, which is how `solar.solarinverterwatts` came to exist; real entities
are always `sensor.`.

Discovery fixes all of that, and the orphaned-entity warnings that come with it.

## Quick start

```bash
pip install -r requirements.txt
cp config.example.ini config.ini
chmod 600 config.ini
```

Fill in `config.ini`, then check what will be published before touching a
broker:

```bash
python3 -m mppt discovery
```

Then run it:

```bash
python3 -m mppt run --foreground
```

## Commands

| Command | Does |
|---|---|
| `run [--foreground\|--background]` | the bridge daemon |
| `decode <hex>...` | decode frames offline; reads stdin if no args |
| `sample <hex>` | show the JSON state payload a frame produces |
| `listen` | tail the source broker and print decoded frames |
| `discovery` | dump the discovery payloads without connecting |
| `remove --yes` | delete this device's entities from HA |

`--foreground` is the default and is what you want under systemd.
`--background` double-forks and requires `[daemon] log_file` to be set.

## Configuration

Everything site-specific — credentials, brokers, topic, serial — is in an INI
config file; nothing is compiled into the code. `configparser` rather than TOML
because the target Python is 3.9, which has no `tomllib`.

Searched in order when `--config` is not given:

1. `./config.ini`
2. `~/.config/solar-mppt/config.ini`
3. `/etc/solar-mppt/config.ini`

`config.ini` is gitignored, `config.example.ini` holds placeholders only, and a
test fails if a real serial or hostname lands in it. Loading warns if the file
is group- or world-readable. Interpolation is disabled, so a password
containing `%` is read verbatim.

**Two brokers, and they are not the same one.** `[source]` is the vendor's
broker that the controller reports to; `[homeassistant]` is your own broker that
HA's MQTT integration is attached to.

`{serial}` and `{module_id}` from `[device]` are substituted into any topic.

### Pinning entity_ids

By default HA derives entity_ids from the device and sensor names. To match
entity_ids an earlier setup used, so dashboards and automations keep working:

```ini
[entity_ids]
charge_power = solarinverterwatts
energy_today = solarinverterdailywatts
```

Valid keys are the sensor keys in the table below. This sets `object_id` in the
discovery payload and does **not** affect `unique_id`, so renaming an entity
never orphans its history.

Note the domain will be `sensor.`, not `solar.` — no version of HA can create a
`solar` domain, and only the REST API ever allowed it. Also delete any leftover
orphan entities with those ids *before* first run, or HA will append `_2` rather
than reuse the name. Spook's `spook.delete_entity` service does this.

## Home Assistant setup

1. Start the daemon. The device and seven sensors appear under
   **Settings → Devices & services → MQTT**.
2. **Settings → Dashboards → Energy → Solar panels → Add solar production**,
   and pick **Generation Total**.

Use the *lifetime* counter, not the daily one — it never resets, so the
statistics engine has nothing to misinterpret. Verified against the captures:
lifetime and daily move 1 Wh for 1 Wh. HA derives daily/monthly/yearly totals
from it on its own.

### Entities created

| Sensor key | Name | Unit | device_class | state_class |
|---|---|---|---|---|
| `charge_power` | PV Power | W | power | measurement |
| `pv_voltage` | PV Voltage | V | voltage | measurement |
| `battery_voltage` | Battery Voltage | V | voltage | measurement |
| `charge_current` | Charge Current | A | current | measurement |
| `temperature` | Temperature | °C | temperature | measurement |
| `energy_today` | Generation Today | kWh | energy | total_increasing |
| `energy_total` | Generation Total | kWh | energy | total_increasing |

All seven read from one retained JSON state topic, so a frame updates
everything in a single message.

### Sporadic reporting

The controller reports several times a minute in full sun and every few minutes
or less overnight. Nothing here treats silence as a fault:

- **No `expire_after`.** Overnight quiet is normal; letting the energy sensors
  expire into `unavailable` would tear holes in the statistics.
- **Availability tracks the daemon, not the data.** An MQTT will marks the
  device offline only if the bridge itself dies.
- **State is retained**, so after an HA restart the last reading is restored
  immediately instead of waiting for the next report.

## When a broker goes offline

paho reconnects both links itself with backoff (1s → 120s), so neither outage
exits the process.

**Vendor broker down.** No frames arrive. Nothing is published, the HA link
stays up, and the entities keep their last values — HA does not show them as
unavailable. That is deliberate: a gap in reporting is indistinguishable from
night-time, and there is no threshold that separates the two without either
false alarms or a delay long enough to be useless. The subscription is
re-established on every reconnect, because the session is clean and the broker
does not remember it.

The trade-off is that a multi-day vendor outage looks like a quiet night on the
dashboard. `journalctl -u solar-mppt` shows the reconnect attempts. If you want
this visible in HA, a `last_seen` timestamp sensor is the usual approach —
it does not risk gapping the energy statistics the way an availability timeout
would.

**Your broker down.** Frames still arrive and are still decoded and logged, but
state cannot be published. Only the **newest** reading is held; each new frame
replaces it. On reconnect the bridge republishes discovery, reasserts
availability, and sends that one held reading.

Stale readings are deliberately dropped rather than queued. paho's outgoing
queue is unlimited by default, so queueing would grow memory without bound
during an outage and then deliver hours of superseded values to HA, all stamped
at reconnect time — which would corrupt the `measurement` statistics for that
window. `MAX_QUEUED_MESSAGES` caps paho's queue as a backstop.

**The daemon itself dying** is the case the MQTT will covers: the broker
publishes `offline` to the availability topic and the entities go unavailable.
A clean shutdown publishes it directly.

`tests/test_daemon_offline.py` drives all of these against a fake client.

## Deployment

### Home Assistant OS — as an add-on

On HA OS the Supervisor owns the machine; there is no supported place to put a
systemd unit, and anything installed over SSH is wiped on update. The supported
route is an add-on, which is this project built as a container the Supervisor
manages.

Add the repository URL under **Settings → Add-ons → Add-on Store → ⋮ →
Repositories**, then install from the store. Add-ons are not installed through
HACS — HACS covers integrations, Lovelace cards and themes, and the Supervisor
handles add-ons natively.

The add-on asks the Supervisor for your Mosquitto host and credentials, so they
never have to be entered twice.

Full option reference and troubleshooting: [DOCS.md](DOCS.md), which is also
shown on the add-on's Documentation tab.

The add-on files are `config.yaml` (the Supervisor manifest — not to be
confused with the application's own `config.ini`), `Dockerfile`, and
`addon_bootstrap.py`, which renders the UI options into a `config.ini` and
hands over to the daemon. The daemon itself is identical either way.

### Anywhere else — systemd

```bash
sudo cp -r . /opt/solar-mppt
sudo install -m 600 config.ini /etc/solar-mppt/config.ini
sudo cp systemd/solar-mppt.service /etc/systemd/system/
sudo systemctl enable --now solar-mppt
journalctl -u solar-mppt -f
```

The unit uses `Type=simple` with `--foreground` and `Restart=always`. paho
reconnects to both brokers internally with backoff, so a dropped link recovers
without the process exiting; the restart covers it dying outright.

The host needs to be always on, with outbound internet to reach the vendor's
broker and LAN access to your own. It does not need to be near Home Assistant.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

No broker and no `paho-mqtt` needed — the parser, config loader and discovery
payloads are all pure functions. The tests assert the properties HA silently
depends on: get `state_class` or `device_class` wrong and nothing raises, the
entity just never shows up in the Energy dashboard.

## Protocol

Topic `jgy/<wifi-module-id>/<device-serial>/device_state`, payload 37 bytes,
big-endian, Modbus-flavoured but with a vendor function code and a one-byte
checksum rather than a CRC16.

| Offset | Size | Field | Scale | Notes |
|---|---|---|---|---|
| 0 | 1 | device address | | `0x01`, validated |
| 1 | 1 | function code | | `0xB3` vendor-specific, validated |
| 2–5 | 4 | *unknown* | | so far `01 00 0D 02` |
| 6–7 | u16 | PV array voltage | ×0.1 V | |
| 8–9 | u16 | battery voltage | ×0.01 V | |
| 10–11 | u16 | charge current | ×0.01 A | |
| 12–13 | u16 | temperature | ×0.1 °C | **tentative**, see below |
| 14–15 | u16 | *unknown* | | so far 0 |
| 16–17 | u16 | *unknown* | | so far `0x10C8` |
| 18–21 | u32 | *unknown* | | so far 0 |
| 22–23 | u16 | generation today | Wh | |
| 24–27 | u32 | generation total | Wh | |
| 28–35 | 8 | *unknown* | | so far 0 |
| 36 | 1 | checksum | | `sum(bytes[0:36]) & 0xFF` |

Only the address, function code, length and checksum are validated. Bytes 2–5
look like a fixed header but are treated as data, so the frame stays valid if
the controller ever changes them.

Charge power is **not** transmitted. The vendor app derives it as
`battery_voltage × charge_current`, which `MPPTFrame.charge_power` reproduces.
Frames count watt-hours; the HA layer converts to kWh to match the declared
unit.

Monthly generation and the daily min/max statistics shown in the app are not in
the frame — the app computes or fetches those separately. HA will derive the
equivalents from the energy sensors.

### How the field map was confirmed

A screenshot of the vendor app was taken while frame
`...0254 13B7 03A3 ...08C6 0021BD55...` was in flight. Every displayed value
lines up:

| App | Frame |
|---|---|
| PV: 59.6V | `0x0254` = 596 → 59.6 V |
| Bat.: 50.47V | `0x13B7` = 5047 → 50.47 V |
| 0.47KW PV Power | 50.47 × 9.31 A = 469.9 W |
| 2.25KWH Generation today | `0x08C6` = 2246 Wh |
| 2.21MWH Total Generation | `0x0021BD55` = 2 211 157 Wh |

`tests/test_parser.py` asserts each of these, so the field map is pinned to
observed ground truth rather than to guesswork.

### Open questions

- **Offset 12–13** reads 326–328 across the samples and tracks charge current
  slightly upward, which is what a controller heatsink does. 32.6–32.8 °C is
  plausible for a unit under load. Confident enough to expose, not confident
  enough to call settled — confirm by watching it overnight, when it should
  fall toward ambient as the array stops producing.
- **Offset 16–17** is a constant `0x10C8`. Could be two bytes (16 and 200 — a
  16-cell LiFePO4 bank at 200 Ah fits a 50.47 V battery at 3.15 V/cell), or one
  value (4296 → a 42.96 V low-voltage cutoff, 2.69 V/cell, also plausible).
  Changing a battery setting in the app and re-capturing would settle it.
- The **all-zero fields** are likely load output current/power and fault or
  status flags on a controller with nothing connected to the load terminals.

None of this is enforced. The unidentified fields are recorded as *observed*
constants in `BASELINE_UNKNOWNS`, and a frame that disagrees still parses
normally — a field coming alive is new information, not a corrupt frame.
`MPPTFrame.changed_unknowns()` reports what moved; the daemon logs it once at
INFO with the raw frame, and `mppt listen --unknown` prints it live. That is how
the rest of the frame gets identified without anything breaking meanwhile.
