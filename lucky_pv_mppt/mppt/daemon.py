"""The long-running bridge: vendor broker in, Home Assistant broker out.

Two MQTT connections. The source client subscribes to the controller's topic on
the vendor broker; the sink client publishes discovery, state and availability
to your own broker. paho reconnects both on its own with backoff, so a dropped
link recovers without the process exiting.

Reporting is sporadic by nature -- several frames a minute in full sun, one
every few minutes or less overnight. Nothing here treats silence as an error.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from typing import Optional

from .config import Config
from .homeassistant import (
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    discovery_messages,
    removal_messages,
    state_payload,
)
from .parser import FrameError, parse

log = logging.getLogger("solar-mppt")

#: Reconnect backoff bounds, seconds.
RECONNECT_MIN = 1
RECONNECT_MAX = 120

#: Backstop on paho's outgoing queue. It defaults to unlimited, which for a
#: process that publishes on every frame means an offline broker grows the
#: queue without bound. Nothing should reach this -- _publish_state drops
#: stale readings while disconnected -- but it caps the damage if it does.
MAX_QUEUED_MESSAGES = 100

#: How long to wait for the final "offline" message to leave on shutdown.
SHUTDOWN_PUBLISH_TIMEOUT = 5.0


class Bridge:
    def __init__(self, config: Config):
        self.config = config
        self.stop_event = threading.Event()
        self.source_client = None
        self.sink_client = None
        self.frames_seen = 0
        self.frames_rejected = 0
        self.frames_dropped = 0
        self._reported_unknowns = set()

        # Set while the sink connection is usable.
        self._sink_ready = threading.Event()

        # The most recent frame that could not be published because the sink
        # was down. Only ever one: this is state, not an event log. Replaying
        # an hour of stale readings on reconnect would land them all in HA
        # with reconnect-time timestamps and pollute the statistics; the
        # newest reading is the only one that means anything.
        self._pending_lock = threading.Lock()
        self._pending_frame = None

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        import paho.mqtt.client as mqtt

        if self.config.homeassistant.enabled:
            self.sink_client = self._build_sink(mqtt)
        else:
            log.warning("Home Assistant publishing is disabled; decoding only")

        self.source_client = self._build_source(mqtt)

        try:
            self.source_client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.shutdown()

        return 0

    def shutdown(self) -> None:
        """Mark the device offline and close both connections."""
        if self.sink_client is not None:
            # Only attempt this if the link is up. Publishing to a dead broker
            # raises from wait_for_publish, and shutdown must not hang or throw.
            if self._sink_ready.is_set():
                try:
                    ha = self.config.homeassistant
                    self.sink_client.publish(
                        ha.availability_topic,
                        PAYLOAD_NOT_AVAILABLE,
                        qos=1,
                        retain=True,
                    ).wait_for_publish(timeout=SHUTDOWN_PUBLISH_TIMEOUT)
                except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                    log.debug("could not publish offline availability: %s", exc)
            else:
                # The will covers this case: the broker publishes "offline" on
                # our behalf once the connection is seen to be gone.
                log.debug("sink already down, leaving availability to the will")
            self.sink_client.loop_stop()
            self.sink_client.disconnect()

        if self.source_client is not None:
            self.source_client.disconnect()

        log.info(
            "stopped after %d frames (%d rejected, %d dropped while HA broker down)",
            self.frames_seen,
            self.frames_rejected,
            self.frames_dropped,
        )

    def request_stop(self, signum, _frame) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        self.stop_event.set()
        if self.source_client is not None:
            self.source_client.disconnect()

    # -- sink (your broker) ------------------------------------------------

    def _build_sink(self, mqtt):
        ha = self.config.homeassistant
        client = mqtt.Client(client_id=ha.client_id)
        if ha.username:
            client.username_pw_set(ha.username, ha.password)
        client.reconnect_delay_set(min_delay=RECONNECT_MIN, max_delay=RECONNECT_MAX)
        client.max_queued_messages_set(MAX_QUEUED_MESSAGES)

        # The will fires if this process dies without a clean disconnect, so HA
        # shows the entities unavailable rather than serving stale values
        # forever.
        client.will_set(
            ha.availability_topic, PAYLOAD_NOT_AVAILABLE, qos=1, retain=True
        )
        client.on_connect = self._on_sink_connect
        client.on_disconnect = self._on_sink_disconnect

        log.info("connecting to HA broker %s:%d", ha.host, ha.port)
        client.connect_async(ha.host, ha.port, keepalive=ha.keepalive)
        client.loop_start()
        return client

    def _on_sink_connect(self, client, userdata, flags, rc, *_):
        if rc != 0:
            # Authentication failures land here on every retry. Log and let
            # paho keep backing off rather than exiting: the broker may simply
            # not be up yet during a boot.
            log.error("HA broker refused connection: %s", _rc_text(rc))
            if rc in (4, 5):
                ha = self.config.homeassistant
                log.error(
                    "  credentials were %s. Either leave the MQTT username and "
                    "password blank so the Mosquitto add-on's own credentials "
                    "are used, or set them to a valid Home Assistant user.",
                    f"username '{ha.username}'" if ha.username else "not supplied",
                )
            return

        log.info("connected to HA broker")
        self._sink_ready.set()

        self._publish_discovery(client)
        client.publish(
            self.config.homeassistant.availability_topic,
            PAYLOAD_AVAILABLE,
            qos=1,
            retain=True,
        )
        self._flush_pending()

    def _on_sink_disconnect(self, client, userdata, rc, *_):
        self._sink_ready.clear()
        if rc == 0:
            log.info("HA broker disconnected cleanly")
        else:
            log.warning("HA broker connection lost (rc=%s), reconnecting", rc)

    def _flush_pending(self) -> None:
        """Publish the reading that arrived while the sink was down, if any."""
        with self._pending_lock:
            frame, self._pending_frame = self._pending_frame, None
        if frame is not None:
            log.info("publishing the reading buffered while the HA broker was down")
            self._send_state(frame)

    def _publish_discovery(self, client) -> None:
        """(Re)publish retained discovery configs.

        Repeated on every reconnect: cheap, idempotent, and it heals the case
        where the broker lost its retained messages.
        """
        ha = self.config.homeassistant
        count = 0
        for topic, payload in discovery_messages(
            serial=self.config.device.serial,
            device_name=self.config.device.name,
            discovery_prefix=ha.discovery_prefix,
            state_topic=ha.state_topic,
            availability_topic=ha.availability_topic,
            object_ids=ha.object_ids,
        ):
            client.publish(topic, payload, qos=1, retain=True)
            count += 1
        log.info("published discovery for %d sensors", count)

    # -- source (vendor broker) -------------------------------------------

    def _build_source(self, mqtt):
        source = self.config.source
        client = mqtt.Client(client_id=source.client_id)
        if source.username:
            client.username_pw_set(source.username, source.password)
        client.reconnect_delay_set(min_delay=RECONNECT_MIN, max_delay=RECONNECT_MAX)
        client.on_connect = self._on_source_connect
        client.on_disconnect = self._on_source_disconnect
        client.on_message = self._on_message

        log.info("connecting to source broker %s:%d", source.host, source.port)
        client.connect_async(source.host, source.port, keepalive=source.keepalive)
        return client

    def _on_source_connect(self, client, userdata, flags, rc, *_):
        if rc != 0:
            log.error("source broker refused connection: %s", _rc_text(rc))
            return
        # Re-subscribed on every reconnect, not just the first: the session is
        # clean, so the broker does not remember our subscription.
        topic = self.config.source.topic
        client.subscribe(topic, qos=1)
        log.info("subscribed to %s", topic)

    def _on_source_disconnect(self, client, userdata, rc, *_):
        if rc == 0:
            log.info("source broker disconnected cleanly")
        else:
            # Somebody else's server. Expect this periodically; paho retries
            # with backoff and loop_forever keeps the process alive.
            log.warning("source broker connection lost (rc=%s), reconnecting", rc)

    def _on_message(self, client, userdata, msg):
        log.debug("frame %s", msg.payload.hex().upper())
        try:
            frame = parse(msg.payload)
        except FrameError as exc:
            # A malformed frame is skipped, never fatal. The controller is on
            # the far side of somebody else's broker.
            self.frames_rejected += 1
            log.warning("skipping frame: %s", exc)
            return

        self.frames_seen += 1

        # A previously-constant field coming alive is new information about the
        # protocol, worth recording once. It is never treated as an error.
        changed = frame.changed_unknowns()
        for key, value in changed.items():
            if (key, value) not in self._reported_unknowns:
                self._reported_unknowns.add((key, value))
                log.info(
                    "unidentified field %s now reads 0x%04X (was 0x%04X) -- "
                    "frame %s",
                    key,
                    value,
                    _baseline(key),
                    frame.raw.hex().upper(),
                )

        log.info(
            "PV %.1f V  Bat %.2f V  %.2f A  %.1f W  %.1f C  today %.3f kWh  "
            "total %.3f kWh",
            frame.pv_voltage,
            frame.battery_voltage,
            frame.charge_current,
            frame.charge_power,
            frame.temperature,
            frame.energy_today_kwh,
            frame.energy_total_kwh,
        )

        self._publish_state(frame)

    def _publish_state(self, frame) -> None:
        if self.sink_client is None:
            return

        if not self._sink_ready.is_set():
            # Hold only the newest reading. paho would happily queue every one
            # of these -- its outgoing queue is unlimited by default -- and
            # dump hours of stale values into HA on reconnect, all stamped
            # with the reconnect time.
            with self._pending_lock:
                had_pending = self._pending_frame is not None
                self._pending_frame = frame
            if had_pending:
                self.frames_dropped += 1
            log.debug("HA broker down, holding the latest reading")
            return

        self._send_state(frame)

    def _send_state(self, frame) -> None:
        ha = self.config.homeassistant
        payload = json.dumps(state_payload(frame))
        result = self.sink_client.publish(
            ha.state_topic, payload, qos=1, retain=ha.retain
        )
        if result.rc != 0:
            # Racing a disconnect: hold it for the reconnect instead of losing it.
            log.warning("failed to publish state (rc=%s), holding it", result.rc)
            with self._pending_lock:
                self._pending_frame = frame


def _baseline(key: str) -> int:
    from .parser import BASELINE_UNKNOWNS

    return BASELINE_UNKNOWNS.get(key, 0)


def _rc_text(rc) -> str:
    return {
        1: "incorrect protocol version",
        2: "invalid client id",
        3: "server unavailable",
        4: "bad username or password",
        5: "not authorised",
    }.get(rc, f"rc={rc}")


# -- process management ----------------------------------------------------


def setup_logging(config: Config, force_stdout: bool = False) -> None:
    level = getattr(logging, config.daemon.log_level, logging.INFO)
    handler: logging.Handler
    if config.daemon.log_file and not force_stdout:
        handler = logging.FileHandler(config.daemon.log_file)
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def daemonize(pid_file: Optional[str]) -> None:
    """Double-fork into the background.

    Under systemd prefer ``run --foreground`` with ``Type=simple`` and let the
    supervisor own the process; this exists for running without one.
    """
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    os.chdir("/")
    os.umask(0)

    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    os.dup2(devnull, sys.stdout.fileno())
    os.dup2(devnull, sys.stderr.fileno())

    if pid_file:
        write_pid_file(pid_file)


def write_pid_file(path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")


def remove_pid_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def run(config: Config, background: bool = False) -> int:
    """Entry point used by the CLI."""
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        print(
            "error: paho-mqtt is not installed. pip install paho-mqtt",
            file=sys.stderr,
        )
        return 2

    if background:
        if config.daemon.log_file is None:
            print(
                "error: --background needs [daemon] log_file set, "
                "otherwise all output is discarded.",
                file=sys.stderr,
            )
            return 2
        daemonize(config.daemon.pid_file)
    elif config.daemon.pid_file:
        write_pid_file(config.daemon.pid_file)

    setup_logging(config, force_stdout=not background and not config.daemon.log_file)

    log.info("solar-mppt starting, config %s", config.path)
    log.info(
        "device %s (%s), source %s",
        config.device.name,
        config.device.serial,
        config.source.topic,
    )

    bridge = Bridge(config)
    signal.signal(signal.SIGTERM, bridge.request_stop)
    signal.signal(signal.SIGINT, bridge.request_stop)

    try:
        return bridge.run()
    finally:
        remove_pid_file(config.daemon.pid_file)


def remove_entities(config: Config) -> int:
    """Publish empty retained discovery payloads so HA deletes the entities."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print(
            "error: paho-mqtt is not installed. pip install paho-mqtt",
            file=sys.stderr,
        )
        return 2

    setup_logging(config, force_stdout=True)
    ha = config.homeassistant
    client = mqtt.Client(client_id=f"{ha.client_id}-remove")
    if ha.username:
        client.username_pw_set(ha.username, ha.password)
    client.connect(ha.host, ha.port, keepalive=ha.keepalive)
    client.loop_start()

    count = 0
    for topic, payload in removal_messages(config.device.serial, ha.discovery_prefix):
        client.publish(topic, payload, qos=1, retain=True).wait_for_publish()
        count += 1

    client.publish(
        ha.availability_topic, PAYLOAD_NOT_AVAILABLE, qos=1, retain=True
    ).wait_for_publish()
    client.loop_stop()
    client.disconnect()

    log.info("removed %d entities from Home Assistant", count)
    return 0
