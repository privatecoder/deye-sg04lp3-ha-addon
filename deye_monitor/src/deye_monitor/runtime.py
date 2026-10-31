"""Wiring: buses, transports and inverter runtimes from the validated configuration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from pathlib import Path

import serial_asyncio_fast

from deye_monitor.config import AppConfig, EndpointConfig, Transport, data_dir
from deye_monitor.device import (
    PROBE_SHARE,
    InverterRuntime,
    InverterState,
    NullSink,
    Route,
    RuntimeSettings,
    StateSink,
)
from deye_monitor.energy_state import (
    EnergyTracker,
    valid_energy_document,
    valid_generation_document,
)
from deye_monitor.identity import IdentityBook, identity_store, normalize_serial
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.hardware import load_hardware
from deye_monitor.profiles.plan import ReadPlan, template_selection
from deye_monitor.profiles.schema import load_profile
from deye_monitor.protocol.guard import Framing
from deye_monitor.scheduler import BusScheduler
from deye_monitor.store import JsonStore, LoadStatus
from deye_monitor.transport.base import (
    Connector,
    StreamTransport,
    TransportSettings,
)
from deye_monitor.transport.modbus_tcp import ModbusTcpTransport, tcp_connector
from deye_monitor.transport.rtu_over_tcp import RtuOverTcpTransport
from deye_monitor.transport.serial_rtu import SerialRtuTransport
from deye_monitor.transport.solarman_v5_tcp import SolarmanV5Transport

log = logging.getLogger(__name__)

ConnectorFactory = Callable[[EndpointConfig], Connector]


def default_connector(ep: EndpointConfig) -> Connector:
    """The only place where addresses from the configuration become connections."""
    if ep.transport is Transport.SERIAL_RTU:
        device, baudrate = ep.serial_device, ep.baudrate or 9600

        async def serial() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await serial_asyncio_fast.open_serial_connection(
                url=device, baudrate=baudrate, bytesize=8, parity="N", stopbits=1
            )

        return serial
    host, port = ep.host or "", ep.port or 0
    return lambda: tcp_connector(host, port)


def transport_settings(ep: EndpointConfig) -> TransportSettings:
    return TransportSettings(
        timeout_s=ep.timeout_s,
        max_read_registers=ep.read_block_limit,
        rtu_late_window_s=ep.rtu_late_window_s,
    )


def make_transport(
    ep: EndpointConfig,
    connector: Connector,
    clock: Callable[[], float],
    rand: Callable[[], float],
    identity_check: Callable[[list[int]], bool],
) -> StreamTransport:
    """The transport of one endpoint (the guard sits inside it, on the only sending path)."""
    kw = {
        "connector": connector,
        "settings": transport_settings(ep),
        "clock": clock,
        "rand": rand,
    }
    transport: StreamTransport
    if ep.transport is Transport.MODBUS_TCP:
        transport = ModbusTcpTransport(identity_check=identity_check, **kw)  # type: ignore[arg-type]
    elif ep.transport is Transport.RTU_OVER_TCP:
        transport = RtuOverTcpTransport(identity_check=identity_check, **kw)  # type: ignore[arg-type]
    elif ep.transport is Transport.SERIAL_RTU:
        transport = SerialRtuTransport(
            identity_check=identity_check,
            baudrate=ep.baudrate or 9600,
            **kw,  # type: ignore[arg-type]
        )
    else:
        transport = SolarmanV5Transport(
            identity_check=identity_check,
            logger_serial=ep.logger_serial,
            **kw,  # type: ignore[arg-type]
        )
    assert transport.framing in (Framing.RTU, Framing.MBAP, Framing.SOLARMAN_V5)
    return transport


class Runtime:
    """All inverter runtimes of one configuration. One ``BusScheduler`` per canonical bus."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        directory: Path | None = None,
        sink: StateSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
        connector_factory: ConnectorFactory = default_connector,
        settings: RuntimeSettings | None = None,
        features: Mapping[str, Features] | None = None,
        selections: Mapping[str, tuple[Collection[str], int]] | None = None,
        restart_min_s: float = 5.0,
        restart_max_s: float = 300.0,
        restart_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        provisional: bool = False,
    ) -> None:
        self.cfg = cfg
        self._restart_min_s = restart_min_s
        self._restart_max_s = restart_max_s
        self._restart_sleep = restart_sleep
        self._rand = rand
        self._clock = clock
        self.connector_factory = connector_factory
        base = data_dir(directory)
        self.book = IdentityBook(
            identity_store(base / "identity.json"),
            {i.id: i.expected_serial for i in cfg.inverters},
            provisional=provisional,
        )
        self._capabilities = JsonStore(base / "capabilities.json")
        self._energy = JsonStore(base / "energy.json", validate=valid_energy_document)
        self._generations = JsonStore(
            base / "energy-generations.json", validate=valid_generation_document
        )
        self._wall = wall
        self.schedulers: dict[str, BusScheduler] = {}
        self.transports: dict[str, StreamTransport] = {}
        self.inverters: dict[str, InverterRuntime] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._unit_owner: dict[tuple[str, int], str] = {}
        self._buses: dict[str, str] = {ep.id: ep.bus for ep in cfg.endpoints}
        self._build(
            cfg,
            sink or NullSink(),
            clock,
            wall,
            rand,
            connector_factory,
            settings,
            features or {},
            selections or {},
        )

    # -- construction ------------------------------------------------------------------------

    def _build(
        self,
        cfg: AppConfig,
        sink: StateSink,
        clock: Callable[[], float],
        wall: Callable[[], float],
        rand: Callable[[], float],
        connector_factory: ConnectorFactory,
        settings: RuntimeSettings | None,
        features: Mapping[str, Features],
        selections: Mapping[str, tuple[Collection[str], int]],
    ) -> None:
        for bus in sorted(set(self._buses.values())):
            members = [ep for ep in cfg.endpoints if ep.bus == bus]
            self.schedulers[bus] = BusScheduler(
                gap_s=max(ep.request_gap_ms for ep in members) / 1000,
                max_queue=max(ep.queue_size for ep in members),
                clock=clock,
            )
        for inv in cfg.inverters:
            self._unit_owner[(inv.endpoint, inv.unit_id)] = inv.id
            if inv.fallback_endpoint and inv.fallback_unit_id is not None:
                self._unit_owner[(inv.fallback_endpoint, inv.fallback_unit_id)] = inv.id
        for ep in cfg.endpoints:
            self.transports[ep.id] = self._transport(ep, connector_factory(ep), clock, rand)
        for inv in cfg.inverters:
            profile = load_profile(inv.profile)
            hardware = load_hardware(profile.hardware)
            feats = features.get(inv.id, Features())
            keys, revision = selections.get(inv.id) or (
                template_selection(profile, hardware, feats),
                0,
            )
            routes = [self._route("primary", inv.endpoint, inv.unit_id)]
            if inv.fallback_endpoint and inv.fallback_unit_id is not None:
                routes.append(self._route("fallback", inv.fallback_endpoint, inv.fallback_unit_id))
            runtime = InverterRuntime(
                inv,
                routes,
                profile=profile,
                hardware=hardware,
                features=feats,
                selection=keys,
                selection_revision=revision,
                book=self.book,
                energy=EnergyTracker(
                    self._energy,
                    inv.id,
                    inv.energy_resolution,
                    high_water=self._generations,
                ),
                capability_store=self._capabilities,
                sink=sink,
                clock=clock,
                wall=wall,
                settings=settings,
            )
            runtime.bus_utilisation = lambda i=inv.id: self.bus_utilisation().get(
                self._bus_of(i), 0.0
            )
            self.inverters[inv.id] = runtime

    def _transport(
        self,
        ep: EndpointConfig,
        connector: Connector,
        clock: Callable[[], float],
        rand: Callable[[], float],
    ) -> StreamTransport:
        return make_transport(ep, connector, clock, rand, self._identity_check(ep.id))

    def _identity_check(self, endpoint_id: str) -> Callable[[list[int]], bool]:
        """Resync probe validator: the answer must be the serial number bound to the inverter
        that is addressed on this endpoint (the unit of the request being recovered). Before a
        binding exists only syntactically valid ASCII is required."""

        def check(words: list[int]) -> bool:
            serial = normalize_serial(words)
            if serial is None:
                return False
            unit = self.transports[endpoint_id].probe_unit
            owner = self._unit_owner.get((endpoint_id, unit)) if unit is not None else None
            bound = self.book.bound(owner) if owner else None
            return bound is None or serial == bound

        return check

    def _route(self, name: str, endpoint_id: str, unit: int) -> Route:
        ep = self.cfg.endpoint(endpoint_id)
        return Route(
            name=name,
            endpoint_id=endpoint_id,
            unit=unit,
            transport=self.transports[endpoint_id],
            scheduler=self.schedulers[ep.bus],
            max_read_registers=ep.read_block_limit,
            max_read_registers_configured=ep.max_read_registers,
            bus_gap_s=self.schedulers[ep.bus].gap_s,
            response_time_s=min(0.25, ep.timeout_s),
        )

    # -- observation --------------------------------------------------------------------

    def _bus_of(self, inverter_id: str) -> str:
        return self._buses[self.inverters[inverter_id].route.endpoint_id]

    def bus_utilisation(self, candidates: Mapping[str, ReadPlan] | None = None) -> dict[str, float]:
        """Rough load per canonical bus: the plan budgets of every inverter on its active path
        plus a small allowance for identity probes and failover reads. Above 1 the bus cannot
        keep all intervals: ticks are coalesced (never caught up) and requests are served in
        FIFO order across inverters."""
        load = dict.fromkeys(self.schedulers, 0.0)
        for inv in self.inverters.values():
            bus = self._buses[inv.route.endpoint_id]
            plan = (candidates or {}).get(inv.id) or inv.current_plan()
            load[bus] += plan.budget.utilisation + PROBE_SHARE
        return load

    def diagnostics(self) -> dict[str, object]:
        return {
            "bus_utilisation": self.bus_utilisation(),
            "inverters": {i: r.diagnostics() for i, r in self.inverters.items()},
        }

    # -- lifecycle ----------------------------------------------------------------------

    def rebind(self, inverter_id: str) -> bool:
        return self.inverters[inverter_id].rebind()

    def reset_store(self, inverter_id: str | None, kind: str) -> None:
        """Explicit user reset of a damaged critical store (T10 offers it).

        ``identity`` with an ``inverter_id``: only this inverter's binding is removed; every other
        inverter keeps its binding and keeps running. If the identity file is damaged as a whole the
        reset is GLOBAL (``inverter_id=None``, otherwise ``ValueError``): all bindings are gone, so
        EVERY inverter is revoked (lifecycle epoch +1, offline, identified again) - the caller must
        make that clear to the user. ``energy`` (needs an ``inverter_id``): starts a NEW statistics
        series for that inverter (a new random namespace if its history was lost)."""
        if kind == "identity":
            lost = self.book.reset(inverter_id)
            # the affected ones, and any inverter that was only blocked by the file problem
            hit = [
                inv
                for inv in self.inverters.values()
                if lost is None or inv.id in lost or inv.state is InverterState.BLOCKED
            ]
            # Phase 1: the core invalidation of EVERY affected inverter, nothing that can fail.
            for inv in hit:
                inv.revoke_after_reset()
            # Phase 2: notifications one by one; a failing sink must not skip the others.
            errors: list[Exception] = []
            for inv in hit:
                try:
                    inv.notify_after_reset()
                except Exception as exc:
                    log.error("%s: reset notification failed (%s)", inv.id, type(exc).__name__)
                    errors.append(exc)
            if errors:
                raise errors[0]
        elif kind == "energy":
            if inverter_id is None:
                raise ValueError("an energy reset needs an inverter id")
            self.inverters[inverter_id].reset_energy_store()
        else:
            raise ValueError(f"unknown store kind {kind!r}")

    async def start(self) -> None:
        for inv in self.inverters.values():
            task = asyncio.create_task(self._supervise(inv), name=f"inverter-{inv.id}")
            task.add_done_callback(_log_crash)
            self._tasks.append(task)

    async def _supervise(self, inv: InverterRuntime) -> None:
        """Keep one inverter task alive: an unexpected exception puts the inverter ``offline``
        (``internal_error``) and restarts it with exponential backoff (5 s .. 300 s, jitter).
        A task that ran stably for a while starts the backoff over. Cancellation (shutdown) also
        interrupts a waiting restart; nothing else can take the application down."""
        delay = self._restart_min_s
        while True:
            started = self._clock()
            try:
                await inv.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                inv.mark_internal_error(exc)
                if inv.state is InverterState.BLOCKED:
                    # a critical store is damaged/unwritable: no reads and no retry loop; wait for
                    # the explicit reset (one supervisor per inverter, never a second loop)
                    inv.released.clear()
                    await inv.released.wait()
                    delay = self._restart_min_s
                    continue
            if self._clock() - started > 2 * self._restart_max_s:
                delay = self._restart_min_s  # it was healthy for long: not a crash loop
            await self._restart_sleep(min(delay * (1 + 0.25 * self._rand()), self._restart_max_s))
            delay = min(delay * 2, self._restart_max_s)

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for transport in self.transports.values():
            with contextlib.suppress(Exception):
                await transport.close()
        self.book.flush()
        for store in (self._capabilities, self._energy, self._generations):
            if store.status is LoadStatus.VALID:  # never touch a missing or damaged file
                with contextlib.suppress(OSError):
                    store.save()


def _log_crash(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("%s stopped unexpectedly: %s", task.get_name(), type(exc).__name__)
