"""Home Assistant entities for the released write-layer settings (0.4.0 part B, SPEC §10.8).

The second transport of the guarded write service. This module owns everything that is specific
to MQTT; the write itself (release, identity, role and state gates at the send point, audit intent
before the send, one FC16 with count 1, pacing, observation) stays in ``WriteService.submit``.

* **Exposure.** A released setting is an entity of its inverter's discovery document only while a
  fresh read (``WriteService.snapshot``) says the release applies (same firmware, master or no
  parallel system). A gated setting (battery control mode, Spain mode, ...) stays an entity and is
  *unavailable* through its own availability topic (decision: unavailable, not omitted, so the
  entity keeps its Home Assistant customisations); only a withdrawn release removes it. Before the
  first fresh read of a process the entities of the persisted inventory are carried over unchanged
  (and unavailable): a restart or an unreadable unit must not delete and re-create them.
* **Commands.** Topics carry a generation token that changes with every session and every change
  of the exposed set; only the current topics are subscribed, so an older generation is dead.
  Retained messages are dropped. Payloads are parsed strictly per kind; a malformed one is a
  ``write_rejected`` event and never reaches the service. Valid commands wait in ONE bounded FIFO
  per plant (no coalescing: ``OFF -> edit -> ON`` keeps its order), expire after
  ``COMMAND_TTL_S`` and are discarded when the session, the exposed set or the generation changes.
* **State.** Entity state is only ever a fresh read-back (periodically with the settings poll and
  after every command). A rejected command publishes the freshly read value, never the request.
* **Results.** One ``event`` entity per inverter carries the outcome of every command.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from deye_monitor.config import PollConfig
from deye_monitor.ha_discovery import (
    EVENT_TYPES,
    SETTING_PREFIX,
    WRITE_RESULT_KEY,
    Namespace,
    encode,
    setting_component,
    setting_key,
    timestamp,
    write_result_component,
)
from deye_monitor.write_ops import BY_KEY, DESCRIPTORS, Kind, Refused, WriteDescriptor, parse_value
from deye_monitor.writes import SettingsSnapshot, SettingState

log = logging.getLogger(__name__)

COMMAND_TTL_S = 60.0  # a queued command older than this is discarded, not executed
QUEUE_LIMIT = 32  # per plant
OUTBOX_LIMIT = 256
MAX_PAYLOAD = 32
SOURCE = "mqtt"

CONFIRMED = "write_readback_confirmed"
REJECTED = "write_rejected"
FAILED = "write_failed"
UNKNOWN = "write_outcome_unknown"
NOOP = "write_noop"
_EVENT_OF_STATUS = {
    "readback_confirmed": CONFIRMED,
    "failed": FAILED,
    "outcome_unknown": UNKNOWN,
    "noop": NOOP,
}
_TIME_PAYLOAD = re.compile(r"[0-9]{2}:[0-9]{2}:00")
_NUMBER_PAYLOAD = re.compile(r"-?[0-9]{1,6}(\.[0-9]{1,6})?")


class SettingsSource(Protocol):
    """What the entities need from the write service (faked in unit tests)."""

    def enabled(self, inverter_id: str) -> frozenset[str]: ...

    async def snapshot(self, inverter_id: str) -> SettingsSnapshot: ...

    async def submit(
        self, inverter_id: str, key: str, value: Any, *, source: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Out:
    """One message for the publisher (QoS 1)."""

    topic: str
    payload: bytes
    retain: bool


def parse_command(desc: WriteDescriptor, payload: bytes) -> tuple[Any, int]:
    """(value for ``submit``, raw field) of a command payload, or ``Refused``. Strict per kind:
    a switch is exactly ``ON`` / ``OFF``; a time is ``HH:MM:SS`` with seconds ``00`` and no
    timezone; a number is a plain decimal (no exponent, no NaN/infinity, no sign other than a
    leading minus) that encodes exactly (integer where the step is 1, two decimals for volts)."""
    if len(payload) > MAX_PAYLOAD:
        raise Refused("invalid_payload")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("invalid_payload") from None
    value: Any
    if desc.kind is Kind.BIT:
        if text not in ("ON", "OFF"):
            raise Refused("invalid_payload")
        value = 1 if text == "ON" else 0
    elif desc.kind is Kind.TIME:
        if not _TIME_PAYLOAD.fullmatch(text):
            raise Refused("invalid_payload")
        value = text
    else:
        if not _NUMBER_PAYLOAD.fullmatch(text):
            raise Refused("invalid_payload")
        value = text
    return value, parse_value(desc, value)


def state_value(desc: WriteDescriptor, state: SettingState) -> Any:
    """JSON value of one setting read-back for its entity."""
    if state.value is None:
        return None
    if desc.kind is Kind.BIT:
        return bool(state.field)
    if desc.kind is Kind.TIME:
        text = str(state.value)
        return f"{text}:00" if re.fullmatch(r"[0-9]{2}:[0-5][0-9]", text) else None
    return state.value


@dataclass(slots=True)
class _Plan:
    """What one inverter currently exposes."""

    status: str = "unknown"  # unknown (no fresh read yet) | exposed | none
    n: int = 0
    keys: dict[str, int | None] = field(default_factory=dict)  # exposed key -> model cap
    token: str = ""
    values: dict[str, Any] = field(default_factory=dict)  # last read-back per key
    ready: dict[str, bool] = field(default_factory=dict)  # gate open per key
    reachable: bool = False  # the last read worked
    published: dict[str, str] = field(default_factory=dict)  # availability last sent per key


@dataclass(slots=True)
class _Command:
    inverter_id: str
    key: str
    value: Any
    token: str
    received: float
    shown: Any  # the request as the owner sees it, for the result event


class SettingsPlane:
    """Entities, commands and state of all inverters of one plant generation."""

    def __init__(
        self,
        ns: Namespace,
        inverters: Mapping[str, PollConfig],
        source: SettingsSource,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        nonce: str | None = None,
    ) -> None:
        self.ns = ns
        self._poll = dict(inverters)
        self._source = source
        self._clock = clock
        self._wall = wall
        self._nonce = nonce or secrets.token_hex(2)
        self._plans = {i: _Plan() for i in inverters}
        for inverter_id, plan in self._plans.items():
            plan.token = self._token(plan, inverter_id)
        self._queue: deque[_Command] = deque()
        self._have = asyncio.Event()
        self._kick = asyncio.Event()
        self._force: set[str] = set()
        self._outbox: deque[Out] = deque()
        self._carry: list[tuple[str, str, Any, str]] = []  # discarded by a session end
        self._tasks: list[asyncio.Task[None]] = []
        self.on_change: Callable[[], None] = lambda: None  # the exposed set changed
        self.on_output: Callable[[], None] = lambda: None  # something to publish
        self.received = 0
        self.dropped_retained = 0
        self.executed = 0

    # -- identity of a generation ---------------------------------------------------------------

    def _token(self, plan: _Plan, inverter_id: str) -> str:
        return f"{self._nonce}{plan.n}"

    def _renew(self, inverter_id: str) -> None:
        plan = self._plans[inverter_id]
        plan.n += 1
        plan.token = self._token(plan, inverter_id)

    # -- discovery ------------------------------------------------------------------------------

    def components(
        self, inverter_id: str, stored: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """The write components of one inverter's discovery document. Before the first fresh read
        the persisted ones of still released settings are carried over unchanged."""
        plan = self._plans[inverter_id]
        out: dict[str, dict[str, Any]] = {}
        if plan.status == "unknown":
            released = self._source.enabled(inverter_id)
            for key, comp in stored.items():
                if (key.startswith(SETTING_PREFIX) and key[len(SETTING_PREFIX) :] in released) or (
                    key == WRITE_RESULT_KEY and released
                ):
                    out[key] = dict(comp)
            return out
        for desc in DESCRIPTORS:
            if desc.key in plan.keys:
                ckey, comp = setting_component(
                    self.ns, inverter_id, desc, plan.token, plan.keys[desc.key]
                )
                out[ckey] = comp
        if plan.keys:
            ckey, comp = write_result_component(self.ns, inverter_id)
            out[ckey] = comp
        return out

    def topics(self) -> set[str]:
        """Exact command topics that are subscribed now (the current generation only)."""
        return {
            self.ns.command_topic(inverter_id, plan.token, key)
            for inverter_id, plan in self._plans.items()
            if plan.status == "exposed"
            for key in plan.keys
        }

    # -- session --------------------------------------------------------------------------------

    def begin_session(self) -> None:
        """A new connection: older commands are void, the generation is new, every availability
        is announced again."""
        for command in self._queue:
            self._carry.append(
                (command.inverter_id, command.key, command.shown, "superseded_context")
            )
        self._queue.clear()
        self._have.clear()
        self._outbox.clear()
        for inverter_id, plan in self._plans.items():
            self._renew(inverter_id)
            plan.published.clear()
            plan.reachable = False
        self._force = set(self._plans)

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._refresh_loop(), name="settings-refresh"),
            asyncio.create_task(self._worker(), name="settings-commands"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for command in self._queue:
            self._carry.append(
                (command.inverter_id, command.key, command.shown, "superseded_context")
            )
        self._queue.clear()

    # -- output ---------------------------------------------------------------------------------

    def pending(self) -> Out | None:
        return self._outbox.popleft() if self._outbox else None

    def _emit(self, out: Out) -> None:
        self._outbox.append(out)
        while len(self._outbox) > OUTBOX_LIMIT:
            self._outbox.popleft()
        self.on_output()

    def announce(self) -> None:
        """After a discovery document went out: availability and state of every inverter again
        (state is not retained, so an entity that was just created needs it)."""
        for inverter_id, plan in self._plans.items():
            plan.published.clear()
            self._publish_availability(inverter_id)
            if plan.status != "unknown":
                self._publish_state(inverter_id)
        for inverter_id, key, shown, reason in self._carry:
            self._event(inverter_id, REJECTED, key, shown, reason, None)
        self._carry.clear()

    def _publish_availability(self, inverter_id: str) -> None:
        plan = self._plans[inverter_id]
        keys = (
            set(plan.keys) if plan.status == "exposed" else set(self._source.enabled(inverter_id))
        )
        for key in sorted(keys):
            online = plan.status == "exposed" and plan.reachable and plan.ready.get(key, False)
            want = "online" if online else "offline"
            if plan.published.get(key) == want:
                continue
            plan.published[key] = want
            topic = self.ns.write_availability_topic(inverter_id, key)
            self._emit(Out(topic, want.encode(), True))

    def _publish_state(self, inverter_id: str) -> None:
        plan = self._plans[inverter_id]
        body: dict[str, Any] = {"timestamp": timestamp(self._wall())}
        body.update({key: value for key, value in plan.values.items() if key in plan.keys})
        self._emit(Out(self.ns.write_state_topic(inverter_id), encode(body), False))

    def _event(
        self,
        inverter_id: str,
        event_type: str,
        key: str,
        requested: Any,
        reason: str | None,
        audit_id: str | None,
    ) -> None:
        assert event_type in EVENT_TYPES
        plan = self._plans.get(inverter_id)
        body = {
            "event_type": event_type,
            "setting": key,
            "requested_value": requested,
            "reason": reason,
            "audit_id": audit_id,
            "value": (plan.values.get(key) if plan is not None else None),
        }
        self._emit(Out(self.ns.write_result_topic(inverter_id), encode(body), False))

    # -- fresh state ----------------------------------------------------------------------------

    async def refresh(self, inverter_id: str) -> None:
        """One fresh read-back of an inverter's released settings; publishes availability and
        state, and reports a change of the exposed set."""
        try:
            snap = await self._source.snapshot(inverter_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a read problem is not a statement about the release
            log.warning("%s: settings read failed (%s)", inverter_id, type(exc).__name__)
            snap = SettingsSnapshot("unavailable", "read_failed", {})
        self._apply(inverter_id, snap)

    def _apply(self, inverter_id: str, snap: SettingsSnapshot) -> None:
        plan = self._plans[inverter_id]
        changed = False
        if snap.status == "ok":
            keys = {k: s.cap for k, s in snap.settings.items()}
            plan.reachable = True
            plan.ready = {k: s.ready for k, s in snap.settings.items()}
            plan.values = {k: state_value(BY_KEY[k], s) for k, s in snap.settings.items()}
            status = "exposed" if keys else "none"
            if status != plan.status or keys != plan.keys:
                if status != plan.status or set(keys) != set(plan.keys):
                    self._renew(inverter_id)  # another set of entities: older commands are void
                plan.keys, plan.status = keys, status
                changed = True
        elif snap.status == "withdrawn":
            plan.reachable = False
            plan.ready, plan.values = {}, {}
            if plan.keys or plan.status != "none":
                self._renew(inverter_id)
                plan.keys, plan.status = {}, "none"
                changed = True
        else:  # unavailable: keep what is exposed, show it unavailable
            plan.reachable = False
        if changed:
            self.on_change()  # discovery follows; ``announce`` then publishes availability/state
            return
        self._publish_availability(inverter_id)
        if plan.status == "exposed" and plan.reachable:
            self._publish_state(inverter_id)

    async def _refresh_loop(self) -> None:
        due: dict[str, float] = {}
        while True:
            now = self._clock()
            for inverter_id in self._plans:
                if inverter_id in self._force or due.get(inverter_id, 0.0) <= now:
                    self._force.discard(inverter_id)
                    await self.refresh(inverter_id)
                    due[inverter_id] = self._clock() + float(self._poll[inverter_id].settings_s)
            wait = max(0.0, min(due.values()) - self._clock()) if due else 3600.0
            self._kick.clear()
            if self._force:
                continue
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    await self._kick.wait()

    def request_refresh(self, inverter_id: str) -> None:
        self._force.add(inverter_id)
        self._kick.set()

    # -- commands -------------------------------------------------------------------------------

    def on_message(self, topic: str, payload: bytes, retain: bool) -> bool:
        """A message of a subscribed command topic. Never blocks. Returns whether ``topic`` was a
        command topic of the current generation."""
        target = self._lookup(topic)
        if target is None:
            return False
        inverter_id, plan, key = target
        if retain:  # a retained command is a replay, never an instruction
            self.dropped_retained += 1
            log.warning("%s: retained command on %s dropped", inverter_id, key)
            return True
        self.received += 1
        desc = BY_KEY[key]
        try:
            value, field_ = parse_command(desc, payload)
        except Refused as refusal:
            self._event(inverter_id, REJECTED, key, self._text(payload), refusal.key, None)
            return True
        shown = desc.display(field_)
        if len(self._queue) >= QUEUE_LIMIT:
            self._event(inverter_id, REJECTED, key, shown, "queue_full", None)
            return True
        self._queue.append(_Command(inverter_id, key, value, plan.token, self._clock(), shown))
        self._have.set()
        return True

    def _lookup(self, topic: str) -> tuple[str, _Plan, str] | None:
        for inverter_id, plan in self._plans.items():
            if plan.status != "exposed":
                continue
            prefix = self.ns.command_prefix(inverter_id, plan.token) + "/"
            if topic.startswith(prefix) and (key := topic[len(prefix) :]) in plan.keys:
                return inverter_id, plan, key
        return None

    @staticmethod
    def _text(payload: bytes) -> str:
        text = payload[:MAX_PAYLOAD].decode("utf-8", errors="replace")
        return "".join(c for c in text if c.isprintable())

    async def _worker(self) -> None:
        while True:
            await self._have.wait()
            if not self._queue:
                self._have.clear()
                continue
            await self._execute(self._queue.popleft())

    async def _execute(self, command: _Command) -> None:
        inverter_id, key = command.inverter_id, command.key
        plan = self._plans[inverter_id]
        if self._clock() - command.received > COMMAND_TTL_S:
            self._event(inverter_id, REJECTED, key, command.shown, "expired", None)
            return
        if plan.status != "exposed" or command.token != plan.token or key not in plan.keys:
            self._event(inverter_id, REJECTED, key, command.shown, "superseded_context", None)
            return
        self.executed += 1
        event_type, reason, audit_id = REJECTED, None, None
        try:
            op = await self._source.submit(inverter_id, key, command.value, source=SOURCE)
        except Refused as refusal:
            reason = self._reason(refusal.key, refusal.params)
            event_type = NOOP if refusal.key == "no_change" else REJECTED
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never lose a command silently
            log.error("%s: command %s failed internally (%s)", inverter_id, key, type(exc).__name__)
            reason = "internal_error"
        else:
            audit_id = op.get("operation_id")
            status = str(op.get("status"))
            reason = op.get("reason")
            event_type = _EVENT_OF_STATUS.get(status, REJECTED if status == "rejected" else UNKNOWN)
            if status == "rejected" and reason == "no_change":
                event_type = NOOP
        # the fresh read-back first: the result event comes with the true state
        await self.refresh(inverter_id)
        self._event(inverter_id, event_type, key, command.shown, reason, audit_id)

    @staticmethod
    def _reason(key: str, params: Mapping[str, Any]) -> str:
        inner = params.get("reason")
        return (
            inner
            if key in ("operation_locked", "inverter_not_ready") and isinstance(inner, str)
            else key
        )

    # -- diagnostics ----------------------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        return {
            "commands_received": self.received,
            "commands_executed": self.executed,
            "retained_dropped": self.dropped_retained,
            "queued": len(self._queue),
            "exposed": {i: sorted(p.keys) for i, p in self._plans.items() if p.status == "exposed"},
        }

    def exposed(self, inverter_id: str) -> Collection[str]:
        plan = self._plans[inverter_id]
        return tuple(plan.keys) if plan.status == "exposed" else ()


__all__ = [
    "COMMAND_TTL_S",
    "QUEUE_LIMIT",
    "SettingsPlane",
    "SettingsSource",
    "parse_command",
    "setting_key",
]
