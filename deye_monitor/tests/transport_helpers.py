"""Shared helpers for the transport test modules."""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from deye_monitor.protocol.guard import Framing, guard_frame
from deye_monitor.transport import (
    ModbusTcpTransport,
    RtuOverTcpTransport,
    SerialRtuTransport,
    SolarmanV5Transport,
    TransportSettings,
    serial_rtu,
)
from deye_monitor.transport.base import StreamTransport
from deye_monitor.transport.modbus_tcp import tcp_connector
from tests.fakes.servers import SERIAL, SERIAL_REGS, FakePty, FakeServer, Kind, value_of

TCP_KINDS: list[Kind] = ["mbap", "rtu", "v5"]
FRAMING = {"mbap": Framing.MBAP, "rtu": Framing.RTU, "v5": Framing.SOLARMAN_V5}


class FakeClock:
    """Monotonic clock with a manual offset: real time keeps flowing (windows and quiet periods
    are really waited for), ``pass_cooldown`` jumps over cooldowns and late windows."""

    def __init__(self) -> None:
        self.offset = 1000.0

    @property
    def t(self) -> float:
        return time.monotonic() + self.offset

    @t.setter
    def t(self, value: float) -> None:
        self.offset = value - time.monotonic()

    def __call__(self) -> float:
        return self.t

    def pass_cooldown(self) -> None:
        self.offset += 1000.0


def settings(timeout: float = 0.3, **over: Any) -> TransportSettings:
    base: dict[str, Any] = {
        "timeout_s": timeout,
        "resync_quiet_ms": 20.0,
        "rtu_late_window_s": 0.05,
        "resync_max_bytes": 512,
    }
    base.update(over)
    if "resync_budget_s" not in base:  # window + quiet + probe deadline (+ margin)
        base["resync_budget_s"] = base["rtu_late_window_s"] + 0.02 + timeout + 0.15
    return TransportSettings(**base)


def build(
    kind: Kind,
    port: int,
    *,
    timeout: float = 0.3,
    clock: Callable[[], float] = time.monotonic,
    connector: Any = None,
    identity_check: Callable[[list[int]], bool] | None = None,
    **over: Any,
) -> StreamTransport:
    kw: dict[str, Any] = {
        "connector": connector or (lambda: tcp_connector("127.0.0.1", port)),
        "settings": settings(timeout, **over),
        "clock": clock,
        "rand": lambda: 0.0,
    }
    if identity_check is not None:
        kw["identity_check"] = identity_check
    if kind == "mbap":
        return ModbusTcpTransport(**kw)
    if kind == "rtu":
        return RtuOverTcpTransport(**kw)
    return SolarmanV5Transport(logger_serial=SERIAL, **kw)


@contextlib.asynccontextmanager
async def running(kind: Kind, **kw: Any) -> AsyncIterator[tuple[FakeServer, StreamTransport]]:
    server = await FakeServer(kind).start()
    transport = build(kind, server.port, **kw)
    try:
        yield server, transport
    finally:
        await transport.close()
        await server.stop()


def expected(start: int, count: int) -> list[int]:
    return [SERIAL_REGS.get(a, value_of(a)) for a in range(start, start + count)]


def assert_wire_clean(server: FakeServer | FakePty, kind: Kind) -> None:
    """Everything the fake received must be whole, valid FC03 requests - nothing else."""
    frames = server.responder.frames
    assert frames, "nothing received"
    assert bytes(server.raw) == b"".join(frames)
    for frame in frames:
        guard_frame(
            frame,
            FRAMING[kind],
            unit_id=1,
            max_read_registers=60,
            logger_serial=SERIAL if kind == "v5" else None,
        )


def build_serial(
    path: str,
    *,
    timeout: float = 0.3,
    clock: Callable[[], float] = time.monotonic,
    identity_check: Callable[[list[int]], bool] | None = None,
    **over: Any,
) -> SerialRtuTransport:
    ref = serial_rtu(path)
    kw: dict[str, Any] = {
        "connector": ref.connector,
        "framing": ref.framing,
        "settings": settings(timeout, **over),
        "clock": clock,
        "rand": lambda: 0.0,
    }
    if identity_check is not None:
        kw["identity_check"] = identity_check
    return SerialRtuTransport(**kw)
