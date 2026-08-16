"""Tests for the Home Assistant add-on entrypoint.

The bootstrap renders the add-on's UI options into the INI the daemon reads.
The important property is round-tripping: whatever it writes must be something
mppt.config.load() actually accepts, since a mistake here only shows up as a
crash loop in the add-on log.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import addon_bootstrap  # noqa: E402
from mppt.config import ConfigError, load  # noqa: E402

OPTIONS = {
    "device_serial": "AABBCCDDEEFF",
    "device_module_id": "wifi00000000",
    "device_name": "Shed Array",
    "source_host": "vendor.example.com",
    "source_port": 1883,
    "source_username": "someuser",
    "source_password": "secret",
    "source_topic": "jgy/{module_id}/{serial}/device_state",
    "mqtt_host": "",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "discovery_prefix": "homeassistant",
    "retain": True,
    "entity_id_charge_power": "",
    "entity_id_energy_today": "",
    "log_level": "info",
}

MOSQUITTO = {
    "host": "core-mosquitto",
    "port": 1883,
    "username": "addons",
    "password": "s3cret",
}


class BootstrapTestCase(unittest.TestCase):
    def render(self, options=None, service=None):
        text = addon_bootstrap.build_config(
            dict(OPTIONS, **(options or {})), service or {}
        )
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".ini", delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        os.chmod(handle.name, 0o600)
        self.addCleanup(os.unlink, handle.name)
        return text, load(handle.name)


class TestRoundTrip(BootstrapTestCase):
    """What the bootstrap writes, the daemon must be able to read."""

    def test_rendered_config_loads(self):
        _, config = self.render(service=MOSQUITTO)
        self.assertEqual(config.device.serial, "AABBCCDDEEFF")
        self.assertEqual(config.device.name, "Shed Array")
        self.assertEqual(config.source.host, "vendor.example.com")
        self.assertEqual(config.source.username, "someuser")
        self.assertEqual(config.source.password, "secret")

    def test_topic_placeholders_still_resolve(self):
        _, config = self.render(service=MOSQUITTO)
        self.assertEqual(
            config.source.topic, "jgy/wifi00000000/AABBCCDDEEFF/device_state"
        )

    def test_log_level_is_uppercased_for_the_daemon(self):
        _, config = self.render({"log_level": "debug"}, service=MOSQUITTO)
        self.assertEqual(config.daemon.log_level, "DEBUG")

    def test_retain_false_survives(self):
        _, config = self.render({"retain": False}, service=MOSQUITTO)
        self.assertFalse(config.homeassistant.retain)


class TestMosquittoDiscovery(BootstrapTestCase):
    def test_supervisor_details_are_used_when_fields_are_blank(self):
        _, config = self.render(service=MOSQUITTO)
        self.assertEqual(config.homeassistant.host, "core-mosquitto")
        self.assertEqual(config.homeassistant.username, "addons")
        self.assertEqual(config.homeassistant.password, "s3cret")

    def test_explicit_host_wins_over_the_supervisor(self):
        _, config = self.render(
            {"mqtt_host": "192.0.2.50", "mqtt_username": "me", "mqtt_password": "mine"},
            service=MOSQUITTO,
        )
        self.assertEqual(config.homeassistant.host, "192.0.2.50")
        self.assertEqual(config.homeassistant.username, "me")

    def test_no_broker_at_all_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self.render(service={})
        self.assertIn("Mosquitto", str(ctx.exception))


class TestValidation(BootstrapTestCase):
    def test_missing_serial_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self.render({"device_serial": ""}, service=MOSQUITTO)
        self.assertIn("device_serial", str(ctx.exception))

    def test_missing_source_host_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self.render({"source_host": ""}, service=MOSQUITTO)
        self.assertIn("source_host", str(ctx.exception))

    def test_blank_credentials_render_as_absent(self):
        _, config = self.render(
            {"source_username": "", "source_password": ""}, service=MOSQUITTO
        )
        self.assertIsNone(config.source.username)
        self.assertIsNone(config.source.password)


class TestEntityIdOverrides(BootstrapTestCase):
    def test_overrides_are_written_when_set(self):
        _, config = self.render(
            {
                "entity_id_charge_power": "solarinverterwatts",
                "entity_id_energy_today": "solarinverterdailywatts",
            },
            service=MOSQUITTO,
        )
        self.assertEqual(
            config.homeassistant.object_ids,
            {
                "charge_power": "solarinverterwatts",
                "energy_today": "solarinverterdailywatts",
            },
        )

    def test_blank_overrides_produce_no_entries(self):
        _, config = self.render(service=MOSQUITTO)
        self.assertEqual(config.homeassistant.object_ids, {})


class TestInjection(BootstrapTestCase):
    def test_a_newline_in_a_value_cannot_forge_ini_lines(self):
        """A password is free text from the UI; it must not inject a section."""
        text, config = self.render(
            {"source_password": "hunter2\n[homeassistant]\nhost = evil.example.com"},
            service=MOSQUITTO,
        )
        self.assertEqual(config.homeassistant.host, "core-mosquitto")
        self.assertNotIn("evil.example.com", text.splitlines()[0])
        self.assertIn("hunter2", config.source.password)

    def test_percent_in_a_password_survives(self):
        _, config = self.render({"source_password": "a%b%%c"}, service=MOSQUITTO)
        self.assertEqual(config.source.password, "a%b%%c")


class TestOptionsReading(unittest.TestCase):
    def test_missing_options_file_exits_clearly(self):
        with self.assertRaises(SystemExit) as ctx:
            addon_bootstrap.read_options("/nonexistent/options.json")
        self.assertIn("add-on", str(ctx.exception))

    def test_malformed_options_file_exits_clearly(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        handle.write("{not json")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(SystemExit):
            addon_bootstrap.read_options(handle.name)

    def test_valid_options_file_reads(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(OPTIONS, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(
            addon_bootstrap.read_options(handle.name)["device_serial"], "AABBCCDDEEFF"
        )


class TestDiscoverMqtt(unittest.TestCase):
    def test_no_supervisor_token_returns_empty(self):
        saved = os.environ.pop("SUPERVISOR_TOKEN", None)
        try:
            self.assertEqual(addon_bootstrap.discover_mqtt(), {})
        finally:
            if saved is not None:
                os.environ["SUPERVISOR_TOKEN"] = saved


class TestPermissions(unittest.TestCase):
    def test_written_config_is_not_world_readable(self):
        """It holds two sets of broker credentials."""
        import stat

        text = addon_bootstrap.build_config(dict(OPTIONS), MOSQUITTO)
        path = Path(tempfile.mkdtemp()) / "config.ini"
        addon_bootstrap.write_config(text, str(path))
        self.addCleanup(os.unlink, path)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"mode was {mode:04o}")


if __name__ == "__main__":
    unittest.main()
