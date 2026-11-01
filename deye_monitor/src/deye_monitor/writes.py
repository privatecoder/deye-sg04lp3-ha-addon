"""Write layer v1 (SPEC §10): previews, single-use permits, one guarded write, audit and peers.

One ``WriteService`` belongs to one runtime generation (a plant change builds a new one, so every
permit dies with its generation). The flow is explicit and owner driven; nothing here runs on its
own (no startup, reconnect, schedule or MQTT trigger):

1. ``preview`` reads the target (fresh, on the primary route, under the bus lock) together with
   everything the preconditions need, snapshots the same register of every other plant inverter
   and issues a single-use operation id with a short-lived permit bound to user, plant revision,
   enablement, inverter, serial, firmware words, role, route, unit and connection epoch.
2. ``confirm`` serialises (one plant write at a time, at least ``pacing_s`` between writes),
   re-checks the authorisation at the final send point, re-reads the whole context inside ONE
   bus critical section (any change since the preview is a conflict), appends the audit intent
   (an unavailable audit log blocks the send) and sends exactly one FC16 with one register
   through the write guard. No retry, no FC06, no fallback endpoint, no fan-out, no rollback.
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
import json
import logging
import os
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
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
    BY_KEY,
    DESCRIPTORS,
    HARDWARE,
    LATE_OBSERVE_DELAY_S,
    OBSERVE_DELAY_S,
    PACING_S,
    PERMIT_S,
    Refused,
    WriteDescriptor,
    check_range,
    model_cap,
    parse_value,
)

if TYPE_CHECKING:
    from deye_monitor.runtime import Runtime

log = logging.getLogger(__name__)

AUDIT_FILE = "write-audit.jsonl"
AUDIT_MAX_BYTES = 8 * 1024 * 1024
MAX_OPERATIONS = 500
MAX_PERMITS = 32

REJECTED = "rejected"
SENT = "sent"
FAILED = "failed"
CONFIRMED = "readback_confirmed"
UNKNOWN = "outcome_unknown"
PENDING = "pending"  # accepted for execution, not yet at the send point (in memory only)
STATUSES = (REJECTED, SENT, FAILED, CONFIRMED, UNKNOWN)

SERIAL_START = 3
SERIAL_WORDS = 5
FINGERPRINT_ADDRESSES = (11, 14, 15, 17, 18)  # board versions: HMI, MCU2, MCU1 words
IDENTITY_ADDRESSES = (3, 4, 5, 6, 7, 11, 14, 15, 17, 18, 20, 21)  # serial, firmware, rated power
IDENTITY_SPAN = (3, 21)
R133 = 133  # GEN port usage
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
    context: dict[int, int]
    field: int
    old_word: int
    new_word: int
    peers: dict[str, dict[str, Any]]
    expires_at: float
    used: bool = False
    revoked: bool = False


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
        for address in sorted(addresses - set(IDENTITY_ADDRESSES)):
            words[address] = (await self._read_span(route, address, 1))[0]
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
        if desc.needs_smart_load:
            out.add(R133)
        if desc.companion:
            out.add(BY_KEY[desc.companion].register)
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
    def _slave_locked(role: Mapping[str, Any]) -> bool:
        """The master distributes the settings block to every slave (a slave's own value would be
        overwritten): only a unit that is known to be a master, or known not to be in a parallel
        system at all, may be written. An unreadable role in an unknown system also locks."""
        parallel = role.get("parallel")
        return parallel is None or (parallel is True and role.get("master") is not True)

    def _check_role(self, inv: InverterRuntime, words: Mapping[int, int]) -> None:
        if self._slave_locked(self._role(inv, words)):
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
        inv = self._target(inverter_id)
        enabled = self.enabled(inverter_id)
        descs = [d for d in DESCRIPTORS if d.key in enabled]
        if not descs:
            return []
        reason = self._not_ready(inv)
        if reason is not None:
            return [self._locked(d, reason) for d in descs]
        addresses: set[int] = set()
        for d in descs:
            addresses |= self._addresses(d)
        try:
            words = await self._context(inv, addresses)
            self._identity(inv, words)
            self._check_release(inv, words)
            self._check_role(inv, words)
        except Refused as exc:
            return [self._locked(d, exc.key) for d in descs]
        cap = model_cap(words[20], words[21])
        out = []
        for d in descs:
            row = self._row(d, words, cap)
            self._check_drift(inverter_id, d, words[d.register])
            out.append(row)
        return out

    def _locked(self, desc: WriteDescriptor, reason: str) -> dict[str, Any]:
        return {"key": desc.key, "state": "locked", "reason": reason}

    def _row(
        self, desc: WriteDescriptor, words: Mapping[int, int], cap: int | None
    ) -> dict[str, Any]:
        field_now = desc.field_of(words[desc.register])
        row: dict[str, Any] = {
            "key": desc.key,
            "state": "ready",
            "word": words[desc.register],
            "field": field_now,
            "value": desc.display(field_now),
            "cap": cap if desc.current_cap else None,
        }
        reason = self._precondition(desc, words, cap)
        if reason is not None:
            row["state"], row["reason"] = "locked", reason
        return row

    def _precondition(
        self, desc: WriteDescriptor, words: Mapping[int, int], cap: int | None
    ) -> str | None:
        """First reason this operation is locked right now (``None`` = open)."""
        if desc.current_cap and cap is None:
            return "model_unknown"
        if desc.needs_smart_load and words.get(R133) != GEN_SMART_LOAD:
            return "gen_not_smart_load"
        field_now = desc.field_of(words[desc.register])
        low, high = desc.limits(cap)
        if not low <= field_now <= high:
            return "existing_value_outside_range"
        return None

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
        if not self._open:
            raise Refused("writes_stopped", 409)
        desc = BY_KEY.get(key)
        if desc is None:
            raise Refused("unknown_operation", 404)
        inv = self._target(inverter_id)
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
        self._check_role(inv, words)
        peers = await self._peer_baselines(inverter_id, desc)
        missing = [p["id"] for p in peers.values() if p["baseline_word"] is None]
        if missing:
            raise Refused("peer_unavailable", 409, peers=missing)
        cap = model_cap(words[20], words[21])
        locked = self._precondition(desc, words, cap)
        if locked is not None:
            raise Refused("operation_locked", 409, reason=locked)
        check_range(desc, field_new, cap)
        if desc.companion:
            other = words[BY_KEY[desc.companion].register]
            if (desc.ordering == "below" and field_new >= other) or (
                desc.ordering == "above" and field_new <= other
            ):
                raise Refused(
                    "threshold_order",
                    422,
                    companion=BY_KEY[desc.companion].key,
                    companion_value=BY_KEY[desc.companion].display(other),
                    ordering=desc.ordering,
                )
        old_word = words[desc.register]
        new_word = desc.encode(old_word, field_new)
        if new_word == old_word:
            raise Refused("no_change", 409)
        route = inv.routes[0]
        for stale in [p for p in self._permits.values() if p.inverter_id == inverter_id]:
            stale.revoked = True
            del self._permits[stale.operation_id]
        self._expire()
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
            context=dict(words),
            field=field_new,
            old_word=old_word,
            new_word=new_word,
            peers=peers,
            expires_at=self.clock() + self.settings.permit_s,
        )
        self._permits[permit.operation_id] = permit
        return self._preview_view(permit, desc, inv, cap)

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
            "cap": cap if desc.current_cap else None,
            "old_value": desc.display(desc.field_of(permit.old_word)),
            "new_value": desc.display(permit.field),
            "old_word": permit.old_word,
            "new_word": permit.new_word,
            "old_word_hex": _hex(permit.old_word),
            "new_word_hex": _hex(permit.new_word),
            "mask_hex": _hex(desc.mask),
            "peers": [self._peer_view(p, desc) for p in permit.peers.values()],
            "plant_revision": permit.plant_revision,
        }

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
        permit.used = True  # single use from here on, whatever happens next
        op = self._new_record(permit)
        self._ops[operation_id] = op
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
            "old_value": desc.display(desc.field_of(permit.old_word)),
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
        op["reason_params"] = {k: v for k, v in refusal.params.items() if isinstance(v, str | int)}
        self._record("result", op)
        return op

    def _authorise(self, permit: Permit) -> None:
        """Everything that must still hold at the final send point."""
        if not self._open:
            raise Refused("writes_stopped", 409)
        if permit.revoked:
            raise Refused("permit_revoked", 409)
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
        self._check_role(inv, words)  # before the comparison: a role change is its own reason
        if words != permit.context:
            raise Refused("conflict", 409)
        fresh = words[desc.register]
        new = desc.encode(fresh, permit.field)
        if new != permit.new_word or new == fresh:
            raise Refused("conflict", 409)
        self._authorise(permit)
        grant = WriteGrant(
            unit=route.unit, register=desc.register, mask=desc.mask, old_word=fresh, new_word=new
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
            "matches_request": None if word is None else word == permit.new_word,
        }
        if op["status"] == SENT:  # a valid acknowledgement arrived
            matched = word is not None and word == permit.new_word
            op["status"] = CONFIRMED if matched else UNKNOWN
            op["reason"] = None if matched else "readback_missing_or_mismatch"
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
