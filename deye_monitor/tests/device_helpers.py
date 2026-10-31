"""Rig for device-loop tests: fake servers per endpoint, recording sink, controllable clock."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deye_monitor.config import AppConfig, EndpointConfig, parse_config
from deye_monitor.device import InverterRuntime, InverterState, RuntimeSettings, Snapshot
from deye_monitor.profiles.decode import Value
from deye_monitor.profiles.features import Features
from deye_monitor.runtime import Runtime
from deye_monitor.transport.modbus_tcp import tcp_connector
from tests.fakes.servers import SERIAL, FakeServer, Kind
from tests.transport_helpers import FakeClock

KIND_OF: dict[str, Kind] = {"modbus_tcp": "mbap", "rtu_over_tcp": "rtu", "solarman_v5_tcp": "v5"}


def serial_words(serial: str) -> dict[int, int]:
    assert len(serial) == 10
    return {3 + i: (ord(serial[2 * i]) << 8) | ord(serial[2 * i + 1]) for i in range(5)}


def device_regs(serial: str, *, soc: int | None = None, **extra: int) -> dict[int, int]:
    """A plausible standalone SG04LP3: R0, R22 (2 MPPT / 3 phases), R24, R336 (no parallel mode)
    and a printable serial number. ``soc`` fills the inverter SOC (R588) and the BMS SOC (R214):
    the default template reads one of them, depending on the BMS answer."""
    regs = {0: 0x0500, 22: 0x0203, 24: 1, 15: 0x1001, 17: 0x1000, 18: 0x0070, 65: 0x0104, 336: 0}
    regs.update(serial_words(serial))
    if soc is not None:
        regs.update({588: soc, 214: soc})
    regs.update({int(k[1:]): v for k, v in extra.items()})  # r588=55 -> {588: 55}
    return regs


@dataclass
class Recorder:
    states: list[tuple[str, Snapshot, Mapping[str, Value]]] = field(default_factory=list)
    availability: list[tuple[str, InverterState, str]] = field(default_factory=list)
    diagnostics: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def on_state(self, inverter_id: str, snapshot: Snapshot, values: Mapping[str, Value]) -> None:
        self.states.append((inverter_id, snapshot, values))

    def on_availability(self, inverter_id: str, state: InverterState, reason: str) -> None:
        self.availability.append((inverter_id, state, reason))

    def on_diagnostics(self, inverter_id: str, diagnostics: Mapping[str, Any]) -> None:
        self.diagnostics.append((inverter_id, dict(diagnostics)))

    def values(self, inverter_id: str, key: str) -> list[Value]:
        return [v[key] for i, _, v in self.states if i == inverter_id and key in v]

    def events(self, name: str) -> list[dict[str, Any]]:
        return [d for _, d in self.diagnostics if d.get("event") == name]


def config(
    endpoints: list[dict[str, Any]], inverters: list[dict[str, Any]], **extra: Any
) -> AppConfig:
    eps = [{"request_gap_ms": 0, "timeout_s": 0.2, **e} for e in endpoints]
    return parse_config({"endpoints": eps, "inverters": inverters, "mqtt": {}, **extra})


@dataclass
class Rig:
    cfg: AppConfig
    servers: dict[str, FakeServer]
    runtime: Runtime
    sink: Recorder
    clock: FakeClock
    directory: Path

    def inv(self, inverter_id: str) -> InverterRuntime:
        return self.runtime.inverters[inverter_id]

    async def step_until(self, inverter_id: str, predicate: Any, *, max_steps: int = 12) -> None:
        """Step the inverter (jumping over cooldowns) until ``predicate(inv)`` holds."""
        inv = self.inv(inverter_id)
        for _ in range(max_steps):
            await inv.step()
            if predicate(inv):
                return
            self.clock.pass_cooldown()
        raise AssertionError(f"condition not reached; state={inv.state} reason={inv.reason!r}")


@contextlib.asynccontextmanager
async def rig(
    endpoints: list[dict[str, Any]],
    inverters: list[dict[str, Any]],
    banks: Mapping[str, Mapping[int, int]],
    directory: Path,
    *,
    settings: RuntimeSettings | None = None,
    features: Mapping[str, Features] | None = None,
    bms: bool = False,
    sink: Recorder | None = None,
    clock: FakeClock | None = None,
    runtime_kw: Mapping[str, Any] | None = None,
    **extra: Any,
) -> AsyncIterator[Rig]:
    cfg = config(endpoints, inverters, **extra)
    if features is None and not bms:
        # the inverter's own battery values (SOC, temperature) are in the template without a BMS
        features = {i.id: Features(bms=False) for i in cfg.inverters}
    clock = clock or FakeClock()
    servers: dict[str, FakeServer] = {}
    for ep in cfg.endpoints:
        server = FakeServer(KIND_OF[ep.transport.value], logger_serial=ep.logger_serial or SERIAL)
        server.responder.regs = dict(banks.get(ep.id, device_regs("2401234567")))
        servers[ep.id] = await server.start()

    def factory(ep: EndpointConfig) -> Any:
        port = servers[ep.id].port
        return lambda: tcp_connector("127.0.0.1", port)

    recorder = sink or Recorder()
    options: dict[str, Any] = {
        "directory": directory,
        "sink": recorder,
        "clock": clock,
        "wall": lambda: 1_700_000_000.0,
        "rand": lambda: 0.0,
        "connector_factory": factory,
        "settings": settings,
        "features": features,
        **(runtime_kw or {}),
    }
    runtime = Runtime(cfg, **options)
    try:
        yield Rig(cfg, servers, runtime, recorder, clock, directory)
    finally:
        await runtime.stop()
        for server in servers.values():
            await server.stop()


def ep(endpoint_id: str, transport: str = "modbus_tcp", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": endpoint_id, "transport": transport, "host": "127.0.0.1"}
    if transport == "solarman_v5_tcp":
        base["logger_serial"] = SERIAL
    return {**base, **kw}


def inverter(inverter_id: str, endpoint: str, **kw: Any) -> dict[str, Any]:
    return {"id": inverter_id, "name": inverter_id, "endpoint": endpoint, "unit_id": 1, **kw}


def state_of(inv: InverterRuntime) -> InverterState:
    """The current state (a function call keeps type checkers from narrowing across awaits)."""
    return inv.state


class GatedTransport:
    """Test proxy around a real transport: hold answers, fail requests, run hooks.

    The real request is always executed (the Fake sees it); ``gates`` delay only the *return* to
    the device loop, which is how an answer that is "in flight" during a rebind or selection
    change is reproduced deterministically."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.gates: dict[int, asyncio.Event] = {}
        self.entered: dict[int, asyncio.Event] = {}
        self.fail_before: dict[int, Exception] = {}
        self.after_request: list[Any] = []  # callables(request_number, start) run after a request
        self.requests: list[tuple[int, int, int]] = []

    @property
    def counters(self) -> Any:
        return self.inner.counters

    @property
    def connection_epoch(self) -> int:
        return int(self.inner.connection_epoch)

    async def read_holding(self, unit: int, start: int, count: int) -> list[int]:
        self.requests.append((unit, start, count))
        if start in self.fail_before:
            raise self.fail_before[start]
        try:
            words: list[int] = await self.inner.read_holding(unit, start, count)
        finally:
            for hook in list(self.after_request):
                hook(len(self.requests), start)
        if start in self.entered:
            self.entered[start].set()
        if start in self.gates:
            await self.gates[start].wait()
        return words

    async def close(self) -> None:
        await self.inner.close()

    def gate(self, start: int) -> tuple[asyncio.Event, asyncio.Event]:
        """Hold the answer of the request that starts at ``start``; returns (entered, release)."""
        self.gates[start] = asyncio.Event()
        self.entered[start] = asyncio.Event()
        return self.entered[start], self.gates[start]


def install_gate(r: Rig, inverter_id: str, route: int = 0) -> GatedTransport:
    proxy = GatedTransport(r.inv(inverter_id).routes[route].transport)
    r.inv(inverter_id).routes[route].transport = proxy
    return proxy
