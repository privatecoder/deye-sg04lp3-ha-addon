import pytest
from hypothesis import given
from hypothesis import strategies as st

from deye_monitor.protocol import modbus as m
from deye_monitor.protocol.guard import Framing, GuardViolation, guard_frame

UNIT = 3
MAXR = 60


def rtu(unit: int, start: int, count: int, fc: int = 3) -> bytes:
    return m.append_crc(bytes([unit, fc]) + start.to_bytes(2, "big") + count.to_bytes(2, "big"))


def mbap(unit: int, start: int, count: int, fc: int = 3, tid: int = 1) -> bytes:
    pdu = bytes([fc]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")
    return (
        tid.to_bytes(2, "big")
        + b"\x00\x00"
        + (len(pdu) + 1).to_bytes(2, "big")
        + bytes([unit])
        + pdu
    )


def check(frame: bytes, framing: Framing, maxr: int = MAXR) -> m.ReadRequest:
    return guard_frame(frame, framing, unit_id=UNIT, max_read_registers=maxr).request


def both(unit: int, start: int, count: int) -> list[tuple[bytes, Framing]]:
    return [(rtu(unit, start, count), Framing.RTU), (mbap(unit, start, count), Framing.MBAP)]


@pytest.mark.parametrize(("start", "count"), [(0, 1), (586, 7), (65535, 1), (0, MAXR), (65476, 60)])
def test_positive_edges(start: int, count: int) -> None:
    for frame, framing in both(UNIT, start, count):
        assert check(frame, framing) == m.ReadRequest(UNIT, start, count)


def test_positive_count_125_with_max_125() -> None:
    for frame, framing in both(UNIT, 0, 125):
        assert check(frame, framing, 125).count == 125


def test_guarded_frame_is_the_checked_bytes() -> None:
    frame = rtu(UNIT, 10, 2)
    guarded = guard_frame(frame, Framing.RTU, unit_id=UNIT, max_read_registers=MAXR)
    assert guarded.frame is frame


@pytest.mark.parametrize(
    ("start", "count"), [(0, 0), (0, MAXR + 1), (0, 126), (65535, 2), (65500, 60), (0, 65535)]
)
def test_rejects_bad_count_and_overflow(start: int, count: int) -> None:
    for frame, framing in both(UNIT, start, count):
        with pytest.raises(GuardViolation):
            check(frame, framing)


@pytest.mark.parametrize("fc", [1, 2, 4, 5, 6, 0x0F, 0x10, 0x16, 0x17, 0x2B, 0x83, 0x90, 0, 0xFF])
def test_rejects_other_function_codes(fc: int) -> None:
    with pytest.raises(GuardViolation):
        check(rtu(UNIT, 0, 1, fc), Framing.RTU)
    with pytest.raises(GuardViolation):
        check(mbap(UNIT, 0, 1, fc), Framing.MBAP)


@pytest.mark.parametrize("unit", [0, 1, 4, 247, 255])
def test_rejects_broadcast_and_foreign_units(unit: int) -> None:
    for frame, framing in both(unit, 0, 1):
        with pytest.raises(GuardViolation):
            check(frame, framing)


def test_rejects_valid_read_plus_write_suffix() -> None:
    write06 = rtu(UNIT, 0, 1, 6)
    write10 = m.append_crc(bytes.fromhex("0310000000010200 01"))
    for suffix in (write06, write10, b"\x00", bytes(8)):
        with pytest.raises(GuardViolation):
            check(rtu(UNIT, 0, 1) + suffix, Framing.RTU)
        with pytest.raises(GuardViolation):
            check(mbap(UNIT, 0, 1) + suffix, Framing.MBAP)


def test_rejects_padding_prefix_and_two_frames() -> None:
    for frame, framing in both(UNIT, 0, 1):
        for bad in (b"\x00" + frame, frame + frame, frame[:-1], frame[:-2], b""):
            with pytest.raises(GuardViolation):
                check(bad, framing)


def test_rejects_bad_crc_and_forged_mbap() -> None:
    good = rtu(UNIT, 0, 1)
    with pytest.raises(GuardViolation):
        check(good[:-1] + bytes([good[-1] ^ 0xFF]), Framing.RTU)
    g = mbap(UNIT, 0, 1)
    forged = [
        g[:4] + b"\x00\x07" + g[6:],
        g[:4] + b"\x00\x05" + g[6:],
        g[:2] + b"\x00\x01" + g[4:],
        g[:2] + b"\x01\x00" + g[4:],
    ]
    for frame in forged:
        with pytest.raises(GuardViolation):
            check(frame, Framing.MBAP)


def test_rejects_mutable_buffers() -> None:
    for frame in (bytearray(rtu(UNIT, 0, 1)), memoryview(rtu(UNIT, 0, 1))):
        with pytest.raises(GuardViolation):
            guard_frame(frame, Framing.RTU, unit_id=UNIT, max_read_registers=MAXR)  # type: ignore[arg-type]


def test_v5_requires_logger_serial() -> None:
    with pytest.raises(GuardViolation):
        check(rtu(UNIT, 0, 1), Framing.SOLARMAN_V5)


@pytest.mark.parametrize(("unit_id", "maxr"), [(0, 60), (248, 60), (1, 0), (1, 126), (1, -1)])
def test_rejects_bad_configuration(unit_id: int, maxr: int) -> None:
    with pytest.raises(GuardViolation):
        guard_frame(rtu(1, 0, 1), Framing.RTU, unit_id=unit_id, max_read_registers=maxr)


@given(
    start=st.integers(0, 65535),
    count=st.integers(1, 125),
    maxr=st.integers(1, 125),
)
def test_property_accepts_exactly_valid_reads(start: int, count: int, maxr: int) -> None:
    ok = count <= maxr and start + count <= 65536
    for frame, framing in both(UNIT, start, count):
        if ok:
            assert check(frame, framing, maxr) == m.ReadRequest(UNIT, start, count)
        else:
            with pytest.raises(GuardViolation):
                check(frame, framing, maxr)


@given(frame=st.binary(max_size=40))
def test_property_random_bytes_never_pass_unless_valid_read(frame: bytes) -> None:
    for framing in (Framing.RTU, Framing.MBAP):
        try:
            result = check(frame, framing)
        except GuardViolation:
            continue
        assert result.unit == UNIT
        assert 1 <= result.count <= MAXR


@given(fc=st.integers(0, 255).filter(lambda x: x != 3), start=st.integers(0, 65535))
def test_property_no_other_function_code_passes(fc: int, start: int) -> None:
    with pytest.raises(GuardViolation):
        check(rtu(UNIT, start, 1, fc), Framing.RTU)
    with pytest.raises(GuardViolation):
        check(mbap(UNIT, start, 1, fc), Framing.MBAP)
