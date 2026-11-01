from __future__ import annotations

import asyncio
import pathlib
import socket
from typing import Any, cast

import pytest

from deye_monitor.protocol.modbus import ModbusExceptionResponse
from deye_monitor.protocol.solarman_v5 import V5LoggerError
from deye_monitor.scheduler import BusScheduler
from deye_monitor.transport import (
    ConnectBackoff,
    ConnectionLost,
    ModbusTcpTransport,
    RequestTimeout,
    RtuOverTcpTransport,
    SerialRtuTransport,
    SolarmanV5Transport,
    TransportSettings,
    serial_rtu,
)
from deye_monitor.transport.base import Backoff
from tests.fakes.servers import Behavior, FakePty, FakeServer, Kind
from tests.transport_helpers import (
    TCP_KINDS,
    FakeClock,
    assert_wire_clean,
    build,
    expected,
    running,
    settings,
)


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_read_tcp_transports(kind: Kind) -> None:
    async with running(kind) as (server, t):
        assert await t.read_holding(1, 586, 7) == expected(586, 7)
        assert await t.read_holding(1, 0, 60) == expected(0, 60)
        assert await t.read_holding(1, 65535, 1) == expected(65535, 1)
        assert server.connections == 1
        assert t.counters.successes == 3
        assert_wire_clean(server, kind)


async def test_read_serial_rtu() -> None:
    pty = FakePty()
    pty.start()
    t = serial_rtu(pty.path, 9600, settings(0.5))
    try:
        assert await t.read_holding(1, 586, 7) == expected(586, 7)
        assert_wire_clean(pty, "rtu")
    finally:
        await t.close()
        pty.close()


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_timeout(kind: Kind) -> None:
    async with running(kind, timeout=0.1) as (server, t):
        server.default = Behavior(silent=True)
        with pytest.raises(RequestTimeout):
            await t.read_holding(1, 0, 1)
        assert t.counters.timeouts == 1


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_late_response_not_assigned_to_next_request(kind: Kind) -> None:
    clock = FakeClock()
    async with running(kind, timeout=0.1, clock=clock) as (server, t):
        server.script = [Behavior(delay_s=0.15)]
        with pytest.raises(RequestTimeout):
            await t.read_holding(1, 10, 3)
        clock.pass_cooldown()
        assert await t.read_holding(1, 100, 2) == expected(100, 2)
        if kind == "rtu":
            assert t.counters.resyncs == 1
            assert server.connections == 2
            starts = [
                (int.from_bytes(f[2:4], "big"), int.from_bytes(f[4:6], "big"))
                for f in server.frames
            ]
            assert starts == [(10, 3), (3, 5), (100, 2)]  # request, resync probe, request
        elif kind == "mbap":
            assert server.connections == 2  # every MBAP timeout drops the connection
        else:
            assert server.connections == 1
        assert_wire_clean(server, kind)


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_dribbling_within_and_beyond_deadline(kind: Kind) -> None:
    async with running(kind, timeout=0.5) as (server, t):
        server.default = Behavior(dribble_gap_s=0.002)
        assert await t.read_holding(1, 5, 2) == expected(5, 2)
    async with running(kind, timeout=0.1) as (server, t):
        server.default = Behavior(dribble_gap_s=0.03)
        with pytest.raises(RequestTimeout):
            await t.read_holding(1, 5, 2)


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_eof_then_reconnect_after_cooldown(kind: Kind) -> None:
    clock = FakeClock()
    async with running(kind, clock=clock) as (server, t):
        assert await t.read_holding(1, 0, 1) == expected(0, 1)
        server.script = [Behavior(close=True)]
        with pytest.raises(ConnectionLost):
            await t.read_holding(1, 0, 1)
        with pytest.raises(ConnectBackoff):
            await t.read_holding(1, 0, 1)
        clock.pass_cooldown()
        assert await t.read_holding(1, 0, 1) == expected(0, 1)
        assert t.counters.reconnects == 1
        assert server.connections == 2


