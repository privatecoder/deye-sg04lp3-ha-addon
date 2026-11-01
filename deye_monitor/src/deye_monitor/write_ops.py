"""The v1 write whitelist (SPEC §10): nine reviewed descriptors and their pure encoding rules.

No device I/O and no state. A descriptor names ONE field of ONE register, how an engineering
value maps onto it, the accepted range, the preconditions and the risk text shown to the owner.
Every wire word is an unsigned 16-bit value; a value that does not encode exactly into an integer
is refused (no rounding). The register/mask table must agree with the one in the lowest byte
guard (``protocol.guard.WRITABLE_MASKS``).

Excluded in v1 (never reachable): R115/R116/R117 SOC protection thresholds, R141/R142 work mode,
time-of-use R146/R148-177, the clock and everything in the deny list of the guard.
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
PACING_S: Final = 5.0
PERMIT_S: Final = 30.0

_DECIMAL_RE = re.compile(r"^-?[0-9]{1,6}(\.[0-9]{1,6})?$")


class Kind(StrEnum):
    SCALAR = "scalar"
    BIT = "bit"


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
    max_raw: int  # documented maximum; for current operations the model cap replaces it
    name: Text
    risk: Text
    source: str
    current_cap: bool = False  # the model cap of R108/R109/R125/R128 replaces ``max_raw``
    needs_battery: bool = False
    needs_smart_load: bool = False  # the GEN port must already be configured as Smart Load
    companion: str | None = None  # key of the paired Smart Load threshold
    ordering: str | None = None  # "below": must stay below the companion, "above": above it

    @property
    def field_mask(self) -> int:
        return self.mask >> self.shift

    def field_of(self, word: int) -> int:
        return (word & self.mask) >> self.shift

    def encode(self, fresh: int, field: int) -> int:
        """Read-modify-write: ``fresh`` with only the bits of this field replaced."""
        if not 0 <= field <= self.field_mask:
            raise Refused("value_out_of_range")
        new = (fresh & (~self.mask & 0xFFFF)) | (field << self.shift)
        if (new ^ fresh) & (~self.mask & 0xFFFF):  # defence in depth, cannot happen
            raise Refused("foreign_bits_changed")
        return new

    def display(self, field: int) -> int | float:
        """Engineering value of a raw field (volts as a float with two decimals)."""
        return field if self.scale == 1 else round(field / self.scale, 2)

    def limits(self, cap: int | None) -> tuple[int, int]:
        """Raw range accepted now. A current operation needs a known model cap, and that cap
        REPLACES the generic maximum of the document (``max_raw`` is only that document value)."""
        if self.current_cap:
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
            "step": 1 / self.scale if self.scale != 1 else 1,
            "name": self.name.as_dict(),
            "risk": self.risk.as_dict(),
            "source": self.source,
            "current_cap": self.current_cap,
            "needs_smart_load": self.needs_smart_load,
            "companion": self.companion,
            "ordering": self.ordering,
        }


_V = "V105.8-20251022 registers.json (R/W)"

_CURRENT_RISK = Text(
    "Begrenzt den Batteriestrom. 0 kann das Laden vollständig stoppen; die Vorgaben von "
    "BMS und Betriebszustand gelten weiterhin.",
    "Limits the battery current. 0 can stop charging completely; the limits of the BMS and the "
    "operating state still apply.",
)
_SOC_RANGE = (0, 100)
_VOLT_RANGE = (3800, 6300)


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
        needs_smart_load=True,
        companion=companion,
        ordering="below" if off else "above",
    )


_OFF_RISK = Text(
    "Nur mit als Smart Load konfiguriertem GEN-Anschluss und dem passenden Modus (Spannung oder "
    "SOC, Prüfung am Display). Kann diese Last abschalten. Der OFF-Wert muss unter dem ON-Wert "
    "liegen (Regel dieser App, kein bewiesener Firmware-Check).",
    "Only with the GEN port already configured as Smart Load and the matching mode (voltage or "
    "SOC, check at the display). May disconnect that load. OFF must stay below ON (a rule of "
    "this app, not a proven firmware check).",
)
_ON_RISK = Text(
    "Nur mit als Smart Load konfiguriertem GEN-Anschluss und dem passenden Modus (Spannung oder "
    "SOC, Prüfung am Display). Kann diese Last einschalten. Der ON-Wert muss über dem OFF-Wert "
    "liegen (Regel dieser App, kein bewiesener Firmware-Check).",
    "Only with the GEN port already configured as Smart Load and the matching mode (voltage or "
    "SOC, check at the display). May energize that load. ON must stay above OFF (a rule of this "
    "app, not a proven firmware check).",
)

DESCRIPTORS: Final[tuple[WriteDescriptor, ...]] = (
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
        name=Text("Maximaler Batterie-Ladestrom (R108)", "Maximum battery charge current (R108)"),
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
            "Inselbetrieb schwächen. Nicht für den ersten Test.",
            "Limits the battery discharge current. A low value can weaken the supply in island "
            "operation. Not for the first test.",
        ),
        source=f"{_V}; cap: {CURRENT_CAP_SOURCE}",
        current_cap=True,
        needs_battery=True,
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
            "Only for a verified generator charging configuration. It is not established that a "
            "generator on the GRID port uses this field.",
        ),
        source=f"{_V}; cap: {CURRENT_CAP_SOURCE}",
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
        name=Text("Netzladen aktivieren (R130 Bit 0)", "GRID charge enable (R130 bit 0)"),
        risk=Text(
            "Schaltet das Laden der Batterie aus dem Netz. Alle anderen Bits des Registers bleiben "
            "unverändert.",
            "Switches charging the battery from the grid. All other bits of the register stay "
            "unchanged.",
        ),
        source=_V,
        needs_battery=True,
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
)

BY_KEY: Final[dict[str, WriteDescriptor]] = {d.key: d for d in DESCRIPTORS}
OPERATION_KEYS: Final[tuple[str, ...]] = tuple(d.key for d in DESCRIPTORS)
HARDWARE = "sg04lp3_eu"  # the descriptors are released for this hardware profile only


def model_cap(r20: int, r21: int) -> int | None:
    """Maximum current in A for the model reported in R20 (low word) and R21 (high word); the
    rated power is the 32-bit value in 0.1 W. Anything that is not exactly 5/6/8/10/12 kW is an
    unknown model (``None``), which locks the current operations."""
    raw = (r21 << 16) | r20
    if raw % 10:
        return None
    return CURRENT_CAP_BY_RATED_W.get(raw // 10)


def parse_value(desc: WriteDescriptor, value: Any) -> int:
    """The raw field for an engineering value, or ``Refused``. No rounding: booleans, NaN/infinity,
    exponents and values with more decimals than the step are refused."""
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
