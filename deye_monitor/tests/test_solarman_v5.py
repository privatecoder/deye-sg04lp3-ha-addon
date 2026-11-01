import contextlib
import inspect

import pytest
from hypothesis import given
from hypothesis import strategies as st

from deye_monitor.protocol import modbus as m
from deye_monitor.protocol import solarman_v5 as v5
from deye_monitor.protocol.guard import Framing, GuardViolation, guard_frame

SERIAL = 0x12345678
SEQ = 0x0102
UNIT = 1

# Hand-derived from the protocol layout table (the reference documents no hex example):
# A5 | len 0x0017 LE | 10 45 | seq 02 01 | serial 78 56 34 12 | 02 + 14 zero bytes | RTU | sum | 15
GOLDEN_REQUEST = bytes.fromhex("a51700104502017856341202" + "00" * 14 + "01030000000ac5cd2515")


def make_frame(
    control: int = v5.CONTROL_RESPONSE,
    sequence: int = SEQ,
    serial: int = SERIAL,
    payload: bytes = b"",
    *,
    checksum_delta: int = 0,
    end: int = v5.V5_END,
) -> bytes:
    body = (
        len(payload).to_bytes(2, "little")
        + control.to_bytes(2, "little")
        + sequence.to_bytes(2, "little")
        + serial.to_bytes(4, "little")
        + payload
    )
    return (
        bytes([v5.V5_START]) + body + bytes([(v5.v5_checksum(body) + checksum_delta) & 0xFF, end])
    )


def rtu_resp(regs: list[int], unit: int = UNIT) -> bytes:
    body = bytes([unit, 3, 2 * len(regs)]) + b"".join(r.to_bytes(2, "big") for r in regs)
    return m.append_crc(body)


def response(rtu: bytes, **kw: int) -> bytes:
    meta = bytes([0x02, 0x01]) + bytes(12)
    return make_frame(payload=meta + rtu, **kw)


GOOD = response(rtu_resp([10, 20, 30]))
HEARTBEAT = make_frame(control=0x4710, payload=bytes(1))
HANDSHAKE = make_frame(control=0x4110, payload=bytes(5))
TIME_REQ = make_frame(control=0x4810, payload=bytes(10))


def test_golden_request() -> None:
    rtu = m.build_rtu_request(1, 0, 10)
    assert v5.build_v5_request(SERIAL, SEQ, rtu) == GOLDEN_REQUEST
    assert len(GOLDEN_REQUEST) == v5.REQUEST_FRAME_LEN == 36


@pytest.mark.parametrize(
    ("serial", "seq", "rtu"),
    [
        (0, 1, bytes(8)),
        (2**32, 1, bytes(8)),
        (1, -1, bytes(8)),
        (1, 0x10000, bytes(8)),
        (1, 1, bytes(7)),
        (1, 1, bytes(9)),
    ],
)
def test_builder_rejects_bad_input(serial: int, seq: int, rtu: bytes) -> None:
    with pytest.raises(ValueError):
        v5.build_v5_request(serial, seq, rtu)


def test_no_sending_api() -> None:
    names = [n for n, _ in inspect.getmembers(v5) if not n.startswith("_")]
    assert [n for n in names if n.startswith(("build", "send", "write", "reply", "answer"))] == [
        "build_v5_request",
        "build_v5_write_request",
    ]
    assert not [n for n in dir(v5.V5StreamParser) if n.startswith(("send", "write", "reply"))]


def parse_all(chunks: list[bytes], seq: int = SEQ) -> tuple[list[v5.V5Response], v5.V5StreamParser]:
    p = v5.V5StreamParser(SERIAL)
    out: list[v5.V5Response] = []
    for c in chunks:
        out += p.feed(c, seq)
    return out, p


def test_valid_response() -> None:
    out, p = parse_all([GOOD])
    assert len(out) == 1
    assert out[0].frame_type == 2
    assert v5.decode_read_response(out[0], UNIT, 3, p.counters) == [10, 20, 30]
    assert p.buffered == 0


def test_every_split_position() -> None:
    for i in range(len(GOOD) + 1):
        out, p = parse_all([GOOD[:i], GOOD[i:]])
        assert len(out) == 1, i
        assert p.buffered == 0


def test_one_byte_chunks() -> None:
    out, _ = parse_all([bytes([b]) for b in GOOD])
    assert len(out) == 1


