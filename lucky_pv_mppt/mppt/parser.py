"""Parser for JGY / inverteriot MPPT solar controller ``device_state`` frames.

Frames arrive on MQTT topic ``jgy/<wifi-module>/<mac>/device_state`` as a
37-byte binary blob (some bridges republish it as an ASCII hex string; both
forms are accepted).

Layout, byte offsets into the frame::

     0      device address              (0x01)
     1      function code               (0xB3)
     2..5   unknown, so far 01 00 0D 02
     6..7   PV array voltage      u16   x0.1  V
     8..9   battery voltage       u16   x0.01 V
    10..11  charge current        u16   x0.01 A
    12..13  temperature           u16   x0.1  degC   (tentative, see README)
    14..15  unknown, so far 0
    16..17  unknown, so far 0x10C8
    18..21  unknown, so far 0
    22..23  generation today      u16   Wh
    24..27  generation total      u32   Wh
    28..35  unknown, so far 0
    36      checksum: sum of bytes 0..35, low 8 bits

Charge power is not transmitted; the vendor app derives it as
``battery_voltage * charge_current``, which this module reproduces.

The unidentified fields are *observed* constants, not guaranteed ones. Parsing
never fails because one of them changes -- a field coming alive is new
information, not a corrupt frame. Their values are always carried on
``MPPTFrame.unknown``, and ``MPPTFrame.changed_unknowns()`` reports which ones
have moved off their previously observed values so a caller can log it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Dict, Union

FRAME_LENGTH = 37
DEVICE_ADDRESS = 0x01
FUNCTION_CODE = 0xB3

#: Offsets of the u16s that have not been identified yet. Their values are
#: always carried through on ``MPPTFrame.unknown`` rather than dropped -- that
#: is how the remaining fields will get identified.
_UNKNOWN_U16_OFFSETS = (2, 4, 14, 16, 18, 20, 28, 30, 32, 34)

#: What each unknown field has read in every frame seen so far. Used only to
#: flag movement for logging; a value that differs is never an error.
BASELINE_UNKNOWNS = {
    "u16@2": 0x0100,
    "u16@4": 0x0D02,
    "u16@14": 0x0000,
    "u16@16": 0x10C8,
    "u16@18": 0x0000,
    "u16@20": 0x0000,
    "u16@28": 0x0000,
    "u16@30": 0x0000,
    "u16@32": 0x0000,
    "u16@34": 0x0000,
}


class FrameError(ValueError):
    """Raised when a payload is not a valid controller frame."""


class FrameLengthError(FrameError):
    pass


class FrameHeaderError(FrameError):
    pass


class ChecksumError(FrameError):
    pass


@dataclass(frozen=True)
class MPPTFrame:
    """A decoded ``device_state`` frame."""

    pv_voltage: float
    """PV array voltage, volts."""

    battery_voltage: float
    """Battery voltage, volts."""

    charge_current: float
    """Battery charge current, amps."""

    temperature: float
    """Controller temperature, degrees Celsius."""

    energy_today: int
    """Generation since midnight, watt-hours."""

    energy_total: int
    """Lifetime generation, watt-hours."""

    unknown: Dict[str, int] = field(default_factory=dict)
    """Raw u16 values of the not-yet-identified fields, keyed by byte offset."""

    raw: bytes = b""
    """The frame exactly as received."""

    def changed_unknowns(self) -> Dict[str, int]:
        """Unidentified fields that differ from what we have seen before.

        Informational only -- a non-empty result means the controller started
        reporting something new and is worth logging, not that the frame is
        bad. Decoding of the known fields is unaffected.
        """
        return {
            key: value
            for key, value in self.unknown.items()
            if BASELINE_UNKNOWNS.get(key) != value
        }

    @property
    def charge_power(self) -> float:
        """Charge power in watts, derived the same way the vendor app does it."""
        return round(self.battery_voltage * self.charge_current, 1)

    @property
    def energy_today_kwh(self) -> float:
        return round(self.energy_today / 1000, 3)

    @property
    def energy_total_kwh(self) -> float:
        return round(self.energy_total / 1000, 3)

    def to_dict(self, include_unknown: bool = False) -> Dict[str, Any]:
        """Flat dict of the decoded values, ready to publish as JSON."""
        out: Dict[str, Any] = {
            "pv_voltage": self.pv_voltage,
            "battery_voltage": self.battery_voltage,
            "charge_current": self.charge_current,
            "charge_power": self.charge_power,
            "temperature": self.temperature,
            "energy_today": self.energy_today,
            "energy_today_kwh": self.energy_today_kwh,
            "energy_total": self.energy_total,
            "energy_total_kwh": self.energy_total_kwh,
        }
        if include_unknown:
            out["unknown"] = dict(self.unknown)
        return out


def checksum(data: bytes) -> int:
    """Frame checksum: the low 8 bits of the sum of every preceding byte."""
    return sum(data) & 0xFF


def _coerce(payload: Union[bytes, bytearray, memoryview, str]) -> bytes:
    if isinstance(payload, str):
        text = "".join(payload.split())
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise FrameError(f"payload is not valid hex: {payload!r}") from exc
    if isinstance(payload, (bytearray, memoryview)):
        return bytes(payload)
    if isinstance(payload, bytes):
        return payload
    raise FrameError(f"unsupported payload type: {type(payload).__name__}")


def parse(
    payload: Union[bytes, bytearray, memoryview, str],
    *,
    verify_checksum: bool = True,
    verify_header: bool = True,
) -> MPPTFrame:
    """Decode a ``device_state`` payload.

    Args:
        payload: Raw frame bytes, or the same bytes as a hex string.
        verify_checksum: Reject frames whose trailing checksum does not match.
        verify_header: Reject frames whose device address and function code are
            not the expected ones. Only those two bytes are checked -- the rest
            of the leading bytes are treated as data, so a frame stays valid if
            the controller ever changes them.

    Raises:
        FrameError: The payload is malformed. Callers polling a live broker
            should catch this and skip the message rather than crash.
    """
    raw = _coerce(payload)

    if len(raw) != FRAME_LENGTH:
        raise FrameLengthError(
            f"expected {FRAME_LENGTH} bytes, got {len(raw)}: {raw.hex().upper()}"
        )

    if verify_header and (raw[0], raw[1]) != (DEVICE_ADDRESS, FUNCTION_CODE):
        raise FrameHeaderError(
            f"expected address {DEVICE_ADDRESS:02X} function {FUNCTION_CODE:02X}, "
            f"got {raw[0]:02X} {raw[1]:02X}"
        )

    if verify_checksum:
        expected = checksum(raw[:-1])
        if raw[-1] != expected:
            raise ChecksumError(
                f"checksum mismatch: frame carries {raw[-1]:02X}, computed {expected:02X}"
            )

    u16 = lambda off: struct.unpack_from(">H", raw, off)[0]  # noqa: E731

    return MPPTFrame(
        pv_voltage=round(u16(6) * 0.1, 1),
        battery_voltage=round(u16(8) * 0.01, 2),
        charge_current=round(u16(10) * 0.01, 2),
        temperature=round(u16(12) * 0.1, 1),
        energy_today=u16(22),
        energy_total=struct.unpack_from(">I", raw, 24)[0],
        unknown={f"u16@{off}": u16(off) for off in _UNKNOWN_U16_OFFSETS},
        raw=raw,
    )
