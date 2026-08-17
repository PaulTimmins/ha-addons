"""Tests for the MPPT frame parser.

The SAMPLES below are frames captured off the live broker. SCREENSHOT_FRAME is
the one that was in flight when a screenshot of the vendor app was taken, so
the values the app displayed act as ground truth for the field map.
"""

import unittest

from mppt import ChecksumError, FrameHeaderError, FrameLengthError, checksum, parse

SAMPLES = [
    "01B301000D02024F13E506A80147000010C80000000008C90021BD580000000000000000E2",
    "01B301000D02024413E906B90148000010C80000000008CC0021BD5B0000000000000000F3",
    "01B301000D02024F13E4066A0148000010C80000000008CB0021BD5A0000000000000000A8",
    "01B301000D02024F13E4066A0148000010C80000000008CB0021BD5A0000000000000000A8",
    "01B301000D02024F13E506A80147000010C80000000008C90021BD580000000000000000E2",
    "01B301000D02025813D905F10147000010C80000000008C80021BD57000000000000000025",
    "01B301000D02025013D205AA0147000010C80000000008C70021BD560000000000000000CD",
    "01B301000D02025413B703A30146000010C80000000008C60021BD550000000000000000AA",
]

SCREENSHOT_FRAME = SAMPLES[7]


class TestFieldMapping(unittest.TestCase):
    """Field map, checked against what the vendor app displayed."""

    def setUp(self):
        self.frame = parse(SCREENSHOT_FRAME)

    def test_pv_voltage(self):
        # App: "PV: 59.6V"
        self.assertEqual(self.frame.pv_voltage, 59.6)

    def test_battery_voltage(self):
        # App: "Bat.: 50.47V"
        self.assertEqual(self.frame.battery_voltage, 50.47)

    def test_charge_current(self):
        # App: "9.88A Max Charge Current" -- instantaneous must not exceed it.
        self.assertEqual(self.frame.charge_current, 9.31)
        self.assertLessEqual(self.frame.charge_current, 9.88)

    def test_charge_power_matches_app(self):
        # App: "0.47KW PV Power". 50.47 V * 9.31 A = 469.9 W.
        self.assertEqual(self.frame.charge_power, 469.9)
        self.assertEqual(round(self.frame.charge_power / 1000, 2), 0.47)

    def test_energy_today_matches_app(self):
        # App: "2.25KWH Generation today"
        self.assertEqual(self.frame.energy_today, 2246)
        self.assertEqual(round(self.frame.energy_today_kwh, 2), 2.25)

    def test_energy_total_matches_app(self):
        # App: "2.21MWH Total Generation"
        self.assertEqual(self.frame.energy_total, 0x0021BD55)
        self.assertEqual(self.frame.energy_total, 2211157)
        self.assertEqual(round(self.frame.energy_total_kwh / 1000, 2), 2.21)

    def test_temperature(self):
        self.assertEqual(self.frame.temperature, 32.6)


class TestAllSamples(unittest.TestCase):
    def test_every_sample_parses(self):
        for hex_frame in SAMPLES:
            with self.subTest(frame=hex_frame):
                parse(hex_frame)

    def test_checksum_is_sum_of_preceding_bytes(self):
        for hex_frame in SAMPLES:
            with self.subTest(frame=hex_frame):
                raw = bytes.fromhex(hex_frame)
                self.assertEqual(checksum(raw[:-1]), raw[-1])

    def test_values_are_physically_plausible(self):
        for hex_frame in SAMPLES:
            with self.subTest(frame=hex_frame):
                f = parse(hex_frame)
                # 48 V nominal LiFePO4 bank on a ~60 V array.
                self.assertTrue(40 <= f.battery_voltage <= 60, f.battery_voltage)
                self.assertTrue(0 <= f.pv_voltage <= 150, f.pv_voltage)
                self.assertTrue(0 <= f.charge_current <= 100, f.charge_current)
                self.assertTrue(-20 <= f.temperature <= 100, f.temperature)

    def test_totals_track_daily_generation(self):
        """energy_total and energy_today move together, 1 Wh per 1 Wh."""
        frames = [parse(h) for h in SAMPLES]
        base = frames[0]
        for f in frames[1:]:
            with self.subTest(frame=f.raw.hex()):
                self.assertEqual(
                    f.energy_total - base.energy_total,
                    f.energy_today - base.energy_today,
                )

    def test_identical_frames_decode_identically(self):
        self.assertEqual(parse(SAMPLES[2]).to_dict(), parse(SAMPLES[3]).to_dict())

    def test_samples_match_the_recorded_baseline(self):
        for hex_frame in SAMPLES:
            with self.subTest(frame=hex_frame):
                self.assertEqual(parse(hex_frame).changed_unknowns(), {})


