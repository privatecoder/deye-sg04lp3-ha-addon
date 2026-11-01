"""Write layer v1, byte level: exact FC16 frames (RTU, MBAP, V5), echo and exception parsers and the
write guard. No device, no socket."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deye_monitor.protocol import modbus as m
from deye_monitor.protocol import solarman_v5 as v5
from deye_monitor.protocol.guard import (
    DENIED_REGISTERS,
    WRITABLE_MASKS,
    Framing,
    GuardViolation,
    WriteGrant,
    guard_frame,
    guard_write_frame,
)
from deye_monitor.write_ops import DESCRIPTORS

LOGGER = 2_700_000_001
RTU_128_50 = bytes.fromhex("0110008000010200323845")
MBAP_128_50 = bytes.fromhex("010200000009011000800001020032")
V5_128_50 = bytes.fromhex(
    "a51a001045050001bbeea002000000000000000000000000000001100080000102003238450315"
)
GRANT = WriteGrant(unit=1, register=128, mask=0xFFFF, old_word=20, new_word=50)


def grant(**kw: int) -> WriteGrant:
    base = {"unit": 1, "register": 128, "mask": 0xFFFF, "old_word": 20, "new_word": 50}
    return WriteGrant(**{**base, **kw})


def rtu_write(unit: int, reg: int, value: int, *, count: int = 1, fc: int = 0x10) -> bytes:
    body = (
        bytes([unit, fc])
        + reg.to_bytes(2, "big")
        + count.to_bytes(2, "big")
        + bytes([2 * count])
        + value.to_bytes(2, "big") * count
    )
    return m.append_crc(body)


# -- exact frames -------------------------------------------------------------------------------


def test_exact_single_register_frames() -> None:
    assert m.build_rtu_write(1, 128, 50) == RTU_128_50
    assert len(RTU_128_50) == m.RTU_WRITE_REQUEST_LEN == 11
    assert m.build_mbap_write(0x0102, 1, 128, 50) == MBAP_128_50
    assert len(MBAP_128_50) == m.MBAP_WRITE_REQUEST_LEN == 15
    assert int.from_bytes(MBAP_128_50[4:6], "big") == 9
    assert v5.build_v5_write_request(LOGGER, 5, RTU_128_50) == V5_128_50
    assert len(V5_128_50) == v5.WRITE_REQUEST_FRAME_LEN == 39
    assert m.build_fc16_pdu(128, 50) == bytes.fromhex("1000800001020032")


@pytest.mark.parametrize(("start", "value"), [(-1, 0), (0x10000, 0), (1, -1), (1, 0x10000)])
def test_builders_reject_out_of_range(start: int, value: int) -> None:
    with pytest.raises(ValueError):
        m.build_rtu_write(1, start, value)
    with pytest.raises(ValueError):
        m.build_mbap_write(1, 1, start, value)


def test_builders_reject_bool_and_bad_unit() -> None:
    with pytest.raises(ValueError):
        m.build_rtu_write(1, 128, True)
    with pytest.raises(ValueError):
        m.build_rtu_write(0, 128, 1)
    with pytest.raises(ValueError):
        v5.build_v5_write_request(LOGGER, 1, RTU_128_50[:-1])
    with pytest.raises(ValueError):
        v5.build_v5_write_request(LOGGER, 1, bytes(8))  # a read ADU is not a write ADU


# -- echo and exception -----------------------------------------------------------------------


def test_rtu_echo_and_exception() -> None:
    ack = bytes.fromhex("0110008000010021")
    assert m.parse_rtu_write_response(ack, 1, 128) == m.WriteAck(1, 128, 1)
    exc = m.append_crc(bytes([1, 0x90, 2]))
    with pytest.raises(m.ModbusExceptionResponse) as info:
        m.parse_rtu_write_response(exc, 1, 128)
    assert info.value.code == 2


@pytest.mark.parametrize(
    "frame",
    [
        m.append_crc(bytes([1, 0x10, 0, 129, 0, 1])),  # wrong start
        m.append_crc(bytes([1, 0x10, 0, 128, 0, 2])),  # wrong count
        m.append_crc(bytes([2, 0x10, 0, 128, 0, 1])),  # wrong unit
        m.append_crc(bytes([1, 0x06, 0, 128, 0, 1])),  # FC06 echo is not accepted
        m.append_crc(bytes([1, 0x03, 2, 0, 1])),  # a read response
        bytes.fromhex("0110008000010022"),  # bad crc
        bytes.fromhex("0110008000010021") + b"\x00",  # concatenated/padded
        bytes.fromhex("01100080000100"),  # truncated
        m.append_crc(bytes([1, 0x90, 2, 0])),  # exception with padding
    ],
)
def test_rtu_malformed_acknowledgements(frame: bytes) -> None:
    with pytest.raises(m.ModbusFrameError):
        m.parse_rtu_write_response(frame, 1, 128)


def test_mbap_echo_and_exception_and_malformed() -> None:
    ack = bytes.fromhex("010200000006011000800001")
    assert m.parse_mbap_write_response(ack, 0x0102, 1, 128) == m.WriteAck(1, 128, 1)
    exc = bytes.fromhex("0102000000030190") + b"\x02"
    with pytest.raises(m.ModbusExceptionResponse):
        m.parse_mbap_write_response(exc, 0x0102, 1, 128)
    for bad in (
        bytes.fromhex("010300000006011000800001"),  # transaction id
        bytes.fromhex("010200010006011000800001"),  # protocol id
        bytes.fromhex("010200000007011000800001"),  # length field
        bytes.fromhex("010200000006021000800001"),  # unit
        bytes.fromhex("010200000006011000810001"),  # start
        bytes.fromhex("010200000006011000800002"),  # count
        bytes.fromhex("010200000006011000800001") + b"\x00",
    ):
        with pytest.raises(m.ModbusFrameError):
            m.parse_mbap_write_response(bad, 0x0102, 1, 128)


def test_v5_write_response_is_strict() -> None:
    ack = bytes.fromhex("0110008000010021")
    good = v5.V5Response(1, 0x02, 0x01, ack)
    assert v5.decode_write_response(good, 1, 128) == m.WriteAck(1, 128, 1)
    with pytest.raises(v5.V5LoggerError):
        v5.decode_write_response(v5.V5Response(1, 0x02, 0x02, b""), 1, 128)
    with pytest.raises(v5.V5LoggerError):
        v5.decode_write_response(v5.V5Response(1, 0x02, 0x01, b"\x01"), 1, 128)
    with pytest.raises(m.ModbusFrameError):  # the double-CRC read quirk is not applied to writes
        v5.decode_write_response(v5.V5Response(1, 0x02, 0x01, ack + b"\x00\x00"), 1, 128)


# -- the write guard ----------------------------------------------------------------------------


def test_guard_accepts_exactly_the_granted_frame() -> None:
    assert guard_write_frame(RTU_128_50, Framing.RTU, grant=GRANT).frame == RTU_128_50
    assert guard_write_frame(MBAP_128_50, Framing.MBAP, grant=GRANT).frame == MBAP_128_50
    assert (
        guard_write_frame(V5_128_50, Framing.SOLARMAN_V5, grant=GRANT, logger_serial=LOGGER).frame
        == V5_128_50
    )


def test_guard_rejects_fc06_and_multi_register_writes() -> None:
    fc06 = m.append_crc(bytes([1, 0x06]) + (128).to_bytes(2, "big") + (50).to_bytes(2, "big"))
    with pytest.raises(GuardViolation):
        guard_write_frame(fc06, Framing.RTU, grant=GRANT)
    two = rtu_write(1, 128, 50, count=2)
    with pytest.raises(GuardViolation):
        guard_write_frame(two, Framing.RTU, grant=GRANT)
    bad_bytecount = bytearray(RTU_128_50[:-2])
    bad_bytecount[6] = 4
    with pytest.raises(GuardViolation):
        guard_write_frame(m.append_crc(bytes(bad_bytecount)), Framing.RTU, grant=GRANT)
    mb_two = bytearray(MBAP_128_50)
    mb_two[11] = 2
    with pytest.raises(GuardViolation):
        guard_write_frame(bytes(mb_two), Framing.MBAP, grant=GRANT)


def test_guard_rejects_wrong_unit_register_word_and_serial() -> None:
    with pytest.raises(GuardViolation):
        guard_write_frame(RTU_128_50, Framing.RTU, grant=grant(unit=2))
    with pytest.raises(GuardViolation):  # register differs from the grant
        guard_write_frame(rtu_write(1, 125, 50), Framing.RTU, grant=GRANT)
    with pytest.raises(GuardViolation):  # word differs from the grant
        guard_write_frame(rtu_write(1, 128, 51), Framing.RTU, grant=GRANT)
    with pytest.raises(GuardViolation):
        guard_write_frame(V5_128_50, Framing.SOLARMAN_V5, grant=GRANT, logger_serial=LOGGER + 1)
    with pytest.raises(GuardViolation):
        guard_write_frame(V5_128_50, Framing.SOLARMAN_V5, grant=GRANT)  # no logger serial
    on_slave_unit = v5.build_v5_write_request(LOGGER, 5, m.build_rtu_write(2, 128, 50))
    with pytest.raises(GuardViolation):  # the inner unit of a logger is always 1
        guard_write_frame(
            on_slave_unit, Framing.SOLARMAN_V5, grant=grant(unit=2), logger_serial=LOGGER
        )


def test_guard_rejects_padding_concatenation_truncation_and_bad_crc() -> None:
    for frame in (
        RTU_128_50 + b"\x00",
        RTU_128_50 + RTU_128_50,
        RTU_128_50[:-1],
        RTU_128_50[:-1] + bytes([RTU_128_50[-1] ^ 1]),
        b"\x00" + RTU_128_50,
    ):
        with pytest.raises(GuardViolation):
            guard_write_frame(frame, Framing.RTU, grant=GRANT)
    for frame in (
        MBAP_128_50 + b"\x00",
        MBAP_128_50[:-1],
        MBAP_128_50[:2] + b"\x00\x01" + MBAP_128_50[4:],
    ):
        with pytest.raises(GuardViolation):
            guard_write_frame(frame, Framing.MBAP, grant=GRANT)
    broken = bytearray(V5_128_50)
    broken[-2] ^= 1
    for frame in (bytes(broken), V5_128_50[:-1], V5_128_50 + b"\x00"):
        with pytest.raises(GuardViolation):
            guard_write_frame(frame, Framing.SOLARMAN_V5, grant=GRANT, logger_serial=LOGGER)
    with pytest.raises(GuardViolation):  # a mutable buffer is not accepted
        guard_write_frame(
            bytearray(V5_128_50),  # type: ignore[arg-type]
            Framing.SOLARMAN_V5,
            grant=GRANT,
            logger_serial=LOGGER,
        )
    meta = bytearray(V5_128_50)
    meta[11] = 0x03  # frame type of the metadata is a fixed constant
    meta[-2] = sum(meta[1:-2]) & 0xFF
    with pytest.raises(GuardViolation):
        guard_write_frame(bytes(meta), Framing.SOLARMAN_V5, grant=GRANT, logger_serial=LOGGER)


@pytest.mark.parametrize("register", [0, 22, 23, 24, 80, 81, 91, 92, 98, 111, 115, 116, 117])
@pytest.mark.parametrize("extra", [0, 1])
def test_guard_rejects_unlisted_and_denied_registers(register: int, extra: int) -> None:
    register += 0  # parametrised for readability
    frame = rtu_write(1, register, 7 + extra)
    with pytest.raises(GuardViolation):
        guard_write_frame(frame, Framing.RTU, grant=grant(register=register, new_word=7 + extra))


def test_guard_rejects_every_deny_listed_and_every_unlisted_register() -> None:
    unlisted = [0, 1, 107, 110, 126, 127, 129, 131, 138, 2000, 65535]
    for register in [*sorted(DENIED_REGISTERS), *unlisted]:
        assert register not in WRITABLE_MASKS
        with pytest.raises(GuardViolation):
            guard_write_frame(
                rtu_write(1, register, 7), Framing.RTU, grant=grant(register=register, new_word=7)
            )
    assert not set(WRITABLE_MASKS) & DENIED_REGISTERS


def test_guard_rejects_foreign_bits_and_unchanged_words_and_wrong_masks() -> None:
    # R130: only bit 0 may change
    assert guard_write_frame(
        rtu_write(1, 130, 0b101),
        Framing.RTU,
        grant=grant(register=130, mask=1, old_word=0b100, new_word=0b101),
    )
    with pytest.raises(GuardViolation):
        guard_write_frame(
            rtu_write(1, 130, 0b111),
            Framing.RTU,
            grant=grant(register=130, mask=1, old_word=0b100, new_word=0b111),
        )
    with pytest.raises(GuardViolation):  # a wider mask than the register's is not a grant
        guard_write_frame(
            rtu_write(1, 130, 0b111),
            Framing.RTU,
            grant=grant(register=130, mask=0xFFFF, old_word=0b100, new_word=0b111),
        )
    with pytest.raises(GuardViolation):  # unchanged word: no write
        guard_write_frame(rtu_write(1, 128, 20), Framing.RTU, grant=grant(old_word=20, new_word=20))
    with pytest.raises(GuardViolation):
        guard_write_frame(RTU_128_50, Framing.RTU, grant=grant(new_word=0x10000))


def test_read_guard_still_refuses_every_write_frame() -> None:
    for frame, framing in (
        (RTU_128_50, Framing.RTU),
        (MBAP_128_50, Framing.MBAP),
        (rtu_write(1, 128, 50)[:8], Framing.RTU),
    ):
        with pytest.raises(GuardViolation):
            guard_frame(frame, framing, unit_id=1, max_read_registers=60)
    with pytest.raises(GuardViolation):
        guard_frame(
            V5_128_50, Framing.SOLARMAN_V5, unit_id=1, max_read_registers=60, logger_serial=LOGGER
        )


def test_descriptor_table_matches_the_guard_table() -> None:
    assert {d.register: d.mask for d in DESCRIPTORS} == WRITABLE_MASKS
    assert len(DESCRIPTORS) == 9


@given(st.binary(max_size=60))
def test_property_random_bytes_never_pass_the_write_guard(frame: bytes) -> None:
    for framing in (Framing.RTU, Framing.MBAP):
        with pytest.raises(GuardViolation):
            guard_write_frame(frame, framing, grant=GRANT)
    with pytest.raises(GuardViolation):
        guard_write_frame(frame, Framing.SOLARMAN_V5, grant=GRANT, logger_serial=LOGGER)


@given(
    st.integers(0, 65535), st.integers(0, 0xFFFF), st.integers(0, 0xFFFF), st.integers(0, 0xFFFF)
)
def test_property_only_whitelisted_register_and_mask_pass(
    register: int, mask: int, old: int, new: int
) -> None:
    try:
        grant_ = WriteGrant(1, register, mask, old, new)
        guard_write_frame(rtu_write(1, register, new), Framing.RTU, grant=grant_)
    except GuardViolation:
        return
    assert WRITABLE_MASKS[register] == mask
    assert (new ^ old) & ~mask & 0xFFFF == 0
    assert new != old
