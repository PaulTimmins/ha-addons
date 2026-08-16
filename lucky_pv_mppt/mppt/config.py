"""Configuration loading.

Credentials live in an INI config file, never in the repo and never in the
environment. ``configparser`` is used rather than TOML because the target
Python here is 3.9, which has no ``tomllib``.

Two brokers are involved and they are almost never the same one: ``[source]``
is the vendor's broker that the controller reports to, ``[homeassistant]`` is
your own broker that HA is attached to.
"""

from __future__ import annotations

import configparser
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

#: Searched in order when no explicit path is given.
SEARCH_PATHS: List[Path] = [
    Path("config.ini"),
    Path.home() / ".config" / "solar-mppt" / "config.ini",
    Path("/etc/solar-mppt/config.ini"),
]

DEFAULT_DISCOVERY_PREFIX = "homeassistant"


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable, or incomplete."""


@dataclass(frozen=True)
class DeviceConfig:
    """Identifies one physical controller.

    The serial is the hex string embedded in the MQTT topic. It is kept as its
    own value, not just buried in the topic, because it is the stable
    identifier Home Assistant keys the device and its entities on.
    """

    serial: str
    module_id: str
    name: str


@dataclass(frozen=True)
class SourceConfig:
    """The vendor broker the controller publishes to."""

    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    topic: str
    client_id: str
    keepalive: int


@dataclass(frozen=True)
class HomeAssistantConfig:
    """Your own broker, the one HA's MQTT integration is connected to."""

    enabled: bool
    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    client_id: str
    keepalive: int
    discovery_prefix: str
    state_topic: str
    availability_topic: str
    retain: bool
    object_ids: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DaemonConfig:
    log_level: str
    log_file: Optional[str]
    pid_file: Optional[str]


@dataclass(frozen=True)
class Config:
    device: DeviceConfig
    source: SourceConfig
    homeassistant: HomeAssistantConfig
    daemon: DaemonConfig
    path: Path


def find_config(explicit: Optional[str] = None) -> Path:
    """Locate the config file, or raise ``ConfigError`` explaining where we looked."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path

    for candidate in SEARCH_PATHS:
        if candidate.is_file():
            return candidate

    looked = "\n  ".join(str(p) for p in SEARCH_PATHS)
    raise ConfigError(
        "no config file found. Looked in:\n  "
        + looked
        + "\nCopy config.example.ini to one of these and fill it in, "
        "or pass --config PATH."
    )


def check_permissions(path: Path) -> Optional[str]:
    """Return a warning if the config is group- or world-readable, else None.

    The file holds broker passwords, so it should be 0600.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{path} is readable by other users (mode {stat.S_IMODE(mode):04o}); "
            f"it holds passwords. Run: chmod 600 {path}"
        )
    return None


def _expand(template: str, device: DeviceConfig, path: Path, where: str) -> str:
    """Substitute ``{serial}`` / ``{module_id}`` into a topic template."""
    try:
        return template.format(serial=device.serial, module_id=device.module_id)
    except (KeyError, IndexError) as exc:
        raise ConfigError(
            f"{path}: {where} has an unknown placeholder {exc}. "
            "Only {serial} and {module_id} are available."
        ) from exc


def _getint(section, key, fallback, path, where):
    try:
        return section.getint(key, fallback=fallback)
    except ValueError as exc:
        raise ConfigError(f"{path}: {where} {key} must be a number ({exc})") from exc


def _getbool(section, key, fallback, path, where):
    try:
        return section.getboolean(key, fallback=fallback)
    except ValueError as exc:
        raise ConfigError(
            f"{path}: {where} {key} must be true or false ({exc})"
        ) from exc


def load(explicit: Optional[str] = None) -> Config:
    """Read and validate the config file."""
    path = find_config(explicit)

    # interpolation=None: a password containing '%' must survive verbatim.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    except configparser.Error as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc

    for required_section in ("device", "source"):
        if not parser.has_section(required_section):
            raise ConfigError(f"{path} has no [{required_section}] section")

    device = _load_device(parser["device"], path)
    source = _load_source(parser["source"], device, path)
    homeassistant = _load_homeassistant(parser, device, path)
    daemon = _load_daemon(parser)

    return Config(
        device=device,
        source=source,
        homeassistant=homeassistant,
        daemon=daemon,
        path=path,
    )


