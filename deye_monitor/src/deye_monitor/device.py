"""Per-inverter runtime (SPEC §3, §5, §8.1, §9): identity, snapshots, failover, energy.

``InverterRuntime`` is a state machine driven by ``step()``: ``starting -> identifying ->
(quarantined | running)``, plus ``offline`` and ``blocked``. All timing uses an injected monotonic
clock, so tests drive ``step()`` directly; ``run()`` is the long-lived task around it.

Safety rules implemented here:

* operating data is only decoded and published for an identified device whose serial number
  equals the bound one. The identification is tied to the transport's *connection epoch*: every
  identification or data cycle remembers the epoch of its first request and checks it after EVERY
  request (answer, Modbus exception or transport failure); a new connection discards the whole
  cycle and the serial number is read completely again on the new connection;
* every read also remembers the *lifecycle token* (rebind, path switch, quarantine, selection
  change, block, ...); a result that finished under an older token is neither persisted nor
  published;
* a value is decoded from the words of ONE snapshot (one group, one cycle, one path); a missing
  word or partner makes exactly that entity ``None``; nothing is carried forward; a group whose
  every read failed is published as a typed failure snapshot (all selected values ``None``);
* the two paths of an inverter are never polled in parallel and never mixed;
* a damaged identity file blocks the binding (``blocked``), a damaged energy file blocks energy
  publication; nothing is re-bound or restarted at generation 0 silently.

A cycle resets the consecutive-failure counter only if it had no transport failure; legitimate
address-related Modbus exceptions are not failures and keep the values that did arrive.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime
import logging
import math
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from deye_monitor.config import EnergyResolution, InverterConfig
from deye_monitor.energy_state import EnergyTracker
from deye_monitor.identity import (
    PROBLEM_UNWRITABLE,
    SERIAL_WORDS,
    IdentityBook,
    normalize_serial,
)
from deye_monitor.observation import Observation, ObservationState
from deye_monitor.profiles.decode import Value, decode, firmware_offset
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.hardware import (
    HardwareProfile,
    capability_conflicts,
    unknown_capability_codes,
    unreported_capabilities,
)
from deye_monitor.profiles.plan import ReadPlan, build_read_plan, unreported_entries
from deye_monitor.profiles.schema import Entry, Profile
from deye_monitor.protocol.modbus import ModbusError, ModbusExceptionResponse
from deye_monitor.protocol.solarman_v5 import V5LoggerError
from deye_monitor.scheduler import BusScheduler, QueueFull
from deye_monitor.store import JsonStore
from deye_monitor.transport.base import ReadTransport, TransportError

log = logging.getLogger(__name__)

IDENTITY_GROUP = "identity"
INTERNAL_ERROR = "internal_error"
SERIAL_START = 3
R65 = 65
_CAPABILITY_WORDS = (0, 15, 17, 18, 20, 21, 22, 24)
_READ_ERRORS = (TransportError, ModbusError, V5LoggerError, QueueFull)
_PV_POINTS = frozenset({"pv"})
# Internal reads for the plant-feature suggestion (SPEC §9.2: R111, R133, R189, R336). They are
# part of the read plan but never of the published selection.
BOOTSTRAP_KEYS = frozenset(
    {"battery_operating_basis", "gen_port_usage", "gen_on_grid_input", "parallel_enable"}
)
SLAVE_TEMPERATURE_KEY = "battery_temperature"  # R586: valid on the parallel master only
INACTIVE_SLAVE_TEMPERATURE = "parallel_slave_temperature"
INACTIVE_SLAVE_NO_BMS = "parallel_slave_no_bms"
R111_NO_BATTERY = 2  # the only numbered meaning of R111 in the V105.8 document
PROBE_SHARE = 0.02  # rough allowance per inverter for identity probes and failover reads
BLOCKED_RETRY_S = 60.0
_CONFLICT_POINTS = {
    "device_type": None,  # every entity
    "mppt_count": frozenset({"pv"}),
    "phase_count": None,
    "battery_inputs": frozenset({"battery", "bms"}),
}
CONFLICT_POINTS = _CONFLICT_POINTS  # measurement points a capability conflict puts in doubt


class InverterState(StrEnum):
    STARTING = "starting"
    IDENTIFYING = "identifying"
    RUNNING = "running"
    QUARANTINED = "quarantined"
    OFFLINE = "offline"
    BLOCKED = "blocked"  # a critical store is damaged or unwritable: nothing runs until reset


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The raw words of one group read (one cycle, one path) plus the immutable context the
    publisher needs to reject messages that were queued before a change."""

    cycle_id: int
    group: str
    monotonic_ts: float
    wall_ts: float
    endpoint_id: str
    words: Mapping[int, int]
    missing: frozenset[int]
    selection_revision: int = 0
    lifecycle_epoch: int = 0
    energy_generation: int = 0
    energy_series: str = "g0"  # opaque statistics series: ``g<n>`` or ``<token>g<n>``
    failed: bool = False  # every read of the group failed: all values are unknown
    profile_keys: frozenset[str] = frozenset()  # values taken from the hardware profile


class StateSink(Protocol):
    """Narrow interface to the publisher layer (T8). Callbacks must not block."""

    def on_state(
        self, inverter_id: str, snapshot: Snapshot, values: Mapping[str, Value]
    ) -> None: ...

    def on_availability(self, inverter_id: str, state: InverterState, reason: str) -> None: ...

    def on_diagnostics(self, inverter_id: str, diagnostics: Mapping[str, Any]) -> None: ...


class NullSink:
    def on_state(self, inverter_id: str, snapshot: Snapshot, values: Mapping[str, Value]) -> None:
        return None

    def on_availability(self, inverter_id: str, state: InverterState, reason: str) -> None:
        return None

    def on_diagnostics(self, inverter_id: str, diagnostics: Mapping[str, Any]) -> None:
        return None


