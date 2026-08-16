"""Tests for config-file loading."""

import os
import tempfile
import unittest
from pathlib import Path

from mppt.config import ConfigError, check_permissions, load

FULL = """
[device]
serial = AABBCCDDEEFF
module_id = wifi00000000
name = Shed Array

[source]
host = vendor.example.com
port = 8883
username = someuser
password = secret
topic = jgy/{module_id}/{serial}/device_state
client_id = my-client
keepalive = 30

[homeassistant]
enabled = true
host = 192.0.2.10
port = 1884
username = hauser
password = hapass
client_id = ha-client
keepalive = 45
discovery_prefix = ha
state_topic = solar/{serial}/state
availability_topic = solar/{serial}/avail
retain = false

[entity_ids]
charge_power = solarinverterwatts
energy_today = solarinverterdailywatts

[daemon]
log_level = debug
log_file = /var/log/solar-mppt.log
pid_file = /run/solar-mppt.pid
"""

MINIMAL = """
[device]
serial = AABBCCDDEEFF

[source]
host = vendor.example.com
topic = jgy/wifi00000000/AABBCCDDEEFF/device_state

[homeassistant]
host = 192.0.2.10
"""


class ConfigFileTestCase(unittest.TestCase):
    def write(self, text, mode=0o600):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".ini", delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        os.chmod(handle.name, mode)
        self.addCleanup(os.unlink, handle.name)
        return handle.name


class TestLoad(ConfigFileTestCase):
    def test_reads_device(self):
        config = load(self.write(FULL))
        self.assertEqual(config.device.serial, "AABBCCDDEEFF")
        self.assertEqual(config.device.module_id, "wifi00000000")
        self.assertEqual(config.device.name, "Shed Array")

    def test_reads_source_broker(self):
        source = load(self.write(FULL)).source
        self.assertEqual(source.host, "vendor.example.com")
        self.assertEqual(source.port, 8883)
        self.assertEqual(source.username, "someuser")
        self.assertEqual(source.password, "secret")
        self.assertEqual(source.client_id, "my-client")
        self.assertEqual(source.keepalive, 30)

    def test_reads_homeassistant_broker(self):
        ha = load(self.write(FULL)).homeassistant
        self.assertTrue(ha.enabled)
        self.assertEqual(ha.host, "192.0.2.10")
        self.assertEqual(ha.port, 1884)
        self.assertEqual(ha.username, "hauser")
        self.assertEqual(ha.password, "hapass")
        self.assertEqual(ha.client_id, "ha-client")
        self.assertEqual(ha.keepalive, 45)
        self.assertEqual(ha.discovery_prefix, "ha")
        self.assertFalse(ha.retain)

    def test_the_two_brokers_stay_separate(self):
        """The vendor broker and your own broker are different machines."""
        config = load(self.write(FULL))
        self.assertNotEqual(config.source.host, config.homeassistant.host)
        self.assertNotEqual(config.source.password, config.homeassistant.password)
        self.assertNotEqual(config.source.client_id, config.homeassistant.client_id)

    def test_reads_daemon(self):
        daemon = load(self.write(FULL)).daemon
        self.assertEqual(daemon.log_level, "DEBUG")
        self.assertEqual(daemon.log_file, "/var/log/solar-mppt.log")
        self.assertEqual(daemon.pid_file, "/run/solar-mppt.pid")

    def test_defaults_when_only_required_keys_present(self):
        config = load(self.write(MINIMAL))
        self.assertEqual(config.source.port, 1883)
        self.assertEqual(config.source.keepalive, 60)
        self.assertIsNone(config.source.username)
        self.assertEqual(config.source.client_id, "solar-mppt-AABBCCDDEEFF")
        self.assertEqual(config.device.name, "Lucky PV MPPT")
        self.assertEqual(config.homeassistant.discovery_prefix, "homeassistant")
        self.assertEqual(
            config.homeassistant.state_topic, "solar-mppt/AABBCCDDEEFF/state"
        )
        self.assertEqual(
            config.homeassistant.availability_topic,
            "solar-mppt/AABBCCDDEEFF/availability",
        )
        self.assertEqual(config.daemon.log_level, "INFO")
        self.assertIsNone(config.daemon.log_file)

    def test_retain_defaults_on(self):
        """So HA restores the last reading immediately after a restart."""
        self.assertTrue(load(self.write(MINIMAL)).homeassistant.retain)

    def test_source_and_ha_client_ids_differ_by_default(self):
        """Two connections sharing a client id would kick each other off."""
        config = load(self.write(MINIMAL))
        self.assertNotEqual(
            config.source.client_id, config.homeassistant.client_id
        )

    def test_blank_password_becomes_none(self):
        config = load(self.write(MINIMAL + "password =\n"))
        self.assertIsNone(config.homeassistant.password)

    def test_password_with_percent_survives(self):
        """configparser interpolation must not mangle the password."""
        config = load(self.write(MINIMAL + "password = a%%b%s100\n"))
        self.assertEqual(config.homeassistant.password, "a%%b%s100")

    def test_records_source_path(self):
        path = self.write(MINIMAL)
        self.assertEqual(str(load(path).path), path)

    def test_the_shipped_example_is_valid(self):
        """The example must parse, and must not carry anyone's real details."""
        example = Path(__file__).resolve().parent.parent / "config.example.ini"
        config = load(str(example))
        self.assertTrue(config.source.topic.endswith("/device_state"))
        self.assertIsNone(config.source.password)
        self.assertIsNone(config.homeassistant.password)
        text = example.read_text()
        for secret in ("inverteriot", "device_client", "4CEBD683EEC0", "wifi20220903"):
            self.assertNotIn(secret, text)


