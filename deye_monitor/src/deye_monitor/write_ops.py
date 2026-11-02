"""The write whitelist (SPEC §10): 41 reviewed descriptors and their pure encoding rules.

No device I/O and no state. A descriptor names ONE field of ONE register, how an engineering
value maps onto it, the accepted range, the preconditions and the risk text shown to the owner.
Every wire word is an unsigned 16-bit value; a value that does not encode exactly into an integer
is refused (no rounding, no clamping). The register/mask table must agree with the one in the
lowest byte guard (``protocol.guard.WRITABLE_MASKS``).

The preconditions are declarative (``requires_r111``, ``needs_smart_load``, ``tou``, ``below`` /
``above``, ``current_cap`` / ``power_cap``, ``whole_word_bool``); ``writes.py`` evaluates them
against fresh reads at execution, whichever transport asked for the write.

Not offered (never reachable): the clock, the registers of the deny list of the guard and every
register without a reviewed final contract.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from deye_monitor.help_texts import write_help

# Battery charge/discharge current envelope per model (SPEC 10.2). Two independent limits:
# * APP POLICY (owner decision): 5K 120 A, 6K 130 A, 8K 185 A, 10K 210 A, 12K 240 A. It is the
#   whole envelope of R125 and R128 (application policy, not a firmware clamp).
# * RETAINED FIRMWARE CEILING of the direct clamps R108/R109 (MCU2 1175, same initializer branch as
#   the rated power): 5K 125/125, 6K 130/130, 8K 185/185, 10K 220/220, 12K 240/240 A (charge/
#   discharge); the 10/12 kW charge ceiling is 200 A on the hardware branch (R28 != 5 and hardware
#   word F7B5 == 1). R108/R109 accept the INTERSECTION of both; a 10/12 kW unit whose branch is not
#   proven by fresh R14/R28 gets the conservative 200 A charge ceiling.
# The model comes from the rated power (R20-R21); an unknown model locks the operation.
CURRENT_CAP_BY_RATED_W: Final[dict[int, int]] = {
    5000: 120,
    6000: 130,
    8000: 185,
    10000: 210,
    12000: 240,
}
FIRMWARE_CHARGE_CAP_BY_RATED_W: Final[dict[int, int]] = {
    5000: 125,
    6000: 130,
    8000: 185,
    10000: 220,
    12000: 240,
}
FIRMWARE_DISCHARGE_CAP_BY_RATED_W: Final[dict[int, int]] = {
    5000: 125,
    6000: 130,
    8000: 185,
    10000: 220,
    12000: 240,
}
HARDWARE_BRANCH_CHARGE_CAP: Final = 200  # 10/12 kW, R28 != 5 and F7B5 == 1
BRANCH_MODELS: Final = frozenset({10000, 12000})
R558: Final = 558  # fault word 3; bit 14 reports an arc fault (AFCI)
AFCI_FAULT_BIT: Final = 0x4000
R14, R28 = 14, 28  # hardware/profile word, generation selector
R28_ORDINARY: Final = 5
CURRENT_CAP_SOURCE: Final = (
    "app policy 5K 120 A, 6K 130 A, 8K 185 A, 10K 210 A, 12K 240 A, intersected for R108/R109 "
    "with the retained firmware ceilings (MCU2 1175: 125/130/185/220/240 A, 200 A charge on the "
    "10/12 kW hardware branch)"
)

OBSERVE_DELAY_S: Final = 1.0
LATE_OBSERVE_DELAY_S: Final = 30.0  # second, later peer observation (owner field: ~18 s)
PACING_S: Final = 5.0
PERMIT_S: Final = 30.0

# R111 battery control mode (SOC thresholds are meaningful in mode 1, voltage thresholds in mode 0)
R111_VOLTAGE: Final = 0
R111_SOC: Final = 1
SENTINEL: Final = 0xFFFF
# R98 battery type words that run the lead / internal charge curve (positively admitted; any
# other encoding, also an unknown one, locks the curve settings) and the contracted R102 domain
CURVE_BATTERY_TYPES: Final = frozenset({0})
VALID_CAPACITY: Final = frozenset(range(1, 2001))
TOU_CONTEXT: Final = (62, 63, 64, 111, *range(148, 154))  # clock, battery basis, slot starts
R146_SPAIN_BIT: Final = 0x0100  # ToU "Spain mode": a different firmware path (own switch)
SPAIN_MASK: Final = R146_SPAIN_BIT
TOU_SLOTS: Final = 6
WEEKDAYS: Final = (
    ("mon", "Montag", "Monday", 1),
    ("tue", "Dienstag", "Tuesday", 2),
    ("wed", "Mittwoch", "Wednesday", 3),
    ("thu", "Donnerstag", "Thursday", 4),
    ("fri", "Freitag", "Friday", 5),
    ("sat", "Samstag", "Saturday", 6),
    ("sun", "Sonntag", "Sunday", 7),
)

_DECIMAL_RE = re.compile(r"^-?[0-9]{1,6}(\.[0-9]{1,6})?$")
_TIME_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?")


class Kind(StrEnum):
    SCALAR = "scalar"
    BIT = "bit"
    TIME = "time"  # raw HHMM (hour x 100 + minute), minutes in steps of 5
    SELECT = "select"  # a closed set of named field values (``options``)
    BUTTON = "button"  # a one-shot command: the only target is ``max_raw`` (R181 clear = 2)


class Refused(Exception):
    """An operation or value is not acceptable; ``key`` is the text key the UI translates."""

    def __init__(self, key: str, status: int = 422, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.status = status
        self.params = params


@dataclass(frozen=True, slots=True)
class Text:
    de: str
    en: str

    def as_dict(self) -> dict[str, str]:
        return {"de": self.de, "en": self.en}

    def __add__(self, other: Text) -> Text:
        return Text(self.de + other.de, self.en + other.en)


@dataclass(frozen=True, slots=True)
class Gate:
    """A fresh-word condition: the field ``mask`` of ``register`` must be one of ``allowed``,
    otherwise the operation (or option) is locked with ``reason``."""

    register: int
    mask: int
    allowed: frozenset[int] | None
    reason: str
    blocked: frozenset[int] = frozenset()  # field values that lock it even when ``allowed`` is None

    def open(self, words: Mapping[int, int]) -> bool:
        word = words.get(self.register)
        if word is None:
            return False
        shift = (self.mask & -self.mask).bit_length() - 1
        field = (word & self.mask) >> shift
        return field not in self.blocked and (self.allowed is None or field in self.allowed)


@dataclass(frozen=True, slots=True)
class FeatureGate:
    """The installation description (a feature of ``Features``) marks the setting as not
    applicable when it holds one of ``blocked``. The setting stays visible and is locked with
    ``reason`` (show, never hide)."""

    feature: str
    blocked: frozenset[bool | str]
    reason: str


@dataclass(frozen=True, slots=True)
class Bound:
    """A dynamic limit from a fresh companion setting: this field must be ``<=`` (upper) or ``>=``
    (lower) the companion's field plus ``offset`` (raw units of both; companions share the unit)."""

    key: str
    offset: int = 0


@dataclass(frozen=True, slots=True)
class Branch:
    """Raw limits that depend on a fresh selector word (R131 by the nominal frequency R183):
    ``ranges`` is ``(selector word, min_raw, max_raw)``; any other selector locks the setting."""

    register: int
    ranges: tuple[tuple[int, int, int], ...]
    reason: str

    def limits(self, words: Mapping[int, int]) -> tuple[int, int] | None:
        word = words.get(self.register)
        for selector, low, high in self.ranges:
            if word == selector:
                return low, high
        return None


@dataclass(frozen=True, slots=True)
class ModeOrder:
    """An ON/OFF pair whose order depends on the fresh GEN port usage R133: in Smart Load mode
    (R133 = 1) the ON member must be at least ``margin`` raw above the OFF member, with AC coupling
    (R133 = 2) the order is reversed. ``on_member`` says which member this descriptor is."""

    companion: str
    margin: int
    on_member: bool


@dataclass(frozen=True, slots=True)
class Option:
    """One named field value of a select. ``gate`` locks only this option; a non-``writable``
    option is shown as the incumbent state but can never be written."""

    field: int
    slug: str
    label: Text
    writable: bool = True
    gate: Gate | None = None
    # the installation description must name this target: (feature, required value, reason)
    feature: tuple[str, bool | str, str] | None = None
    # a contextual check of ``WriteService`` (``_option_guard_<name>``) on fresh words and the
    # description: the reason it closes this option, or ``None``
    guard: str | None = None


@dataclass(frozen=True, slots=True)
class WriteDescriptor:
    key: str
    register: int
    mask: int
    shift: int
    kind: Kind
    unit: str  # display unit of the engineering value
    scale: int  # raw field = engineering value x scale
    min_raw: int
    max_raw: int  # documented maximum; a model cap replaces it
    name: Text
    risk: Text
    source: str
    group: str = "battery"  # dialog / release group: battery, grid_charge, smart_load, tou
    current_cap: bool = False  # the model cap of R108/R109/R125/R128 replaces ``max_raw``
    power_cap: bool = False  # the rated power in W (R20/R21) replaces ``max_raw``
    needs_battery: bool = False
    needs_smart_load: bool = False  # the GEN port must already be configured as Smart Load
    requires_r111: int | None = None  # fresh R111 must equal this battery control mode
    tou: bool = False  # ToU family: fresh R146 bit 8 (Spain mode) must be clear
    whole_word_bool: bool = False  # the whole word is 0 or 1 (R130), not one bit of a mixed word
    # Order against other settings, evaluated with their fresh values: this value must stay
    # strictly below every key in ``below`` and strictly above every key in ``above``.
    below: tuple[str, ...] = ()
    above: tuple[str, ...] = ()
    order_error: str = "threshold_order"
    # The WHOLE ordered group this value belongs to (ascending, itself included). At execution every
    # member is read and validated and the resulting group must be strictly ascending.
    chain: tuple[str, ...] = ()
    firmware_clamp: str | None = None  # "charge" / "discharge": R108 / R109 direct firmware clamp
    # A switch whose field is wider than one bit (R178 bits 8-9): ``on_field`` / ``off_field`` are
    # the only two field values, every other value reads as "unknown" and is never overwritten.
    on_field: int = 1
    off_field: int = 0
    reject_sentinel: bool = False  # 0xFFFF is the firmware's "unset" word: refuse it in and out
    options: tuple[Option, ...] = ()  # Kind.SELECT: the named field values
    gates: tuple[Gate, ...] = ()  # fresh-word conditions of the whole setting
    feature_gates: tuple[FeatureGate, ...] = ()  # installation description conditions
    plain_number: bool = False  # a number without a Home Assistant device class (a ratio, not SOC)
    step_raw: int = 1  # a target must lie on ``min_raw + n * step_raw``
    chain_strict: bool = True  # the ordered group is strictly (True) or weakly ascending
    branch: Branch | None = None  # raw limits chosen by a fresh selector word
    upper: tuple[Bound, ...] = ()  # field <= companion + offset, companions read fresh
    lower: tuple[Bound, ...] = ()  # field >= companion + offset
    mode_order: ModeOrder | None = None  # pair order by the fresh GEN port usage (R133)
    # One weekday (0 = Monday ... 6 = Sunday) of the ToU allow mask in R146 bits 1-7. All seven
    # bits zero means "every day": the switch then reads ON for each day and a switch-off writes
    # the other six days explicitly. The mask never becomes empty through a write.
    weekday: int | None = None
    # A special command (R35): one FC16 with a second, fixed word; ``None`` is a plain write.
    command_word: int | None = None
    # Per-unit setting where all units should agree: the dialog shows the configured values of
    # every unit of the plant next to it (an advisory, never a write).
    equal_hint: bool = False
    # Words a setting may already hold although a target must lie in [min_raw, max_raw]: they are
    # shown as they are and never repaired (R180: 901 s or an unaligned value stays readable).
    incumbent_range: tuple[int, int] | None = None
    # Kind.BUTTON: field values of the read-back that count as "command accepted" (the firmware
    # normalizes the command word 2 to 1), and a fault word read afterwards ``(register, mask)``
    # whose state is reported separately from the acceptance.
    accepted_after: frozenset[int] = frozenset()
    # A role change whose newly active companions are validated before it is written (R133): the
    # kind of check and the registers it needs fresh. Companions are shown, never repaired.
    mode_check: str | None = None
    mode_registers: tuple[int, ...] = ()
    fault_probe: tuple[int, int] | None = None
    # Contextual guards of ``WriteService`` (named, evaluated on the fresh words and the
    # installation description at every stage: current, snapshot, preview and the final send), the
    # extra words they read, and the target-dependent check run on a change (``transition``).
    guards: tuple[str, ...] = ()
    context_registers: tuple[int, ...] = ()
    transition: str | None = None
    # Words read and reported next to the setting for context only (no validation, no write):
    # R179 beside R180, R344 beside R347.
    report_registers: tuple[int, ...] = ()
    # The per-installation limit (a ``Features`` field, watts) the maximum is intersected with; a
    # missing entry keeps the setting unavailable (``envelope_missing``).
    envelope: str | None = None
    # A ``Features`` field (SOC %) that is the lowest value accepted (a commissioned reserve).
    floor_feature: str | None = None

    @property
    def synchronized(self) -> bool:
        """Does the parallel master copy this register to the slaves (R65-R209, R339-R429)? Only
        then is the setting master / non-parallel only; any other register is a per-unit setting
        writable on the addressed unit (same identity, route, release and audit guards)."""
        return 65 <= self.register <= 209 or 339 <= self.register <= 429

    @property
    def field_mask(self) -> int:
        return self.mask >> self.shift

    @property
    def related(self) -> tuple[str, ...]:
        others = tuple(k for k in self.chain if k != self.key)
        bounds = tuple(b.key for b in self.upper + self.lower)
        pair = (self.mode_order.companion,) if self.mode_order else ()
        return (others or self.below + self.above) + pair + bounds

    @property
    def model_limited(self) -> bool:
        return self.current_cap or self.power_cap or self.envelope is not None

    @property
    def engineering_step(self) -> int | float:
        """Smallest engineering step of a target (always positive)."""
        step = self.step_raw / abs(self.scale)
        return int(step) if step == int(step) else round(step, 4)

    def field_of(self, word: int) -> int:
        if self.weekday is not None:
            days = (word & self.mask) >> self.shift
            return 1 if (days or 0x7F) >> self.weekday & 1 else 0
        return (word & self.mask) >> self.shift

    def encode(self, fresh: int, field: int) -> int:
        """Read-modify-write: ``fresh`` with only the bits of this field replaced. A whole-word
        switch (R130) is exactly 0 or 1 and refuses a fresh word that is anything else."""
        if self.weekday is not None:
            return self._encode_weekday(fresh, field)
        if not 0 <= field <= self.field_mask:
            raise Refused("value_out_of_range")
        if self.whole_word_bool:
            if fresh not in (0, 1):
                raise Refused("unexpected_bits", 409)
            return field
        if self.reject_sentinel and fresh == SENTINEL:
            raise Refused("unexpected_bits", 409)
        new = (fresh & (~self.mask & 0xFFFF)) | (field << self.shift)
        if self.reject_sentinel and new == SENTINEL:
            raise Refused("unexpected_bits", 409)
        if (new ^ fresh) & (~self.mask & 0xFFFF):  # defence in depth, cannot happen
            raise Refused("foreign_bits_changed")
        return new

    def _encode_weekday(self, fresh: int, field: int) -> int:
        if field not in (0, 1):
            raise Refused("value_out_of_range")
        assert self.weekday is not None
        effective = ((fresh & self.mask) >> self.shift) or 0x7F
        bit = 1 << self.weekday
        wanted = effective | bit if field else effective & ~bit
        if wanted == effective:
            return fresh  # nothing to change (an "every day" mask already reads ON)
        if wanted == 0:
            raise Refused("weekday_none", 422)
        return (fresh & (~self.mask & 0xFFFF)) | (wanted << self.shift)

    @property
    def paired(self) -> bool:
        """A switch over a multi-bit field (``on_field`` / ``off_field`` differ from 1 / 0)."""
        return self.kind is Kind.BIT and (self.on_field, self.off_field) != (1, 0)

    def display(self, field: int) -> int | float | str | None:
        """Engineering value of a raw field (volts as a float with two decimals, a time as
        ``HH:MM``, a switch 1 / 0 or ``None`` for a field value that is neither state)."""
        if self.kind is Kind.TIME:
            return f"{field // 100:02d}:{field % 100:02d}"
        if self.kind is Kind.SELECT:
            option = self.option_of(field)
            return None if option is None else option.slug
        if self.kind is Kind.BUTTON:
            return None
        if self.paired:
            return 1 if field == self.on_field else 0 if field == self.off_field else None
        if self.scale == 1:
            return field
        return round(field / self.scale, 2) + 0.0  # + 0.0: never "-0.0"

    def option_of(self, field: int) -> Option | None:
        return next((o for o in self.options if o.field == field), None)

    def option_by_slug(self, slug: str) -> Option | None:
        return next((o for o in self.options if o.slug == slug), None)

    def incumbent_valid(self, field: int, cap: int | None = None) -> bool:
        """Is the stored ``field`` a legal word of this setting? Separate from the target rules: a
        time is valid as any HHMM 00:00-23:59 (minutes < 60) although new times are 5-minute steps
        up to 23:55; a scalar must lie in the range accepted now."""
        if self.kind is Kind.TIME:
            return 0 <= field <= 2359 and field % 100 < 60
        if self.kind is Kind.SELECT:
            return self.option_of(field) is not None
        if self.kind is Kind.BUTTON:
            return field in (0, 1, 2)
        if self.paired:
            return field in (self.on_field, self.off_field)
        if self.incumbent_range is not None:
            return self.incumbent_range[0] <= field <= self.incumbent_range[1]
        low, high = self.limits(cap)
        return low <= field <= high

    def limits(self, cap: int | None) -> tuple[int, int]:
        """Raw range accepted now. A model-limited operation needs a known model, and the cap (A
        for a current, W for a power) REPLACES the generic maximum of the document (``max_raw``
        is only that document value)."""
        if self.model_limited:
            if cap is None:
                raise Refused("model_unknown", 409)
            return self.min_raw, cap
        return self.min_raw, self.max_raw

    def view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "register": self.register,
            "mask": self.mask,
            "kind": self.kind.value,
            "unit": self.unit,
            "min": self.display(self.max_raw if self.scale < 0 else self.min_raw),
            "max": self.display(self.min_raw if self.scale < 0 else self.max_raw),
            "step": 5 if self.kind is Kind.TIME else self.engineering_step,
            "name": self.name.as_dict(),
            "risk": self.risk.as_dict(),
            "help": write_help(self.key),
            "source": self.source,
            "group": self.group,
            "current_cap": self.current_cap,
            "power_cap": self.power_cap,
            "needs_smart_load": self.needs_smart_load,
            "requires_r111": self.requires_r111,
            "tou": self.tou,
            "synchronized": self.synchronized,
            "whole_word_bool": self.whole_word_bool,
            "paired": self.paired,
            "options": [
                {
                    "field": o.field,
                    "slug": o.slug,
                    "label": o.label.as_dict(),
                    "writable": o.writable,
                    "gate_reason": None if o.gate is None else o.gate.reason,
                    "feature_reason": None if o.feature is None else o.feature[2],
                }
                for o in self.options
            ],
            "below": list(self.below),
            "above": list(self.above),
            "chain": list(self.chain),
            "command_word": self.command_word,
            "mode_registers": list(self.mode_registers),
            "report_registers": list(self.report_registers),
            "envelope": self.envelope,
            "equal_hint": self.equal_hint,
            "fault_probe": None if self.fault_probe is None else list(self.fault_probe),
            "incumbent_range": None if self.incumbent_range is None else list(self.incumbent_range),
            "mode_order": None
            if self.mode_order is None
            else {
                "companion": self.mode_order.companion,
                "margin": self.mode_order.margin,
                "on_member": self.mode_order.on_member,
            },
        }


