# Paul's HA Add-ons

A Home Assistant add-on repository containing one add-on:

### [Lucky PV MPPT Device](lucky_pv_mppt/)

Bridges a JGY / inverteriot MPPT solar charge controller into Home Assistant.
It subscribes to the controller's topic on the vendor's MQTT broker, decodes
the binary frames, and republishes them via MQTT Discovery — giving real
registry entities with long-term statistics and an Energy dashboard PV source.

## Installing

1. **Settings → Add-ons → Add-on Store**
2. **⋮** (top right) → **Repositories**
3. Paste this repository's URL and click **Add**
4. *Lucky PV MPPT Device* appears in the store. Install it, configure it, start it.

Home Assistant builds the image on your machine, so no prebuilt containers are
published and any architecture in `config.yaml` works.

Configuration options and troubleshooting: [lucky_pv_mppt/DOCS.md](lucky_pv_mppt/DOCS.md).
Protocol notes and design decisions: [lucky_pv_mppt/README.md](lucky_pv_mppt/README.md).

> **Not HACS.** HACS has no add-on category — it covers integrations, Lovelace
> cards and themes. Adding this URL to HACS will not work. Add-ons are handled
> natively by the Supervisor, which is what the steps above use.

## Releasing an update

Home Assistant compares the installed version against `version:` in the
add-on's `config.yaml`. To ship a change:

1. Make the change and run the tests.
2. Bump `version:` in [lucky_pv_mppt/config.yaml](lucky_pv_mppt/config.yaml).
3. Commit and push.

The update appears in the add-on page — sometimes after **⋮ → Check for
updates**. Installing it rebuilds the image; the add-on's configuration and
its `/data` directory are preserved.

## Running the tests

```bash
cd lucky_pv_mppt
python3 -m unittest discover -s tests -v
```

No broker, no Home Assistant, and no `paho-mqtt` needed — the parser, config
loader, discovery payloads and outage handling are all exercised against fakes.

## Layout

```
repository.yaml        this repository's manifest, read by the Supervisor
lucky_pv_mppt/         the add-on (each add-on needs its own directory)
  config.yaml          add-on manifest: options schema, version, services
  build.yaml           base image per architecture (required: without it the
                       Supervisor passes no BUILD_FROM and the build fails)
  Dockerfile           built on your machine at install time
  addon_bootstrap.py   renders the add-on options into config.ini, then execs
  translations/        per-field labels and help text for the add-on UI
  DOCS.md              shown on the add-on's Documentation tab
  mppt/                the application
  tests/
  systemd/             unit file for running outside Home Assistant OS
```

The daemon runs identically as an add-on or standalone; the add-on layer only
supplies the config file. See [lucky_pv_mppt/README.md](lucky_pv_mppt/README.md) for
running it on a plain Linux host.
