"""RTU resynchronisation (CR1-01, CR1-04): uncertain state, drain, identity probe, budgets."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, cast

import pytest

from deye_monitor.protocol import modbus as m
from deye_monitor.protocol.modbus import ModbusFrameError
from deye_monitor.scheduler import BusScheduler
from deye_monitor.transport import ConnectBackoff, ConnectionLost, RequestTimeout
from deye_monitor.transport.base import ResyncFailed, StreamTransport
from tests.fakes.servers import Behavior, FakePty, FakeServer, Kind, value_of
from tests.transport_helpers import FakeClock, build, build_serial, expected

Env = Literal["tcp", "serial"]
ENVS: list[Env] = ["tcp", "serial"]


@dataclass
class Rig:
    name: Env
    t: StreamTransport
    link: FakeServer | FakePty
    clock: FakeClock

    @property
    def frames(self) -> list[bytes]:
        return self.link.responder.frames

    @property
    def probe_shapes(self) -> list[tuple[int, int]]:
        return [(int.from_bytes(f[2:4], "big"), int.from_bytes(f[4:6], "big")) for f in self.frames]


@contextlib.asynccontextmanager
async def rig(name: Env, *, deadline: float = 0.3, **kw: Any) -> AsyncIterator[Rig]:
    clock = FakeClock()
    if name == "tcp":
        server = await FakeServer("rtu").start()
        t = build("rtu", server.port, timeout=deadline, clock=clock, **kw)
        link: FakeServer | FakePty = server
    else:
        pty = FakePty()
        pty.start()
        t = build_serial(pty.path, timeout=deadline, clock=clock, **kw)
        link = pty
    try:
        yield Rig(name, t, link, clock)
    finally:
        await t.close()
        if isinstance(link, FakeServer):
            await link.stop()
        else:
            link.close()


def uncertain(r: Rig) -> bool:
    return bool(cast(Any, r.t)._uncertain)


def attached(r: Rig) -> bool:
    return cast(Any, r.t)._writer is not None


@pytest.mark.parametrize("env", ENVS)
async def test_stale_answer_inside_the_drain_window_is_discarded(env: Env) -> None:
    async with rig(env, deadline=0.05) as r:
        r.link.script = [Behavior(delay_s=0.06)]  # same unit, same count: indistinguishable
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        assert uncertain(r)
        assert attached(r) is (env == "serial")  # RTU-over-TCP rebuilds the connection
        r.clock.pass_cooldown()
        assert await r.t.read_holding(1, 100, 1) == [value_of(100)]  # not value_of(10)
        assert not uncertain(r)
        assert r.probe_shapes == [(10, 1), (3, 5), (100, 1)]


@pytest.mark.parametrize("env", ENVS)
async def test_stale_answer_during_the_probe_is_detected(env: Env) -> None:
    async with rig(env, deadline=0.12) as r:
        r.link.script = [Behavior(delay_s=0.18), Behavior(delay_s=0.3)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        with pytest.raises(ResyncFailed):  # the stale 1-register answer arrives as probe answer
            await r.t.read_holding(1, 100, 1)
        assert r.t.counters.resync_failures == 1
        assert uncertain(r)
        assert not attached(r)
        with pytest.raises(ConnectBackoff):
            await r.t.read_holding(1, 100, 1)


@pytest.mark.parametrize("env", ENVS)
async def test_probe_uses_a_different_length_than_the_failed_request(env: Env) -> None:
    async with rig(env, deadline=0.05) as r:
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 20, 5)
        r.clock.pass_cooldown()
        assert await r.t.read_holding(1, 30, 2) == expected(30, 2)
        assert r.probe_shapes == [(20, 5), (3, 6), (30, 2)]


@pytest.mark.parametrize("env", ENVS)
async def test_identity_mismatch_keeps_the_line_uncertain(env: Env) -> None:
    accept = {"ok": False}
    async with rig(env, deadline=0.05, identity_check=lambda words: accept["ok"]) as r:
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        with pytest.raises(ResyncFailed, match="identity"):
            await r.t.read_holding(1, 100, 1)
        assert uncertain(r)
        r.clock.pass_cooldown()
        accept["ok"] = True
        assert await r.t.read_holding(1, 100, 1) == [value_of(100)]
        assert not uncertain(r)


@pytest.mark.parametrize("env", ENVS)
async def test_default_identity_check_requires_printable_ascii(env: Env) -> None:
    async with rig(env, deadline=0.05) as r:
        r.link.responder.regs[3] = 0x0001  # not printable
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        with pytest.raises(ResyncFailed):
            await r.t.read_holding(1, 100, 1)


@pytest.mark.parametrize("env", ENVS)
async def test_probe_exception_or_garbage_fails_the_resync(env: Env) -> None:
    for probe_behavior, fail in (
        (Behavior(exception_code=2), ResyncFailed),
        (Behavior(garbage_before=b"\xff\xff\xff"), ResyncFailed),
    ):
        async with rig(env, deadline=0.05) as r:
            r.link.script = [Behavior(silent=True), probe_behavior]
            with pytest.raises(RequestTimeout):
                await r.t.read_holding(1, 10, 1)
            r.clock.pass_cooldown()
            with pytest.raises(fail):
                await r.t.read_holding(1, 100, 1)
            assert uncertain(r)


@pytest.mark.parametrize("env", ENVS)
async def test_garbage_reply_is_a_frame_error_and_resyncs(env: Env) -> None:
    async with rig(env, deadline=0.2) as r:
        r.link.script = [Behavior(garbage_before=b"\xff\xff\xff")]
        with pytest.raises(ModbusFrameError):
            await r.t.read_holding(1, 7, 1)
        assert uncertain(r)
        assert attached(r) is (env == "serial")
        with pytest.raises(ConnectBackoff):
            await r.t.read_holding(1, 7, 1)
        r.clock.pass_cooldown()
        assert await r.t.read_holding(1, 7, 1) == expected(7, 1)
        assert r.probe_shapes[1] == (3, 5)


# -- CR1-04: bounded cleanup, cancellation-safe -----------------------------------------------


@pytest.mark.parametrize("env", ENVS)
async def test_continuous_noise_exhausts_the_budget_and_releases_everything(env: Env) -> None:
    async with rig(env, deadline=0.05, resync_budget_s=0.3) as r:
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        r.link.start_noise(0.005)
        sched = BusScheduler(gap_s=0)
        started = time.monotonic()
        with pytest.raises(ResyncFailed, match="budget"):
            await sched.run(lambda: r.t.read_holding(1, 100, 1))
        assert time.monotonic() - started < 0.3 + 0.25
        r.link.stop_noise()
        assert sched.pending == 0
        assert not cast(Any, r.t)._lock.locked()
        assert not attached(r)
        assert uncertain(r)
        assert await asyncio.wait_for(sched.run(lambda: asyncio.sleep(0, "free")), 1) == "free"
        with pytest.raises(ConnectBackoff):  # failure cooldown, no tight retry loop
            await r.t.read_holding(1, 100, 1)


@pytest.mark.parametrize("env", ENVS)
async def test_noise_byte_limit(env: Env) -> None:
    async with rig(env, deadline=0.05, resync_max_bytes=10, resync_budget_s=1.0) as r:
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        r.link.start_noise(0.002)
        started = time.monotonic()
        with pytest.raises(ResyncFailed, match="noise"):
            await r.t.read_holding(1, 100, 1)
        assert time.monotonic() - started < 0.5
        r.link.stop_noise()


@pytest.mark.parametrize("env", ENVS)
async def test_cancellation_during_the_drain_leaves_no_clean_stream(env: Env) -> None:
    async with rig(env, deadline=0.05) as r:
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        r.link.start_noise(0.005)
        task = asyncio.create_task(r.t.read_holding(1, 100, 1))
        await asyncio.sleep(0.06)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        r.link.stop_noise()
        assert uncertain(r) and not attached(r)
        assert not cast(Any, r.t)._lock.locked()
        assert await r.t.read_holding(1, 100, 1) == [value_of(100)]  # cancellation is no failure
        assert not uncertain(r)


@pytest.mark.parametrize("env", ENVS)
async def test_cancellation_during_the_probe(env: Env) -> None:
    async with rig(env, deadline=5) as r:
        r.link.script = [Behavior(silent=True), Behavior(silent=True)]
        first = asyncio.create_task(r.t.read_holding(1, 10, 1))
        await asyncio.sleep(0.05)
        first.cancel()  # cancelled in the middle of an RTU exchange
        with pytest.raises(asyncio.CancelledError):
            await first
        assert uncertain(r) and not attached(r)
        second = asyncio.create_task(r.t.read_holding(1, 100, 1))
        for _ in range(100):
            if len(r.frames) >= 2:  # the probe went out
                break
            await asyncio.sleep(0.01)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert uncertain(r) and not attached(r)
        assert not cast(Any, r.t)._lock.locked()
        assert await r.t.read_holding(1, 100, 1) == [value_of(100)]


async def test_eof_during_the_drain() -> None:
    async with rig("tcp", deadline=0.05) as r:
        assert isinstance(r.link, FakeServer)
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        r.link.start_noise(0.005)
        task = asyncio.create_task(r.t.read_holding(1, 100, 1))
        await asyncio.sleep(0.05)
        r.link.kick()
        with pytest.raises(ConnectionLost):
            await task
        r.link.stop_noise()
        assert uncertain(r)
        assert r.t.counters.connection_lost == 1
        with pytest.raises(ConnectBackoff):
            await r.t.read_holding(1, 100, 1)


async def test_serial_hangup_during_resync_is_a_failure() -> None:
    async with rig("serial", deadline=0.05) as r:
        assert isinstance(r.link, FakePty)
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        r.link.hang_up()
        with pytest.raises((ConnectionLost, ResyncFailed, RequestTimeout)):
            await r.t.read_holding(1, 100, 1)
        assert uncertain(r)


# -- late-answer window (addendum to CR1-01) ------------------------------------------------------

WINDOW = {"rtu_late_window_s": 0.2, "backoff_min_s": 0.01, "backoff_max_s": 0.05}


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stale_delay", [0.12, 0.18])
async def test_old_answer_inside_the_window_is_discarded_even_after_the_quiet_time(
    env: Env, stale_delay: float
) -> None:
    async with rig(env, deadline=0.05, **WINDOW) as r:
        r.link.script = [Behavior(delay_s=stale_delay)]  # same unit and count as the next read
        started = time.monotonic()
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        await asyncio.sleep(0.02)  # let the (tiny) cooldown pass, the window is NOT skipped
        assert await r.t.read_holding(1, 100, 1) == [value_of(100)]
        assert time.monotonic() - started >= 0.2  # the window was really waited for
        assert r.probe_shapes == [(10, 1), (3, 5), (100, 1)]
        assert not uncertain(r)


@pytest.mark.parametrize("env", ENVS)
async def test_old_count_5_answer_with_count_6_probe(env: Env) -> None:
    seen: list[list[int]] = []

    def check(words: list[int]) -> bool:
        seen.append(list(words))
        return True

    async with rig(env, deadline=0.05, identity_check=check, **WINDOW) as r:
        r.link.script = [Behavior(delay_s=0.12)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 20, 5)
        await asyncio.sleep(0.02)
        assert await r.t.read_holding(1, 30, 5) == expected(30, 5)  # not the stale R20..R24
        assert r.probe_shapes == [(20, 5), (3, 6), (30, 5)]
        assert seen == [expected(3, 5)]  # only R3..R7 are the serial number, R8 is not passed on


@pytest.mark.parametrize("env", ENVS)
async def test_wrong_bound_serial_drops_the_stream_and_cools_down(env: Env) -> None:
    bound = expected(3, 5)

    def check(words: list[int]) -> bool:
        return words == bound

    async with rig(env, deadline=0.05, identity_check=check) as r:
        r.link.responder.regs[7] = 0x3939  # a different device answers on this line
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.clock.pass_cooldown()
        with pytest.raises(ResyncFailed, match="identity"):
            await r.t.read_holding(1, 100, 1)
        assert not attached(r)
        assert uncertain(r)
        with pytest.raises(ConnectBackoff):
            await r.t.read_holding(1, 100, 1)
        assert r.probe_shapes == [(10, 1), (3, 5)]  # no measurement was requested in that state


@pytest.mark.parametrize("env", ENVS)
async def test_nothing_is_returned_while_uncertain(env: Env) -> None:
    async with rig(env, deadline=0.05, identity_check=lambda words: False) as r:
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        for _ in range(3):
            r.clock.pass_cooldown()
            with pytest.raises(ResyncFailed):
                await r.t.read_holding(1, 100, 1)
        assert all(shape == (3, 5) for shape in r.probe_shapes[1:])  # only probes, never (100, 1)
        assert r.t.counters.successes == 0


async def test_gateway_reconnect_with_old_rtu_frames_on_the_new_connection() -> None:
    async with rig("tcp", deadline=0.05) as r:
        assert isinstance(r.link, FakeServer)
        stale = m.append_crc(bytes([1, 3, 2]) + value_of(10).to_bytes(2, "big"))  # R10 answer
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        r.link.on_connect = stale * 3  # the gateway replays old downstream frames on connect
        r.clock.pass_cooldown()
        assert await r.t.read_holding(1, 100, 1) == [value_of(100)]
        assert r.link.connections == 2


@pytest.mark.parametrize("env", ENVS)
async def test_cancellation_during_the_late_window(env: Env) -> None:
    async with rig(env, deadline=0.05, **WINDOW) as r:
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        await asyncio.sleep(0.02)
        task = asyncio.create_task(r.t.read_holding(1, 100, 1))
        await asyncio.sleep(0.08)  # inside the 0.2 s window
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert uncertain(r) and not attached(r)
        assert not cast(Any, r.t)._lock.locked()
        assert r.probe_shapes == [(10, 1)]  # still no probe, no measurement
        assert await r.t.read_holding(1, 100, 1) == [
            value_of(100)
        ]  # waits out the rest of the window
        assert not uncertain(r)


async def test_eof_during_the_late_window() -> None:
    async with rig("tcp", deadline=0.05, **WINDOW) as r:
        assert isinstance(r.link, FakeServer)
        r.link.script = [Behavior(silent=True)]
        with pytest.raises(RequestTimeout):
            await r.t.read_holding(1, 10, 1)
        await asyncio.sleep(0.02)
        task = asyncio.create_task(r.t.read_holding(1, 100, 1))
        await asyncio.sleep(0.08)
        r.link.kick()
        with pytest.raises(ConnectionLost):
            await task
        assert uncertain(r) and not attached(r)
        assert not cast(Any, r.t)._lock.locked()


async def test_mbap_and_v5_are_never_uncertain() -> None:
    from tests.transport_helpers import running

    kinds: list[Kind] = ["mbap", "v5"]
    for kind in kinds:
        async with running(kind, timeout=0.05) as (server, t):
            server.script = [Behavior(silent=True)]
            with pytest.raises(RequestTimeout):
                await t.read_holding(1, 10, 1)
            assert cast(Any, t)._uncertain is False