_V = "V105.8-20251022 registers.json (R/W)"
_E = "write layer v2 (Codex design review, settings catalogue)"
_SOC_RANGE = (0, 100)
_VOLT_RANGE = (4100, 6000)  # contracts-1 R134/R136: LCD editor range, 0.1 V steps

_CURRENT_RISK = Text(
    "Begrenzt den Batteriestrom. 0 kann das Laden vollständig stoppen; die Vorgaben von "
    "BMS und Betriebszustand gelten weiterhin.",
    "Limits the battery current. 0 can stop charging completely; the limits of the BMS and the "
    "operating state still apply.",
)
_SOC_MODE = Text(
    " Nur im SOC-Modus der Batterie (R111 = 1).",
    " Only while the battery control mode is SOC (R111 = 1).",
)


def _soc_risk(de: str, en: str) -> Text:
    return Text(de + _SOC_MODE.de, en + _SOC_MODE.en)


def _smart(
    key: str,
    register: int,
    *,
    voltage: bool,
    off: bool,
    companion: str,
    name: Text,
    risk: Text,
) -> WriteDescriptor:
    low, high = _VOLT_RANGE if voltage else _SOC_RANGE
    return WriteDescriptor(
        key=key,
        register=register,
        mask=0xFFFF,
        shift=0,
        kind=Kind.SCALAR,
        unit="V" if voltage else "%",
        scale=100 if voltage else 1,
        min_raw=low,
        max_raw=high,
        step_raw=10 if voltage else 1,
        name=name,
        risk=risk,
        source=f"{_V}; contracts-1 (R{register})",
        group="smart_load",
        needs_battery=True,
        requires_r111=R111_VOLTAGE if voltage else R111_SOC,
        gates=(Gate(133, 0xFFFF, frozenset({1, 2}), "gen_not_smart_load"),),
        mode_order=ModeOrder(companion, 50 if voltage else 5, on_member=not off),
    )


def _soc(
    key: str,
    register: int,
    *,
    name: Text,
    risk: Text,
    low: int = 0,
    high: int = 100,
    group: str = "battery",
    tou: bool = False,
    below: tuple[str, ...] = (),
    above: tuple[str, ...] = (),
) -> WriteDescriptor:
    return WriteDescriptor(
        key=key,
        register=register,
        mask=0xFFFF,
        shift=0,
        kind=Kind.SCALAR,
        unit="%",
        scale=1,
        min_raw=low,
        max_raw=high,
        name=name,
        risk=risk,
        source=_V if not tou else f"{_V}; {_E}",
        group=group,
        needs_battery=True,
        requires_r111=R111_SOC,
        tou=tou,
        below=below,
        above=above,
    )


_PAIR_TAIL = Text(
    " Mit Smart Load (R133 = 1) muss ON mindestens 0,5 V (SOC: 5 Punkte) über OFF liegen; mit "
    "AC-Kopplung (R133 = 2) ist die Reihenfolge umgekehrt (OFF mindestens 0,5 V bzw. 5 Punkte "
    "über ON). Die App ändert das andere Mitglied nie von selbst.",
    " With Smart Load (R133 = 1) ON must be at least 0.5 V (SOC: 5 points) above OFF; with AC "
    "coupling (R133 = 2) the order is reversed (OFF at least 0.5 V or 5 points above ON). The "
    "app never changes the other member by itself.",
)
_OFF_RISK = (
    Text(
        "Nur mit GEN-Anschluss als Smart Load oder AC-Kopplung und dem passenden Modus (R111: "
        "Spannung = 0, SOC = 1). Kann diese Last bzw. Quelle abschalten.",
        "Only with the GEN port configured as Smart Load or AC coupling and the matching mode "
        "(R111: voltage = 0, SOC = 1). May disconnect that load or source.",
    )
    + _PAIR_TAIL
)
_ON_RISK = (
    Text(
        "Nur mit GEN-Anschluss als Smart Load oder AC-Kopplung und dem passenden Modus (R111: "
        "Spannung = 0, SOC = 1). Kann diese Last bzw. Quelle einschalten.",
        "Only with the GEN port configured as Smart Load or AC coupling and the matching mode "
        "(R111: voltage = 0, SOC = 1). May energize that load or source.",
    )
    + _PAIR_TAIL
)
_PROTECTION_ORDER = Text(
    " Es muss gelten: Abschalt-SOC < Niedrig-SOC < Neustart-SOC (Regel dieser App); die "
    "Grenzen legt die Inbetriebnahme der Batterie fest, die App gibt keinen sicheren "
    "Mindestwert vor.",
    " Must hold: shutdown SOC < low SOC < restart SOC (a rule of this app); the limits are "
    "set by the battery commissioning, the app does not claim a safe minimum.",
)


def _slot_descriptors(n: int) -> tuple[WriteDescriptor, ...]:
    prev_start = f"tou_slot{n - 1}_start" if n > 1 else None
    next_start = f"tou_slot{n + 1}_start" if n < TOU_SLOTS else None
    start = 147 + n
    power = 153 + n
    soc = 165 + n
    flags = 171 + n
    return (
        WriteDescriptor(
            key=f"tou_slot{n}_start",
            register=start,
            mask=0xFFFF,
            shift=0,
            kind=Kind.TIME,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=2355,
            name=Text(
                f"Time of Use Slot {n}: Startzeit (R{start})",
                f"Time of Use slot {n}: start time (R{start})",
            ),
            risk=Text(
                "Beginn dieses Zeitabschnitts (HH:MM, Minuten in 5er-Schritten). Die Startzeiten "
                "müssen von Slot 1 bis 6 streng aufsteigen; Slot 6 gilt bis zum Start von Slot 1 "
                "(über Mitternacht). Eine Änderung wirkt sofort auf die angrenzenden Abschnitte; "
                "andere Slots werden nicht umsortiert.",
                "Start of this interval (HH:MM, minutes in steps of 5). The start times must "
                "ascend strictly from slot 1 to 6; slot 6 runs until slot 1 starts (across "
                "midnight). A change affects the adjacent intervals at once; other slots are "
                "never rearranged.",
            ),
            source=f"{_V}; {_E}",
            group="tou",
            needs_battery=True,
            tou=True,
            below=(next_start,) if next_start else (),
            above=(prev_start,) if prev_start else (),
            order_error="tou_order",
        ),
        WriteDescriptor(
            key=f"tou_slot{n}_power",
            register=power,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="W",
            scale=1,
            min_raw=0,
            max_raw=12000,
            name=Text(
                f"Time of Use Slot {n}: Entladeleistung (R{power})",
                f"Time of Use slot {n}: discharge power (R{power})",
            ),
            risk=Text(
                "Leistungsvorgabe des Zeitabschnitts in W, 0 bis zur Nennleistung des Modells "
                "(R20/R21). Keine Garantie für die tatsächlich gelieferte Leistung.",
                "Power setting of the interval in W, 0 up to the model's rated power (R20/R21). "
                "Not a guarantee of the power actually delivered.",
            ),
            source=f"{_V}; {_E}",
            group="tou",
            power_cap=True,
            needs_battery=True,
            tou=True,
        ),
        _soc(
            f"tou_slot{n}_soc",
            soc,
            name=Text(
                f"Time of Use Slot {n}: Ziel-SOC (R{soc})",
                f"Time of Use slot {n}: target SOC (R{soc})",
            ),
            risk=_soc_risk(
                "SOC-Ziel des Zeitabschnitts (0 bis 100 %). Ein Wert nahe dem Abschalt-SOC kann "
                "die Reserve aufbrauchen; die App prüft nur den Bereich.",
                "SOC target of the interval (0 to 100 %). A value close to the shutdown SOC can "
                "use up the reserve; the app only checks the range.",
            ),
            group="tou",
            tou=True,
        ),
        WriteDescriptor(
            key=f"tou_slot{n}_voltage",
            register=159 + n,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="V",
            scale=100,
            min_raw=4100,
            max_raw=5880,
            step_raw=10,
            name=Text(
                f"Time of Use Slot {n}: Ziel-Spannung (R{159 + n})",
                f"Time of Use slot {n}: target voltage (R{159 + n})",
            ),
            risk=Text(
                "Batteriespannungsziel des Zeitabschnitts (Spannungsmodus, R111 = 0), 0,1-V-"
                "Schritte, mindestens die Spannung R120 und höchstens die Float-Spannung "
                "R101 (beide frisch gelesen; die App ändert keinen der beiden). Ein Wert nahe "
                "der unteren Grenze kann die Reserve aufbrauchen; die App erfindet keinen "
                "allgemeinen Mindestabstand.",
                "Battery voltage target of the interval (voltage mode, R111 = 0), 0.1 V steps, "
                "at least the voltage R120 and at most the float voltage R101 (both read fresh; "
                "the app changes neither). A value close to the lower limit can use up the "
                "reserve; the app invents no general minimum gap.",
            ),
            source=f"{_V}; contracts-1 (R{159 + n})",
            group="tou",
            needs_battery=True,
            tou=True,
            requires_r111=R111_VOLTAGE,
            lower=(Bound("battery_low_voltage"),),
            upper=(Bound("battery_float_voltage"),),
        ),
        WriteDescriptor(
            key=f"tou_slot{n}_gen_charge",
            register=flags,
            mask=0x0002,
            shift=1,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text(
                f"Time of Use Slot {n}: Generatorladen erlauben (R{flags} Bit 1)",
                f"Time of Use slot {n}: allow generator charging (R{flags} bit 1)",
            ),
            risk=Text(
                "Erlaubt in diesem Zeitabschnitt das Laden aus dem Generator (kein erzwungenes "
                "Laden, kein Generatorstart). Nur mit einem in der Anlagenbeschreibung am "
                "GEN-Anschluss beschriebenen Generator und frisch gelesenem R133 = 0; ein "
                "Generator am GRID-Anschluss nutzt das Netzlade-Bit. Alle anderen Bits des "
                "Wortes bleiben unverändert.",
                "Allows charging from the generator in this interval (not forced charging, no "
                "generator start). Only with a generator described at the GEN port in the "
                "installation description and R133 = 0 read fresh; a generator on the GRID port "
                "uses the GRID charge bit. All other bits of the word stay unchanged.",
            ),
            source=f"{_V}; contracts-1 (R{flags})",
            group="tou",
            needs_battery=True,
            tou=True,
            feature_gates=(
                FeatureGate(
                    "gen_port_mode",
                    frozenset({"unused", "smart_load", "ac_coupled"}),
                    "gen_port_not_generator",
                ),
            ),
            # positive, source-correct: the GEN port must be a generator now (fresh R133 = 0)
            gates=(Gate(133, 0xFFFF, frozenset({0}), "gen_port_not_generator"),),
        ),
        *(
            WriteDescriptor(
                key=f"tou_slot{n}_spain_{tag}",
                register=flags,
                mask=1 << bit,
                shift=bit,
                kind=Kind.BIT,
                unit="",
                scale=1,
                min_raw=0,
                max_raw=1,
                name=Text(
                    f"Time of Use Slot {n}: Spanien-Modus {de} (R{flags} Bit {bit})",
                    f"Time of Use slot {n}: Spain mode {en} (R{flags} bit {bit})",
                ),
                risk=Text(
                    f"Nur im Spanien-Modus (R146 Bit 8): Untermodus „{de}“ dieses Zeitabschnitts. "
                    "Diese Bits sind keine unabhängigen Schalter; setzt man mehrere, gilt "
                    "Bit 4 vor Bit 3 vor Bit 2 (so prüft es die Firmware, ein Ein-aus-Zwang "
                    "besteht nicht). Die App löscht kein anderes Bit von selbst. Alle anderen "
                    "Bits des Wortes (auch das unbekannte Bit 5) bleiben unverändert.",
                    f"Only in Spain mode (R146 bit 8): sub-mode “{en}” of this interval. These "
                    "bits are not independent switches; if several are set, bit 4 wins over "
                    "bit 3 over bit 2 (the firmware checks them in that order, there is no "
                    "one-hot rule). The app never clears another bit by itself. All other bits "
                    "of the word (including the unknown bit 5) stay unchanged.",
                ),
                source=f"{_V}; contracts-1 (R{flags})",
                group="tou",
                needs_battery=True,
                gates=(Gate(146, SPAIN_MASK, frozenset({1}), "spain_mode_off"),),
            )
            for tag, bit, de, en in (
                ("gm", 2, "GM", "GM"),
                ("bu", 3, "BU", "BU"),
                ("ch", 4, "CH", "CH"),
            )
        ),
        WriteDescriptor(
            key=f"tou_slot{n}_grid_charge",
            register=flags,
            mask=0x0001,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text(
                f"Time of Use Slot {n}: Netzladen erlauben (R{flags} Bit 0)",
                f"Time of Use slot {n}: allow GRID charging (R{flags} bit 0)",
            ),
            risk=Text(
                "Erlaubt in diesem Zeitabschnitt das Laden aus dem GRID-Anschluss (kein "
                "erzwungenes Laden). Alle anderen Bits des Wortes, auch das Generator-Ladebit, "
                "bleiben unverändert.",
                "Allows charging from the GRID port in this interval (not forced charging). All "
                "other bits of the word, including the generator charge bit, stay unchanged.",
            ),
            source=f"{_V}; {_E}",
            group="tou",
            needs_battery=True,
            tou=True,
        ),
    )


