"""Profile data format (SPEC §5) and loader.

Profiles are package data (YAML) validated completely at load time; nothing is read from outside
the package at runtime.
"""

from __future__ import annotations

import functools
import hashlib
import re
from importlib import resources
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from deye_monitor.profiles.features import FEATURE_VALUES, Feature, Features

Group = Literal["identity", "status", "battery", "ac", "pv", "energy", "settings"]
Category = Literal["power", "energy", "battery", "grid", "pv", "status", "settings", "diagnostic"]
MeasurementPoint = Literal[
    "grid_terminal",
    "battery",
    "bms",
    "gen_port",
    "inverter_output",
    "ups_load",
    "pv",
    "parallel",
    "tou",
    "diagnostic",
]
EntryType = Literal["u16", "s16", "u32", "s32", "ascii", "bitfield", "enum", "hhmm", "raw"]
SupportStatus = Literal["supported", "placeholder", "unsupported_for_profile", "unverified"]
Release = Literal["confirmed", "profile_dependent", "unresolved"]
Platform = Literal["sensor", "binary_sensor"]
PollKey = Literal["fast_s", "energy_s", "settings_s", "identity_s"]

# SPEC §5.1: the only entries that may use ``scale_source: energy_resolution``.
ENERGY_DAILY_ADDRS = frozenset({514, 515, 520, 521, 526, 529})
ENERGY_TOTAL_PAIRS = frozenset(
    {(516, 517), (518, 519), (522, 523), (524, 525), (527, 528), (534, 535)}
)

_ZERO_CATEGORIES = frozenset({"power", "grid", "pv", "battery"})
_ZERO_CLASSES = frozenset({"voltage", "frequency", "battery", "temperature"})
_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PAIRS = frozenset({(620, 709), (709, 620)})  # V105.8: standalone fields, never a pair


def _as_tuple(value: object) -> object:
    return (value,) if isinstance(value, int) and not isinstance(value, bool) else value


