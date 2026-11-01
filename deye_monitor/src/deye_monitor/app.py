"""Application entry point and lifecycle (SPEC §2): device loops plus the MQTT publisher."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from deye_monitor import __version__
from deye_monitor.config import (
    AppConfig,
    ConfigError,
    MqttConfig,
    data_dir,
    load_config,
    resolve_instance_id,
    with_supervisor_mqtt,
)
from deye_monitor.device import NullSink, RelaySink
from deye_monitor.ha_discovery import Namespace
from deye_monitor.mqtt import (
    ConnectionFactory,
    InverterInfo,
    MqttPublisher,
    PublisherSettings,
    aiomqtt_factory,
)
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.mqtt_settings import SettingsPlane
from deye_monitor.plant import PlantService, PlantStore, SwitchError
from deye_monitor.profiles.plan import SelectionError
from deye_monitor.runtime import Runtime
from deye_monitor.selection import (
    STALE_CODES,
    InverterSelection,
    InverterSpec,
    SelectionManager,
    SelectionStore,
    SpecsUndo,
    specs_from_config,
)
from deye_monitor.store import JsonStore
from deye_monitor.totals import PlantSeries, PlantTotals, TotalsMember, known_series
from deye_monitor.webapi import WebApi

log = logging.getLogger("deye_monitor")

EXIT_CONFIG = 2
# Validate the options and idle without starting the device loops (packaging tests, CI start check).
NO_DEVICES_ENV = "DEYE_MONITOR_NO_DEVICES"


def open_discovery_store(path: Path) -> JsonStore:
    """``/data/discovery.json``. A damaged file is kept as ``*.corrupt`` and a new inventory is
    started (loud log): without it earlier removals cannot be repeated, but nothing unsafe
    follows from losing it."""
    store = JsonStore(path, validate=valid_discovery_document)
    if store.corrupt:
        log.error("discovery inventory %s is damaged; starting a new one (backup kept)", path.name)
        store.reset({"devices": {}})
    return store


class LiveApplier:
    """Adopts a persisted selection revision: publisher first (it drops everything older), then
    the inverter loop (it rebuilds its plan and discards reads that were in flight)."""

    def __init__(self, runtime: Runtime, publisher: MqttPublisher | None) -> None:
        self.runtime = runtime
        self.publisher = publisher

    def prepare(
        self, inverter_id: str, selection: InverterSelection, *, selection_changed: bool
    ) -> None:
        if selection_changed:
            self.runtime.inverters[inverter_id].check_selection(selection.active)

    def apply(
        self,
        inverter_id: str,
        selection: InverterSelection,
        revision: int,
        *,
        selection_changed: bool,
    ) -> None:
        inv = self.runtime.inverters[inverter_id]
        inv.features = selection.features
        inv.awaiting_selection = not selection.chosen
        if not selection_changed:
            return
        if self.publisher is not None:
            self.publisher.set_selection(inverter_id, selection.active, revision)
        try:
            inv.set_selection(selection.active, revision)
        except SelectionError:  # validated before it was persisted; never expected
            log.exception(
                "%s: selection revision %d rejected by the plan builder", inverter_id, revision
            )
            raise

    def revoke(self, inverter_id: str, reason: str) -> None:
        self.runtime.inverters[inverter_id].block_for_recovery(reason)


def build_publisher(
    cfg: AppConfig,
    runtime: Runtime,
    mqtt: MqttConfig,
    instance_id: str,
    *,
    factory: ConnectionFactory | None = None,
    directory: Path | None = None,
    settings: PublisherSettings | None = None,
) -> MqttPublisher:
    """The publisher for a finished runtime (selection, generation, serial and refresh hooks)."""
    ns = Namespace(instance_id, mqtt.discovery_prefix, mqtt.topic_prefix)
    settings = settings or PublisherSettings()
    invs = runtime.inverters
    runtime.settings_polling = True  # the entities poll the settings: part of the bus load
    store = open_discovery_store(data_dir(directory) / "discovery.json")
    publisher = MqttPublisher(
        ns,
        [InverterInfo(i.id, i.name, invs[i.id].profile, i.poll) for i in cfg.inverters],
        store,
        factory or aiomqtt_factory(ns, mqtt, settings),
        birth_topic=mqtt.birth_topic,
        selections={i: (r.selection, r.selection_revision) for i, r in invs.items()},
        series_of=lambda i: invs[i].energy.series,
        serial_of=lambda i: invs[i].seen_serial,
        request_refresh=lambda i: invs[i].request_refresh(),
        diagnostics_of=lambda i: invs[i].diagnostics(),
        totals=build_totals(cfg, runtime, ns, store, directory),
        settings=settings,
        plane=SettingsPlane(
            ns,
            {i.id: i.poll for i in cfg.inverters},
            runtime.writes,
            clock=runtime.writes.clock,  # the intent deadlines are in the service's clock
        ),
    )
    if publisher.plane is not None:
        runtime.settings_retry_s = publisher.plane.retry_cadence_s  # recovery load in the budget
    return publisher


def _parallel_role(runtime: Runtime, inverter_id: str) -> str | None:
    """``master`` / ``slave`` as the device reports it (R336); ``None`` if it did not say."""
    role = runtime.inverters[inverter_id].identity_info.get("role")
    if not isinstance(role, Mapping) or role.get("parallel_enable") is not True:
        return None
    master = role.get("master")
    return None if not isinstance(master, bool) else ("master" if master else "slave")


def build_totals(
    cfg: AppConfig,
    runtime: Runtime,
    ns: Namespace,
    store: JsonStore,
    directory: Path | None,
) -> PlantTotals | None:
    """The plant totals of the configured members, or ``None`` while the plant device does not
    exist (fewer than two members, unless a single one is enabled)."""
    if not cfg.totals.active(cfg.inverters):
        return None
    members = set(cfg.totals.members(cfg.inverters))
    invs = runtime.inverters
    entry = store.data.get("devices", {}).get(ns.discovery_topic(ns.app_device_id), {})
    known = known_series([*entry.get("components", {}), *entry.get("retired", {})])
    return PlantTotals(
        [
            TotalsMember(i.id, i.name, invs[i.id].profile, i.poll)
            for i in cfg.inverters
            if i.id in members
        ],
        PlantSeries(data_dir(directory), known),
        keys_of=lambda i: (
            invs[i].selection - frozenset(invs[i].unreported) - frozenset(invs[i].inactive)
        ),
        role_of=lambda i: _parallel_role(runtime, i),
        member_series_of=lambda i: invs[i].energy.series,
    )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


class Application:
    """The running generation: inverter runtime, MQTT publisher and their wiring. A plant change
    stops the whole generation in order and starts a new one (``switch``); the web interface, the
    selection manager and the sink relay live across generations."""

    def __init__(
        self,
        options: AppConfig,
        instance_id: str,
        mqtt: MqttConfig,
        manager: SelectionManager,
        relay: RelaySink,
    ) -> None:
        self.options = options
        self.instance_id = instance_id
        self.mqtt = mqtt
        self.manager = manager
        self.relay = relay
        self.cfg = options
        self.web: WebApi | None = None
        self.runtime: Runtime | None = None
        self.publisher: MqttPublisher | None = None
        self.publisher_task: asyncio.Task[None] | None = None
        self.running = False
        self.unaccepted = False
        self.proposing = False
        # The plant revision commands are bound to (set once the plant service exists): a second
        # transport (MQTT) must not depend on a web request having wired it first.
        self.plant_revision: Callable[[], int | None] = lambda: None

    def install(self, cfg: AppConfig) -> Runtime:
        """Build runtime and publisher for ``cfg`` (synchronous: nothing awaits in between, so the
        web interface never sees a half-built generation).

        While ``proposing`` is set (by ``switch``) the generation is only proposed: it touches no
        shared file (identity bindings stay in memory) and its publisher does not run (no
        discovery, no state) until ``accept_generation()``; ``stop_generation()`` discards it."""
        provisional, self.proposing = self.proposing, False
        initial = self.manager.initial()
        runtime = Runtime(
            cfg,
            sink=self.relay,
            features={i: f for i, (f, _, _) in initial.items()},
            selections={i: (keys, rev) for i, (_, keys, rev) in initial.items()},
            provisional=provisional,
        )
        for inverter_id, inv in runtime.inverters.items():
            inv.awaiting_selection = not self.manager.selection(inverter_id).chosen
        runtime.writes.plant_revision = lambda: self.plant_revision()
        publisher: MqttPublisher | None = None
        if self.mqtt.host is None:
            log.error("no MQTT broker configured and no MQTT service found: nothing is published")
        else:
            publisher = build_publisher(cfg, runtime, self.mqtt, self.instance_id)
        self.relay.target = publisher if publisher is not None and not provisional else NullSink()
        self.manager.attach(LiveApplier(runtime, publisher))
        self.cfg, self.runtime, self.publisher = cfg, runtime, publisher
        self.unaccepted = provisional
        if self.web is not None:
            self.web.runtime, self.web.publisher = runtime, publisher
        return runtime

    def _start_publisher(self) -> None:
        if self.publisher is not None:
            self.publisher_task = asyncio.create_task(self.publisher.run(), name="mqtt-publisher")
            self.publisher_task.add_done_callback(_log_publisher_end)

    async def start_generation(self) -> None:
        assert self.runtime is not None
        if not self.unaccepted:
            self._start_publisher()
        await self.runtime.start()
        self.running = True

    def accept_generation(self) -> None:
        """The proposed generation is durable: write its held-back bindings and let its publisher
        run."""
        assert self.runtime is not None
        if not self.unaccepted:
            return
        self.unaccepted = False
        self.runtime.book.accept()
        if self.publisher is not None:
            self.relay.target = self.publisher
        self._start_publisher()

    async def stop_generation(self) -> None:
        self.running = False
        if self.runtime is not None:
            await self.runtime.stop()
        publisher, task = self.publisher, self.publisher_task
        self.publisher_task = None
        if publisher is not None and task is not None:
            publisher.request_stop()  # inverters and the app go offline, then DISCONNECT
            try:
                await asyncio.wait_for(task, publisher.stop_budget_s)
            except TimeoutError, asyncio.CancelledError:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    async def switch(
        self,
        cfg: AppConfig,
        specs: Mapping[str, InverterSpec],
        *,
        restore: SpecsUndo | None = None,
        fallback: bool = True,
    ) -> SpecsUndo:
        """Replace the running generation by one for ``cfg`` and return what takes the selection
        state back (the caller holds the selection manager's lock).

        A proposed generation (``restore`` is None) stays unaccepted until ``accept_generation()``.
        ``restore`` is the roll-back of such a proposal: the selection state is put back exactly
        and the generation is the accepted one. If the new generation cannot be started, the
        previous one is rebuilt when ``fallback`` is set; ``SwitchError`` says whether that worked.
        Without a fallback (or when it fails too) nothing is left running."""
        previous_cfg = self.cfg
        await self.stop_generation()
        if restore is not None:
            self.manager.restore_specs(restore)
            undo = restore
        else:
            undo = self.manager.set_specs(specs)
        try:
            self.proposing = restore is None
            self.install(cfg)
            await self.start_generation()
        except Exception:
            log.exception("new plant could not be started")
            self.proposing = False
            self.manager.restore_specs(undo)
            try:
                await self.stop_generation()
            except Exception:
                log.exception("the new generation did not stop cleanly")
            if not fallback:
                raise SwitchError(restored=False) from None
            try:
                self.install(previous_cfg)
                await self.start_generation()
            except Exception:
                log.exception("the previous plant could not be restored")
                with contextlib.suppress(Exception):
                    await self.stop_generation()
                raise SwitchError(restored=False) from None
            raise SwitchError(restored=True) from None
        return undo

    def forget_binding(self, inverter_id: str) -> bool:
        assert self.runtime is not None
        self.runtime.book.reset(inverter_id)
        return True


async def serve(cfg: AppConfig, stop: asyncio.Event) -> None:
    """Run the device loops until ``stop`` is set, then shut down in order."""
    instance_id = resolve_instance_id(cfg)
    mqtt = with_supervisor_mqtt(cfg.mqtt, os.environ)
    devices = os.environ.get(NO_DEVICES_ENV) != "1"
    directory = data_dir()
    plant_store = PlantStore(directory)
    for problem in plant_store.problems:
        log.error("plant store: %s (%s)", problem.code, problem.subject)
    effective = cfg.with_plant(plant_store.plant)
    relay = RelaySink()
    manager = SelectionManager(
        SelectionStore(directory, specs_from_config(effective), require_choice=True)
    )
    for problem in manager.store.problems:
        if problem.code in STALE_CODES:
            log.info(
                "selection store: stale %s dropped (%s)", problem.detail or "entry", problem.subject
            )
            continue
        log.error("selection store: %s (%s) %s", problem.code, problem.subject, problem.detail)
    manager.prune_locked()  # stale records of removed inverters must not outlive a restart
    application = Application(effective, instance_id, mqtt, manager, relay)
    log.info(
        "deye_monitor %s ready: %d endpoint(s), %d inverter(s), instance %s, mqtt %s, "
        "device loops %s",
        __version__,
        len(effective.endpoints),
        len(effective.inverters),
        instance_id,
        f"{mqtt.host}:{mqtt.port}" if mqtt.host else "not configured",
        "started" if devices else "disabled",
    )
    web: WebApi | None = None
    if devices:
        runtime = application.install(effective)
        service = PlantService(plant_store, cfg, manager, application)
        application.plant_revision = lambda: service.revision
        web = WebApi(runtime, manager, publisher=application.publisher, plant=service)
        application.web = web
        await application.start_generation()
        try:
            await web.start()
        except OSError as exc:  # the status/selection UI is optional; monitoring continues
            log.error("ingress web server could not start (%s)", type(exc).__name__)
            web = None
            application.web = None
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        if web is not None:
            await web.stop()
        if devices:
            async with manager.lock:  # a plant change in progress finishes first
                await application.stop_generation()


def _log_publisher_end(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("MQTT publisher stopped unexpectedly: %s", type(exc).__name__)


async def _run(cfg: AppConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await serve(cfg, stop)


def main() -> int:
    _configure_logging("info")
    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("invalid configuration:\n%s", exc)
        return EXIT_CONFIG
    _configure_logging(cfg.log_level)
    try:
        asyncio.run(_run(cfg))
    except ConfigError as exc:
        log.error("invalid configuration:\n%s", exc)
        return EXIT_CONFIG
    return 0