_CHEMISTRY = (
    " Die zulässigen Spannungen und Zeiten legt der Batteriehersteller fest; die App kennt "
    "weder Zellzahl noch Chemie und prüft nur den Bereich der Inverter-Anzeige.",
    " The permitted voltages and times are set by the battery manufacturer; the app knows "
    "neither cell count nor chemistry and checks only the range of the inverter display.",
)
_CURVE_GATE = (
    " Nur für Blei-/interne Ladekurve: Bei Batterietyp-Wort R98 = 1 mit gültigem BMS-Zustand "
    "überspringt die Firmware diese Kurve; dann und bei CAN-BMS in der Anlagenbeschreibung "
    "ist die Einstellung gesperrt.",
    " Only for the lead / internal charge curve: with the battery type word R98 = 1 and a valid "
    "BMS state the firmware skips this curve; then, and with a CAN BMS in the installation "
    "description, the setting is locked.",
)
_STAGE_ORDER = (
    " Regel dieser App: Erhaltung ≤ Absorption ≤ Ausgleich (die Firmware erzwingt das nicht).",
    " Rule of this app: float ≤ absorption ≤ equalization (the firmware does not enforce it).",
)
_PROTECT_V = (
    " Nur im Spannungsmodus (R111 = 0). Es muss gelten: Abschaltspannung < Niedrigspannung < "
    "Neustartspannung (Regel dieser App; die Anzeige des Geräts erlaubt auch Gleichheit). Das "
    "Gerät kennt keinen universell sicheren Wert; ein zu niedriger Abschaltwert kann die "
    "Batterie tiefentladen.",
    " Only in voltage mode (R111 = 0). Must hold: shutdown voltage < low voltage < restart "
    "voltage (a rule of this app; the device display also allows equality). The device knows no "
    "universally safe value; a shutdown value that is too low can deep-discharge the battery.",
)


def _volt(
    key: str,
    register: int,
    tag: str,
    name: tuple[str, str],
    risk: tuple[str, str],
    *,
    high: int,
    group: str,
    **extra: Any,
) -> WriteDescriptor:
    return WriteDescriptor(
        key=key,
        register=register,
        mask=0xFFFF,
        shift=0,
        kind=Kind.SCALAR,
        unit="V",
        scale=100,
        min_raw=4100,
        max_raw=high,
        step_raw=10,
        name=Text(f"{name[0]} ({tag})", f"{name[1]} ({tag})"),
        risk=Text(risk[0], risk[1]),
        source=f"{_V}; contracts-1 (R{register})",
        group=group,
        needs_battery=True,
        **extra,
    )


def _curve_gates() -> dict[str, Any]:
    return {
        "gates": (Gate(98, 0xFFFF, CURVE_BATTERY_TYPES, "bms_curve_skipped"),),
        "feature_gates": (FeatureGate("bms", frozenset({True}), "bms_battery"),),
    }


def _battery_descriptors() -> tuple[WriteDescriptor, ...]:
    curve = _curve_gates()
    stage = (
        ("battery_float_voltage", 101, "Float voltage", "Float-Spannung", "R101"),
        ("battery_absorption_voltage", 100, "Absorption voltage", "Absorptionsspannung", "R100"),
        ("battery_equalization_voltage", 99, "Equalization voltage", "Ausgleichsspannung", "R99"),
    )
    out: list[WriteDescriptor] = []
    for key, register, en, de, tag in stage:
        out.append(
            _volt(
                key,
                register,
                tag,
                (f"Batterie-{de}", f"Battery {en.lower()}"),
                (
                    f"Zielspannung der Ladestufe ({de}), 41,0 bis 58,8 V in Schritten von 0,1 V. "
                    "Eine zu hohe Spannung kann die Batterie schädigen."
                    + _STAGE_ORDER[0]
                    + _CURVE_GATE[0]
                    + _CHEMISTRY[0],
                    f"Target voltage of the charge stage ({en.lower()}), 41.0 to 58.8 V in steps "
                    "of 0.1 V. A voltage that is too high can damage the battery."
                    + _STAGE_ORDER[1]
                    + _CURVE_GATE[1]
                    + _CHEMISTRY[1],
                ),
                high=5880,
                group="battery_curve",
                chain=(
                    "battery_float_voltage",
                    "battery_absorption_voltage",
                    "battery_equalization_voltage",
                ),
                chain_strict=False,
                **curve,
            )
        )
    out.append(
        WriteDescriptor(
            key="battery_capacity",
            register=102,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="Ah",
            scale=1,
            min_raw=1,
            max_raw=2000,
            name=Text("Batteriekapazität (R102)", "Battery capacity (R102)"),
            risk=Text(
                "Kapazität für die interne SOC-Schätzung, 1 bis 2000 Ah. Ein falscher Wert "
                "verfälscht die SOC-Anzeige und alle SOC-Schwellen; auch mit BMS-SOC kann die "
                "Wirkung zweigabhängig sein. Gilt für die ganze Parallelanlage.",
                "Capacity for the internal SOC estimator, 1 to 2000 Ah. A wrong value skews the "
                "SOC display and every SOC threshold; with a BMS SOC the effect may depend on "
                "the branch. Applies to the whole parallel plant.",
            ),
            source=f"{_V}; contracts-1 (R102)",
            group="battery_setup",
            needs_battery=True,
        )
    )
    out.append(
        _volt(
            "battery_empty_voltage",
            103,
            "R103",
            ("Batterie-Leerspannung", "Battery empty voltage"),
            (
                "Spannung, die die interne Spannungs-/SOC-Kennlinie als leer ansieht, 41,0 bis "
                "52,0 V in Schritten von 0,1 V (Schrittweite ist eine App-Regel). Die Firmware "
                "setzt Werte außerhalb 41 to 52 V beim Start auf 45,0 V zurück." + _CHEMISTRY[0],
                "Voltage the internal voltage / SOC curve treats as empty, 41.0 to 52.0 V in "
                "steps of 0.1 V (the step is a rule of this app). At start the firmware resets "
                "values outside 41 to 52 V to 45.0 V." + _CHEMISTRY[1],
            ),
            high=5200,
            group="battery_setup",
        )
    )
    out.append(
        WriteDescriptor(
            key="battery_equalization_interval",
            register=105,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="d",
            scale=1,
            min_raw=0,
            max_raw=90,
            name=Text("Ausgleichsladung: Abstand (R105)", "Equalization charge interval (R105)"),
            risk=Text(
                "Abstand der Ausgleichsladung in Tagen (0 bis 90); 0 verhindert sie. Nur für "
                "dafür geeignete Bleibatterien." + _CURVE_GATE[0] + _CHEMISTRY[0],
                "Interval of the equalization charge in days (0 to 90); 0 inhibits it. Only for "
                "suitable lead batteries." + _CURVE_GATE[1] + _CHEMISTRY[1],
            ),
            source=f"{_V}; contracts-1 (R105)",
            group="battery_curve",
            needs_battery=True,
            **curve,
        )
    )
    out.append(
        WriteDescriptor(
            key="battery_equalization_duration",
            register=106,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="h",
            scale=2,
            min_raw=0,
            max_raw=20,
            name=Text("Ausgleichsladung: Dauer (R106)", "Equalization charge duration (R106)"),
            risk=Text(
                "Dauer der Ausgleichsladung in Stunden, 0 bis 10 h in Schritten von 0,5 h "
                "(das Register hält die doppelte Zahl); 0 verhindert sie. Angezeigter Wert des "
                "Geräts, keine gemessene Dauer." + _CURVE_GATE[0] + _CHEMISTRY[0],
                "Duration of the equalization charge in hours, 0 to 10 h in steps of 0.5 h (the "
                "register holds twice the number); 0 inhibits it. The value the device shows, "
                "not a measured duration." + _CURVE_GATE[1] + _CHEMISTRY[1],
            ),
            source=f"{_V}; contracts-1 (R106)",
            group="battery_curve",
            needs_battery=True,
            **curve,
        )
    )
    out.append(
        WriteDescriptor(
            key="battery_temperature_compensation",
            register=107,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="mV/°C/cell",
            scale=-1,
            min_raw=0,
            max_raw=20,
            name=Text(
                "Batterie-Temperaturkompensation (R107)", "Battery temperature compensation (R107)"
            ),
            risk=Text(
                "Spannungskorrektur je °C und Zelle, -20 bis 0 mV (das Register hält den "
                "Betrag; die App zeigt und nimmt den negativen Koeffizienten wie das Gerät). "
                "Die Firmware begrenzt die Korrektur auf ±2,00 V. Die Zellzahl ist der App "
                "unbekannt." + _CURVE_GATE[0] + _CHEMISTRY[0],
                "Voltage correction per °C and cell, -20 to 0 mV (the register holds the "
                "magnitude; the app shows and takes the negative coefficient like the device). "
                "The firmware limits the correction to ±2.00 V. The cell count is unknown to "
                "the app." + _CURVE_GATE[1] + _CHEMISTRY[1],
            ),
            source=f"{_V}; contracts-1 (R107)",
            group="battery_curve",
            needs_battery=True,
            **curve,
        )
    )
    out.append(
        WriteDescriptor(
            key="battery_activation",
            register=112,
            mask=0x0001,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("Batterie-Aktivierung (R112)", "Battery activation (R112)"),
            risk=Text(
                "EIN erlaubt dem Gerät, eine schlafende oder getrennte Batterie über seine "
                "Aktivierungs- und Weckfunktion zu versorgen. Das ganze Register wird als 0 "
                "oder 1 geschrieben; steht ein anderer Wert darin, wird nicht geschrieben. Das "
                "Wecken hängt von Spannung und Betriebszustand ab und ist nicht sofort.",
                "ON lets the device try to wake a sleeping or disconnected battery through its "
                "activation function. The whole register is written as 0 or 1; if it holds "
                "anything else nothing is written. Waking depends on voltage and operating "
                "state and is not immediate.",
            ),
            source=f"{_V}; contracts-1 (R112)",
            group="battery_setup",
            needs_battery=True,
            whole_word_bool=True,
        )
    )
    out.append(
        WriteDescriptor(
            key="battery_internal_resistance",
            register=113,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="mΩ",
            scale=1,
            min_raw=1,
            max_raw=200,
            name=Text("Batterie-Innenwiderstand (R113)", "Battery internal resistance (R113)"),
            risk=Text(
                "Innenwiderstand für die Spannungskompensation, 1 bis 200 mΩ (Bereich der "
                "Geräteanzeige). Ein falscher Wert verschiebt die Lade- und Entladespannungen; "
                "es wurde nichts gemessen." + _CHEMISTRY[0],
                "Internal resistance for the voltage compensation, 1 to 200 mΩ (the range of "
                "the device display). A wrong value shifts the charge and discharge voltages; "
                "nothing was measured." + _CHEMISTRY[1],
            ),
            source=f"{_V}; contracts-1 (R113)",
            group="battery_setup",
            needs_battery=True,
        )
    )
    out.append(
        WriteDescriptor(
            key="battery_charge_efficiency",
            register=114,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="%",
            scale=10,
            min_raw=1,
            max_raw=1000,
            name=Text("Lade-Wirkungsgrad (R114)", "Battery charge efficiency (R114)"),
            risk=Text(
                "Wirkungsgradfaktor der internen SOC-Schätzung, 0,1 bis 100,0 % in Schritten "
                "von 0,1 % (App-Bereich, kein Firmware-Limit). Ein falscher Wert verfälscht die "
                "SOC-Schätzung. Gesperrt, solange die Kapazität (R102) nicht positiv ist.",
                "Efficiency factor of the internal SOC estimator, 0.1 to 100.0 % in steps of "
                "0.1 % (an app range, not a firmware limit). A wrong value skews the SOC "
                "estimate. Locked while the capacity (R102) is not positive.",
            ),
            source=f"{_V}; contracts-1 (R114)",
            group="battery_setup",
            needs_battery=True,
            plain_number=True,
            gates=(Gate(102, 0xFFFF, VALID_CAPACITY, "capacity_not_positive"),),
        )
    )
    protect = (
        ("battery_shutdown_voltage", 118, "shutdown", "Abschaltspannung", "Shutdown voltage"),
        ("battery_low_voltage", 120, "low", "Niedrigspannung", "Low voltage"),
        ("battery_restart_voltage", 119, "restart", "Neustartspannung", "Restart voltage"),
    )
    for key, register, _, de, en in protect:
        out.append(
            _volt(
                key,
                register,
                f"R{register}",
                (f"Batterie-{de}", f"Battery {en.lower()}"),
                (
                    f"{de} der Batterie, 41,0 bis 60,0 V in Schritten von 0,1 V." + _PROTECT_V[0],
                    f"{en} of the battery, 41.0 to 60.0 V in steps of 0.1 V." + _PROTECT_V[1],
                ),
                high=6000,
                group="battery",
                requires_r111=R111_VOLTAGE,
                chain=(
                    "battery_shutdown_voltage",
                    "battery_low_voltage",
                    "battery_restart_voltage",
                ),
            )
        )
    out.append(
        _volt(
            "grid_charge_start_voltage",
            126,
            "R126",
            ("Netzladen ab Spannung", "GRID charge start voltage"),
            (
                "Unter dieser Batteriespannung beginnt das Laden aus dem Netz (Spannungsmodus, "
                "R111 = 0), mindestens 41,0 V und höchstens die Float-Spannung (R101) "
                "minus 2,0 V, nie über 58,0 V. Die App ändert die Float-Spannung nicht, um "
                "einen Wert zuzulassen. Ob geladen wird, hängt zusätzlich von R130 und dem "
                "Betriebszustand ab.",
                "Below this battery voltage charging from the grid starts (voltage mode, R111 = "
                "0), at least 41.0 V and at most the float voltage (R101) minus 2.0 V, never "
                "above 58.0 V. The app never changes the float voltage to admit a value. "
                "Whether it charges also depends on R130 and the operating state.",
            ),
            high=5800,
            group="grid_charge",
            requires_r111=R111_VOLTAGE,
            upper=(Bound("battery_float_voltage", -200),),
            feature_gates=(
                FeatureGate("grid_port_use", frozenset({"unused"}), "grid_port_unused"),
            ),
        )
    )
    return tuple(out)


