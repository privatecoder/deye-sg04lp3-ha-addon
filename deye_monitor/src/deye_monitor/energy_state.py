"""Energy resolution state and statistics series (SPEC §5, DR2-02, N2-01).

The effective resolution (kWh per count) comes from R65 (``auto``) or the override and is only
confirmed for an *identified* device. A missing R65 never defaults; a change needs two
*consecutive* identical valid readings (a missing or invalid reading resets the confirmation).

A statistics series ends when the effective factor changes or when another physical inverter
(serial number) is bound; a simultaneous change migrates once. Reconnects, path changes and
re-checks of the same serial change nothing. A series is named ``<namespace>g<generation>``
(``energy_series``, regex ``^([a-z0-9]{8})?g[0-9]+$``): the namespace is empty normally and a random
8-character token after an explicit reset that had to give up the generation history.

Two files are evaluated TOGETHER: ``energy.json`` (the record in use) and
``energy-generations.json`` (per inverter: namespace, high-water mark and the reserved, not yet
committed transition). Only "both missing" is a first installation. A damaged file, a record whose
namespace differs from the history or a record above the high-water mark blocks energy publication
(``energy_store_corrupt`` / ``energy_history_inconsistent`` / ``energy_history_missing``)
until an explicit ``reset``. The next
generation is always ``max(high-water, record) + 1``; a record that is older than the high-water
mark (restored backup) therefore never reuses a generation, not even for the same serial and the
same factor: its series ended, so each release reserves a new one (a matching crash reservation
is continued instead). The high-water mark and the reservation
are written BEFORE the record, so a crash in between continues with the same generation. There is no
time-based substitute: after a lost history ``reset`` starts a new random namespace at ``g0``.
"""

from __future__ import annotations

import copy
import re
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from deye_monitor.config import EnergyResolution
from deye_monitor.identity import valid_serial
from deye_monitor.profiles.energy import effective_resolution
from deye_monitor.store import JsonStore

CONFIRMATIONS_FOR_CHANGE = 2
PROBLEM_CORRUPT = "energy_store_corrupt"
PROBLEM_INCONSISTENT = "energy_history_inconsistent"
PROBLEM_HISTORY_MISSING = "energy_history_missing"
PROBLEM_UNWRITABLE = "energy_store_unwritable"
_FACTORS = (0.1, 1.0)
_SOURCES = ("r65", "override")
_NAMESPACE_RE = re.compile(r"^([a-z0-9]{8})?$")
SERIES_RE = re.compile(r"^([a-z0-9]{8})?g[0-9]+$")
_TOKEN_ALPHABET = string.ascii_lowercase + string.digits


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_record(rec: object) -> bool:
    if not isinstance(rec, dict) or set(rec) - {
        "effective_resolution",
        "source",
        "generation",
        "serial",
        "namespace",
    }:
        return False
    serial = rec.get("serial")
    namespace = rec.get("namespace", "")
    return (
        rec.get("effective_resolution") in _FACTORS
        and not isinstance(rec.get("effective_resolution"), bool)
        and rec.get("source") in _SOURCES
        and _is_int(rec.get("generation"))
        and rec["generation"] >= 0
        and (serial is None or valid_serial(serial))
        and isinstance(namespace, str)
        and bool(_NAMESPACE_RE.match(namespace))
    )


def valid_energy_document(doc: dict[str, Any]) -> bool:
    return all(isinstance(k, str) and _valid_record(v) for k, v in doc.items())


def _valid_history_entry(entry: object) -> bool:
    if isinstance(entry, int) and not isinstance(entry, bool):  # first layout: high-water only
        return entry >= 0
    if not isinstance(entry, dict) or set(entry) != {"namespace", "high_water", "pending"}:
        return False
    return (
        isinstance(entry["namespace"], str)
        and bool(_NAMESPACE_RE.match(entry["namespace"]))
        and _is_int(entry["high_water"])
        and entry["high_water"] >= -1
        and (entry["pending"] is None or _valid_record(entry["pending"]))
    )


def valid_generation_document(doc: dict[str, Any]) -> bool:
    return all(isinstance(k, str) and _valid_history_entry(v) for k, v in doc.items())


def random_namespace() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(8))


@dataclass(frozen=True, slots=True)
class _History:
    namespace: str
    high_water: int
    pending: dict[str, Any] | None


def _history(entry: object) -> _History:
    if isinstance(entry, int):
        return _History("", entry, None)
    assert isinstance(entry, dict)
    return _History(entry["namespace"], entry["high_water"], entry["pending"])


def series_name(namespace: str, generation: int) -> str:
    return f"{namespace}g{generation}"