class TestToleratesNewValues(unittest.TestCase):
    """A field coming alive is new information, never a failure.

    Every unidentified byte in this frame is an observed constant, not a
    guaranteed one. If the controller starts populating one -- a load is
    connected, a fault flag is raised, a setting is changed -- the frame must
    still decode and the known values must still be correct.
    """

    @staticmethod
    def _reseal(hex_frame, offset, value):
        """Write a u16 into a frame and fix up the checksum."""
        raw = bytearray(bytes.fromhex(hex_frame))
        raw[offset : offset + 2] = value.to_bytes(2, "big")
        raw[-1] = sum(raw[:-1]) & 0xFF
        return bytes(raw)

    def test_every_unknown_field_may_change(self):
        for offset in (2, 4, 14, 16, 18, 20, 28, 30, 32, 34):
            with self.subTest(offset=offset):
                frame = parse(self._reseal(SCREENSHOT_FRAME, offset, 0x1234))
                self.assertEqual(frame.unknown[f"u16@{offset}"], 0x1234)
                # The identified fields are untouched.
                self.assertEqual(frame.battery_voltage, 50.47)
                self.assertEqual(frame.charge_current, 9.31)
                self.assertEqual(frame.energy_today, 2246)

    def test_change_is_reported_for_logging(self):
        frame = parse(self._reseal(SCREENSHOT_FRAME, 28, 0x0064))
        self.assertEqual(frame.changed_unknowns(), {"u16@28": 0x0064})

    def test_other_fields_are_not_reported_as_changed(self):
        frame = parse(self._reseal(SCREENSHOT_FRAME, 28, 0x0064))
        self.assertNotIn("u16@30", frame.changed_unknowns())

    def test_leading_bytes_may_change(self):
        """Only address and function code identify the frame; 2..5 are data."""
        raw = bytearray(bytes.fromhex(SCREENSHOT_FRAME))
        raw[2:6] = b"\x09\x09\x09\x09"
        raw[-1] = sum(raw[:-1]) & 0xFF
        frame = parse(bytes(raw))
        self.assertEqual(frame.battery_voltage, 50.47)
        self.assertNotEqual(frame.changed_unknowns(), {})


#: Captured live off the broker at night. The controller publishes the frame as
#: ASCII hex *text*, so an MQTT payload is b"01B301..." not b"\x01\xb3...".
#: Byte 4 also differs from the daytime captures, which is what revealed it as a
#: live status field rather than part of a fixed header.
NIGHT_FRAMES_AS_PUBLISHED = [
    b"01B301000902023E13940001011F000010C8000000000B7C0021C00B000000000000000013",
    b"01B301000002023613940002011F000010C8000000000B7C0021C00B000000000000000003",
    b"01B301000902024613940001011F000010C8000000000B7C0021C00B00000000000000001B",
    b"01B301000002023413940002011E000010C8000000000B7C0021C00B000000000000000000",
    b"01B301000902025B13940001011F000010C8000000000B7C0021C00B000000000000000030",
]


class TestHexTextPayloads(unittest.TestCase):
    """The device publishes hex text, not binary.

    A 37-byte frame arrives as a 74-byte ASCII payload. Treating it as binary
    fails the length check, which is how every live frame was rejected.
    """

    def test_payloads_as_published_parse(self):
        for payload in NIGHT_FRAMES_AS_PUBLISHED:
            with self.subTest(payload=payload[:16]):
                self.assertEqual(len(payload), 74)
                parse(payload)

    def test_hex_text_and_binary_agree(self):
        for payload in NIGHT_FRAMES_AS_PUBLISHED:
            with self.subTest(payload=payload[:16]):
                binary = bytes.fromhex(payload.decode())
                self.assertEqual(parse(payload).to_dict(), parse(binary).to_dict())

    def test_binary_is_preferred_when_length_matches(self):
        """A real 37-byte frame must never be reinterpreted as text."""
        binary = bytes.fromhex(SCREENSHOT_FRAME)
        self.assertEqual(parse(binary).raw, binary)

    def test_lowercase_hex_text_works(self):
        payload = NIGHT_FRAMES_AS_PUBLISHED[0].lower()
        self.assertEqual(parse(payload).battery_voltage, 50.12)

    def test_trailing_whitespace_is_tolerated(self):
        payload = NIGHT_FRAMES_AS_PUBLISHED[0] + b"\r\n"
        self.assertEqual(parse(payload).battery_voltage, 50.12)

    def test_night_values_are_plausible(self):
        """Dark: open-circuit PV, no meaningful current, cooler than daytime."""
        for payload in NIGHT_FRAMES_AS_PUBLISHED:
            with self.subTest(payload=payload[:16]):
                f = parse(payload)
                self.assertEqual(f.battery_voltage, 50.12)
                self.assertLess(f.charge_current, 0.1)
                self.assertLess(f.temperature, 30.0)

    def test_temperature_falls_overnight(self):
        """Confirms offset 12-13 is temperature: it tracks ambient, not load."""
        day = parse(SCREENSHOT_FRAME).temperature
        night = parse(NIGHT_FRAMES_AS_PUBLISHED[0]).temperature
        self.assertGreater(day, night)
        self.assertAlmostEqual(day, 32.6, places=1)
        self.assertAlmostEqual(night, 28.7, places=1)

    def test_energy_only_moves_forward(self):
        """Night totals must exceed the earlier daytime capture."""
        day = parse(SCREENSHOT_FRAME)
        night = parse(NIGHT_FRAMES_AS_PUBLISHED[0])
        self.assertGreater(night.energy_total, day.energy_total)
        self.assertEqual(
            night.energy_total - day.energy_total,
            night.energy_today - day.energy_today,
        )

    def test_varying_status_byte_is_not_reported_as_a_change(self):
        """Offset 4 moves with charge state; alerting on it would be noise."""
        for payload in NIGHT_FRAMES_AS_PUBLISHED:
            with self.subTest(payload=payload[:16]):
                self.assertEqual(parse(payload).changed_unknowns(), {})

    def test_varying_status_byte_is_still_carried(self):
        values = {parse(p).unknown["u16@4"] for p in NIGHT_FRAMES_AS_PUBLISHED}
        self.assertEqual(values, {0x0902, 0x0002})