def test_two_frames_in_one_chunk_only_matching_accepted() -> None:
    other = response(rtu_resp([1, 2, 3]), sequence=0x0999)
    out, p = parse_all([other + GOOD])
    assert len(out) == 1
    assert p.counters.foreign_sequence == 1
    out, _ = parse_all([GOOD + GOOD])
    assert len(out) == 2


def test_heartbeat_and_management_frames_ignored() -> None:
    stream = HEARTBEAT + HANDSHAKE + GOOD + TIME_REQ + HEARTBEAT
    out, p = parse_all([stream])
    assert len(out) == 1
    assert p.counters.ignored_control == 4
    # the parser result type carries no frame that could be echoed back
    assert all(isinstance(r, v5.V5Response) for r in out)
    out, p = parse_all([HEARTBEAT, TIME_REQ])
    assert out == []
    assert p.counters.ignored_control == 2


def test_resync_after_garbage() -> None:
    out, p = parse_all([b"\x00\x01\xa5\xff\xff" + GOOD])
    assert len(out) == 1
    assert p.counters.resync_bytes > 0


def test_rejections() -> None:
    cases = {
        "sequence": (response(rtu_resp([1]), sequence=0x0103), "foreign_sequence"),
        "serial": (response(rtu_resp([1]), serial=SERIAL + 1), "foreign_serial"),
        "checksum": (response(rtu_resp([1]), checksum_delta=1), "bad_checksum"),
        "end": (response(rtu_resp([1]), end=0x16), "bad_end"),
    }
    for name, (frame, counter) in cases.items():
        out, p = parse_all([frame])
        assert out == [], name
        assert getattr(p.counters, counter) >= 1, name


def test_rejected_frame_does_not_block_following_good_frame() -> None:
    bad = response(rtu_resp([1]), checksum_delta=1)
    out, _ = parse_all([bad + GOOD])
    assert len(out) == 1


def test_huge_length_field_no_memory_growth() -> None:
    p = v5.V5StreamParser(SERIAL)
    forged = bytes([v5.V5_START, 0xFF, 0xFF]) + bytes(10)
    for _ in range(2000):
        p.feed(forged, SEQ)
        assert p.buffered <= v5.MAX_FRAME_LEN
    assert p.counters.oversize > 0
    assert p.feed(GOOD, SEQ)
    p.feed(b"\xa5" * 100_000, SEQ)
    assert p.buffered <= v5.MAX_FRAME_LEN


def test_logger_error_payloads() -> None:
    p = v5.V5StreamParser(SERIAL)
    short = make_frame(payload=bytes(5))
    assert p.feed(short, SEQ) == []
    assert p.counters.short_payload == 1
    out = p.feed(response(b""), SEQ)
    with pytest.raises(v5.V5LoggerError):
        v5.decode_read_response(out[0], UNIT, 1, p.counters)


def test_modbus_exception_through_v5() -> None:
    out, p = parse_all([response(m.append_crc(bytes([UNIT, 0x83, 2])))])
    with pytest.raises(m.ModbusExceptionResponse):
        v5.decode_read_response(out[0], UNIT, 1, p.counters)


def test_double_crc_quirk_valid() -> None:
    out, p = parse_all([response(rtu_resp([7, 8]) + b"\x00\x00")])
    assert v5.decode_read_response(out[0], UNIT, 2, p.counters) == [7, 8]
    assert p.counters.quirk_double_crc == 1


def test_double_crc_quirk_invalid_not_trimmed() -> None:
    good = rtu_resp([7, 8])
    broken = good[:-1] + bytes([good[-1] ^ 1])
    for rtu in (
        broken + b"\x00\x00",
        good + b"\x01\x00",
        good + b"\x00\x00\x00\x00",
        good[:-2] + b"\x00\x00\x00\x00",
    ):
        out, p = parse_all([response(rtu)])
        with pytest.raises(m.ModbusFrameError):
            v5.decode_read_response(out[0], UNIT, 2, p.counters)
        assert p.counters.quirk_double_crc == 0


def test_wrong_unit_through_v5() -> None:
    out, p = parse_all([response(rtu_resp([1], unit=2))])
    with pytest.raises(m.ModbusFrameError):
        v5.decode_read_response(out[0], UNIT, 1, p.counters)