_GEN_ONLY = Gate(133, 0xFFFF, frozenset({0}), "gen_port_not_generator")
_GEN_COMMON = (
    " Nur mit als Generator konfiguriertem GEN-Anschluss (R133 = 0). Gilt für die ganze "
    "Parallelanlage (vom Master kopiert).",
    " Only with the GEN port configured as a generator (R133 = 0). Applies to the whole "
    "parallel plant (copied from the master).",
)
_LCD_REPAIR = (
    " Der LCD-Dialog setzt beim Wechsel zusätzlich R129 zurück und kann Schwellen "
    "korrigieren; diese Folgeänderungen führt ein Schreiben über Modbus (FC16) nicht aus. "
    "Danach alle Werte der GEN-Gruppe prüfen.",
    " The LCD dialog additionally clears R129 on a change and may repair thresholds; those "
    "follow-up changes are not executed by a Modbus write (FC16). Check all values of the GEN "
    "group afterwards.",
)


def _gen_hours(key: str, register: int, en: str, de: str, risk: Text) -> WriteDescriptor:
    return WriteDescriptor(
        key=key,
        register=register,
        mask=0xFFFF,
        shift=0,
        kind=Kind.SCALAR,
        unit="h",
        scale=10,
        min_raw=0,
        max_raw=240,
        step_raw=5,
        name=Text(f"{de} (R{register})", f"{en} (R{register})"),
        risk=risk,
        source=f"{_V}; contracts-1 (R{register})",
        group="gen",
        gates=(_GEN_ONLY,),
    )


def _gen_descriptors() -> tuple[WriteDescriptor, ...]:
    hours_risk = (
        "0 bis 24 h in Schritten von 0,5 h (das Register hält das Zehnfache, in Fünferschritten). "
        "Null ist ein tatsächlich eingestellter Endpunkt, kein Nichtstun. Zu kurze Zeiten können "
        "wiederholte Starts auslösen oder das Laden beenden, zu lange einen Generator "
        "überfahren; die Firmware zählt in Sechser-Einheiten bis 1440, eine Uhrenkalibrierung "
        "liegt nicht vor.",
        "0 to 24 h in steps of 0.5 h (the register holds ten times the number, in steps of "
        "five). Zero is a real configured endpoint, not a no-op. Too short a time can cause "
        "repeated starts or stop charging, too long can overrun a generator; the firmware "
        "counts in units of six up to 1440 and no clock calibration is available.",
    )
    out = [
        _gen_hours(
            "generator_max_run_time",
            121,
            "Generator maximum running time",
            "Generator-Höchstlaufzeit",
            Text(hours_risk[0] + _GEN_COMMON[0], hours_risk[1] + _GEN_COMMON[1]),
        ),
        _gen_hours(
            "generator_cooling_time",
            122,
            "Generator cooling time",
            "Generator-Abkühlzeit",
            Text(hours_risk[0] + _GEN_COMMON[0], hours_risk[1] + _GEN_COMMON[1]),
        ),
        _volt(
            "generator_charge_start_voltage",
            123,
            "R123",
            ("Generator-Ladestart-Spannung", "Generator charge start voltage"),
            (
                "Unter dieser Batteriespannung fordert der Spannungsmodus (R111 = 0) das Laden "
                "aus dem Generator an, mindestens 41,0 V und höchstens die Float-Spannung "
                "(R101) minus 2,0 V. Die App ändert die Float-Spannung nicht, um einen Wert "
                "zuzulassen. Das Laden braucht zusätzlich R129 = 1 und eine verfügbare Quelle."
                + _GEN_COMMON[0],
                "Below this battery voltage the voltage mode (R111 = 0) requests charging from "
                "the generator, at least 41.0 V and at most the float voltage (R101) minus 2.0 "
                "V. The app never changes the float voltage to admit a value. Charging also "
                "needs R129 = 1 and an available source." + _GEN_COMMON[1],
            ),
            high=5680,
            group="gen",
            requires_r111=R111_VOLTAGE,
            gates=(_GEN_ONLY,),
            upper=(Bound("battery_float_voltage", -200),),
        ),
        WriteDescriptor(
            key="generator_charge_soc",
            register=124,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="%",
            scale=1,
            min_raw=10,
            max_raw=90,
            name=Text("Generator-Lade-SOC (R124)", "Generator charge SOC (R124)"),
            risk=Text(
                "SOC, unter dem das Laden aus dem Generator angefordert wird (SOC-Modus, R111 = "
                "1), 10 bis 90 %. Das ganze Register wird geschrieben; ein gepacktes Byte-Paar "
                "(Start/Stopp) wird nicht erzeugt, ein solcher vorhandener Wert ist gesperrt. "
                "Das Laden braucht zusätzlich R129 = 1." + _GEN_COMMON[0],
                "SOC below which charging from the generator is requested (SOC mode, R111 = 1), "
                "10 to 90 %. The whole register is written; a packed byte pair (start / stop) is "
                "never produced and such an incumbent is locked. Charging also needs R129 = 1."
                + _GEN_COMMON[1],
            ),
            source=f"{_V}; contracts-1 (R124)",
            group="gen",
            needs_battery=True,
            requires_r111=R111_SOC,
            gates=(_GEN_ONLY,),
        ),
        WriteDescriptor(
            key="generator_charge_enable",
            register=129,
            mask=0x0001,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("Generator-Laden erlauben (R129)", "Allow generator charging (R129)"),
            risk=Text(
                "EIN erlaubt das Laden aus dem Generator und das externe Startverhalten, sofern "
                "die übrigen Bedingungen erfüllt sind; es ist eine Erlaubnis, keine Zusage, dass "
                "der Generator läuft. Das ganze Register wird als 0 oder 1 geschrieben; steht "
                "ein anderer Wert darin, wird nicht geschrieben. Kontakt und Polarität sind aus "
                "dem Wort nicht ableitbar." + _GEN_COMMON[0],
                "ON permits charging from the generator and the external start behaviour when "
                "the other conditions hold; it is a permission, not a promise that the "
                "generator runs. The whole register is written as 0 or 1; if it holds anything "
                "else nothing is written. Contact and polarity cannot be derived from the "
                "word." + _GEN_COMMON[1],
            ),
            source=f"{_V}; contracts-1 (R129)",
            group="gen",
            needs_battery=True,
            whole_word_bool=True,
            gates=(_GEN_ONLY,),
        ),
        WriteDescriptor(
            key="ac_coupling_upper_frequency",
            register=131,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="Hz",
            scale=100,
            min_raw=5050,
            max_raw=6500,
            name=Text(
                "AC-Kopplung: obere Frequenzgrenze (R131)",
                "AC coupling upper frequency limit (R131)",
            ),
            risk=Text(
                "Frequenzgrenze der frequenzgesteuerten Abregelung am AC-gekoppelten GEN-"
                "Anschluss (R133 = 2): bei 50 Hz Netz 50,50 bis 55,00 Hz, bei 60 Hz Netz 60,50 "
                "bis 65,00 Hz in Schritten von 0,01 Hz (nach R183); ohne bekannte Nennfrequenz "
                "gesperrt. Inkompatible Mikrowechselrichter oder Lasten können abschalten oder "
                "außerhalb ihres Frequenzbereichs laufen. Gilt für die ganze Parallelanlage.",
                "Frequency limit of the frequency-based curtailment at the AC-coupled GEN port "
                "(R133 = 2): 50.50 to 55.00 Hz on a 50 Hz grid, 60.50 to 65.00 Hz on a 60 Hz "
                "grid, in steps of 0.01 Hz (by R183); locked without a known nominal frequency. "
                "Incompatible microinverters or loads can disconnect or run outside their "
                "frequency range. Applies to the whole parallel plant.",
            ),
            source=f"{_V}; contracts-1 (R131)",
            group="gen",
            gates=(Gate(133, 0xFFFF, frozenset({2}), "gen_port_not_ac_coupled"),),
            branch=Branch(
                183,
                ((0, 5050, 5500), (1, 6050, 6500)),
                "nominal_frequency_unknown",
            ),
        ),
        WriteDescriptor(
            key="smart_load_force",
            register=132,
            mask=0x0001,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("Smart Load erzwingen (R132)", "Force Smart Load (R132)"),
            risk=Text(
                "EIN erzwingt die Smart-Load-Funktion sofort; die Firmware setzt den Wert beim "
                "Start und nach einem internen Zeitgeber selbst auf 0 zurück, es gibt also "
                "keinen gehaltenen Zustand und keine zugesagte Dauer. Das ganze Register wird "
                "als 0 oder 1 geschrieben. Nur mit als Smart Load konfiguriertem GEN-Anschluss "
                "(R133 = 1) und passend angeschlossener Last.",
                "ON forces the Smart Load function at once; the firmware clears the value itself "
                "at start and after an internal timer, so there is no held state and no promised "
                "duration. The whole register is written as 0 or 1. Only with the GEN port "
                "configured as Smart Load (R133 = 1) and a matching connected load.",
            ),
            source=f"{_V}; contracts-1 (R132)",
            group="gen",
            needs_smart_load=True,
            whole_word_bool=True,
        ),
        WriteDescriptor(
            key="gen_port_usage",
            register=133,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SELECT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=2,
            name=Text("GEN-Anschluss: Verwendung (R133)", "GEN port usage (R133)"),
            risk=Text(
                "Schaltet den GEN-Anschluss zwischen Generator, Smart Load und AC-Kopplung "
                "(Eingang oder Ausgang). Falsche Verdrahtung oder ein Wechsel im Betrieb kann "
                "die Versorgung unterbrechen oder inkompatible Quellen verbinden; der MCU2 setzt "
                "dabei den Relaiszustand zurück. Nur wählbar, wenn die Anlagenbeschreibung die "
                "Zielrolle nennt, und nur als genau 0, 1 oder 2. Die LCD-Folgeaktionen (R129 "
                "zurücksetzen, Schwellen korrigieren) führt ein Schreiben über Modbus (FC16) "
                "nicht aus; vor dem Wechsel werden die Schwellen des neuen Modus (R134-R137) gegen "
                "den Batteriemodus R111 geprüft und die betroffenen Werte (R129, R132, R134-R137, "
                "R140, R178, R189) angezeigt, aber nie angepasst; danach alle Werte der GEN-Gruppe "
                "prüfen. Nicht ändern, während eine Quelle oder Last angeschlossen ist und "
                "arbeitet. Gilt für die ganze Parallelanlage.",
                "Switches the GEN port between generator, Smart Load and AC coupling (input or "
                "output). Wrong wiring or a change during operation can interrupt the supply or "
                "connect incompatible sources; the MCU2 resets the relay state on a change. "
                "Selectable only when the installation description names the target role, and "
                "only as exactly 0, 1 or 2. The LCD follow-up actions (clearing R129, repairing "
                "thresholds) are not executed by a Modbus write (FC16); before the change the "
                "thresholds of the new mode (R134-R137) are checked against the battery mode R111 "
                "and the affected values (R129, R132, R134-R137, R140, R178, R189) are shown, "
                "never adjusted; check all values of the GEN group afterwards. Do not change it "
                "while a source or load is connected and operating. Applies to the whole "
                "parallel plant.",
            ),
            source=f"{_V}; contracts-1 (R133)",
            group="gen",
            mode_check="gen_port",
            mode_registers=(111, 129, 132, 134, 135, 136, 137, 140, 178, 189),
            options=(
                Option(
                    0,
                    "generator",
                    Text("Generator", "Generator"),
                    feature=("gen_port_mode", "generator", "port_role_not_described"),
                ),
                Option(
                    1,
                    "smart_load",
                    Text("Smart Load", "Smart Load"),
                    feature=("gen_port_mode", "smart_load", "port_role_not_described"),
                ),
                Option(
                    2,
                    "ac_coupled",
                    Text("AC-Kopplung", "AC coupling"),
                    feature=("gen_port_mode", "ac_coupled", "port_role_not_described"),
                ),
            ),
        ),
    ]
    return tuple(out)


_NO_GRID = FeatureGate("utility_grid", frozenset({False}), "no_utility_grid")
_EXPORT_TAIL = Text(
    " Gilt für die ganze Parallelanlage (vom Master kopiert). Der gelesene Wert ist die "
    "konfigurierte Einstellung, kein Nachweis der tatsächlichen Einspeisung; die Firmware kann "
    "ihn (etwa bei Peak Shaving) selbst ändern und die App kämpft nicht dagegen.",
    " Applies to the whole parallel plant (copied from the master). The value read is the "
    "configured setting, not proof of the power actually exported; the firmware can change it "
    "itself (for example with peak shaving) and the app does not fight it.",
)


def _power(
    key: str,
    register: int,
    *,
    name: Text,
    risk: Text,
    low: int,
    high: int,
    step: int,
    group: str,
    **extra: Any,
) -> WriteDescriptor:
    return WriteDescriptor(
        key=key,
        register=register,
        mask=0xFFFF,
        shift=0,
        kind=Kind.SCALAR,
        unit="W",
        scale=1,
        min_raw=low,
        max_raw=high,
        step_raw=step,
        name=name,
        risk=risk,
        source=f"{_V}; contracts-1 (R{register})",
        group=group,
        power_cap=True,  # never above the rated power of the model (R20/R21)
        plain_number=True,
        **extra,
    )