def _load_device(section, path: Path) -> DeviceConfig:
    serial = (section.get("serial") or "").strip()
    if not serial:
        raise ConfigError(
            f"{path}: [device] serial is required (the hex string in your MQTT topic)"
        )
    return DeviceConfig(
        serial=serial,
        module_id=(section.get("module_id") or "").strip(),
        name=(section.get("name") or "").strip() or "Lucky PV MPPT",
    )


def _load_source(section, device: DeviceConfig, path: Path) -> SourceConfig:
    for required in ("host", "topic"):
        if not section.get(required):
            raise ConfigError(f"{path}: [source] {required} is required")

    return SourceConfig(
        host=section["host"].strip(),
        port=_getint(section, "port", 1883, path, "[source]"),
        username=(section.get("username") or None),
        password=(section.get("password") or None),
        topic=_expand(section["topic"].strip(), device, path, "[source] topic"),
        client_id=(section.get("client_id") or "").strip()
        or f"solar-mppt-{device.serial}",
        keepalive=_getint(section, "keepalive", 60, path, "[source]"),
    )


def _load_homeassistant(parser, device: DeviceConfig, path: Path) -> HomeAssistantConfig:
    if not parser.has_section("homeassistant"):
        raise ConfigError(f"{path} has no [homeassistant] section")

    section = parser["homeassistant"]
    enabled = _getbool(section, "enabled", True, path, "[homeassistant]")

    host = (section.get("host") or "").strip()
    if enabled and not host:
        raise ConfigError(
            f"{path}: [homeassistant] host is required "
            "(your own broker, the one HA is connected to). "
            "Set enabled = false to run without publishing."
        )

    default_state = "solar-mppt/{serial}/state"
    default_availability = "solar-mppt/{serial}/availability"

    object_ids = {}
    if parser.has_section("entity_ids"):
        from .homeassistant import SENSORS_BY_KEY

        object_ids = {
            key: value.strip()
            for key, value in parser["entity_ids"].items()
            if value.strip()
        }
        unknown = sorted(set(object_ids) - set(SENSORS_BY_KEY))
        if unknown:
            raise ConfigError(
                f"{path}: [entity_ids] has unknown sensor(s) {', '.join(unknown)}. "
                f"Valid keys: {', '.join(sorted(SENSORS_BY_KEY))}"
            )

    return HomeAssistantConfig(
        enabled=enabled,
        host=host,
        port=_getint(section, "port", 1883, path, "[homeassistant]"),
        username=(section.get("username") or None),
        password=(section.get("password") or None),
        client_id=(section.get("client_id") or "").strip()
        or f"solar-mppt-ha-{device.serial}",
        keepalive=_getint(section, "keepalive", 60, path, "[homeassistant]"),
        discovery_prefix=(section.get("discovery_prefix") or "").strip()
        or DEFAULT_DISCOVERY_PREFIX,
        state_topic=_expand(
            (section.get("state_topic") or "").strip() or default_state,
            device,
            path,
            "[homeassistant] state_topic",
        ),
        availability_topic=_expand(
            (section.get("availability_topic") or "").strip() or default_availability,
            device,
            path,
            "[homeassistant] availability_topic",
        ),
        retain=_getbool(section, "retain", True, path, "[homeassistant]"),
        object_ids=object_ids,
    )


def _load_daemon(parser) -> DaemonConfig:
    if not parser.has_section("daemon"):
        return DaemonConfig(log_level="INFO", log_file=None, pid_file=None)

    section = parser["daemon"]
    return DaemonConfig(
        log_level=(section.get("log_level") or "INFO").strip().upper(),
        log_file=(section.get("log_file") or None),
        pid_file=(section.get("pid_file") or None),
    )
