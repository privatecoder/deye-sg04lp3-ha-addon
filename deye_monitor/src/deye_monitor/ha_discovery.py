"""Home Assistant MQTT Device Discovery: namespace, payload builders and the read-only allowlist.

Normative contract: ``reviewer-codex-6.1-t8-contract.md`` (HA Core 2026.10.4). Everything here is
pure: no I/O, no clock, no MQTT client.

* The measurement platforms are ``sensor`` and ``binary_sensor`` (no command topic). Released
  write-layer settings (0.4.0 part B) are ``number``, ``switch`` and ``time`` entities with a
  command topic, plus one ``event`` entity per inverter for write results. A command topic exists
  only on those three platforms, always with ``retain: false``, ``optimistic: false`` and
  ``entity_category: config``, and no command template: ``validate_document`` accepts exclusively
  the field names listed below (an allowlist, not a denylist) and is applied to every document
  before it is sent.
* Keys are always written out in full (never the short forms ``dev``, ``cmps``, ``uniq_id`` ...).
* ``default_entity_id`` is used instead of the old payload ``object_id``.
* Energy counters carry their statistics generation in the component key, the unique id, the
  default entity id and in a template guard on ``energy_series`` of the state payload, so a
  counter of another generation can never receive values of a new scaling.
* Missing or invalid values are JSON ``null`` and render as ``None`` (HA ``unknown``); templates
  never use defaults.
"""

from __future__ import annotations

import datetime
import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final

from deye_monitor import __version__
from deye_monitor.config import PollConfig
from deye_monitor.profiles.alarms import active_items
from deye_monitor.profiles.decode import Value
from deye_monitor.profiles.schema import Entry, Profile
from deye_monitor.profiles.states import (
    NOT_REPORTED,
    STATES,
    Presented,
    attributes_key,
    present,
    primary_valid,
)
from deye_monitor.write_ops import Kind, WriteDescriptor

READ_PLATFORMS: Final = ("sensor", "binary_sensor")
WRITE_PLATFORMS: Final = ("number", "switch", "time", "select", "button")
PLATFORMS: Final = (*READ_PLATFORMS, *WRITE_PLATFORMS, "event")
MAX_DOCUMENT_BYTES: Final = 256 * 1024  # application limit per device discovery document
APP_DEVICE_SUFFIX: Final = "app"
ORIGIN_NAME: Final = "Deye SG04LP3 Monitor"
MANUFACTURER: Final = "Deye"
INVERTER_MODEL: Final = "Deye SG04LP3 three-phase low-voltage hybrid inverter"
NONE_TEMPLATE: Final = "{{ none }}"
APP_HEARTBEAT_S: Final = 30.0
STATIC_GROUPS: Final = frozenset({"identity", "settings"})
WRITE_TOPIC: Final = "write"
WRITE_RESULT_KEY: Final = "write_result"
SETTING_PREFIX: Final = "set_"
EVENT_TYPES: Final = (
    "write_readback_confirmed",
    "write_rejected",
    "write_failed",
    "write_outcome_unknown",
    "write_noop",
)

_ROOT_FIELDS = frozenset({"device", "origin", "availability", "availability_mode", "components"})
_DEVICE_FIELDS = frozenset(
    {"identifiers", "name", "manufacturer", "model", "serial_number", "via_device"}
)
_ORIGIN_FIELDS = frozenset({"name", "sw_version"})
_AVAILABILITY_FIELDS = frozenset({"topic"})
_COMPONENT_FIELDS = frozenset(
    {
        "platform",
        "unique_id",
        "default_entity_id",
        "name",
        "state_topic",
        "value_template",
        "expire_after",
        "entity_category",
        "device_class",
        "state_class",
        "unit_of_measurement",
        "suggested_display_precision",
        "options",
        "payload_on",
        "payload_off",
        "json_attributes_topic",
        "json_attributes_template",
        "availability",
        "command_topic",
        "min",
        "max",
        "step",
        "mode",
        "optimistic",
        "retain",
        "event_types",
    }
)
_READ_FIELDS = _COMPONENT_FIELDS - frozenset(
    {"command_topic", "min", "max", "step", "mode", "optimistic", "retain", "event_types"}
)
_SENSOR_ONLY = frozenset(
    {
        "state_class",
        "unit_of_measurement",
        "suggested_display_precision",
        "options",
    }
)
_BINARY_ONLY = frozenset({"payload_on", "payload_off"})
_ENTITY_CATEGORIES = frozenset({"diagnostic"})
_WRITE_FIELDS = frozenset(
    {
        "platform",
        "unique_id",
        "default_entity_id",
        "name",
        "state_topic",
        "value_template",
        "entity_category",
        "availability",
        "command_topic",
        "optimistic",
        "retain",
    }
)
_NUMBER_FIELDS = frozenset({"min", "max", "step", "mode", "device_class", "unit_of_measurement"})
_EVENT_FIELDS = frozenset(
    {"platform", "unique_id", "default_entity_id", "name", "state_topic", "event_types"}
    | {"availability"}
)
_NUMBER_CLASSES = frozenset({"current", "battery", "power", "voltage"})
_NUMBER_UNITS = {"current": "A", "battery": "%", "power": "W", "voltage": "V"}
_STATE_CLASSES = frozenset({"measurement", "total", "total_increasing"})


class DiscoveryError(ValueError):
    """A discovery document violates the read-only contract (never sent)."""


