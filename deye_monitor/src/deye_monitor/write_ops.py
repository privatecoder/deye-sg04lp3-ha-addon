"""The write whitelist (SPEC §10): 38 reviewed descriptors and their pure encoding rules.

No device I/O and no state. A descriptor names ONE field of ONE register, how an engineering
value maps onto it, the accepted range, the preconditions and the risk text shown to the owner.
Every wire word is an unsigned 16-bit value; a value that does not encode exactly into an integer
is refused (no rounding, no clamping). The register/mask table must agree with the one in the
lowest byte guard (``protocol.guard.WRITABLE_MASKS``).

The preconditions are declarative (``requires_r111``, ``needs_smart_load``, ``tou``, ``below`` /
``above``, ``current_cap`` / ``power_cap``, ``whole_word_bool``); ``writes.py`` evaluates them
against fresh reads at execution, whichever transport asked for the write.

Not offered (never reachable): R141/R142 work mode, the slot voltage words R160-R165, slot bit 1
(generator charge), the clock and everything in the deny list of the guard.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

# Maximum battery charge/discharge current per model, from the vendor datasheet
# "Datenblatt SUN-5-12k-SG04LP3.pdf" (knowledge base, Specs/): 5K 120 A, 6K 150 A, 8K 190 A,
# 10K 210 A, 12K 240 A. It replaces the generic 0-185 A of the V105.8 document for R108, R109, R125
# and R128. The model comes from the rated power (R20-R21); an unknown model locks the operation.
CURRENT_CAP_BY_RATED_W: Final[dict[int, int]] = {
    5000: 120,
    6000: 150,
    8000: 190,
    10000: 210,
    12000: 240,
}
CURRENT_CAP_SOURCE: Final = (
    "Datenblatt SUN-5-12k-SG04LP3.pdf (vendor datasheet): max. charge/discharge current "
    "5K 120 A, 6K 150 A, 8K 190 A, 10K 210 A, 12K 240 A"
)

OBSERVE_DELAY_S: Final = 1.0
LATE_OBSERVE_DELAY_S: Final = 30.0  # second, later peer observation (owner field: ~18 s)
PACING_S: Final = 5.0
PERMIT_S: Final = 30.0

# R111 battery control mode (SOC thresholds are meaningful in mode 1, voltage thresholds in mode 0)
R111_VOLTAGE: Final = 0
R111_SOC: Final = 1
R146_SPAIN_BIT: Final = 0x0100  # ToU "Spain mode": a different firmware path, not offered
TOU_SLOTS: Final = 6

_DECIMAL_RE = re.compile(r"^-?[0-9]{1,6}(\.[0-9]{1,6})?$")
_TIME_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?")


class Kind(StrEnum):
    SCALAR = "scalar"
    BIT = "bit"
    TIME = "time"  # raw HHMM (hour x 100 + minute), minutes in steps of 5


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

    @property
    def field_mask(self) -> int:
        return self.mask >> self.shift

    @property
    def related(self) -> tuple[str, ...]:
        return self.below + self.above

    @property
    def model_limited(self) -> bool:
        return self.current_cap or self.power_cap

    def field_of(self, word: int) -> int:
        return (word & self.mask) >> self.shift

    def encode(self, fresh: int, field: int) -> int:
        """Read-modify-write: ``fresh`` with only the bits of this field replaced. A whole-word
        switch (R130) is exactly 0 or 1 and refuses a fresh word that is anything else."""
        if not 0 <= field <= self.field_mask:
            raise Refused("value_out_of_range")
        if self.whole_word_bool:
            if fresh not in (0, 1):
                raise Refused("unexpected_bits", 409)
            return field
        new = (fresh & (~self.mask & 0xFFFF)) | (field << self.shift)
        if (new ^ fresh) & (~self.mask & 0xFFFF):  # defence in depth, cannot happen
            raise Refused("foreign_bits_changed")
        return new

    def display(self, field: int) -> int | float | str:
        """Engineering value of a raw field (volts as a float with two decimals, a time as
        ``HH:MM``)."""
        if self.kind is Kind.TIME:
            return f"{field // 100:02d}:{field % 100:02d}"
        return field if self.scale == 1 else round(field / self.scale, 2)

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
            "min": self.display(self.min_raw),
            "max": self.display(self.max_raw),
            "step": 5 if self.kind is Kind.TIME else (1 / self.scale if self.scale != 1 else 1),
            "name": self.name.as_dict(),
            "risk": self.risk.as_dict(),
            "source": self.source,
            "group": self.group,
            "current_cap": self.current_cap,
            "power_cap": self.power_cap,
            "needs_smart_load": self.needs_smart_load,
            "requires_r111": self.requires_r111,
            "tou": self.tou,
            "whole_word_bool": self.whole_word_bool,
            "below": list(self.below),
            "above": list(self.above),
        }


_V = "V105.8-20251022 registers.json (R/W)"
_E = "write layer v2 (Codex design review, settings catalogue)"
_SOC_RANGE = (0, 100)
_VOLT_RANGE = (3800, 6300)

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
        name=name,
        risk=risk,
        source=_V,
        group="smart_load",
        needs_battery=True,
        needs_smart_load=True,
        requires_r111=R111_VOLTAGE if voltage else R111_SOC,
        below=(companion,) if off else (),
        above=() if off else (companion,),
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


_OFF_RISK = Text(
    "Nur mit als Smart Load konfiguriertem GEN-Anschluss und dem passenden Modus (R111: "
    "Spannung = 0, SOC = 1). Kann diese Last abschalten. Der OFF-Wert muss unter dem ON-Wert "
    "liegen (Regel dieser App, kein bewiesener Firmware-Check).",
    "Only with the GEN port already configured as Smart Load and the matching mode (R111: "
    "voltage = 0, SOC = 1). May disconnect that load. OFF must stay below ON (a rule of "
    "this app, not a proven firmware check).",
)
_ON_RISK = Text(
    "Nur mit als Smart Load konfiguriertem GEN-Anschluss und dem passenden Modus (R111: "
    "Spannung = 0, SOC = 1). Kann diese Last einschalten. Der ON-Wert muss über dem OFF-Wert "
    "liegen (Regel dieser App, kein bewiesener Firmware-Check).",
    "Only with the GEN port already configured as Smart Load and the matching mode (R111: "
    "voltage = 0, SOC = 1). May energize that load. ON must stay above OFF (a rule of this "
    "app, not a proven firmware check).",
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
                f"Zeitplan Slot {n}: Startzeit (R{start})", f"ToU slot {n}: start time (R{start})"
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
                f"Zeitplan Slot {n}: Entladeleistung (R{power})",
                f"ToU slot {n}: discharge power (R{power})",
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
                f"Zeitplan Slot {n}: Ziel-SOC (R{soc})", f"ToU slot {n}: target SOC (R{soc})"
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
                f"Zeitplan Slot {n}: Netzladen erlauben (R{flags} Bit 0)",
                f"ToU slot {n}: allow GRID charging (R{flags} bit 0)",
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
            name=Text("Zeitplan (ToU) aktivieren (R146 Bit 0)", "ToU schedule enable (R146 bit 0)"),
            risk=Text(
                "Schaltet die zeitgesteuerte Batteriesteuerung ein oder aus. Die Wochentagsbits "
                "und alle anderen Bits bleiben unverändert. Die Firmware kann Bit 0 selbst setzen "
                "(etwa bei Lastspitzenkappung); der Schalter zeigt nur die gelesene Einstellung. "
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
    ]
    for n in range(1, TOU_SLOTS + 1):
        out.extend(_slot_descriptors(n))
    return tuple(out)


DESCRIPTORS: Final[tuple[WriteDescriptor, ...]] = _build()
BY_KEY: Final[dict[str, WriteDescriptor]] = {d.key: d for d in DESCRIPTORS}
OPERATION_KEYS: Final[tuple[str, ...]] = tuple(d.key for d in DESCRIPTORS)
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
    """Maximum current in A for the model in R20/R21; an unknown model (``None``) locks the
    current operations."""
    watts = rated_watts(r20, r21)
    return None if watts is None else CURRENT_CAP_BY_RATED_W[watts]


def model_limit(desc: WriteDescriptor, r20: int, r21: int) -> int | None:
    """The model-dependent maximum of ``desc`` (A or W), ``None`` if unknown or not applicable."""
    if desc.current_cap:
        return model_cap(r20, r21)
    if desc.power_cap:
        return rated_watts(r20, r21)
    return None


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
    return int(raw)


def check_range(desc: WriteDescriptor, field: int, cap: int | None) -> None:
    low, high = desc.limits(cap)
    if not low <= field <= high:
        raise Refused("value_out_of_range", low=desc.display(low), high=desc.display(high))
