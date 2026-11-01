"""Write layer v1 on the real transports against the fake devices: exact bytes on the wire, echo,
exception, and the failure semantics (exactly one FC16, never a retry or a fallback)."""

from __future__ import annotations

import pytest

from deye_monitor.protocol import modbus as m
from deye_monitor.protocol.guard import WriteGrant
from deye_monitor.protocol.modbus import ModbusExceptionResponse
from deye_monitor.transport import WriteIndeterminate, WriteNotSent
from deye_monitor.transport.base import StreamTransport
from tests.fakes.servers import Behavior, FakePty, Kind
from tests.transport_helpers import TCP_KINDS, build, build_serial, running

GRANT = WriteGrant(unit=1, register=128, mask=0xFFFF, old_word=0x1234, new_word=50)


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_one_register_write_is_acknowledged(kind: Kind) -> None:
    async with running(kind) as (server, t):
        assert isinstance(t, StreamTransport)
        ack = await t.write_register(GRANT)
        assert ack == m.WriteAck(1, 128, 1)
        assert server.responder.regs[128] == 50
        frames = list(server.frames)
        assert len(frames) == 1
        frame = frames[0]
        rtu = m.build_rtu_write(1, 128, 50)
        if kind == "rtu":
            assert frame == rtu
        elif kind == "mbap":
            assert len(frame) == 15
            assert frame[2:7] == bytes.fromhex("0000000901")  # protocol id, length 9, unit
            assert frame[7:] == bytes.fromhex("1000800001020032")
        else:
            assert len(frame) == 39
            assert frame[26:-2] == rtu
        assert len(server.responder.writes) == 1
        assert server.responder.other_fc == []


async def test_one_register_write_on_a_serial_line() -> None:
    pty = FakePty()
    pty.start()
    try:
        t = build_serial(pty.path)
        ack = await t.write_register(GRANT)
        assert ack == m.WriteAck(1, 128, 1)
        assert pty.responder.regs[128] == 50
        assert [len(f) for f in pty.responder.frames] == [11]
        await t.close()
    finally:
        pty.close()


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_device_exception_is_a_validated_rejection(kind: Kind) -> None:
    async with running(kind) as (server, t):
        assert isinstance(t, StreamTransport)
        server.write_script = [Behavior(exception_code=3)]
        with pytest.raises(ModbusExceptionResponse) as info:
            await t.write_register(GRANT)
        assert info.value.code == 3
        assert 128 not in server.responder.regs
        assert len(server.responder.writes) == 1


@pytest.mark.parametrize("kind", TCP_KINDS)
@pytest.mark.parametrize(
    "behavior",
    [
        Behavior(silent=True),  # lost answer, word applied
        Behavior(silent=True, write_applied=False),  # lost answer, word not applied
        Behavior(close=True),  # connection closed instead of an answer
        Behavior(raw_reply=b"\x01\x02\x03"),  # garbage instead of an acknowledgement
        Behavior(split_at=3, drop_rest=True),  # partial answer
    ],
)
async def test_missing_or_malformed_ack_is_indeterminate_and_never_retried(
    kind: Kind, behavior: Behavior
) -> None:
    async with running(kind) as (server, t):
        assert isinstance(t, StreamTransport)
        server.write_script = [behavior]
        with pytest.raises(WriteIndeterminate):
            await t.write_register(GRANT)
        assert len(server.frames) == 1  # exactly one FC16 frame, no retry, no fallback
        assert server.responder.other_fc == []


@pytest.mark.parametrize("kind", TCP_KINDS)
async def test_a_guard_rejection_sends_nothing(kind: Kind) -> None:
    async with running(kind) as (server, t):
        assert isinstance(t, StreamTransport)
        denied = WriteGrant(unit=1, register=80, mask=0xFFFF, old_word=1, new_word=2)
        with pytest.raises(WriteNotSent):
            await t.write_register(denied)
        assert bytes(server.raw) == b""
        wide = WriteGrant(unit=1, register=130, mask=0xFFFF, old_word=1, new_word=2)
        with pytest.raises(WriteNotSent):
            await t.write_register(wide)
        assert bytes(server.raw) == b""


async def test_connect_failure_and_cooldown_are_not_sent() -> None:
    t = build("mbap", 1)  # nothing listens on port 1
    assert isinstance(t, StreamTransport)
    with pytest.raises(WriteNotSent):
        await t.write_register(GRANT)
    with pytest.raises(WriteNotSent):  # the endpoint cooldown applies to writes as to reads
        await t.write_register(GRANT)


async def test_reads_still_work_after_a_write_on_one_connection() -> None:
    async with running("mbap") as (server, t):
        assert isinstance(t, StreamTransport)
        await t.write_register(GRANT)
        assert (await t.read_holding(1, 128, 1)) == [50]
        assert server.connections == 1


async def test_v5_write_on_the_logger_uses_inner_unit_one() -> None:
    async with running("v5") as (server, t):
        assert isinstance(t, StreamTransport)
        slave = WriteGrant(unit=2, register=128, mask=0xFFFF, old_word=1, new_word=50)
        with pytest.raises(WriteNotSent):
            await t.write_register(slave)
        assert bytes(server.raw) == b""