# -- namespace -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Namespace:
    """All topics and identities of one app instance (disjoint per instance)."""

    instance: str
    discovery_prefix: str
    state_prefix: str

    @property
    def client_id(self) -> str:
        return f"deye_{self.instance}_monitor"

    @property
    def app_device_id(self) -> str:
        return f"deye_{self.instance}_{APP_DEVICE_SUFFIX}"

    def inverter_device_id(self, inverter_id: str) -> str:
        return f"deye_{self.instance}_{inverter_id}"

    def discovery_topic(self, device_id: str) -> str:
        return f"{self.discovery_prefix}/device/{device_id}/config"

    @property
    def app_status_topic(self) -> str:
        return f"{self.state_prefix}/{self.instance}/status"

    @property
    def app_state_topic(self) -> str:
        return f"{self.state_prefix}/{self.instance}/{APP_DEVICE_SUFFIX}/diagnostics"

    @property
    def cleanup_topic(self) -> str:
        """Never published as ``online``: replayed cleanup entities stay unavailable."""
        return f"{self.state_prefix}/{self.instance}/cleanup"

    @property
    def cleanup_command_topic(self) -> str:
        """The command topic of a replayed (retired) writable component: nobody subscribes to it."""
        return f"{self.cleanup_topic}/void"

    def write_base(self, inverter_id: str) -> str:
        """Topics of the write entities of one inverter (``write`` is no profile group name)."""
        return f"{self.state_prefix}/{self.instance}/{inverter_id}/{WRITE_TOPIC}"

    def write_state_topic(self, inverter_id: str) -> str:
        return f"{self.write_base(inverter_id)}/state"

    def write_result_topic(self, inverter_id: str) -> str:
        return f"{self.write_base(inverter_id)}/result"

    def write_availability_topic(self, inverter_id: str, key: str) -> str:
        return f"{self.write_base(inverter_id)}/available/{key}"

    def command_prefix(self, inverter_id: str, token: str) -> str:
        """Generation-scoped: a topic of an older token is no longer subscribed."""
        return f"{self.write_base(inverter_id)}/set/{token}"

    def command_topic(self, inverter_id: str, token: str, key: str) -> str:
        return f"{self.command_prefix(inverter_id, token)}/{key}"

    def inverter_availability_topic(self, inverter_id: str) -> str:
        return f"{self.state_prefix}/{self.instance}/{inverter_id}/availability"

    def group_topic(self, inverter_id: str, group: str) -> str:
        return f"{self.state_prefix}/{self.instance}/{inverter_id}/{group}"

    @property
    def plant_state_topic(self) -> str:
        """Plant totals (below the reserved ``app`` id, so no inverter topic can collide)."""
        return f"{self.state_prefix}/{self.instance}/{APP_DEVICE_SUFFIX}/plant"

    def inverter_diagnostics_topic(self, inverter_id: str) -> str:
        """Per-inverter diagnostics (a name no profile group can have)."""
        return self.group_topic(inverter_id, "diagnostics")


# -- component builders --------------------------------------------------------------------------


SERIES_RE: Final = re.compile(r"^([a-z0-9]{8})?g[0-9]+$")


ENERGY_SERIES_ATTRIBUTE = "{{ {'energy_series': value_json.get('energy_series')} | tojson }}"


def check_series(series: str) -> str:
    if not SERIES_RE.match(series):
        raise DiscoveryError(f"invalid energy series {series!r}")
    return series


def component_key(entry: Entry, series: str) -> str:
    """Stable component key; energy counters carry ``_<energy_series>`` from the first publish."""
    return f"{entry.key}_{check_series(series)}" if entry.scale_source else entry.key


def expire_after(poll: PollConfig, profile: Profile, group: str) -> int:
    seconds = float(getattr(poll, profile.groups[group].poll))  # type: ignore[index]
    return max(1, math.ceil(3 * seconds))


def _decimals(scale: float | None) -> int:
    if scale is None or scale >= 1:
        return 0
    text = f"{scale:.10f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def value_template(entry: Entry, series: str | None = None) -> str:
    """The Jinja template that reads exactly this entity's JSON key (no defaults)."""
    return _template(entry.key, entry.platform, series if entry.scale_source else None)


def _template(key: str, platform: str, series: str | None = None) -> str:
    if platform == "binary_sensor":
        return (
            f"{{% set v = value_json.get('{key}') if value_json is mapping else none %}}"
            "{{ 'ON' if v is sameas true else 'OFF' if v is sameas false else 'None' }}"
        )
    if series is not None:
        return (
            f"{{{{ value_json.get('{key}') if value_json is mapping "
            f"and value_json.get('energy_series') == '{check_series(series)}' else none }}}}"
        )
    return f"{{{{ value_json.get('{key}') if value_json is mapping else none }}}}"


# the GEN total of a member of a parallel plant is the share allocated to it (HA entity and web UI)
PARALLEL_GEN_SHARE = {"de": "GEN-Anteil (gemeldet)", "en": "GEN reported share"}
PARALLEL_GEN_SHARE_KEY = "gen_power_raw_total"