def _export_descriptors() -> tuple[WriteDescriptor, ...]:
    peak = Text(
        " Wirkt nur bei aktiviertem Peak Shaving (R178-Paar); die Firmware kann dabei "
        "Energie-Modus und ToU selbst umstellen. Die Obergrenze ist die Nennleistung des Modells "
        "(R20/R21), nie mehr als 16000 W und nie mehr als der in der Anlagenbeschreibung "
        "eingetragene Wert (Generatorleistung bzw. Netzanschlussgrenze); ohne diesen Eintrag ist "
        "die Einstellung gesperrt.",
        " Only effective with peak shaving enabled (R178 pair); the firmware may then change the "
        "energy mode and ToU itself. The upper limit is the model's rated power (R20/R21), never "
        "above 16000 W and never above the value entered in the installation description "
        "(generator capacity or grid connection limit); without that entry the setting is "
        "locked.",
    )
    return (
        _power(
            "generator_peak_shaving_power",
            190,
            name=Text(
                "Generator Peak Shaving: Leistung (R190)",
                "Generator Peak Shaving power (R190)",
            ),
            risk=Text(
                "Leistungsgrenze der Quelle für das Generator-Peak-Shaving, 500 bis 16000 W "
                "in Schritten von 10 W. Eine falsche Grenze kann die Batterie stärker entladen "
                "oder einen Generator überlasten.",
                "Source power limit for generator peak shaving, 500 to 16000 W in steps of "
                "10 W. A wrong limit can discharge the battery harder or overload a generator.",
            )
            + peak,
            low=500,
            high=16000,
            step=10,
            group="peak_shaving",
            gates=(Gate(178, 0x000C, frozenset({3}), "gen_peak_shaving_off"),),
            guards=("gen_source",),
            report_registers=(133, 189),
            envelope="generator_capacity_w",
        ),
        _power(
            "grid_peak_shaving_power",
            191,
            name=Text("Grid Peak Shaving: Leistung (R191)", "Grid Peak Shaving power (R191)"),
            risk=Text(
                "Leistungsgrenze für das GRID-Peak-Shaving, 1000 bis 16000 W in Schritten "
                "von 100 W (App-Bereich; Werte unter 50 würden in der Firmware unterlaufen und "
                "bleiben ausgeschlossen). Eine falsche Grenze ändert Bezug und Batterieeinsatz.",
                "Power limit for GRID peak shaving, 1000 to 16000 W in steps of 100 W (an app "
                "range; values below 50 would underflow in the firmware and stay excluded). A "
                "wrong limit changes import and battery use.",
            )
            + peak,
            low=1000,
            high=16000,
            step=100,
            group="peak_shaving",
            gates=(Gate(178, 0x0030, frozenset({3}), "grid_peak_shaving_off"),),
            envelope="grid_limit_w",
        ),
        WriteDescriptor(
            key="export_mode",
            register=142,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SELECT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=2,
            name=Text("System Work Mode (R142)", "System Work Mode (R142)"),
            risk=Text(
                "Wählt Selling First, Zero Export to Load oder Zero Export to CT. Ein falscher "
                "Modus kann unzulässig einspeisen oder "
                "unerwartet Netzbezug und Batterieentladung auslösen; Zero Export to CT "
                "braucht einen in Betrieb genommenen CT/Zähler, ein Messausfall "
                "wird nicht abgefangen. Vor einer Änderung werden die dadurch wirksamen "
                "Einstellungen (R143, R145, R190/R191, Messweg R344) frisch gelesen, geprüft und "
                "angezeigt, aber nie geändert; Zero Export to CT braucht einen "
                "beschriebenen und passend konfigurierten Messweg (ob die Messung "
                "tatsächlich arbeitet, prüft die App nicht). Eine Änderung der "
                "Anlagenbeschreibung schreibt nie selbst." + _EXPORT_TAIL.de,
                "Selects Selling First, Zero Export to Load or Zero Export to CT. A wrong "
                "mode can export without permission or cause unexpected import and battery "
                "discharge; Zero Export to CT needs a commissioned CT / meter and a loss of "
                "measurement is not handled. Before a change the settings that become active "
                "(R143, R145, R190/R191, measurement path R344) are read fresh, validated and "
                "shown but never changed; Zero Export to CT needs a described and matching "
                "configured measurement path (whether the measurement actually works is not "
                "checked by the app). Changing the installation description never writes by "
                "itself." + _EXPORT_TAIL.en,
            ),
            source=f"{_V}; contracts-1 (R142)",
            group="export",
            reject_sentinel=True,
            report_registers=(143, 145, 190, 191, 344),
            context_registers=(178, 133, 189, 340, 553),
            options=(
                Option(
                    0,
                    "sell",
                    Text("Selling First", "Selling First"),
                    feature=("export_policy", "export_allowed", "export_policy_mismatch"),
                    guard="export_target",
                ),
                Option(
                    1,
                    "internal_zero",
                    Text("Zero Export to Load", "Zero Export to Load"),
                    feature=("export_policy", "zero_export_load", "export_policy_mismatch"),
                    guard="export_target",
                ),
                Option(
                    2,
                    "external_zero",
                    Text("Zero Export to CT", "Zero Export to CT"),
                    feature=("export_policy", "zero_export_external", "export_policy_mismatch"),
                    guard="export_target",
                ),
            ),
            feature_gates=(_NO_GRID,),
        ),
        _power(
            "export_max_power",
            143,
            name=Text("Maximale Einspeiseleistung (R143)", "Maximum power sold (R143)"),
            risk=Text(
                "Obergrenze der Netzeinspeisung, 0 bis 12000 W in Schritten von 100 W, höchstens "
                "die Nennleistung des Modells (R20/R21). Ersetzt keine Einspeisegenehmigung des "
                "Netzbetreibers und garantiert keine genau gemessene Leistung. Nur im Modus "
                "„Selling First“ (R142 = 0) und nie über der in der Anlagenbeschreibung "
                "eingetragenen Einspeisegrenze; ohne Eintrag gesperrt." + _EXPORT_TAIL.de,
                "Upper limit of the grid export, 0 to 12000 W in steps of 100 W, at most the "
                "model's rated power (R20/R21). Does not replace an export permit of the grid "
                "operator and guarantees no exactly metered power. Only in the mode “power sales "
                "enabled” (R142 = 0) and never above the export limit entered in the "
                "installation description; locked without that entry." + _EXPORT_TAIL.en,
            ),
            low=0,
            high=12000,
            step=100,
            group="export",
            gates=(Gate(142, 0xFFFF, frozenset({0}), "export_not_enabled"),),
            feature_gates=(_NO_GRID,),
            envelope="export_limit_w",
        ),
        WriteDescriptor(
            key="pv_sale_enable",
            register=145,
            mask=0xFFFF,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("Solar Sell (R145)", "Solar Sell (R145)"),
            risk=Text(
                "EIN erlaubt den PV-Verkauf (Solar Sell), AUS sperrt ihn. Das ganze Register wird "
                "als 0 oder 1 geschrieben; steht ein anderer Wert darin, wird nicht geschrieben. "
                "Falsche Topologie oder Grenzen können gegen die Anlagenvorgabe einspeisen. Nur "
                "mit Zero Export (R142 = 1 oder 2); EIN zusätzlich nur mit gültiger "
                "Einspeisegrenze R143 und eingetragener Einspeisegrenze der Anlage."
                + _EXPORT_TAIL.de,
                "ON permits PV sales (Solar Sell), OFF blocks them. The whole register is "
                "written as 0 or 1; if it holds anything else nothing is written. A wrong "
                "topology or limits can export against the plant policy. Only with Zero Export "
                "(R142 = 1 or 2); ON additionally only with a valid export limit R143 and an "
                "export limit entered in the installation description." + _EXPORT_TAIL.en,
            ),
            source=f"{_V}; contracts-1 (R145)",
            group="export",
            whole_word_bool=True,
            gates=(Gate(142, 0xFFFF, frozenset({1, 2}), "solar_sell_mode_required"),),
            feature_gates=(_NO_GRID,),
            transition="solar_sell",
            context_registers=(143, 340),
        ),
        WriteDescriptor(
            key="zero_export_bias",
            register=104,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="",
            scale=1,
            min_raw=20,
            max_raw=500,
            step_raw=10,
            name=Text("Zero Export: Mindestgrenze (R104)", "Zero Export minimum limit (R104)"),
            risk=Text(
                "Mindest-Leistungsvorgabe der Zero Export als Rohwert 20 bis 500 in Schritten "
                "von 10 (Bereich der LCD-Eingabe). Die physikalische Einheit und alle "
                "Moduswechsel sind nicht belegt; die App rechnet nichts um. Ein falscher Wert "
                "kann unerwartet Netzbezug oder Einspeisung erzeugen. Nur in den "
                "Zero-Export-Modi (R142 = 1 oder 2) mit in Betrieb genommener Messung."
                + _EXPORT_TAIL.de,
                "Minimum power setting of the Zero Export control as a raw value 20 to 500 in "
                "steps of 10 (the range of the LCD editor). The physical unit and every mode "
                "transition are unproved; the app converts nothing. A wrong value can cause "
                "unexpected import or export. Only in the Zero Export modes (R142 = 1 or 2) with "
                "a commissioned measurement." + _EXPORT_TAIL.en,
            ),
            source=f"{_V}; contracts-1 (R104)",
            group="export",
            plain_number=True,
            gates=(Gate(142, 0xFFFF, frozenset({1, 2}), "zero_export_mode_required"),),
            feature_gates=(_NO_GRID,),
        ),
        _power(
            "pv_sale_max_power",
            340,
            name=Text("Maximale PV-Verkaufsleistung (R340)", "Maximum PV power sold (R340)"),
            risk=Text(
                "Obergrenze der verkauften PV-Leistung, 400 bis 12000 W in Schritten von 100 W, "
                "höchstens die Nennleistung des Modells. Die Firmware rechnet kleine Werte über "
                "eine modellabhängige Ersatzformel; das LCD-Rohmaß muss nicht der internen "
                "1-W-Größe entsprechen. Eine falsche Grenze kann viel mehr einspeisen oder viel "
                "Erzeugung abregeln. Nie über der in der Anlagenbeschreibung eingetragenen "
                "Einspeisegrenze; ohne Eintrag gesperrt." + _EXPORT_TAIL.de,
                "Upper limit of the PV power sold, 400 to 12000 W in steps of 100 W, at most the "
                "model's rated power. For small values the firmware uses a model-dependent "
                "fallback formula; the LCD raw unit need not equal the internal 1 W quantity. A "
                "wrong limit can export far more or curtail far more generation. Never above the "
                "export limit entered in the installation description; locked without that "
                "entry." + _EXPORT_TAIL.en,
            ),
            low=400,
            high=12000,
            step=100,
            group="export",
            feature_gates=(_NO_GRID,),
            envelope="export_limit_w",
        ),
    )


_PAIR_RISK_TAIL = Text(
    " EIN schreibt das Bit-Paar 11, AUS das Paar 10; alle anderen Bit-Paare des Wortes bleiben "
    "unverändert. Steht dort 00 oder 01 („keine Änderung“) oder das Wort 0xFFFF, ist der "
    "Zustand unbekannt und es wird nicht geschrieben. Gilt für die ganze Parallelanlage (vom "
    "Master kopiert). Der gelesene Wert ist die konfigurierte Anforderung, kein Nachweis einer "
    "gemessenen Reaktion oder eines geprüften Schutzes.",
    " ON writes the bit pair 11, OFF the pair 10; all other bit pairs of the word stay "
    "unchanged. A pair of 00 or 01 (“no action”) or the word 0xFFFF is an unknown state and "
    "nothing is written. Applies to the whole parallel plant (copied from the master). The "
    "value read is the configured request, not proof of a measured response or a verified "
    "protection.",
)


def _pair(
    key: str, mask: int, shift: int, group: str, name: Text, risk: Text, **extra: Any
) -> WriteDescriptor:
    return WriteDescriptor(
        key=key,
        register=178,
        mask=mask,
        shift=shift,
        kind=Kind.BIT,
        unit="",
        scale=1,
        min_raw=2,
        max_raw=3,
        name=name,
        risk=risk + _PAIR_RISK_TAIL,
        source=f"{_V}; contracts-1 (R178 selective pair, binding owner)",
        group=group,
        on_field=3,
        off_field=2,
        reject_sentinel=True,
        **extra,
    )


def _r178_descriptors() -> tuple[WriteDescriptor, ...]:
    return (
        _pair(
            "microinverter_export_cutoff",
            0x0003,
            0,
            "island",
            Text(
                "Mikro-Wechselrichter-Einspeisekappung (R178 Bit 0-1)",
                "Microinverter export cutoff (R178 bits 0-1)",
            ),
            Text(
                "Schaltet die Abregelung gekoppelter Mikro-Wechselrichter bei Überschuss.",
                "Switches the curtailment of coupled microinverters on surplus.",
            ),
        ),
        _pair(
            "generator_peak_shaving",
            0x000C,
            2,
            "peak_shaving",
            Text(
                "Generator Peak Shaving (R178 Bit 2-3)",
                "Generator Peak Shaving (R178 bits 2-3)",
            ),
            Text(
                "Schaltet das Peak Shaving der Generatorquelle (Leistung R190). Die "
                "Firmware kann dabei Energie-Modus und ToU selbst umstellen. EIN prüft die "
                "Leistung R190 gegen die Anlagenbeschreibung (Generatorleistung) und die "
                "Generatorquelle; AUS ist immer möglich.",
                "Switches peak shaving of the generator source (power R190). The firmware may "
                "then change the energy mode and ToU itself. ON checks the power R190 against "
                "the installation description (generator capacity) and the generator source; "
                "OFF is always possible.",
            ),
            transition="peak_shaving_on",
            context_registers=(133, 189, 190),
        ),
        _pair(
            "grid_peak_shaving",
            0x0030,
            4,
            "peak_shaving",
            Text("Grid Peak Shaving (R178 Bit 4-5)", "Grid Peak Shaving (R178 bits 4-5)"),
            Text(
                "Schaltet das Peak Shaving des Netzanschlusses (Leistung R191). Die "
                "Firmware kann dabei Energie-Modus und ToU selbst umstellen. EIN prüft die "
                "Leistung R191 gegen die Netzanschlussgrenze der Anlagenbeschreibung; AUS ist "
                "immer möglich.",
                "Switches peak shaving of the grid connection (power R191). The firmware may "
                "then change the energy mode and ToU itself. ON checks the power R191 against "
                "the grid connection limit of the installation description; OFF is always "
                "possible.",
            ),
            transition="peak_shaving_on",
            context_registers=(191,),
        ),
        _pair(
            "on_grid_smart_load_always_on",
            0x00C0,
            6,
            "smart_load",
            Text(
                "Smart Load im Netzbetrieb immer an (R178 Bit 6-7)",
                "Smart Load always on while on-grid (R178 bits 6-7)",
            ),
            Text(
                "EIN hält die Smart-Load-Ausgabe im Netzbetrieb dauerhaft eingeschaltet, "
                "unabhängig von den Schwellen. Eine angeschlossene Last wird dann nicht "
                "mehr durch die Batterieschwellen geschützt.",
                "ON keeps the Smart Load output energised permanently while on-grid, regardless "
                "of the thresholds. A connected load is then no longer protected by the battery "
                "thresholds.",
            ),
        ),
        _pair(
            "drm_enable",
            0x3000,
            12,
            "island",
            Text("DRM-Eingang aktivieren (R178 Bit 12-13)", "DRM input enable (R178 bits 12-13)"),
            Text(
                "Schaltet die Auswertung des DRM-Anschlusses (Netzbetreiber-Abregelung). AUS "
                "ignoriert Vorgaben des Netzbetreibers, die dort anliegen; die Zulässigkeit "
                "hängt von den örtlichen Vorschriften ab.",
                "Switches the evaluation of the DRM port (grid-operator curtailment). OFF "
                "ignores commands from the grid operator present there; whether that is "
                "permitted depends on local rules.",
            ),
        ),
    )


EV_SIGNATURES: Final = frozenset({0xDD, 0xDE, 0xED, 0xEE})  # the R259 low bytes the flags exist in
_EV_GATE = Gate(259, 0x00FF, EV_SIGNATURES, "ev_signature_unknown")
# the whole R259 encoding must be supported: the incumbent SOC byte is 0..100 (an invalid byte such
# as 0xFF is never coerced, and no EV change, also of R260, is made over it)
_EV_SOC_WORD = Gate(259, 0xFF00, frozenset(range(101)), "ev_soc_word_invalid")
_EV_TAIL = Text(
    " Gilt nur für das angesprochene Gerät (der Master kopiert dieses Register nicht). Das "
    "Register wird nur gelesen und geschrieben, wenn sein unteres Byte eine der bekannten "
    "Kennungen DD, DE, ED oder EE und ein gültiges SOC-Byte (0 bis 100 %) hat; eine unbekannte "
    "Kennung oder ein ungültiges Byte wird nie neu gesetzt oder korrigiert. Voraussetzung ist ein "
    "in der Anlagenbeschreibung angegebener Fahrzeuglader, dessen Anschluss (GRID oder LOAD) zur "
    "gelesenen Einstellung passt. Die Wirkung auf die tatsächliche Ladeleistung des Fahrzeugs ist "
    "nicht belegt.",
    " Applies to the addressed unit only (the master does not copy this register). The register "
    "is read and written only while its low byte holds one of the known signatures DD, DE, ED "
    "or EE and a valid SOC byte (0 to 100 %); an unknown signature or an invalid byte is never "
    "set or corrected. A vehicle charger stated in the installation description is required, "
    "whose port (GRID or LOAD) matches the setting read. The effect on the power the vehicle "
    "actually charges with is not proved.",
)


