"""Solarman V5 framing: request building and a defensive stream parser.

The module only ever builds REQUEST frames carrying an FC03 read or the single-register FC16 write
(write layer v1, SPEC §10). It contains no function that
builds a response, time, heartbeat or management frame: incoming frames other than the awaited
response are counted and dropped, never answered (SPEC §1, GRUNDLAGEN §2.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from deye_monitor.protocol.modbus import (
    RTU_REQUEST_LEN,
    RTU_WRITE_REQUEST_LEN,
    ModbusExceptionResponse,
    ModbusFrameError,
    WriteAck,
    parse_rtu_response,
    parse_rtu_write_response,
)

V5_START = 0xA5
V5_END = 0x15
CONTROL_REQUEST = 0x4510  # wire: 10 45
CONTROL_RESPONSE = 0x1510  # wire: 10 15
HEADER_LEN = 11  # start, length(2), control(2), sequence(2), logger serial(4)
TRAILER_LEN = 2  # checksum, end
MAX_FRAME_LEN = 512  # application limit (SPEC §8.1), not a device limit
REQUEST_METADATA_LEN = 15
RESPONSE_METADATA_LEN = 14
# frame type 0x02 (inverter), sensor type 0x0000, three zero time fields
REQUEST_METADATA = bytes([0x02]) + bytes(REQUEST_METADATA_LEN - 1)
REQUEST_PAYLOAD_LEN = REQUEST_METADATA_LEN + RTU_REQUEST_LEN
REQUEST_FRAME_LEN = HEADER_LEN + REQUEST_PAYLOAD_LEN + TRAILER_LEN
WRITE_REQUEST_PAYLOAD_LEN = REQUEST_METADATA_LEN + RTU_WRITE_REQUEST_LEN
WRITE_REQUEST_FRAME_LEN = HEADER_LEN + WRITE_REQUEST_PAYLOAD_LEN + TRAILER_LEN  # 39
_MIN_RTU_LEN = 5
# Inverter response metadata accepted by this app (frame type 0x02 = inverter, status 0x01 =
# real-time data). Other combinations are not decoded: the RTU offset and meaning are unknown.
RESPONSE_FRAME_TYPE = 0x02
RESPONSE_STATUS = 0x01
_MAX_SERIAL = 0xFFFFFFFF


class V5Error(Exception):
    """Base class for Solarman V5 errors."""


class V5LoggerError(V5Error):
    """Logger answered without usable Modbus data (e.g. it timed out talking to the inverter)."""


def v5_checksum(data: bytes) -> int:
    """Sum modulo 256 over the bytes between start and checksum."""
    return sum(data) & 0xFF


def build_v5_request(logger_serial: int, sequence: int, rtu_frame: bytes) -> bytes:
    """Wrap one complete 8-byte RTU read ADU in a V5 request frame."""
    if not 1 <= logger_serial <= _MAX_SERIAL:
        raise ValueError(f"logger serial out of range: {logger_serial}")
    if not 0 <= sequence <= 0xFFFF:
        raise ValueError(f"sequence out of range: {sequence}")
    if type(rtu_frame) is not bytes or len(rtu_frame) != RTU_REQUEST_LEN:
        raise ValueError("rtu_frame must be exactly one 8-byte RTU request")
    body = (
        REQUEST_PAYLOAD_LEN.to_bytes(2, "little")
        + CONTROL_REQUEST.to_bytes(2, "little")
        + sequence.to_bytes(2, "little")
        + logger_serial.to_bytes(4, "little")
        + REQUEST_METADATA
        + rtu_frame
    )
    return bytes([V5_START]) + body + bytes([v5_checksum(body), V5_END])


def build_v5_write_request(logger_serial: int, sequence: int, rtu_frame: bytes) -> bytes:
    """Wrap one complete 11-byte RTU FC16 (one register) ADU in a V5 request frame (39 bytes)."""
    if not 1 <= logger_serial <= _MAX_SERIAL:
        raise ValueError(f"logger serial out of range: {logger_serial}")
    if not 0 <= sequence <= 0xFFFF:
        raise ValueError(f"sequence out of range: {sequence}")
    if type(rtu_frame) is not bytes or len(rtu_frame) != RTU_WRITE_REQUEST_LEN:
        raise ValueError("rtu_frame must be exactly one 11-byte RTU write request")
    body = (
        WRITE_REQUEST_PAYLOAD_LEN.to_bytes(2, "little")
        + CONTROL_REQUEST.to_bytes(2, "little")
        + sequence.to_bytes(2, "little")
        + logger_serial.to_bytes(4, "little")
        + REQUEST_METADATA
        + rtu_frame
    )
    return bytes([V5_START]) + body + bytes([v5_checksum(body), V5_END])


@dataclass(slots=True)
class V5Counters:
    """Diagnostic counters (SPEC §2 diagnostics)."""

    frames: int = 0
    resync_bytes: int = 0
    oversize: int = 0
    bad_checksum: int = 0
    bad_end: int = 0
    ignored_control: int = 0
    foreign_serial: int = 0
    foreign_sequence: int = 0
    short_payload: int = 0
    unsupported_metadata: int = 0
    quirk_double_crc: int = 0


@dataclass(frozen=True, slots=True)
class V5Response:
    """An accepted response frame: sequence/serial matched, checksum and end verified.

    ``rtu`` is empty when the metadata is not the supported inverter response; the bytes after
    the metadata are then not interpreted at all (``supported`` is false)."""

    sequence: int
    frame_type: int
    status: int
    rtu: bytes

    @property
    def supported(self) -> bool:
        return self.frame_type == RESPONSE_FRAME_TYPE and self.status == RESPONSE_STATUS


@dataclass(slots=True)
class V5StreamParser:
    """Byte-stream parser. ``feed`` returns accepted responses only; it never produces output
    that could be sent to a device."""

    logger_serial: int
    counters: V5Counters = field(default_factory=V5Counters)
    _buf: bytearray = field(default_factory=bytearray)

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes | bytearray, sequence: int) -> list[V5Response]:
        """Add received bytes; return responses matching ``sequence`` (low byte) and serial."""
        buf = self._buf
        buf.extend(data)
        out: list[V5Response] = []
        while True:
            start = buf.find(V5_START)
            if start < 0:
                self.counters.resync_bytes += len(buf)
                buf.clear()
                break
            if start:
                self.counters.resync_bytes += start
                del buf[:start]
            if len(buf) < 3:
                break
            total = HEADER_LEN + TRAILER_LEN + int.from_bytes(buf[1:3], "little")
            if total > MAX_FRAME_LEN:
                self.counters.oversize += 1
                del buf[0]
                continue
            if len(buf) < total:
                break
            frame = bytes(buf[:total])
            if frame[-1] != V5_END:
                self.counters.bad_end += 1
                del buf[0]
                continue
            if v5_checksum(frame[1:-2]) != frame[-2]:
                self.counters.bad_checksum += 1
                del buf[0]
                continue
            del buf[:total]
            self.counters.frames += 1
            response = self._select(frame, sequence)
            if response is not None:
                out.append(response)
        return out

    def _select(self, frame: bytes, sequence: int) -> V5Response | None:
        if int.from_bytes(frame[3:5], "little") != CONTROL_RESPONSE:
            self.counters.ignored_control += 1
            return None
        if int.from_bytes(frame[7:11], "little") != self.logger_serial:
            self.counters.foreign_serial += 1
            return None
        if frame[5] != sequence & 0xFF:
            self.counters.foreign_sequence += 1
            return None
        payload = frame[HEADER_LEN:-TRAILER_LEN]
        if len(payload) < RESPONSE_METADATA_LEN:
            self.counters.short_payload += 1
            return None
        frame_type, status = payload[0], payload[1]
        supported = frame_type == RESPONSE_FRAME_TYPE and status == RESPONSE_STATUS
        if not supported:
            self.counters.unsupported_metadata += 1
        return V5Response(
            sequence=int.from_bytes(frame[5:7], "little"),
            frame_type=frame_type,
            status=status,
            rtu=payload[RESPONSE_METADATA_LEN:] if supported else b"",
        )


def decode_read_response(
    response: V5Response, unit: int, count: int, counters: V5Counters
) -> list[int]:
    """Decode the inner RTU response of an accepted V5 response.

    The known Deye double-CRC quirk (two extra zero bytes) is only corrected when the trimmed
    frame is strictly valid (byte count and CRC); otherwise the original error is raised.
    """
    if not response.supported:
        raise V5LoggerError(
            f"unsupported logger response (frame type 0x{response.frame_type:02x}, "
            f"status 0x{response.status:02x})"
        )
    rtu = response.rtu
    if len(rtu) < _MIN_RTU_LEN:
        raise V5LoggerError(f"logger returned no modbus response ({len(rtu)} bytes)")
    try:
        return parse_rtu_response(rtu, unit, count)
    except ModbusFrameError:
        if rtu[-2:] != b"\x00\x00" or len(rtu) < _MIN_RTU_LEN + 2:
            raise
        trimmed = rtu[:-2]
        try:
            registers = parse_rtu_response(trimmed, unit, count)
        except ModbusExceptionResponse:
            counters.quirk_double_crc += 1
            raise
        except ModbusFrameError:
            pass
        else:
            counters.quirk_double_crc += 1
            return registers
        raise


def decode_write_response(response: V5Response, unit: int, start: int) -> WriteAck:
    """Decode the inner RTU FC16 response of an accepted V5 response. Strict: no double-CRC
    correction is applied to a write acknowledgement."""
    if not response.supported:
        raise V5LoggerError(
            f"unsupported logger response (frame type 0x{response.frame_type:02x}, "
            f"status 0x{response.status:02x})"
        )
    if len(response.rtu) < _MIN_RTU_LEN:
        raise V5LoggerError(f"logger returned no modbus response ({len(response.rtu)} bytes)")
    return parse_rtu_write_response(response.rtu, unit, start)