def inverter_component(
    ns: Namespace,
    inverter_id: str,
    entry: Entry,
    *,
    series: str,
    poll: PollConfig,
    profile: Profile,
    parallel: bool = False,
) -> tuple[str, dict[str, Any]]:
    """(component key, discovery component) of one selected profile entry."""
    device_id = ns.inverter_device_id(inverter_id)
    ckey = component_key(entry, series)
    # in a parallel plant the GEN total is the share the firmware allocates to this unit
    name = (
        PARALLEL_GEN_SHARE["en"]
        if parallel and entry.key == PARALLEL_GEN_SHARE_KEY
        else entry.name.en
    )
    comp: dict[str, Any] = {
        "platform": entry.platform,
        "unique_id": f"{device_id}_{ckey}",
        "default_entity_id": f"{entry.platform}.{device_id}_{ckey}",
        "name": name,
        "state_topic": ns.group_topic(inverter_id, entry.group),
        "value_template": value_template(entry, series),
        "expire_after": expire_after(poll, profile, entry.group),
    }
    if entry.entity_category == "diagnostic":
        comp["entity_category"] = "diagnostic"
    if entry.platform == "binary_sensor":
        comp.update(payload_on="ON", payload_off="OFF")
        if entry.device_class:
            comp["device_class"] = entry.device_class
    elif (spec := STATES.get(entry.key)) is not None and spec.mode == "text":
        # plain-language state (R33-14): the English label, an enum sensor when the list is fixed
        if spec.options:
            comp["device_class"] = "enum"
            comp["options"] = [o.en for o in spec.options]
    elif entry.type == "enum" and entry.enum:
        # A fixed, complete option list: HA ignores values outside it (and would keep the old
        # state), so the publisher maps every unlisted value to null.
        comp["device_class"] = "enum"
        comp["options"] = list(dict.fromkeys(entry.label(v) for v in entry.enum.values()))
    else:
        if entry.device_class:
            comp["device_class"] = entry.device_class
        if entry.unit:
            comp["unit_of_measurement"] = entry.unit
        # static identity/settings values get no statistics class (contract §3.4)
        if entry.state_class and entry.group not in STATIC_GROUPS:
            comp["state_class"] = entry.state_class
        if entry.unit and entry.is_numeric and not entry.scale_source:
            comp["suggested_display_precision"] = _decimals(entry.scale)
    if entry.type == "alarms":
        comp["json_attributes_topic"] = comp["state_topic"]
        comp["json_attributes_template"] = (
            f"{{{{ {{'alarms': value_json.get('{entry.key}{ALARM_LIST_SUFFIX}')}} | tojson }}}}"
        )
    if entry.key in STATES:  # the raw word and the German label travel as attributes
        comp["json_attributes_topic"] = comp["state_topic"]
        comp["json_attributes_template"] = (
            f"{{{{ value_json.get('{attributes_key(entry.key)}') | tojson }}}}"
        )
    if entry.scale_source:  # the series is not in the name: an attribute tells the series apart
        comp["json_attributes_topic"] = comp["state_topic"]
        comp["json_attributes_template"] = ENERGY_SERIES_ATTRIBUTE
    if entry.profile_fallback and entry.platform == "sensor" and entry.key not in STATES:
        # ``source`` tells a device value from the hardware profile's value (the device left it out)
        comp["json_attributes_topic"] = comp["state_topic"]
        comp["json_attributes_template"] = (
            "{{ {'source': (value_json.get('sources') or {}).get('"
            f"{entry.key}"
            "', 'device')} | tojson }}"
        )
    return ckey, comp


@dataclass(frozen=True, slots=True)
class AppDiagnostic:
    key: str
    name: str
    options: tuple[str, ...] | None = None
    state_class: str | None = None


APP_DIAGNOSTICS: Final = (
    AppDiagnostic("app_version", "App version"),
    AppDiagnostic("mqtt_reconnects", "MQTT reconnects", state_class="total_increasing"),
    AppDiagnostic("inverters_online", "Inverters online", state_class="measurement"),
    AppDiagnostic("inverters_configured", "Inverters configured", state_class="measurement"),
)


@dataclass(frozen=True, slots=True)
class InverterDiagnostic:
    """One curated per-inverter diagnostic sensor (T8-04). ``kind``: text | number | timestamp."""

    suffix: str
    name: str
    kind: str = "text"
    state_class: str | None = None
    precision: int | None = None
    attributes: bool = False


INVERTER_DIAGNOSTICS: Final = (
    InverterDiagnostic("state", "State"),
    InverterDiagnostic("reason", "State reason"),
    InverterDiagnostic("path", "Connection path"),
    InverterDiagnostic("serial", "Bound serial number"),
    InverterDiagnostic("firmware_mcu1", "Firmware MCU1"),
    InverterDiagnostic("firmware_mcu2", "Firmware MCU2"),
    InverterDiagnostic("energy_resolution", "Energy resolution", "number", precision=1),
    InverterDiagnostic("energy_source", "Energy resolution source"),
    InverterDiagnostic("energy_status", "Energy confirmation"),
    InverterDiagnostic("energy_series", "Energy series"),
    InverterDiagnostic(
        "capability_conflicts", "Capability conflicts", "number", "measurement", attributes=True
    ),
    InverterDiagnostic("bus_utilisation", "Bus utilisation", "number", "measurement", 3),
    InverterDiagnostic("transport_timeouts", "Transport timeouts", "number", "total_increasing"),
    InverterDiagnostic(
        "transport_frame_errors", "Transport frame errors", "number", "total_increasing"
    ),
    InverterDiagnostic("transport_resyncs", "Transport resyncs", "number", "total_increasing"),
    InverterDiagnostic("transport_cooldown", "Transport cooldown"),
    InverterDiagnostic("last_success", "Last successful read", "timestamp"),
)
ENERGY_SOURCES: Final = {
    "override": "user choice",
    "r65": "R65 (before 0.5.1)",  # a record written by an older version
    "profile": "profile default 0.1 kWh",
}
CONFLICT_LIST_KEY: Final = "capability_conflict_list"
ALARM_LIST_SUFFIX: Final = "_list"
_TEXT_LIMIT: Final = 120
_MAX_CONFLICTS: Final = 20


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    clean = "".join(c for c in value if c.isprintable())[:_TEXT_LIMIT]
    return clean or None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def firmware_board(words: object, board: str) -> str | None:
    """One firmware board as the Deye cloud shows it: MCU1 ``1001-C070`` (communication/HMI board,
    R17-R18), MCU2 ``2005-1175-1807`` (control board, R14-R15-R11)."""
    if not isinstance(words, Mapping):
        return None
    values = [words.get(a) for a in _FIRMWARE_BOARDS[board]]
    if not any(isinstance(w, int) and not isinstance(w, bool) for w in values):
        return None
    return "-".join(
        f"{w:04X}" if isinstance(w, int) and not isinstance(w, bool) else "????" for w in values
    )


