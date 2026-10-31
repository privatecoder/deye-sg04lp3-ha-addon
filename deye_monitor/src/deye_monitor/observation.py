"""Runtime observation of entities (SPEC §9.4): data for the status view only.

Observations never change ``support_status``/``release`` and never switch an entity off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

ZERO_ONLY_AFTER = 30  # responses before "only zeros" is reported (still inconclusive)
_MAX_DISTINCT = 16


class ObservationState(StrEnum):
    OBSERVED_ACTIVE = "observed_active"
    ZERO_ONLY_INCONCLUSIVE = "zero_only_inconclusive"
    KNOWN_PLACEHOLDER = "known_placeholder"
    READ_FAILED = "read_failed"
    CAPABILITY_CONFLICT = "capability_conflict"
    NO_DATA = "no_data"


@dataclass(slots=True)
class Observation:
    responses: int = 0
    zeros: int = 0
    minimum: float | None = None
    maximum: float | None = None
    daylight_samples: int = 0
    daylight_nonzero: int = 0
    last_failed: bool = False
    placeholder: bool = False
    conflict: bool = False
    # Only entries that should not read 0 in operation can be "zero only" (``Entry.observes_zero``);
    # PV entries count their daylight samples only, 0 at night is normal.
    watch_zero: bool = False
    daylight_only: bool = False
    _distinct: set[float] = field(default_factory=set)

    @property
    def variability(self) -> int:
        """Number of distinct values seen (capped): 1 means constant."""
        return len(self._distinct)

    @property
    def zero_share(self) -> float:
        return self.zeros / self.responses if self.responses else 0.0

    def record(self, value: object, *, daylight: bool = False) -> None:
        if value is None:
            self.last_failed = True
            return
        self.last_failed = False
        self.responses += 1
        if isinstance(value, bool) or not isinstance(value, int | float):
            return
        number = float(value)
        zero = number == 0
        self.zeros += zero
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)
        if len(self._distinct) < _MAX_DISTINCT:
            self._distinct.add(number)
        if daylight:
            self.daylight_samples += 1
            self.daylight_nonzero += not zero

    @property
    def state(self) -> ObservationState:
        if self.placeholder:
            return ObservationState.KNOWN_PLACEHOLDER
        if self.conflict:
            return ObservationState.CAPABILITY_CONFLICT
        if self.last_failed:
            return ObservationState.READ_FAILED
        if not self.responses:
            return ObservationState.NO_DATA
        if self.watch_zero and self._zero_only():
            return ObservationState.ZERO_ONLY_INCONCLUSIVE
        return ObservationState.OBSERVED_ACTIVE

    def _zero_only(self) -> bool:
        if self.daylight_only:
            return self.daylight_samples >= ZERO_ONLY_AFTER and self.daylight_nonzero == 0
        return self.zeros == self.responses and self.responses >= ZERO_ONLY_AFTER