async def test_connect_failure_cooldown_grows() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    clock = FakeClock()
    t = build("mbap", port, clock=clock)
    delays: list[float] = []
    for _ in range(4):
        with pytest.raises(ConnectionLost):
            await t.read_holding(1, 0, 1)
        delays.append(round(cast(Any, t)._not_before - clock.t, 1))
        with pytest.raises(ConnectBackoff):
            await t.read_holding(1, 0, 1)
        clock.t += delays[-1] + 0.001
    assert delays == [2.0, 4.0, 8.0, 16.0]
    assert t.counters.connect_failures == 4


def test_backoff_sequence_and_jitter() -> None:
    b = Backoff(2.0, 60.0, rand=lambda: 0.0)
    assert [b.next_delay() for _ in range(7)] == [2, 4, 8, 16, 32, 60, 60]
    b.reset()
    assert b.next_delay() == 2
    hi = Backoff(2.0, 60.0, rand=lambda: 1.0)
    assert [hi.next_delay() for _ in range(6)] == [2.5, 5.0, 10.0, 20.0, 40.0, 60.0]
    for _ in range(100):
        assert hi.next_delay() <= 60.0


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_modbus_exception_keeps_connection_and_is_no_failure(kind: Kind) -> None:
    async with running(kind) as (server, t):
        server.script = [Behavior(exception_code=2)]
        with pytest.raises(ModbusExceptionResponse):
            await t.read_holding(1, 0, 1)
        assert await t.read_holding(1, 0, 1) == expected(0, 1)  # no cooldown
        assert server.connections == 1
        assert t.counters.cooldown_rejections == 0


async def test_v5_heartbeats_and_double_crc() -> None:
    async with running("v5") as (server, t):
        server.script = [Behavior(heartbeat=True, double_crc=True)]
        assert await t.read_holding(1, 20, 4) == expected(20, 4)
        assert isinstance(t, SolarmanV5Transport)
        assert t.v5_counters.ignored_control == 2
        assert t.v5_counters.quirk_double_crc == 1
        assert_wire_clean(server, "v5")
        assert len(server.frames) == 1  # nothing was sent in reaction to the heartbeats


@pytest.mark.parametrize(
    ("frame_type", "status"), [(0x00, 0x00), (0x02, 0x00), (0x01, 0x01), (0x02, 0x02), (0xFF, 0x01)]
)
async def test_v5_unsupported_metadata_yields_no_data(frame_type: int, status: int) -> None:
    clock = FakeClock()
    async with running("v5", clock=clock) as (server, t):
        assert isinstance(t, SolarmanV5Transport)
        server.script = [Behavior(v5_frame_type=frame_type, v5_status=status)]
        with pytest.raises(V5LoggerError, match="unsupported"):
            await t.read_holding(1, 20, 2)  # the inner RTU frame is valid but must not be used
        assert t.v5_counters.unsupported_metadata == 1
        assert cast(Any, t)._writer is not None  # stream is intact
        with pytest.raises(ConnectBackoff):  # a logger error is a failure for the cooldown
            await t.read_holding(1, 20, 2)
        clock.pass_cooldown()
        assert await t.read_holding(1, 20, 2) == expected(20, 2)


async def test_v5_sequence_high_byte_may_differ() -> None:
    async with running("v5") as (server, t):
        server.default = Behavior(v5_seq_high=0x07)
        assert await t.read_holding(1, 20, 2) == expected(20, 2)


# -- MBAP: timeout inside a response (CR1-02) --------------------------------------------------

MBAP_REPLY_LEN = 13  # 2 registers: 7 byte header + fc + bytecount + 4 data bytes


async def _partial_case(kind: Kind, reply_len: int, n: int) -> tuple[bool, int, list[int]]:
    clock = FakeClock()
    server = await FakeServer(kind).start()
    t = build(kind, server.port, timeout=0.08, clock=clock)
    try:
        server.script = [Behavior(split_at=n, split_pause_s=0.12)]
        with pytest.raises(RequestTimeout):
            await t.read_holding(1, 10, 2)
        dropped = cast(Any, t)._writer is None
        clock.pass_cooldown()
        values = await t.read_holding(1, 100, 2)
        return dropped, server.connections, values
    finally:
        await t.close()
        await server.stop()


