"""Configuration: pydantic models, domain validation (SPEC §4, §8.1) and loading.

No device I/O happens here. ``allow_writes`` exists only as a placeholder for the later write
layer (SPEC §10) and has no effect beyond a log warning.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from deye_monitor.scheduler import canonical_bus_key, normalize_host

log = logging.getLogger(__name__)

# Larger single reads fail on gateways and loggers (87 and 89 registers work, 131 does not).
MAX_READ_BLOCK = 80

OPTIONS_ENV = "DEYE_MONITOR_OPTIONS"
DATA_DIR_ENV = "DEYE_MONITOR_DATA"
DEFAULT_OPTIONS_PATH = "/data/options.json"
DEFAULT_DATA_DIR = "/data"
DEFAULT_PROFILE = "sg04lp3_lv_v105_8"

_ID_PATTERN = r"^[a-z0-9_]{1,32}$"
_INSTANCE_ID_RE = re.compile(r"^[a-z0-9]{4,16}$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

Id = Annotated[str, Field(pattern=_ID_PATTERN)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]
UnitId = Annotated[int, Field(strict=True, ge=1, le=247)]
PositiveSeconds = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
PollSeconds = Annotated[float, Field(strict=True, ge=1, allow_inf_nan=False)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Transport(StrEnum):
    MODBUS_TCP = "modbus_tcp"
    RTU_OVER_TCP = "rtu_over_tcp"
    SERIAL_RTU = "serial_rtu"
    SOLARMAN_V5_TCP = "solarman_v5_tcp"


_DEFAULT_PORTS = {
    Transport.MODBUS_TCP: 502,
    Transport.RTU_OVER_TCP: 502,
    Transport.SOLARMAN_V5_TCP: 8899,
}


class EnergyResolution(StrEnum):
    AUTO = "auto"
    TENTH = "0.1"
    ONE = "1"

    @property
    def kwh_step(self) -> float | None:
        return None if self is EnergyResolution.AUTO else float(self.value)


class EndpointConfig(_Model):
    id: Id
    transport: Transport
    host: str | None = None  # canonical connect address (see normalize_host)
    host_display: str | None = None  # text as entered, for display only; derived automatically
    port: Port | None = None
    logger_serial: Annotated[int, Field(strict=True, ge=1, le=0xFFFFFFFF)] | None = None
    serial_device: str | None = None
    baudrate: Annotated[int, Field(strict=True, ge=50, le=4_000_000)] | None = None
    bus_id: Id | None = None
    timeout_s: PositiveSeconds = 5.0
    # Assumed latest time an RTU answer can arrive after its request (serial_rtu and rtu_over_tcp);
    # default derived by the transport: max(1 s, 2 x timeout_s). An assumption, not a Deye limit.
    rtu_late_window_s: PositiveSeconds | None = None
    # at least 6: the RTU identity probe reads 5 (or, after a count-5 request, 6) registers and
    # is never shortened (VF-03)
    max_read_registers: Annotated[int, Field(strict=True, ge=6, le=100)] = 60
    request_gap_ms: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)] = 50.0
    queue_size: Annotated[int, Field(strict=True, ge=1, le=1000)] = 32

    @property
    def read_block_limit(self) -> int:
        """Registers per read actually used: the configured value, capped at ``MAX_READ_BLOCK``."""
        return min(self.max_read_registers, MAX_READ_BLOCK)

    @model_validator(mode="before")
    @classmethod
    def _defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        try:
            transport = Transport(data.get("transport"))
        except ValueError:
            return data
        raw_host = data.get("host")
        if isinstance(raw_host, str):
            try:
                data["host"] = normalize_host(raw_host)
            except ValueError:
                pass  # reported by the host validator with the field path
            else:
                data.setdefault("host_display", raw_host)
        if transport in _DEFAULT_PORTS and data.get("port") is None:
            data["port"] = _DEFAULT_PORTS[transport]
        if transport is Transport.SERIAL_RTU and data.get("baudrate") is None:
            data["baudrate"] = 9600
        return data

    @field_validator("host")
    @classmethod
    def _host(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return normalize_host(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("serial_device")
    @classmethod
    def _serial_device(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("/"):
            raise ValueError("must be an absolute device path")
        return v

    @model_validator(mode="after")
    def _transport_fields(self) -> EndpointConfig:
        t = self.transport
        serial = t is Transport.SERIAL_RTU
        problems: list[str] = []
        if serial:
            if self.serial_device is None:
                problems.append("serial_device is required for serial_rtu")
            for name in ("host", "port", "logger_serial"):
                if getattr(self, name) is not None:
                    problems.append(f"{name} is not allowed for serial_rtu")
        else:
            if self.host is None:
                problems.append(f"host is required for {t.value}")
            for name in ("serial_device", "baudrate"):
                if getattr(self, name) is not None:
                    problems.append(f"{name} is only allowed for serial_rtu")
            if t is Transport.SOLARMAN_V5_TCP:
                if self.logger_serial is None:
                    problems.append("logger_serial is required for solarman_v5_tcp")
            elif self.logger_serial is not None:
                problems.append("logger_serial is only allowed for solarman_v5_tcp")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def canonical_key(self) -> str:
        if self.transport is Transport.SERIAL_RTU:
            assert self.serial_device is not None
            return canonical_bus_key(serial_device=self.serial_device)
        assert self.host is not None
        assert self.port is not None
        return canonical_bus_key(host=self.host, port=self.port)

    @property
    def bus(self) -> str:
        """Bus key: explicit ``bus_id`` groups endpoints, otherwise the canonical endpoint."""
        return self.bus_id if self.bus_id is not None else self.canonical_key


class PollConfig(_Model):
    """Poll intervals of one inverter. Not an option group: Supervisor requires nested mappings,
    so the user options are the four flat ``poll_*_s`` fields of ``InverterConfig``."""

    fast_s: PollSeconds = 10.0
    energy_s: PollSeconds = 60.0
    settings_s: PollSeconds = 300.0
    identity_s: PollSeconds = 3600.0


class InverterConfig(_Model):
    id: Id
    name: Annotated[str, Field(min_length=1, max_length=64)]
    endpoint: Id
    unit_id: UnitId
    fallback_endpoint: Id | None = None
    fallback_unit_id: UnitId | None = None
    expected_serial: str | None = None
    profile: str = DEFAULT_PROFILE
    energy_resolution: EnergyResolution = EnergyResolution.AUTO
    poll_fast_s: PollSeconds = 10.0
    poll_energy_s: PollSeconds = 60.0
    poll_settings_s: PollSeconds = 300.0
    poll_identity_s: PollSeconds = 3600.0
    allow_writes: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="before")
    @classmethod
    def _defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("energy_resolution") is not None and not isinstance(
            data["energy_resolution"], str
        ):
            value = data["energy_resolution"]
            if isinstance(value, bool) or not isinstance(value, int | float):
                return data
            data["energy_resolution"] = "1" if value == 1 else str(value)
        if (
            data.get("fallback_endpoint") is not None
            and data.get("fallback_unit_id") is None
            and "unit_id" in data
        ):
            data["fallback_unit_id"] = data["unit_id"]
        return data

    @property
    def poll(self) -> PollConfig:
        return PollConfig(
            fast_s=self.poll_fast_s,
            energy_s=self.poll_energy_s,
            settings_s=self.poll_settings_s,
            identity_s=self.poll_identity_s,
        )

    @field_validator("expected_serial")
    @classmethod
    def _serial(cls, v: str | None) -> str | None:
        if v is not None and not (
            len(v) == 10 and v == v.strip() and all(0x20 <= ord(c) <= 0x7E for c in v)
        ):
            raise ValueError("must be exactly 10 printable ASCII characters")
        return v

    @model_validator(mode="after")
    def _fallback(self) -> InverterConfig:
        if self.fallback_endpoint is None:
            if self.fallback_unit_id is not None:
                raise ValueError("fallback_unit_id requires fallback_endpoint")
        elif self.fallback_endpoint == self.endpoint:
            raise ValueError("fallback_endpoint must differ from endpoint")
        return self


def _topic_prefix(v: str) -> str:
    segments = v.split("/")
    if any(not s or re.search(r"[+#\s\x00]", s) for s in segments):
        raise ValueError("must be non-empty topic segments without wildcards or whitespace")
    return v


TLS_DIR = "/ssl"
RESERVED_INVERTER_ID = "app"  # the app itself is the Home Assistant device deye_<instance>_app


class MqttConfig(_Model):
    host: str | None = None
    port: Port = 1883
    username: str | None = None
    password: SecretStr | None = None
    tls: Annotated[bool, Field(strict=True)] = False
    # TLS files live under /ssl (manifest ``map: ssl:ro``); the server certificate and host name
    # are always verified (no insecure mode). Without a CA file the system trust store is used.
    tls_ca_file: str | None = None
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    discovery_prefix: str = "homeassistant"
    topic_prefix: str = "deye_monitor"
    birth_topic: str = "homeassistant/status"  # Home Assistant's birth message (payload "online")

    @model_validator(mode="before")
    @classmethod
    def _blank_host_means_service(cls, data: Any) -> Any:
        """An empty ``host`` (the documented "leave empty") means: use the MQTT service.

        Only for MQTT; a blank endpoint host stays an error. The run script applies the same
        rule (trimmed empty -> service)."""
        host = data.get("host") if isinstance(data, dict) else None
        if isinstance(host, str) and not host.strip():
            data = {**data, "host": None}
        return data

    @field_validator("host")
    @classmethod
    def _host(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return normalize_host(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("discovery_prefix", "topic_prefix", "birth_topic")
    @classmethod
    def _prefix(cls, v: str) -> str:
        return _topic_prefix(v)

    @field_validator("tls_ca_file", "tls_certfile", "tls_keyfile", mode="before")
    @classmethod
    def _blank_tls_file(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("tls_ca_file", "tls_certfile", "tls_keyfile")
    @classmethod
    def _tls_file(cls, v: str | None) -> str | None:
        if v is None:
            return None
        parts = v.split("/")
        if not v.startswith(TLS_DIR + "/") or any(p in ("", ".", "..") for p in parts[1:]):
            raise ValueError(f"must be a file path below {TLS_DIR}/")
        return v

    @model_validator(mode="after")
    def _tls_options(self) -> MqttConfig:
        used = [
            n
            for n in ("tls_ca_file", "tls_certfile", "tls_keyfile")
            if getattr(self, n) is not None
        ]
        if used and not self.tls:
            raise ValueError(f"{', '.join(used)} require tls: true")
        if (self.tls_certfile is None) != (self.tls_keyfile is None):
            raise ValueError("tls_certfile and tls_keyfile must be set together")
        return self


@dataclass(frozen=True, slots=True)
class PlantProblem:
    """One domain problem of an endpoint/inverter set; ``code`` and ``params`` are stable keys for
    the web interface, ``text`` is the English message used in logs and option errors."""

    code: str
    params: dict[str, Any]
    text: str


def plant_problems(
    endpoints: Sequence[EndpointConfig], inverters: Sequence[InverterConfig]
) -> list[PlantProblem]:
    """Cross-checks of connections and inverters (SPEC §4, §8.1). Pure, no I/O."""
    problems: list[PlantProblem] = []
    _unique("endpoint", [e.id for e in endpoints], problems)
    _unique("inverter", [i.id for i in inverters], problems)
    if any(i.id == RESERVED_INVERTER_ID for i in inverters):
        problems.append(
            PlantProblem(
                "reserved_inverter_id",
                {"id": RESERVED_INVERTER_ID},
                f"inverter id '{RESERVED_INVERTER_ID}' is reserved for the app device",
            )
        )

    by_key: dict[str, str] = {}
    for e in endpoints:
        other = by_key.setdefault(e.canonical_key, e.id)
        if other != e.id:
            problems.append(
                PlantProblem(
                    "same_target",
                    {"a": other, "b": e.id},
                    f"endpoints '{other}' and '{e.id}' address the same target "
                    f"({e.canonical_key}); define it once",
                )
            )

    by_id = {e.id: e for e in endpoints}
    owners: dict[tuple[str, int], str] = {}
    for inv in inverters:
        routes = [(inv.endpoint, inv.unit_id, "endpoint")]
        if inv.fallback_endpoint is not None:
            assert inv.fallback_unit_id is not None
            routes.append((inv.fallback_endpoint, inv.fallback_unit_id, "fallback_endpoint"))
        for ep_id, unit, label in routes:
            endpoint = by_id.get(ep_id)
            if endpoint is None:
                problems.append(
                    PlantProblem(
                        "undefined_endpoint",
                        {"inverter": inv.id, "field": label, "endpoint": ep_id},
                        f"inverter '{inv.id}': {label} '{ep_id}' is not defined",
                    )
                )
                continue
            other = owners.setdefault((endpoint.bus, unit), inv.id)
            if other != inv.id:
                problems.append(
                    PlantProblem(
                        "unit_conflict",
                        {"a": other, "b": inv.id, "unit": unit},
                        f"inverters '{other}' and '{inv.id}' both use unit {unit} on the "
                        f"same bus ({endpoint.bus})",
                    )
                )
    return problems


def _v5_fallback_defaults(data: Any) -> Any:
    """A Solarman V5 logger only answers on unit 1, so an unset ``fallback_unit_id`` on such a
    fallback defaults to 1 instead of the primary's unit. Explicit values stay untouched."""
    if not isinstance(data, dict):
        return data
    endpoints = data.get("endpoints")
    inverters = data.get("inverters")
    if not isinstance(endpoints, list | tuple) or not isinstance(inverters, list | tuple):
        return data
    v5 = {
        e.get("id")
        for e in endpoints
        if isinstance(e, dict) and e.get("transport") == Transport.SOLARMAN_V5_TCP.value
    }
    fixed = []
    for i in inverters:
        if (
            isinstance(i, dict)
            and i.get("fallback_endpoint") in v5
            and i.get("fallback_unit_id") is None
        ):
            i = {**i, "fallback_unit_id": 1}
        fixed.append(i)
    return {**data, "inverters": fixed}