ProfileFallback = Literal["mppt_count", "phase_count", "battery_inputs"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ProfileError(Exception):
    """The profile file is invalid."""


class Names(_Model):
    de: str = Field(min_length=1)
    en: str = Field(min_length=1)


class ProducerEvidence(_Model):
    """Firmware producer evidence (REVIEW2 §5.3/§5.4). Offsets are file bytes of the image."""

    mcu2: Annotated[str, Field(pattern=r"^\d{4}$")]
    image_sha256: str
    store_byte: Annotated[int, Field(ge=0)]
    effect: Literal["store", "zero_fill", "alias", "copy", "computed"]
    ram_word: Annotated[int, Field(ge=0, le=0xFFFF)] | None = None  # copy/alias: internal source
    load_byte: Annotated[int, Field(ge=0)] | None = None  # copy/alias: file byte of the load
    note: str | None = None

    @model_validator(mode="after")
    def _sha(self) -> ProducerEvidence:
        if not _SHA256_RE.match(self.image_sha256):
            raise ValueError("image_sha256 must be 64 lowercase hex characters")
        return self


class Predicate(_Model):
    """Usage predicate over plant features. ``{}`` is always true."""

    all_of: tuple[Predicate, ...] | None = None
    any_of: tuple[Predicate, ...] | None = None
    feature: Feature | None = None
    equals: bool | str | None = None
    in_: tuple[str, ...] | None = Field(default=None, alias="in")

    @model_validator(mode="after")
    def _shape(self) -> Predicate:
        forms = [
            self.all_of is not None,
            self.any_of is not None,
            self.feature is not None,
        ]
        if sum(forms) > 1:
            raise ValueError("use exactly one of all_of, any_of or feature")
        if self.all_of is not None and not self.all_of:
            raise ValueError("all_of must not be empty")
        if self.any_of is not None and not self.any_of:
            raise ValueError("any_of must not be empty")
        if self.feature is None:
            if self.equals is not None or self.in_ is not None:
                raise ValueError("equals/in require a feature")
            return self
        if (self.equals is None) == (self.in_ is None):
            raise ValueError("a feature comparison needs exactly one of equals or in")
        allowed = FEATURE_VALUES[self.feature]
        values = (self.equals,) if self.equals is not None else tuple(self.in_ or ())
        for v in values:
            if v not in allowed or type(v) is not type(allowed[0]):
                raise ValueError(f"{v!r} is not a valid value of feature {self.feature.value}")
        return self

    def evaluate(self, features: Features) -> bool:
        if self.all_of is not None:
            return all(p.evaluate(features) for p in self.all_of)
        if self.any_of is not None:
            return any(p.evaluate(features) for p in self.any_of)
        if self.feature is None:
            return True
        actual = features.value(self.feature)
        if self.equals is not None:
            return actual == self.equals
        return actual in (self.in_ or ())

    @property
    def is_always(self) -> bool:
        return self.all_of is None and self.any_of is None and self.feature is None

    def features_used(self) -> frozenset[Feature]:
        used: set[Feature] = set()
        for child in (*(self.all_of or ()), *(self.any_of or ())):
            used |= child.features_used()
        if self.feature is not None:
            used.add(self.feature)
        return frozenset(used)


class Entry(_Model):
    key: str
    name: Names
    description: Names | None = None  # one plain sentence for the UI (T11)
    addr: Annotated[int, Field(ge=0, le=0xFFFF)] | None = None
    low: Annotated[int, Field(ge=0, le=0xFFFF)] | None = None
    high: Annotated[int, Field(ge=0, le=0xFFFF)] | None = None
    addrs: tuple[Annotated[int, Field(ge=0, le=0xFFFF)], ...] | None = None
    type: EntryType
    mask: Annotated[int, Field(ge=1, le=0xFFFF)] | None = None
    shift: Annotated[int, Field(ge=0, le=15)] = 0
    enum: dict[int, str] | None = None
    scale: Annotated[float, Field(allow_inf_nan=False)] | None = None
    raw_offset: Annotated[float, Field(allow_inf_nan=False)] = 0.0
    # Offset by main firmware (R15) where the producer differs between paths; a firmware that is
    # not listed uses the plausibility rule of ``decode`` (shown as a hint, never as certain).
    fw_offsets: (
        dict[Annotated[int, Field(ge=0, le=0xFFFF)], Annotated[float, Field(allow_inf_nan=False)]]
        | None
    ) = None
    scale_source: Literal["energy_resolution"] | None = None
    # Words that mean "not reported by the device" (decoded as unknown, never as a value). Per
    # field, never global: 0xFFFF is a valid value of signed or 32-bit entries.
    not_reported: Annotated[
        tuple[Annotated[int, Field(ge=0, le=0xFFFF)], ...], BeforeValidator(_as_tuple)
    ] = ()
    # enum / value_map entries: every code the profile does not define counts as not reported
    undefined_not_reported: bool = False
    # u16 entries: defined code -> published number; every other code is unknown
    value_map: dict[Annotated[int, Field(ge=0, le=0xFFFF)], int] | None = None
    # enum entries: published label per enum value (what Home Assistant shows)
    enum_labels: dict[str, str] | None = None
    # shown with the hardware profile's value (source ``profile``) while the device reports none
    profile_fallback: ProfileFallback | None = None
    display: Literal["hex"] | None = None  # raw words shown as hex digits (no prefix)
    unit: str | None = None
    device_class: str | None = None
    state_class: Literal["measurement", "total", "total_increasing"] | None = None
    entity_category: Literal["diagnostic", "config"] | None = None
    platform: Platform = "sensor"
    category: Category
    point: MeasurementPoint
    group: Group
    tags: tuple[str, ...] = ()
    requires: Predicate
    supported_on: tuple[str, ...] = Field(min_length=1)
    support_status: SupportStatus
    release: Release
    source: str = Field(min_length=1)
    confidence: str = Field(min_length=1)
    producer_evidence: tuple[ProducerEvidence, ...] = ()
    default_enabled: bool
    # "Only zeros" is worth a hint for this entry: ``None`` follows the category default of
    # ``observes_zero``; a profile sets it where the default is wrong for the entry.
    observe_zero: bool | None = None
    internal: bool = False
    mandatory: bool = False
    notes: str | None = None

    # -- derived -----------------------------------------------------------------------------

    @property
    def addresses(self) -> tuple[int, ...]:
        """Every wire address the entry needs, partners included, in word order low..high."""
        if self.addr is not None:
            return (self.addr,)
        if self.low is not None and self.high is not None:
            return (self.low, self.high)
        return self.addrs or ()

    @property
    def declares_not_reported(self) -> bool:
        return bool(self.not_reported) or self.undefined_not_reported

    def is_not_reported(self, word: int) -> bool:
        """The device leaves this field out: a declared sentinel, or (``undefined_not_reported``)
        a code the profile does not define."""
        if word in self.not_reported:
            return True
        if not self.undefined_not_reported:
            return False
        defined = self.enum.keys() if self.enum is not None else (self.value_map or {}).keys()
        return word not in defined

    def label(self, value: str) -> str:
        """Published label of an enum value (the value itself without ``enum_labels``)."""
        return (self.enum_labels or {}).get(value, value)

    @property
    def is_numeric(self) -> bool:
        return self.type in ("u16", "s16", "u32", "s32")

    @property
    def is_physical(self) -> bool:
        """Physical sensor: supported hardware and a decoded, released meaning (SPEC §5)."""
        return (
            self.support_status == "supported"
            and self.release in ("confirmed", "profile_dependent")
            and not self.internal
        )

    @property
    def is_raw_diagnostic(self) -> bool:
        """Offered at most as an explicitly marked, default-disabled raw value."""
        return (
            self.support_status in ("supported", "unverified")
            and self.release == "unresolved"
            and not self.internal
        )

    @property
    def observes_zero(self) -> bool:
        """0 is suspicious for this entry. Only physical measurements that are expected to be
        non-zero in operation qualify (voltages, frequency, SOC, temperatures, PV with daylight);
        alarm/fault/status words, bit fields, enums, raw words, counters, settings and the
        gen port (an unused port reads 0) are healthy at 0 and default to no observation."""
        if self.observe_zero is not None:
            return self.observe_zero
        if not self.is_numeric or self.platform != "sensor" or self.release == "unresolved":
            return False
        if self.category not in _ZERO_CATEGORIES or self.point == "gen_port":
            return False
        return self.device_class in _ZERO_CLASSES or (
            self.point == "pv" and self.device_class in ("power", "current")
        )

    @property
    def zero_needs_daylight(self) -> bool:
        """PV values are 0 at night: only daylight samples count."""
        return self.point == "pv"

    @property
    def selectable(self) -> bool:
        return self.is_physical or self.is_raw_diagnostic

    # -- validation --------------------------------------------------------------------------

    @model_validator(mode="after")
    def _validate(self) -> Entry:
        problems: list[str] = []
        if not _KEY_RE.match(self.key):
            problems.append("key must match [a-z0-9_]{1,64}")
        problems += self._layout_problems()
        problems += self._decode_problems()
        problems += self._policy_problems()
        if problems:
            raise ValueError(f"{self.key}: " + "; ".join(problems))
        return self

    def _layout_problems(self) -> list[str]:
        forms = [self.addr is not None, self.low is not None or self.high is not None]
        forms.append(self.addrs is not None)
        out: list[str] = []
        if sum(forms) != 1:
            return ["exactly one of addr, low+high or addrs is required"]
        if (self.low is None) != (self.high is None):
            out.append("32-bit entries need both low and high")
        if self.low is not None and self.low == self.high:
            out.append("low and high must differ")
        if (self.low, self.high) in _FORBIDDEN_PAIRS:
            out.append("R620/R709 must never be paired")
        if self.addrs is not None and (not self.addrs or len(set(self.addrs)) != len(self.addrs)):
            out.append("addrs must be non-empty and unique")
        wide = self.type in ("u32", "s32")
        if wide and self.low is None:
            out.append(f"{self.type} needs explicit low and high")
        if self.type == "ascii" and self.addrs is None:
            out.append("ascii needs addrs")
        if not wide and self.type != "ascii" and self.addr is None:
            out.append(f"{self.type} needs addr")
        if self.mask is not None:
            if self.addr is None:
                out.append("mask requires a single addr")
            elif self.mask & ((1 << self.shift) - 1):
                out.append("mask has bits below shift")
        elif self.shift:
            out.append("shift requires mask")
        return out

    def _decode_problems(self) -> list[str]:
        out: list[str] = []
        if (self.type == "enum") != (self.enum is not None):
            out.append("enum mapping is required for (and only for) type enum")
        if self.is_numeric:
            has_scale = self.scale is not None
            has_source = self.scale_source is not None
            if self.release == "unresolved":
                if has_scale or has_source or self.raw_offset:
                    out.append("unresolved numeric entries carry no scale/offset (raw value only)")
            elif has_scale == has_source:
                out.append("numeric entries need exactly one of scale and scale_source")
            if self.scale is not None and self.scale <= 0:
                out.append("scale must be > 0")
            if self.fw_offsets is not None and (
                self.scale is None or self.raw_offset or self.release == "unresolved"
            ):
                out.append("fw_offsets need a scale, a released entry and no raw_offset")
        if self.declares_not_reported and self.addr is None:
            out.append("not_reported applies to single-word entries")
        if self.undefined_not_reported and self.enum is None and self.value_map is None:
            out.append("undefined_not_reported needs an enum or a value_map")
        if self.value_map is not None and self.type != "u16":
            out.append("value_map applies to u16 entries")
        if self.enum is not None:
            if self.enum_labels is None or set(self.enum_labels) != set(self.enum.values()):
                out.append("enum_labels must label exactly the enum values")
        elif self.enum_labels is not None:
            out.append("enum_labels apply to enum entries")
        if self.profile_fallback is not None and not (
            self.type == "u16" and (self.not_reported or self.undefined_not_reported)
        ):
            out.append("profile_fallback needs a u16 entry that declares not_reported")
        if self.display is not None and (self.type not in ("raw", "u32") or self.scale is not None):
            out.append("display requires an unscaled raw or u32 entry")
        if not self.is_numeric and (
            self.scale is not None or self.scale_source or self.raw_offset or self.fw_offsets
        ):
            out.append(f"{self.type} entries carry no scale/offset")
        if self.platform == "binary_sensor" and not (
            self.type == "bitfield" and self.mask is not None and self.mask & (self.mask - 1) == 0
        ):
            out.append("binary_sensor needs a bitfield with a single-bit mask")
        if self.observe_zero and not self.is_numeric:
            out.append("observe_zero applies to numeric entries")
        if self.scale_source is not None:
            out += self._energy_problems()
        return out

    def _energy_problems(self) -> list[str]:
        out: list[str] = []
        daily = self.type == "u16" and self.addr in ENERGY_DAILY_ADDRS and self.mask is None
        total = (
            self.type == "u32"
            and self.low is not None
            and (self.low, self.high) in ENERGY_TOTAL_PAIRS
        )
        if not (daily or total):
            out.append("scale_source energy_resolution is only allowed on the SPEC §5.1 whitelist")
        if (self.unit, self.device_class, self.state_class) != (
            "kWh",
            "energy",
            "total_increasing",
        ):
            out.append("energy counters need unit kWh, device_class energy, total_increasing")
        return out

    def _policy_problems(self) -> list[str]:
        out: list[str] = []
        if self.release == "unresolved" and (self.unit or self.device_class or self.state_class):
            out.append("physical unit/class only for release != unresolved")
        if self.support_status == "placeholder":
            if not self.producer_evidence:
                out.append("placeholder needs producer_evidence")
            if self.default_enabled:
                out.append("placeholder must not be default_enabled")
        if self.support_status == "unverified" and self.release != "unresolved":
            out.append("unverified entries must be release unresolved")
        if self.default_enabled and not (
            self.support_status == "supported" and self.release != "unresolved"
        ):
            out.append("default_enabled needs support_status supported and a released decoding")
        if self.internal and (self.default_enabled or self.platform != "sensor"):
            out.append("internal entries create no entity and are never default_enabled")
        if self.mandatory and self.support_status != "supported":
            out.append("mandatory entries must be supported")
        return out


class GroupConfig(_Model):
    poll: PollKey


class Profile(_Model):
    profile: str
    protocol_revision: str
    hardware: str
    energy_resolution_key: str
    groups: dict[Group, GroupConfig]
    entries: tuple[Entry, ...]
    revision: str = ""  # SHA-256 of the YAML bytes, set by the loader

    def entry(self, key: str) -> Entry:
        for e in self.entries:
            if e.key == key:
                return e
        raise KeyError(key)

    @property
    def known_addresses(self) -> frozenset[int]:
        return frozenset(a for e in self.entries for a in e.addresses)

    @model_validator(mode="after")
    def _validate(self) -> Profile:
        problems: list[str] = []
        keys = [e.key for e in self.entries]
        for k in sorted({k for k in keys if keys.count(k) > 1}):
            problems.append(f"duplicate key {k}")
        for e in self.entries:
            if e.group not in self.groups:
                problems.append(f"{e.key}: group {e.group} is not configured")
            if self.hardware not in e.supported_on:
                problems.append(f"{e.key}: supported_on must list {self.hardware}")
        problems += self._address_problems()
        by_key = {e.key: e for e in self.entries}
        r65 = by_key.get(self.energy_resolution_key)
        if r65 is None or r65.addr != 65 or r65.mask is not None:
            problems.append("energy_resolution_key must name the unmasked R65 entry")
        if r65 is not None and r65.support_status != "supported":
            problems.append("the R65 entry must be supported")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _address_problems(self) -> list[str]:
        """A word belongs to one entity. Allowed overlaps: masked bit fields with disjoint masks,
        and any number of those on top of at most one unmasked ``bitfield``/``raw`` entry that
        exposes the whole word (raw view plus decoded bits). Internal entries are logic-only
        twins and may overlap."""
        words: dict[int, list[Entry]] = {}
        for e in self.entries:
            if not e.internal:
                for a in e.addresses:
                    words.setdefault(a, []).append(e)
        out: list[str] = []
        for addr, items in sorted(words.items()):
            if len(items) == 1:
                continue
            masked = [e for e in items if e.mask is not None]
            whole = [e for e in items if e.mask is None]
            if len(whole) > 1 or (whole and whole[0].type not in ("bitfield", "raw")):
                out.append(f"address {addr} is claimed twice ({', '.join(e.key for e in whole)})")
                continue
            seen = 0
            for e in masked:
                assert e.mask is not None
                if seen & e.mask:
                    out.append(f"address {addr}: overlapping masks (at {e.key})")
                seen |= e.mask
        return out


def _read_yaml(name: str, kind: str) -> tuple[bytes, Any]:
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ProfileError(f"invalid {kind} name {name!r}")
    folder = resources.files("deye_monitor.profiles")
    if kind == "hardware":
        folder = folder / "hardware"
    try:
        raw = (folder / f"{name}.yaml").read_bytes()
    except FileNotFoundError:
        raise ProfileError(f"unknown {kind} profile {name!r}") from None
    return raw, yaml.safe_load(raw)


def format_error(exc: ValidationError) -> str:
    lines = []
    for e in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(p) for p in e["loc"]) or "profile"
        lines.append(f"{loc}: {e['msg'].removeprefix('Value error, ')}")
    return "\n".join(lines)


@functools.cache
def load_profile(name: str) -> Profile:
    """Load and validate a register profile from package data."""
    raw, data = _read_yaml(name, "register")
    try:
        profile = Profile.model_validate({**data, "revision": hashlib.sha256(raw).hexdigest()})
    except ValidationError as exc:
        raise ProfileError(format_error(exc)) from None
    if profile.profile != name:
        raise ProfileError(f"profile name {profile.profile!r} does not match file {name!r}")
    return profile