async def test_mbap_timeout_at_every_header_and_body_position() -> None:
    results = await asyncio.gather(
        *(_partial_case("mbap", MBAP_REPLY_LEN, n) for n in range(MBAP_REPLY_LEN))
    )
    for n, (dropped, connections, values) in enumerate(results):
        assert values == expected(100, 2), n
        assert (dropped, connections) == (True, 2), n  # no "nothing read yet" exception


async def test_mbap_timeout_always_drops_the_connection() -> None:
    clock = FakeClock()
    async with running("mbap", timeout=0.1, clock=clock) as (server, t):
        server.script = [Behavior(delay_s=0.3)]  # not a single byte before the deadline
        with pytest.raises(RequestTimeout):
            await t.read_holding(1, 10, 2)
        assert cast(Any, t)._writer is None
        clock.pass_cooldown()
        assert await t.read_holding(1, 100, 2) == expected(100, 2)
        assert server.connections == 2


async def test_mbap_complete_stale_frame_is_dropped_by_tid() -> None:
    async with running("mbap") as (server, t):
        server.script = [Behavior(stale_frame_first=True)]
        assert await t.read_holding(1, 100, 2) == expected(100, 2)
        assert t.counters.stale_frames == 1
        assert server.connections == 1


# -- V5: parser lifecycle (CR1-03) --------------------------------------------------------------

V5_REPLY_LEN = 36  # 2 registers: 11 header + 14 metadata + 9 RTU + checksum + end


async def _v5_eof_case(n: int) -> tuple[int, list[int]]:
    clock = FakeClock()
    server = await FakeServer("v5").start()
    t = build("v5", server.port, timeout=0.3, clock=clock)
    assert isinstance(t, SolarmanV5Transport)
    try:
        server.script = [Behavior(split_at=n, drop_rest=True, close_after=True)]
        with pytest.raises(ConnectionLost):
            await t.read_holding(1, 10, 2)
        buffered_after_drop = t.v5.buffered
        clock.pass_cooldown()
        return buffered_after_drop, await t.read_holding(1, 100, 2)
    finally:
        await t.close()
        await server.stop()


async def test_v5_eof_at_every_byte_position_starts_the_next_connection_clean() -> None:
    results = await asyncio.gather(*(_v5_eof_case(n) for n in range(1, V5_REPLY_LEN)))
    for n, (buffered, values) in zip(range(1, V5_REPLY_LEN), results, strict=True):
        assert buffered == 0, n
        assert values == expected(100, 2), n


async def test_v5_oversized_prefix_does_not_poison_the_reconnect() -> None:
    # CR1 probe: eleven bytes of a frame that announces 400 payload bytes, then EOF
    clock = FakeClock()
    server = await FakeServer("v5").start()
    t = build("v5", server.port, clock=clock)
    assert isinstance(t, SolarmanV5Transport)
    try:
        junk = bytes([0xA5, 0x90, 0x01]) + bytes(8)
        server.script = [Behavior(raw_reply=junk, close_after=True)]
        with pytest.raises(ConnectionLost):
            await t.read_holding(1, 10, 2)
        assert t.v5.buffered == 0
        clock.pass_cooldown()
        assert await t.read_holding(1, 100, 2) == expected(100, 2)
    finally:
        await t.close()
        await server.stop()


async def _v5_partial_timeout_case(n: int) -> tuple[bool, int, list[int]]:
    clock = FakeClock()
    server = await FakeServer("v5").start()
    t = build("v5", server.port, timeout=0.08, clock=clock)
    try:
        server.script = [Behavior(split_at=n, split_pause_s=0.12)]
        with pytest.raises(RequestTimeout):
            await t.read_holding(1, 10, 2)
        dropped = cast(Any, t)._writer is None
        clock.pass_cooldown()
        values = await t.read_holding(1, 100, 2)
        return dropped, server.connections, values
    finally:
        await t.close()
        await server.stop()


