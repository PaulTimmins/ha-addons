"""What the bridge does when either broker goes away.

Driven with a fake MQTT client rather than paho, so the outage paths are
exercised without a broker. The behaviour being pinned:

  * an offline HA broker must not grow an unbounded backlog -- paho's outgoing
    queue is unlimited by default, and this process publishes on every frame
  * only the newest reading survives an outage; replaying stale ones would
    land an hour of history in HA stamped at reconnect time
  * a frame that arrives during an outage is still published once the link
    comes back
"""

import json
import logging
import unittest
from types import SimpleNamespace

from mppt.config import (
    Config,
    DaemonConfig,
    DeviceConfig,
    HomeAssistantConfig,
    SourceConfig,
)
from mppt.daemon import Bridge
from mppt.homeassistant import PAYLOAD_AVAILABLE, PAYLOAD_NOT_AVAILABLE
from pathlib import Path

def setUpModule():
    """These tests deliberately drive error paths; keep their logging quiet."""
    logging.getLogger("solar-mppt").disabled = True


def tearDownModule():
    logging.getLogger("solar-mppt").disabled = False


FRAMES = [
    "01B301000D02024F13E506A80147000010C80000000008C90021BD580000000000000000E2",
    "01B301000D02024413E906B90148000010C80000000008CC0021BD5B0000000000000000F3",
    "01B301000D02025413B703A30146000010C80000000008C60021BD550000000000000000AA",
]


class FakeInfo:
    def __init__(self, rc=0):
        self.rc = rc

    def wait_for_publish(self, timeout=None):
        if self.rc != 0:
            raise RuntimeError("not connected")


class FakeClient:
    """Just enough of paho to drive the callbacks."""

    def __init__(self):
        self.published = []
        self.connected = False
        self.max_queued = None
        self.loop_stopped = False

    def publish(self, topic, payload=None, qos=0, retain=False):
        if not self.connected:
            return FakeInfo(rc=4)  # MQTT_ERR_NO_CONN
        self.published.append((topic, payload, retain))
        return FakeInfo(rc=0)

    def max_queued_messages_set(self, n):
        self.max_queued = n

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.connected = False

    def topics(self):
        return [t for t, _, _ in self.published]


def make_config(**ha_overrides):
    ha = dict(
        enabled=True,
        host="192.0.2.10",
        port=1883,
        username=None,
        password=None,
        client_id="ha-client",
        keepalive=60,
        discovery_prefix="homeassistant",
        state_topic="solar-mppt/AABB/state",
        availability_topic="solar-mppt/AABB/availability",
        retain=True,
        object_ids={},
    )
    ha.update(ha_overrides)
    return Config(
        device=DeviceConfig(serial="AABB", module_id="wifi0", name="Solar MPPT"),
        source=SourceConfig(
            host="vendor.example.com",
            port=1883,
            username=None,
            password=None,
            topic="jgy/wifi0/AABB/device_state",
            client_id="src",
            keepalive=60,
        ),
        homeassistant=HomeAssistantConfig(**ha),
        daemon=DaemonConfig(log_level="CRITICAL", log_file=None, pid_file=None),
        path=Path("/dev/null"),
    )


def message(hex_frame):
    return SimpleNamespace(payload=bytes.fromhex(hex_frame))


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.bridge = Bridge(self.config)
        self.sink = FakeClient()
        self.bridge.sink_client = self.sink

    def connect_sink(self):
        self.sink.connected = True
        self.bridge._on_sink_connect(self.sink, None, {}, 0)

    def drop_sink(self, rc=1):
        self.sink.connected = False
        self.bridge._on_sink_disconnect(self.sink, None, rc)

    def feed(self, hex_frame):
        self.bridge._on_message(None, None, message(hex_frame))

    def state_messages(self):
        return [
            (payload, retain)
            for topic, payload, retain in self.sink.published
            if topic == self.config.homeassistant.state_topic
        ]


