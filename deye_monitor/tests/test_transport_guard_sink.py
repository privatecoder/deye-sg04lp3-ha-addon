"""The guard runs at the sink (CR1-05): a rejected frame never reaches any writer."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from deye_monitor.protocol import modbus as m
from deye_monitor.protocol import solarman_v5 as v5
from deye_monitor.protocol.guard import Framing, GuardedFrame, GuardViolation, guard_frame
from deye_monitor.transport.base import StreamTransport
from tests.fakes.servers import SERIAL, FakePty, FakeServer, Kind
from tests.transport_helpers import FRAMING, build, build_serial, expected

SINKS = ["mbap", "rtu", "v5", "serial"]


class SpyWriter:
    """Records every byte string handed to the real writer."""

    def __init__(self, inner: asyncio.StreamWriter, log: list[bytes]) -> None:
        self.inner = inner
        self.log = log

    def write(self, data: bytes) -> None:
        self.log.append(data)
        self.inner.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class Sink:
    def __init__(self, name: str, t: StreamTransport, log: list[bytes]) -> None:
        self.name, self.t, self.log = name, t, log

    @property
    def framing(self) -> Framing:
        return Framing.RTU if self.name == "serial" else FRAMING[cast(Kind, self.name)]

    def good(self, unit: int = 1, start: int = 5, count: int = 2) -> bytes:
        if self.name in ("rtu", "serial"):
            return m.build_rtu_request(unit, start, count)
        if self.name == "mbap":
            return m.build_mbap_request(7, unit, start, count)
        return v5.build_v5_request(SERIAL, 3, m.build_rtu_request(unit, start, count))

    def bad_frames(self) -> dict[str, Any]:
        write06 = m.append_crc(bytes([1, 6, 0, 1, 0, 1]))
        good = self.good()
        out: dict[str, Any] = {
            "appended ADU": good + self.good(),
            "appended byte": good + b"\x00",
            "truncated": good[:-1],
            "wrong unit": self.good(unit=2),
            "bytearray": bytearray(good),
            "marker object": GuardedFrame(good, m.ReadRequest(1, 5, 2)),
            "empty": b"",
        }
        if self.name in ("rtu", "serial"):
            out["FC06"] = write06
            out["FC16"] = m.append_crc(bytes([1, 0x10, 0, 1, 0, 1, 2, 0, 1]))
            out["broadcast"] = m.build_rtu_request(1, 0, 1)[:0] + m.append_crc(
                bytes([0, 3, 0, 0, 0, 1])
            )
        elif self.name == "mbap":
            out["FC06"] = (7).to_bytes(2, "big") + b"\x00\x00\x00\x06\x01\x06\x00\x01\x00\x01"
        else:
            out["FC06"] = v5.build_v5_request(SERIAL, 3, write06)
            out["foreign logger serial"] = v5.build_v5_request(
                SERIAL + 1, 3, m.build_rtu_request(1, 5, 2)
            )
            out["management control"] = good[:3] + b"\x10\x41" + good[5:]
        return out


async def make(name: str) -> tuple[Sink, Any]:
    log: list[bytes] = []
    t: StreamTransport
    if name == "serial":
        pty = FakePty()
        pty.start()
        t = build_serial(pty.path)
        link: Any = pty
    else:
        server = await FakeServer(cast(Kind, name)).start()
        t = build(cast(Kind, name), server.port)
        link = server
    real = t.connector

    async def spying() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await real()
        return reader, cast(asyncio.StreamWriter, SpyWriter(writer, log))

    t.connector = spying
    return Sink(name, t, log), link


async def stop(sink: Sink, link: Any) -> None:
    await sink.t.close()
    if isinstance(link, FakePty):
        link.close()
    else:
        await link.stop()


@pytest.fixture(params=SINKS)
async def sink(request: pytest.FixtureRequest) -> AsyncIterator[Sink]:
    s, link = await make(request.param)
    try:
        yield s
    finally:
        await stop(s, link)


async def test_rejected_frames_never_reach_the_writer(sink: Sink) -> None:
    t = cast(Any, sink.t)
    await t._ensure_connected()
    for label, frame in sink.bad_frames().items():
        with pytest.raises((GuardViolation, TypeError)):
            await t._write(frame, 1)
        assert sink.log == [], label
    # the sink still works and sends exactly the guard's bytes
    good = sink.good()
    await t._write(good, 1)
    assert sink.log == [good]
    assert (
        sink.log[0]
        == guard_frame(
            good,
            sink.framing,
            unit_id=1,
            max_read_registers=60,
            logger_serial=SERIAL if sink.name == "v5" else None,
        ).frame
    )


async def test_guard_still_applies_after_reconnect_and_cancellation(sink: Sink) -> None:
    t = cast(Any, sink.t)
    await t._ensure_connected()
    t._drop()  # reconnect / UART reopen
    await t._ensure_connected()
    for frame in sink.bad_frames().values():
        with pytest.raises((GuardViolation, TypeError)):
            await t._write(frame, 1)
    task = asyncio.create_task(t.read_holding(1, 5, 2))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await t._ensure_connected()
    before = len(sink.log)
    for frame in sink.bad_frames().values():
        with pytest.raises((GuardViolation, TypeError)):
            await t._write(frame, 1)
    assert len(sink.log) == before


async def test_guard_rejection_uses_the_production_guard_not_a_test_copy(sink: Sink) -> None:
    """Unit and limits come from the transport configuration, not from the frame."""
    t = cast(Any, sink.t)
    await t._ensure_connected()
    with pytest.raises(GuardViolation):
        await t._write(sink.good(count=61), 1)  # > max_read_registers (60)
    with pytest.raises(GuardViolation):
        await t._write(sink.good(unit=1), 2)  # target unit differs from the frame's unit
    assert sink.log == []


async def test_normal_reads_pass_through_the_sink(sink: Sink) -> None:
    assert await sink.t.read_holding(1, 5, 2) == expected(5, 2)
    assert len(sink.log) == 1
    assert sink.log[0] == sink.good(start=5, count=2) or sink.name in ("mbap", "v5")


def test_write_signature_takes_bytes_and_unit_only() -> None:
    params = list(inspect.signature(StreamTransport._write).parameters)
    assert params == ["self", "frame", "unit"]
    assert not hasattr(StreamTransport, "_guard")
