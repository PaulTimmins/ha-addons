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

#: Renamed across Supervisor versions; try each in order.
TOKEN_ENV_VARS = ("SUPERVISOR_TOKEN", "HASSIO_TOKEN")

#: Where the Mosquitto add-on listens on the Supervisor's internal network.
#: Used only to tell the user what to type, never assumed to be reachable.
MOSQUITTO_INTERNAL_HOST = "core-mosquitto"


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

    Returns {} if they cannot be obtained -- the add-on declares ``mqtt:want``,
    so running against a hand-configured broker is legitimate.

    Every failure path says why. An earlier version returned {} silently when
    the token was missing or the API said something unexpected, which made a
    failure here indistinguishable from "no broker installed".
    """
    token = token or _supervisor_token()
    if not token:
        print(
            "warning: no Supervisor token in the environment (looked for "
            f"{', '.join(TOKEN_ENV_VARS)}, and any *TOKEN* variable). "
            "Auto-detection of your MQTT broker is not available on this "
            "Supervisor; set mqtt_username and mqtt_password by hand."
        )
        print(f"note: environment variables present: {_env_diagnostics()}")
        return {}

    # Newer Supervisors want X-Supervisor-Token; older ones take an
    # Authorization bearer. Sending both satisfies either without a version
    # check, and neither rejects the presence of the other.
    request = urllib.request.Request(
        SUPERVISOR_MQTT_URL,
        headers={
            "X-Supervisor-Token": token,
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - diagnostics must not mask the error
            pass
        print(
            f"warning: Supervisor returned HTTP {exc.code} for "
            f"{SUPERVISOR_MQTT_URL}: {detail or exc.reason}"
        )
        return {}
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not reach the Supervisor MQTT service: {exc}")
        return {}

    if body.get("result") != "ok":
        print(f"warning: Supervisor MQTT service replied: {str(body)[:300]}")
        return {}

    data = body.get("data") or {}
    if not data.get("host"):
        print(
            "warning: the Supervisor knows of no MQTT service. Is the "
            "Mosquitto broker add-on installed and started? "
            f"Reply was: {str(body)[:300]}"
        )
        return {}
    return data


def _supervisor_token() -> str:
    """Find the Supervisor token, whatever this version chose to call it.

    The variable has been HASSIO_TOKEN and SUPERVISOR_TOKEN historically, and a
    Supervisor that calls add-ons "apps" may well use a third name. Rather than
    keep guessing, fall back to any environment variable that looks like a
    token and say which one was used.
    """
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value

    # Deliberately narrow. Matching every *TOKEN* variable would send an
    # unrelated credential that happens to be in the environment to an HTTP
    # endpoint, so require the name to look like it belongs to the Supervisor.
    candidates = sorted(
        name
        for name, value in os.environ.items()
        if value
        and name.upper().endswith("TOKEN")
        and any(
            marker in name.upper()
            for marker in ("SUPERVISOR", "HASSIO", "ADDON", "HOMEASSISTANT", "APP")
        )
    )
    if candidates:
        chosen = candidates[0]
        print(
            f"note: using the token from {chosen} "
            f"(expected one of {', '.join(TOKEN_ENV_VARS)})."
        )
        return os.environ[chosen]
    return ""


def _env_diagnostics() -> str:
    """Names only -- never values -- of what the Supervisor did inject."""
    names = sorted(os.environ)
    return ", ".join(names) if names else "(empty environment)"


def _manual_broker_help(problem: str) -> str:
    """The whole fix, spelled out. Shown whenever the broker cannot be reached."""
    return (
        f"error: {problem}.\n"
        "\n"
        "  Set these on the add-on's Configuration tab:\n"
        f"    mqtt_host     = {MOSQUITTO_INTERNAL_HOST}   "
        "(the Mosquitto add-on; otherwise your broker's IP)\n"
        "    mqtt_port     = 1883\n"
        "    mqtt_username = a Home Assistant username\n"
        "    mqtt_password = that user's password\n"
        "\n"
        "  The Mosquitto add-on authenticates against Home Assistant user\n"
        "  accounts. If you would rather not reuse your own login, create a\n"
        "  dedicated one under Settings -> People -> Add person, with 'Allow\n"
        "  person to login' enabled, and use those credentials here.\n"
        "\n"
        "  Auto-detection is only a convenience; setting these by hand is\n"
        "  fully supported and nothing else depends on it."
    )


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
        sys.exit(_manual_broker_help("could not determine your MQTT broker"))

    # A host with no credentials produces an anonymous connect. The Mosquitto
    # add-on always refuses that, so fail here where the message can be useful
    # rather than in an endless reconnect loop. A broker the user named
    # themselves may genuinely allow anonymous, so only warn in that case.
    if not mqtt_user and not mqtt_pass:
        if mqtt_host == MOSQUITTO_INTERNAL_HOST:
            sys.exit(
                _manual_broker_help(
                    f"MQTT host is {mqtt_host} but no username or password is "
                    "available, and the Mosquitto add-on refuses anonymous "
                    "connections"
                )
            )
        print(
            f"warning: connecting to {mqtt_host} with no credentials. If it "
            "refuses with 'not authorised', set mqtt_username and "
            "mqtt_password."
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

    # Always ask, and let each field fall back on its own. Gating the whole
    # lookup on a blank mqtt_host meant that setting just the host -- the
    # obvious thing to do -- silently threw away the discovered credentials
    # and produced an anonymous connect the broker refused.
    mqtt_service = discover_mqtt()
    if mqtt_service.get("host"):
        print(
            "found the Mosquitto add-on at "
            f"{mqtt_service['host']}:{mqtt_service.get('port', 1883)}"
        )

    for field, label in (
        ("mqtt_host", "host"),
        ("mqtt_username", "username"),
        ("mqtt_password", "password"),
    ):
        if (options.get(field) or "").strip():
            print(f"using the configured MQTT {label} instead of the discovered one")

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