class TestInputForms(unittest.TestCase):
    def test_bytes_and_hex_agree(self):
        as_hex = parse(SCREENSHOT_FRAME)
        as_bytes = parse(bytes.fromhex(SCREENSHOT_FRAME))
        self.assertEqual(as_hex.to_dict(), as_bytes.to_dict())

    def test_accepts_bytearray_and_memoryview(self):
        raw = bytes.fromhex(SCREENSHOT_FRAME)
        expected = parse(raw).to_dict()
        self.assertEqual(parse(bytearray(raw)).to_dict(), expected)
        self.assertEqual(parse(memoryview(raw)).to_dict(), expected)

    def test_accepts_lowercase_and_spaced_hex(self):
        spaced = " ".join(
            SCREENSHOT_FRAME[i : i + 2].lower()
            for i in range(0, len(SCREENSHOT_FRAME), 2)
        )
        self.assertEqual(parse(spaced).to_dict(), parse(SCREENSHOT_FRAME).to_dict())

    def test_raw_is_preserved(self):
        self.assertEqual(parse(SCREENSHOT_FRAME).raw.hex().upper(), SCREENSHOT_FRAME)


class TestRejection(unittest.TestCase):
    def test_rejects_short_frame(self):
        with self.assertRaises(FrameLengthError):
            parse(SCREENSHOT_FRAME[:-2])

    def test_rejects_long_frame(self):
        with self.assertRaises(FrameLengthError):
            parse(SCREENSHOT_FRAME + "00")

    def test_rejects_bad_checksum(self):
        with self.assertRaises(ChecksumError):
            parse(SCREENSHOT_FRAME[:-2] + "AB")

    def test_rejects_foreign_address_or_function_code(self):
        for prefix in ("02B3", "01B4", "02B4"):
            with self.subTest(prefix=prefix):
                raw = bytearray(bytes.fromhex(prefix + SCREENSHOT_FRAME[4:]))
                raw[-1] = sum(raw[:-1]) & 0xFF  # valid checksum, wrong frame type
                with self.assertRaises(FrameHeaderError):
                    parse(bytes(raw))

    def test_rejects_non_hex_text(self):
        with self.assertRaises(ValueError):
            parse("not a frame")

    def test_corrupt_byte_is_caught_by_checksum(self):
        """Flip one bit in the battery voltage; the checksum must notice."""
        raw = bytearray(bytes.fromhex(SCREENSHOT_FRAME))
        raw[9] ^= 0x01
        with self.assertRaises(ChecksumError):
            parse(bytes(raw))

    def test_no_verify_decodes_anyway(self):
        broken = SCREENSHOT_FRAME[:-2] + "00"
        frame = parse(broken, verify_checksum=False)
        self.assertEqual(frame.battery_voltage, 50.47)


class TestOutput(unittest.TestCase):
    def test_to_dict_keys(self):
        d = parse(SCREENSHOT_FRAME).to_dict()
        self.assertEqual(
            set(d),
            {
                "pv_voltage",
                "battery_voltage",
                "charge_current",
                "charge_power",
                "temperature",
                "energy_today",
                "energy_today_kwh",
                "energy_total",
                "energy_total_kwh",
            },
        )

    def test_to_dict_is_json_serialisable(self):
        import json

        json.loads(json.dumps(parse(SCREENSHOT_FRAME).to_dict(include_unknown=True)))

    def test_unknown_excluded_by_default(self):
        self.assertNotIn("unknown", parse(SCREENSHOT_FRAME).to_dict())


if __name__ == "__main__":
    unittest.main()