class TestHomeAssistantOptional(ConfigFileTestCase):
    def test_can_be_disabled_without_a_host(self):
        text = MINIMAL.replace(
            "[homeassistant]\nhost = 192.0.2.10",
            "[homeassistant]\nenabled = false",
        )
        config = load(self.write(text))
        self.assertFalse(config.homeassistant.enabled)

    def test_enabled_without_host_is_an_error(self):
        text = MINIMAL.replace(
            "[homeassistant]\nhost = 192.0.2.10", "[homeassistant]\nenabled = true"
        )
        with self.assertRaises(ConfigError) as ctx:
            load(self.write(text))
        self.assertIn("host", str(ctx.exception))

    def test_non_boolean_enabled_is_reported(self):
        with self.assertRaises(ConfigError):
            load(self.write(MINIMAL + "enabled = perhaps\n"))


class TestEntityIds(ConfigFileTestCase):
    def test_overrides_are_read(self):
        ha = load(self.write(FULL)).homeassistant
        self.assertEqual(ha.object_ids["charge_power"], "solarinverterwatts")
        self.assertEqual(ha.object_ids["energy_today"], "solarinverterdailywatts")

    def test_absent_section_means_no_overrides(self):
        self.assertEqual(load(self.write(MINIMAL)).homeassistant.object_ids, {})

    def test_blank_value_is_not_an_override(self):
        text = MINIMAL + "\n[entity_ids]\ncharge_power =\n"
        self.assertEqual(load(self.write(text)).homeassistant.object_ids, {})

    def test_unknown_sensor_key_is_rejected(self):
        """A typo here would otherwise be silently ignored."""
        text = MINIMAL + "\n[entity_ids]\nchagre_power = oops\n"
        with self.assertRaises(ConfigError) as ctx:
            load(self.write(text))
        self.assertIn("chagre_power", str(ctx.exception))


class TestTopicTemplate(ConfigFileTestCase):
    def test_placeholders_are_substituted(self):
        config = load(self.write(FULL))
        self.assertEqual(
            config.source.topic, "jgy/wifi00000000/AABBCCDDEEFF/device_state"
        )
        self.assertEqual(
            config.homeassistant.state_topic, "solar/AABBCCDDEEFF/state"
        )
        self.assertEqual(
            config.homeassistant.availability_topic, "solar/AABBCCDDEEFF/avail"
        )

    def test_literal_topic_passes_through(self):
        config = load(self.write(MINIMAL))
        self.assertEqual(
            config.source.topic, "jgy/wifi00000000/AABBCCDDEEFF/device_state"
        )

    def test_unknown_placeholder_is_reported(self):
        bad = MINIMAL.replace(
            "topic = jgy/wifi00000000/AABBCCDDEEFF/device_state",
            "topic = jgy/{nope}/device_state",
        )
        with self.assertRaises(ConfigError) as ctx:
            load(self.write(bad))
        self.assertIn("nope", str(ctx.exception))


class TestRejection(ConfigFileTestCase):
    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load("/nonexistent/config.ini")

    def test_missing_device_section(self):
        text = MINIMAL.replace("[device]\nserial = AABBCCDDEEFF\n", "")
        with self.assertRaises(ConfigError):
            load(self.write(text))

    def test_missing_serial(self):
        text = MINIMAL.replace("serial = AABBCCDDEEFF", "")
        with self.assertRaises(ConfigError):
            load(self.write(text))

    def test_missing_source_section(self):
        text = MINIMAL.replace(
            "[source]\nhost = vendor.example.com\n"
            "topic = jgy/wifi00000000/AABBCCDDEEFF/device_state\n",
            "",
        )
        with self.assertRaises(ConfigError):
            load(self.write(text))

    def test_missing_homeassistant_section(self):
        text = MINIMAL.replace("[homeassistant]\nhost = 192.0.2.10\n", "")
        with self.assertRaises(ConfigError):
            load(self.write(text))

    def test_missing_source_host(self):
        text = MINIMAL.replace("host = vendor.example.com", "")
        with self.assertRaises(ConfigError):
            load(self.write(text))

    def test_missing_source_topic(self):
        text = MINIMAL.replace(
            "topic = jgy/wifi00000000/AABBCCDDEEFF/device_state", ""
        )
        with self.assertRaises(ConfigError):
            load(self.write(text))

    def test_non_integer_port(self):
        with self.assertRaises(ConfigError):
            load(self.write(MINIMAL + "port = not-a-number\n"))

    def test_malformed_ini(self):
        with self.assertRaises(ConfigError):
            load(self.write("this is not ini\n"))


class TestPermissions(ConfigFileTestCase):
    def test_private_file_is_quiet(self):
        self.assertIsNone(check_permissions(Path(self.write(MINIMAL, mode=0o600))))

    def test_world_readable_file_warns(self):
        warning = check_permissions(Path(self.write(MINIMAL, mode=0o644)))
        self.assertIsNotNone(warning)
        self.assertIn("chmod 600", warning)


if __name__ == "__main__":
    unittest.main()