async def test_v5_incomplete_frame_outliving_the_deadline_drops_the_connection() -> None:
    results = await asyncio.gather(*(_v5_partial_timeout_case(n) for n in range(V5_REPLY_LEN)))
    for n, (dropped, connections, values) in enumerate(results):
        assert values == expected(100, 2), n
        assert (dropped, connections) == ((False, 1) if n == 0 else (True, 2)), n


async def test_v5_plausible_garbage_prefix_then_valid_frame() -> None:
    clock = FakeClock()
    async with running("v5", timeout=0.1, clock=clock) as (server, t):
        assert isinstance(t, SolarmanV5Transport)
        server.script = [Behavior(garbage_before=bytes([0xA5, 0x90, 0x01]))]
        with pytest.raises(RequestTimeout):  # the valid frame hides behind the announced 400 bytes
            await t.read_holding(1, 10, 2)
        assert cast(Any, t)._writer is None
        assert t.v5.buffered == 0
        clock.pass_cooldown()
        assert await t.read_holding(1, 100, 2) == expected(100, 2)


async def test_v5_cancel_and_close_clear_the_parser() -> None:
    for how in ("cancel", "close"):
        async with running("v5", timeout=5) as (server, t):
            assert isinstance(t, SolarmanV5Transport)
            server.script = [Behavior(split_at=12, split_pause_s=5, drop_rest=True)]
            task = asyncio.create_task(t.read_holding(1, 10, 2))
            for _ in range(100):
                if t.v5.buffered:
                    break
                await asyncio.sleep(0.01)
            assert t.v5.buffered > 0
            if how == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await t.close()
                with pytest.raises((ConnectionLost, asyncio.CancelledError)):
                    await task
            assert t.v5.buffered == 0
            assert cast(Any, t)._writer is None
            assert t.v5_counters is t.v5.counters  # counters survive


# -- cancellation, settings, API surface ------------------------------------------------


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_cancellation_under_load(kind: Kind) -> None:
    async with running(kind, timeout=5) as (server, t):
        server.default = Behavior(silent=True)
        sched = BusScheduler(gap_s=0)
        tasks = [asyncio.create_task(sched.run(lambda: t.read_holding(1, 0, 1))) for _ in range(6)]
        await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(r, asyncio.CancelledError) for r in results)
        assert sched.pending == 0
        assert cast(Any, t)._writer is None
        server.default = Behavior()
        assert await t.read_holding(1, 3, 1) == expected(
            3, 1
        )  # RTU: resync probe first, no cooldown


def test_single_write_site_in_transport_package() -> None:
    root = pathlib.Path(__file__).parents[1] / "src" / "deye_monitor"
    hits = [
        (p.name, line.strip())
        for p in root.rglob("*.py")
        # local state files only, never a device (the audit log of the write layer is a file)
        if p.name not in ("config.py", "store.py", "writes.py")
        for line in p.read_text().splitlines()
        if ".write(" in line or ".send(" in line or "sendall" in line or "write_eof" in line
    ]
    # the read sink and the (guarded, single-register) write sink of the base transport
    assert hits == [("base.py", "writer.write(guarded.frame)")] * 2


def test_no_public_send_api() -> None:
    for cls in (ModbusTcpTransport, RtuOverTcpTransport, SerialRtuTransport, SolarmanV5Transport):
        public = {n for n in dir(cls) if not n.startswith("_")}
        # the one guarded single-register write; no generic send/raw/execute
        assert {n for n in public if n.startswith(("write", "send", "execute", "raw"))} == {
            "write_register"
        }


