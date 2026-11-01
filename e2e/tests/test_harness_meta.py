"""Checks of the harness itself (no Docker, no Home Assistant): the acceptance logic must reject
what it claims to reject."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from harness import docker
from harness.fakes import FakeInverter, with_crc


def mbap_read(tid: int, start: int, count: int) -> bytes:
    return (
        tid.to_bytes(2, "big")
        + b"\x00\x00\x00\x06\x01\x03"
        + start.to_bytes(2, "big")
        + count.to_bytes(2, "big")
    )


async def test_the_wire_acceptance_rejects_an_unparsed_tail():
    fake = FakeInverter("x", "mbap")
    await fake.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", fake.port)
        for i in range(51):
            writer.write(mbap_read(i, 100, 3))
            await writer.drain()
            await reader.readexactly(6 + 3 + 6)
        writer.write(b"\x00\xff\x00")  # three bytes of an incomplete frame, after the last request
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()
    finally:
        await fake.stop()
    assert fake.wire.pending_bytes == 3
    with pytest.raises(AssertionError):
        fake.wire.assert_clean(name="x")


async def test_a_clean_stream_passes_and_a_write_frame_is_a_violation():
    fake = FakeInverter("y", "rtu")
    await fake.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", fake.port)
        writer.write(with_crc(bytes([1, 3, 0, 10, 0, 2])))
        await writer.drain()
        await reader.readexactly(9)
        writer.close()
    finally:
        await fake.stop()
    fake.wire.assert_clean(name="y")
    write_fc06 = FakeInverter("z", "rtu")
    await write_fc06.start()
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", write_fc06.port)
        writer.write(with_crc(bytes([1, 6, 0, 10, 0, 2])))  # FC06 with a valid CRC
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()
    finally:
        await write_fc06.stop()
    with pytest.raises(AssertionError):
        write_fc06.wire.assert_clean(name="z", expect_traffic=False)


async def test_stopping_a_serial_fake_closes_both_ends_of_the_pty():
    fake = FakeInverter("s", "serial")
    await fake.start()
    slave, master = fake._slave, fake._master
    assert slave is not None and master is not None
    await fake.stop()
    for fd in (slave, master):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_the_startup_cleanup_never_touches_a_live_run(tmp_path, monkeypatch):
    """A lock that is held means a live run: its resources are not removed. A released lock is an
    aborted run: exactly its labelled resources are removed."""
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, **kw):
        calls.append(args)
        listing = "id1\n" if args[:2] in (("ps", "-aq"), ("network", "ls")) else ""
        return subprocess.CompletedProcess(args, 0, listing, "")

    monkeypatch.setattr(docker, "run", fake_run)
    monkeypatch.setattr(docker, "LOCK_DIR", tmp_path)
    live = docker._flock(tmp_path / "live0001.lock")  # held by "another live run"
    assert live is not None
    dead = tmp_path / "dead0002.lock"
    dead.write_text("12345")  # nobody holds it
    cleaned = docker.remove_orphans()
    assert cleaned == ["dead0002"]
    touched = [a for a in calls if any("deye-e2e.run=live0001" in x for x in a)]
    assert touched == [], "a live run was touched"
    assert any("deye-e2e.run=dead0002" in " ".join(a) for a in calls)
    assert not dead.exists() and (tmp_path / "live0001.lock").exists()
    live.close()  # type: ignore[attr-defined]


async def test_exit_stack_cleanup_runs_after_a_failing_assertion():
    from contextlib import AsyncExitStack

    stopped: list[str] = []
    fake = FakeInverter("c", "serial")

    async def stop(name: str) -> None:
        stopped.append(name)
        await fake.stop()

    with pytest.raises(AssertionError):
        async with AsyncExitStack() as ex:
            await fake.start()
            ex.push_async_callback(stop, "fake")
            ex.callback(stopped.append, "app")
            raise AssertionError("a wire violation")
    assert stopped == ["app", "fake"] and fake._slave is None


async def test_the_plant_setup_fixture_rejects_an_unparsed_tail_when_it_stops(monkeypatch):
    """The module fixture of the plant setup case ends with the final wire acceptance: a fake that
    ends with bytes that belong to no frame makes it fail, and the app and the fake are cleaned up
    anyway."""
    from types import SimpleNamespace

    from harness.fakes import Wire
    from tests import test_10_plant_setup as target

    observed = {"app_cleaned": False, "fake_stopped": False}

    class Fake:
        def __init__(self, *args, **kwargs):
            self.wire = Wire("mbap")
            self.wire.raw.extend(b"\x00\xff\x00")
            self.wire.pending_bytes = 3
            self.wire.violations.append("mbap: truncated frame at close 00ff00")

        async def start(self):
            pass

        async def stop(self):
            observed["fake_stopped"] = True

    class App:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        async def wait_api(self, *args):
            pass

        def cleanup(self):
            observed["app_cleaned"] = True

    monkeypatch.setattr(target, "FakeInverter", Fake)
    monkeypatch.setattr(target, "ProcessApp", App)
    gen = target.instance_p.__wrapped__(SimpleNamespace(broker=SimpleNamespace(port=1883)))
    try:
        await anext(gen)
        with pytest.raises(AssertionError):
            await anext(gen)
    finally:
        await gen.aclose()
    assert observed == {"app_cleaned": True, "fake_stopped": True}


def fc16(unit: int, start: int, value: int, *, count: int = 1) -> bytes:
    body = bytes([unit, 0x10]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
    body += bytes([2 * count]) + value.to_bytes(2, "big") * count
    return with_crc(body)


async def test_a_single_register_write_is_stored_echoed_and_counted():
    fake = FakeInverter("w", "rtu", regs={128: 20})
    await fake.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", fake.port)
        writer.write(fc16(1, 128, 50))
        await writer.drain()
        echo = await reader.readexactly(8)
        assert echo == with_crc(bytes([1, 0x10, 0, 128, 0, 1]))
        writer.write(with_crc(bytes([1, 3, 0, 128, 0, 1])))  # the new word is what a read returns
        await writer.drain()
        assert (await reader.readexactly(7))[3:5] == (50).to_bytes(2, "big")
        writer.close()
    finally:
        await fake.stop()
    assert fake.regs[128] == 50
    assert [(w.start, w.value) for w in fake.wire.writes] == [(128, 50)]
    with pytest.raises(AssertionError):  # a write nobody expected is an acceptance failure
        fake.wire.assert_clean(name="w")
    fake.wire.assert_clean(name="w", allow_writes=True)


async def test_writes_over_mbap_and_a_rejecting_or_silent_device():
    mbap = FakeInverter("m", "mbap", regs={128: 20}, reject_writes=True)
    await mbap.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", mbap.port)
        frame = (7).to_bytes(2, "big") + b"\x00\x00\x00\x09\x01\x10\x00\x80\x00\x01\x02\x00\x32"
        writer.write(frame)
        await writer.drain()
        reply = await reader.readexactly(9)
        assert reply == (7).to_bytes(2, "big") + b"\x00\x00\x00\x03\x01\x90\x03"
        writer.close()
    finally:
        await mbap.stop()
    assert mbap.regs[128] == 20  # a rejected write stores nothing
    mbap.wire.assert_clean(name="m", allow_writes=True)
    silent = FakeInverter("s", "rtu", regs={128: 20}, silent_writes=True)
    await silent.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", silent.port)
        writer.write(fc16(1, 128, 50))
        await writer.drain()
        await asyncio.sleep(0.15)
        assert reader.at_eof() is False
        writer.close()
    finally:
        await silent.stop()
    assert silent.regs[128] == 50  # applied, but the acknowledgement was lost


@pytest.mark.parametrize(
    "frame",
    [
        fc16(1, 128, 50, count=2),  # more than one register
        with_crc(bytes([1, 0x10, 0, 128, 0, 1, 4, 0, 50, 0, 0])),  # byte count not 2
        fc16(0, 128, 50),  # broadcast unit
    ],
)
async def test_other_write_shapes_stay_violations(frame: bytes):
    fake = FakeInverter("v", "rtu", regs={128: 20})
    await fake.start()
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", fake.port)
        writer.write(frame)
        await writer.drain()
        await asyncio.sleep(0.1)
        writer.close()
    finally:
        await fake.stop()
    assert fake.regs[128] == 20
    with pytest.raises(AssertionError):
        fake.wire.assert_clean(name="v", expect_traffic=False, allow_writes=True)
