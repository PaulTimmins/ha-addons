"""Tests for the Home Assistant discovery and state payloads.

These assert the properties HA actually depends on. Getting `state_class` or
`device_class` wrong does not raise anything -- the entity just silently never
appears in the Energy dashboard, or gets no long-term statistics. So they are
pinned here.
"""

import json
import unittest

from mppt import parse
from mppt.homeassistant import (
    ENERGY_DASHBOARD_SENSOR,
    PAYLOAD_AVAILABLE,
    PAYLOAD_NOT_AVAILABLE,
    SENSORS,
    SENSORS_BY_KEY,
    discovery_messages,
    discovery_payload,
    removal_messages,
    state_payload,
    unique_id,
)

SERIAL = "AABBCCDDEEFF"
FRAME = "01B301000D02025413B703A30146000010C80000000008C60021BD550000000000000000AA"

#: Arguments shared by discovery_payload().
SENSOR_COMMON = dict(
    serial=SERIAL,
    device_name="Solar MPPT",
    state_topic="solar-mppt/AABBCCDDEEFF/state",
    availability_topic="solar-mppt/AABBCCDDEEFF/availability",
)

#: discovery_messages() additionally needs the discovery prefix.
COMMON = dict(SENSOR_COMMON, discovery_prefix="homeassistant")


def payloads(object_ids=None):
    return {
        topic: json.loads(payload)
        for topic, payload in discovery_messages(object_ids=object_ids, **COMMON)
    }


def one(key, object_id=None):
    return discovery_payload(SENSORS_BY_KEY[key], object_id=object_id, **SENSOR_COMMON)


class TestEnergyDashboard(unittest.TestCase):
    """The whole point: a PV source the Energy dashboard will accept."""

    def setUp(self):
        self.sensor = SENSORS_BY_KEY[ENERGY_DASHBOARD_SENSOR]
        self.payload = one(ENERGY_DASHBOARD_SENSOR)

    def test_is_the_lifetime_counter_not_the_daily_one(self):
        """The daily counter resets at midnight; the lifetime one never does."""
        self.assertEqual(ENERGY_DASHBOARD_SENSOR, "energy_total")

    def test_device_class_is_energy(self):
        self.assertEqual(self.payload["device_class"], "energy")

    def test_state_class_is_total_increasing(self):
        self.assertEqual(self.payload["state_class"], "total_increasing")

    def test_unit_is_accepted_by_the_energy_dashboard(self):
        self.assertIn(self.payload["unit_of_measurement"], {"Wh", "kWh", "MWh"})

    def test_has_unique_id_so_it_becomes_a_registry_entity(self):
        """No unique_id means no statistics, which is what broke the REST setup."""
        self.assertTrue(self.payload["unique_id"])


class TestStatisticsRequirements(unittest.TestCase):
    def test_every_sensor_has_a_unique_id(self):
        ids = [p["unique_id"] for p in payloads().values()]
        self.assertEqual(len(ids), len(SENSORS))
        self.assertEqual(len(set(ids)), len(SENSORS), "unique_ids must not collide")

    def test_unique_ids_are_namespaced_by_serial(self):
        for payload in payloads().values():
            self.assertIn(SERIAL, payload["unique_id"])

    def test_no_expire_after_anywhere(self):
        """Overnight silence is normal; expiring would gap the statistics."""
        for topic, payload in payloads().items():
            with self.subTest(topic=topic):
                self.assertNotIn("expire_after", payload)

    def test_energy_sensors_are_total_increasing(self):
        for key in ("energy_today", "energy_total"):
            with self.subTest(key=key):
                payload = one(key)
                self.assertEqual(payload["state_class"], "total_increasing")
                self.assertEqual(payload["device_class"], "energy")
                self.assertEqual(payload["unit_of_measurement"], "kWh")

    def test_live_sensors_are_measurement(self):
        for key in ("charge_power", "pv_voltage", "battery_voltage",
                    "charge_current", "temperature"):
            with self.subTest(key=key):
                payload = one(key)
                self.assertEqual(payload["state_class"], "measurement")

    def test_device_classes_and_units_agree(self):
        expected = {
            "charge_power": ("power", "W"),
            "pv_voltage": ("voltage", "V"),
            "battery_voltage": ("voltage", "V"),
            "charge_current": ("current", "A"),
            "temperature": ("temperature", "°C"),
        }
        for key, (device_class, unit) in expected.items():
            with self.subTest(key=key):
                payload = one(key)
                self.assertEqual(payload["device_class"], device_class)
                self.assertEqual(payload["unit_of_measurement"], unit)


class TestDeviceGrouping(unittest.TestCase):
    def test_all_sensors_share_one_device(self):
        devices = {
            json.dumps(p["device"], sort_keys=True) for p in payloads().values()
        }
        self.assertEqual(len(devices), 1)

    def test_device_identifier_is_stable(self):
        device = next(iter(payloads().values()))["device"]
        self.assertEqual(device["identifiers"], [f"solar_mppt_{SERIAL}"])
        self.assertEqual(device["serial_number"], SERIAL)


