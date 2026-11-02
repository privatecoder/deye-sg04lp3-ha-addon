"""Write layer (SPEC §10): previews, single-use permits, one guarded write, audit and peers.

One ``WriteService`` belongs to one runtime generation (a plant change builds a new one, so every
permit dies with its generation). The flow is explicit and owner driven; nothing here runs on its
own (no startup, reconnect or schedule trigger). The guarded core (``_execute`` and ``_send``)
knows no transport: the Web path reaches it through ``preview`` / ``confirm``, a second transport
(for example MQTT commands) through ``submit``, which has no human preview but the same
execution-time guards, audit, pacing and observation:

1. ``preview`` reads the target (fresh, on the primary route, under the bus lock) together with
   everything the preconditions need, snapshots the same register of every other plant inverter
   and issues a single-use operation id with a short-lived permit bound to user, plant revision,
   enablement, inverter, serial, firmware words, role, route, unit and connection epoch.
2. ``confirm`` serialises (one plant write at a time, at least ``pacing_s`` between writes),
   re-checks the authorisation at the final send point, re-reads the whole context inside ONE
   bus critical section (any change since the preview is a conflict), appends the audit intent
   (an unavailable audit log blocks the send) and sends exactly one FC16 with one register
   through the write guard. No retry, no FC06, no fallback endpoint, no fan-out, no rollback.
   Every gate (release, firmware fingerprint, identity, role, R111/R133/R146 gates, ordering
   against the fresh companions, the old word) is evaluated again on the fresh reads of this
   critical section, not taken from the preview.
3. After ``observe_delay_s`` the target and all peers are read back (bus locks released in the
   meantime). Statuses: ``rejected`` (nothing sent), ``sent`` (unresolved), ``failed`` (device
   exception), ``readback_confirmed``, ``outcome_unknown``. A second, read-only peer observation
   follows at ``late_observe_delay_s`` (a later observation, never an attribution; the write
   serialisation is not held while it waits), and the owner can read the peers again at any time.

In a parallel plant the master distributes the whole settings block to every slave, so a slave is
locked for writing: the role is read fresh at the preview and again at the send point.

A valid acknowledgement and a matching read-back prove the value in MCU1's settings RAM only, not
that MCU2 adopted it, not persistence, and not that peers follow: peer values are observed and
reported as same/different/unavailable, never as "propagated".
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from deye_monitor.config import WriteRelease
from deye_monitor.device import InverterRuntime, InverterState, Route, _epoch
from deye_monitor.identity import normalize_serial
from deye_monitor.profiles.decode import decode
from deye_monitor.protocol.guard import WriteGrant
from deye_monitor.protocol.modbus import ModbusError, ModbusExceptionResponse, WriteAck
from deye_monitor.protocol.solarman_v5 import V5LoggerError
from deye_monitor.scheduler import QueueFull
from deye_monitor.transport.base import (
    TransportError,
    WriteIndeterminate,
    WriteNotSent,
    WriteTransport,
)
from deye_monitor.write_ops import (
    AFCI_FAULT_BIT,
    BOUND_CHILDREN,
    BY_KEY,
    DESCRIPTORS,
    HARDWARE,
    LATE_OBSERVE_DELAY_S,
    OBSERVE_DELAY_S,
    PACING_S,
    PERMIT_S,
    R28,
    R111_SOC,
    R146_SPAIN_BIT,
    R558,
    SENTINEL,
    TOU_SLOTS,
    Option,
    Refused,
    WriteDescriptor,
    check_range,
    limit_from_words,
    parse_value,
)

if TYPE_CHECKING:
    from deye_monitor.runtime import Runtime

log = logging.getLogger(__name__)

AUDIT_FILE = "write-audit.jsonl"
AUDIT_MAX_BYTES = 8 * 1024 * 1024
MAX_OPERATIONS = 500
MAX_PERMITS = 32
SUPERSEDED = "superseded_context"  # the context an intent was received in no longer holds
EXPIRED = "expired"  # the intent's own deadline passed before the send point

REJECTED = "rejected"
SENT = "sent"
FAILED = "failed"
CONFIRMED = "readback_confirmed"
UNKNOWN = "outcome_unknown"
NOOP = "noop"  # the fresh word already equals the request: audited, nothing sent (``submit`` only)
PENDING = "pending"  # accepted for execution, not yet at the send point (in memory only)
STATUSES = (REJECTED, SENT, FAILED, CONFIRMED, UNKNOWN, NOOP)

SERIAL_START = 3
SERIAL_WORDS = 5
FINGERPRINT_ADDRESSES = (11, 14, 15, 17, 18)  # board versions: HMI, MCU2, MCU1 words
IDENTITY_ADDRESSES = (3, 4, 5, 6, 7, 11, 14, 15, 17, 18, 20, 21)  # serial, firmware, rated power
IDENTITY_SPAN = (3, 21)
R111 = 111  # battery control mode (0 voltage, 1 SOC)
R133 = 133  # GEN port usage
R146 = 146  # ToU control word
R189 = 189  # generator input selector (0 = GEN port, 1 = GRID terminal)
R553 = 553  # warning word 1; bit 3 = meter communication failure (W4)
W4_METER_COMM = 0x0008
CLOCK_ADDRESSES = (62, 63, 64)  # inverter clock (YY<<8|MM, DD<<8|hh, mm<<8|ss)
R336 = 336  # parallel role
GEN_SMART_LOAD = 1
MAX_COOLDOWN_WAIT_S = 15.0
PARALLEL_SLAVE = "parallel_slave"
MAX_REREADS = 20
LATE_PENDING = "pending"
LATE_DONE = "done"
LATE_INTERRUPTED = "interrupted"
READ_ERRORS = (TransportError, ModbusError, V5LoggerError, QueueFull)


@dataclass(frozen=True, slots=True)
class WriteSettings:
    """Application limits of the write flow (contract defaults)."""

    observe_delay_s: float = OBSERVE_DELAY_S
    late_observe_delay_s: float = LATE_OBSERVE_DELAY_S
    pacing_s: float = PACING_S
    permit_s: float = PERMIT_S


class AuditError(Exception):
    """The audit log could not be written: nothing may be sent."""


class AuditLog:
    """Durable append-only JSON lines. Every line carries the complete record of one operation at
    that moment; the last line of an operation id is its current state."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: str, record: Mapping[str, Any], wall: float) -> None:
        line = json.dumps(
            {"v": 1, "ts": wall, "event": event, "record": record},
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                data = line.encode() + b"\n"
                if self._torn_tail():
                    data = b"\n" + data  # an interrupted earlier record must not swallow this one
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("audit write made no progress")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, ValueError) as exc:
            raise AuditError(type(exc).__name__) from exc

    def _torn_tail(self) -> bool:
        """The file does not end with a newline (an earlier write was interrupted)."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    return False
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) != b"\n"
        except OSError:
            return False

    def load(self) -> dict[str, dict[str, Any]]:
        """The latest record per operation id (damaged lines are skipped)."""
        out: dict[str, dict[str, Any]] = {}
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        for line in text.splitlines():
            try:
                doc = json.loads(line)
                record = doc["record"]
                out[record["operation_id"]] = record
            except ValueError, KeyError, TypeError:
                if line.strip():
                    log.warning("write audit: skipped an unreadable line")
                continue
        return out

    def rotate_if_large(self) -> None:
        with contextlib.suppress(OSError):
            if self.path.stat().st_size > AUDIT_MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))


@dataclass(frozen=True, slots=True)
class Binding:
    """The immutable execution context captured when an intent is RECEIVED (memory only, no bus):
    which physical inverter (serial), on which connection (lifecycle and route epoch, endpoint,
    unit), in which role and firmware, under which release revision and plant generation."""

    inverter_id: str
    serial: str
    lifecycle: int
    endpoint_id: str
    unit: int
    epoch: int
    role: tuple[bool | None, bool | None] | None  # (parallel, master) as identified, if known
    release: WriteRelease | None
    firmware: tuple[int, ...]  # R11, R14, R15, R17, R18
    plant_revision: int


@dataclass(frozen=True, slots=True)
class Intent:
    """What a transport without a human preview hands to ``submit``: the binding of the receipt,
    the absolute deadline (same monotonic clock as the service) and a revocation test (the
    transport's session or exposure ended). Both are compared again at the final send point."""

    binding: Binding
    deadline: float
    revoked: Callable[[], bool]


@dataclass(slots=True)
class Permit:
    operation_id: str
    user_id: str
    boot_id: str
    plant_revision: int
    inverter_id: str
    key: str
    enabled: frozenset[str]
    serial: str
    firmware: tuple[int, int, int]
    role: dict[str, Any]
    endpoint_id: str
    unit: int
    epoch: int
    lifecycle: int
    context: dict[int, int] | None  # the preview's reads; ``None`` = no preview (``submit``)
    field: int
    old_word: int | None
    new_word: int | None
    peers: dict[str, dict[str, Any]]
    expires_at: float
    source: str = "web"
    used: bool = False
    revoked: bool = False
    noop: bool = False  # ``submit`` found the fresh word already equal to the request
    intent: Intent | None = None


# Reasons that take a setting away (the owner's release no longer applies to what the unit is
# now); every other reason is transient and only makes the setting unavailable.
WITHDRAWN_REASONS = frozenset({"firmware_changed", PARALLEL_SLAVE, "hardware_not_released"})


@dataclass(frozen=True, slots=True)
class SettingState:
    """One released setting as read fresh: ``ready`` false means a gate is closed (``reason``)."""

    key: str
    ready: bool
    reason: str | None
    word: int
    field: int
    value: int | float | str | None
    cap: int | None
    floor: int | None = None


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """``ok`` (a fresh read), ``unavailable`` (nothing could be established now, transient) or
    ``withdrawn`` (the release does not apply: not released, other firmware, a slave)."""

    status: str
    reason: str | None
    settings: Mapping[str, SettingState]


RETRY_FIRST_S = 5.0  # a snapshot that was not fresh is repeated after this, then doubled


def settings_read_requests(keys: Collection[str], step: int) -> int:
    """Modbus requests of ONE settings snapshot (``snapshot``): the identity block plus the runs of
    the other words of the released ``keys``, each split at the endpoint's block limit ``step``."""
    step = max(1, step)
    first, last = IDENTITY_SPAN
    requests = -(-(last - first + 1) // step)
    addresses: set[int] = set()
    for key in keys:
        if key in BY_KEY:
            addresses |= WriteService._addresses(BY_KEY[key])
    run = 0
    previous: int | None = None
    for address in sorted(addresses - set(IDENTITY_ADDRESSES)):
        if previous is None or address != previous + 1:
            requests += -(-run // step) if run else 0
            run = 0
        run += 1
        previous = address
    return requests + (-(-run // step) if run else 0)


def settings_poll_budget(
    inv: InverterRuntime, retry_s: float | None = None
) -> dict[str, float | int] | None:
    """Recurring bus cost of the periodic settings snapshot of ``inv`` (conditional: only while the
    Home Assistant entities poll it): requests per snapshot after splitting at the endpoint's
    block limit, the cadence (``poll_settings_s``) and the resulting bus utilisation.

    Recovery load is reported separately: after a snapshot that was not fresh the read repeats at
    ``retry_s`` (the publisher's current retry delay, ``None`` while healthy), starting at
    ``RETRY_FIRST_S``. ``recovery_utilisation`` is the load at the current cadence,
    ``recovery_worst_utilisation`` the load at the first retry, which also bounds forced
    (RUNNING-triggered) refreshes: the publisher services them no sooner than ``RETRY_FIRST_S``
    after the previous attempt; ``utilisation`` stays the steady
    state and ``load`` is what the bus carries now (the larger of steady and current recovery)."""
    cfg = inv.cfg
    keys = [k for k in cfg.enabled_write_operations if k in BY_KEY]
    if not cfg.allow_writes or not keys:
        return None
    route = inv.routes[0]
    requests = settings_read_requests(keys, route.max_read_registers)
    cycle = requests * (route.response_time_s + route.bus_gap_s)
    interval = float(cfg.poll.settings_s)
    recovery = min(interval, retry_s) if retry_s is not None and retry_s > 0 else interval
    worst = min(interval, RETRY_FIRST_S)
    return {
        "requests": requests,
        "max_block": route.max_read_registers,
        "cadence_s": interval,
        "cycle_s": cycle,
        "utilisation": cycle / interval,
        "recovery_cadence_s": recovery,
        "recovery_utilisation": cycle / recovery,
        "recovery_worst_cadence_s": worst,
        "recovery_worst_utilisation": cycle / worst,
        "load": cycle / recovery,
    }


@dataclass(slots=True)
class _Sent:
    """What happened at the single send point."""

    attempted: bool = False
    status: str = SENT
    reason: str | None = None
    response: dict[str, Any] = field(default_factory=dict)


def _hex(word: int | None) -> str | None:
    return None if word is None else f"0x{word:04X}"


class WriteService:
    def __init__(
        self,
        runtime: Runtime,
        *,
        directory: Path,
        settings: WriteSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.runtime = runtime
        self.settings = settings or WriteSettings()
        self.clock = clock
        self.wall = wall
        self.sleep = sleep
        self.boot_id = secrets.token_hex(4)
        self.plant_revision: Callable[[], int | None] = lambda: None
        self.audit = AuditLog(directory / AUDIT_FILE)
        self._permits: OrderedDict[str, Permit] = OrderedDict()
        self._expired: OrderedDict[str, None] = OrderedDict()
        self._ops: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = asyncio.Lock()  # one plant write at a time
        self._last_send: float | None = None
        self._tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._late: set[asyncio.Task[None]] = set()
        self._open = True
        self._load()

    # -- audit / history ------------------------------------------------------------------------

    def _load(self) -> None:
        self.audit.rotate_if_large()
        for op_id, record in self.audit.load().items():
            self._ops[op_id] = record
        for record in self._ops.values():
            if record.get("status") in (SENT, PENDING):
                # an unfinished intent after a restart: never replayed, outcome not known
                record["status"] = UNKNOWN
                record["reason"] = "restart"
                with contextlib.suppress(AuditError):
                    self.audit.append("restart", record, self.wall())
            late = record.get("late_observation")
            if late and late.get("status") == LATE_PENDING:
                late["status"] = LATE_INTERRUPTED  # the later read did not happen
                with contextlib.suppress(AuditError):
                    self.audit.append("restart", record, self.wall())
        while len(self._ops) > MAX_OPERATIONS:
            self._ops.popitem(last=False)

    def _record(self, event: str, op: dict[str, Any]) -> bool:
        op["updated_at"] = self.wall()
        try:
            self.audit.append(event, op, op["updated_at"])
        except AuditError:
            log.error("write audit log is not writable (%s)", event)
            op["audit_incomplete"] = True
            return False
        return True

    def operations(self, inverter_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        out = [
            self.operation_view(op)
            for op in reversed(self._ops.values())
            if inverter_id is None or op.get("inverter") == inverter_id
        ]
        return out[:limit]

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        op = self._ops.get(operation_id)
        return None if op is None else self.operation_view(op)

    def operation_view(self, op: Mapping[str, Any]) -> dict[str, Any]:
        """Public form of a record: no raw frames; a pending later observation says how long it
        still takes (the server clock, so the browser needs no synchronised time)."""
        view = {k: v for k, v in op.items() if k != "response"}
        response = dict(op.get("response") or {})
        response.pop("raw", None)
        view["response"] = response
        late = op.get("late_observation")
        if late:
            view["late_observation"] = {
                **late,
                "in_s": max(0.0, late["due_at"] - self.wall())
                if late.get("status") == LATE_PENDING
                else 0.0,
            }
        return view

    # -- enablement -----------------------------------------------------------------------------

    def enabled(self, inverter_id: str) -> frozenset[str]:
        cfg = self.runtime.inverters[inverter_id].cfg
        return frozenset(cfg.enabled_write_operations) if cfg.allow_writes else frozenset()

    def revoke_all(self) -> None:
        for permit in self._permits.values():
            permit.revoked = True
        self._permits.clear()

    async def close(self) -> None:
        """End of the generation: permits are void, a write in flight is awaited briefly."""
        self._open = False
        self.revoke_all()
        for late in list(self._late):
            late.cancel()  # a later look is no write: it ends with the generation
        tasks = list(self._tasks)
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait(tasks, timeout=10)
        if self._late:
            with contextlib.suppress(Exception):
                await asyncio.wait(list(self._late), timeout=10)

    # -- reading helpers ------------------------------------------------------------------------

    def _target(self, inverter_id: str) -> InverterRuntime:
        inv = self.runtime.inverters.get(inverter_id)
        if inv is None:
            raise Refused("unknown_inverter", 404)
        return inv

    @staticmethod
    def _not_ready(inv: InverterRuntime) -> str | None:
        if inv.hardware.name != HARDWARE:
            return "hardware_not_released"
        if inv.state is not InverterState.RUNNING:
            return "not_running"
        if inv.needs_identity:
            return "identifying"
        if inv.active != 0:
            return "fallback_route"
        route = inv.routes[0]
        if route.verified_epoch is None or _epoch(route) != route.verified_epoch:
            return "connection_changed"
        bound = inv.book.bound(inv.id)
        if inv.seen_serial is None or inv.seen_serial != bound:
            return "identity_mismatch"
        if not hasattr(route.transport, "write_register"):
            return "no_write_transport"
        return None

    @staticmethod
    async def _read_span(route: Route, start: int, count: int) -> list[int]:
        words: list[int] = []
        step = max(1, route.max_read_registers)
        for first in range(start, start + count, step):
            words += await route.transport.read_holding(
                route.unit, first, min(step, start + count - first)
            )
        return words

    async def _read_context(self, route: Route, addresses: set[int]) -> dict[int, int]:
        """Inside ONE bus critical section (the caller holds the scheduler): the words of
        ``addresses`` plus the identity block, on the primary route; a changed connection means
        the identity check no longer applies."""
        words: dict[int, int] = {}
        if route.verified_epoch is None or _epoch(route) != route.verified_epoch:
            raise Refused("connection_changed", 409)
        first, last = IDENTITY_SPAN
        block = await self._read_span(route, first, last - first + 1)
        for address, value in zip(range(first, last + 1), block, strict=True):
            if address in IDENTITY_ADDRESSES:
                words[address] = value
        runs: list[list[int]] = []
        for address in sorted(addresses - set(IDENTITY_ADDRESSES)):
            if runs and runs[-1][-1] + 1 == address:
                runs[-1].append(address)
            else:
                runs.append([address])
        for run in runs:  # consecutive addresses in one request
            for address, value in zip(
                run, await self._read_span(route, run[0], len(run)), strict=True
            ):
                words[address] = value
        if _epoch(route) != route.verified_epoch:
            raise Refused("connection_changed", 409)
        return words

    async def _context(self, inv: InverterRuntime, addresses: set[int]) -> dict[int, int]:
        route = inv.routes[0]
        try:
            return await route.scheduler.run(lambda: self._read_context(route, addresses))
        except Refused:
            raise
        except READ_ERRORS as exc:
            raise Refused("read_failed", 409, error=type(exc).__name__) from None

    async def _guarded_word(
        self, inv: InverterRuntime, register: int, expected: str | None = None
    ) -> int | None:
        """One register of ``inv`` on its PRIMARY route, only if the serial number read in the same
        bus critical section is the bound one (a changed connection may be another device) and,
        when ``expected`` is given, the bound one is the serial saved with an operation."""
        route = inv.routes[0]
        bound = inv.book.bound(inv.id)
        if bound is None or (expected is not None and bound != expected):
            return None

        async def read() -> int | None:
            serial = normalize_serial(
                await route.transport.read_holding(route.unit, SERIAL_START, SERIAL_WORDS)
            )
            if serial != bound:
                return None
            return (await route.transport.read_holding(route.unit, register, 1))[0]

        try:
            return await route.scheduler.run(read)
        except READ_ERRORS:
            return None

    async def _observed_word(
        self, inv: InverterRuntime, register: int, expected: str | None = None
    ) -> int | None:
        """Read-back of one register after a write. A failed exchange (an unanswered write starts
        the endpoint cooldown) is retried ONCE after the cooldown: reading is harmless, writing
        is never repeated."""
        word = await self._guarded_word(inv, register, expected)
        if word is not None:
            return word
        remaining = float(getattr(inv.routes[0].transport, "cooldown_remaining_s", 0.0))
        if 0 < remaining <= MAX_COOLDOWN_WAIT_S:
            await self.sleep(remaining + 0.05)
            return await self._guarded_word(inv, register, expected)
        return None

    @staticmethod
    def _addresses(desc: WriteDescriptor) -> set[int]:
        out = {desc.register, R336}
        if desc.fault_probe is not None:
            out.add(desc.fault_probe[0])
        if desc.needs_smart_load:
            out.add(R133)
        if desc.requires_r111 is not None:
            out.add(R111)
        if desc.tou:
            out.add(R146)
        out.update(WriteService._dependency_addresses(desc))
        out.update(desc.mode_registers)
        out.update(desc.context_registers)
        out.update(desc.report_registers)
        out.update(g.register for g in desc.gates)
        if desc.branch is not None:
            out.add(desc.branch.register)
        out.update(o.gate.register for o in desc.options if o.gate is not None)
        if desc.firmware_clamp == "charge":
            out.add(R28)  # selector of the 10/12 kW charge ceiling (R14 is in the identity block)
        return out

    @staticmethod
    def _dependency_addresses(desc: WriteDescriptor) -> set[int]:
        """Registers needed to validate the relationships of ``desc`` from either side: every
        companion with the inputs of ITS numeric validation (the R28 selector of an R108 ceiling),
        and every descriptor whose dynamic bound refers to ``desc``. One visited set, so
        reciprocal bounds cannot loop; the same set serves preview, send, current and snapshot."""
        out: set[int] = set()
        seen = {desc.key}
        todo = [BY_KEY[k] for k in desc.related] + [
            c for c, _, _ in BOUND_CHILDREN.get(desc.key, ())
        ]
        while todo:
            other = todo.pop()
            if other.key in seen:
                continue
            seen.add(other.key)
            out.add(other.register)
            if other.firmware_clamp == "charge":
                out.add(R28)
        return out

    @staticmethod
    def _role(inv: InverterRuntime, words: Mapping[int, int]) -> dict[str, Any]:
        enable = decode(inv.profile.entry("parallel_enable"), words)
        master = decode(inv.profile.entry("parallel_master"), words)
        return {
            "parallel": enable if isinstance(enable, bool) else None,
            "master": master if isinstance(master, bool) else None,
        }

    @staticmethod
    def _slave_locked(role: Mapping[str, Any], synchronized: bool = True) -> bool:
        """The master distributes the settings block to every slave (a slave's own value would be
        overwritten): only a unit that is known to be a master, or known not to be in a parallel
        system at all, may be written. An unreadable role in an unknown system also locks."""
        parallel = role.get("parallel")
        if not synchronized:  # a register the master does not copy: every unit, addressed only
            return parallel is None
        return parallel is None or (parallel is True and role.get("master") is not True)

    def _check_role(
        self, inv: InverterRuntime, words: Mapping[int, int], desc: WriteDescriptor | None = None
    ) -> None:
        if self._slave_locked(self._role(inv, words), desc.synchronized if desc else True):
            raise Refused(PARALLEL_SLAVE, 409)

    @staticmethod
    def _fingerprint(inv: InverterRuntime, words: Mapping[int, int]) -> WriteRelease:
        return WriteRelease(
            profile=inv.hardware.name, words=tuple(words[a] for a in FINGERPRINT_ADDRESSES)
        )

    def _check_release(self, inv: InverterRuntime, words: Mapping[int, int]) -> None:
        """The release is bound to the firmware seen when the owner gave it."""
        if inv.cfg.write_release_firmware != self._fingerprint(inv, words):
            raise Refused("firmware_changed", 409)

    def bind(self, inverter_id: str, key: str) -> Binding:
        """The execution binding of an intent received now (no bus access). Refuses when the
        setting or the unit is not available for writing at this moment."""
        if not self._open:
            raise Refused("writes_stopped", 409)
        if key not in BY_KEY:
            raise Refused("unknown_operation", 404)
        inv = self._target(inverter_id)
        if key not in self.enabled(inverter_id):
            raise Refused("writes_not_enabled", 403)
        reason = self._not_ready(inv)
        if reason is not None:
            raise Refused("inverter_not_ready", 409, reason=reason)
        revision = self.plant_revision()
        if revision is None:
            raise Refused("writes_stopped", 409)
        firmware = self._identified_firmware(inv)
        if firmware is None or inv.seen_serial is None:
            raise Refused("inverter_not_ready", 409, reason="identifying")
        role = self._identified_role(inv)
        if role is not None and self._slave_locked(
            {"parallel": role[0], "master": role[1]}, BY_KEY[key].synchronized
        ):
            raise Refused(PARALLEL_SLAVE, 409)
        route = inv.routes[0]
        return Binding(
            inverter_id=inverter_id,
            serial=inv.seen_serial,
            lifecycle=inv.lifecycle_epoch,
            endpoint_id=route.endpoint_id,
            unit=route.unit,
            epoch=route.verified_epoch or 0,
            role=role,
            release=inv.cfg.write_release_firmware,
            firmware=firmware,
            plant_revision=revision,
        )

    @staticmethod
    def _identified_firmware(inv: InverterRuntime) -> tuple[int, ...] | None:
        seen = inv.identity_info.get("firmware_words")
        if not isinstance(seen, Mapping):
            return None
        words = tuple(seen.get(a) for a in FINGERPRINT_ADDRESSES)
        return None if any(not isinstance(w, int) for w in words) else cast(tuple[int, ...], words)

    @staticmethod
    def _identified_role(inv: InverterRuntime) -> tuple[bool | None, bool | None] | None:
        role = inv.identity_info.get("role")
        if not isinstance(role, Mapping):
            return None
        enable, master = role.get("parallel_enable"), role.get("master")
        return (
            enable if isinstance(enable, bool) else None,
            master if isinstance(master, bool) else None,
        )

    def _check_intent(self, intent: Intent) -> None:
        """Revocation, deadline and binding of an intent against the live state."""
        if intent.revoked():
            raise Refused(SUPERSEDED, 409)
        if self.clock() > intent.deadline:
            raise Refused(EXPIRED, 410)
        bound = intent.binding
        inv = self._target(bound.inverter_id)
        route = inv.routes[0]
        if (
            self.plant_revision() != bound.plant_revision
            or inv.lifecycle_epoch != bound.lifecycle
            or inv.seen_serial != bound.serial
            or route.verified_epoch != bound.epoch
            or route.endpoint_id != bound.endpoint_id
            or route.unit != bound.unit
            or inv.cfg.write_release_firmware != bound.release
            or self._identified_firmware(inv) != bound.firmware
            or (bound.role is not None and self._identified_role(inv) != bound.role)
        ):
            raise Refused(SUPERSEDED, 409)

    async def release_fingerprint(self, inverter_id: str) -> WriteRelease:
        """Fresh identity read for a release (reads only)."""
        inv = self._target(inverter_id)
        reason = self._not_ready(inv)
        if reason is not None:
            raise Refused("inverter_not_ready", 409, reason=reason)
        words = await self._context(inv, set())
        self._identity(inv, words)
        return self._fingerprint(inv, words)

    @staticmethod
    def _identity(
        inv: InverterRuntime, words: Mapping[int, int]
    ) -> tuple[str, tuple[int, int, int]]:
        serial = normalize_serial([words[a] for a in range(SERIAL_START, SERIAL_START + 5)])
        if serial is None or serial != inv.seen_serial or serial != inv.book.bound(inv.id):
            raise Refused("identity_mismatch", 409)
        return serial, (words[15], words[17], words[18])

    # -- fresh values -------------------------------------------------------------------------

    @staticmethod
    def catalogue_views() -> list[dict[str, Any]]:
        return [d.view() for d in DESCRIPTORS]

    async def current(self, inverter_id: str) -> list[dict[str, Any]]:
        """Fresh values of the operations enabled for ``inverter_id`` (one bus critical section)."""
        rows: list[dict[str, Any]] = (await self.current_view(inverter_id))["operations"]
        return rows

    async def current_view(self, inverter_id: str) -> dict[str, Any]:
        """``current`` plus read-only context: the effective ToU weekday mask and the inverter
        clock when a ToU operation is enabled."""
        inv = self._target(inverter_id)
        enabled = self.enabled(inverter_id)
        descs = [d for d in DESCRIPTORS if d.key in enabled]
        if not descs:
            return {"operations": [], "info": {}}
        reason = self._not_ready(inv)
        if reason is not None:
            return {"operations": [self._locked(d, reason) for d in descs], "info": {}}
        addresses: set[int] = set()
        for d in descs:
            addresses |= self._addresses(d)
        with_tou = any(d.tou for d in descs)
        if with_tou:
            addresses |= {R146, *CLOCK_ADDRESSES}
        try:
            words = await self._context(inv, addresses)
            self._identity(inv, words)
            self._check_release(inv, words)
        except Refused as exc:
            return {"operations": [self._locked(d, exc.key) for d in descs], "info": {}}
        role = self._role(inv, words)
        out = []
        for d in descs:
            if self._slave_locked(role, d.synchronized):  # per descriptor: R35 ... stay writable
                out.append(self._locked(d, PARALLEL_SLAVE))
                continue
            row = self._row(d, words, self._effective_cap(inv, d, words), inv)
            self._check_drift(inverter_id, d, words[d.register])
            out.append(row)
        if all(d.synchronized for d in descs) and all(
            r.get("reason") == PARALLEL_SLAVE for r in out
        ):
            return {"operations": out, "info": {}}
        info: dict[str, Any] = {"tou": self._tou_info(words)} if with_tou else {}
        if any(d.fault_probe is not None for d in descs):  # AFCI: module version + fault bit
            info["afci"] = {
                "module_version": words.get(12),
                "fault_word": words.get(R558),
                "fault": None
                if R558 not in words
                else "still_reported"
                if words[R558] & AFCI_FAULT_BIT
                else "not_reported",
            }
        hints = {d.key: await self._equal_hint(inv, d, words) for d in descs if d.equal_hint}
        if hints:
            info["equal"] = hints
        return {"operations": out, "info": info}

    async def _equal_hint(
        self, inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> dict[str, Any]:
        """The configured value of a per-unit setting on every unit of the plant (the addressed
        unit from the fresh ``words``, the others by one guarded read each). ``equal`` is ``None``
        while any unit is unknown or holds a word outside the setting's domain."""
        values: dict[str, Any] = {}
        for unit_id, other in self.runtime.inverters.items():
            word = (
                words[desc.register]
                if unit_id == inv.id
                else await self._guarded_word(other, desc.register)
            )
            valid = word is not None and desc.incumbent_valid(desc.field_of(word))
            values[unit_id] = (
                desc.display(desc.field_of(word)) if valid and word is not None else None
            )
        known = list(values.values())
        equal = None if None in known else len(set(known)) == 1
        return {"values": values, "equal": equal}

    async def snapshot(self, inverter_id: str) -> SettingsSnapshot:
        """Fresh state of every released setting for a transport that shows values (MQTT
        entities): one bus critical section, the same release, identity and role checks as
        ``current_view``. Reads only."""
        inv = self._target(inverter_id)
        enabled = self.enabled(inverter_id)
        descs = [
            d
            for d in DESCRIPTORS
            if d.key in enabled and not (d.needs_battery and not inv.features.battery)
        ]
        if not descs:
            return SettingsSnapshot("withdrawn", "not_released", {})
        reason = self._not_ready(inv)
        if reason is not None:
            status = "withdrawn" if reason in WITHDRAWN_REASONS else "unavailable"
            return SettingsSnapshot(status, reason, {})
        addresses: set[int] = set()
        for d in descs:
            addresses |= self._addresses(d)
        try:
            words = await self._context(inv, addresses)
            self._identity(inv, words)
            self._check_release(inv, words)
        except Refused as exc:
            status = "withdrawn" if exc.key in WITHDRAWN_REASONS else "unavailable"
            return SettingsSnapshot(status, exc.key, {})
        role = self._role(inv, words)
        if all(self._slave_locked(role, d.synchronized) for d in descs):
            return SettingsSnapshot("withdrawn", PARALLEL_SLAVE, {})  # nothing left to write here
        out: dict[str, SettingState] = {}
        for d in descs:
            if self._slave_locked(role, d.synchronized):
                continue  # not exposed here (the master's copy is the setting), like a withdrawal
            row = self._row(d, words, self._effective_cap(inv, d, words), inv)
            self._check_drift(inverter_id, d, words[d.register])
            hidden = row.get("reason") == "unexpected_bits"  # the bit is not the setting's value
            out[d.key] = SettingState(
                key=d.key,
                ready=row["state"] == "ready",
                reason=row.get("reason"),
                word=row["word"],
                field=row["field"],
                value=None if hidden else row["value"],
                cap=row["cap"],
                floor=row.get("floor"),
            )
        return SettingsSnapshot("ok", None, out)

    @staticmethod
    def _tou_info(words: Mapping[int, int]) -> dict[str, Any]:
        word = words[R146]
        mask = (word >> 1) & 0x7F  # bits 1..7 = Monday..Sunday; all zero = no weekday filter
        r62, r63, r64 = (words[a] for a in CLOCK_ADDRESSES)
        year, month, day = 2000 + (r62 >> 8), r62 & 0xFF, r63 >> 8
        hour, minute, second = r63 & 0xFF, r64 >> 8, r64 & 0xFF
        try:  # a real Gregorian date of the supported years (month lengths, leap years)
            datetime.date(year, month, day)
            valid = year <= 2099 and hour < 24 and minute < 60 and second < 60
        except ValueError:
            valid = False
        return {
            "word": word,
            "enabled": bool(word & 1),
            "spain_mode": bool(word & R146_SPAIN_BIT),
            "weekday_mask": mask,
            "weekdays": [bool(mask >> i & 1) for i in range(7)],
            "weekday_filter": mask != 0,
            "clock": (
                f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
                if valid
                else None
            ),
            "clock_words": [r62, r63, r64],
        }

    def _locked(self, desc: WriteDescriptor, reason: str) -> dict[str, Any]:
        return {"key": desc.key, "state": "locked", "reason": reason}

    def _row(
        self,
        desc: WriteDescriptor,
        words: Mapping[int, int],
        cap: int | None,
        inv: InverterRuntime,
    ) -> dict[str, Any]:
        field_now = desc.field_of(words[desc.register])
        row: dict[str, Any] = {
            "key": desc.key,
            "state": "ready",
            "word": words[desc.register],
            "field": field_now,
            "value": desc.display(field_now),
            "cap": cap if desc.model_limited else None,
        }
        if desc.floor_feature is not None:  # the lowest value accepted now (a commissioned reserve)
            row["floor"] = self._effective_floor(inv, desc, words)
        reason = self._feature_lock(inv, desc) or self._precondition(desc, words, cap, inv)
        if reason is not None:
            row["state"], row["reason"] = "locked", reason
        if desc.report_registers:  # context only: shown, never validated or written
            row["companions"] = self._companion_view(desc, words)
        if desc.branch or desc.upper or desc.lower:
            row["range"] = self._dynamic_range(desc, words, cap)
        if desc.options:  # an option that cannot be chosen now is shown with its reason
            row["options"] = {o.slug: self._option_lock(o, words, inv) for o in desc.options}
        return row

    @staticmethod
    def _dynamic_range(
        desc: WriteDescriptor, words: Mapping[int, int], cap: int | None
    ) -> list[Any] | None:
        """[min, max] in engineering units as the fresh companions allow it now (``None``: the
        range is empty or a companion is not a legal word)."""
        try:
            low, high = desc.limits(cap)
        except Refused:
            return None
        if desc.branch is not None:
            span = desc.branch.limits(words)
            if span is None:
                return None
            low, high = max(low, span[0]), min(high, span[1])
        for bound in desc.upper + desc.lower:
            other = BY_KEY[bound.key]
            value = other.field_of(words[other.register])
            other_cap = limit_from_words(other, words) if other.model_limited else None
            if not other.incumbent_valid(value, other_cap):
                return None
            if bound in desc.upper:
                high = min(high, value + bound.offset)
            else:
                low = max(low, value + bound.offset)
        if low > high:
            return None
        ends = (low, high) if desc.scale > 0 else (high, low)
        return [desc.display(ends[0]), desc.display(ends[1])]

    @staticmethod
    def _feature_lock(inv: InverterRuntime, desc: WriteDescriptor) -> str | None:
        """The installation description marks the setting as not applicable (shown, locked)."""
        for gate in desc.feature_gates:
            if getattr(inv.features, gate.feature) in gate.blocked:
                return gate.reason
        return None

    def _option_lock(
        self, option: Option, words: Mapping[int, int], inv: InverterRuntime
    ) -> str | None:
        """Why one option of a select cannot be chosen right now (``None`` = it can)."""
        if not option.writable:
            return "option_not_writable"
        if option.feature is not None:
            name, wanted, reason = option.feature
            if getattr(inv.features, name) != wanted:
                return reason
        if option.gate is not None and not option.gate.open(words):
            return option.gate.reason
        if option.guard is not None:
            closed: str | None = getattr(self, f"_option_guard_{option.guard}")(inv, option, words)
            return closed
        return None

    @staticmethod
    def _effective_cap(
        inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> int | None:
        """The maximum accepted now: the model limit (A / W) intersected with the per-installation
        limit of the installation description (``desc.envelope``) and the documented maximum,
        aligned down to the step. ``None`` while a required input is missing (model unknown, or
        the installation limit not entered: ``_precondition`` names which)."""
        model = limit_from_words(desc, words)
        if desc.envelope is None:
            return model
        entered = getattr(inv.features, desc.envelope)
        if entered is None or (desc.power_cap and model is None):
            return None
        top: int = min(entered, desc.max_raw, *(() if model is None else (model,)))
        return top - (top - desc.min_raw) % desc.step_raw if top >= desc.min_raw else top

    @staticmethod
    def _incumbent_cap(desc: WriteDescriptor, words: Mapping[int, int]) -> int | None:
        """The cap an EXISTING word is validated against: the model limit only, so a stored value
        above the installation limit stays readable (and can be lowered)."""
        model = limit_from_words(desc, words)
        if desc.envelope is None:
            return model
        return desc.max_raw if model is None else min(model, desc.max_raw)

    def _precondition(
        self,
        desc: WriteDescriptor,
        words: Mapping[int, int],
        cap: int | None,
        inv: InverterRuntime,
    ) -> str | None:
        """First reason this operation is locked right now (``None`` = open)."""
        if desc.envelope is not None and getattr(inv.features, desc.envelope) is None:
            return "envelope_missing"
        if desc.model_limited and cap is None:
            return "model_unknown"
        if desc.envelope is not None and cap is not None and cap < desc.min_raw:
            return "envelope_too_low"
        if desc.needs_smart_load and words.get(R133) != GEN_SMART_LOAD:
            return "gen_not_smart_load"
        if desc.tou and words.get(R146, 0) & R146_SPAIN_BIT:
            return "spain_mode"
        for gate in desc.gates:
            if not gate.open(words):
                return gate.reason
        if desc.requires_r111 is not None and words.get(R111) != desc.requires_r111:
            return (
                "soc_mode_required" if desc.requires_r111 == R111_SOC else "voltage_mode_required"
            )
        if desc.branch is not None and desc.branch.limits(words) is None:
            return desc.branch.reason
        word = words[desc.register]
        if desc.whole_word_bool and word not in (0, 1):
            return "unexpected_bits"
        if desc.reject_sentinel and word == SENTINEL:
            return "unexpected_bits"
        if not desc.incumbent_valid(desc.field_of(word), self._incumbent_cap(desc, words)):
            return "existing_value_outside_range"
        return self._guard_lock(inv, desc, words)

    def _evaluate(
        self,
        inv: InverterRuntime,
        desc: WriteDescriptor,
        field_new: int,
        words: Mapping[int, int],
    ) -> tuple[int | None, int, int]:
        """Every value and state gate of one operation against the fresh ``words`` (the same code
        at the preview and at the send point): battery, model limit, R111/R133/R146 gates, range,
        order against the companions, encoding. Returns the model limit, old word and new word."""
        if desc.needs_battery and not inv.features.battery:
            raise Refused("no_battery", 409)
        cap = self._effective_cap(inv, desc, words)
        locked = self._feature_lock(inv, desc) or self._precondition(desc, words, cap, inv)
        if locked is not None:
            raise Refused("operation_locked", 409, reason=locked)
        check_range(desc, field_new, cap)
        if desc.branch is not None and (span := desc.branch.limits(words)) is not None:
            low, high = span
            if not low <= field_new <= high:
                raise Refused("value_out_of_range", low=desc.display(low), high=desc.display(high))
        chosen = desc.option_of(field_new)
        if (
            chosen is not None
            and (option_lock := self._option_lock(chosen, words, inv)) is not None
        ):
            raise Refused("operation_locked", 409, reason=option_lock)
        if desc.chain:
            self._check_group(desc, field_new, words, cap)
        self._check_bounds(desc, field_new, words)
        self._check_children(desc, field_new, words)
        self._check_mode_order(desc, field_new, words)
        self._check_gen_transition(desc, field_new, words)
        self._check_transition(inv, desc, field_new, words)
        old_word = words[desc.register]
        new_word = desc.encode(old_word, field_new)
        if new_word == old_word:
            raise Refused("no_change", 409)
        return cap, old_word, new_word

    @staticmethod
    def _check_bounds(desc: WriteDescriptor, field_new: int, words: Mapping[int, int]) -> None:
        """Dynamic limits from fresh companions (R128 <= R108, a float voltage 2 V above the
        start threshold ...). A companion that is not a legal word of its own setting refuses."""
        for bound, upper in [(b, True) for b in desc.upper] + [(b, False) for b in desc.lower]:
            other = BY_KEY[bound.key]
            value = other.field_of(words[other.register])
            other_cap = limit_from_words(other, words) if other.model_limited else None
            if not other.incumbent_valid(value, other_cap):
                raise Refused(
                    "companion_invalid",
                    409,
                    companion=other.key,
                    companion_value=other.display(value),
                )
            limit = value + bound.offset
            if (upper and field_new > limit) or (not upper and field_new < limit):
                raise Refused(
                    "bound_max" if upper else "bound_min",
                    422,
                    companion=other.key,
                    companion_value=other.display(value),
                    limit=desc.display(limit),
                )

    @staticmethod
    def _check_children(desc: WriteDescriptor, field_new: int, words: Mapping[int, int]) -> None:
        """The other side of ``_check_bounds``: a parent (R108, R101, R120 ...) may not move so
        that a legal child (R128, R123 / R126, a ToU voltage ...) is left outside its bound. The
        child is rejected, never reduced. A child that is not a legal word is locked on its own."""
        for child, bound, upper in BOUND_CHILDREN.get(desc.key, ()):
            value = child.field_of(words[child.register])
            try:
                cap = limit_from_words(child, words) if child.model_limited else None
                if not child.incumbent_valid(value, cap):
                    continue
            except Refused:
                continue
            limit = field_new + bound.offset
            if (upper and value > limit) or (not upper and value < limit):
                raise Refused(
                    "bound_child",
                    422,
                    child=child.key,
                    child_value=child.display(value),
                    limit=child.display(limit),
                )

    # -- contextual guards (descriptor ``guards`` / ``transition`` / option ``guard``) -------------

    def _guard_lock(
        self, inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> str | None:
        """First reason a named contextual guard of ``desc`` closes it right now."""
        for name in desc.guards:
            reason: str | None = getattr(self, f"_guard_{name}")(inv, desc, words)
            if reason is not None:
                return reason
        return None

    @staticmethod
    def _guard_gen_source(
        inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> str | None:
        """R190: a positively mapped generator source. Mapped: a generator described on the GEN
        port with fresh R133 = 0 and the generator input selector R189 = 0 (GEN port). R189 = 1
        selects the generator on the GRID terminal and an unknown word is no selection at all:
        both are unavailable. A generator described on the GRID terminal is not mapped (the
        operating branch of R190 with the generator on GRID is not proved): unavailable."""
        features = inv.features
        if features.gen_port_mode != "generator":
            return (
                "gen_source_unresolved"
                if features.grid_port_use == "generator"
                else "gen_source_not_described"
            )
        if words.get(R133) != 0:
            return "gen_port_not_generator"
        selector = words.get(R189, SENTINEL)
        if selector == 1:
            return "gen_source_on_grid"
        if selector != 0:
            return "gen_source_selector_unknown"
        return None

    @staticmethod
    def _guard_ev_charger(
        inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> str | None:
        if inv.features.ev_charger not in ("grid_port", "load_port"):
            return "ev_charger_not_described"
        return None

    @classmethod
    def _guard_ev_context(
        cls, inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> str | None:
        """An EV setting needs a described charger whose port matches the configured location."""
        reason = cls._guard_ev_charger(inv, desc, words)
        if reason is not None:
            return reason
        wanted = 1 if inv.features.ev_charger == "grid_port" else 2
        return None if (words[259] & 0x0030) >> 4 == wanted else "ev_location_mismatch"

    @classmethod
    def _tou_problem(cls, words: Mapping[int, int]) -> str | None:
        """Is the ToU context valid: a plausible device clock, a known battery basis and a
        schedule of valid, strictly ascending start times?"""
        if cls._tou_info(words)["clock"] is None:
            return "tou_clock_invalid"
        if words.get(R111) not in (0, 1):
            return "tou_basis_invalid"
        starts = []
        for n in range(1, TOU_SLOTS + 1):
            slot = BY_KEY[f"tou_slot{n}_start"]
            value = slot.field_of(words[slot.register])
            if not slot.incumbent_valid(value):
                return "tou_schedule_invalid"
            starts.append(value)
        if any(a >= b for a, b in pairwise(starts)):  # strictly ascending
            return "tou_schedule_invalid"
        return None

    @classmethod
    def _guard_tou_context(
        cls, inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> str | None:
        return cls._tou_problem(words)

    def _check_transition(
        self, inv: InverterRuntime, desc: WriteDescriptor, field_new: int, words: Mapping[int, int]
    ) -> None:
        """Target-dependent prerequisites, evaluated at the preview and at the send point."""
        if desc.transition is not None:
            getattr(self, f"_transition_{desc.transition}")(inv, desc, field_new, words)

    @classmethod
    def _transition_spain_on(
        cls, inv: InverterRuntime, desc: WriteDescriptor, field_new: int, words: Mapping[int, int]
    ) -> None:
        """Spain mode ON activates a different branch: valid clock, basis and schedule. OFF is
        always possible."""
        if field_new == 1 and (problem := cls._tou_problem(words)) is not None:
            raise Refused("operation_locked", 409, reason=problem)

    @staticmethod
    def _legal_word(key: str, words: Mapping[int, int]) -> int | None:
        """The field of a companion setting if it is a legal word of that setting (model limit
        only), else ``None``."""
        other = BY_KEY[key]
        value = other.field_of(words[other.register])
        try:
            return (
                value
                if other.incumbent_valid(value, WriteService._incumbent_cap(other, words))
                else None
            )
        except Refused:
            return None

    @classmethod
    def _transition_solar_sell(
        cls, inv: InverterRuntime, desc: WriteDescriptor, field_new: int, words: Mapping[int, int]
    ) -> None:
        """Solar Sell ON needs the commissioned export allowance (installation description), a
        valid export limit R143 within it and a valid PV sale power R340 within its envelope."""
        if field_new == 1 and (problem := cls._sale_problem(inv, words)) is not None:
            raise Refused("operation_locked", 409, reason=problem[0], **problem[1])

    @classmethod
    def _sale_problem(
        cls, inv: InverterRuntime, words: Mapping[int, int]
    ) -> tuple[str, dict[str, Any]] | None:
        """What active sales arm: the export limit R143 and the PV sale power R340."""
        return cls._export_limit_problem(inv, words) or cls._activation_problem(
            inv, "pv_sale_max_power", words
        )

    @classmethod
    def _transition_ev_free_work(
        cls, inv: InverterRuntime, desc: WriteDescriptor, field_new: int, words: Mapping[int, int]
    ) -> None:
        """EV free work ON arms the stored charger limit R260 and the stored off-grid SOC: the
        power must be within its envelope, the SOC at or above the commissioned reserve and the
        battery low SOC R117 (never repaired). OFF is always possible."""
        if field_new != desc.on_field:
            return
        problem = cls._activation_problem(inv, "ev_max_power", words)
        if problem is None:
            problem = cls._ev_reserve_problem(inv, words)
        if problem is not None:
            raise Refused("operation_locked", 409, reason=problem[0], **problem[1])

    @classmethod
    def _transition_ev_location(
        cls, inv: InverterRuntime, desc: WriteDescriptor, field_new: int, words: Mapping[int, int]
    ) -> None:
        """With free work already ON, a new port redirects an active setting: the same activation
        checks as for free work ON, against the prospective word. OFF stays untouched."""
        if (words[259] & 0x0003) != BY_KEY["ev_free_work"].on_field:
            return
        prospective = {**words, 259: desc.encode(words[259], field_new)}
        cls._transition_ev_free_work(inv, BY_KEY["ev_free_work"], 2, prospective)

    @classmethod
    def _ev_reserve_problem(
        cls, inv: InverterRuntime, words: Mapping[int, int]
    ) -> tuple[str, dict[str, Any]] | None:
        soc = BY_KEY["ev_offgrid_soc"]
        reserve: int | None = getattr(inv.features, soc.floor_feature or "")
        if reserve is None:
            return "ev_reserve_missing", {}
        battery = cls._legal_word("battery_low_soc", words)
        if battery is None:  # an invalid additional bound is never dropped or guessed
            return "companion_invalid", {"companion": "battery_low_soc"}
        floor = max(reserve, battery)
        value = cls._legal_word("ev_offgrid_soc", words)
        if value is None:
            return "companion_invalid", {"companion": "ev_offgrid_soc"}
        if value < floor:
            return "ev_soc_below_reserve", {
                "companion": "ev_offgrid_soc",
                "companion_value": value,
                "limit": floor,
            }
        return None

    @classmethod
    def _export_limit_problem(
        cls, inv: InverterRuntime, words: Mapping[int, int]
    ) -> tuple[str, dict[str, Any]] | None:
        allowance = inv.features.export_limit_w
        if allowance is None:
            return "envelope_missing", {}
        if allowance == 0:  # an explicit "no export": never arms selling
            return "export_allowance_zero", {}
        limit = cls._legal_word("export_max_power", words)
        if limit is None:
            return "export_limit_invalid", {"companion": "export_max_power"}
        if limit > allowance:
            return "export_limit_exceeded", {"companion": "export_max_power", "limit": allowance}
        return None

    @classmethod
    def _peak_context_problem(
        cls, inv: InverterRuntime, words: Mapping[int, int]
    ) -> tuple[str, dict[str, Any]] | None:
        """Mode changes arm the peak-shaving powers that R178 has enabled: a pair is OFF (2), ON
        (3) or unknown (00 / 01 / FFFF, never "off"); an enabled power must pass its activation
        check."""
        r178 = words.get(178, SENTINEL)
        if r178 == SENTINEL:
            return "peak_context_unknown", {"companion": "peak_shaving"}
        for shift, key in ((2, "generator_peak_shaving_power"), (4, "grid_peak_shaving_power")):
            pair = (r178 >> shift) & 3
            if pair not in (2, 3):
                return "peak_context_unknown", {"companion": "peak_shaving"}
            if pair == 3 and (problem := cls._activation_problem(inv, key, words)):
                return problem
        return None

    @classmethod
    def _activation_problem(
        cls, inv: InverterRuntime, key: str, words: Mapping[int, int]
    ) -> tuple[str, dict[str, Any]] | None:
        """What an enable arms: the parameter ``key`` must be a legal word within its EFFECTIVE
        installation envelope (fresh words and the saved limit), and its source conditions must
        hold. A stored over-limit value stays readable and lowerable, but is not newly armed."""
        desc = BY_KEY[key]
        if (reason := cls._feature_lock(inv, desc)) is not None:
            return reason, {"companion": key}
        if desc.envelope is not None and getattr(inv.features, desc.envelope) is None:
            return "envelope_missing", {"companion": key}
        for name in desc.guards:
            if (reason := getattr(cls, f"_guard_{name}")(inv, desc, words)) is not None:
                return reason, {"companion": key}
        value = cls._legal_word(key, words)
        if value is None:
            return "companion_invalid", {"companion": key}
        cap = cls._effective_cap(inv, desc, words)
        if cap is None or cap < desc.min_raw:
            return "envelope_too_low", {"companion": key}
        if value > cap:
            return "peak_power_exceeds_envelope", {
                "companion": key,
                "companion_value": desc.display(value),
                "limit": desc.display(cap),
            }
        return None

    @classmethod
    def _transition_peak_shaving_on(
        cls, inv: InverterRuntime, desc: WriteDescriptor, field_new: int, words: Mapping[int, int]
    ) -> None:
        """Enabling a peak-shaving pair arms its power R190 / R191: validated like a direct write
        of that power. OFF (and the pair's other value) is always possible."""
        if field_new != desc.on_field:
            return
        key = f"{desc.key}_power"
        if (problem := cls._activation_problem(inv, key, words)) is not None:
            raise Refused("operation_locked", 409, reason=problem[0], **problem[1])

    @classmethod
    def _option_guard_export_target(
        cls, inv: InverterRuntime, option: Option, words: Mapping[int, int]
    ) -> str | None:
        """R142: what the target mode makes active must be valid NOW (never repaired): R145 as a
        legal whole word, the export limit R143 where sales are or become active, an enabled
        peak-shaving power, and for external zero-export a described, matching measurement path."""
        problem = cls._export_target_problem(inv, option.field, words)
        return None if problem is None else problem[0]

    @classmethod
    def _export_target_problem(
        cls, inv: InverterRuntime, target: int, words: Mapping[int, int]
    ) -> tuple[str, dict[str, Any]] | None:
        if words[145] not in (0, 1):
            return "companion_invalid", {"companion": "pv_sale_enable"}
        problem = cls._peak_context_problem(inv, words)
        if problem is not None:
            return problem
        if target == 0 or words[145] == 1:  # sales are or become active: R143 within the allowance
            problem = cls._sale_problem(inv, words)
            if problem is not None:
                return problem
        if target == 2:
            return cls._measurement_problem(inv, words)
        return None

    @staticmethod
    def _measurement_problem(
        inv: InverterRuntime, words: Mapping[int, int]
    ) -> tuple[str, dict[str, Any]] | None:
        """External zero-export (a product policy, not a proof of function): the described path
        (CT / meter, second meter) must equal the configured R344 in the layout established for
        this build (bit 0 meter, bit 1 second meter; any other bit is a layout that is not
        established and is no substitute), and the meter communication warning W4 (R553 bit 3)
        must not be active, read fresh. That the measurement actually works beyond this is not
        verified."""
        described = inv.features.external_measurement
        if described in ("none", "unknown"):
            return "measurement_not_described", {}
        word = words[344]
        if word == SENTINEL:
            return "measurement_path_unreadable", {}
        if word & ~3:
            return "measurement_path_unsupported", {}
        if bool(word & 1) != described.startswith("meter") or bool(word & 2) != described.endswith(
            "second_meter"
        ):
            return "measurement_path_mismatch", {}
        status = words.get(R553, SENTINEL)
        if status == SENTINEL:
            return "measurement_status_unreadable", {}
        if status & W4_METER_COMM:
            return "measurement_fault_active", {}
        return None

    @staticmethod
    def _guard_ev_reserve(
        inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> str | None:
        """The EV off-grid SOC needs the commissioned reserve of the installation description."""
        return "ev_reserve_missing" if inv.features.ev_offgrid_reserve_soc is None else None

    @classmethod
    def _effective_floor(
        cls, inv: InverterRuntime, desc: WriteDescriptor, words: Mapping[int, int]
    ) -> int | None:
        """The lowest EV off-grid SOC accepted now: the commissioned reserve, and as an additional,
        explicit bound the battery low SOC R117 (read fresh, a legal word). ``None`` while the
        reserve is not entered."""
        if desc.floor_feature is None:
            return None
        reserve: int | None = getattr(inv.features, desc.floor_feature)
        if reserve is None:
            return None
        battery = cls._legal_word("battery_low_soc", words)
        return reserve if battery is None else max(reserve, battery)

    @classmethod
    def _transition_ev_soc(
        cls, inv: InverterRuntime, desc: WriteDescriptor, field_new: int, words: Mapping[int, int]
    ) -> None:
        """The EV off-grid SOC may not go below the commissioned EV reserve of the installation
        description, nor below the battery low SOC R117 (an additional, stated bound; read fresh
        and valid). Never silently raised."""
        reserve = inv.features.ev_offgrid_reserve_soc
        if reserve is None:
            raise Refused("operation_locked", 409, reason="ev_reserve_missing")
        battery = cls._legal_word("battery_low_soc", words)
        if battery is None:
            raise Refused(
                "companion_invalid", 409, companion="battery_low_soc", companion_value=None
            )
        if field_new < max(reserve, battery):
            raise Refused(
                "ev_soc_below_reserve",
                422,
                floor=max(reserve, battery),
                reserve=reserve,
                battery_low=battery,
                companion="battery_low_soc",
            )

    @staticmethod
    def _check_gen_transition(
        desc: WriteDescriptor, field_new: int, words: Mapping[int, int]
    ) -> None:
        """R133: before the GEN port takes the role Smart Load (1) or AC coupling (2), the
        thresholds of the newly active mode are read fresh and must already satisfy that mode's
        order for the battery basis R111 (Smart Load: ON >= OFF + margin, AC coupling reversed).
        Nothing is repaired and no other register is written; the LCD repairs do not run."""
        if desc.mode_check != "gen_port" or field_new == 0:
            return
        basis = words.get(R111)
        if basis not in (0, 1):
            raise Refused("mode_companions_invalid", 409, reason="basis_unknown")
        keys = (
            ("smart_load_off_voltage", "smart_load_on_voltage")
            if basis == 0
            else ("smart_load_off_soc", "smart_load_on_soc")
        )
        off, on = BY_KEY[keys[0]], BY_KEY[keys[1]]
        values = []
        for member in (off, on):
            value = member.field_of(words[member.register])
            if not member.incumbent_valid(value):
                raise Refused(
                    "mode_companions_invalid",
                    409,
                    reason="companion_invalid",
                    companion=member.key,
                    companion_value=member.display(value),
                )
            values.append(value)
        margin = on.mode_order.margin if on.mode_order else 0
        ok = (
            values[1] >= values[0] + margin
            if field_new == GEN_SMART_LOAD
            else values[0] >= values[1] + margin
        )
        if not ok:
            raise Refused(
                "mode_threshold_order",
                422,
                mode="smart_load" if field_new == GEN_SMART_LOAD else "ac_coupled",
                off=off.display(values[0]),
                on=on.display(values[1]),
            )

    @staticmethod
    def _check_mode_order(desc: WriteDescriptor, field_new: int, words: Mapping[int, int]) -> None:
        """An ON/OFF pair whose order follows the fresh GEN port usage (R133 = 1 Smart Load: ON >=
        OFF + margin; R133 = 2 AC coupling: reversed). The other member is read fresh and must be
        a legal word of its own setting; it is never repaired."""
        order = desc.mode_order
        if order is None:
            return
        other = BY_KEY[order.companion]
        value = other.field_of(words[other.register])
        if not other.incumbent_valid(value):
            raise Refused(
                "companion_invalid",
                409,
                companion=other.key,
                companion_value=other.display(value),
            )
        upper_me = order.on_member == (words.get(R133) == GEN_SMART_LOAD)
        if upper_me:
            ok, relation = field_new >= value + order.margin, "above"
        else:
            ok, relation = value >= field_new + order.margin, "below"
        if not ok:
            raise Refused(
                "threshold_order",
                422,
                companion=other.key,
                companion_value=other.display(value),
                ordering=relation,
                margin=desc.display(order.margin) if desc.scale == 1 else order.margin / desc.scale,
            )

    @staticmethod
    def _check_group(
        desc: WriteDescriptor, field_new: int, words: Mapping[int, int], cap: int | None
    ) -> None:
        """The whole ordered group (all three SOC thresholds, all six ToU starts, an ON/OFF pair)
        as it would stand after this edit: every member read fresh and valid in its own domain, the
        result strictly ascending. No neighbour-only repair: a group that is broken elsewhere is
        refused until each edit produces a valid group (or the owner repairs it on the inverter)."""
        fields: list[int] = []
        for key in desc.chain:
            other = BY_KEY[key]
            if key == desc.key:
                fields.append(field_new)
                continue
            value = other.field_of(words[other.register])
            if not other.incumbent_valid(value, cap if other.model_limited else None):
                raise Refused(
                    "companion_invalid",
                    409,
                    companion=other.key,
                    companion_value=other.display(value),
                )
            fields.append(value)
        me = desc.chain.index(desc.key)
        pairs = [(i, i + 1) for i in range(len(fields) - 1)]
        # the pairs next to the edited member first: they explain the refusal best
        pairs.sort(key=lambda p: me not in p)
        for i, j in pairs:
            if fields[i] > fields[j] or (desc.chain_strict and fields[i] == fields[j]):
                if me in (i, j):
                    near = j if i == me else i
                    relation = "below" if near > me else "above"
                else:
                    near, relation = i, "group"  # broken elsewhere in the group
                other = BY_KEY[desc.chain[near]]
                raise Refused(
                    desc.order_error,
                    422,
                    companion=other.key,
                    companion_value=other.display(fields[near]),
                    ordering=relation,
                )

    def _check_drift(self, inverter_id: str, desc: WriteDescriptor, word: int) -> None:
        """A later fresh read that differs from the value of the last operation on this register
        is recorded once; the setting is never reasserted."""
        for op in reversed(self._ops.values()):
            if op.get("inverter") != inverter_id or op.get("register") != desc.register:
                continue
            if op.get("status") not in (CONFIRMED, UNKNOWN, FAILED):
                continue
            observed = (op.get("target") or {}).get("word")
            if observed is None or observed == word:
                return
            if (op.get("drift") or {}).get("word") != word:
                op["drift"] = {"word": word, "at": self.wall()}
                self._record("drift", op)
            return

    # -- preview ------------------------------------------------------------------------------

    async def preview(
        self, inverter_id: str, key: str, value: Any, *, user_id: str, plant_revision: int
    ) -> dict[str, Any]:
        permit, desc, cap = await self._prepare(
            inverter_id, key, value, source="web", user_id=user_id, plant_revision=plant_revision
        )
        inv = self.runtime.inverters[inverter_id]
        for stale in [p for p in self._permits.values() if p.inverter_id == inverter_id]:
            stale.revoked = True
            del self._permits[stale.operation_id]
        self._expire()
        self._permits[permit.operation_id] = permit
        return self._preview_view(permit, desc, inv, cap)

    async def _prepare(
        self,
        inverter_id: str,
        key: str,
        value: Any,
        *,
        source: str,
        user_id: str,
        plant_revision: int,
        direct: bool = False,
        intent: Intent | None = None,
    ) -> tuple[Permit, WriteDescriptor, int | None]:
        """Validate a request against fresh reads and bind it to the current identity, role,
        route and release. A preview keeps the words it saw (the send point must find them
        unchanged); a direct request (``direct``) keeps none: everything is decided again from the
        reads of the send point."""
        if not self._open:
            raise Refused("writes_stopped", 409)
        desc = BY_KEY.get(key)
        if desc is None:
            raise Refused("unknown_operation", 404)
        inv = self._target(inverter_id)
        if intent is not None:
            self._check_intent(intent)
        if key not in self.enabled(inverter_id):
            raise Refused("writes_not_enabled", 403)
        reason = self._not_ready(inv)
        if reason is not None:
            raise Refused("inverter_not_ready", 409, reason=reason)
        if desc.needs_battery and not inv.features.battery:
            raise Refused("no_battery", 409)
        field_new = parse_value(desc, value)
        words = await self._context(inv, self._addresses(desc))
        serial, firmware = self._identity(inv, words)
        self._check_release(inv, words)
        self._check_role(inv, words, desc)
        peers = await self._peer_baselines(inverter_id, desc)
        missing = [p["id"] for p in peers.values() if p["baseline_word"] is None]
        if missing:
            raise Refused("peer_unavailable", 409, peers=missing)
        noop = False
        try:
            cap, old_word, new_word = self._evaluate(inv, desc, field_new, words)
        except Refused as refusal:
            if not (direct and refusal.key == "no_change"):
                raise
            cap = self._effective_cap(inv, desc, words)
            old_word = new_word = words[desc.register]
            noop = True
        route = inv.routes[0]
        if intent is not None:
            self._check_intent(intent)  # the reads above can take long
        permit = Permit(
            operation_id=secrets.token_hex(16),
            user_id=user_id,
            boot_id=self.boot_id,
            plant_revision=plant_revision,
            inverter_id=inverter_id,
            key=key,
            enabled=self.enabled(inverter_id),
            serial=serial,
            firmware=firmware,
            role=self._role(inv, words),
            endpoint_id=route.endpoint_id,
            unit=route.unit,
            epoch=route.verified_epoch or 0,
            lifecycle=inv.lifecycle_epoch,
            context=None if direct else dict(words),
            field=field_new,
            old_word=old_word if noop or not direct else None,
            new_word=new_word if noop or not direct else None,
            peers=peers,
            expires_at=self.clock() + self.settings.permit_s,
            source=source,
            noop=noop,
            intent=intent,
        )
        return permit, desc, cap

    def _expire(self) -> None:
        now = self.clock()
        for op_id in [i for i, p in self._permits.items() if p.used or now > p.expires_at]:
            if not self._permits[op_id].used:
                self._expired[op_id] = None
            del self._permits[op_id]
        while len(self._expired) > MAX_PERMITS:
            self._expired.popitem(last=False)
        while len(self._permits) > MAX_PERMITS:
            self._permits.popitem(last=False)

    def _preview_view(
        self, permit: Permit, desc: WriteDescriptor, inv: InverterRuntime, cap: int | None
    ) -> dict[str, Any]:
        return {
            "operation_id": permit.operation_id,
            "expires_in_s": self.settings.permit_s,
            "operation": desc.view(),
            "inverter": {
                "id": inv.id,
                "name": inv.cfg.name,
                "serial": permit.serial,
                "role": permit.role,
                "endpoint": permit.endpoint_id,
                "unit": permit.unit,
            },
            "cap": cap if desc.model_limited else None,
            "old_value": None
            if permit.old_word is None
            else desc.display(desc.field_of(permit.old_word)),
            "new_value": desc.display(permit.field),
            "old_word": permit.old_word,
            "new_word": permit.new_word,
            "old_word_hex": _hex(permit.old_word),
            "new_word_hex": _hex(permit.new_word),
            "mask_hex": _hex(desc.mask),
            "peers": [self._peer_view(p, desc) for p in permit.peers.values()],
            "companions": self._companion_view(desc, permit.context),
            "plant_revision": permit.plant_revision,
        }

    @staticmethod
    def _companion_view(
        desc: WriteDescriptor, context: Mapping[int, int] | None
    ) -> list[dict[str, Any]]:
        """The companions a role change affects, as the preview read them (shown, never changed)."""
        registers = desc.mode_registers + desc.report_registers
        if not registers or context is None:
            return []
        by_register = {d.register: d for d in DESCRIPTORS if d.mask == 0xFFFF}
        out = []
        for register in registers:
            word = context.get(register)
            other = by_register.get(register)
            out.append(
                {
                    "register": register,
                    "word": word,
                    "key": None if other is None else other.key,
                    "value": None
                    if other is None or word is None
                    else other.display(other.field_of(word)),
                }
            )
        return out

    @staticmethod
    def _peer_view(peer: Mapping[str, Any], desc: WriteDescriptor) -> dict[str, Any]:
        word = peer.get("baseline_word")
        return {
            "id": peer["id"],
            "name": peer["name"],
            "serial": peer.get("serial"),
            "role": peer.get("role"),
            "baseline_word": word,
            "baseline_word_hex": _hex(word),
            "baseline_value": None if word is None else desc.display(desc.field_of(word)),
        }

    async def _peer_baselines(
        self, target: str, desc: WriteDescriptor
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for peer_id, peer in self.runtime.inverters.items():
            if peer_id == target:
                continue
            word = await self._guarded_word(peer, desc.register)
            out[peer_id] = {
                "id": peer_id,
                "name": peer.cfg.name,
                "serial": peer.book.bound(peer_id),
                "role": peer.identity_info.get("role"),
                "baseline_word": word,
            }
        return out

    # -- confirm ------------------------------------------------------------------------------

    async def confirm(
        self, operation_id: str, *, user_id: str, plant_revision: int
    ) -> dict[str, Any]:
        """Execute the operation of a permit exactly once. Returns the operation record."""
        self._expire_used()
        permit = self._permits.get(operation_id)
        if permit is None:
            if operation_id in self._expired:
                raise Refused("permit_expired", 410)
            if operation_id in self._ops:
                raise Refused("operation_already_used", 409)
            raise Refused("unknown_operation", 404)
        if permit.used:
            raise Refused("operation_already_used", 409)
        if permit.user_id != user_id:
            raise Refused("permit_user_mismatch", 403)
        if permit.plant_revision != plant_revision:
            raise Refused("stale_revision", 409)
        return await self._run(permit)

    async def submit(
        self,
        inverter_id: str,
        key: str,
        value: Any,
        *,
        source: str,
        user_id: str = "",
        plant_revision: int | None = None,
        intent: Intent | None = None,
    ) -> dict[str, Any]:
        """A write request without a human preview (a second transport, for example MQTT). Same
        release, identity, role, gates, ordering, audit, pacing, single FC16 and observation as the
        Web path: nothing is taken from the request except the value; the old word and every
        guard come from the fresh reads at the send point. ``source`` is recorded in the audit.
        An ``intent`` (binding of the receipt, deadline, revocation) is compared again at the
        final send point, after every lock, pacing and bus wait: a change is a rejection, never a
        write."""
        revision = self.plant_revision() if plant_revision is None else plant_revision
        if revision is None:
            raise Refused("writes_stopped", 409)
        permit, _, _ = await self._prepare(
            inverter_id,
            key,
            value,
            source=source,
            user_id=user_id,
            plant_revision=revision,
            direct=True,
            intent=intent,
        )
        if permit.noop:
            return self._noop(permit)
        return await self._run(permit)

    def _noop(self, permit: Permit) -> dict[str, Any]:
        """The request equals the fresh word: an audited result without a send."""
        permit.used = True
        op = self._new_record(permit)
        op["status"], op["reason"] = NOOP, "no_change"
        self._ops[permit.operation_id] = op
        while len(self._ops) > MAX_OPERATIONS:
            self._ops.popitem(last=False)
        self._record("result", op)
        return self.operation_view(op)

    async def _run(self, permit: Permit) -> dict[str, Any]:
        permit.used = True  # single use from here on, whatever happens next
        op = self._new_record(permit)
        self._ops[permit.operation_id] = op
        while len(self._ops) > MAX_OPERATIONS:
            self._ops.popitem(last=False)
        task = asyncio.ensure_future(self._execute(permit, op))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self.operation_view(await asyncio.shield(task))

    def _expire_used(self) -> None:
        self._expire()

    def _new_record(self, permit: Permit) -> dict[str, Any]:
        desc = BY_KEY[permit.key]
        inv = self.runtime.inverters[permit.inverter_id]
        return {
            "operation_id": permit.operation_id,
            "inverter": permit.inverter_id,
            "inverter_name": inv.cfg.name,
            "user": permit.user_id,
            "source": permit.source,
            "boot_id": permit.boot_id,
            "plant_revision": permit.plant_revision,
            "enabled_operations": sorted(permit.enabled),
            "operation": desc.key,
            "register": desc.register,
            "mask": desc.mask,
            "mask_hex": _hex(desc.mask),
            "serial": permit.serial,
            "firmware_words": list(permit.firmware),
            "role": permit.role,
            "route": {"endpoint": permit.endpoint_id, "unit": permit.unit, "epoch": permit.epoch},
            "lifecycle_epoch": permit.lifecycle,
            "old_word": permit.old_word,
            "old_word_hex": _hex(permit.old_word),
            "old_value": None
            if permit.old_word is None
            else desc.display(desc.field_of(permit.old_word)),
            "requested_value": desc.display(permit.field),
            "requested_field": permit.field,
            "new_word": permit.new_word,
            "new_word_hex": _hex(permit.new_word),
            "created_at": self.wall(),
            "status": PENDING,
            "reason": None,
            "response": {},
            "target": None,
            "peers": [],
            "verification": None,
            "drift": None,
            "attribution": None,
            "late_observation": None,
            "peer_rereads": [],
        }

    async def _execute(self, permit: Permit, op: dict[str, Any]) -> dict[str, Any]:
        desc = BY_KEY[permit.key]
        async with self._lock:
            sent = _Sent()
            try:
                await self._pace()
                inv = self.runtime.inverters[permit.inverter_id]
                route = inv.routes[0]
                self._authorise(permit)
                try:
                    sent = await route.scheduler.run(lambda: self._send(permit, op, desc))
                except QueueFull:
                    raise Refused("bus_busy", 409) from None
            except Refused as refusal:
                return self._reject(op, refusal)
            except Exception as exc:  # never lose the record of an operation
                log.error(
                    "write %s failed internally (%s)", permit.operation_id, type(exc).__name__
                )
                if op["status"] == SENT:
                    op["status"], op["reason"] = UNKNOWN, "internal_error"
                    self._record("result", op)
                    return op
                return self._reject(op, Refused("internal_error", 500))
            if not sent.attempted:
                op["status"], op["reason"] = REJECTED, sent.reason
                op["response"] = sent.response
                self._record("result", op)
                return op
            op["status"], op["reason"], op["response"] = sent.status, sent.reason, sent.response
            sent_at = self._last_send if self._last_send is not None else self.clock()
            self._record("result", op)
            await self.sleep(self.settings.observe_delay_s)
            await self._observe(permit, op, desc)
            if op["peers"]:
                self._schedule_late(op, desc, sent_at)
            self._record("result", op)
            return op

    async def _pace(self) -> None:
        if self._last_send is None:
            return
        wait = self.settings.pacing_s - (self.clock() - self._last_send)
        if wait > 0:
            await self.sleep(wait)

    def _reject(self, op: dict[str, Any], refusal: Refused) -> dict[str, Any]:
        op["status"], op["reason"] = REJECTED, refusal.key
        if refusal.key == "operation_locked" and isinstance(refusal.params.get("reason"), str):
            op["reason"] = refusal.params["reason"]  # the gate that closed, not its wrapper
        op["reason_params"] = {k: v for k, v in refusal.params.items() if isinstance(v, str | int)}
        self._record("result", op)
        return op

    def _authorise(self, permit: Permit) -> None:
        """Everything that must still hold at the final send point."""
        if not self._open:
            raise Refused("writes_stopped", 409)
        if permit.revoked:
            raise Refused("permit_revoked", 409)
        if permit.intent is not None:
            self._check_intent(permit.intent)
        if self.clock() > permit.expires_at:
            raise Refused("permit_expired", 410)
        if self.plant_revision() != permit.plant_revision:
            raise Refused("stale_revision", 409)
        inv = self._target(permit.inverter_id)
        if self.enabled(permit.inverter_id) != permit.enabled or permit.key not in permit.enabled:
            raise Refused("writes_not_enabled", 403)
        reason = self._not_ready(inv)
        if reason is not None:
            raise Refused("inverter_not_ready", 409, reason=reason)
        route = inv.routes[0]
        if (
            inv.lifecycle_epoch != permit.lifecycle
            or route.verified_epoch != permit.epoch
            or route.endpoint_id != permit.endpoint_id
            or route.unit != permit.unit
            or inv.seen_serial != permit.serial
        ):
            raise Refused("binding_changed", 409)

    async def _send(self, permit: Permit, op: dict[str, Any], desc: WriteDescriptor) -> _Sent:
        """The bus critical section: re-read, compare, audit intent, ONE write."""
        inv = self.runtime.inverters[permit.inverter_id]
        route = inv.routes[0]
        try:
            words = await self._read_context(route, self._addresses(desc))
        except READ_ERRORS as exc:
            raise Refused("read_failed", 409, error=type(exc).__name__) from None
        self._check_release(inv, words)
        self._check_role(inv, words, desc)  # before the comparison: a role change is its own reason
        serial, _ = self._identity(inv, words)
        if serial != permit.serial:
            raise Refused("binding_changed", 409)
        if permit.intent is not None:
            bound = permit.intent.binding
            fresh_role = self._role(inv, words)
            if tuple(words[a] for a in FINGERPRINT_ADDRESSES) != bound.firmware or (
                bound.role is not None
                and (fresh_role["parallel"], fresh_role["master"]) != bound.role
            ):
                raise Refused(SUPERSEDED, 409)
        if permit.context is not None and words != permit.context:
            raise Refused("conflict", 409)  # the preview's reads are no longer true
        _, fresh, new = self._evaluate(inv, desc, permit.field, words)
        if permit.context is not None and new != permit.new_word:
            raise Refused("conflict", 409)
        if permit.context is None:  # no preview: the words are those of this critical section
            self._bind_words(permit, op, desc, fresh, new)
        if desc.fault_probe is not None:  # the fault word as it stood before the command
            op["fault_before"] = {
                "register": desc.fault_probe[0],
                "word": words.get(desc.fault_probe[0]),
            }
        self._authorise(permit)
        grant = WriteGrant(
            unit=route.unit,
            register=desc.register,
            mask=desc.mask,
            old_word=fresh,
            new_word=new,
            command_word=desc.command_word,
        )
        op["status"] = SENT
        if not self._record("intent", op):
            op["status"] = PENDING
            raise Refused("audit_unavailable", 503)
        transport = cast(WriteTransport, route.transport)
        result = _Sent()
        try:
            ack = await transport.write_register(grant)
        except ModbusExceptionResponse as exc:
            result.attempted, result.status, result.reason = True, FAILED, "device_exception"
            result.response = {
                "kind": "exception",
                "exception_code": exc.code,
                "raw": exc.raw.hex(),
            }
        except WriteNotSent as exc:
            result.status, result.reason = REJECTED, f"not_sent:{exc}"
        except (WriteIndeterminate, TransportError, ModbusError, V5LoggerError) as exc:
            result.attempted, result.status = True, UNKNOWN
            result.reason = f"no_valid_acknowledgement:{type(exc).__name__}"
            result.response = {"kind": "none"}
        else:
            result.attempted, result.status = True, SENT
            result.response = self._ack_view(ack)
        if result.attempted:
            self._last_send = self.clock()
        return result

    @staticmethod
    def _bind_words(
        permit: Permit, op: dict[str, Any], desc: WriteDescriptor, old: int, new: int
    ) -> None:
        permit.old_word, permit.new_word = old, new
        op["old_word"], op["old_word_hex"] = old, _hex(old)
        op["old_value"] = desc.display(desc.field_of(old))
        op["new_word"], op["new_word_hex"] = new, _hex(new)

    @staticmethod
    def _ack_view(ack: WriteAck) -> dict[str, Any]:
        return {"kind": "ack", "start": ack.start, "count": ack.count, "raw": ack.raw.hex()}

    # -- observation --------------------------------------------------------------------------

    async def _observe(self, permit: Permit, op: dict[str, Any], desc: WriteDescriptor) -> None:
        inv = self.runtime.inverters[permit.inverter_id]
        word = await self._observed_word(inv, desc.register)
        field_seen = None if word is None else desc.field_of(word)
        op["target"] = {
            "word": word,
            "word_hex": _hex(word),
            "field": field_seen,
            "value": None if field_seen is None else desc.display(field_seen),
            "matches_request": self._matches_request(desc, word, permit),
        }
        if desc.accepted_after:  # a command: acknowledged / stored request / consumption unknown
            op["target"]["command_state"] = self._command_state(desc, word, permit)
        if desc.fault_probe is not None:  # reported next to, never instead of, the acceptance
            reg, mask = desc.fault_probe
            fault = await self._observed_word(inv, reg)
            before = (op.get("fault_before") or {}).get("word")
            op["fault"] = {
                "register": reg,
                "mask": mask,
                "word_before": before,
                "word": fault,
                "state": self._fault_verdict(before, fault, mask),
            }
        if op["status"] == SENT:  # a valid acknowledgement arrived
            matched = word is not None and self._accepted(desc, word, permit)
            op["status"] = CONFIRMED if matched else UNKNOWN
            op["reason"] = (
                None
                if matched
                else (
                    "consumption_not_established"
                    if desc.accepted_after
                    and self._command_state(desc, word, permit) == "consumption_unknown"
                    else "readback_missing_or_mismatch"
                )
            )
        elif op["status"] == UNKNOWN:
            # a later matching value is recorded but does not show that this command caused it
            op["attribution"] = "not_established"
        peers = []
        for peer_id, baseline in permit.peers.items():
            seen = await self._peer_read(peer_id, baseline.get("serial"), desc, permit.field)
            later = seen["word"]
            base = baseline["baseline_word"]
            peers.append(
                {
                    "id": peer_id,
                    "name": baseline["name"],
                    "serial": baseline.get("serial"),
                    "baseline_word": base,
                    "baseline_value": None if base is None else desc.display(desc.field_of(base)),
                    "later_word": later,
                    "later_value": None if later is None else desc.display(desc.field_of(later)),
                    "state": seen["state"],
                    "reason": seen["reason"],
                    "expected_serial": seen["expected_serial"],
                    "observed_serial": seen["observed_serial"],
                    "changed": None if later is None or base is None else later != base,
                }
            )
        op["peers"] = peers

    @staticmethod
    def _accepted(desc: WriteDescriptor, word: int, permit: Permit) -> bool:
        """Does the read-back word equal the request? Only then is a stored request observed. A
        command (R181 clear) that the firmware already normalised (2 to 1) shows nothing: the
        unchanged 1 is also what an ignored command leaves behind."""
        return word == permit.new_word

    @classmethod
    def _matches_request(
        cls, desc: WriteDescriptor, word: int | None, permit: Permit
    ) -> bool | None:
        if word is None:
            return None
        if cls._accepted(desc, word, permit):
            return True
        return None if cls._command_state(desc, word, permit) == "consumption_unknown" else False

    @staticmethod
    def _command_state(desc: WriteDescriptor, word: int | None, permit: Permit) -> str | None:
        """Transport acknowledgement, observed stored request and unknown consumption are separate
        facts: a read-back equal to the request is a stored request; the normalised word cannot tell
        a consumed command from an ignored one."""
        if word is None:
            return "readback_missing"
        if word == permit.new_word:
            return "request_stored"
        return "consumption_unknown" if desc.field_of(word) in desc.accepted_after else "mismatch"

    @staticmethod
    def _fault_verdict(before: int | None, after: int | None, mask: int) -> str:
        """The reported fault bit around a command: only a 1 to 0 change is "no longer reported"
        (0 to 0 is "not reported"); a missing read before or after leaves it unknown."""
        if after is None:
            return "unknown"
        if after & mask:
            return "still_reported"
        if before is None:
            return "unknown"
        return "no_longer_reported" if before & mask else "not_reported"

    async def _peer_read(
        self, peer_id: str, expected: str | None, desc: WriteDescriptor, field_requested: int
    ) -> dict[str, Any]:
        """One read-only peer look bound to the serial saved at preview: another device behind
        the same id is ``unavailable`` / ``identity_changed`` and nothing is read or compared."""
        inv = self.runtime.inverters.get(peer_id)
        observed = None if inv is None else inv.book.bound(peer_id)
        changed = expected is not None and observed != expected
        word = (
            None
            if inv is None or changed
            else await self._observed_word(inv, desc.register, expected)
        )
        return {
            "word": word,
            "state": self._peer_state(desc, word, field_requested),
            "reason": "identity_changed" if changed else None,
            "expected_serial": expected,
            "observed_serial": observed,
        }

    @staticmethod
    def _peer_state(desc: WriteDescriptor, word: int | None, field_requested: int) -> str:
        if word is None:
            return "unavailable"
        return "same" if desc.field_of(word) == field_requested else "different"

    async def _peer_reads(
        self, op: Mapping[str, Any], desc: WriteDescriptor
    ) -> list[dict[str, Any]]:
        """A fresh read-only observation of the peers recorded with ``op`` (never a write). Each
        read is bound to the peer serial saved in the operation: another device behind the same
        id is ``unavailable`` / ``identity_changed``, its value is never compared."""
        out = []
        for peer in op["peers"]:
            seen = await self._peer_read(
                peer["id"], peer.get("serial"), desc, op["requested_field"]
            )
            word = seen["word"]
            out.append(
                {
                    "id": peer["id"],
                    "value": None if word is None else desc.display(desc.field_of(word)),
                    **seen,
                }
            )
        return out

    async def _late_observe(
        self, op: dict[str, Any], desc: WriteDescriptor, delay: float, sent_at: float
    ) -> None:
        """The second peer observation, a read-only look at a later moment: it shows whether the
        peers followed, it does not say why."""
        late = op["late_observation"]
        try:
            await self.sleep(delay)
            if not self._open:
                late["status"] = LATE_INTERRUPTED
            else:
                reads = {r["id"]: r for r in await self._peer_reads(op, desc)}
                for peer in op["peers"]:
                    read = reads[peer["id"]]
                    peer["late_word"], peer["late_value"] = read["word"], read["value"]
                    peer["late_state"] = read["state"]
                    peer["late_reason"] = read["reason"]
                    peer["late_expected_serial"] = read["expected_serial"]
                    peer["late_observed_serial"] = read["observed_serial"]
                late["status"], late["at"] = LATE_DONE, self.wall()
                late["observed_after_s"] = round(self.clock() - sent_at, 1)
        except asyncio.CancelledError:
            late["status"] = LATE_INTERRUPTED
            raise
        except Exception as exc:  # the first observation and the record stay valid
            log.error("late observation of %s failed (%s)", op["operation_id"], type(exc).__name__)
            late["status"] = LATE_INTERRUPTED
        finally:
            self._record("late_observation", op)

    def _schedule_late(self, op: dict[str, Any], desc: WriteDescriptor, sent_at: float) -> None:
        """Due ``late_observe_delay_s`` after the send, however long the first look took."""
        delay = max(0.0, self.settings.late_observe_delay_s - (self.clock() - sent_at))
        op["late_observation"] = {
            "status": LATE_PENDING,
            "after_s": self.settings.late_observe_delay_s,
            "due_at": self.wall() + delay,
            "at": None,
        }
        task = asyncio.ensure_future(self._late_observe(op, desc, delay, sent_at))
        self._late.add(task)
        task.add_done_callback(self._late.discard)

    async def observe_again(self, operation_id: str) -> dict[str, Any]:
        """Owner request: read the other inverters again (read-only), appended to the audit."""
        if not self._open:
            raise Refused("writes_stopped", 409)
        op = self._ops.get(operation_id)
        if op is None:
            raise Refused("unknown_operation", 404)
        if not op.get("peers") or op.get("status") in (REJECTED, PENDING):
            raise Refused("nothing_to_observe", 409)
        desc = BY_KEY[op["operation"]]
        reads = await self._peer_reads(op, desc)
        rereads = op.setdefault("peer_rereads", [])
        rereads.append({"at": self.wall(), "peers": reads})
        del rereads[:-MAX_REREADS]
        self._record("peer_reread", op)
        return self.operation_view(op)

    # -- owner verification -------------------------------------------------------------------

    def verify(self, operation_id: str, state: str, note: str, user_id: str) -> dict[str, Any]:
        """The owner's own check (display / effect), recorded separately from the read-back."""
        op = self._ops.get(operation_id)
        if op is None:
            raise Refused("unknown_operation", 404)
        if state not in ("confirmed", "not_confirmed"):
            raise Refused("invalid_request", 422)
        op["verification"] = {
            "state": state,
            "note": note[:200],
            "user": user_id,
            "at": self.wall(),
        }
        self._record("verification", op)
        return self.operation_view(op)