def _ev_descriptors() -> tuple[WriteDescriptor, ...]:
    return (
        WriteDescriptor(
            key="ev_free_work",
            register=259,
            mask=0x0003,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=1,
            max_raw=2,
            name=Text(
                "EV-Laden: Free Work (R259 Bit 0-1)", "EV charging: free work (R259 bits 0-1)"
            ),
            risk=Text(
                "EIN erlaubt dem EV-Laden die freie Arbeitsweise, AUS nur Solarüberschuss "
                "(„Solar-only“). Ein falscher Wert ändert die Quelle des Ladestroms (PV, "
                "Batterie, Netz) und kann gewollte Reserven umgehen. Beide Bits werden in "
                "einem Wort ersetzt, ohne Zwischenzustand 0 oder 3." + _EV_TAIL.de,
                "ON allows the free working mode of EV charging, OFF solar surplus only "
                "(“solar-only”). A wrong value changes the source of the charging power (PV, "
                "battery, grid) and can bypass intended reserves. Both bits are replaced in "
                "one word, with no intermediate state 0 or 3." + _EV_TAIL.en,
            ),
            source=f"{_V}; contracts-1 (R259)",
            group="ev",
            on_field=2,
            off_field=1,
            reject_sentinel=True,
            gates=(_EV_GATE, _EV_SOC_WORD),
            guards=("ev_context",),
            transition="ev_free_work",
            context_registers=(117, 260),
        ),
        WriteDescriptor(
            key="ev_location",
            register=259,
            mask=0x0030,
            shift=4,
            kind=Kind.SELECT,
            unit="",
            scale=1,
            min_raw=1,
            max_raw=2,
            name=Text(
                "EV-Laden: Anschluss (R259 Bit 4-5)", "EV charging: location (R259 bits 4-5)"
            ),
            risk=Text(
                "Wählt, ob der Fahrzeuglader am GRID- oder am LOAD-Anschluss hängt. Ein "
                "falscher Anschluss verändert die Leistungsflüsse und die Begrenzung des "
                "Ladestroms. Beide Bits werden in einem Wort ersetzt." + _EV_TAIL.de,
                "Selects whether the vehicle charger hangs on the GRID or the LOAD port. A wrong "
                "port changes the power flows and the limit of the charging power. Both bits "
                "are replaced in one word." + _EV_TAIL.en,
            ),
            source=f"{_V}; contracts-1 (R259)",
            group="ev",
            reject_sentinel=True,
            gates=(_EV_GATE, _EV_SOC_WORD),
            guards=("ev_charger",),
            transition="ev_location",
            context_registers=(117, 260),
            options=(
                Option(
                    1,
                    "grid",
                    Text("GRID-Anschluss", "GRID port"),
                    feature=("ev_charger", "grid_port", "ev_port_not_described"),
                ),
                Option(
                    2,
                    "load",
                    Text("LOAD-Anschluss", "LOAD port"),
                    feature=("ev_charger", "load_port", "ev_port_not_described"),
                ),
            ),
        ),
        WriteDescriptor(
            key="ev_offgrid_soc",
            register=259,
            mask=0xFF00,
            shift=8,
            kind=Kind.SCALAR,
            unit="%",
            scale=1,
            min_raw=0,
            max_raw=100,
            step_raw=10,
            name=Text(
                "EV-Laden: Inselbetrieb-SOC (R259 Bit 8-15)",
                "EV charging: off-grid SOC (R259 bits 8-15)",
            ),
            risk=Text(
                "Batterie-SOC (0 bis 100 % in Schritten von 10 %), ab dem im Inselbetrieb "
                "für das EV-Laden Energie entnommen werden darf. Nur im SOC-Modus (R111 = 1), nie "
                "unter der in der Anlagenbeschreibung eingetragenen EV-Reserve (ohne Eintrag "
                "gesperrt) und zusätzlich nie unter dem Niedrig-SOC der Batterie (R117, frisch "
                "gelesen); ein zu niedriger Wert kann die Reserve für die Hausversorgung "
                "aufbrauchen. Ein Zielwert wird nie stillschweigend angehoben." + _EV_TAIL.de,
                "Battery SOC (0 to 100 % in steps of 10 %) from which energy may be taken for "
                "EV charging in off-grid operation. Only in SOC mode (R111 = 1), never below "
                "the EV reserve entered in the installation description (locked without that "
                "entry) and additionally never below the battery low SOC (R117, read fresh); a "
                "value that is too low can use up the reserve for the house supply. A target is "
                "never raised silently." + _EV_TAIL.en,
            ),
            source=f"{_V}; contracts-1 (R259)",
            group="ev",
            reject_sentinel=True,
            needs_battery=True,
            requires_r111=R111_SOC,
            gates=(_EV_GATE, _EV_SOC_WORD),
            guards=("ev_context", "ev_reserve"),
            transition="ev_soc",
            context_registers=(115, 117),
            floor_feature="ev_offgrid_reserve_soc",
        ),
        WriteDescriptor(
            key="ev_max_power",
            register=260,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="W",
            scale=1,
            min_raw=0,
            max_raw=22000,
            step_raw=100,
            name=Text("EV-Laden: maximale Leistung (R260)", "EV charging: maximum power (R260)"),
            risk=Text(
                "Obergrenze der EV-Ladeleistung, 0 bis 22000 W in Schritten von 100 W, nie über "
                "der in der Anlagenbeschreibung eingetragenen Leistung von Lader bzw. Zuleitung "
                "(ohne Eintrag gesperrt). Ein zu hoher Wert kann den Fahrzeugkreis oder Lader "
                "überlasten oder unerwartet Netz- und Batterieleistung ziehen. Historische "
                "Parameter-Watt garantieren keine gelieferte Leistung." + _EV_TAIL.de,
                "Upper limit of the EV charging power, 0 to 22000 W in steps of 100 W. A value "
                "that is too high can overload the vehicle circuit or charger or draw "
                "unexpected grid and battery power. Never above the charger / supply line "
                "rating entered in the installation description (locked without that entry). "
                "Historic parameter watts do not guarantee delivered power." + _EV_TAIL.en,
            ),
            source=f"{_V}; contracts-1 (R260)",
            group="ev",
            plain_number=True,
            reject_sentinel=True,
            gates=(_EV_GATE, _EV_SOC_WORD),
            guards=("ev_context",),
            envelope="ev_charger_limit_w",
        ),
        WriteDescriptor(
            key="unbalanced_load",
            register=237,
            mask=0xFFFF,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("Asymmetric phase feeding (R237)", "Asymmetric phase feeding (R237)"),
            risk=Text(
                "EIN schaltet die Behandlung unsymmetrischer Last um (die Leistungsgrenze je "
                "Phase wird dann anders abgeleitet); einzelne Phasen können ihre Grenze vor der "
                "Gesamtnennleistung erreichen und Versorgung oder Schutz beeinflussen. Das ganze "
                "Register wird als 0 oder 1 geschrieben; steht ein anderer Wert darin, wird "
                "nicht geschrieben. Phasenströme und -leistungen müssen innerhalb der in "
                "Betrieb genommenen Grenzen von Modell und Parallelsystem bleiben. Gilt nur für "
                "das angesprochene Gerät; in einer Parallelanlage bitte an allen Geräten "
                "gleich halten.",
                "ON switches the handling of unbalanced load (the per-phase power limit is then "
                "derived differently); single phases can reach their limit before the total "
                "rated power and affect supply or protection. The whole register is written as "
                "0 or 1; if it holds anything else nothing is written. Phase currents and "
                "powers must stay inside the commissioned limits of the model and the parallel "
                "system. Applies to the addressed unit only; in a parallel plant please keep "
                "it equal on all units.",
            ),
            source=f"{_V}; contracts-1 (R237)",
            group="engineering",
            whole_word_bool=True,
            equal_hint=True,
        ),
    )


def _energy_pattern() -> WriteDescriptor:
    return WriteDescriptor(
        key="energy_pattern",
        register=141,
        mask=0xFFFF,
        shift=0,
        kind=Kind.SELECT,
        unit="",
        scale=1,
        min_raw=0,
        max_raw=1,
        name=Text("Energiemuster (R141)", "Energy pattern (R141)"),
        risk=Text(
            "Wählt das Energiemuster der Regelung: 0 = Batterie zuerst, 1 = Last zuerst. Das "
            "ändert, ob PV-Überschuss zuerst die Batterie lädt oder die Last versorgt, und "
            "damit Strom-, Spannungs- und SOC-Regelzweige. Das ganze Register wird als 0 oder "
            "1 geschrieben; steht ein anderer Wert darin, wird nicht geschrieben. Die "
            "Firmware kann den Wert bei aktivem Peak Shaving selbst auf 1 setzen und die "
            "App kämpft nicht dagegen. Gilt für die ganze Parallelanlage (vom Master kopiert); "
            "ein gelesener Wert ist die konfigurierte Einstellung, kein Nachweis der "
            "tatsächlichen Wirkung.",
            "Selects the energy pattern of the control: 0 = battery first, 1 = load first. This "
            "changes whether PV surplus first charges the battery or supplies the load, and so "
            "the current, voltage and SOC regulation branches. The whole register is written as "
            "0 or 1; if it holds anything else nothing is written. The firmware can set the "
            "value to 1 itself while peak shaving is active and the app does not fight it. "
            "Applies to the whole parallel plant (copied from the master); the value read is "
            "the configured setting, not proof of the actual effect.",
        ),
        source=f"{_V}; contracts-1 (R141 C070 staging 200003F4)",
        group="battery",
        reject_sentinel=True,
        options=(
            Option(0, "batt_first", Text("Batterie zuerst", "Batt First")),
            Option(1, "load_first", Text("Last zuerst", "Load First")),
        ),
    )


