"""Home Assistant MQTT Device Discovery: namespace, payload builders and the read-only allowlist.

Normative contract: ``reviewer-codex-6.1-t8-contract.md`` (HA Core 2026.10.4). Everything here is
pure: no I/O, no clock, no MQTT client.

* Only the platforms ``sensor`` and ``binary_sensor`` exist. There is no command topic, command
  template or alias of one: ``validate_document`` accepts exclusively the field names listed
  below (an allowlist, not a denylist) and is applied to every document before it is sent.
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
from deye_monitor.profiles.decode import Value
from deye_monitor.profiles.schema import Entry, Profile

PLATFORMS: Final = ("sensor", "binary_sensor")
MAX_DOCUMENT_BYTES: Final = 256 * 1024  # application limit per device discovery document
APP_DEVICE_SUFFIX: Final = "app"
ORIGIN_NAME: Final = "Deye SG04LP3 Monitor"
MANUFACTURER: Final = "Deye"
INVERTER_MODEL: Final = "SG04LP3 three-phase low-voltage hybrid inverter"
NONE_TEMPLATE: Final = "{{ none }}"
APP_HEARTBEAT_S: Final = 30.0
STATIC_GROUPS: Final = frozenset({"identity", "settings"})

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
    }
)
_SENSOR_ONLY = frozenset(
    {
        "state_class",
        "unit_of_measurement",
        "suggested_display_precision",
        "options",
        "json_attributes_topic",
        "json_attributes_template",
    }
)
_BINARY_ONLY = frozenset({"payload_on", "payload_off"})
_ENTITY_CATEGORIES = frozenset({"diagnostic"})
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

    def inverter_availability_topic(self, inverter_id: str) -> str:
        return f"{self.state_prefix}/{self.instance}/{inverter_id}/availability"

    def group_topic(self, inverter_id: str, group: str) -> str:
        return f"{self.state_prefix}/{self.instance}/{inverter_id}/{group}"


# -- component builders --------------------------------------------------------------------------


SERIES_RE: Final = re.compile(r"^([a-z0-9]{8})?g[0-9]+$")


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


def inverter_component(
    ns: Namespace,
    inverter_id: str,
    entry: Entry,
    *,
    series: str,
    poll: PollConfig,
    profile: Profile,
) -> tuple[str, dict[str, Any]]:
    """(component key, discovery component) of one selected profile entry."""
    device_id = ns.inverter_device_id(inverter_id)
    ckey = component_key(entry, series)
    name = entry.name.en + (f" ({series})" if entry.scale_source else "")
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
    if entry.profile_fallback and entry.platform == "sensor":
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
    InverterDiagnostic("path", "connection path"),
    InverterDiagnostic("serial", "bound serial number"),
    InverterDiagnostic("firmware", "firmware words"),
    InverterDiagnostic("role", "parallel role (R336)"),
    InverterDiagnostic("energy_resolution", "energy resolution", "number", precision=1),
    InverterDiagnostic("energy_source", "energy resolution source"),
    InverterDiagnostic("energy_status", "energy confirmation"),
    InverterDiagnostic("energy_series", "energy series"),
    InverterDiagnostic(
        "capability_conflicts", "capability conflicts", "number", "measurement", attributes=True
    ),
    InverterDiagnostic("bus_utilisation", "bus utilisation", "number", "measurement", 3),
    InverterDiagnostic("transport_timeouts", "transport timeouts", "number", "total_increasing"),
    InverterDiagnostic(
        "transport_frame_errors", "transport frame errors", "number", "total_increasing"
    ),
    InverterDiagnostic("transport_resyncs", "transport resyncs", "number", "total_increasing"),
    InverterDiagnostic("transport_cooldown", "transport cooldown"),
    InverterDiagnostic("last_success", "last successful read", "timestamp"),
)
# Only present while R65 is reported; the raw value then lives in the app diagnostics only.
R65_DIAGNOSTICS: Final = (
    InverterDiagnostic("r65_raw", "R65 raw value", "number"),
    InverterDiagnostic("r65_status", "R65 last reading"),
)
ENERGY_SOURCES: Final = {"override": "user choice", "r65": "R65"}
CONFLICT_LIST_KEY: Final = "capability_conflict_list"
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


def inverter_diagnostic_values(diag: Mapping[str, Any] | None) -> dict[str, Any]:
    """The curated diagnostic values of one inverter from ``InverterRuntime.diagnostics()``.
    Only the fields below are ever read (no pass-through of arbitrary dictionaries, no secrets);
    anything unknown or malformed is ``None``."""
    out: dict[str, Any] = {d.suffix: None for d in (*INVERTER_DIAGNOSTICS, *R65_DIAGNOSTICS)}
    out[CONFLICT_LIST_KEY] = []
    if not isinstance(diag, Mapping):
        return out

    def sub(name: str) -> Mapping[str, Any]:
        value = diag.get(name)
        return value if isinstance(value, Mapping) else {}

    path, endpoint = _text(diag.get("active_path")), _text(diag.get("endpoint"))
    out["path"] = f"{path}:{endpoint}" if path and endpoint else path
    out["serial"] = _text(diag.get("bound_serial"))
    words = sub("identity").get("firmware_words")
    if isinstance(words, Mapping):
        parts = [
            f"r{a}={w:04X}"
            for a in (15, 17, 18)
            if isinstance(w := words.get(a), int) and not isinstance(w, bool)
        ]
        out["firmware"] = " ".join(parts) or None
    role = sub("identity").get("role")
    if isinstance(role, Mapping):

        def flag(v: object) -> str:
            return "?" if not isinstance(v, bool) else ("yes" if v else "no")

        phase = _text(role.get("phase")) or "?"
        out["role"] = (
            f"parallel={flag(role.get('parallel_enable'))} "
            f"master={flag(role.get('master'))} phase={phase}"
        )
    r65 = sub("r65")
    out["r65_raw"] = _number(r65.get("raw"))
    ok = r65.get("last_read_ok")
    out["r65_status"] = None if not isinstance(ok, bool) else ("ok" if ok else "failed")
    energy = sub("energy")
    out["energy_resolution"] = _number(energy.get("effective_resolution"))
    source = _text(energy.get("source"))
    out["energy_source"] = ENERGY_SOURCES.get(source, source) if source else None
    blocked, pending = _text(energy.get("blocked")), energy.get("pending_change")
    if blocked:
        out["energy_status"] = f"blocked: {blocked}"
    elif energy.get("needs_choice") is True:
        out["energy_status"] = "r65_invalid"
    elif pending is not None:
        out["energy_status"] = "change_pending"
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


def app_components(
    ns: Namespace,
    inverters: Mapping[str, str],
    states: tuple[str, ...],
    r65_unreported: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Diagnostic entities of the app device. Per inverter the connection state and reason and the
    curated diagnostics (``INVERTER_DIAGNOSTICS``) live here, so an inverter without selected
    sensors still has its status visible while its own device has no entities at all. The R65
    entities of an inverter in ``r65_unreported`` (the device reports no R65) are left out."""
    device_id = ns.app_device_id
    expire = math.ceil(3 * APP_HEARTBEAT_S)
    out: dict[str, dict[str, Any]] = {}

    def add(key: str, name: str, **extra: Any) -> None:
        comp: dict[str, Any] = {
            "platform": "sensor",
            "unique_id": f"{device_id}_{key}",
            "default_entity_id": f"sensor.{device_id}_{key}",
            "name": name,
            "state_topic": ns.app_state_topic,
            "value_template": _template(key, "sensor"),
            "expire_after": expire,
            "entity_category": "diagnostic",
        }
        comp.update(extra)
        out[key] = comp

    for diag in APP_DIAGNOSTICS:
        extra: dict[str, Any] = {}
        if diag.state_class:
            extra["state_class"] = diag.state_class
        add(diag.key, diag.name, **extra)
    for inverter_id, label in inverters.items():
        add(
            f"inverter_{inverter_id}_state",
            f"{label} state",
            device_class="enum",
            options=list(states),
        )
        add(f"inverter_{inverter_id}_reason", f"{label} state reason")
        shown = (
            INVERTER_DIAGNOSTICS
            if inverter_id in r65_unreported
            else (
                *INVERTER_DIAGNOSTICS,
                *R65_DIAGNOSTICS,
            )
        )
        for d in shown:
            extra = {}
            if d.kind == "timestamp":
                extra["device_class"] = "timestamp"
            if d.state_class:
                extra["state_class"] = d.state_class
            if d.precision is not None:
                extra["suggested_display_precision"] = d.precision
            if d.attributes:
                extra["json_attributes_topic"] = ns.app_state_topic
                extra["json_attributes_template"] = (
                    "{{ {'conflicts': value_json.get('"
                    f"inverter_{inverter_id}_{CONFLICT_LIST_KEY}"
                    "')} | tojson }}"
                )
            add(f"inverter_{inverter_id}_{d.suffix}", f"{label} {d.name}", **extra)
    return out


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
    comp = {k: v for k, v in component.items() if k in _COMPONENT_FIELDS and k != "value_template"}
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


def validate_component(key: str, comp: Mapping[str, Any]) -> None:
    """Read-only contract for one component (active or tombstone)."""
    platform = comp.get("platform")
    if platform not in PLATFORMS:
        raise DiscoveryError(f"component {key}: platform {platform!r} is not allowed")
    if set(comp) == {"platform"}:
        return  # tombstone
    _unknown(f"component {key}", set(comp), _COMPONENT_FIELDS)
    for need in ("unique_id", "default_entity_id", "state_topic", "value_template"):
        if not isinstance(comp.get(need), str) or not comp[need]:
            raise DiscoveryError(f"component {key}: {need} is required")
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


def state_payload(
    *,
    cycle_id: int,
    wall_ts: float,
    energy_generation: int,
    energy_series: str,
    values: Mapping[str, Value],
    entries: Mapping[str, Entry],
    profile_keys: frozenset[str] = frozenset(),
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
    if profile_keys:
        body["sources"] = {k: "profile" for k in sorted(profile_keys) if k in values}
    return encode(body), replaced
