"""Command line interface.

    solar-mppt run [--foreground] [--background]   the daemon
    solar-mppt decode <hex>...                     decode frames offline
    solar-mppt listen                              tail the source broker
    solar-mppt discovery                           dump the HA payloads
    solar-mppt remove                              delete the HA entities
"""

from __future__ import annotations

import argparse
import json
import sys

from . import daemon as daemon_module
from .config import ConfigError, check_permissions, load
from .homeassistant import (
    ENERGY_DASHBOARD_SENSOR,
    discovery_messages,
    state_payload,
    unique_id,
)
from .parser import FrameError, parse


def _format(frame) -> str:
    return (
        f"PV {frame.pv_voltage:6.1f} V | "
        f"Bat {frame.battery_voltage:6.2f} V | "
        f"{frame.charge_current:6.2f} A | "
        f"{frame.charge_power:7.1f} W | "
        f"{frame.temperature:5.1f} C | "
        f"today {frame.energy_today_kwh:7.3f} kWh | "
        f"total {frame.energy_total_kwh:10.3f} kWh"
    )


def _load_or_exit(args):
    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)

    warning = check_permissions(config.path)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return config


# -- decode ----------------------------------------------------------------


def cmd_decode(args) -> int:
    lines = args.frames or [ln.strip() for ln in sys.stdin if ln.strip()]
    status = 0

    for line in lines:
        try:
            frame = parse(
                line,
                verify_checksum=not args.no_verify,
                verify_header=not args.no_verify,
            )
        except FrameError as exc:
            print(f"error: {exc}", file=sys.stderr)
            status = 1
            continue

        if args.json:
            print(json.dumps(frame.to_dict(include_unknown=args.unknown)))
        else:
            print(_format(frame))
            if args.unknown:
                print(f"    unknown: {frame.unknown}")
                changed = frame.changed_unknowns()
                if changed:
                    print(f"    changed: {changed}")

    return status


# -- listen ----------------------------------------------------------------


def cmd_listen(args) -> int:
    config = _load_or_exit(args)

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("error: paho-mqtt is not installed. pip install paho-mqtt", file=sys.stderr)
        return 2

    source = config.source
    topic = args.topic or source.topic

    def on_connect(client, userdata, flags, rc, *_):
        if rc != 0:
            print(f"connect failed, rc={rc}", file=sys.stderr)
            return
        print(f"connected to {source.host}:{source.port}, subscribing to {topic}")
        client.subscribe(topic)

    def on_message(client, userdata, msg):
        if args.raw:
            print(f"raw {msg.payload.hex().upper()}")
        try:
            frame = parse(msg.payload)
        except FrameError as exc:
            print(f"skipped: {exc}", file=sys.stderr)
            return

        if args.json:
            print(json.dumps(frame.to_dict(include_unknown=args.unknown)), flush=True)
        else:
            print(_format(frame), flush=True)
            if args.unknown:
                print(f"    unknown: {frame.changed_unknowns() or 'nothing new'}")

    client = mqtt.Client(client_id=f"{source.client_id}-listen")
    if source.username:
        client.username_pw_set(source.username, source.password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(source.host, source.port, keepalive=source.keepalive)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


# -- discovery -------------------------------------------------------------


def cmd_discovery(args) -> int:
    """Print exactly what would be published, without touching a broker."""
    config = _load_or_exit(args)
    ha = config.homeassistant

    for topic, payload in discovery_messages(
        serial=config.device.serial,
        device_name=config.device.name,
        discovery_prefix=ha.discovery_prefix,
        state_topic=ha.state_topic,
        availability_topic=ha.availability_topic,
        object_ids=ha.object_ids,
    ):
        if args.json:
            print(json.dumps({"topic": topic, "payload": json.loads(payload)}))
        else:
            entity = json.loads(payload)
            entity_id = entity.get("object_id")
            shown = f"sensor.{entity_id}" if entity_id else "(entity_id from HA)"
            print(f"{topic}\n  -> {shown}")
            print(f"     {payload}\n")

    if not args.json:
        print(f"state topic:        {ha.state_topic}")
        print(f"availability topic: {ha.availability_topic}")
        print(
            "\nEnergy dashboard PV source: the sensor with unique_id "
            f"{unique_id(config.device.serial, ENERGY_DASHBOARD_SENSOR)}"
        )
    return 0


# -- sample ----------------------------------------------------------------


def cmd_sample(args) -> int:
    """Show the state payload a given frame would produce."""
    try:
        frame = parse(args.frame)
    except FrameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(state_payload(frame), indent=2))
    return 0


# -- run / remove ----------------------------------------------------------


def cmd_run(args) -> int:
    config = _load_or_exit(args)
    return daemon_module.run(config, background=args.background)


def cmd_remove(args) -> int:
    config = _load_or_exit(args)
    if not args.yes:
        print(
            "This deletes the Home Assistant entities for device "
            f"{config.device.serial}, including their history. "
            "Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 1
    return daemon_module.remove_entities(config)


# -- wiring ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="solar-mppt", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    def with_config(p):
        p.add_argument("-c", "--config", help="path to config.ini")
        return p

    run_p = with_config(sub.add_parser("run", help="run the bridge daemon"))
    group = run_p.add_mutually_exclusive_group()
    group.add_argument(
        "--foreground",
        dest="background",
        action="store_false",
        help="stay in the foreground (default; use this under systemd)",
    )
    group.add_argument(
        "--background",
        dest="background",
        action="store_true",
        help="detach and run as a daemon (requires [daemon] log_file)",
    )
    run_p.set_defaults(background=False, func=cmd_run)

    decode_p = sub.add_parser("decode", help="decode hex frames offline")
    decode_p.add_argument("frames", nargs="*", help="hex frames; omit to read stdin")
    decode_p.add_argument("--json", action="store_true")
    decode_p.add_argument("--unknown", action="store_true")
    decode_p.add_argument("--no-verify", action="store_true")
    decode_p.set_defaults(func=cmd_decode)

    listen_p = with_config(sub.add_parser("listen", help="tail the source broker"))
    listen_p.add_argument("--topic", help="override the configured topic")
    listen_p.add_argument("--json", action="store_true")
    listen_p.add_argument("--unknown", action="store_true")
    listen_p.add_argument("--raw", action="store_true")
    listen_p.set_defaults(func=cmd_listen)

    disc_p = with_config(
        sub.add_parser("discovery", help="dump the HA discovery payloads")
    )
    disc_p.add_argument("--json", action="store_true")
    disc_p.set_defaults(func=cmd_discovery)

    sample_p = sub.add_parser("sample", help="show the HA state payload for a frame")
    sample_p.add_argument("frame", help="hex frame")
    sample_p.set_defaults(func=cmd_sample)

    remove_p = with_config(
        sub.add_parser("remove", help="delete the HA entities for this device")
    )
    remove_p.add_argument("--yes", action="store_true", help="confirm deletion")
    remove_p.set_defaults(func=cmd_remove)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
