#!/usr/bin/env python3
"""Add-on entrypoint: render the HA add-on options into a config.ini, then run.

The Supervisor writes the UI settings to /data/options.json. The application
itself only understands its own INI file, so this translates one into the other
and hands over. Keeping the INI as the single source of truth means the daemon
behaves identically whether it is running as an add-on or standalone.

If the broker fields are left blank, the Mosquitto details are fetched from the
Supervisor services API, so they never have to be typed twice.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

OPTIONS_PATH = "/data/options.json"
CONFIG_PATH = "/data/config.ini"
SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"


def read_options(path: str = OPTIONS_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        sys.exit(f"error: {path} not found; is this running as an add-on?")
    except json.JSONDecodeError as exc:
        sys.exit(f"error: could not parse {path}: {exc}")


def discover_mqtt(token: str = None) -> dict:
    """Ask the Supervisor for the Mosquitto connection details.

    Returns {} if the service is not available -- the add-on declares
    ``mqtt:want``, so running without it is legitimate.
    """
    token = token or os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {}

    request = urllib.request.Request(
        SUPERVISOR_MQTT_URL, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not reach the Supervisor MQTT service: {exc}")
        return {}

    if body.get("result") != "ok":
        return {}
    return body.get("data") or {}


def _ini_escape(value: str) -> str:
    """configparser reads to end of line, so only newlines are dangerous."""
    return str(value).replace("\n", " ").replace("\r", " ")


def build_config(options: dict, mqtt_service: dict) -> str:
    """Render the INI text. Blank broker fields fall back to Mosquitto."""
    serial = (options.get("device_serial") or "").strip()
    if not serial:
        sys.exit(
            "error: device_serial is not set. Put the hex string from your "
            "controller's MQTT topic in the add-on configuration."
        )

    source_host = (options.get("source_host") or "").strip()
    if not source_host:
        sys.exit("error: source_host is not set (the vendor's broker).")

    mqtt_host = (options.get("mqtt_host") or "").strip() or mqtt_service.get("host", "")
    mqtt_port = options.get("mqtt_port") or mqtt_service.get("port", 1883)
    mqtt_user = (
        options.get("mqtt_username") or ""
    ).strip() or mqtt_service.get("username", "")
    mqtt_pass = (
        options.get("mqtt_password") or ""
    ).strip() or mqtt_service.get("password", "")

    if not mqtt_host:
        sys.exit(
            "error: no MQTT broker. Either install the Mosquitto add-on, or "
            "set mqtt_host in the add-on configuration."
        )

    lines = [
        "# Generated from the add-on options at startup. Edits are overwritten;",
        "# change the settings in the Home Assistant UI instead.",
        "",
        "[device]",
        f"serial = {_ini_escape(serial)}",
        f"module_id = {_ini_escape(options.get('device_module_id', ''))}",
        f"name = {_ini_escape(options.get('device_name') or 'Lucky PV MPPT')}",
        "",
        "[source]",
        f"host = {_ini_escape(source_host)}",
        f"port = {options.get('source_port', 1883)}",
        f"username = {_ini_escape(options.get('source_username', ''))}",
        f"password = {_ini_escape(options.get('source_password', ''))}",
        f"topic = {_ini_escape(options.get('source_topic', ''))}",
        "",
        "[homeassistant]",
        "enabled = true",
        f"host = {_ini_escape(mqtt_host)}",
        f"port = {mqtt_port}",
        f"username = {_ini_escape(mqtt_user)}",
        f"password = {_ini_escape(mqtt_pass)}",
        f"discovery_prefix = "
        f"{_ini_escape(options.get('discovery_prefix') or 'homeassistant')}",
        f"retain = {'true' if options.get('retain', True) else 'false'}",
        "",
        "[entity_ids]",
    ]

    overrides = {
        "charge_power": (options.get("entity_id_charge_power") or "").strip(),
        "energy_today": (options.get("entity_id_energy_today") or "").strip(),
    }
    for key, value in overrides.items():
        if value:
            lines.append(f"{key} = {_ini_escape(value)}")

    lines += [
        "",
        "[daemon]",
        f"log_level = {(options.get('log_level') or 'info').upper()}",
        "",
    ]
    return "\n".join(lines)


def write_config(text: str, path: str = CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)  # holds two sets of broker credentials


def main() -> None:
    options = read_options()
    mqtt_service = {}
    if not (options.get("mqtt_host") or "").strip():
        mqtt_service = discover_mqtt()
        if mqtt_service.get("host"):
            print(
                "using the Mosquitto add-on at "
                f"{mqtt_service['host']}:{mqtt_service.get('port', 1883)}"
            )

    write_config(build_config(options, mqtt_service))
    print(f"wrote {CONFIG_PATH}, starting bridge")
    sys.stdout.flush()

    # exec rather than spawn: the daemon becomes PID 1 so the Supervisor's
    # stop signal reaches it directly and shutdown stays clean.
    os.execv(
        sys.executable,
        [sys.executable, "-m", "mppt", "run", "--foreground", "--config", CONFIG_PATH],
    )


if __name__ == "__main__":
    main()