_FIRMWARE_BOARDS: Final = {"mcu1": (17, 18), "mcu2": (14, 15, 11)}


def inverter_diagnostic_values(diag: Mapping[str, Any] | None) -> dict[str, Any]:
    """The curated diagnostic values of one inverter from ``InverterRuntime.diagnostics()``.
    Only the fields below are ever read (no pass-through of arbitrary dictionaries, no secrets);
    anything unknown or malformed is ``None``."""
    out: dict[str, Any] = {d.suffix: None for d in INVERTER_DIAGNOSTICS}
    out[CONFLICT_LIST_KEY] = []
    out["state"] = _text(diag.get("state")) if isinstance(diag, Mapping) else None
    out["reason"] = _text(diag.get("reason")) if isinstance(diag, Mapping) else None
    if not isinstance(diag, Mapping):
        return out

    def sub(name: str) -> Mapping[str, Any]:
        value = diag.get(name)
        return value if isinstance(value, Mapping) else {}

    path, endpoint = _text(diag.get("active_path")), _text(diag.get("endpoint"))
    out["path"] = f"{path}:{endpoint}" if path and endpoint else path
    out["serial"] = _text(diag.get("bound_serial"))
    words = sub("identity").get("firmware_words")
    out["firmware_mcu1"] = firmware_board(words, "mcu1")
    out["firmware_mcu2"] = firmware_board(words, "mcu2")
    energy = sub("energy")
    out["energy_resolution"] = _number(energy.get("effective_resolution"))
    source = _text(energy.get("source"))
    out["energy_source"] = ENERGY_SOURCES.get(source, source) if source else None
    blocked = _text(energy.get("blocked"))
    if blocked:
        out["energy_status"] = f"blocked: {blocked}"
    elif out["energy_resolution"] is not None:
        out["energy_status"] = "confirmed"
    series = _text(energy.get("series"))
    out["energy_series"] = series if series and SERIES_RE.match(series) else None
    conflicts = diag.get("capability_conflicts")
    if isinstance(conflicts, list):
        out["capability_conflicts"] = len(conflicts)
        for item in conflicts[:_MAX_CONFLICTS]:
            if isinstance(item, Mapping) and (code := _text(item.get("code"))):
                out[CONFLICT_LIST_KEY].append(
                    {"code": code, "detail": _text(item.get("detail")) or ""}
                )
    util = _number(diag.get("bus_utilisation"))
    out["bus_utilisation"] = None if util is None else round(util, 3)
    counters = sub("transport").get(path) if path else None
    if isinstance(counters, Mapping):
        out["transport_timeouts"] = _number(counters.get("timeouts"))
        out["transport_frame_errors"] = _number(counters.get("frame_errors"))
        out["transport_resyncs"] = _number(counters.get("resyncs"))
    cooldown = diag.get("cooldown_active")
    if isinstance(cooldown, bool):
        out["transport_cooldown"] = "active" if cooldown else "inactive"
    last = _number(diag.get("last_success_wall"))
    out["last_success"] = None if last is None else timestamp(last)
    return out


def app_components(ns: Namespace) -> dict[str, dict[str, Any]]:
    """Diagnostic entities of the app device: only what concerns the whole app. Everything that
    belongs to one inverter lives on that inverter's device (``inverter_diagnostic_components``)."""
    device_id = ns.app_device_id
    expire = math.ceil(3 * APP_HEARTBEAT_S)
    out: dict[str, dict[str, Any]] = {}
    for diag in APP_DIAGNOSTICS:
        comp: dict[str, Any] = {
            "platform": "sensor",
            "unique_id": f"{device_id}_{diag.key}",
            "default_entity_id": f"sensor.{device_id}_{diag.key}",
            "name": diag.name,
            "state_topic": ns.app_state_topic,
            "value_template": _template(diag.key, "sensor"),
            "expire_after": expire,
            "entity_category": "diagnostic",
        }
        if diag.state_class:
            comp["state_class"] = diag.state_class
        out[diag.key] = comp
    return out


def plant_components(
    ns: Namespace,
    series: str,
    expire: Mapping[str, int],
    *,
    gates: Collection[str] = frozenset({"gen", "grid"}),
) -> dict[str, dict[str, Any]]:
    """Plant totals on the app device (``expire``: seconds per metric key; ``gates``: the exposure
    predicates that hold (a GEN use, a used GRID port)). Energy totals carry the
    plant series in key, unique id, entity id and the template guard, like the inverter counters."""
    from deye_monitor.totals import METRICS  # local: totals imports this module's siblings

    device_id = ns.app_device_id
    heartbeat = math.ceil(3 * APP_HEARTBEAT_S)
    out: dict[str, dict[str, Any]] = {}
    for m in METRICS:
        if m.gate is not None and m.gate not in gates:
            continue
        energy = m.kind == "energy"
        ckey = f"{m.key}_{check_series(series)}" if energy else m.key
        comp: dict[str, Any] = {
            "platform": "sensor",
            "unique_id": f"{device_id}_{ckey}",
            "default_entity_id": f"sensor.{device_id}_{ckey}",
            "name": m.name,
            "state_topic": ns.plant_state_topic,
            "value_template": _template(m.key, "sensor", series if energy else None),
            "expire_after": max(expire.get(m.key, heartbeat), heartbeat),
            "unit_of_measurement": m.unit,
        }
        if energy:
            comp["json_attributes_topic"] = comp["state_topic"]
            comp["json_attributes_template"] = ENERGY_SERIES_ATTRIBUTE
        if m.device_class:
            comp["device_class"] = m.device_class
        if m.state_class:
            comp["state_class"] = m.state_class
        if m.precision is not None:
            comp["suggested_display_precision"] = m.precision
        if m.diagnostic:
            comp["entity_category"] = "diagnostic"
        out[ckey] = comp
    diagnostics = (
        ("plant_members", "Plant members", True),
        ("plant_members_stale", "Plant stale members", True),
        ("plant_master", "Plant master", False),
        ("energy_series", "Plant energy series", False),
    )
    for key, name, number in diagnostics:
        ckey = "plant_energy_series" if key == "energy_series" else key
        comp = {
            "platform": "sensor",
            "unique_id": f"{device_id}_{ckey}",
            "default_entity_id": f"sensor.{device_id}_{ckey}",
            "name": name,
            "state_topic": ns.plant_state_topic,
            "value_template": _template(key, "sensor"),
            "expire_after": heartbeat,
            "entity_category": "diagnostic",
        }
        if number:
            comp["state_class"] = "measurement"
            list_key = f"{key}_list"
            comp["json_attributes_topic"] = comp["state_topic"]
            comp["json_attributes_template"] = (
                f"{{{{ {{'members': value_json.get('{list_key}')}} | tojson }}}}"
            )
        if key == "plant_master":
            comp["json_attributes_topic"] = comp["state_topic"]
            comp["json_attributes_template"] = (
                "{{ {'inverter_id': value_json.get('plant_master_id')} | tojson }}"
            )
        out[ckey] = comp
    return out


