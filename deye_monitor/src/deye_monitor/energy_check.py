"""Plausibility check of the energy resolution: the daily PV counter against the PV power integrated
over the same time. A hint only (SPEC §5): nothing here changes the resolution or any value.

The counter is read as a delta from the first reading after (re)start or a counter reset, and the
PV power is integrated from that same moment, so a restart in the middle of the day does not matter.
Power samples further apart than ``MAX_GAP_S`` are not bridged (the integral stays a lower bound
and the check simply waits for more data)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from deye_monitor.profiles.decode import Value

PV_POWER_KEYS: Final = ("pv1_power", "pv2_power", "pv3_power", "pv4_power")
PV_COUNTER_KEY: Final = "pv_energy_today"
MAX_GAP_S: Final = 300.0
MIN_INTEGRATED_KWH: Final = 2.0  # below this the sun is too weak or the data too short to judge
# A real factor error is a power of ten; losses and sampling move a correct plant by well under 2x.
LOW_RATIO: Final = 6.5
HIGH_RATIO: Final = 15.0

PLAUSIBLE: Final = "plausible"
TOO_HIGH: Final = "counter_too_high"  # the counter shows about 10x what the power adds up to
TOO_LOW: Final = "counter_too_low"
UNKNOWN: Final = "unknown"


def _number(value: Value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class EnergyPlausibility:
    def __init__(self) -> None:
        self._generation: int | None = None
        self._reset()

    def _reset(self) -> None:
        self._baseline: float | None = None  # counter reading (kWh) the integral starts at
        self._last_counter: float | None = None
        self._integrated_wh = 0.0
        self._last_power: tuple[float, float] | None = None  # (wall_ts, W)
        self._counter_kwh: float | None = None

    def feed(self, values: Mapping[str, Value], wall_ts: float, generation: int) -> None:
        """One published group: the PV power keys and/or the daily PV counter."""
        if generation != self._generation:  # a new energy series: the old comparison is void
            self._generation = generation
            self._reset()
        counter = _number(values.get(PV_COUNTER_KEY)) if PV_COUNTER_KEY in values else None
        if counter is not None:
            if self._baseline is None or (
                self._last_counter is not None and counter < self._last_counter
            ):  # first reading, or the daily counter restarted
                self._baseline = counter
                self._integrated_wh = 0.0
                self._last_power = None
            self._last_counter = counter
            self._counter_kwh = counter - self._baseline
        if any(k in values for k in PV_POWER_KEYS):
            watts = [_number(values.get(k)) for k in PV_POWER_KEYS if k in values]
            if any(w is None for w in watts):
                self._last_power = None
                return
            total = sum(w for w in watts if w is not None)
            if self._baseline is not None and self._last_power is not None:
                dt = wall_ts - self._last_power[0]
                if 0 < dt <= MAX_GAP_S:
                    self._integrated_wh += self._last_power[1] * dt / 3600.0
            self._last_power = (wall_ts, total)

    def view(self) -> dict[str, Any]:
        integrated = self._integrated_wh / 1000.0
        state, ratio = UNKNOWN, None
        if self._counter_kwh is not None and integrated >= MIN_INTEGRATED_KWH:
            ratio = self._counter_kwh / integrated
            if LOW_RATIO <= ratio <= HIGH_RATIO:
                state = TOO_HIGH
            elif 1 / HIGH_RATIO <= ratio <= 1 / LOW_RATIO:
                state = TOO_LOW
            else:
                state = PLAUSIBLE
        return {
            "state": state,
            "ratio": None if ratio is None else round(ratio, 2),
            "integrated_kwh": round(integrated, 3),
            "counter_kwh": None if self._counter_kwh is None else round(self._counter_kwh, 3),
        }