class TestAvailability(unittest.TestCase):
    def test_every_sensor_tracks_availability(self):
        for topic, payload in payloads().items():
            with self.subTest(topic=topic):
                self.assertEqual(
                    payload["availability_topic"], COMMON["availability_topic"]
                )
                self.assertEqual(payload["payload_available"], PAYLOAD_AVAILABLE)
                self.assertEqual(
                    payload["payload_not_available"], PAYLOAD_NOT_AVAILABLE
                )


class TestTopics(unittest.TestCase):
    def test_discovery_topics_follow_the_ha_convention(self):
        for topic in payloads():
            with self.subTest(topic=topic):
                parts = topic.split("/")
                self.assertEqual(parts[0], "homeassistant")
                self.assertEqual(parts[1], "sensor")
                self.assertEqual(parts[2], f"solar_mppt_{SERIAL}")
                self.assertEqual(parts[-1], "config")

    def test_custom_discovery_prefix_is_honoured(self):
        common = dict(COMMON, discovery_prefix="ha")
        for topic, _ in discovery_messages(**common):
            self.assertTrue(topic.startswith("ha/sensor/"))

    def test_all_sensors_read_from_one_state_topic(self):
        topics = {p["state_topic"] for p in payloads().values()}
        self.assertEqual(topics, {COMMON["state_topic"]})


class TestEntityIdOverrides(unittest.TestCase):
    def test_object_id_pins_the_entity_id(self):
        overrides = {
            "charge_power": "solarinverterwatts",
            "energy_today": "solarinverterdailywatts",
        }
        result = payloads(object_ids=overrides)
        by_key = {p["unique_id"].rsplit(f"{SERIAL}_", 1)[1]: p for p in result.values()}
        self.assertEqual(by_key["charge_power"]["object_id"], "solarinverterwatts")
        self.assertEqual(
            by_key["energy_today"]["object_id"], "solarinverterdailywatts"
        )

    def test_sensors_without_an_override_have_no_object_id(self):
        result = payloads(object_ids={"charge_power": "solarinverterwatts"})
        by_key = {p["unique_id"].rsplit(f"{SERIAL}_", 1)[1]: p for p in result.values()}
        self.assertNotIn("object_id", by_key["pv_voltage"])

    def test_overrides_do_not_change_unique_ids(self):
        """Renaming the entity must not orphan its history."""
        plain = {p["unique_id"] for p in payloads().values()}
        renamed = {
            p["unique_id"]
            for p in payloads(object_ids={"charge_power": "whatever"}).values()
        }
        self.assertEqual(plain, renamed)


class TestValueTemplates(unittest.TestCase):
    def test_every_template_resolves_against_the_state_payload(self):
        state = state_payload(parse(FRAME))
        for payload in payloads().values():
            template = payload["value_template"]
            key = template.replace("{{ value_json.", "").replace(" }}", "")
            with self.subTest(key=key):
                self.assertIn(key, state)

    def test_state_payload_has_no_extra_keys(self):
        """Anything published but not declared is dead weight in every message."""
        state = state_payload(parse(FRAME))
        self.assertEqual(set(state), {s.key for s in SENSORS})

    def test_state_payload_is_json_serialisable(self):
        json.loads(json.dumps(state_payload(parse(FRAME))))


class TestStateValues(unittest.TestCase):
    def setUp(self):
        self.state = state_payload(parse(FRAME))

    def test_energy_is_converted_to_the_declared_unit(self):
        """Frame counts Wh; discovery declares kWh. A 1000x error here would
        show up as 2.2 GWh of lifetime solar production."""
        self.assertEqual(self.state["energy_total"], 2211.157)
        self.assertEqual(self.state["energy_today"], 2.246)

    def test_live_values_match_the_app(self):
        self.assertEqual(self.state["charge_power"], 469.9)
        self.assertEqual(self.state["pv_voltage"], 59.6)
        self.assertEqual(self.state["battery_voltage"], 50.47)


class TestRemoval(unittest.TestCase):
    def test_removal_targets_the_same_topics(self):
        created = set(payloads())
        removed = {t for t, _ in removal_messages(SERIAL, "homeassistant")}
        self.assertEqual(created, removed)

    def test_removal_payloads_are_empty(self):
        """An empty retained payload is how HA is told to delete an entity."""
        for _, payload in removal_messages(SERIAL, "homeassistant"):
            self.assertEqual(payload, "")


class TestHelpers(unittest.TestCase):
    def test_unique_id_is_deterministic(self):
        self.assertEqual(
            unique_id(SERIAL, "energy_total"), f"solar_mppt_{SERIAL}_energy_total"
        )


if __name__ == "__main__":
    unittest.main()