def inverter_diagnostic_components(
    ns: Namespace, inverter_id: str, states: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Diagnostic entities of one inverter's own device. They stay available while the inverter is
    offline (the state and the reason are exactly what one looks at then): their availability is
    the app's only."""
    device_id = ns.inverter_device_id(inverter_id)
    expire = math.ceil(3 * APP_HEARTBEAT_S)
    out: dict[str, dict[str, Any]] = {}
    for d in INVERTER_DIAGNOSTICS:
        key = f"diag_{d.suffix}"
        comp: dict[str, Any] = {
            "platform": "sensor",
            "unique_id": f"{device_id}_{key}",
            "default_entity_id": f"sensor.{device_id}_{key}",
            "name": d.name,
            "state_topic": ns.inverter_diagnostics_topic(inverter_id),
            "value_template": _template(d.suffix, "sensor"),
            "expire_after": expire,
            "entity_category": "diagnostic",
            "availability": [{"topic": ns.app_status_topic}],
        }
        if d.suffix == "state":
            comp["device_class"] = "enum"
            comp["options"] = list(states)
        if d.kind == "timestamp":
            comp["device_class"] = "timestamp"
        if d.state_class:
            comp["state_class"] = d.state_class
        if d.precision is not None:
            comp["suggested_display_precision"] = d.precision
        if d.attributes:
            comp["json_attributes_topic"] = comp["state_topic"]
            comp["json_attributes_template"] = (
                f"{{{{ {{'conflicts': value_json.get('{CONFLICT_LIST_KEY}')}} | tojson }}}}"
            )
        out[key] = comp
    return out


# -- write entities (0.4.0 part B) ---------------------------------------------------------------


def setting_key(operation: str) -> str:
    """Component key of a released setting."""
    return f"{SETTING_PREFIX}{operation}"


def _setting_template(desc: WriteDescriptor) -> str:
    key = desc.key
    if desc.kind is Kind.BIT:
        return (
            f"{{% set v = value_json.get('{key}') if value_json is mapping else none %}}"
            "{{ 'ON' if v is sameas true else 'OFF' if v is sameas false else 'None' }}"
        )
    return f"{{{{ value_json.get('{key}') if value_json is mapping else none }}}}"


def _number_class(desc: WriteDescriptor) -> str | None:
    if desc.plain_number:
        return None
    return {"A": "current", "%": "battery", "W": "power", "V": "voltage"}.get(desc.unit)


_REGISTER_TAG = re.compile(r"\s*\(R\d+(?: bits? \d+(?:-\d+)?)?\)")


def entity_name(name: str) -> str:
    """The Home Assistant name of a setting: the Web UI name without its register tag."""
    return _REGISTER_TAG.sub("", name)


def setting_component(
    ns: Namespace,
    inverter_id: str,
    desc: WriteDescriptor,
    token: str,
    cap: int | None,
    floor: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """(component key, discovery component) of one released setting. ``cap`` is the model limit
    (A or W) of a model-limited setting; limits are the descriptor's current ones, nothing is
    clamped or guessed (an unknown model keeps the document maximum, the entity is unavailable).
    ``floor`` is the lowest value accepted now (a commissioned reserve), aligned up to the step."""
    device_id = ns.inverter_device_id(inverter_id)
    ckey = setting_key(desc.key)
    platform = {
        Kind.SCALAR: "number",
        Kind.BIT: "switch",
        Kind.TIME: "time",
        Kind.SELECT: "select",
        Kind.BUTTON: "button",
    }[desc.kind]
    if platform == "button":  # a command only: no state, no value template, nothing optimistic
        return ckey, {
            "platform": "button",
            "unique_id": f"{device_id}_{ckey}",
            "default_entity_id": f"button.{device_id}_{ckey}",
            "name": entity_name(desc.name.en),
            "command_topic": ns.command_topic(inverter_id, token, desc.key),
            "payload_press": "PRESS",
            "retain": False,
            "entity_category": "config",
            "availability": [
                {"topic": ns.app_status_topic},
                {"topic": ns.inverter_availability_topic(inverter_id)},
                {"topic": ns.write_availability_topic(inverter_id, desc.key)},
            ],
        }
    comp: dict[str, Any] = {
        "platform": platform,
        "unique_id": f"{device_id}_{ckey}",
        "default_entity_id": f"{platform}.{device_id}_{ckey}",
        "name": entity_name(desc.name.en),
        "state_topic": ns.write_state_topic(inverter_id),
        "value_template": _setting_template(desc),
        "command_topic": ns.command_topic(inverter_id, token, desc.key),
        "optimistic": False,
        "retain": False,
        "entity_category": "config",
        "availability": [
            {"topic": ns.app_status_topic},
            {"topic": ns.inverter_availability_topic(inverter_id)},
            {"topic": ns.write_availability_topic(inverter_id, desc.key)},
        ],
    }
    if platform == "select":  # the option texts are the state and command payloads
        comp["options"] = [o.label.en for o in desc.options]
    if platform == "number":
        high = cap if desc.model_limited and cap is not None else desc.max_raw
        low = desc.min_raw
        if floor is not None and floor > low:
            low += -(-(floor - low) // desc.step_raw) * desc.step_raw
        if high < low:  # an empty permitted domain: the entity is unavailable with its reason, the
            low, high = desc.min_raw, desc.max_raw  # discovered range stays the static, valid one
        comp.update(
            min=desc.display(high if desc.scale < 0 else low),
            max=desc.display(low if desc.scale < 0 else high),
            step=desc.engineering_step,
            mode="box",
        )
        if desc.unit:
            comp["unit_of_measurement"] = desc.unit
        if (device_class := _number_class(desc)) is not None:
            comp["device_class"] = device_class
    return ckey, comp


def write_result_component(ns: Namespace, inverter_id: str) -> tuple[str, dict[str, Any]]:
    device_id = ns.inverter_device_id(inverter_id)
    return WRITE_RESULT_KEY, {
        "platform": "event",
        "unique_id": f"{device_id}_{WRITE_RESULT_KEY}",
        "default_entity_id": f"event.{device_id}_{WRITE_RESULT_KEY}",
        "name": "Write result",
        "state_topic": ns.write_result_topic(inverter_id),
        "event_types": list(EVENT_TYPES),
    }


# -- device documents ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceMeta:
    """Descriptive part of a device (rebuilds the document root, also for cleanup replays)."""

    device_id: str
    name: str
    kind: str  # "app" | "inverter"
    inverter_id: str | None = None
    serial_number: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "kind": self.kind,
            "inverter_id": self.inverter_id,
            "serial_number": self.serial_number,
        }


def _root(
    ns: Namespace, meta: DeviceMeta, *, cleanup: bool = False, app_registered: bool = True
) -> dict[str, Any]:
    device: dict[str, Any] = {
        "identifiers": [meta.device_id],
        "name": meta.name,
        "manufacturer": MANUFACTURER,
    }
    availability = [{"topic": ns.app_status_topic}]
    if meta.kind == "inverter":
        assert meta.inverter_id is not None
        device["model"] = INVERTER_MODEL
        if meta.serial_number:
            device["serial_number"] = meta.serial_number
        if app_registered:
            device["via_device"] = ns.app_device_id
        availability.append({"topic": ns.inverter_availability_topic(meta.inverter_id)})
    if cleanup:
        availability.append({"topic": ns.cleanup_topic})
    return {
        "device": device,
        "origin": {"name": ORIGIN_NAME, "sw_version": __version__},
        "availability": availability,
        "availability_mode": "all",
    }


def tombstone(platform: str) -> dict[str, str]:
    return {"platform": platform}


def device_document(
    ns: Namespace,
    meta: DeviceMeta,
    components: Mapping[str, Mapping[str, Any]],
    tombstones: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Full retained document: active components complete, retired ones platform-only."""
    doc = _root(ns, meta)
    comps: dict[str, Any] = {k: dict(v) for k, v in components.items()}
    for key, platform in (tombstones or {}).items():
        if key in comps:
            raise DiscoveryError(f"{key} is both active and retired")
        comps[key] = tombstone(platform)
    doc["components"] = dict(sorted(comps.items()))
    validate_document(doc)
    return doc


def replay_document(
    ns: Namespace, meta: DeviceMeta, key: str, component: Mapping[str, Any]
) -> dict[str, Any]:
    """Cold-start cleanup step 2: ONE old component under its original unique id. Its template
    renders ``none`` and the extra cleanup availability topic (always ``offline``) keeps it
    unavailable, so no new measurement can appear under the old identity."""
    # no own availability: the cleanup topic of the document root must keep the entity unavailable
    skipped = {"value_template", "availability"}
    allowed = _READ_FIELDS if component.get("platform") in READ_PLATFORMS else _COMPONENT_FIELDS
    comp = {k: v for k, v in component.items() if k in allowed and k not in skipped}
    if comp.get("platform") in WRITE_PLATFORMS:
        comp["command_topic"] = ns.cleanup_command_topic  # never the old live command topic
    if comp.get("platform") != "event":
        comp["value_template"] = NONE_TEMPLATE
    doc = _root(ns, meta, cleanup=True)
    doc["components"] = {key: comp}
    validate_document(doc)
    return doc


def tombstone_document(ns: Namespace, meta: DeviceMeta, key: str, platform: str) -> dict[str, Any]:
    """Cold-start cleanup step 3: exactly the same key as platform-only tombstone."""
    doc = _root(ns, meta)
    doc["components"] = {key: tombstone(platform)}
    validate_document(doc)
    return doc


# -- allowlist -----------------------------------------------------------------------------------


def _unknown(where: str, keys: set[str], allowed: frozenset[str]) -> None:
    extra = sorted(keys - allowed)
    if extra:
        raise DiscoveryError(f"{where}: field(s) not allowed: {', '.join(extra)}")


def _check_availability(key: str, comp: Mapping[str, Any]) -> None:
    own = comp.get("availability")
    if own is not None and (
        not isinstance(own, list)
        or not own
        or not all(isinstance(i, dict) and set(i) == {"topic"} for i in own)
    ):
        raise DiscoveryError(f"component {key}: availability must be a list of topics")


def _require_strings(key: str, comp: Mapping[str, Any], names: tuple[str, ...]) -> None:
    for need in names:
        if not isinstance(comp.get(need), str) or not comp[need]:
            raise DiscoveryError(f"component {key}: {need} is required")


def _validate_button(key: str, comp: Mapping[str, Any]) -> None:
    """A command button: the guarded write shape without any state, never retained."""
    _unknown(
        f"component {key}",
        set(comp),
        frozenset(
            {
                "platform",
                "unique_id",
                "default_entity_id",
                "name",
                "entity_category",
                "availability",
                "command_topic",
                "payload_press",
                "retain",
            }
        ),
    )
    _require_strings(key, comp, ("unique_id", "default_entity_id", "command_topic"))
    if not str(comp["default_entity_id"]).startswith("button."):
        raise DiscoveryError(f"component {key}: default_entity_id must start with button.")
    if comp.get("payload_press") != "PRESS" or comp.get("retain") is not False:
        raise DiscoveryError(f"component {key}: a button presses PRESS and is not retained")
    if comp.get("entity_category") != "config":
        raise DiscoveryError(f"component {key}: a writable entity is in the config category")
    _check_availability(key, comp)


def _validate_write(key: str, comp: Mapping[str, Any]) -> None:
    """A writable setting entity: the only components that carry a command topic. Never retained
    commands, never optimistic, always the config category, no command template."""
    platform = comp["platform"]
    if platform == "button":
        _validate_button(key, comp)
        return
    allowed = _WRITE_FIELDS | (
        _NUMBER_FIELDS
        if platform == "number"
        else frozenset({"options"})
        if platform == "select"
        else frozenset()
    )
    _unknown(f"component {key}", set(comp), allowed)
    _require_strings(
        key,
        comp,
        ("unique_id", "default_entity_id", "state_topic", "value_template", "command_topic"),
    )
    if not str(comp["default_entity_id"]).startswith(f"{platform}."):
        raise DiscoveryError(f"component {key}: default_entity_id must start with {platform}.")
    if comp["command_topic"] == comp["state_topic"]:
        raise DiscoveryError(f"component {key}: command and state topic must differ")
    if comp.get("retain") is not False or comp.get("optimistic") is not False:
        raise DiscoveryError(f"component {key}: commands are not retained and not optimistic")
    if comp.get("entity_category") != "config":
        raise DiscoveryError(f"component {key}: a writable entity is in the config category")
    _check_availability(key, comp)
    if platform == "select":
        options = comp.get("options")
        if (
            not isinstance(options, list)
            or len(options) < 2
            or not all(isinstance(o, str) and o for o in options)
            or len(set(options)) != len(options)
        ):
            raise DiscoveryError(f"component {key}: a select needs 2 or more distinct options")
    if platform != "number":
        return
    low, high, step = comp.get("min"), comp.get("max"), comp.get("step")
    numbers = (low, high, step)
    if not all(isinstance(n, int | float) and not isinstance(n, bool) for n in numbers):
        raise DiscoveryError(f"component {key}: min, max and step must be numbers")
    if not all(math.isfinite(n) for n in numbers):  # type: ignore[arg-type]
        raise DiscoveryError(f"component {key}: min, max and step must be finite")
    if low > high or step <= 0:  # type: ignore[operator]
        raise DiscoveryError(f"component {key}: invalid number range")
    if comp.get("mode") != "box":
        raise DiscoveryError(f"component {key}: a number uses mode box")
    device_class = comp.get("device_class")
    if device_class is not None and (
        device_class not in _NUMBER_CLASSES
        or comp.get("unit_of_measurement") != _NUMBER_UNITS[device_class]
    ):
        raise DiscoveryError(f"component {key}: device class and unit do not match")


def _validate_event(key: str, comp: Mapping[str, Any]) -> None:
    _unknown(f"component {key}", set(comp), _EVENT_FIELDS)
    _require_strings(key, comp, ("unique_id", "default_entity_id", "state_topic"))
    if not str(comp["default_entity_id"]).startswith("event."):
        raise DiscoveryError(f"component {key}: default_entity_id must start with event.")
    types = comp.get("event_types")
    if not isinstance(types, list) or not types or not all(isinstance(t, str) for t in types):
        raise DiscoveryError(f"component {key}: event_types must be a non-empty list of strings")
    _check_availability(key, comp)


def validate_component(key: str, comp: Mapping[str, Any]) -> None:
    """Contract for one component (active or tombstone): measurement platforms have no command
    topic, writable platforms have exactly the guarded shape of ``_validate_write``."""
    platform = comp.get("platform")
    if platform not in PLATFORMS:
        raise DiscoveryError(f"component {key}: platform {platform!r} is not allowed")
    if set(comp) == {"platform"}:
        return  # tombstone
    if platform in WRITE_PLATFORMS:
        return _validate_write(key, comp)
    if platform == "event":
        return _validate_event(key, comp)
    _unknown(f"component {key}", set(comp), _READ_FIELDS)
    _require_strings(key, comp, ("unique_id", "default_entity_id", "state_topic", "value_template"))
    if not str(comp["default_entity_id"]).startswith(f"{platform}."):
        raise DiscoveryError(f"component {key}: default_entity_id must start with {platform}.")
    if platform == "sensor" and set(comp) & _BINARY_ONLY:
        raise DiscoveryError(f"component {key}: binary_sensor field on a sensor")
    if platform == "binary_sensor" and set(comp) & _SENSOR_ONLY:
        raise DiscoveryError(f"component {key}: sensor field on a binary_sensor")
    if comp.get("entity_category", "diagnostic") not in _ENTITY_CATEGORIES:
        raise DiscoveryError(f"component {key}: entity_category not allowed")
    if comp.get("state_class", "measurement") not in _STATE_CLASSES:
        raise DiscoveryError(f"component {key}: state_class not allowed")
    _check_availability(key, comp)
    options = comp.get("options")
    if options is not None and (
        not isinstance(options, list) or not options or not all(isinstance(o, str) for o in options)
    ):
        raise DiscoveryError(f"component {key}: options must be a non-empty list of strings")
    if options is not None and ("unit_of_measurement" in comp or "state_class" in comp):
        raise DiscoveryError(f"component {key}: enum sensors take no unit or state class")
    if "expire_after" in comp and (
        not isinstance(comp["expire_after"], int) or isinstance(comp["expire_after"], bool)
    ):
        raise DiscoveryError(f"component {key}: expire_after must be an integer")


def validate_document(doc: Mapping[str, Any]) -> bytes:
    """Strict check of a whole device document; returns its UTF-8 JSON (size-limited)."""
    _unknown("document", set(doc), _ROOT_FIELDS)
    device = doc.get("device")
    if not isinstance(device, dict) or not device.get("identifiers"):
        raise DiscoveryError("device.identifiers is required")
    _unknown("device", set(device), _DEVICE_FIELDS)
    origin = doc.get("origin")
    if not isinstance(origin, dict) or not origin.get("name"):
        raise DiscoveryError("origin.name is required")
    _unknown("origin", set(origin), _ORIGIN_FIELDS)
    availability = doc.get("availability")
    if not isinstance(availability, list) or not availability:
        raise DiscoveryError("availability is required")
    for item in availability:
        if not isinstance(item, dict):
            raise DiscoveryError("availability entries must be objects")
        _unknown("availability", set(item), _AVAILABILITY_FIELDS)
    if doc.get("availability_mode") != "all":
        raise DiscoveryError("availability_mode must be 'all'")
    components = doc.get("components")
    if not isinstance(components, dict) or not components:
        raise DiscoveryError("components must not be empty")
    for key, comp in components.items():
        if not isinstance(comp, dict):
            raise DiscoveryError(f"component {key}: must be an object")
        validate_component(key, comp)
    return encode(doc)


def encode(doc: Mapping[str, Any]) -> bytes:
    """Compact, deterministic JSON without NaN/Infinity; refuses oversized documents."""
    try:
        data = json.dumps(
            doc, separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DiscoveryError(f"document is not valid JSON: {exc}") from None
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DiscoveryError(f"document is {len(data)} bytes, limit is {MAX_DOCUMENT_BYTES}")
    return data


# -- state payloads ------------------------------------------------------------------------------


def clean_value(entry: Entry, value: Value) -> tuple[Value, bool]:
    """(publishable value, was it replaced by null). Never invents a number."""
    if value is None:
        return None, False
    if entry.platform == "binary_sensor":
        return (value, False) if isinstance(value, bool) else (None, True)
    if isinstance(value, bool):
        return None, True  # a sensor never shows a boolean
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (None, True)
    if entry.type == "enum" and entry.enum:
        if not (isinstance(value, str) and value in set(entry.enum.values())):
            return None, True  # e.g. "unknown_99": the physical enum is unknown
        return entry.label(value), False
    return value, False


def timestamp(wall_ts: float) -> str:
    return datetime.datetime.fromtimestamp(wall_ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _profile_presented(entry: Entry, reported: Presented | None, value: Value) -> Presented:
    """The hardware profile's value as the state of a field the device did not report. The label
    comes from the same rule applied to the profile value; ``source`` and ``profile_expected`` keep
    it apart from a device measurement and the raw report stays in the attributes."""
    spec = STATES[entry.key]
    attrs = {**(reported.attributes if reported else {}), "source": "profile"}
    attrs["profile_expected"] = value
    if not isinstance(value, int) or isinstance(value, bool):
        label = reported.label if reported else NOT_REPORTED
    else:
        word = value << entry.shift if entry.mask is not None else value
        result = spec.rule(entry, (word,))
        label = (result[0] if isinstance(result, tuple) else result) or NOT_REPORTED
    attrs["state_de"] = label.de  # the German state follows the presented label, never the raw one
    return Presented(label, attrs)


def state_payload(
    *,
    cycle_id: int,
    wall_ts: float,
    energy_generation: int,
    energy_series: str,
    values: Mapping[str, Value],
    entries: Mapping[str, Entry],
    profile_keys: frozenset[str] = frozenset(),
    words: Mapping[int, int] | None = None,
) -> tuple[bytes, int]:
    """Group JSON: only the selected keys, ``null`` for unknown values. Returns (bytes, number of
    values replaced by null because they were not publishable)."""
    body: dict[str, Any] = {
        "cycle_id": cycle_id,
        "timestamp": timestamp(wall_ts),
        "energy_generation": energy_generation,
        "energy_series": energy_series,
    }
    replaced = 0
    for key, value in values.items():
        entry = entries.get(key)
        if entry is None:
            continue
        body[key], flag = clean_value(entry, value)
        replaced += flag
        decoded = body[key]
        if key in STATES:
            presented = present(entry, words) if words else None
            if key in profile_keys and entry.profile_fallback:
                # the device left the field out: keep the profile's capability as the value,
                # the raw report stays in the attributes next to the provenance
                presented = _profile_presented(entry, presented, value)
            elif entry.profile_fallback and presented is not None:
                presented.attributes["source"] = "device"
            if STATES[key].mode == "text":
                body[key] = presented.label.en if presented else None
            elif words and not primary_valid(entry, words):
                body[key] = None  # a binary of a legacy word that is not whole 0/1
            body[attributes_key(key)] = presented.attributes if presented else None
        if entry.type == "alarms":
            body[f"{key}{ALARM_LIST_SUFFIX}"] = None if decoded is None else active_items(decoded)
    if profile_keys:
        body["sources"] = {k: "profile" for k in sorted(profile_keys) if k in values}
    return encode(body), replaced
