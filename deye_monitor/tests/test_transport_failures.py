"""Failure model and cooldown (CR1-08): which events are failures, how the cooldown grows."""

from __future__ import annotations

from typing import Any, cast

import pytest

from deye_monitor.protocol.modbus import ModbusFrameError
from deye_monitor.transport import ConnectBackoff, RequestTimeout
from deye_monitor.transport.base import ResyncFailed, StreamTransport
from tests.fakes.servers import Behavior, FakeServer, Kind
from tests.transport_helpers import TCP_KINDS, FakeClock, build, expected, running


def delay_of(t: StreamTransport, clock: FakeClock) -> float:
    return round(float(cast(Any, t)._not_before - clock.t), 1)  # clock keeps running


async def failing_series(
    kind: Kind, behavior: Behavior, attempts: int, deadline: float = 0.1
) -> tuple[list[float], FakeServer]:
    """Run ``attempts`` failing requests; return the cooldown after each one."""
    clock = FakeClock()
    server = await FakeServer(kind).start()
    t = build(kind, server.port, timeout=deadline, clock=clock)
    server.default = behavior
    delays: list[float] = []
    try:
        for _ in range(attempts):
            with pytest.raises((ModbusFrameError, ResyncFailed, RequestTimeout)):
                await t.read_holding(1, 100, 1)
            delays.append(delay_of(t, clock))
            with pytest.raises(ConnectBackoff):  # immediately blocked, also with a live connection
                await t.read_holding(1, 100, 1)
            clock.t += delays[-1] + 0.001
        return delays, server
    finally:
        await t.close()
        await server.stop()


EXPECTED = [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]

BAD_FRAMES = {
    "mbap": Behavior(raw_reply=b"\x00\x01\x00\x00\xff\xff\x01\x03\x02"),  # implausible length
    "rtu": Behavior(garbage_before=b"\xff\xff\xff"),
    "v5": Behavior(bad_inner_crc=True),
}


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_malformed_frames_grow_a_capped_cooldown(kind: Kind) -> None:
    delays, server = await failing_series(kind, BAD_FRAMES[kind], len(EXPECTED))
    assert delays == EXPECTED
    if kind == "v5":
        assert server.connections == 1  # the stream is intact: the live connection is kept ...
    # ... and the cooldown applied to it anyway (asserted inside failing_series)


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_silent_timeouts_grow_the_cooldown(kind: Kind) -> None:
    delays, _ = await failing_series(kind, Behavior(silent=True), 5, deadline=0.05)
    assert delays == EXPECTED[:5]


async def test_v5_inner_crc_failure_keeps_the_connection_during_cooldown() -> None:
    clock = FakeClock()
    async with running("v5", clock=clock) as (server, t):
        server.script = [Behavior(bad_inner_crc=True)]
        with pytest.raises(ModbusFrameError):
            await t.read_holding(1, 100, 1)
        assert cast(Any, t)._writer is not None
        with pytest.raises(ConnectBackoff):
            await t.read_holding(1, 100, 1)
        assert len(server.frames) == 1  # nothing was sent while cooling down
        clock.pass_cooldown()
        assert await t.read_holding(1, 100, 1) == expected(100, 1)
        assert server.connections == 1


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_success_resets_the_cooldown(kind: Kind) -> None:
    clock = FakeClock()
    async with running(kind, clock=clock, timeout=0.05) as (server, t):
        silent = Behavior(silent=True)
        # RTU answers a resync probe (default behaviour) before the second request
        server.script = [silent, Behavior(), silent] if kind == "rtu" else [silent, silent]
        for want in (2.0, 4.0):
            with pytest.raises(RequestTimeout):
                await t.read_holding(1, 100, 1)
            assert delay_of(t, clock) == want
            clock.pass_cooldown()
        assert await t.read_holding(1, 100, 1) == expected(100, 1)
        assert cast(Any, t)._backoff.failures == 0
        assert cast(Any, t)._not_before == 0.0
        server.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await t.read_holding(1, 100, 1)
        assert delay_of(t, clock) == 2.0


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_modbus_exception_is_not_a_failure(kind: Kind) -> None:
    clock = FakeClock()
    async with running(kind, clock=clock) as (server, t):
        from deye_monitor.protocol.modbus import ModbusExceptionResponse

        server.script = [Behavior(exception_code=2)] * 3
        for _ in range(3):
            with pytest.raises(ModbusExceptionResponse):
                await t.read_holding(1, 100, 1)  # no ConnectBackoff in between
        assert cast(Any, t)._backoff.failures == 0
        assert t.counters.cooldown_rejections == 0


async def test_endpoints_are_independent() -> None:
    clock_a, clock_b = FakeClock(), FakeClock()
    sa = await FakeServer("mbap").start()
    sb = await FakeServer("mbap").start()
    a = build("mbap", sa.port, timeout=0.05, clock=clock_a)
    b = build("mbap", sb.port, timeout=0.05, clock=clock_b)
    try:
        sa.default = Behavior(silent=True)
        with pytest.raises(RequestTimeout):
            await a.read_holding(1, 1, 1)
        with pytest.raises(ConnectBackoff):
            await a.read_holding(1, 1, 1)
        assert await b.read_holding(1, 1, 1) == expected(1, 1)
    finally:
        await a.close()
        await b.close()
        await sa.stop()
        await sb.stop()
