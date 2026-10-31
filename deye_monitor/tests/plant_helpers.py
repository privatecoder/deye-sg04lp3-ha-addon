"""Rig for the plant setup (T14): the real ``Application`` (runtime + live switch), plant service
and web API on local fake devices. Nothing here talks to a real device or broker."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from deye_monitor.app import Application
from deye_monitor.config import AppConfig, PlantConfig, parse_config
from deye_monitor.device import InverterState, RelaySink
from deye_monitor.plant import PlantService, PlantStore
from deye_monitor.selection import SelectionManager, SelectionStore, specs_from_config
from deye_monitor.webapi import WebApi, WebSettings
from tests.device_helpers import KIND_OF, device_regs
from tests.fakes.servers import SERIAL, FakePty, FakeServer, Kind

USER = {"X-Remote-User-Id": "u1", "X-Remote-User-Name": "alice"}
A = "2401234567"
B = "2409876543"


@dataclass
class PlantRig:
    directory: Path
    options: AppConfig
    store: PlantStore
    manager: SelectionManager
    app: Application
    service: PlantService
    api: WebApi
    http: aiohttp.ClientSession
    base: str
    servers: dict[str, FakeServer] = field(default_factory=dict)
    migrated: bool = False

    async def add_server(
        self, name: str, transport: str, serial: str = A, **regs: int
    ) -> FakeServer:
        kind: Kind = KIND_OF[transport]
        server = FakeServer(kind, logger_serial=SERIAL)
        server.responder.regs = device_regs(serial, **regs)
        self.servers[name] = await server.start()
        return server

    def endpoint_body(
        self, name: str, transport: str = "modbus_tcp", **extra: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": name,
            "transport": transport,
            "host": "127.0.0.1",
            "port": self.servers[name].port,
            "timeout_s": 1.0,
            "request_gap_ms": 0,
        }
        if transport == "solarman_v5_tcp":
            body["logger_serial"] = SERIAL
        return {**body, **extra}

    async def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        revision: int | str | None = "auto",
        user: Mapping[str, str] | None = USER,
        csrf: str | None = "auto",
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        h: dict[str, str] = dict(user or {})
        if csrf == "auto" and user:
            _, session = await self.call("GET", "/api/session", user=user, csrf=None)
            csrf = session["csrf"]
        if csrf and csrf != "auto":
            h["X-CSRF-Token"] = csrf
        if revision == "auto":
            revision = self.service.revision
        if revision is not None and method in ("POST", "PUT"):
            h["If-Match"] = f'"{revision}"'
        h["Content-Type"] = "application/json"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        async with self.http.request(method, self.base + path, data=data, headers=h) as resp:
            text = await resp.text()
            try:
                return resp.status, json.loads(text)
            except ValueError:
                return resp.status, text

    async def wait_state(
        self, inverter_id: str, state: InverterState, *, seconds: float = 8
    ) -> None:
        import asyncio

        for _ in range(int(seconds * 50)):
            runtime = self.app.runtime
            if (
                runtime is not None
                and inverter_id in runtime.inverters
                and runtime.inverters[inverter_id].state is state
            ):
                return
            await asyncio.sleep(0.02)
        runtime = self.app.runtime
        have = (
            runtime.inverters[inverter_id].state
            if runtime and inverter_id in runtime.inverters
            else None
        )
        raise AssertionError(f"{inverter_id} did not reach {state}; it is {have}")


@contextlib.asynccontextmanager
async def plant_rig(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    options: Mapping[str, Any] | None = None,
    serial_dir: Path | None = None,
    peers: frozenset[str] = frozenset({"127.0.0.1"}),
) -> AsyncIterator[PlantRig]:
    monkeypatch.setenv("DEYE_MONITOR_DATA", str(directory))
    monkeypatch.delenv("DEYE_MONITOR_SUPERVISOR_MQTT_HOST", raising=False)
    # ``endpoints``/``inverters`` seed the plant store (a test shortcut: the app options carry none)
    given = dict(options or {})
    seed = {k: given.pop(k) for k in ("endpoints", "inverters") if k in given}
    opts = parse_config({"mqtt": {}, **given})
    store = PlantStore(directory)
    migrated = False
    if seed and store.status.value == "missing":
        store.commit(PlantConfig.model_validate(seed), 1)
        migrated = True
    effective = opts.with_plant(store.plant)
    relay = RelaySink()
    manager = SelectionManager(
        SelectionStore(directory, specs_from_config(effective), require_choice=True)
    )
    app = Application(effective, "testinst", opts.mqtt, manager, relay)
    runtime = app.install(effective)
    service = PlantService(store, opts, manager, app)
    api = WebApi(
        runtime,
        manager,
        plant=service,
        settings=WebSettings(
            "127.0.0.1", 0, peers, serial_dir=str(serial_dir or directory / "no-serial")
        ),
    )
    app.web = api
    await app.start_generation()
    port = await api.start()
    rig = PlantRig(directory, opts, store, manager, app, service, api, None, "")  # type: ignore[arg-type]
    try:
        async with aiohttp.ClientSession() as http:
            rig.http = http
            rig.base = f"http://127.0.0.1:{port}"
            rig.migrated = migrated
            yield rig
    finally:
        await api.stop()
        await app.stop_generation()
        for server in rig.servers.values():
            await server.stop()


def serial_symlink(directory: Path, pty: FakePty, name: str = "usb-test-if00") -> Path:
    """A ``by-id`` style directory with one symlink to the PTY of a fake serial peer."""
    by_id = directory / "by-id"
    by_id.mkdir(exist_ok=True)
    link = by_id / name
    if link.exists() or link.is_symlink():
        link.unlink()
    os.symlink(pty.path, link)
    return by_id


@contextlib.asynccontextmanager
async def plant_ui(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    user: bool = True,
    **kw: Any,
) -> AsyncIterator[tuple[PlantRig, str]]:
    """The plant rig behind the Ingress-like proxy of the browser tests; yields (rig, start url)."""
    from tests.ui_helpers import PREFIX, ingress_proxy

    async with plant_rig(directory, monkeypatch, **kw) as rig:
        assert rig.api.port is not None
        async with ingress_proxy(rig.api.port, user=user) as proxy_port:
            yield rig, f"http://127.0.0.1:{proxy_port}{PREFIX}/"