def _build() -> tuple[WriteDescriptor, ...]:
    out: list[WriteDescriptor] = [
        WriteDescriptor(
            key="battery_charge_current",
            register=108,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="A",
            scale=1,
            min_raw=0,
            max_raw=185,
            name=Text(
                "Maximaler Batterie-Ladestrom (R108)", "Maximum battery charge current (R108)"
            ),
            risk=_CURRENT_RISK,
            source=f"{_V}; cap: {CURRENT_CAP_SOURCE}",
            current_cap=True,
            firmware_clamp="charge",
            needs_battery=True,
        ),
        WriteDescriptor(
            key="battery_discharge_current",
            register=109,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="A",
            scale=1,
            min_raw=0,
            max_raw=185,
            name=Text(
                "Maximaler Batterie-Entladestrom (R109)", "Maximum battery discharge current (R109)"
            ),
            risk=Text(
                "Begrenzt den Batterie-Entladestrom. Ein niedriger Wert kann die Versorgung im "
                "Inselbetrieb schwächen.",
                "Limits the battery discharge current. A low value can weaken the supply in "
                "island operation.",
            ),
            source=f"{_V}; cap: {CURRENT_CAP_SOURCE}",
            current_cap=True,
            firmware_clamp="discharge",
            needs_battery=True,
        ),
        _soc(
            "battery_shutdown_soc",
            115,
            name=Text("Batterie-Abschalt-SOC (R115)", "Battery shutdown SOC (R115)"),
            risk=Text(
                "Unterhalb dieses SOC schaltet der Wechselrichter die Batterie ab (kein bloßer "
                "Alarm; im Inselbetrieb endet damit die Versorgung).",
                "Below this SOC the inverter shuts the battery down (not just an alarm; in "
                "island operation the supply ends).",
            )
            + _PROTECTION_ORDER
            + _SOC_MODE,
            below=("battery_low_soc",),
        ),
        _soc(
            "battery_low_soc",
            117,
            name=Text("Batterie-Niedrig-SOC (R117)", "Battery low SOC (R117)"),
            risk=Text(
                "Niedrig-Schwelle der Batterie: beeinflusst, wann der Wechselrichter die Batterie "
                "als nahezu leer behandelt.",
                "Low threshold of the battery: affects when the inverter treats the battery as "
                "nearly empty.",
            )
            + _PROTECTION_ORDER
            + _SOC_MODE,
            above=("battery_shutdown_soc",),
            below=("battery_restart_soc",),
        ),
        _soc(
            "battery_restart_soc",
            116,
            name=Text("Batterie-Neustart-SOC (R116)", "Battery restart SOC (R116)"),
            risk=Text(
                "Ab diesem SOC startet der Wechselrichter nach einer Abschaltung die Batterie "
                "wieder.",
                "From this SOC the inverter starts the battery again after a shutdown.",
            )
            + _PROTECTION_ORDER
            + _SOC_MODE,
            above=("battery_low_soc",),
        ),
        WriteDescriptor(
            key="generator_charge_current",
            register=125,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="A",
            scale=1,
            min_raw=0,
            max_raw=185,
            name=Text("Generator-Ladestrom (R125)", "Generator charge current (R125)"),
            risk=Text(
                "Nur für eine geprüfte Generator-Ladekonfiguration. Es ist nicht belegt, dass ein "
                "Generator am GRID-Anschluss dieses Feld nutzt.",
                "Only for a verified generator charging configuration. It is not established that "
                "a generator on the GRID port uses this field.",
            ),
            source=f"{_V}; cap: {CURRENT_CAP_SOURCE}",
            group="grid_charge",
            current_cap=True,
            needs_battery=True,
        ),
        WriteDescriptor(
            key="grid_charge_current",
            register=128,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="A",
            scale=1,
            min_raw=0,
            max_raw=185,
            name=Text("Netz-Ladestrom (R128)", "GRID charge current (R128)"),
            risk=Text(
                "Wirkt beim Laden aus dem Netz. Ändert sich nur etwas, wenn Netzladen aktiv ist; "
                "ein inaktiver Wert kann später plötzlich wirksam werden.",
                "Applies to charging from the grid. It only has an effect while grid charging is "
                "active; an inactive value can take effect later.",
            ),
            source=f"{_V}; cap: {CURRENT_CAP_SOURCE}",
            group="grid_charge",
            current_cap=True,
            needs_battery=True,
            upper=(Bound("battery_charge_current"),),
        ),
        WriteDescriptor(
            key="grid_charge_enable",
            register=130,
            mask=0x0001,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("Netzladen aktivieren (R130)", "GRID charge enable (R130)"),
            risk=Text(
                "Schaltet das Laden der Batterie aus dem Netz. Das ganze Register wird als 0 oder "
                "1 geschrieben (die Firmware vergleicht das ganze Wort mit 1); steht ein anderer "
                "Wert darin, wird nicht geschrieben.",
                "Switches charging the battery from the grid. The whole register is written as 0 "
                "or 1 (the firmware compares the whole word with 1); if it holds anything else "
                "nothing is written.",
            ),
            source=f"{_V}; {_E}",
            group="grid_charge",
            needs_battery=True,
            whole_word_bool=True,
        ),
        _soc(
            "grid_charge_start_soc",
            127,
            name=Text("Netzladen ab SOC (R127)", "GRID charge start SOC (R127)"),
            risk=_soc_risk(
                "Unter diesem SOC beginnt das Laden aus dem Netz (10 bis 90 %, so begrenzt es die "
                "Firmware). Ob geladen wird, hängt zusätzlich von R130 und dem Betriebszustand ab.",
                "Below this SOC charging from the grid starts (10 to 90 %, the firmware limit). "
                "Whether it charges also depends on R130 and the operating state.",
            ),
            low=10,
            high=90,
            group="grid_charge",
        ),
        _smart(
            "smart_load_off_voltage",
            134,
            voltage=True,
            off=True,
            companion="smart_load_on_voltage",
            name=Text("Smart Load AUS-Spannung (R134)", "Smart Load OFF voltage (R134)"),
            risk=_OFF_RISK,
        ),
        _smart(
            "smart_load_off_soc",
            135,
            voltage=False,
            off=True,
            companion="smart_load_on_soc",
            name=Text("Smart Load AUS-SOC (R135)", "Smart Load OFF SOC (R135)"),
            risk=_OFF_RISK,
        ),
        _smart(
            "smart_load_on_voltage",
            136,
            voltage=True,
            off=False,
            companion="smart_load_off_voltage",
            name=Text("Smart Load EIN-Spannung (R136)", "Smart Load ON voltage (R136)"),
            risk=_ON_RISK,
        ),
        _smart(
            "smart_load_on_soc",
            137,
            voltage=False,
            off=False,
            companion="smart_load_off_soc",
            name=Text("Smart Load EIN-SOC (R137)", "Smart Load ON SOC (R137)"),
            risk=_ON_RISK,
        ),
        WriteDescriptor(
            key="tou_enable",
            register=146,
            mask=0x0001,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text(
                "Time of Use aktivieren (R146 Bit 0)", "Time of Use schedule enable (R146 bit 0)"
            ),
            risk=Text(
                "Schaltet die zeitgesteuerte Batteriesteuerung ein oder aus. Die Wochentagsbits "
                "und alle anderen Bits bleiben unverändert. Die Firmware kann Bit 0 selbst setzen "
                "(etwa bei Peak Shaving); der Schalter zeigt nur die gelesene Einstellung. "
                "Nicht im Spanien-Modus (Bit 8).",
                "Switches the time-of-use battery control on or off. The weekday bits and all "
                "other bits stay unchanged. The firmware can set bit 0 itself (for example with "
                "peak shaving); the switch shows the configuration read. Not in Spain mode "
                "(bit 8).",
            ),
            source=f"{_V}; {_E}",
            group="tou",
            needs_battery=True,
            tou=True,
        ),
        *(
            WriteDescriptor(
                key=f"tou_weekday_{tag}",
                register=146,
                mask=0x00FE,
                shift=1,
                kind=Kind.BIT,
                unit="",
                scale=1,
                min_raw=0,
                max_raw=1,
                name=Text(
                    f"Time of Use gilt am {de} (R146 Bit {bit})",
                    f"Time of Use schedule applies on {en} (R146 bit {bit})",
                ),
                risk=Text(
                    "Wochentagsfilter des Zeitplans (R146 Bits 1-7 = Montag bis Sonntag). Sind "
                    "alle sieben Bits null, gilt der Zeitplan an jedem Tag; die App zeigt dann "
                    "alle Tage als EIN und schreibt beim Ausschalten eines Tages die übrigen "
                    "sechs ausdrücklich. Der letzte Tag kann nicht ausgeschaltet werden. Die "
                    "Zuordnung der Bits zu Wochentagen und die Uhr des Geräts stammen aus der "
                    "LCD-Beschriftung; ein falscher Wochentag verschiebt den Zeitplan.",
                    "Weekday filter of the schedule (R146 bits 1-7 = Monday to Sunday). If all "
                    "seven bits are zero the schedule applies every day; the app then shows all "
                    "days as ON and writes the other six explicitly when one day is switched "
                    "off. The last day cannot be switched off. The bit-to-weekday mapping and "
                    "the device clock come from the LCD labels; a wrong weekday shifts the "
                    "schedule.",
                ),
                source=f"{_V}; contracts-1 (R146)",
                group="tou",
                needs_battery=True,
                tou=True,
                weekday=bit - 1,
                guards=("tou_context",),
                context_registers=TOU_CONTEXT,
            )
            for tag, de, en, bit in WEEKDAYS
        ),
        WriteDescriptor(
            key="tou_spain_mode",
            register=146,
            mask=SPAIN_MASK,
            shift=8,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text(
                "Time of Use: Spanien-Modus (R146 Bit 8)", "Time of Use: Spain mode (R146 bit 8)"
            ),
            risk=Text(
                "Schaltet die Zeitplanlogik auf einen anderen Firmwarezweig (Spanien-Modus, vom "
                "LCD „Mode4“ genannt): Leistung, Ziel-SOC und Netzladen der Zeitabschnitte "
                "werden dann aus den Spanien-Bits der Slot-Flags abgeleitet. Das ist kein "
                "Nachweis einer Normkonformität. Solange der Modus an ist, sind die normalen "
                "Zeitplan-Einstellungen gesperrt. Alle anderen Bits von R146 bleiben unverändert.",
                "Switches the schedule logic to a different firmware branch (Spain mode, called "
                "“Mode4” by the LCD): power, target SOC and grid charging of the intervals are "
                "then derived from the Spain bits of the slot flags. This is no proof of "
                "regulatory compliance. While the mode is on, the normal schedule settings are "
                "locked. All other bits of R146 stay unchanged.",
            ),
            source=f"{_V}; contracts-1 (R146)",
            group="tou",
            needs_battery=True,
            transition="spain_on",  # ON needs a valid schedule, clock and basis; OFF always works
            context_registers=TOU_CONTEXT,
        ),
        WriteDescriptor(
            key="force_off_grid",
            register=179,
            mask=0x0001,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("Inselbetrieb erzwingen (R179)", "Force off-grid operation (R179)"),
            risk=Text(
                "EIN erzwingt den Inselbetrieb (1), AUS ist „Auto“ (0): die normale Netzprüfung "
                "gilt wieder. Das ganze Register wird als 0 oder 1 geschrieben; steht ein anderer "
                "Wert darin, wird nicht geschrieben. Ein Wechsel kann Quellen- und "
                "Relaisübergänge sowie die Inselversorgung beeinflussen. Gilt für die ganze "
                "Parallelanlage (wird vom Master kopiert).",
                "ON forces off-grid operation (1), OFF is “Auto” (0): the normal grid "
                "qualification applies again. The whole register is written as 0 or 1; if it "
                "holds anything else nothing is written. Switching can affect source and relay "
                "transitions and the island supply. Applies to the whole parallel plant (copied "
                "from the master).",
            ),
            source=f"{_V}; switches-1 (MCU2 1175 consumes the whole word == 1)",
            group="island",
            whole_word_bool=True,
        ),
        WriteDescriptor(
            key="signal_island_mode",
            register=178,
            mask=0x0300,
            shift=8,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=2,
            max_raw=3,
            name=Text("Signal Island Mode (R178 Bit 8-9)", "Signal Island Mode (R178 bits 8-9)"),
            risk=Text(
                "Ändert die netzabhängige Relais-Vorgabe der ganzen Parallelanlage (vom Master "
                "kopiert). Im Inselbetrieb kann das die N-PE-Verbindung schließen; Erdung und "
                "Schutzverhalten setzen eine geprüfte Verdrahtung voraus. EIN schreibt das Paar "
                "11, AUS das Paar 10; alle anderen Bit-Paare bleiben unverändert. Steht dort "
                "00 oder 01 („keine Änderung“), ist der Zustand unbekannt und es wird nicht "
                "geschrieben.",
                "Changes the grid-dependent relay policy of the whole parallel plant (copied from "
                "the master). In island operation this can close the N-PE bond; earthing and "
                "protection behaviour require a verified wiring design. ON writes the pair 11, "
                "OFF the pair 10; all other bit pairs stay unchanged. A pair of 00 or 01 (“no "
                "action”) is an unknown state and nothing is written.",
            ),
            source=f"{_V}; switches-1 (C070 widget 0x0810, MCU2 0x44410)",
            group="island",
            on_field=3,
            off_field=2,
            reject_sentinel=True,
        ),
        WriteDescriptor(
            key="mppt_scan",
            register=341,
            mask=0x0004,
            shift=2,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("MPPT Scan (R341 Bit 2)", "MPPT Scan (R341 bit 2)"),
            risk=Text(
                "Ändert das MPPT-Suchverhalten und kann die PV-Arbeitspunkte verändern; die "
                "Wirkung hängt vom Reglerzustand ab. Alle anderen Bits des Wortes, besonders "
                "Bit 5-6 (Begrenzer), bleiben unverändert. Das Wort 0xFFFF (Firmware-Platzhalter) "
                "wird weder erwartet noch geschrieben.",
                "Changes the MPPT scanning behaviour and may change the PV operating points; "
                "activation depends on the controller state. All other bits of the word, "
                "especially bits 5-6 (limiter), stay unchanged. The word 0xFFFF (firmware "
                "placeholder) is neither accepted nor written.",
            ),
            source=f"{_V}; switches-1 (C070 widget 0x0814 MPPT_Scan)",
            group="pv",
            reject_sentinel=True,
        ),
        WriteDescriptor(
            key="grid_reconnection_time",
            register=180,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit="s",
            scale=1,
            min_raw=10,
            max_raw=900,
            step_raw=10,
            name=Text("Netz-Wiedereinschaltzeit (R180)", "Grid reconnection time (R180)"),
            risk=Text(
                "Wartezeit, bis sich das Gerät nach einem Netzfehler wieder zuschaltet, 10 bis "
                "900 s in Schritten von 10 s. Netzgekoppelte Einspeisung braucht die für den "
                "Netzcode passende Wiedereinschaltverzögerung (etwa VDE-AR-N 4105); ein zu "
                "kurzer Wert kann zu früh zuschalten. Ein gelesener Wert außerhalb des Rasters "
                "(etwa 901 s) wird so angezeigt und nie verändert; ein Ziel muss im Raster "
                "liegen. Ein geänderter Wert setzt einen laufenden Zähler nicht zurück. Beim "
                "Profilwechsel (R182) den Endwert neu lesen. Nur mit Netzanschluss in der "
                "Anlagenbeschreibung. Zur Einordnung wird „Inselbetrieb erzwingen“ (R179) frisch "
                "gelesen und angezeigt: bei R179 = 1 setzt der Zähler dauernd zurück und die "
                "Wiedereinschaltzeit läuft nicht an; die App schaltet R179 nie um. Gilt für die "
                "ganze Parallelanlage (vom Master kopiert).",
                "Wait before the unit reconnects after a grid fault, 10 to 900 s in steps of "
                "10 s. Grid-tied export needs the reconnection delay of the grid code (for "
                "example VDE-AR-N 4105); a value that is too short can reconnect too early. A "
                "value read off the grid (for example 901 s) is shown as it is and never "
                "changed; a target must lie on the grid. A changed value does not reset a "
                "running counter. Re-read the final value after a profile change (R182). Only "
                "with a utility grid in the installation description. For context “force "
                "off-grid” (R179) is read fresh and shown: with R179 = 1 the counter resets "
                "continuously and the reconnection time does not start; the app never toggles "
                "R179. Applies to the whole parallel plant (copied from the master).",
            ),
            source=f"{_V}; contracts-2 (R180)",
            group="grid",
            plain_number=True,
            incumbent_range=(0, 0xFFFE),
            reject_sentinel=True,
            feature_gates=(_NO_GRID,),
            report_registers=(179,),
        ),
        WriteDescriptor(
            key="external_ct_ratio",
            register=347,
            mask=0xFFFF,
            shift=0,
            kind=Kind.SCALAR,
            unit=":1",
            scale=1,
            min_raw=100,
            max_raw=20000,
            step_raw=100,
            name=Text("Externes CT-Verhältnis (R347)", "External CT ratio (R347)"),
            risk=Text(
                "Übersetzungsverhältnis N:1 des externen Stromwandlers, 100 bis 20000 in "
                "Schritten von 100. Ein falsches Verhältnis verfälscht die Messung und kann die "
                "Einspeiseregelung täuschen, soweit der aktive CT-Pfad diesen Wert nutzt (der "
                "letzte Verbraucher ist nicht belegt). Der Wert wirkt nur für einen CT; mit "
                "digitalem Zähler wird er gespeichert, kalibriert aber keine Zählerdaten. Die "
                "App ändert weder Messquelle noch Richtung oder Zählertyp (R344); der "
                "konfigurierte Messweg (R344) wird frisch gelesen und nur angezeigt. Gilt für "
                "die ganze Parallelanlage (vom Master kopiert).",
                "Turns ratio N:1 of the external current transformer, 100 to 20000 in steps of "
                "100. A wrong ratio falsifies the measurement and can mislead export control "
                "where the active CT path uses this value (the final consumer is not "
                "established). It acts for a CT only; with a digital meter it is stored but "
                "calibrates no meter data. The app changes neither measurement source nor "
                "direction nor meter type (R344); the configured measurement path (R344) is read "
                "fresh and only shown. Applies to the whole parallel plant (copied from the "
                "master).",
            ),
            source=f"{_V}; contracts-2 (R347)",
            group="export",
            plain_number=True,
            reject_sentinel=True,
            report_registers=(344,),
        ),
        _pair(
            "bms_loss_fault",
            0x0C00,
            10,
            "battery",
            Text("BMS Err_Stop (R178 Bit 10-11)", "BMS Err_Stop (R178 bits 10-11)"),
            Text(
                "OFF unterdrückt den nachgewiesenen BMS Err_Stop. Bei Kommunikationsverlust "
                "kann der Wechselrichter ohne aktuelle BMS-Vorgaben mit interner "
                "Batteriekennlinie weiterarbeiten. Modellgrenzen und andere Schutzpfade bleiben "
                "bestehen; ein Wegfall sämtlicher BMS-Grenzen ist nicht bewiesen. ON kann bei "
                "einem bestehenden Verlust den gemeinsamen Stopp und Reset auslösen; OFF löscht "
                "keinen bereits gespeicherten Fehler und startet nichts neu. Nur mit Batterie und "
                "BMS-Kommunikation in der Anlagenbeschreibung (Sicherheitsrichtlinie des "
                "Eigentümers) und nur bei Batterietyp-Wort R98 = 1 (Lithium/BMS-Betrieb, frisch "
                "gelesen); eine Abweichung zwischen Beschreibung und Gerät wird angezeigt und "
                "sperrt die Einstellung.",
                "OFF suppresses the proven BMS-loss stop. On a loss of communication the inverter "
                "can keep running on its internal battery curve without current BMS limits. "
                "Model limits and other protection paths remain; the loss of all BMS limits is "
                "not proved. ON can trigger the common stop and reset when a loss already "
                "exists; OFF clears no stored fault and restarts nothing. Only with a battery and "
                "BMS communication in the installation description (safety policy of the owner) "
                "and only with the battery type word R98 = 1 (Lithium / BMS operation, read "
                "fresh); a mismatch between description and device is shown and locks the "
                "setting.",
            ),
            feature_gates=(
                FeatureGate("battery", frozenset({False}), "no_battery"),
                FeatureGate("bms", frozenset({False}), "no_bms"),
            ),
            # fresh R98 = 1 is the proven Lithium/BMS consumer mode; no healthy-communication
            # requirement, so a deliberate OFF stays possible during a loss
            gates=(Gate(98, 0xFFFF, frozenset({1}), "bms_mode_required"),),
        ),
        WriteDescriptor(
            key="afci_enable",
            register=181,
            mask=0xFFFF,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text("AFCI konfiguriert aktiv (R181)", "AFCI configured enable (R181)"),
            risk=Text(
                "AUS schaltet den Lichtbogen-Brandschutz (DC-Arc-Erkennung) ab. Der Schalter "
                "zeigt und setzt die konfigurierte Einstellung (0 oder 1), nicht den "
                "Fehlerzustand; AUS löscht keinen gespeicherten Lichtbogenfehler, und ein "
                "erneutes EIN kann einen behaltenen Fehler wieder sichtbar machen. Das ganze "
                "Register wird als 0 oder 1 geschrieben; steht ein anderer Wert (auch 2) darin, "
                "wird nicht geschrieben. Löschen geschieht nur über die Taste „Lichtbogenfehler "
                "löschen“. AFCI-Modul-Version (R12) siehe Hardware-Hinweis.",
                "OFF disables the DC arc (fire) protection. The switch shows and sets the "
                "configured setting (0 or 1), not the fault state; OFF clears no stored arc "
                "fault, and switching ON again can expose a retained fault. The whole register "
                "is written as 0 or 1; if it holds anything else (also 2) nothing is written. "
                "Clearing is done only by the button “Clear arc fault”. For the AFCI module "
                "version (R12) see the hardware hint.",
            ),
            source=f"{_V}; contracts-2 (R181)",
            group="engineering",
            whole_word_bool=True,
            fault_probe=(R558, AFCI_FAULT_BIT),
        ),
        WriteDescriptor(
            key="afci_clear_fault",
            register=181,
            mask=0xFFFF,
            shift=0,
            kind=Kind.BUTTON,
            unit="",
            scale=1,
            min_raw=2,
            max_raw=2,
            name=Text("AFCI-Fehler löschen (R181)", "Clear AFCI fault (R181)"),
            risk=Text(
                "Schreibt einmalig den Befehl 2 und nur, wenn AFCI bereits aktiv (1) ist; von 0 "
                "aus würde der Befehl AFCI stillschweigend einschalten und wird verweigert. Die "
                "Firmware setzt 2 auf 1 zurück. „Befehl angenommen“ und „Fehler gemeldet / nicht "
                "mehr gemeldet / unbekannt“ (R558 Bit 14) werden getrennt berichtet. Das Löschen "
                "einer Meldung beweist nicht, dass die physische Ursache behoben ist. Kein "
                "Umschalten, keine Wiederholung, kein Zurücksetzen.",
                "Writes the command 2 once, and only if AFCI is already enabled (1); from 0 the "
                "command would silently enable AFCI and is refused. The firmware normalizes 2 to "
                "1. “Command accepted” and “fault still reported / no longer reported / unknown” "
                "(R558 bit 14) are reported separately. Clearing a report does not prove that "
                "the physical cause is gone. No toggling, no repeat, no restore.",
            ),
            source=f"{_V}; contracts-2 (R181 one-shot clear)",
            group="engineering",
            gates=(Gate(181, 0xFFFF, frozenset({1}), "afci_clear_needs_enabled"),),
            accepted_after=frozenset({1, 2}),
            fault_probe=(R558, AFCI_FAULT_BIT),
        ),
        WriteDescriptor(
            key="low_noise",
            register=35,
            mask=0xFFFF,
            shift=0,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text(
                "Low Noise: konfigurierte Auswahl (R35)", "Configured Low Noise selection (R35)"
            ),
            risk=Text(
                "EIN wählt 20 kHz, AUS 15 kHz als Schaltfrequenz der Brücke (keine Lüfterdrehzahl, "
                "kein gemessener Geräuschwert). Der Schalter aktualisiert die Auswahl und löst "
                "einen Steuerungs-Reset des Geräts aus; ob sich die Trägerfrequenz schon vor dem "
                "nächsten Neustart ändert, ist nicht belegt, ein unterbrechungsfreier oder "
                "glitchfreier Wechsel ebenfalls nicht. Geschrieben wird ein einziger "
                "Sonderbefehl (FC16, zwei Wörter, das zweite fest); das Register wird nur als "
                "exakt 0 oder 1 akzeptiert. Die Auswahl gilt nur für das angesprochene Gerät "
                "(kein Master-Kopieren, auch Slaves lassen sich einzeln schreiben): bitte "
                "Low_Noise an allen Geräten gleich halten.",
                "ON selects 20 kHz, OFF 15 kHz as the bridge switching frequency (not fan speed, "
                "no measured noise value). The switch updates the selector and resets the control "
                "of the unit; whether the carrier frequency changes before the next restart is "
                "unproved, and so is an uninterrupted or glitch-free change. A single special "
                "command is written (FC16, two words, the second fixed); the register is "
                "accepted only as exactly 0 or 1. The selection applies to the addressed unit "
                "only (no master copy, slaves can be written individually too): please keep "
                "Low_Noise equal on all units.",
            ),
            source=f"{_V}; contracts-2 (R35 special command)",
            group="engineering",
            command_word=100,
            whole_word_bool=True,
            equal_hint=True,
        ),
        WriteDescriptor(
            key="bypass_fan_minimum_request",
            register=341,
            mask=0x0010,
            shift=4,
            kind=Kind.BIT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=1,
            name=Text(
                "Mindest-Lüfteranforderung übergehen (R341 Bit 4)",
                "Bypass minimum fan request (R341 bit 4)",
            ),
            risk=Text(
                "EIN unterdrückt nur die Mindest-Lüfteranforderung unter bestimmten "
                "Betriebsbedingungen; AUS stellt die normale Logik wieder her. Es setzt weder "
                "den Kühlbedarf auf null noch schaltet es die Regelung oder beide Lüfter ab, "
                "und andere Anforderungen können die Lüfter weiter laufen lassen. Lüfterdrehzahl, "
                "Leistungsaufnahme und thermische Reserve sind nicht gemessen. Alle anderen Bits "
                "des Wortes (besonders Bit 5-6) bleiben unverändert; 0xFFFF wird weder erwartet "
                "noch geschrieben.",
                "ON suppresses only the minimum fan request under certain operating conditions; "
                "OFF restores the normal logic. It neither zeroes the cooling demand nor switches "
                "off the control or both fans, and other requests can keep the fans running. Fan "
                "speed, power consumption and thermal margin are not measured. All other bits of "
                "the word (especially bits 5-6) stay unchanged; 0xFFFF is neither accepted nor "
                "written.",
            ),
            source=f"{_V}; contracts-1 (R341 bit 4)",
            group="pv",
            reject_sentinel=True,
        ),
        WriteDescriptor(
            key="external_signal",
            register=140,
            mask=0x0003,
            shift=0,
            kind=Kind.SELECT,
            unit="",
            scale=1,
            min_raw=0,
            max_raw=2,
            name=Text("Grid Signal / Gen Signal (R140)", "Grid Signal / Gen Signal (R140)"),
            risk=Text(
                "Grid-Signal und Gen-Signal teilen sich einen Ausgang (einen Zwischenspeicher): "
                "ein Wechsel der Quelle kann einen angeschlossenen externen Schütz trotzdem "
                "lösen, und die letzte Änderung auf Aus löst ihn. Es wird immer ein einziges "
                "Wort geschrieben (nie erst Aus, dann Ein); alle anderen Bits des Wortes bleiben "
                "unverändert. „Beide“ (ein vorhandener Wert) wird nur angezeigt und kann nicht "
                "gewählt werden. Gen-Signal gibt es nur, wenn der GEN-Anschluss als Generator "
                "konfiguriert ist (R133 = 0). Gilt für die ganze Parallelanlage (vom Master "
                "kopiert).",
                "Grid Signal and Gen Signal share one output (one latch): switching the source "
                "can still release a connected external contactor, and the last change to Off "
                "releases it. Exactly one word is written (never Off first, then On); all other "
                "bits of the word stay unchanged. “Both” (an incumbent value) is shown only and "
                "cannot be chosen. Gen Signal exists only while the GEN port is configured as a "
                "generator (R133 = 0). Applies to the whole parallel plant (copied from the "
                "master).",
            ),
            source=f"{_V}; switches-1 Addendum 2 + owner update 02.11.2026; contracts-1 (R140)",
            group="signal",
            reject_sentinel=True,
            options=(
                Option(0, "off", Text("Aus", "Off")),
                Option(1, "grid", Text("Grid-Signal", "Grid Signal")),
                Option(
                    2,
                    "gen",
                    Text("Gen-Signal", "Gen Signal"),
                    gate=Gate(133, 0xFFFF, frozenset({0}), "gen_port_not_generator"),
                ),
                Option(
                    3, "both", Text("Beide (gemeinsamer Ausgang)", "Both (shared output)"), False
                ),
            ),
            feature_gates=(
                FeatureGate(
                    "external_signal_use", frozenset({"none", "unknown"}), "signal_not_used"
                ),
            ),
        ),
    ]
    out.append(_energy_pattern())
    out.extend(_battery_descriptors())
    out.extend(_gen_descriptors())
    out.extend(_export_descriptors())
    out.extend(_r178_descriptors())
    out.extend(_ev_descriptors())
    for n in range(1, TOU_SLOTS + 1):
        out.extend(_slot_descriptors(n))
    return tuple(out)