class TestHomeAssistantBrokerOffline(BridgeTestCase):
    def test_no_backlog_builds_up_during_an_outage(self):
        """The bug this guards: paho queues every publish, unbounded."""
        self.connect_sink()
        self.drop_sink()

        for _ in range(500):
            self.feed(FRAMES[0])

        self.assertEqual(self.state_messages(), [], "nothing should be sent")
        # Exactly one frame is retained in memory, regardless of outage length.
        self.assertIsNotNone(self.bridge._pending_frame)
        self.assertEqual(self.bridge.frames_seen, 500)
        self.assertEqual(self.bridge.frames_dropped, 499)

    def test_only_the_newest_reading_survives_the_outage(self):
        self.connect_sink()
        self.drop_sink()

        for hex_frame in FRAMES:
            self.feed(hex_frame)

        before = len(self.state_messages())
        self.connect_sink()
        published = self.state_messages()[before:]

        self.assertEqual(len(published), 1, "one catch-up message, not three")
        payload = json.loads(published[0][0])
        # FRAMES[2] is the newest fed; 50.47 V is its battery voltage.
        self.assertEqual(payload["battery_voltage"], 50.47)

    def test_frames_flow_again_after_reconnect(self):
        self.connect_sink()
        self.drop_sink()
        self.connect_sink()

        before = len(self.state_messages())
        self.feed(FRAMES[0])
        self.assertEqual(len(self.state_messages()), before + 1)

    def test_nothing_is_lost_if_the_outage_starts_mid_publish(self):
        """publish() reporting a failure must not discard the reading."""
        self.connect_sink()
        self.sink.connected = False  # drop without firing the callback
        self.feed(FRAMES[0])
        self.assertIsNotNone(self.bridge._pending_frame)

    def test_discovery_is_republished_on_every_reconnect(self):
        """Heals a broker that lost its retained messages."""
        self.connect_sink()
        first = [t for t in self.sink.topics() if t.endswith("/config")]
        self.drop_sink()
        self.connect_sink()
        both = [t for t in self.sink.topics() if t.endswith("/config")]
        self.assertEqual(len(both), 2 * len(first))

    def test_availability_is_reasserted_on_reconnect(self):
        self.connect_sink()
        self.drop_sink()
        self.connect_sink()
        online = [
            (t, p)
            for t, p, _ in self.sink.published
            if t == self.config.homeassistant.availability_topic
        ]
        self.assertEqual(len(online), 2)
        self.assertTrue(all(p == PAYLOAD_AVAILABLE for _, p in online))

    def test_discovery_and_state_are_retained(self):
        self.connect_sink()
        self.feed(FRAMES[0])
        for topic, _, retain in self.sink.published:
            with self.subTest(topic=topic):
                self.assertTrue(retain, "discovery, availability and state persist")

    def test_outgoing_queue_is_bounded(self):
        """Belt and braces if a reading ever slips past the ready check."""
        from mppt.daemon import MAX_QUEUED_MESSAGES

        self.assertGreater(MAX_QUEUED_MESSAGES, 0, "0 means unlimited in paho")


class TestShutdown(BridgeTestCase):
    def test_publishes_offline_when_connected(self):
        self.connect_sink()
        self.bridge.shutdown()
        last = self.sink.published[-1]
        self.assertEqual(last[0], self.config.homeassistant.availability_topic)
        self.assertEqual(last[1], PAYLOAD_NOT_AVAILABLE)

    def test_does_not_hang_or_raise_when_the_broker_is_gone(self):
        """wait_for_publish raises on a dead link; shutdown must absorb it."""
        self.connect_sink()
        self.drop_sink()
        self.bridge.shutdown()  # must not raise
        self.assertTrue(self.sink.loop_stopped)


class TestSourceBrokerOffline(BridgeTestCase):
    def test_resubscribes_on_every_reconnect(self):
        """The session is clean, so the broker forgets our subscription."""
        subscribed = []
        source = SimpleNamespace(subscribe=lambda t, qos=0: subscribed.append(t))

        self.bridge._on_source_connect(source, None, {}, 0)
        self.bridge._on_source_connect(source, None, {}, 0)

        self.assertEqual(subscribed, [self.config.source.topic] * 2)

    def test_a_refused_connection_does_not_subscribe(self):
        subscribed = []
        source = SimpleNamespace(subscribe=lambda t, qos=0: subscribed.append(t))
        self.bridge._on_source_connect(source, None, {}, 5)  # not authorised
        self.assertEqual(subscribed, [])

    def test_source_outage_leaves_ha_entities_alone(self):
        """No frames is indistinguishable from night. HA keeps the last value."""
        self.connect_sink()
        self.feed(FRAMES[0])
        before = list(self.sink.published)

        self.bridge._on_source_disconnect(None, None, 1)

        self.assertEqual(self.sink.published, before, "nothing published")
        self.assertTrue(self.bridge._sink_ready.is_set(), "HA link stays up")


class TestBadFrames(BridgeTestCase):
    def test_corrupt_frame_is_skipped_not_fatal(self):
        self.connect_sink()
        self.bridge._on_message(None, None, SimpleNamespace(payload=b"\x01\x02\x03"))
        self.assertEqual(self.bridge.frames_rejected, 1)
        self.assertEqual(self.state_messages(), [])

    def test_good_frame_after_a_bad_one_still_publishes(self):
        self.connect_sink()
        self.bridge._on_message(None, None, SimpleNamespace(payload=b"\x01\x02\x03"))
        self.feed(FRAMES[0])
        self.assertEqual(len(self.state_messages()), 1)


if __name__ == "__main__":
    unittest.main()