@given(data=st.binary(max_size=600), cut=st.integers(0, 600))
def test_property_parser_never_raises_on_garbage(data: bytes, cut: int) -> None:
    p = v5.V5StreamParser(SERIAL)
    for chunk in (data[:cut], data[cut:]):
        for r in p.feed(chunk, SEQ):
            with contextlib.suppress(m.ModbusError, v5.V5Error):
                v5.decode_read_response(r, UNIT, 1, p.counters)
        assert p.buffered <= v5.MAX_FRAME_LEN


@given(prefix=st.binary(max_size=50), split=st.integers(0, 50))
def test_property_good_frame_after_garbage_or_waiting_prefix(prefix: bytes, split: int) -> None:
    """A plausible incomplete prefix (e.g. ``A5 90 01``) may hide the frame until the transport
    resets the parser (timeout policy); otherwise the frame is found."""
    p = v5.V5StreamParser(SERIAL)
    stream = prefix + GOOD
    out = p.feed(stream[:split], SEQ) + p.feed(stream[split:], SEQ)
    if any(r.rtu == rtu_resp([10, 20, 30]) for r in out):
        return
    assert p.buffered > 0  # still waiting for a (bogus) announced frame, never silently lost
    p.reset()
    assert any(r.rtu == rtu_resp([10, 20, 30]) for r in p.feed(GOOD, SEQ))


def test_plausible_prefix_blocks_until_reset() -> None:
    p = v5.V5StreamParser(SERIAL)
    assert p.feed(bytes([0xA5, 0x90, 0x01]) + GOOD, SEQ) == []
    assert p.buffered == 3 + len(GOOD)
    p.reset()
    assert p.buffered == 0
    assert len(p.feed(GOOD, SEQ)) == 1


def test_unsupported_response_metadata_is_not_decoded() -> None:
    for frame_type, status in [(0x00, 0x00), (0x02, 0x00), (0x01, 0x01), (0x02, 0x02)]:
        meta = bytes([frame_type, status]) + bytes(12)
        frame = make_frame(payload=meta + rtu_resp([1, 2]))
        out, p = parse_all([frame])
        assert len(out) == 1
        assert not out[0].supported
        assert out[0].rtu == b""  # the bytes behind unknown metadata are never interpreted
        assert p.counters.unsupported_metadata == 1
        with pytest.raises(v5.V5LoggerError, match="unsupported"):
            v5.decode_read_response(out[0], UNIT, 2, p.counters)


def test_supported_metadata_and_sequence_high_byte() -> None:
    out, p = parse_all([response(rtu_resp([1, 2]), sequence=0x0702)], seq=0x0102)
    assert len(out) == 1 and out[0].supported
    assert v5.decode_read_response(out[0], UNIT, 2, p.counters) == [1, 2]
    assert p.counters.unsupported_metadata == 0


# --- guard -----------------------------------------------------------------------------------


def v5_req(unit: int = UNIT, start: int = 0, count: int = 10, seq: int = SEQ) -> bytes:
    return v5.build_v5_request(SERIAL, seq, m.build_rtu_request(unit, start, count))


def vcheck(frame: bytes, maxr: int = 60) -> m.ReadRequest:
    return guard_frame(
        frame, Framing.SOLARMAN_V5, unit_id=UNIT, max_read_registers=maxr, logger_serial=SERIAL
    ).request


def fix(frame: bytes) -> bytes:
    """Recompute the V5 checksum of a mutated frame."""
    return frame[:-2] + bytes([v5.v5_checksum(frame[1:-2]), v5.V5_END])


def test_guard_v5_golden_and_edges() -> None:
    assert vcheck(GOLDEN_REQUEST) == m.ReadRequest(UNIT, 0, 10)
    assert vcheck(v5_req(start=65535, count=1)) == m.ReadRequest(UNIT, 65535, 1)
    assert vcheck(v5_req(count=1)).count == 1
    assert vcheck(v5_req(count=125), 125).count == 125
    assert vcheck(v5_req(start=65411, count=125), 125).start == 65411


def test_guard_v5_rejects_range() -> None:
    for start, count in [(0, 61), (65535, 2)]:
        rtu = m.append_crc(bytes([UNIT, 3]) + start.to_bytes(2, "big") + count.to_bytes(2, "big"))
        with pytest.raises(GuardViolation):
            vcheck(v5.build_v5_request(SERIAL, SEQ, rtu))
    rtu0 = m.append_crc(bytes([UNIT, 3, 0, 0, 0, 0]))
    with pytest.raises(GuardViolation):
        vcheck(v5.build_v5_request(SERIAL, SEQ, rtu0))


