"""Application entry point and lifecycle (SPEC §2): device loops plus the MQTT publisher."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
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
from deye_monitor.device import RelaySink
from deye_monitor.ha_discovery import Namespace
from deye_monitor.mqtt import (
    ConnectionFactory,
    InverterInfo,
    MqttPublisher,
    PublisherSettings,
    aiomqtt_factory,
)
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.profiles.plan import SelectionError
from deye_monitor.runtime import Runtime
from deye_monitor.selection import (
    InverterSelection,
    SelectionManager,
    SelectionStore,
    specs_from_config,
)
from deye_monitor.store import JsonStore
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
    return MqttPublisher(
        ns,
        [InverterInfo(i.id, i.name, invs[i.id].profile, i.poll) for i in cfg.inverters],
        open_discovery_store(data_dir(directory) / "discovery.json"),
        factory or aiomqtt_factory(ns, mqtt, settings),
        birth_topic=mqtt.birth_topic,
        selections={i: (r.selection, r.selection_revision) for i, r in invs.items()},
        series_of=lambda i: invs[i].energy.series,
        serial_of=lambda i: invs[i].seen_serial,
        request_refresh=lambda i: invs[i].request_refresh(),
        diagnostics_of=lambda i: invs[i].diagnostics(),
        settings=settings,
    )


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


async def serve(cfg: AppConfig, stop: asyncio.Event) -> None:
    """Run the device loops until ``stop`` is set, then shut down in order."""
    instance_id = resolve_instance_id(cfg)
    mqtt = with_supervisor_mqtt(cfg.mqtt, os.environ)
    devices = os.environ.get(NO_DEVICES_ENV) != "1"
    relay = RelaySink()
    manager = SelectionManager(
        SelectionStore(data_dir(), specs_from_config(cfg), require_choice=True)
    )
    for problem in manager.store.problems:
        log.error("selection store: %s (%s) %s", problem.code, problem.subject, problem.detail)
    initial = manager.initial()
    runtime = (
        Runtime(
            cfg,
            sink=relay,
            features={i: f for i, (f, _, _) in initial.items()},
            selections={i: (keys, rev) for i, (_, keys, rev) in initial.items()},
        )
        if devices
        else None
    )
    publisher: MqttPublisher | None = None
    publisher_task: asyncio.Task[None] | None = None
    if runtime is not None:
        for inverter_id, inv in runtime.inverters.items():
            inv.awaiting_selection = not manager.selection(inverter_id).chosen
        if mqtt.host is None:
            log.error("no MQTT broker configured and no MQTT service found: nothing is published")
        else:
            publisher = build_publisher(cfg, runtime, mqtt, instance_id)
            relay.target = publisher
    log.info(
        "deye_monitor %s ready: %d endpoint(s), %d inverter(s), instance %s, mqtt %s, "
        "device loops %s",
        __version__,
        len(cfg.endpoints),
        len(cfg.inverters),
        instance_id,
        f"{mqtt.host}:{mqtt.port}" if mqtt.host else "not configured",
        "started" if devices else "disabled",
    )
    if publisher is not None:
        publisher_task = asyncio.create_task(publisher.run(), name="mqtt-publisher")
        publisher_task.add_done_callback(_log_publisher_end)
    web: WebApi | None = None
    if runtime is not None:
        manager.attach(LiveApplier(runtime, publisher))
        await runtime.start()
        web = WebApi(runtime, manager, publisher=publisher)
        try:
            await web.start()
        except OSError as exc:  # the status/selection UI is optional; monitoring continues
            log.error("ingress web server could not start (%s)", type(exc).__name__)
            web = None
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        if web is not None:
            await web.stop()
        if runtime is not None:
            await runtime.stop()
        if publisher is not None and publisher_task is not None:
            publisher.request_stop()  # inverters and the app go offline, then DISCONNECT
            try:
                await asyncio.wait_for(publisher_task, publisher.stop_budget_s)
            except TimeoutError, asyncio.CancelledError:
                publisher_task.cancel()
                with contextlib.suppress(BaseException):
                    await publisher_task


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
