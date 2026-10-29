import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deye_monitor.protocol import modbus as m


def test_crc_goldens() -> None:
    assert m.crc16(b"123456789") == 0x4B37
    assert m.build_rtu_request(1, 0, 10) == bytes.fromhex("01030000000AC5CD")
    assert m.build_rtu_request(1, 0, 1) == bytes.fromhex("010300000001840A")
    assert m.append_crc(bytes.fromhex("01030200 00")) == bytes.fromhex("0103020000B844")


def test_mbap_request_layout() -> None:
    assert m.build_mbap_request(0x1234, 1, 586, 7) == bytes.fromhex("123400000006010302 4A0007")


@pytest.mark.parametrize(
    ("start", "count"), [(-1, 1), (0, 0), (0, 126), (65536, 1), (65535, 2), (65411, 126)]
)
def test_builders_reject_bad_range(start: int, count: int) -> None:
    with pytest.raises(ValueError):
        m.build_rtu_request(1, start, count)
    with pytest.raises(ValueError):
        m.build_mbap_request(1, 1, start, count)


@pytest.mark.parametrize("unit", [0, 248, -1])
def test_builders_reject_bad_unit(unit: int) -> None:
    with pytest.raises(ValueError):
        m.build_rtu_request(unit, 0, 1)


def _rtu_resp(unit: int, regs: list[int]) -> bytes:
    body = bytes([unit, 3, 2 * len(regs)]) + b"".join(r.to_bytes(2, "big") for r in regs)
    return m.append_crc(body)


def _mbap_resp(tid: int, unit: int, regs: list[int]) -> bytes:
    pdu = bytes([3, 2 * len(regs)]) + b"".join(r.to_bytes(2, "big") for r in regs)
    return (
        tid.to_bytes(2, "big")
        + b"\x00\x00"
        + (len(pdu) + 1).to_bytes(2, "big")
        + bytes([unit])
        + pdu
    )


def test_rtu_roundtrip_and_lengths() -> None:
    frame = _rtu_resp(1, [1, 0xFFFF])
    assert len(frame) == m.rtu_response_length(2)
    assert m.parse_rtu_response(frame, 1, 2) == [1, 0xFFFF]


def test_mbap_roundtrip_and_lengths() -> None:
    frame = _mbap_resp(7, 1, [5, 6, 7])
    assert len(frame) == m.mbap_response_length(3)
    assert m.parse_mbap_response(frame, 7, 1, 3) == [5, 6, 7]


def test_rtu_exception() -> None:
    frame = m.append_crc(bytes([1, 0x83, 2]))
    with pytest.raises(m.ModbusExceptionResponse) as exc:
        m.parse_rtu_response(frame, 1, 1)
    assert exc.value.code == 2


def test_mbap_exception() -> None:
    frame = bytes.fromhex("000700000003018302")
    with pytest.raises(m.ModbusExceptionResponse) as exc:
        m.parse_mbap_response(frame, 7, 1, 1)
    assert exc.value.code == 2


def test_rtu_errors() -> None:
    good = _rtu_resp(1, [1, 2])
    bad_crc = good[:-1] + bytes([good[-1] ^ 1])
    wrong_fc = m.append_crc(bytes([1, 4, 4, 0, 1, 0, 2]))
    bad_bytecount = m.append_crc(bytes([1, 3, 2, 0, 1, 0, 2]))
    for frame, unit, count in [
        (good[:-1], 1, 2),
        (b"", 1, 2),
        (bad_crc, 1, 2),
        (good, 2, 2),
        (wrong_fc, 1, 2),
        (bad_bytecount, 1, 2),
        (good, 1, 3),
        (good + b"\x00", 1, 2),
        (m.append_crc(bytes([1, 0x83, 2, 0])), 1, 1),
    ]:
        with pytest.raises(m.ModbusFrameError):
            m.parse_rtu_response(frame, unit, count)


def test_mbap_errors() -> None:
    good = _mbap_resp(7, 1, [1, 2])
    bad_bytecount = good[:8] + bytes([2]) + good[9:]
    for frame, tid, unit, count in [
        (good[:-1], 7, 1, 2),
        (good, 8, 1, 2),
        (good[:2] + b"\x00\x01" + good[4:], 7, 1, 2),
        (good + b"\x00", 7, 1, 2),
        (good, 7, 2, 2),
        (good[:7] + b"\x04" + good[8:], 7, 1, 2),
        (bad_bytecount, 7, 1, 2),
        (good, 7, 1, 3),
    ]:
        with pytest.raises(m.ModbusFrameError):
            m.parse_mbap_response(frame, tid, unit, count)


@given(
    unit=st.integers(1, 247),
    regs=st.lists(st.integers(0, 0xFFFF), min_size=1, max_size=125),
)
def test_property_response_roundtrip(unit: int, regs: list[int]) -> None:
    assert m.parse_rtu_response(_rtu_resp(unit, regs), unit, len(regs)) == regs
    assert m.parse_mbap_response(_mbap_resp(9, unit, regs), 9, unit, len(regs)) == regs


@given(frame=st.binary(max_size=300))
def test_property_parsers_only_raise_modbus_errors(frame: bytes) -> None:
    with contextlib.suppress(m.ModbusError):
        m.parse_rtu_response(frame, 1, 2)
    with contextlib.suppress(m.ModbusError):
        m.parse_mbap_response(frame, 1, 1, 2)