def test_guard_v5_negatives() -> None:
    good = v5_req()
    write06 = m.append_crc(bytes([UNIT, 6, 0, 1, 0, 1]))
    write10 = m.append_crc(bytes([UNIT, 0x10, 0, 1, 0, 1, 2, 0, 1]))
    bad: dict[str, bytes] = {
        "suffix": good + b"\x00",
        "prefix": b"\x00" + good,
        "two frames": good + good,
        "truncated": good[:-1],
        "empty": b"",
        "control": fix(good[:3] + b"\x10\x15" + good[5:]),
        "metadata": fix(good[:11] + b"\x03" + good[12:]),
        "metadata time": fix(good[:20] + b"\x01" + good[21:]),
        "foreign serial": v5.build_v5_request(SERIAL + 1, SEQ, m.build_rtu_request(UNIT, 0, 1)),
        "bad checksum": good[:-2] + bytes([good[-2] ^ 1]) + good[-1:],
        "bad end": good[:-1] + b"\x16",
        "bad start": fix(b"\xa4" + good[1:]),
        "length +1": fix(good[:1] + b"\x18\x00" + good[3:]),
        "length -1": fix(good[:1] + b"\x16\x00" + good[3:]),
        "inner crc": fix(good[:33] + bytes([good[33] ^ 1]) + good[34:]),
        "inner FC06": v5.build_v5_request(SERIAL, SEQ, write06),
        "inner FC10": fix(
            good[:1] + (len(write10) + 15).to_bytes(2, "little") + good[3:26] + write10 + good[-2:]
        ),
        "inner foreign unit": v5.build_v5_request(SERIAL, SEQ, m.build_rtu_request(2, 0, 1)),
        "inner broadcast": v5.build_v5_request(
            SERIAL, SEQ, m.append_crc(bytes([0, 3, 0, 0, 0, 1]))
        ),
        "mutable": bytearray(good),  # type: ignore[dict-item]
    }
    for name, frame in bad.items():
        with pytest.raises(GuardViolation):
            vcheck(frame)
        assert name


def test_guard_v5_serial_configuration() -> None:
    for serial in (None, 0, 2**32):
        with pytest.raises(GuardViolation):
            guard_frame(
                v5_req(),
                Framing.SOLARMAN_V5,
                unit_id=UNIT,
                max_read_registers=60,
                logger_serial=serial,
            )
    with pytest.raises(GuardViolation):
        guard_frame(
            v5_req(),
            Framing.SOLARMAN_V5,
            unit_id=UNIT,
            max_read_registers=60,
            logger_serial=SERIAL + 1,
        )


@given(index=st.integers(0, v5.REQUEST_FRAME_LEN - 1), delta=st.integers(1, 255))
def test_property_single_byte_mutation_rejected(index: int, delta: int) -> None:
    good = v5_req()
    mutated = bytearray(good)
    mutated[index] = (mutated[index] + delta) & 0xFF
    with pytest.raises(GuardViolation):
        vcheck(bytes(mutated))


@given(index=st.integers(0, v5.REQUEST_FRAME_LEN - 3), delta=st.integers(1, 255))
def test_property_mutation_with_fixed_checksum_only_sequence_survives(
    index: int, delta: int
) -> None:
    good = v5_req()
    mutated = bytearray(good)
    mutated[index] = (mutated[index] + delta) & 0xFF
    frame = fix(bytes(mutated))
    if index in (5, 6):
        assert vcheck(frame) == m.ReadRequest(UNIT, 0, 10)
    else:
        with pytest.raises(GuardViolation):
            vcheck(frame)


@given(frame=st.binary(max_size=60))
def test_property_random_bytes_never_pass(frame: bytes) -> None:
    with pytest.raises(GuardViolation):
        vcheck(frame)


@given(
    seq=st.integers(0, 0xFFFF),
    start=st.integers(0, 65535),
    count=st.integers(1, 125),
    maxr=st.integers(1, 125),
)
def test_property_guard_accepts_exactly_valid_requests(
    seq: int, start: int, count: int, maxr: int
) -> None:
    rtu = m.append_crc(bytes([UNIT, 3]) + start.to_bytes(2, "big") + count.to_bytes(2, "big"))
    frame = v5.build_v5_request(SERIAL, seq, rtu)
    if count <= maxr and start + count <= 65536:
        assert vcheck(frame, maxr) == m.ReadRequest(UNIT, start, count)
    else:
        with pytest.raises(GuardViolation):
            vcheck(frame, maxr)