class PlantConfig(_Model):
    """Connections and inverters: what the web interface edits and ``/data/plant.json`` stores.
    May be empty (a fresh installation)."""

    endpoints: tuple[EndpointConfig, ...] = ()
    inverters: tuple[InverterConfig, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _defaults(cls, data: Any) -> Any:
        return _v5_fallback_defaults(data)

    @model_validator(mode="after")
    def _domain(self) -> PlantConfig:
        problems = plant_problems(self.endpoints, self.inverters)
        if problems:
            raise ValueError("; ".join(p.text for p in problems))
        return self

    def endpoint(self, endpoint_id: str) -> EndpointConfig:
        for e in self.endpoints:
            if e.id == endpoint_id:
                return e
        raise KeyError(endpoint_id)


class AppConfig(_Model):
    """The effective configuration: the app options (MQTT, log level, instance id) plus the plant.
    Options may still carry ``endpoints``/``inverters`` (older installations, migration source);
    ``with_plant`` replaces them by the plant store's content."""

    endpoints: tuple[EndpointConfig, ...] = ()
    inverters: tuple[InverterConfig, ...] = ()
    mqtt: MqttConfig = MqttConfig()
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    instance_id: str | None = None

    @field_validator("instance_id")
    @classmethod
    def _instance_id(cls, v: str | None) -> str | None:
        if v is not None and not _INSTANCE_ID_RE.match(v):
            raise ValueError("must match [a-z0-9]{4,16}")
        return v

    def endpoint(self, endpoint_id: str) -> EndpointConfig:
        for e in self.endpoints:
            if e.id == endpoint_id:
                return e
        raise KeyError(endpoint_id)

    @model_validator(mode="before")
    @classmethod
    def _defaults(cls, data: Any) -> Any:
        return _v5_fallback_defaults(data)

    @model_validator(mode="after")
    def _domain(self) -> AppConfig:
        problems = plant_problems(self.endpoints, self.inverters)
        if problems:
            raise ValueError("; ".join(p.text for p in problems))
        return self

    @property
    def plant(self) -> PlantConfig:
        return PlantConfig(endpoints=self.endpoints, inverters=self.inverters)

    def with_plant(self, plant: PlantConfig) -> AppConfig:
        return AppConfig(
            endpoints=plant.endpoints,
            inverters=plant.inverters,
            mqtt=self.mqtt,
            log_level=self.log_level,
            instance_id=self.instance_id,
        )


def _unique(kind: str, ids: list[str], problems: list[PlantProblem]) -> None:
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            problems.append(
                PlantProblem(f"duplicate_{kind}", {"id": i}, f"duplicate {kind} id '{i}'")
            )
        seen.add(i)


class ConfigError(Exception):
    """User-facing configuration problem; the text never contains input values."""


def format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(p) for p in err["loc"]) or "config"
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"{loc}: {msg}")
    return "\n".join(lines)


