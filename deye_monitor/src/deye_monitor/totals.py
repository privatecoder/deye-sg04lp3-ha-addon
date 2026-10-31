"""Plant totals over the member inverters (SPEC §4 Nachtrag, release 0.2.7).

Pure logic, no I/O except the small series store. The publisher feeds every accepted state message
into ``PlantTotals``; the totals are computed from the newest value of each member:

* a member value that is missing, ``None`` or older than three poll intervals of its group makes
  the total unknown (``None``): a partial sum is never shown as a total;
* PV power and PV energy are the exception for a member without PV inputs (nothing selected):
  that member adds 0;
* energy totals are sums of the members' ``total_increasing`` counters. A plant statistics series
  (``g<n>`` like the inverter series) ends when the membership changes or when any member starts a
  new series of its own, so the total never drops or jumps inside a series;
* battery state (SOC, BMS) comes from the parallel master only, never averaged.

Battery power of one inverter is battery voltage x battery current; the current is negative while
charging (R591), so positive power is discharging.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from deye_monitor.config import PollConfig
from deye_monitor.profiles.decode import Value
from deye_monitor.profiles.schema import Profile
from deye_monitor.store import JsonStore, LoadStatus

log = logging.getLogger(__name__)

PV_POWER_KEYS: Final = ("pv1_power", "pv2_power", "pv3_power", "pv4_power")
STALE_FACTOR: Final = 3  # a value is stale after this many poll intervals (as expire_after)
SERIES_FILE: Final = "plant-totals.json"
_SERIES_KEY = re.compile(r"^plant_[a-z_]+_energy_total_(?P<series>g(?P<n>\d+))$")


@dataclass(frozen=True, slots=True)
class Metric:
    """One plant sensor. ``kind`` selects the computation, ``keys`` are the profile entries a
    member must deliver (used for the staleness/expire window)."""

    key: str
    name: str
    kind: str  # power | energy | daily | master
    keys: tuple[str, ...]
    unit: str
    device_class: str | None = None
    state_class: str | None = None
    precision: int | None = None
    optional: bool = False  # a member without these entries adds 0 (PV)


METRICS: Final = (
    Metric(
        "plant_pv_power",
        "Plant PV power",
        "power",
        PV_POWER_KEYS,
        "W",
        "power",
        "measurement",
        0,
        optional=True,
    ),
    Metric(
        "plant_battery_power",
        "Plant battery power",
        "power",
        ("battery_voltage", "battery_current"),
        "W",
        "power",
        "measurement",
        0,
    ),
    Metric(
        "plant_load_power",
        "Plant load power",
        "power",
        ("load_power_total",),
        "W",
        "power",
        "measurement",
        0,
    ),
    Metric(
        "plant_inverter_power",
        "Plant inverter output power",
        "power",
        ("inverter_power_total",),
        "W",
        "power",
        "measurement",
        0,
    ),
    Metric(
        "plant_pv_energy_total",
        "Plant PV energy",
        "energy",
        ("pv_energy_total",),
        "kWh",
        "energy",
        "total_increasing",
        optional=True,
    ),
    Metric(
        "plant_battery_charge_energy_total",
        "Plant battery charge",
        "energy",
        ("battery_energy_charge_total",),
        "kWh",
        "energy",
        "total_increasing",
    ),
    Metric(
        "plant_battery_discharge_energy_total",
        "Plant battery discharge",
        "energy",
        ("battery_energy_discharge_total",),
        "kWh",
        "energy",
        "total_increasing",
    ),
    Metric(
        "plant_load_energy_total",
        "Plant load energy",
        "energy",
        ("load_energy_total",),
        "kWh",
        "energy",
        "total_increasing",
    ),
    # measurement only: the members' day counters reset at slightly different moments
    Metric(
        "plant_pv_energy_today",
        "Plant PV energy today",
        "daily",
        ("pv_energy_today",),
        "kWh",
        None,
        "measurement",
        1,
        optional=True,
    ),
    Metric(
        "plant_battery_charge_energy_today",
        "Plant battery charge today",
        "daily",
        ("battery_energy_charge_today",),
        "kWh",
        None,
        "measurement",
        1,
    ),
    Metric(
        "plant_battery_discharge_energy_today",
        "Plant battery discharge today",
        "daily",
        ("battery_energy_discharge_today",),
        "kWh",
        None,
        "measurement",
        1,
    ),
    Metric(
        "plant_load_energy_today",
        "Plant load energy today",
        "daily",
        ("load_energy_today",),
        "kWh",
        None,
        "measurement",
        1,
    ),
    Metric(
        "plant_battery_soc",
        "Plant battery SOC",
        "master",
        ("battery_soc",),
        "%",
        "battery",
        "measurement",
        0,
    ),
    Metric(
        "plant_bms_soc", "Plant BMS SOC", "master", ("bms_soc",), "%", "battery", "measurement", 0
    ),
    Metric(
        "plant_bms_voltage",
        "Plant BMS voltage",
        "master",
        ("bms_voltage",),
        "V",
        "voltage",
        "measurement",
        2,
    ),
    Metric(
        "plant_bms_current",
        "Plant BMS current (bank)",
        "master",
        ("bms_current",),
        "A",
        "current",
        "measurement",
        0,
    ),
    Metric(
        "plant_bms_power",
        "Plant battery power (BMS)",
        "master",
        ("bms_voltage", "bms_current"),
        "W",
        "power",
        "measurement",
        0,
    ),
    Metric(
        "plant_bms_temperature",
        "Plant BMS temperature",
        "master",
        ("bms_temperature",),
        "°C",
        "temperature",
        "measurement",
        1,
    ),
)
METRIC_BY_KEY: Final = {m.key: m for m in METRICS}
ENERGY_METRICS: Final = tuple(m for m in METRICS if m.kind == "energy")


@dataclass(frozen=True, slots=True)
class TotalsMember:
    id: str
    name: str
    profile: Profile
    poll: PollConfig


def energy_key(metric: Metric, series: str) -> str:
    return f"{metric.key}_{series}"


def _finite(value: Value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


class PlantSeries:
    """The plant's statistics series: ``g<n>``, never reused. Persisted in ``plant-totals.json``
    as the generation plus the signature (members and their own series) it was started for; a
    different signature at start-up or at run time starts the next generation. If the file is
    missing or damaged the generation continues after the highest one seen in the discovery
    inventory."""

    def __init__(self, directory: Path, known: Collection[str] = ()) -> None:
        self._store = JsonStore(directory / SERIES_FILE, validate=_valid_series_document)
        self.persist_failed = False
        self.signature: tuple[tuple[str, str], ...] | None = None
        self.generation = -1
        self._high = max(
            (int(m["n"]) for k in known if (m := re.fullmatch(r"g(?P<n>\d+)", k))), default=-1
        )
        if self._store.status is LoadStatus.VALID:
            self.generation = int(self._store.data["generation"])
            self.signature = tuple((a, b) for a, b in self._store.data["signature"])

    @property
    def series(self) -> str:
        return f"g{max(self.generation, 0)}"

    def follow(self, signature: Sequence[tuple[str, str]]) -> bool:
        """Adopt ``signature``. Returns ``True`` when that started a new series."""
        sig = tuple(sorted(signature))
        if self.signature == sig and self.generation >= 0 and self.generation >= self._high:
            if not self.persist_failed:
                return False
            return self._save()  # retry an unsaved generation; True once it is safe to publish
        started = (
            self.generation >= 0 or self._high >= 0 or self._store.status is not LoadStatus.MISSING
        )
        # the first series of a fresh installation is g0; anything else continues after the
        # highest generation that ever existed
        self.generation = max(self.generation, self._high) + 1 if started else 0
        self._high = max(self._high, self.generation)
        self.signature = sig
        self._save()
        return started or self.generation == 0

    def _save(self) -> bool:
        assert self.signature is not None
        try:
            self._store.commit(
                {"generation": self.generation, "signature": [list(p) for p in self.signature]}
            )
        except OSError:
            self.persist_failed = True
            log.error("plant totals: the series state could not be written")
            return False
        self.persist_failed = False
        return True


def _valid_series_document(doc: dict[str, Any]) -> bool:
    sig = doc.get("signature")
    gen = doc.get("generation")
    return (
        set(doc) == {"generation", "signature"}
        and isinstance(gen, int)
        and not isinstance(gen, bool)
        and gen >= 0
        and isinstance(sig, list)
        and all(
            isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in sig
        )
    )


def known_series(components: Collection[str]) -> set[str]:
    """Plant series found in a set of component keys (discovery inventory)."""
    found = (_SERIES_KEY.match(k) for k in components)
    return {m["series"] for m in found if m}


class PlantTotals:
    """Newest member values and the totals computed from them."""

    def __init__(
        self,
        members: Sequence[TotalsMember],
        series: PlantSeries,
        *,
        keys_of: Callable[[str], Collection[str]],
        role_of: Callable[[str], str | None],
        member_series_of: Callable[[str], str],
    ) -> None:
        self.members = {m.id: m for m in members}
        self.series = series
        self._keys_of = keys_of
        self._role_of = role_of
        self._member_series_of = member_series_of
        self._latest: dict[str, dict[str, tuple[Value, float]]] = {i: {} for i in self.members}
        self._online: dict[str, bool] = dict.fromkeys(self.members, False)
        self.dirty = True
        self._own_series: dict[str, str] = {}
        self.sync_series()

    # -- series ---------------------------------------------------------------------------------

    def signature(self) -> list[tuple[str, str]]:
        return [(i, self._member_series_of(i)) for i in self.members]

    def sync_series(self) -> bool:
        """Follow the members' own series; ``True`` if the plant series changed (the caller
        republishes the discovery)."""
        changed = self.series.follow(self.signature())
        if changed:
            self.dirty = True
        return changed

    # -- feed -----------------------------------------------------------------------------------

    def update(self, inverter_id: str, values: Mapping[str, Value], now: float) -> None:
        if inverter_id not in self.members:
            return
        latest = self._latest[inverter_id]
        for key, value in values.items():
            latest[key] = (value, now)
        self.dirty = True

    def drop(self, inverter_id: str) -> None:
        if inverter_id in self._latest:
            self._latest[inverter_id] = {}
            self.dirty = True

    def drop_energy(self, inverter_id: str) -> None:
        """A member started a new series: its counters of the old one are no longer valid."""
        latest = self._latest.get(inverter_id)
        if latest is None:
            return
        energy = {k for m in ENERGY_METRICS for k in m.keys}
        for key in energy & set(latest):
            del latest[key]
        self.dirty = True

    def clear(self) -> None:
        for i in self._latest:
            self._latest[i] = {}
        self.dirty = True

    def set_online(self, inverter_id: str, online: bool) -> None:
        if inverter_id in self._online and self._online[inverter_id] != online:
            self._online[inverter_id] = online
            self.dirty = True

    # -- computation ----------------------------------------------------------------------------

    def _window(self, member: TotalsMember, key: str) -> float:
        try:
            entry = member.profile.entry(key)
        except KeyError:
            return 0.0
        seconds = float(getattr(member.poll, member.profile.groups[entry.group].poll))
        return STALE_FACTOR * seconds

    def _fresh(self, member: TotalsMember, key: str, now: float) -> float | None:
        item = self._latest[member.id].get(key)
        if item is None or now - item[1] > self._window(member, key):
            return None
        return _finite(item[0])

    def _member_value(self, metric: Metric, member: TotalsMember, now: float) -> float | None:
        selected = self._keys_of(member.id)
        if metric.key == "plant_pv_power":
            expected = [k for k in metric.keys if k in selected]
            if not expected:
                return 0.0
            parts = [self._fresh(member, k, now) for k in expected]
            return None if any(p is None for p in parts) else sum(p for p in parts if p is not None)
        if metric.optional and not any(k in selected for k in metric.keys):
            return 0.0
        if metric.key == "plant_battery_power":
            volts = self._fresh(member, "battery_voltage", now)
            amps = self._fresh(member, "battery_current", now)
            return None if volts is None or amps is None else volts * amps
        return self._fresh(member, metric.keys[0], now)

    def master(self) -> str | None:
        """The one member that the device reports as parallel master (R336); ``None`` if there is
        none or more than one."""
        masters = [i for i in self.members if self._role_of(i) == "master"]
        return masters[0] if len(masters) == 1 else None

    def _master_value(self, metric: Metric, now: float) -> float | None:
        master = self.master()
        if master is None:
            return None
        member = self.members[master]
        if metric.key == "plant_bms_power":
            volts = self._fresh(member, "bms_voltage", now)
            amps = self._fresh(member, "bms_current", now)
            # the BMS current is positive while charging: negate it to show discharging as positive
            return None if volts is None or amps is None else -(volts * amps)
        return self._fresh(member, metric.keys[0], now)

    def value(self, metric: Metric, now: float) -> float | None:
        if metric.kind == "master":
            return self._master_value(metric, now)
        total = 0.0
        for member in self.members.values():
            part = self._member_value(metric, member, now)
            if part is None:
                return None
            total += part
        if metric.kind == "power":
            return round(total, 1)
        return round(total, 3)

    def stale_members(self, now: float) -> list[str]:
        """Members that do not deliver all the values the totals need right now."""
        bad = []
        for member in self.members.values():
            if not self._online[member.id]:
                bad.append(member.id)
                continue
            for metric in METRICS:
                if metric.kind != "master" and self._member_value(metric, member, now) is None:
                    bad.append(member.id)
                    break
        return bad

    def payload(self, now: float) -> dict[str, Any]:
        body: dict[str, Any] = {"energy_series": self.series.series}
        for metric in METRICS:
            body[metric.key] = self.value(metric, now)
        stale = self.stale_members(now)
        body["plant_members"] = len(self.members)
        body["plant_members_stale"] = len(stale)
        body["plant_members_list"] = list(self.members)
        body["plant_members_stale_list"] = stale
        master = self.master()
        body["plant_master"] = self.members[master].name if master else None
        body["plant_master_id"] = master
        return body

    def expire_after(self, metric: Metric) -> int:
        windows = [self._window(m, k) for m in self.members.values() for k in metric.keys]
        return max(1, math.ceil(max(windows, default=0.0)))

    def view(self, now: float) -> dict[str, Any]:
        """For the web interface: the totals plus the member status."""
        stale = set(self.stale_members(now))
        return {
            "active": True,
            "series": self.series.series,
            "master": self.master(),
            "values": {m.key: self.value(m, now) for m in METRICS},
            "members": [
                {
                    "id": m.id,
                    "name": m.name,
                    "online": self._online[m.id],
                    "stale": m.id in stale,
                    "role": self._role_of(m.id),
                }
                for m in self.members.values()
            ],
        }