def test_settings_validation() -> None:
    for kw in (
        {"timeout_s": 0},
        {"timeout_s": float("nan")},
        {"timeout_s": float("inf")},
        {"backoff_min_s": -1},
        {"backoff_max_s": 1.0},
        {"max_read_registers": 0},
        {"max_read_registers": 126},
        {"resync_quiet_ms": 0},
        {"resync_quiet_ms": float("nan")},
        {"resync_budget_s": -1},
        {"resync_budget_s": float("inf")},
        {"rtu_late_window_s": 0},
        {"rtu_late_window_s": -1.0},
        {"rtu_late_window_s": float("nan")},
        {"rtu_late_window_s": float("inf")},
        {"rtu_late_window_s": 3.0, "resync_budget_s": 3.0},  # budget < window + quiet + timeout
        {"resync_max_bytes": 0},
    ):
        with pytest.raises(ValueError):
            TransportSettings(**kw)
    defaults = TransportSettings()
    assert defaults.resync_quiet_ms == 200.0
    assert defaults.late_window_s == 10.0  # max(1 s, 2 x timeout 5 s)
    assert TransportSettings(timeout_s=0.2).late_window_s == 1.0
    assert TransportSettings(timeout_s=0.2, rtu_late_window_s=0.3).late_window_s == 0.3
    # the derived budget always covers window + quiet + probe deadline
    for timeout in (0.1, 1.0, 5.0, 30.0):
        st = TransportSettings(timeout_s=timeout)
        assert st.budget_s >= st.late_window_s + 0.2 + timeout


async def test_guard_violation_is_not_swallowed() -> None:
    from deye_monitor.protocol.guard import GuardViolation

    async with running("rtu") as (server, t):
        with pytest.raises((GuardViolation, ValueError)):
            await t.read_holding(1, 0, 126)
        with pytest.raises((GuardViolation, ValueError)):
            await t.read_holding(0, 0, 1)
        assert not server.raw
        assert t.counters.timeouts == 0 and cast(Any, t)._uncertain is False


async def test_serial_hangup_and_lazy_reopen() -> None:
    clock = FakeClock()
    pty = FakePty()
    pty.start()
    t = SerialRtuTransport(
        connector=serial_rtu(pty.path).connector,
        framing=serial_rtu(pty.path).framing,
        settings=settings(0.2),
        clock=clock,
        rand=lambda: 0.0,
    )
    try:
        assert await t.read_holding(1, 1, 1) == expected(1, 1)
        await t.close()
        assert await t.read_holding(1, 2, 1) == expected(2, 1)
        pty.hang_up()
        with pytest.raises(Exception, match=r".*"):
            await t.read_holding(1, 3, 1)
    finally:
        await t.close()
        pty.close()


async def test_rtu_probe_is_never_shortened_at_the_minimum_limit() -> None:
    """VF-03: with max_read_registers=6 a failed count-5 request is followed by the full count-6
    probe (R3..R8) and a complete five-word check, not by a clipped one-word read."""
    clock = FakeClock()
    async with running("rtu", timeout=0.1, clock=clock, max_read_registers=6) as (server, t):
        server.script = [Behavior(delay_s=0.15)]
        with pytest.raises(RequestTimeout):
            await t.read_holding(1, 3, 5)
        clock.pass_cooldown()
        assert await t.read_holding(1, 100, 2) == expected(100, 2)
        starts = [
            (int.from_bytes(f[2:4], "big"), int.from_bytes(f[4:6], "big")) for f in server.frames
        ]
        assert starts == [(3, 5), (3, 6), (100, 2)]
        assert t.counters.resyncs == 1


async def test_serial_close_never_waits_for_a_flush_the_peer_does_not_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2E finding: closing the port must not start a flush (tcdrain) in a worker thread; with a
    peer that does not read it never returns and blocks the shutdown (executor join)."""
    import threading

    import serial.serialposix  # type: ignore[import-untyped]

    release = threading.Event()
    flushed = threading.Event()

    def blocking_flush(self: object) -> None:
        flushed.set()
        release.wait(30)

    monkeypatch.setattr(serial.serialposix.Serial, "flush", blocking_flush)
    pty = FakePty()
    pty.start()
    t = serial_rtu(pty.path, 9600, settings(0.5))
    try:
        assert await t.read_holding(1, 586, 7) == expected(586, 7)
        pty.default = Behavior(silent=True)  # from now on the peer neither answers nor reads on
        await asyncio.wait_for(t.close(), 5)
        await asyncio.sleep(0.3)
        assert not flushed.is_set(), "close started a flush that can block forever"
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.shutdown_default_executor(), 5)  # nothing left to wait for
    finally:
        release.set()
        pty.close()