def parse_config(data: object) -> AppConfig:
    """Validate a decoded options document; raises ``ConfigError`` without leaking secrets."""
    try:
        cfg = AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from None
    for ep in cfg.endpoints:
        if ep.max_read_registers > MAX_READ_BLOCK:
            log.warning(
                "endpoint '%s': max_read_registers %d is above %d and is limited to %d",
                ep.id,
                ep.max_read_registers,
                MAX_READ_BLOCK,
                MAX_READ_BLOCK,
            )
    for inv in cfg.inverters:
        if inv.allow_writes:
            log.warning(
                "allow_writes is set for inverter '%s', but the write layer is not implemented "
                "(Schreibschicht nicht implementiert); the setting is ignored",
                inv.id,
            )
    return cfg


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load options from ``path``, ``$DEYE_MONITOR_OPTIONS`` or ``/data/options.json``."""
    target = Path(path or os.environ.get(OPTIONS_ENV) or DEFAULT_OPTIONS_PATH)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc.strerror or type(exc).__name__}") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{target} is not valid JSON: {exc}") from None
    return parse_config(data)


def data_dir(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR)


def resolve_instance_id(cfg: AppConfig, directory: str | os.PathLike[str] | None = None) -> str:
    """Configured id, else the persisted one, else a freshly generated and persisted one."""
    if cfg.instance_id is not None:
        return cfg.instance_id
    target = data_dir(directory) / "instance_id"
    try:
        stored = target.read_text(encoding="ascii").strip()
    except OSError, UnicodeDecodeError:
        stored = ""
    if _INSTANCE_ID_RE.match(stored):
        return stored
    if stored:
        log.warning("ignoring invalid %s; generating a new instance id", target)
    new = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    _atomic_write(target, new + "\n")
    return new


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


SUPERVISOR_MQTT_ENV = "DEYE_MONITOR_SUPERVISOR_MQTT_"


def with_supervisor_mqtt(mqtt: MqttConfig, environ: Mapping[str, str]) -> MqttConfig:
    """Fill an empty ``mqtt.host`` from the Supervisor MQTT service (exported by the s6 run
    script as ``DEYE_MONITOR_SUPERVISOR_MQTT_{HOST,PORT,USERNAME,PASSWORD,SSL}``)."""
    if mqtt.host is not None:
        return mqtt
    host = environ.get(SUPERVISOR_MQTT_ENV + "HOST", "").strip()
    if not host:
        return mqtt
    update: dict[str, object] = {"host": host}
    try:
        update["port"] = int(environ.get(SUPERVISOR_MQTT_ENV + "PORT", "") or mqtt.port)
    except ValueError:
        raise ConfigError("Supervisor MQTT service reported an invalid port") from None
    username = environ.get(SUPERVISOR_MQTT_ENV + "USERNAME")
    password = environ.get(SUPERVISOR_MQTT_ENV + "PASSWORD")
    if username and mqtt.username is None:
        update["username"] = username
    if password and mqtt.password is None:
        update["password"] = SecretStr(password)
    if environ.get(SUPERVISOR_MQTT_ENV + "SSL", "").lower() == "true":
        update["tls"] = True
    try:
        return MqttConfig.model_validate({**mqtt.model_dump(), **update})
    except ValidationError as exc:
        raise ConfigError("Supervisor MQTT service: " + format_validation_error(exc)) from None
