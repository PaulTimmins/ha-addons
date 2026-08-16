"""Home Assistant MQTT Discovery payloads.

Pure payload construction -- no network, no paho -- so the exact JSON that will
hit the broker can be asserted in tests and dumped from the CLI.

Why discovery rather than the REST API: ``POST /api/states/...`` only pushes a
value into the state machine. The result has no unique_id, no device registry
entry and no config entry, so it cannot be renamed or placed in an area, it
disappears on restart, and -- the reason the Energy dashboard never worked --
it gets no long-term statistics. Discovery creates real registry entities.

One state topic carries a JSON document for the whole device; each sensor picks
its value out with a ``value_template``. That way a frame updates every sensor
from a single retained message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .parser import MPPTFrame

MANUFACTURER = "inverteriot"
MODEL = "JGY MPPT Solar Charge Controller"

PAYLOAD_AVAILABLE = "online"
PAYLOAD_NOT_AVAILABLE = "offline"


@dataclass(frozen=True)
class Sensor:
    """One discovered entity."""

    key: str
    """Matches the field name in the state JSON."""

    name: str
    device_class: Optional[str]
    state_class: str
    unit: str
    precision: int
    icon: Optional[str] = None


#: Energy sensors are ``total_increasing`` in kWh with ``device_class: energy``,
#: which is what makes them eligible as an Energy dashboard PV source.
SENSORS: List[Sensor] = [
    Sensor("charge_power", "PV Power", "power", "measurement", "W", 1),
    Sensor("pv_voltage", "PV Voltage", "voltage", "measurement", "V", 1),
    Sensor("battery_voltage", "Battery Voltage", "voltage", "measurement", "V", 2),
    Sensor("charge_current", "Charge Current", "current", "measurement", "A", 2),
    Sensor("temperature", "Temperature", "temperature", "measurement", "°C", 1),
    Sensor("energy_today", "Generation Today", "energy", "total_increasing", "kWh", 3),
    Sensor("energy_total", "Generation Total", "energy", "total_increasing", "kWh", 3),
]

SENSORS_BY_KEY = {sensor.key: sensor for sensor in SENSORS}

#: The Energy dashboard should be pointed at the lifetime counter, not the
#: daily one: it never resets, so the statistics engine has nothing to
#: misinterpret. Verified against the captures -- lifetime and daily move
#: 1 Wh for 1 Wh.
ENERGY_DASHBOARD_SENSOR = "energy_total"


def node_id(serial: str) -> str:
    """Discovery node id. Groups this device's topics under one subtree."""
    return f"solar_mppt_{serial}"


def unique_id(serial: str, key: str) -> str:
    """Stable per-entity id. Changing this orphans the entity in HA."""
    return f"solar_mppt_{serial}_{key}"


def discovery_topic(prefix: str, serial: str, key: str) -> str:
    return f"{prefix}/sensor/{node_id(serial)}/{key}/config"


def device_block(serial: str, name: str) -> Dict[str, Any]:
    """Shared device registry entry -- what groups the sensors into one device."""
    return {
        "identifiers": [node_id(serial)],
        "name": name,
        "manufacturer": MANUFACTURER,
        "model": MODEL,
        "serial_number": serial,
    }


def state_payload(frame: MPPTFrame) -> Dict[str, Any]:
    """The JSON document published to the state topic.

    Energy is converted to kWh here because that is the unit declared in
    discovery; the frame itself counts watt-hours.
    """
    return {
        "pv_voltage": frame.pv_voltage,
        "battery_voltage": frame.battery_voltage,
        "charge_current": frame.charge_current,
        "charge_power": frame.charge_power,
        "temperature": frame.temperature,
        "energy_today": frame.energy_today_kwh,
        "energy_total": frame.energy_total_kwh,
    }


def discovery_payload(
    sensor: Sensor,
    serial: str,
    device_name: str,
    state_topic: str,
    availability_topic: str,
    object_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Config payload for one sensor.

    Args:
        object_id: Suggests the entity_id suffix, e.g. ``solarinverterwatts``
            becomes ``sensor.solarinverterwatts``. Omit to let HA derive one
            from the device and sensor names.
    """
    payload: Dict[str, Any] = {
        "name": sensor.name,
        "unique_id": unique_id(serial, sensor.key),
        "state_topic": state_topic,
        "value_template": f"{{{{ value_json.{sensor.key} }}}}",
        "unit_of_measurement": sensor.unit,
        "state_class": sensor.state_class,
        "suggested_display_precision": sensor.precision,
        "availability_topic": availability_topic,
        "payload_available": PAYLOAD_AVAILABLE,
        "payload_not_available": PAYLOAD_NOT_AVAILABLE,
        "device": device_block(serial, device_name),
    }

    if sensor.device_class:
        payload["device_class"] = sensor.device_class
    if sensor.icon:
        payload["icon"] = sensor.icon
    if object_id:
        payload["object_id"] = object_id

    # Deliberately no expire_after. This controller reports sporadically and
    # goes quiet overnight; expiring the energy sensors into "unavailable"
    # would tear holes in the long-term statistics. Liveness is handled by the
    # availability topic and the MQTT will instead.
    return payload


def discovery_messages(
    serial: str,
    device_name: str,
    discovery_prefix: str,
    state_topic: str,
    availability_topic: str,
    object_ids: Optional[Dict[str, str]] = None,
):
    """Yield ``(topic, json_payload)`` for every sensor.

    Publish these retained so the entities survive a HA restart.
    """
    object_ids = object_ids or {}
    for sensor in SENSORS:
        topic = discovery_topic(discovery_prefix, serial, sensor.key)
        payload = discovery_payload(
            sensor,
            serial=serial,
            device_name=device_name,
            state_topic=state_topic,
            availability_topic=availability_topic,
            object_id=object_ids.get(sensor.key),
        )
        yield topic, json.dumps(payload)


def removal_messages(serial: str, discovery_prefix: str):
    """Yield ``(topic, "")`` for every sensor.

    An empty retained payload on a discovery topic tells HA to delete the
    entity. Used by ``--remove`` to tear down cleanly rather than leaving the
    orphans that Spook complains about.
    """
    for sensor in SENSORS:
        yield discovery_topic(discovery_prefix, serial, sensor.key), ""
