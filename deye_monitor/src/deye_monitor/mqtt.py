"""MQTT publisher (SPEC §2, §6, §8.1; contract reviewer-codex-6.1-t8-contract.md).

``MqttPublisher`` is the ``StateSink`` of the device loops. Its callbacks only record the newest
state (they never block, nothing grows): one slot per inverter and group, availability and
diagnostics as plain fields. A single session task owns the broker connection and publishes from
these records, strictly one message at a time:

    connect -> inverters ``offline`` -> subscribe HA birth -> discovery inventory (+ cleanup
    replay) -> app ``online`` -> refresh requested -> loop (birth, discovery changes,
    availability, newest group states, app diagnostics)

* MQTT 3.1.1, a NEW client per connection attempt (no old paho queues), the will
  ``offline`` (QoS 1, retained) is set before CONNECT, our own reconnect loop with exponential
  backoff and jitter. Any error ends the whole session; shutdown publishes ``offline`` first.
* Discovery, availability: QoS 1 retained. States: QoS 1, NOT retained, always a freshly
  closed cycle; at (re)connect and on the HA birth all older measurements are discarded and a new
  read is requested from the device loops - nothing is replayed from a cache.
* The only subscription is the HA birth topic. There is no command topic and no other handler.
* Messages that were queued before a change are rejected by selection revision, lifecycle epoch
  and energy series (the contract's hand-over rule).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import socket
import ssl
import time
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

import aiomqtt

from deye_monitor import __version__
from deye_monitor.config import MqttConfig, PollConfig
from deye_monitor.device import InverterState, Snapshot
from deye_monitor.ha_discovery import (
    APP_HEARTBEAT_S,
    DeviceMeta,
    DiscoveryError,
    Namespace,
    app_components,
    encode,
    inverter_component,
    inverter_diagnostic_values,
    state_payload,
    timestamp,
)
from deye_monitor.mqtt_discovery import (
    MAX_RETIRED,
    DeviceTarget,
    DiscoveryEngine,
    DiscoveryStoreError,
    LedgerFullError,
)
from deye_monitor.profiles.decode import Value
from deye_monitor.profiles.schema import Entry, Profile
from deye_monitor.store import JsonStore

log = logging.getLogger(__name__)

QOS = 1
BIRTH_PAYLOAD = b"online"
_ENERGY_KEY = re.compile(r"^(?P<base>.+)_(?P<series>(?:[a-z0-9]{8})?g(?P<n>\d+))$")


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    topic: str
    payload: bytes


class Connection(Protocol):
    """What the publisher needs from a broker connection (faked in unit tests)."""

    async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None: ...

    async def subscribe(self, topic: str, *, qos: int) -> None: ...

    def messages(self) -> AsyncIterator[IncomingMessage]: ...

    async def abort(self) -> None:
        """Cut the connection WITHOUT a DISCONNECT, so that the broker fires the will."""


ConnectionFactory = Callable[[], AbstractAsyncContextManager[Connection]]


@dataclass(frozen=True, slots=True)
class InverterInfo:
    id: str
    name: str
    profile: Profile
    poll: PollConfig


STOP_RESERVE_S = 5.0


@dataclass(frozen=True, slots=True)
class PublisherSettings:
    """Application limits (not device limits)."""

    reconnect_min_s: float = 2.0
    reconnect_max_s: float = 60.0
    stable_after_s: float = 60.0  # a session this long starts the backoff over
    publish_timeout_s: float = 10.0
    shutdown_deadline_s: float = 5.0
    heartbeat_s: float = APP_HEARTBEAT_S
    diagnostics_min_interval_s: float = 0.5  # app diagnostics are published at most this often
    # After a discovery change the groups are read once more this long after it was sent, so that a
    # slow group (identity: hourly) does not stay 'unknown' because its first message arrived before
    # Home Assistant had subscribed the new entity. Not a higher poll rate: one extra cycle.
    discovery_settle_s: float = 3.0

    def __post_init__(self) -> None:
        for name in (
            "reconnect_min_s",
            "reconnect_max_s",
            "stable_after_s",
            "publish_timeout_s",
            "shutdown_deadline_s",
            "heartbeat_s",
        ):
            value = float(getattr(self, name))
            if not 0 < value < float("inf"):
                raise ValueError(f"{name} must be finite and > 0")
        for name in ("diagnostics_min_interval_s", "discovery_settle_s"):
            value = float(getattr(self, name))
            if not 0 <= value < float("inf"):
                raise ValueError(f"{name} must be finite and >= 0")
        if self.reconnect_max_s < self.reconnect_min_s:
            raise ValueError("reconnect_max_s must be >= reconnect_min_s")


# -- real broker connection ----------------------------------------------------------------------


def build_tls_context(cfg: MqttConfig) -> ssl.SSLContext | None:
    """Certificate and host name are always verified (there is no insecure mode)."""
    if not cfg.tls:
        return None
    context = ssl.create_default_context(cafile=cfg.tls_ca_file)
    if cfg.tls_certfile is not None:
        context.load_cert_chain(cfg.tls_certfile, cfg.tls_keyfile)
    return context


class _AiomqttConnection:
    def __init__(self, client: aiomqtt.Client) -> None:
        self._client = client

    async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
        await self._client.publish(topic, payload, qos=qos, retain=retain)

    async def subscribe(self, topic: str, *, qos: int) -> None:
        await self._client.subscribe(topic, qos=qos)

    async def abort(self) -> None:
        # Shut the socket down and leave the rest to paho/aiomqtt: the broker sees the connection
        # vanish (no DISCONNECT) and publishes the will. The socket is not closed here because
        # aiomqtt still has a reader registered for it.
        sock: Any = self._client._client.socket()
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    async def messages(self) -> AsyncIterator[IncomingMessage]:
        async for message in self._client.messages:
            raw: Any = message.payload
            data = raw if isinstance(raw, bytes | bytearray) else str(raw or "").encode()
            yield IncomingMessage(str(message.topic), bytes(data))


def aiomqtt_factory(
    ns: Namespace, cfg: MqttConfig, settings: PublisherSettings
) -> ConnectionFactory:
    """A factory that builds a NEW ``aiomqtt.Client`` per connection attempt."""
    if cfg.host is None:
        raise ValueError("mqtt.host is not configured")
    host = cfg.host

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[Connection]:
        client = aiomqtt.Client(
            hostname=host,
            port=cfg.port,
            username=cfg.username,
            password=cfg.password.get_secret_value() if cfg.password else None,
            identifier=ns.client_id,
            protocol=aiomqtt.ProtocolVersion.V311,
            clean_session=True,
            will=aiomqtt.Will(ns.app_status_topic, "offline", qos=QOS, retain=True),
            keepalive=30,
            timeout=settings.publish_timeout_s,
            tls_context=build_tls_context(cfg),
            max_queued_incoming_messages=32,
            max_queued_outgoing_messages=32,
            max_inflight_messages=8,
        )
        async with client:
            yield _AiomqttConnection(client)

    return connect


# -- per-inverter record -------------------------------------------------------------------------


@dataclass(slots=True)
class _Slot:
    snapshot: Snapshot
    values: Mapping[str, Value]


@dataclass(slots=True)
class _Inverter:
    info: InverterInfo
    keys: frozenset[str]
    revision: int
    series: str = "g0"  # opaque suffix of the active energy series (``g<n>`` / ``<token>g<n>``)
    known: set[str] = field(default_factory=set)  # every series ever published: never reused
    epoch: int = 0
    state: InverterState = InverterState.STARTING
    reason: str = "starting"
    fresh: bool = False  # a non-failed state was published since the connect / last offline
    published: str = "offline"  # what the broker holds retained for the availability topic
    serial: str | None = None
    entries: dict[str, Entry] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] | None = None  # newest event payload of the device loop

    @property
    def wants_online(self) -> bool:
        return self.state is InverterState.RUNNING and (self.fresh or not self.keys)


class MqttPublisher:
    """Sink + broker session. ``run()`` is the long-lived task."""

    def __init__(
        self,
        ns: Namespace,
        inverters: Sequence[InverterInfo],
        store: JsonStore,
        factory: ConnectionFactory,
        *,
        birth_topic: str = "homeassistant/status",
        selections: Mapping[str, tuple[Collection[str], int]] | None = None,
        series_of: Callable[[str], str] | None = None,
        serial_of: Callable[[str], str | None] | None = None,
        request_refresh: Callable[[str], None] | None = None,
        settings: PublisherSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
        diagnostics_of: Callable[[str], Mapping[str, Any] | None] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.ns = ns
        self.birth_topic = birth_topic
        self.settings = settings or PublisherSettings()
        self._factory = factory
        self._store = store
        self._clock = clock
        self._rand = rand
        self._sleep = sleep
        self._serial_of = serial_of
        self._request_refresh = request_refresh
        self._diagnostics_of = diagnostics_of
        self._purges: list[str | None] = []
        self._last_app_publish = -1e18
        self._settle_due: float | None = None  # the extra refresh after a discovery change
        self._inv: dict[str, _Inverter] = {}
        for info in inverters:
            keys, revision = (selections or {}).get(info.id, (frozenset(), 0))
            rec = _Inverter(info=info, keys=frozenset(keys), revision=revision)
            rec.entries = {k: info.profile.entry(k) for k in rec.keys}
            rec.series = series_of(info.id) if series_of else "g0"
            self._inv[info.id] = rec
        self._slots: dict[tuple[str, str], _Slot] = {}
        self._wake = asyncio.Event()
        self._birth = asyncio.Event()
        self._stop = asyncio.Event()
        self._discovery_dirty = True
        self._app_dirty = True
        self._last_heartbeat = -1e18
        self._engine: DiscoveryEngine | None = None
        self._consumer_error: BaseException | None = None
        # counters / diagnostics
        self.reconnects = 0
        self.sessions = 0
        self.connected = False
        self.dropped_stale = 0
        self.nulled_values = 0
        self.last_error: str | None = None
        self.last_discovery_error: str | None = None
        self.states_published = 0
        # a series published earlier is never used again (cleanup inventory)
        for rec in self._inv.values():
            active, rec.known = self._stored_series(rec)
            if active is not None and rec.series in rec.known and rec.series != active:
                rec.series = active
            rec.known.add(rec.series)

    # -- StateSink (called by the device loops; must not block) --------------------------------

    def on_state(self, inverter_id: str, snapshot: Snapshot, values: Mapping[str, Value]) -> None:
        rec = self._inv.get(inverter_id)
        if rec is None:
            return
        if snapshot.selection_revision < rec.revision or snapshot.lifecycle_epoch < rec.epoch:
            self.dropped_stale += 1
            return
        if snapshot.energy_series != rec.series and any(
            (e := rec.entries.get(k)) is not None and e.scale_source for k in values
        ):
            if snapshot.energy_series in rec.known:
                self.dropped_stale += 1  # a counter value of an older statistics series
                return
            rec.series = snapshot.energy_series  # a new series: the discovery follows first
            rec.known.add(rec.series)
            self._discovery_dirty = True
        old = self._slots.get((inverter_id, snapshot.group))
        if old is not None and old.snapshot.cycle_id > snapshot.cycle_id:
            self.dropped_stale += 1
            return
        rec.epoch = snapshot.lifecycle_epoch
        self._slots[(inverter_id, snapshot.group)] = _Slot(snapshot, dict(values))
        self._wake.set()

    def on_availability(self, inverter_id: str, state: InverterState, reason: str) -> None:
        rec = self._inv.get(inverter_id)
        if rec is None:
            return
        rec.state, rec.reason = state, reason
        if state is not InverterState.RUNNING:
            rec.fresh = False  # online again only after a fresh measurement
            self._drop_slots(inverter_id)
        self._check_serial(rec)
        self._app_dirty = True
        self._wake.set()

    def on_diagnostics(self, inverter_id: str, diagnostics: Mapping[str, Any]) -> None:
        rec = self._inv.get(inverter_id)
        if rec is None:
            return
        epoch = diagnostics.get("lifecycle_epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch > rec.epoch:
            rec.epoch = epoch
            self._drop_slots(inverter_id)
        self._check_serial(rec)
        rec.diagnostics = dict(diagnostics)
        self._app_dirty = True  # published (throttled) with the next app diagnostics
        self._wake.set()

    # -- control --------------------------------------------------------------------------------

    def set_selection(self, inverter_id: str, keys: Collection[str], revision: int) -> None:
        """New published selection (T10 calls this together with ``InverterRuntime.set_selection``).
        Slots of an older revision are dropped; the discovery inventory follows."""
        rec = self._inv[inverter_id]
        rec.keys = frozenset(keys)
        rec.revision = revision
        rec.entries = {k: rec.info.profile.entry(k) for k in rec.keys}
        self._drop_slots(inverter_id)
        self._discovery_dirty = True
        self._wake.set()

    def purge_cleanup_ledger(self, inverter_id: str | None = None) -> int:
        """Explicit user action (needs a confirmation by the caller): forget the cleanup ledger of
        one inverter or of every device. Returns how many entries will be dropped. The change is
        applied by the session at its next discovery sync."""
        if inverter_id is not None and inverter_id not in self._inv:
            raise KeyError(inverter_id)
        devices = self._store.data.get("devices", {})
        count = sum(
            len(e["retired"])
            for e in devices.values()
            if inverter_id is None or e["meta"].get("inverter_id") == inverter_id
        )
        self._purges.append(inverter_id)
        self._discovery_dirty = True
        self._wake.set()
        return count

    @property
    def stop_budget_s(self) -> float:
        """Upper bound of an orderly stop, derived from the inner budgets: a publish that is
        still running when the stop is requested, the inverters' offline, the app's offline and a
        reserve for the consumer cleanup and the connection teardown. The caller's outer deadline
        must not be shorter."""
        s = self.settings
        return s.publish_timeout_s + 2 * s.shutdown_deadline_s + STOP_RESERVE_S

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _drop_slots(self, inverter_id: str) -> None:
        for key in [k for k in self._slots if k[0] == inverter_id]:
            del self._slots[key]

    def _check_serial(self, rec: _Inverter) -> None:
        if self._serial_of is None:
            return
        serial = self._serial_of(rec.info.id)
        if serial and serial != rec.serial:
            rec.serial = serial
            self._discovery_dirty = True
            self._wake.set()

    def _stored_series(self, rec: _Inverter) -> tuple[str | None, set[str]]:
        """(series of the active energy components, all series ever published) of one inverter."""
        energy = {e.key for e in rec.info.profile.entries if e.scale_source}
        entry = self._store.data.get("devices", {}).get(
            self.ns.discovery_topic(self.ns.inverter_device_id(rec.info.id)), {}
        )

        def series(keys: Any) -> set[str]:
            found = (_ENERGY_KEY.match(k) for k in keys)
            return {m["series"] for m in found if m and m["base"] in energy}

        active = series(entry.get("components", {}))
        known = active | series(entry.get("retired", {}))
        return (next(iter(active)) if active else None), known

    def diagnostics(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "sessions": self.sessions,
            "reconnects": self.reconnects,
            "dropped_stale": self.dropped_stale,
            "nulled_values": self.nulled_values,
            "states_published": self.states_published,
            "pending_slots": len(self._slots),
            "last_error": self.last_error,
            "last_discovery_error": self.last_discovery_error,
            "retired_components": self._engine.retired_count() if self._engine else None,
            "ledger_limit": MAX_RETIRED,
        }

    # -- targets ----------------------------------------------------------------------------------

    def _targets(self) -> dict[str, DeviceTarget]:
        ns = self.ns
        app_meta = DeviceMeta(ns.app_device_id, "Deye Monitor", "app")
        names = {i: r.info.name for i, r in self._inv.items()}
        states = tuple(s.value for s in InverterState)
        targets = {
            ns.discovery_topic(ns.app_device_id): DeviceTarget(
                app_meta, app_components(ns, names, states)
            )
        }
        for inverter_id, rec in self._inv.items():
            components: dict[str, dict[str, Any]] = {}
            for key in sorted(rec.keys):
                entry = rec.entries[key]
                if entry.internal or not entry.selectable:
                    log.warning("%s: %s is not selectable and is not published", inverter_id, key)
                    continue
                ckey, comp = inverter_component(
                    ns,
                    inverter_id,
                    entry,
                    series=rec.series,
                    poll=rec.info.poll,
                    profile=rec.info.profile,
                )
                components[ckey] = comp
            meta = DeviceMeta(
                ns.inverter_device_id(inverter_id),
                rec.info.name,
                "inverter",
                inverter_id,
                rec.serial,
            )
            targets[ns.discovery_topic(meta.device_id)] = DeviceTarget(meta, components)
        return targets

    # -- session loop -----------------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, publish, reconnect with backoff until ``request_stop()``."""
        delay = self.settings.reconnect_min_s
        while not self._stop.is_set():
            started = self._clock()
            try:
                async with self._factory() as conn:
                    await self._session(conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("MQTT session ended (%s)", self.last_error)
            finally:
                self.connected = False
            if self._stop.is_set():
                return
            self.reconnects += 1
            if self._clock() - started >= self.settings.stable_after_s:
                delay = self.settings.reconnect_min_s
            await self._sleep(min(delay * (1 + 0.25 * self._rand()), self.settings.reconnect_max_s))
            delay = min(delay * 2, self.settings.reconnect_max_s)

    def _make_publish(self, conn: Connection) -> Callable[[str, bytes, bool], Any]:
        async def publish(topic: str, payload: bytes, retain: bool) -> None:
            async with asyncio.timeout(self.settings.publish_timeout_s):
                await conn.publish(topic, payload, qos=QOS, retain=retain)

        return publish

    async def _session(self, conn: Connection) -> None:
        publish = self._make_publish(conn)
        self._engine = DiscoveryEngine(self.ns, self._store, publish)
        self._slots.clear()  # old measurements are never replayed
        self._birth.clear()
        self._consumer_error = None
        self.sessions += 1
        consumer: asyncio.Task[None] | None = None
        try:
            for rec in self._inv.values():
                rec.published = "offline"
                rec.fresh = False
                await publish(self.ns.inverter_availability_topic(rec.info.id), b"offline", True)
            await conn.subscribe(self.birth_topic, qos=QOS)
            consumer = asyncio.create_task(self._consume(conn), name="mqtt-consumer")
            birth = self._birth.is_set()
            self._birth.clear()
            await self._sync(replay=True, birth=birth)
            await publish(self.ns.app_status_topic, b"online", True)
            self.connected = True
            self._refresh_all()
            self._app_dirty = True
            self._last_heartbeat = -1e18
            await self._pump(publish)
        finally:
            try:
                if consumer is not None:
                    consumer.cancel()
                    await asyncio.gather(consumer, return_exceptions=True)
            finally:
                await self._withdraw(conn, publish)

    async def _consume(self, conn: Connection) -> None:
        try:
            async for message in conn.messages():
                if message.topic == self.birth_topic and message.payload.strip() == BIRTH_PAYLOAD:
                    self._birth.set()
                    self._wake.set()
            raise aiomqtt.MqttError("message stream ended")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._consumer_error = exc
            self._wake.set()

    async def _withdraw(self, conn: Connection, publish: Callable[[str, bytes, bool], Any]) -> None:
        """Whatever ended the session (shutdown, broker error, discovery or persistence error):
        say ``offline`` for every inverter and then for the app, each part with its own time
        budget. A normal DISCONNECT does not fire the will, so if the app's ``offline`` could not
        be delivered the connection is cut without DISCONNECT and the broker publishes the will."""
        app_offline = False
        try:
            try:
                async with asyncio.timeout(self.settings.shutdown_deadline_s):
                    for rec in self._inv.values():
                        await publish(
                            self.ns.inverter_availability_topic(rec.info.id), b"offline", True
                        )
            except Exception as exc:
                log.warning("could not publish inverter offline (%s)", type(exc).__name__)
            try:
                async with asyncio.timeout(self.settings.shutdown_deadline_s):
                    await publish(self.ns.app_status_topic, b"offline", True)
                app_offline = True
            except Exception as exc:
                log.warning("could not publish app offline (%s)", type(exc).__name__)
        finally:
            # Also when the outer shutdown deadline cancels this very coroutine: before the
            # connection context is left in an orderly way (DISCONNECT, no will) the connection is
            # cut unless the app's ``offline`` is confirmed. The abort is shielded from a
            # cancellation that is already on its way.
            if not app_offline:
                log.warning("app offline not confirmed: aborting the connection")
                with contextlib.suppress(Exception):
                    await asyncio.shield(conn.abort())

    def _refresh_all(self) -> None:
        if self._request_refresh is None:
            return
        for inverter_id in self._inv:
            self._request_refresh(inverter_id)

    async def _sync(self, *, replay: bool, birth: bool) -> None:
        assert self._engine is not None
        self._discovery_dirty = False
        republish = False
        try:
            while self._purges:
                dropped = self._engine.purge(self._purges[0])
                self._purges.pop(0)
                republish = True
                log.info("cleanup ledger purged (%d entries)", dropped)
            report = await self._engine.sync(
                self._targets(), replay=replay, birth=birth, republish=republish
            )
        except LedgerFullError as exc:
            self.last_discovery_error = f"discovery_ledger_full: {exc}"[:200]
            self._discovery_dirty = True
            raise
        except DiscoveryStoreError as exc:
            self.last_discovery_error = f"discovery_store_unwritable: {exc}"[:200]
            self._discovery_dirty = True
            raise
        except DiscoveryError as exc:
            self.last_discovery_error = f"discovery_invalid: {exc}"[:200]
            raise
        self.last_discovery_error = None
        log.info(
            "discovery published: %d document(s), %d cleanup replay(s)",
            report.documents,
            report.replays,
        )

    async def _pump(self, publish: Callable[[str, bytes, bool], Any]) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self._consumer_error is not None:
                raise aiomqtt.MqttError(f"consumer stopped: {type(self._consumer_error).__name__}")
            if await self._step(publish):
                continue
            wait = max(0.0, self._last_heartbeat + self.settings.heartbeat_s - self._clock())
            if self._app_dirty:  # a throttled diagnostics update is due when the interval is over
                gap = self._last_app_publish + self.settings.diagnostics_min_interval_s
                wait = min(wait, max(0.0, gap - self._clock()))
            if self._settle_due is not None:
                wait = min(wait, max(0.0, self._settle_due - self._clock()))
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait):
                    await self._wake.wait()

    async def _step(self, publish: Callable[[str, bytes, bool], Any]) -> bool:
        """Do the next piece of pending work; ``True`` if something was done."""
        if self._birth.is_set():
            self._birth.clear()
            self._slots.clear()  # HA is (re)starting: only fresh cycles from now on
            await self._sync(replay=True, birth=True)
            self._refresh_all()
            return True
        if self._discovery_dirty:
            await self._sync(replay=False, birth=False)
            self._settle_due = self._clock() + self.settings.discovery_settle_s
            return True
        if self._settle_due is not None and self._clock() >= self._settle_due:
            self._settle_due = None
            self._refresh_all()
            return True
        for rec in self._inv.values():
            want = "online" if rec.wants_online else "offline"
            if rec.published != want:
                await publish(self.ns.inverter_availability_topic(rec.info.id), want.encode(), True)
                rec.published = want
                self._app_dirty = True
                return True
        if self._slots:
            key = min(self._slots, key=lambda k: (self._slots[k].snapshot.cycle_id, k))
            await self._publish_slot(publish, self._slots.pop(key), key[0])
            return True
        if self._app_due():
            await self._publish_app(publish)
            return True
        return False

    def _app_due(self) -> bool:
        now = self._clock()
        if now >= self._last_heartbeat + self.settings.heartbeat_s:
            return True
        return (
            self._app_dirty
            and now >= self._last_app_publish + self.settings.diagnostics_min_interval_s
        )

    async def _publish_slot(
        self, publish: Callable[[str, bytes, bool], Any], slot: _Slot, inverter_id: str
    ) -> None:
        rec = self._inv[inverter_id]
        snap = slot.snapshot
        energy_keys = [k for k in slot.values if (e := rec.entries.get(k)) and e.scale_source]
        if (
            snap.selection_revision != rec.revision
            or snap.lifecycle_epoch < rec.epoch
            or (energy_keys and snap.energy_series != rec.series)
        ):
            self.dropped_stale += 1
            return
        payload, replaced = state_payload(
            cycle_id=snap.cycle_id,
            wall_ts=snap.wall_ts,
            energy_generation=snap.energy_generation,
            energy_series=snap.energy_series,
            values=slot.values,
            entries=rec.entries,
        )
        self.nulled_values += replaced
        await publish(self.ns.group_topic(inverter_id, snap.group), payload, False)
        self.states_published += 1
        if not snap.failed and rec.state is InverterState.RUNNING:
            rec.fresh = True

    async def _publish_app(self, publish: Callable[[str, bytes, bool], Any]) -> None:
        body: dict[str, Any] = {
            "timestamp": timestamp(time.time()),
            "app_version": __version__,
            "mqtt_reconnects": self.reconnects,
            "inverters_online": sum(1 for r in self._inv.values() if r.published == "online"),
            "inverters_configured": len(self._inv),
        }
        for inverter_id, rec in self._inv.items():
            body[f"inverter_{inverter_id}_state"] = rec.state.value
            body[f"inverter_{inverter_id}_reason"] = rec.reason
            source = self._diagnostics_of(inverter_id) if self._diagnostics_of else rec.diagnostics
            for name, value in inverter_diagnostic_values(source).items():
                body[f"inverter_{inverter_id}_{name}"] = value
            # fallback only without a diagnostics provider: an explicit "no binding" stays null
            if (
                self._diagnostics_of is None
                and body[f"inverter_{inverter_id}_serial"] is None
                and rec.serial
            ):
                body[f"inverter_{inverter_id}_serial"] = rec.serial
        self._app_dirty = False
        self._last_heartbeat = self._clock()
        self._last_app_publish = self._last_heartbeat
        await publish(self.ns.app_state_topic, encode(body), False)
