"""Plausibility check of the energy resolution: the daily PV counter against the PV power integrated
over the same time. A hint only (SPEC §5): nothing here changes the resolution or any value.

A verdict needs equal, fresh time windows and one common measuring boundary:

* the counter delta is only compared over a window between two consecutive counter readings in
  which the power integral is complete (no gap over ``MAX_GAP_S``, no unknown value); a window
  that is not covered is dropped, never counted on one side only;
* the counter and the power must both be fresh when the verdict is read (a failed energy or power
  read lets the verdict expire);
* the caller names the power keys that together make up the counted PV sources (``power_keys``);
  every one must be present and numeric in every power sample. ``None`` means the boundary is not
  proven (AC-coupled generator port, no active MPPT): the verdict stays ``unknown``.

A change of the energy series, of its opaque identity or of the power keys starts over."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from deye_monitor.profiles.decode import Value

PV_POWER_KEYS: Final = ("pv1_power", "pv2_power")
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
    def __init__(self, counter_interval_s: float = 60.0) -> None:
        # a counter reading older than this is stale (the energy group failed repeatedly)
        self._counter_max_age_s = max(3.0 * counter_interval_s, 120.0)
        self._identity: tuple[int, str | None, frozenset[str] | None] | None = None
        self._reset()

    def _reset(self) -> None:
        self._checkpoint: tuple[float, float, float] | None = None  # (wall_ts, kWh, integral Wh)
        self._integral_wh = 0.0  # running integral of the (complete) power samples
        self._last_power: tuple[float, float] | None = None  # (wall_ts, W)
        self._broken = False  # the power integral has a hole since the checkpoint
        self._matched_counter_kwh = 0.0
        self._matched_integral_wh = 0.0
        self._last_counter: tuple[float, float] | None = None  # (wall_ts, kWh)

    def _integral_at(self, wall_ts: float) -> float | None:
        """The power integral at ``wall_ts`` (the last power held), or None when not covered."""
        if self._last_power is None:
            return None
        dt = wall_ts - self._last_power[0]
        if not 0 <= dt <= MAX_GAP_S:
            return None
        return self._integral_wh + self._last_power[1] * dt / 3600.0

    def feed(
        self,
        values: Mapping[str, Value],
        wall_ts: float,
        generation: int,
        series: str | None = None,
        power_keys: frozenset[str] | None = None,
    ) -> None:
        """One published group: the PV power keys and/or the daily PV counter."""
        identity = (generation, series, power_keys)
        if (
            identity != self._identity
        ):  # a new energy series or boundary: the old comparison is void
            self._identity = identity
            self._reset()
        if any(k in values for k in PV_POWER_KEYS):
            self._feed_power(values, wall_ts, power_keys)
        if PV_COUNTER_KEY in values:
            self._feed_counter(_number(values[PV_COUNTER_KEY]), wall_ts)

    def _feed_power(
        self, values: Mapping[str, Value], wall_ts: float, power_keys: frozenset[str] | None
    ) -> None:
        watts = None
        if power_keys:
            parts = [_number(values.get(k)) if k in values else None for k in sorted(power_keys)]
            if all(w is not None and w >= 0 for w in parts):
                watts = sum(w for w in parts if w is not None)
        if watts is None:  # a counted source is missing or unknown: the integral has a hole
            self._last_power = None
            self._broken = True
            return
        if self._last_power is not None:
            dt = wall_ts - self._last_power[0]
            if 0 < dt <= MAX_GAP_S:
                self._integral_wh += self._last_power[1] * dt / 3600.0
            else:
                self._broken = True
                if dt <= 0:
                    return
        self._last_power = (wall_ts, watts)

    def _feed_counter(self, counter: float | None, wall_ts: float) -> None:
        if counter is None:
            self._checkpoint = None
            self._last_counter = None
            return
        previous = self._last_counter
        self._last_counter = (wall_ts, counter)
        integral = self._integral_at(wall_ts)
        if previous is not None and counter < previous[1]:  # the daily counter restarted
            self._matched_counter_kwh = 0.0
            self._matched_integral_wh = 0.0
            self._checkpoint = None
        if integral is None:
            self._checkpoint = None
            self._broken = True
            return
        if self._checkpoint is not None and not self._broken and wall_ts > self._checkpoint[0]:
            self._matched_counter_kwh += counter - self._checkpoint[1]
            self._matched_integral_wh += integral - self._checkpoint[2]
        self._checkpoint = (wall_ts, counter, integral)
        self._broken = False

    def view(self, now: float | None = None) -> dict[str, Any]:
        """``now`` (wall time) lets the verdict expire when the counter or the power is stale."""
        integrated = self._matched_integral_wh / 1000.0
        counter = self._matched_counter_kwh
        fresh = now is None or (
            self._last_counter is not None
            and now - self._last_counter[0] <= self._counter_max_age_s
            and self._last_power is not None
            and now - self._last_power[0] <= MAX_GAP_S
        )
        state, ratio = UNKNOWN, None
        if (
            fresh
            and self._identity is not None
            and self._identity[2]
            and integrated >= MIN_INTEGRATED_KWH
        ):
            ratio = counter / integrated
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
            "counter_kwh": round(counter, 3),
        }