@dataclass(frozen=True, slots=True)
class EnergyEvent:
    kind: str  # "initialised" | "generation_changed" | "change_pending"
    resolution: float | None
    generation: int
    source: str


class EnergyTracker:
    def __init__(
        self,
        store: JsonStore,
        inverter_id: str,
        override: EnergyResolution,
        *,
        high_water: JsonStore | None = None,
        namespace_source: Callable[[], str] = random_namespace,
    ) -> None:
        self._store = store
        self._hw = high_water
        self._id = inverter_id
        self._override = override
        self._namespace_source = namespace_source
        self._confirmed: float | None = None
        self._pending: tuple[float, int] | None = None
        self.blocked: str | None = self._evaluate()

    def _evaluate(self) -> str | None:
        """Main record and history are judged together."""
        if self._store.corrupt or (self._hw is not None and self._hw.corrupt):
            return PROBLEM_CORRUPT
        rec, hist = self._record(), self._hist()
        if rec is not None and hist is None and self._hw is not None:
            # the history is always written before the record: a record without history means the
            # history was lost; only "both missing" is a fresh installation (SPEC §5)
            return PROBLEM_HISTORY_MISSING
        if (
            rec is not None
            and hist is not None
            and (rec.get("namespace", "") != hist.namespace or rec["generation"] > hist.high_water)
        ):
            return PROBLEM_INCONSISTENT
        return None

    # -- read side --------------------------------------------------------------------------

    @property
    def resolution(self) -> float | None:
        """kWh per count once confirmed for the current identified device, else ``None``."""
        return None if self.blocked else self._confirmed

    @property
    def pending(self) -> bool:
        return self._pending is not None

    @property
    def generation(self) -> int:
        rec = self._record()
        return int(rec["generation"]) if rec else 0

    @property
    def namespace(self) -> str:
        rec, hist = self._record(), self._hist()
        if rec is not None:
            return str(rec.get("namespace", ""))
        return hist.namespace if hist else ""

    @property
    def series(self) -> str:
        """The opaque ``energy_series`` of the record in use; before the first record the series
        it will get (the reserved one, else the next above the high-water mark)."""
        rec, hist = self._record(), self._hist()
        if rec is not None:
            return series_name(str(rec.get("namespace", "")), int(rec["generation"]))
        if hist is None:
            return series_name("", 0)
        reserved = hist.pending["generation"] if hist.pending else hist.high_water + 1
        return series_name(hist.namespace, int(reserved))

    def snapshot(self) -> dict[str, object]:
        rec = self._record() or {}
        return {
            "effective_resolution": self.resolution,
            "persisted_resolution": rec.get("effective_resolution"),
            "source": rec.get("source"),
            "serial_epoch": rec.get("serial") is not None,
            "generation": self.generation,
            "series": self.series,
            "pending_change": self._pending[0] if self._pending else None,
            "blocked": self.blocked,
        }

    def _record(self) -> dict[str, Any] | None:
        rec = self._store.data.get(self._id)
        return rec if isinstance(rec, dict) else None

    def _hist(self) -> _History | None:
        if self._hw is None or self._id not in self._hw.data:
            return None
        return _history(self._hw.data[self._id])

    # -- state changes ----------------------------------------------------------------------

    def invalidate(self) -> None:
        """New identity/path/rebind: the factor must be confirmed again for this device."""
        self._confirmed = None
        self._pending = None

    def observe(self, r65: int | None, serial: str | None = None) -> list[EnergyEvent]:
        """Feed the latest R65 reading (``None`` = not read / failed, never treated as 0) for the
        identified device with ``serial``."""
        if self.blocked:
            return []
        candidate = effective_resolution(self._override, r65)
        if candidate is None:
            self._pending = None  # a gap breaks the *consecutive* confirmation
            return []
        source = "override" if self._override is not EnergyResolution.AUTO else "r65"
        rec = self._record()
        changed = rec is None or rec["effective_resolution"] != candidate or self._ended(rec)
        new_device = (
            rec is not None and serial is not None and rec.get("serial") not in (None, serial)
        )
        if not changed and not new_device:
            return self._commit(candidate, source, serial)
        if source == "override":
            return self._commit(candidate, source, serial)
        # auto mode: first record of a fresh install is taken at once, a change is confirmed
        if rec is None and self._hist() is None:
            return self._commit(candidate, source, serial)
        count = self._pending[1] + 1 if self._pending and self._pending[0] == candidate else 1
        self._pending = (candidate, count)
        self._confirmed = None
        if count < CONFIRMATIONS_FOR_CHANGE:
            return [EnergyEvent("change_pending", candidate, self.generation, source)]
        return self._commit(candidate, source, serial)

    def _ended(self, rec: dict[str, Any] | None) -> bool:
        """The record is not the newest series: the history knows a higher generation (restored
        backup). Its series is over and must never be released again, whatever its factor is."""
        hist = self._hist()
        return rec is not None and hist is not None and rec["generation"] < hist.high_water

    def _next_generation(self, rec: dict[str, Any] | None) -> int:
        """Above everything ever used: the high-water mark AND the record in use."""
        hist = self._hist()
        known = max(hist.high_water if hist else -1, int(rec["generation"]) if rec else -1)
        return known + 1

    def _reserved(
        self, rec: dict[str, Any] | None, candidate: float, serial: str | None
    ) -> dict[str, Any] | None:
        """A transition that was reserved in the history but did not reach the record (crash):
        the same change continues with the SAME generation."""
        hist = self._hist()
        if hist is None or hist.pending is None:
            return None
        pend = hist.pending
        newer = pend["generation"] > (int(rec["generation"]) if rec else -1)
        if newer and pend["effective_resolution"] == candidate and pend.get("serial") == serial:
            return pend
        return None

    def _commit(self, candidate: float, source: str, serial: str | None) -> list[EnergyEvent]:
        rec = self._record()
        events: list[EnergyEvent] = []
        keeps_series = (
            rec is not None
            and not self._ended(rec)
            and rec["effective_resolution"] == candidate
            and (serial is None or rec.get("serial") in (None, serial))
        )
        if keeps_series:
            assert rec is not None
            if rec["source"] != source or (serial and rec.get("serial") is None):
                updated = dict(rec, source=source, serial=serial or rec.get("serial"))
                if not self._write(updated, reserve=False):
                    return []
        else:
            first = rec is None
            reserved = self._reserved(rec, candidate, serial)
            generation = reserved["generation"] if reserved else self._next_generation(rec)
            hist = self._hist()
            namespace = hist.namespace if hist else str(rec.get("namespace", "") if rec else "")
            new = {
                "effective_resolution": candidate,
                "source": source,
                "generation": generation,
                "serial": serial,
            }
            if namespace:  # the default namespace is not written
                new["namespace"] = namespace
            if not self._write(new, reserve=True):
                return []
            events.append(
                EnergyEvent(
                    "initialised" if first and generation == 0 else "generation_changed",
                    candidate,
                    generation,
                    source,
                )
            )
        self._confirmed = candidate
        self._pending = None
        return events

    def _write(self, record: dict[str, Any], *, reserve: bool) -> bool:
        """Persist: history (high-water mark + reservation) first, then the record; the state only
        changes afterwards."""
        try:
            if self._hw is not None and reserve:
                hw = copy.deepcopy(self._hw.data)
                hist = self._hist()
                high = max(hist.high_water if hist else -1, int(record["generation"]))
                hw[self._id] = {
                    "namespace": record.get("namespace", ""),
                    "high_water": high,
                    "pending": dict(record),
                }
                self._hw.commit(hw)
            doc = copy.deepcopy(self._store.data)
            doc[self._id] = record
            self._store.commit(doc)
        except OSError:
            self.blocked = PROBLEM_UNWRITABLE
            self._confirmed = None
            return False
        return True

    def reset(self) -> str:
        """Explicit user action after a damaged or inconsistent store; returns the series the next
        record will get.

        History intact: the same namespace continues above its high-water mark. History lost
        (damaged, or no entry for this inverter): a NEW random namespace starting at ``g0``. The
        history is written before the record store, so a repeated reset after a crash continues in
        the same namespace instead of drawing another token."""
        hist_ok = self._hw is not None and not self._hw.corrupt
        entry = self._hist() if hist_ok else None
        if entry is not None:
            namespace, high = entry.namespace, entry.high_water
        else:
            namespace = self._namespace_source()
            while not _NAMESPACE_RE.match(namespace) or namespace in ("", self.namespace):
                namespace = self._namespace_source()
            high = -1
        if self._hw is not None:
            hw = copy.deepcopy(self._hw.data) if hist_ok else {}
            hw[self._id] = {"namespace": namespace, "high_water": high, "pending": None}
            self._hw.reset(hw) if self._hw.corrupt else self._hw.commit(hw)
        docs = {k: v for k, v in self._store.data.items() if k != self._id and _valid_record(v)}
        self._store.reset(docs) if self._store.corrupt else self._store.commit(docs)
        self.blocked = None
        self.invalidate()
        return series_name(namespace, high + 1)