class RelaySink:
    """Forwards to a sink that is attached later (the publisher needs the finished runtime)."""

    def __init__(self, target: StateSink | None = None) -> None:
        self.target: StateSink = target or NullSink()

    def on_state(self, inverter_id: str, snapshot: Snapshot, values: Mapping[str, Value]) -> None:
        self.target.on_state(inverter_id, snapshot, values)

    def on_availability(self, inverter_id: str, state: InverterState, reason: str) -> None:
        self.target.on_availability(inverter_id, state, reason)

    def on_diagnostics(self, inverter_id: str, diagnostics: Mapping[str, Any]) -> None:
        self.target.on_diagnostics(inverter_id, diagnostics)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Application limits of the device loop (SPEC §3 defaults)."""

    failover_after: int = 3  # consecutive failed cycles before the other path is tried
    failback_probe_s: float = 300.0  # how often the primary path is probed while on fallback
    identify_retry_s: float = 30.0  # retry of a failed identification
    quarantine_retry_s: float = 60.0  # re-identification of a quarantined inverter
    r65_confirm_s: float = 60.0  # delay of the confirming R65 read after a pending change

    def __post_init__(self) -> None:
        if self.failover_after < 1:
            raise ValueError("failover_after must be >= 1")
        for name in ("failback_probe_s", "identify_retry_s", "quarantine_retry_s", "r65_confirm_s"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0")


@dataclass(slots=True)
class Route:
    """One way to an inverter: an endpoint plus the unit used on it."""

    name: str  # "primary" | "fallback"
    endpoint_id: str
    unit: int
    transport: ReadTransport
    scheduler: BusScheduler
    max_read_registers: int
    max_read_registers_configured: int | None = None  # as configured; differs when capped
    bus_gap_s: float = 0.05  # effective gap of the canonical bus (maximum of its endpoints)
    response_time_s: float = 0.25  # budget assumption (capped by the endpoint timeout)
    bad_until: float = 0.0  # a fallback that answered with a foreign serial is not tried before
    verified_epoch: int | None = None  # connection epoch under which the identity was verified


def default_daylight(wall_ts: float) -> bool:
    """Crude daylight window (local time 08:00-17:00) for the PV observation counters."""
    return 8 <= datetime.datetime.fromtimestamp(wall_ts).hour < 17


@dataclass(slots=True)
class _GroupRead:
    words: dict[int, int] = field(default_factory=dict)
    missing: set[int] = field(default_factory=set)
    blocks_ok: int = 0
    error: Exception | None = None
    epoch: int | None = None  # the single connection epoch of the whole cycle
    reconnected: bool = False  # a new connection appeared during the cycle: discard everything
    stale: bool = False  # the lifecycle token changed while the cycle was in flight


class InverterRuntime:
    def __init__(
        self,
        cfg: InverterConfig,
        routes: Sequence[Route],
        *,
        profile: Profile,
        hardware: HardwareProfile,
        features: Features,
        selection: Collection[str],
        book: IdentityBook,
        energy: EnergyTracker,
        capability_store: JsonStore,
        sink: StateSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        settings: RuntimeSettings | None = None,
        selection_revision: int = 0,
        daylight: Callable[[float], bool] = default_daylight,
    ) -> None:
        if not routes:
            raise ValueError("at least one route is required")
        self.cfg = cfg
        self.id = cfg.id
        self.routes = list(routes)
        self.profile = profile
        self.hardware = hardware
        self.features = features
        self.selection = frozenset(selection)
        self.book = book
        self.capability_store = capability_store
        self.sink: StateSink = sink or NullSink()
        self.clock = clock
        self.wall = wall
        self.settings = settings or RuntimeSettings()
        self.selection_revision = selection_revision
        # True until the user chose a selection: only the mandatory reads run (SPEC §9.3)
        self.awaiting_selection = False
        self.lifecycle_epoch = 0
        self.suggested_features: dict[str, Any] = {}
        self.identity_info: dict[str, Any] = {}  # firmware words / R336 role for the status view
        self.last_success_wall: float | None = None
        self.r65_raw: int | None = (
            None  # last VALID R65 reading (never invented, never 0 by default)
        )
        self.r65_ok: bool | None = None  # did the latest R65 read deliver a value? None = not read
        self.bus_utilisation: Callable[[], float] | None = None
        self.daylight = daylight
        self.energy = energy
        self.state = InverterState.STARTING
        self.reason = "starting"
        self.active = 0
        self.fails = 0
        self.needs_identity = True
        self.coalesced_ticks = 0
        self.cycle_id = 0
        self.observations: dict[str, Observation] = {}
        self.capability_conflicts: list[dict[str, str]] = []
        # entries the device does not report (key -> raw word), checked at the identity read
        self.unreported: dict[str, int] = {}
        # entries that do not apply on this unit (key -> reason code): on a parallel slave the
        # battery temperature and the BMS block. Same effect as ``unreported`` (not polled, no
        # entity), but derived from the device-reported role and checked at every identity pass.
        self.inactive: dict[str, str] = {}
        self._unreported_key: list[Any] | None = None
        self._load_unreported()
        self.seen_serial: str | None = None
        self._plans: dict[int, ReadPlan] = {}
        self._due: dict[str, float] = {}
        self._next_identify_at = 0.0
        self._failback_due = 0.0
        self._confirm_due: float | None = None
        self._announced: tuple[InverterState, str] | None = None
        self.internal_errors = 0
        self.last_internal_error: str | None = None
        self._reported_energy_block: str | None = None
        self._reported_r65_invalid = False
        self._wake = asyncio.Event()
        self.released = asyncio.Event()  # set by a store reset: a waiting supervisor may restart
        self._energy_selected = any(profile.entry(k).scale_source for k in self.selection)

    # -- public ---------------------------------------------------------------------------

    @property
    def route(self) -> Route:
        return self.routes[self.active]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "active_path": self.route.name,
            "endpoint": self.route.endpoint_id,
            "bound": self.book.bound(self.id) is not None,
            "binding_source": self.book.source(self.id),
            "serial_matches": (
                None if self.seen_serial is None else self.seen_serial == self.book.bound(self.id)
            ),
            "consecutive_failed_cycles": self.fails,
            "capability_conflicts": list(self.capability_conflicts),
            "unreported": self._unreported_view(),
            "inactive": {k: {"reason": r} for k, r in sorted(self.inactive.items())},
            "energy": self.energy.snapshot(),
            "coalesced_ticks": self.coalesced_ticks,
            "internal_errors": self.internal_errors,
            "last_internal_error": self.last_internal_error,
            "selection_revision": self.selection_revision,
            "awaiting_selection": self.awaiting_selection,
            "lifecycle_epoch": self.lifecycle_epoch,
            "suggested_features": dict(self.suggested_features),
            "identity": dict(self.identity_info),
            "bound_serial": self.book.bound(self.id),
            "r65": {"raw": self.r65_raw, "last_read_ok": self.r65_ok},
            "cooldown_active": bool(getattr(self.route.transport, "cooldown_active", False)),
            "last_success_wall": self.last_success_wall,
            "bus_utilisation": self.bus_utilisation() if self.bus_utilisation else None,
            "transport": {r.name: _counters(r) for r in self.routes},
            "max_read_registers": {
                "configured": self.route.max_read_registers_configured,
                "effective": self.route.max_read_registers,
            },
        }

    def _load_unreported(self) -> None:
        cached = self.capability_store.data.get(self.id)
        if not isinstance(cached, dict) or not isinstance(cached.get("key"), list):
            return
        known = {e.key for e in self.profile.entries}
        stored = cached.get("unreported")
        if isinstance(stored, dict):
            self.unreported = {
                k: v
                for k, v in stored.items()
                if k in known and isinstance(v, int) and not isinstance(v, bool)
            }
            self._unreported_key = cached["key"]

    def _unreported_view(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, raw in sorted(self.unreported.items()):
            entry = self.profile.entry(key)
            out[key] = {
                "register": entry.addr,
                "raw": raw,
                "profile_value": self._profile_value(entry),
            }
        return out

    def _profile_value(self, entry: Entry) -> int | None:
        return {
            "mppt_count": self.hardware.expected_mppt_count,
            "phase_count": self.hardware.expected_phase_count,
            "battery_inputs": self.hardware.battery_inputs,
            None: None,
        }[entry.profile_fallback]

    def mark_internal_error(self, exc: BaseException) -> None:
        """An unexpected exception ended the task: go ``offline`` at once (reason
        ``internal_error``), invalidate every release and identify again after the restart. Only
        the exception type is kept (never its message); a sink that raises must not break the
        error handling itself."""
        self.internal_errors += 1
        self.last_internal_error = type(exc).__name__
        self._bump()
        self._invalidate()
        self._next_identify_at = 0.0
        if self.state is not InverterState.BLOCKED:
            self.state = InverterState.OFFLINE
            self.reason = INTERNAL_ERROR
        self._announced = (self.state, self.reason)
        log.error("%s: internal error (%s)", self.id, self.last_internal_error)
        with contextlib.suppress(Exception):
            self.sink.on_availability(self.id, self.state, self.reason)
            self.sink.on_diagnostics(self.id, {"event": "internal_error", **self.diagnostics()})

    def current_plan(self) -> ReadPlan:
        return self._plan(self.route)

    def candidate_plan(self, selection: Collection[str]) -> ReadPlan:
        """The plan ``selection`` would get on the ACTIVE route (its limits, response time and the
        effective bus gap): the same description the running plan is built from."""
        route = self.route
        return build_read_plan(
            self.profile,
            frozenset(selection) | BOOTSTRAP_KEYS,
            self.features,
            max_read_registers=route.max_read_registers,
            energy_auto=self.cfg.energy_resolution is EnergyResolution.AUTO,
            poll=self.cfg.poll,
            response_time_s=route.response_time_s,
            gap_s=route.bus_gap_s,
        )

    def check_selection(self, selection: Collection[str]) -> None:
        """Raises ``SelectionError`` (placeholders etc.) without changing anything."""
        build_read_plan(
            self.profile,
            frozenset(selection) | BOOTSTRAP_KEYS,
            self.features,
            max_read_registers=self.route.max_read_registers,
            energy_auto=self.cfg.energy_resolution is EnergyResolution.AUTO,
        )

    def set_selection(self, selection: Collection[str], revision: int) -> None:
        """Replace the published selection (T10). Validated by the plan builder; the plan is
        rebuilt and answers of a read that was already in flight are not published."""
        new = frozenset(selection)
        self.check_selection(new)
        self.selection = new
        self.selection_revision = revision
        self._bump()
        self._energy_selected = any(self.profile.entry(k).scale_source for k in new)
        self._plans.clear()
        self._due.clear()
        self.observations = {k: v for k, v in self.observations.items() if k in new}
        if self.state is InverterState.RUNNING:
            self.needs_identity = True  # R65 may have become necessary: identify again
            self._next_identify_at = self.clock()
        self._wake.set()

    def request_refresh(self) -> None:
        """Every group is due now (the publisher asks for fresh cycles after a reconnect or the
        Home Assistant birth). Uses the normal scheduler; only an identified, running inverter
        polls, anything else is read as soon as it is identified anyway."""
        if self.state is not InverterState.RUNNING or self.needs_identity:
            return
        now = self.clock()
        for group in self._groups():
            self._due[group] = now
        self._wake.set()

    def observation_states(self) -> dict[str, ObservationState]:
        return {key: obs.state for key, obs in self.observations.items()}

    def rebind(self) -> bool:
        """Explicit user action: forget a first-seen binding and bind the next valid serial.
        Invalidates snapshots, capabilities and the energy confirmation, and reports the inverter
        offline at once. A binding made by the ``expected_serial`` option can only be changed
        through the option (returns False)."""
        try:
            if not self.book.rebind(self.id):
                return False
        except OSError:
            self._block(PROBLEM_UNWRITABLE)
            return False
        self._revoke("rebind requested")
        self._wake.set()
        self._announce(InverterState.OFFLINE, "rebind requested")  # never keep the old online state
        self._diag("rebound")
        return True

    def _revoke(self, why: str) -> None:
        """Core invalidation (no sink calls, nothing that can raise except the guarded capability
        cache): lifecycle epoch +1, energy confirmation and due times gone, identify again."""
        self._bump()
        self._drop_capabilities()
        self._invalidate()
        self._next_identify_at = 0.0
        self.seen_serial = None
        self.observations.clear()
        self.capability_conflicts = []
        self.unreported = {}
        self.inactive = {}
        self._unreported_key = None
        self._plans.clear()
        self.state = InverterState.STARTING
        self.reason = why

    def revoke_after_reset(self, why: str = "store reset") -> None:
        """Phase 1 of a store reset (core, cannot be interrupted by a notification): every release
        is revoked (lifecycle epoch +1, old in-flight results and queued messages become stale)
        and the inverter identifies from scratch. Wakes a supervisor that waits for the release."""
        self._revoke(why)
        self._announced = None
        self.released.set()
        self._wake.set()

    def notify_after_reset(self, why: str = "store reset") -> None:
        """Phase 2: availability and diagnostics (the sink may raise)."""
        self._announce(InverterState.OFFLINE, why)
        self._diag("store_reset")

    def invalidate_after_reset(self, why: str = "store reset") -> None:
        """Both phases for a single inverter (notification errors propagate after the core)."""
        self.revoke_after_reset(why)
        self.notify_after_reset(why)

    def reset_energy_store(self) -> str:
        """Explicit user action: start a NEW energy series (never a reused one); returns the
        series the next record gets."""
        self._bump()
        series = self.energy.reset()
        self.needs_identity = True  # the resolution is confirmed again for the new series
        self._next_identify_at = self.clock()
        self._diag("energy_store_reset", next_series=series)
        self.released.set()
        self._wake.set()
        return series

    async def run(self) -> None:
        """Long-lived task: ``step()`` until cancelled."""
        while True:
            delay = await self.step()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(delay, 0.001)):
                    await self._wake.wait()

    async def step(self) -> float:
        """Do everything that is due now; return the delay until the next due time."""
        if self.book.problem and self.state is not InverterState.BLOCKED:
            self._block(self.book.problem)
        if self.state is InverterState.BLOCKED:
            return BLOCKED_RETRY_S
        self._detect_reconnect()
        now = self.clock()
        if self._must_identify():
            if now >= self._next_identify_at:
                await self._identify()
            if self.state is not InverterState.RUNNING or self.needs_identity:
                return max(0.0, self._next_identify_at - self.clock())
        if self._confirm_due is not None and self.clock() >= self._confirm_due:
            await self._confirm_r65()
        if self.active != 0 and self.clock() >= self._failback_due:
            await self._probe_primary()
            if self.needs_identity:
                return 0.0
        for group in self._groups():
            if self.state is not InverterState.RUNNING or self.needs_identity:
                break
            if self.clock() >= self._due.get(group, 0.0):
                await self._run_group(group)
        return self._delay()

    # -- identification ---------------------------------------------------------------------

    def _must_identify(self) -> bool:
        return self.needs_identity or self.state is not InverterState.RUNNING

    def _token(self) -> int:
        return self.lifecycle_epoch

    def _bump(self) -> None:
        self.lifecycle_epoch += 1

    async def _identify(self) -> bool:
        token, route = self._token(), self.route
        if self.state is InverterState.STARTING:
            self._set(InverterState.IDENTIFYING, "identifying")
        plan = self._plan(route)
        read = await self._read_group(route, plan, IDENTITY_GROUP, token, identify=True)
        if read.stale or token != self._token():
            return False  # superseded by a rebind, a path switch, a selection change ...
        now = self.clock()
        if read.reconnected:
            self._connection_unidentified("connection changed during identification")
            return False
        if read.blocks_ok == 0:
            return self._identify_failed(read.error, now)
        serial_words = [read.words.get(SERIAL_START + i) for i in range(SERIAL_WORDS)]
        serial = (
            None
            if None in serial_words
            else normalize_serial([w for w in serial_words if w is not None])
        )
        if serial is None:
            return self._identify_failed(None, now, "invalid or missing serial number")
        if self.book.problem:
            self._block(self.book.problem)
            return False
        self.seen_serial = serial
        bound = self.book.bound(self.id)
        if bound is None:
            try:
                self.book.bind_first_seen(self.id, serial)
            except OSError, ValueError:
                self._block(PROBLEM_UNWRITABLE)
                return False
            self._diag("bound", source="first_seen")
        elif serial != bound:
            return self._mismatch(route, now)
        if self._unreported_stale(read.words, serial):
            return False
        self._after_identity(route, read, now, serial)
        await self._check_parallel_slave(route, read.words, token)
        return True

    def _identify_failed(self, error: Exception | None, now: float, why: str | None = None) -> bool:
        reason = (
            why or f"no usable identity answer ({type(error).__name__ if error else 'incomplete'})"
        )
        self._next_identify_at = now + self.settings.identify_retry_s
        self._invalidate()
        if self.state is not InverterState.QUARANTINED:
            self._set(InverterState.OFFLINE, reason)
        self._note_failure(now)
        return False

    def _mismatch(self, route: Route, now: float) -> bool:
        self._bump()
        self._invalidate()
        if self.active == 0:
            self._next_identify_at = now + self.settings.quarantine_retry_s
            self._set(InverterState.QUARANTINED, "serial number differs from the bound one")
            self._diag("quarantined", path=route.name)
            return False
        route.bad_until = now + self.settings.failback_probe_s
        self._diag("fallback_quarantined", path=route.name)
        self._switch(0, now, "fallback path answers with a different serial number")
        return False

    def _after_identity(self, route: Route, read: _GroupRead, now: float, serial: str) -> None:
        route.verified_epoch = read.epoch
        self._check_capabilities(read.words, serial)
        if self._plan(route).reads_r65:
            self._note_r65(read.words.get(R65))
        self._observe_energy(read.words.get(R65), serial, now)
        self.needs_identity = False
        self.fails = 0 if read.error is None else self.fails + 1
        first = self.state is not InverterState.RUNNING
        self.state = InverterState.RUNNING
        self.reason = "identified"
        if first:
            for group in self._groups():
                self._due[group] = now  # first data cycle right away
        self._due[IDENTITY_GROUP] = now + self.cfg.poll_identity_s
        self._emit(IDENTITY_GROUP, route, read)

    def _parallel_role(self, words: Mapping[int, int]) -> str | None:
        """The device itself says (R336, never names or manual flags): ``slave`` (parallel on and
        not the master), ``other`` (master or standalone) or ``None`` when the role word is missing
        or cannot be decoded. Unknown is not "not a slave"."""
        enable = decode(self.profile.entry("parallel_enable"), words)
        master = decode(self.profile.entry("parallel_master"), words)
        if not isinstance(enable, bool) or not isinstance(master, bool):
            return None
        return "slave" if enable and not master else "other"

    async def _check_parallel_slave(
        self, route: Route, words: Mapping[int, int], token: int
    ) -> None:
        """Only the parallel master has the battery and its BMS: a slave reports garbage for the
        battery temperature (R586) and zeros for the BMS block. On a slave those entries are
        inactive (not polled, no entity); an all-zero BMS block on the master stays visible.

        An unknown role (R336 missing or undecodable in this pass) keeps the established verdict
        untouched and infers nothing; only a valid master/standalone role clears it."""
        role = self._parallel_role(words)
        if role is None:
            return
        inactive: dict[str, str] = {}
        if role == "slave":
            inactive = {
                e.key: INACTIVE_SLAVE_TEMPERATURE
                for e in self.profile.entries
                if e.key == SLAVE_TEMPERATURE_KEY
            }
            bms = [e for e in self.profile.entries if e.point == "bms" and not e.internal]
            addresses = sorted({a for e in bms for a in e.addresses})
            silent = await self._bms_block_silent(route, addresses, token)
            if silent is None:  # no verdict (read failed, connection changed): keep what is known
                kept = {k: r for k, r in self.inactive.items() if r == INACTIVE_SLAVE_NO_BMS}
                inactive |= kept
            elif silent:
                inactive |= {e.key: INACTIVE_SLAVE_NO_BMS for e in bms}
        if inactive != self.inactive and token == self._token():
            self.inactive = inactive
            self._plans.clear()
            self._diag("inactive_updated")

    async def _bms_block_silent(
        self, route: Route, addresses: list[int], token: int
    ) -> bool | None:
        """Read the BMS block (in chunks that fit the route's read limit) and tell whether every
        word is zero; ``None`` when no verdict is possible."""
        if not addresses or route.max_read_registers < 1:
            return None
        words: dict[int, int] = {}
        pending = list(addresses)
        while pending:
            start = pending[0]
            span = [a for a in pending if a - start < route.max_read_registers]
            count = span[-1] - start + 1
            outcome, epoch = await self._read(route, start, count)
            if token != self._token():
                return None
            if epoch != route.verified_epoch:
                self._connection_unidentified("connection changed")
                return None
            if isinstance(outcome, Exception):
                return None
            words.update({a: outcome[a - start] for a in span})
            pending = pending[len(span) :]
        return all(words[a] == 0 for a in addresses)

    def _identity_key(self, words: Mapping[int, int], serial: str) -> list[Any]:
        return [serial, words.get(15), words.get(17), words.get(18), self.profile.revision]

    def _unreported_stale(self, words: Mapping[int, int], serial: str) -> bool:
        """A new serial, firmware or profile revision voids what is known about unreported
        fields: identify again with the full read plan so every field is checked once more."""
        if not self.unreported or self._identity_key(words, serial) == self._unreported_key:
            return False
        self.unreported = {}
        self._unreported_key = None
        self._plans.clear()
        self._next_identify_at = self.clock()
        return True

    def _check_capabilities(self, words: Mapping[int, int], serial: str) -> None:
        caps = {a: words[a] for a in _CAPABILITY_WORDS if a in words}
        self.identity_info["firmware_words"] = {a: words.get(a) for a in (14, 15, 16, 17, 18)}
        self.identity_info["capability_words"] = dict(caps)  # raw, for comparing logger and gateway
        self.identity_info["capabilities_unreported"] = unreported_capabilities(caps)
        self.identity_info["capabilities_unknown"] = unknown_capability_codes(self.hardware, caps)
        conflicts = capability_conflicts(self.hardware, caps)
        self.capability_conflicts = [{"code": c.code, "detail": c.detail} for c in conflicts]
        key = self._identity_key(words, serial)
        found = unreported_entries(self.profile, words)
        checked = {
            e.key
            for e in self.profile.entries
            if not e.internal and e.declares_not_reported and e.addr in words
        }
        unreported = {k: v for k, v in self.unreported.items() if k not in checked} | found
        changed = unreported != self.unreported
        self.unreported = unreported
        self._unreported_key = key
        if changed:
            self._plans.clear()
        if self.capability_store.corrupt:  # a cache may be rebuilt after a diagnostic
            self._diag("capability_store_rebuilt", problem=self.capability_store.problem)
            with contextlib.suppress(OSError):
                self.capability_store.reset({})
        cached = self.capability_store.data.get(self.id)
        if (
            not isinstance(cached, dict)
            or cached.get("key") != key
            or cached.get("unreported") != unreported
        ):
            doc = copy.deepcopy(self.capability_store.data)
            doc[self.id] = {
                "key": key,
                "conflicts": self.capability_conflicts,
                "unreported": unreported,
            }
            try:
                self.capability_store.commit(doc)
            except OSError:
                self._diag("capability_store_unwritable")  # a cache: keep running
            else:
                self._diag("capabilities_updated" if cached else "capabilities_cached")
        flagged = {_CONFLICT_POINTS.get(c.code) for c in conflicts}
        for key_, entry in ((k, self.profile.entry(k)) for k in self.selection):
            obs = self._observation(key_, entry)
            obs.conflict = any(f is None or entry.point in f for f in flagged) and bool(conflicts)
        if changed:
            self._diag("unreported_updated")
        if conflicts:
            self._diag("capability_conflict")

    def _drop_capabilities(self) -> None:
        doc = copy.deepcopy(self.capability_store.data)
        if doc.pop(self.id, None) is not None:
            with contextlib.suppress(OSError):
                self.capability_store.commit(doc)

    def _observe_energy(self, r65: int | None, serial: str | None, now: float) -> None:
        if not self._energy_selected:
            return
        was_confirmed = self.energy.resolution is not None
        for event in self.energy.observe(r65, serial):
            self._diag(
                f"energy_{event.kind}",
                resolution=event.resolution,
                generation=event.generation,
                source=event.source,
            )
        if self.energy.needs_choice and not self._reported_r65_invalid:
            self._reported_r65_invalid = True
            self._diag("energy_resolution_unknown", reason="r65_invalid", r65=r65)
        elif not self.energy.needs_choice:
            self._reported_r65_invalid = False
        if self.energy.blocked and self.energy.blocked != self._reported_energy_block:
            self._reported_energy_block = self.energy.blocked
            self._diag(self.energy.blocked)
        elif not self.energy.blocked:
            self._reported_energy_block = None
        self._confirm_due = now + self.settings.r65_confirm_s if self.energy.pending else None
        if not was_confirmed and self.energy.resolution is not None:
            for key in self.selection:  # publish the energy values as soon as they are known
                entry = self.profile.entry(key)
                if entry.scale_source:
                    self._due[entry.group] = now

    def _note_r65(self, value: int | None) -> None:
        self.r65_ok = value is not None
        if value is not None:
            self.r65_raw = value

    async def _confirm_r65(self) -> None:
        """The confirming (consecutive) R65 read after a pending change, on the same scheduler,
        on the verified connection."""
        token, route = self._token(), self.route
        self._confirm_due = None
        outcome, epoch = await self._read(route, R65, 1)
        if token != self._token():
            return
        if epoch != route.verified_epoch:  # whatever the outcome: identify first
            self._connection_unidentified("connection changed")
            return
        value = None if isinstance(outcome, Exception) else outcome[0]
        self._note_r65(value)
        self._observe_energy(value, self.seen_serial, self.clock())

    # -- data cycles --------------------------------------------------------------------------

    def _groups(self) -> list[str]:
        plan = self._plan(self.route)
        present = {b.group for b in plan.blocks}
        return [g for g in self.profile.groups if g in present]

    def _interval(self, group: str) -> float:
        return float(getattr(self.cfg.poll, self.profile.groups[group].poll))  # type: ignore[index]

    async def _run_group(self, group: str) -> None:
        route = self.route
        if group == IDENTITY_GROUP:
            await self._identify()  # periodic refresh doubles as the identity check
            return
        token = self._token()
        read = await self._read_group(route, self._plan(route), group, token)
        if read.stale or token != self._token():
            return  # a rebind, path switch or selection change overtook this cycle: drop it
        now = self.clock()
        self._schedule_next(group, now)
        if read.reconnected:
            self._connection_unidentified("connection changed")  # not trusted before identity
            return
        transport_failed = read.error is not None
        if transport_failed:
            self._cycle_failed(group, read.error, now)
        else:
            self.fails = 0  # a cycle without transport failure heals the counter
        if token != self._token():
            return  # the failure triggered a path switch: the partial cycle is not released
        complete_failure = read.blocks_ok == 0
        if (
            not transport_failed
            and not complete_failure
            and self._announced
            != (
                InverterState.RUNNING,
                "identified, data flowing",
            )
        ):
            self._announce(InverterState.RUNNING, "identified, data flowing")
        self._emit(group, route, read, failed=complete_failure)

    async def _read_group(
        self,
        route: Route,
        plan: ReadPlan,
        group: str,
        token: int,
        *,
        identify: bool = False,
    ) -> _GroupRead:
        out = _GroupRead()
        blocks = [b for b in plan.blocks if b.group == group]
        out.epoch = None if identify else route.verified_epoch
        for i, block in enumerate(blocks):
            outcome, epoch = await self._read(route, block.start, block.count)
            if out.epoch is None:
                out.epoch = epoch  # identification: the first request defines the connection
            elif epoch != out.epoch:
                out.reconnected = True
                return out
            if token != self._token():
                out.stale = True
                return out
            if isinstance(outcome, ModbusExceptionResponse):
                out.missing.update(block.needed)  # the device refused this block; the rest may work
                continue
            if isinstance(outcome, Exception):
                out.error = outcome
                for rest in blocks[i:]:
                    out.missing.update(rest.needed)
                return out
            out.blocks_ok += 1
            out.words.update(
                zip(range(block.start, block.start + block.count), outcome, strict=True)
            )
        return out

    @staticmethod
    async def _read(route: Route, start: int, count: int) -> tuple[list[int] | Exception, int]:
        """The only place that reads from the verified connection: the outcome (words, Modbus
        exception or transport failure) together with the connection epoch AFTER the request. A
        new connection may belong to another device, so every caller compares the epoch, whatever
        the outcome was."""
        outcome: list[int] | Exception
        try:
            outcome = await route.scheduler.run(
                lambda: route.transport.read_holding(route.unit, start, count)
            )
        except _READ_ERRORS as exc:
            outcome = exc
        return outcome, _epoch(route)

    def _emit(self, group: str, route: Route, read: _GroupRead, *, failed: bool = False) -> None:
        self.cycle_id += 1
        dropped = {k for k in self.unreported if self.profile.entry(k).profile_fallback is None}
        dropped |= set(self.inactive)
        keys = [
            k
            for k in sorted(self.selection)
            if self.profile.entry(k).group == group and k not in dropped
        ]
        if failed:
            missing = {a for b in self._plan(route).blocks if b.group == group for a in b.needed}
        else:
            missing = set(read.missing)
        profile_keys = frozenset(
            k for k in keys if k in self.unreported and not failed
        )  # fallback entries
        snapshot = Snapshot(
            cycle_id=self.cycle_id,
            group=group,
            monotonic_ts=self.clock(),
            wall_ts=self.wall(),
            endpoint_id=route.endpoint_id,
            words={} if failed else dict(read.words),
            missing=frozenset(missing),
            selection_revision=self.selection_revision,
            lifecycle_epoch=self.lifecycle_epoch,
            energy_generation=self.energy.generation,
            energy_series=self.energy.series,
            failed=failed,
            profile_keys=profile_keys,
        )
        if not failed:
            self._update_suggestions(snapshot)
            self.last_success_wall = snapshot.wall_ts
        resolution = self.energy.resolution
        values: dict[str, Value] = {}
        for key in keys:
            entry = self.profile.entry(key)
            if key in profile_keys:
                value: Value = self._profile_value(entry)
            else:
                value = (
                    None
                    if failed
                    else decode(
                        entry,
                        snapshot.words,
                        energy_resolution=resolution,
                        main_firmware=self._main_firmware(),
                    )
                )
            if entry.fw_offsets is not None and not failed:
                self._note_firmware_offset(entry, snapshot.words)
            values[key] = value
            obs = self._observation(key, entry)
            obs.record(
                value, daylight=entry.point in _PV_POINTS and self.daylight(snapshot.wall_ts)
            )
        if values:  # an empty selection (or a group without selected keys) creates no entities
            self.sink.on_state(self.id, snapshot, values)

    def _observation(self, key: str, entry: Entry) -> Observation:
        obs = self.observations.get(key)
        if obs is None:
            obs = self.observations[key] = Observation(
                watch_zero=entry.observes_zero, daylight_only=entry.zero_needs_daylight
            )
        return obs

    def _main_firmware(self) -> int | None:
        words = self.identity_info.get("firmware_words")
        value = words.get(15) if isinstance(words, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _note_firmware_offset(self, entry: Entry, words: Mapping[int, int]) -> None:
        """Diagnostics: which offset of a firmware-dependent entry was applied and why."""
        raw = words.get(entry.addresses[0])
        if raw is None:
            return
        signed = raw - 0x10000 if raw & 0x8000 else raw
        offset, basis = firmware_offset(entry, self._main_firmware(), signed)
        info: dict[str, Any] = {"offset": offset, "basis": basis}
        if offset and signed == 0:
            info["unavailable"] = "raw_zero_with_offset"  # parallel slaves report raw 0
        self.identity_info.setdefault("firmware_offsets", {})[entry.key] = info

    def _update_suggestions(self, snapshot: Snapshot) -> None:
        """Suggest plant features from the settings that were read (the user confirms, T10)."""
        words = snapshot.words
        basis: dict[str, Any] = {}

        def read(key: str) -> Value:
            return decode(self.profile.entry(key), words)

        battery_basis = read("battery_operating_basis")  # raw protocol word; only 2 is numbered
        if isinstance(battery_basis, int) and not isinstance(battery_basis, bool):
            self.suggested_features["battery"] = battery_basis != R111_NO_BATTERY
            basis["battery"] = (
                "R111=2 (documented: no battery)"
                if battery_basis == R111_NO_BATTERY
                else f"R111={battery_basis} (an operating mode, not proof of a battery)"
            )
        port = read("gen_port_usage")
        if isinstance(port, str) and not port.startswith("unknown"):
            self.suggested_features["gen_port_mode"] = port
            basis["gen_port_mode"] = "R133 setting (usage, no proof of a connected source)"
        on_grid = read("gen_on_grid_input")
        if on_grid is not None:
            self.suggested_features["grid_port_use"] = "generator" if on_grid else "utility"
            basis["grid_port_use"] = "R189 bit0"
        master = read("parallel_master")
        if master is not None:
            self.identity_info["role"] = {
                "parallel_enable": read("parallel_enable"),
                "master": master,
                "phase": read("parallel_phase"),
            }
        enable = read("parallel_enable")
        if enable is not None:
            self.suggested_features["parallel"] = bool(enable)
            basis["parallel"] = "R336 bit0"
        if basis:
            self.suggested_features.setdefault("_basis", {}).update(basis)

    def _schedule_next(self, group: str, now: float) -> None:
        interval = self._interval(group)
        due = self._due.get(group, now) + interval
        if due <= now:  # overloaded: coalesce the missed ticks instead of catching up
            skipped = int((now - self._due.get(group, now)) // interval)
            self.coalesced_ticks += skipped
            due = self._due.get(group, now) + (skipped + 1) * interval
        self._due[group] = due

    def _cycle_failed(self, group: str, error: Exception | None, now: float) -> None:
        self._note_failure(now)
        log.debug(
            "%s: group %s failed (%s)", self.id, group, type(error).__name__ if error else "?"
        )

    # -- failure, failover, failback -----------------------------------------------------------

    def _note_failure(self, now: float) -> None:
        self.fails += 1
        if self.fails < self.settings.failover_after:
            return
        other = 1 if self.active == 0 else 0
        if len(self.routes) > 1 and now >= self.routes[other].bad_until:
            self._switch(other, now, f"{self.fails} failed cycles on the {self.route.name} path")
        elif self.state is InverterState.RUNNING:
            self._bump()
            self.needs_identity = True
            self._next_identify_at = now
            self._invalidate()
            self._set(InverterState.OFFLINE, f"{self.fails} consecutive failed cycles")

    def _switch(self, index: int, now: float, why: str) -> None:
        """Move to the other path; nothing of the old path is kept (no mixing of two paths)."""
        self._bump()
        self.active = index
        self.fails = 0
        self._invalidate()
        self._due.clear()
        self._plans.pop(index, None)  # the budget is recomputed for the target bus
        self.routes[index].verified_epoch = None
        self._next_identify_at = now
        if index != 0:
            self._failback_due = now + self.settings.failback_probe_s
        self._set(InverterState.OFFLINE, why)
        self._diag("path_switched", path=self.route.name)

    async def _probe_primary(self) -> None:
        """While on the fallback path: look at the primary path (identity only)."""
        token = self._token()
        primary = self.routes[0]
        now = self.clock()
        self._failback_due = now + self.settings.failback_probe_s
        try:
            words = await primary.scheduler.run(
                lambda: primary.transport.read_holding(primary.unit, SERIAL_START, SERIAL_WORDS)
            )
        except _READ_ERRORS:
            return
        if token != self._token():
            return
        serial = normalize_serial(words)
        if serial is not None and serial == self.book.bound(self.id):
            self._switch(0, now, "primary path is back")
            self._diag("failback")
        elif serial is not None:
            self._diag("primary_serial_mismatch")

    # -- bookkeeping --------------------------------------------------------------------------

    def _plan(self, route: Route) -> ReadPlan:
        index = self.routes.index(route)
        plan = self._plans.get(index)
        if plan is None:
            plan = build_read_plan(
                self.profile,
                self.selection | BOOTSTRAP_KEYS,
                self.features,
                max_read_registers=route.max_read_registers,
                energy_auto=self.cfg.energy_resolution is EnergyResolution.AUTO,
                unreported=self.unreported.keys() | self.inactive.keys(),
                poll=self.cfg.poll,
                response_time_s=route.response_time_s,
                gap_s=route.bus_gap_s,
            )
            self._plans[index] = plan
        return plan

    def _connection_unidentified(self, why: str) -> None:
        """The single transition for "this connection is not identified (any more)": revoke every
        release (lifecycle epoch +1, energy confirmation, due times), report ``offline`` and
        identify again at once. Online only again after serial and data release."""
        self._bump()
        self._invalidate()
        self._next_identify_at = self.clock()
        self._wake.set()
        if self.state is InverterState.RUNNING:
            self._set(InverterState.OFFLINE, why)

    def _invalidate(self) -> None:
        # R65 diagnostics belong to the identity/connection they were read from: unknown again
        self.r65_raw = None
        self.r65_ok = None
        self.needs_identity = True
        self.energy.invalidate()
        self._confirm_due = None
        self._due.clear()

    def _detect_reconnect(self) -> None:
        route = self.route
        if (
            self.state is InverterState.RUNNING
            and route.verified_epoch is not None
            and _epoch(route) != route.verified_epoch
        ):
            self._connection_unidentified("connection changed")

    def _delay(self) -> float:
        now = self.clock()
        times = [t for g, t in self._due.items() if g in self._groups()]
        if self.active != 0:
            times.append(self._failback_due)
        if self._confirm_due is not None:
            times.append(self._confirm_due)
        if self._must_identify():
            # a path switch (or any loss of the release) inside a group run: the identification is
            # due then, not at the next group or the failback probe
            times.append(self._next_identify_at)
        if not times:
            return 1.0
        return max(0.0, min(times) - now)

    def _block(self, reason: str) -> None:
        """A critical store is damaged or unwritable: no binding, no data, no retry loop."""
        self._bump()
        self._invalidate()
        self.state = InverterState.BLOCKED
        self.reason = reason
        self._announced = None
        self._wake.set()
        self._announce(InverterState.BLOCKED, reason)
        self._diag(reason)

    def block_for_recovery(self, reason: str) -> None:
        """A selection swap could be neither completed nor taken back: nothing is released until
        the application is restarted (the next start evaluates the recovery marker)."""
        try:
            self._block(reason)
        except Exception:  # the core invalidation ran first; a failing sink must not undo it
            log.exception("%s: notification of the recovery block failed", self.id)

    def _set(self, state: InverterState, reason: str) -> None:
        self.state = state
        self.reason = reason
        if state is not InverterState.IDENTIFYING and state is not InverterState.STARTING:
            self._announce(state, reason)

    def _announce(self, state: InverterState, reason: str) -> None:
        if self._announced == (state, reason):
            return
        self._announced = (state, reason)
        log.info("%s: %s (%s)", self.id, state.value, reason)
        self.sink.on_availability(self.id, state, reason)
        self._diag("state")

    def _diag(self, event: str, **extra: Any) -> None:
        self.sink.on_diagnostics(self.id, {"event": event, **extra, **self.diagnostics()})


def _epoch(route: Route) -> int:
    return int(getattr(route.transport, "connection_epoch", 0))


def _counters(route: Route) -> dict[str, int]:
    counters = getattr(route.transport, "counters", None)
    if counters is None:
        return {}
    return {name: getattr(counters, name) for name in counters.__slots__}