def _chained(descriptors: tuple[WriteDescriptor, ...]) -> tuple[WriteDescriptor, ...]:
    groups: list[tuple[str, ...]] = [
        ("battery_shutdown_soc", "battery_low_soc", "battery_restart_soc"),
        tuple(f"tou_slot{n}_start" for n in range(1, TOU_SLOTS + 1)),
    ]
    chain_of = {key: group for group in groups for key in group}
    return tuple(replace(d, chain=chain_of[d.key]) if d.key in chain_of else d for d in descriptors)


DESCRIPTORS: Final[tuple[WriteDescriptor, ...]] = _chained(_build())
BY_KEY: Final[dict[str, WriteDescriptor]] = {d.key: d for d in DESCRIPTORS}
OPERATION_KEYS: Final[tuple[str, ...]] = tuple(d.key for d in DESCRIPTORS)


def _bound_children() -> dict[str, tuple[tuple[WriteDescriptor, Bound, bool], ...]]:
    out: dict[str, list[tuple[WriteDescriptor, Bound, bool]]] = {}
    for d in DESCRIPTORS:
        for bound in d.upper:
            out.setdefault(bound.key, []).append((d, bound, True))
        for bound in d.lower:
            out.setdefault(bound.key, []).append((d, bound, False))
    return {k: tuple(v) for k, v in out.items()}


# parent key -> the descriptors whose dynamic bound refers to it (checked from the parent's side)
BOUND_CHILDREN: Final = _bound_children()
HARDWARE = "sg04lp3_eu"  # the descriptors are released for this hardware profile only


def rated_watts(r20: int, r21: int) -> int | None:
    """Rated power in W of the model reported in R20 (low word) and R21 (high word, 0.1 W
    units), only if it is exactly 5/6/8/10/12 kW; anything else is an unknown model (``None``)."""
    raw = (r21 << 16) | r20
    if raw % 10:
        return None
    watts = raw // 10
    return watts if watts in CURRENT_CAP_BY_RATED_W else None


def model_cap(r20: int, r21: int) -> int | None:
    """App-policy current envelope in A for the model in R20/R21; an unknown model (``None``)
    locks the current operations. R125 and R128 use exactly this."""
    watts = rated_watts(r20, r21)
    return None if watts is None else CURRENT_CAP_BY_RATED_W[watts]


def charge_branch_proven(r14: int | None, r28: int | None) -> bool:
    """Is the ordinary (220/240 A) charge-ceiling branch of a 10/12 kW unit proven by fresh words?
    R28 == 5 takes it; otherwise the hardware word must not be 1 under any reading of R14 (nibble,
    byte or either half): the derating condition is R28 != 5 and hardware word == 1."""
    if r28 == R28_ORDINARY:
        return True
    if r14 is None or r28 is None:
        return False
    return 1 not in (r14 & 0xF, r14 & 0xFF, r14 >> 8, r14 >> 12)


def firmware_ceiling(kind: str, watts: int, r14: int | None, r28: int | None) -> int:
    """Retained MCU2 firmware ceiling of the direct clamp R108 (``charge``) / R109 (``discharge``);
    an unproven 10/12 kW charge branch gets the conservative 200 A."""
    if kind == "charge":
        ceiling = FIRMWARE_CHARGE_CAP_BY_RATED_W[watts]
        if watts in BRANCH_MODELS and not charge_branch_proven(r14, r28):
            return min(ceiling, HARDWARE_BRANCH_CHARGE_CAP)
        return ceiling
    return FIRMWARE_DISCHARGE_CAP_BY_RATED_W[watts]


def model_limit(
    desc: WriteDescriptor,
    r20: int,
    r21: int,
    *,
    r14: int | None = None,
    r28: int | None = None,
) -> int | None:
    """The model-dependent maximum of ``desc`` (A or W), ``None`` if unknown or not applicable.
    R108/R109: the intersection of the app policy and the firmware ceiling (selector words
    ``r14``/``r28``, conservative when absent); R125/R128: the app policy."""
    if desc.current_cap:
        cap = model_cap(r20, r21)
        watts = rated_watts(r20, r21)
        if cap is None or watts is None or desc.firmware_clamp is None:
            return cap
        return min(cap, firmware_ceiling(desc.firmware_clamp, watts, r14, r28))
    if desc.power_cap:
        return rated_watts(r20, r21)
    return None


def limit_from_words(desc: WriteDescriptor, words: Mapping[int, int]) -> int | None:
    """``model_limit`` from a dict of fresh words (R20, R21 and, for R108, R14 and R28)."""
    return model_limit(desc, words[20], words[21], r14=words.get(R14), r28=words.get(R28))


def parse_time(value: Any) -> int:
    """Raw HHMM for ``HH:MM`` (or ``HH:MM:00``). No timezone, no seconds other than 00, minutes in
    steps of 5 (an interoperability rule of this app)."""
    if not isinstance(value, str):
        raise Refused("invalid_value")
    match = _TIME_RE.fullmatch(value.strip())
    if match is None:
        raise Refused("invalid_value")
    hour, minute = int(match[1]), int(match[2])
    if match[3] not in (None, "00") or minute > 59:
        raise Refused("invalid_value")
    if minute % 5:
        raise Refused("value_not_exact")
    if hour > 23:
        raise Refused("value_out_of_range", low="00:00", high="23:55")
    return hour * 100 + minute


def parse_value(desc: WriteDescriptor, value: Any) -> int:
    """The raw field for an engineering value, or ``Refused``. No rounding: booleans, NaN/infinity,
    exponents and values with more decimals than the step are refused."""
    if desc.kind is Kind.TIME:
        return parse_time(value)
    if desc.kind is Kind.BUTTON:
        if value != "press":
            raise Refused("invalid_value")
        return desc.max_raw
    if desc.kind is Kind.SELECT:
        if not isinstance(value, str):
            raise Refused("invalid_value")
        option = desc.option_by_slug(value)
        if option is None:
            raise Refused("invalid_value")
        if not option.writable:
            raise Refused("option_not_writable", 422, option=option.slug)
        return option.field
    if isinstance(value, bool) or value is None:
        raise Refused("invalid_value")
    number: Decimal
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise Refused("invalid_value")
        number = Decimal(repr(value))
    elif isinstance(value, str):
        text = value.strip()
        if not _DECIMAL_RE.match(text):
            raise Refused("invalid_value")
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise Refused("invalid_value") from None
    else:
        raise Refused("invalid_value")
    raw = number * desc.scale
    if raw != raw.to_integral_value():
        raise Refused("value_not_exact")
    if desc.paired:  # a switch over a wider field: 1 / 0 select the two field values
        if raw not in (0, 1):
            raise Refused("value_out_of_range", low=0, high=1)
        return desc.on_field if raw == 1 else desc.off_field
    return int(raw)


def check_range(desc: WriteDescriptor, field: int, cap: int | None) -> None:
    if desc.kind is Kind.BUTTON:
        if field != desc.max_raw:
            raise Refused("invalid_value")
        return
    if desc.kind is Kind.SELECT:
        option = desc.option_of(field)
        if option is None or not option.writable:
            raise Refused("invalid_value")
        return
    low, high = desc.limits(cap)
    if not low <= field <= high:
        shown = (low, high) if desc.scale > 0 else (high, low)
        raise Refused("value_out_of_range", low=desc.display(shown[0]), high=desc.display(shown[1]))
    if desc.step_raw > 1 and (field - low) % desc.step_raw:
        raise Refused("value_not_exact")
