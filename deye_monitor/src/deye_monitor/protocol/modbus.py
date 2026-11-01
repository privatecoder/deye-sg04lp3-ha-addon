"""Modbus framing: FC03 read requests, the single-register FC16 write (v1 write layer), CRC16,
RTU and MBAP parsers.

The only write that can be built is FC16 with exactly one register (SPEC §10); there is no FC06
and no multi-register write. Frame limits are application limits, not Deye limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FC_READ_HOLDING = 0x03
FC_READ_HOLDING_EXC = 0x83
FC_WRITE_MULTIPLE = 0x10
FC_WRITE_MULTIPLE_EXC = 0x90
MAX_READ_REGISTERS = 125
MAX_ADDRESS_SPACE = 0x10000

RTU_REQUEST_LEN = 8
MBAP_REQUEST_LEN = 12
RTU_EXCEPTION_LEN = 5
MBAP_EXCEPTION_LEN = 9
# FC16 with count=1, byte count=2: PDU 8 bytes (fc, start, count, byte count, value)
WRITE_PDU_LEN = 8
RTU_WRITE_REQUEST_LEN = 1 + WRITE_PDU_LEN + 2  # 11
MBAP_WRITE_REQUEST_LEN = 7 + WRITE_PDU_LEN  # 15 (MBAP length field = 9)
RTU_WRITE_ACK_LEN = 8  # unit, fc, start(2), count(2), crc(2)
MBAP_WRITE_ACK_LEN = 12  # MBAP header (7) + fc, start(2), count(2)


class ModbusError(Exception):
    """Base class for protocol errors."""


class ModbusFrameError(ModbusError):
    """Malformed frame: length, byte count, CRC, unit, function code, transaction id."""


class ModbusExceptionResponse(ModbusError):
    """Device answered with a Modbus exception (function code | 0x80)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"modbus exception 0x{code:02x}")
        self.code = code
        self.raw = b""  # the response frame of a write (kept for the audit log)


def crc16(data: bytes) -> int:
    """Modbus CRC16 (poly 0xA001 reflected, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def append_crc(data: bytes) -> bytes:
    return data + crc16(data).to_bytes(2, "little")


def check_read_range(start: int, count: int) -> None:
    if not 0 <= start < MAX_ADDRESS_SPACE:
        raise ValueError(f"start address out of range: {start}")
    if not 1 <= count <= MAX_READ_REGISTERS:
        raise ValueError(f"register count out of range: {count}")
    if start + count > MAX_ADDRESS_SPACE:
        raise ValueError(f"read range exceeds address space: {start}+{count}")


def _check_unit(unit: int) -> None:
    if not 1 <= unit <= 247:
        raise ValueError(f"unit id out of range: {unit}")


def build_fc03_pdu(start: int, count: int) -> bytes:
    check_read_range(start, count)
    return bytes([FC_READ_HOLDING]) + start.to_bytes(2, "big") + count.to_bytes(2, "big")


def build_rtu_request(unit: int, start: int, count: int) -> bytes:
    _check_unit(unit)
    return append_crc(bytes([unit]) + build_fc03_pdu(start, count))


def build_mbap_request(transaction_id: int, unit: int, start: int, count: int) -> bytes:
    if not 0 <= transaction_id <= 0xFFFF:
        raise ValueError(f"transaction id out of range: {transaction_id}")
    _check_unit(unit)
    pdu = build_fc03_pdu(start, count)
    header = (
        transaction_id.to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (len(pdu) + 1).to_bytes(2, "big")
        + bytes([unit])
    )
    return header + pdu


def rtu_response_length(count: int) -> int:
    """Length of a normal RTU FC03 response for ``count`` registers."""
    return 5 + 2 * count


def mbap_response_length(count: int) -> int:
    """Length of a normal MBAP FC03 response for ``count`` registers."""
    return 9 + 2 * count


def _registers(data: bytes, count: int) -> list[int]:
    if len(data) != 1 + 2 * count or data[0] != 2 * count:
        raise ModbusFrameError("byte count mismatch")
    return [int.from_bytes(data[1 + 2 * i : 3 + 2 * i], "big") for i in range(count)]


def parse_rtu_response(frame: bytes, unit: int, count: int) -> list[int]:
    """Parse one complete RTU response. Raises on any deviation."""
    if len(frame) < RTU_EXCEPTION_LEN:
        raise ModbusFrameError("frame too short")
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ModbusFrameError("crc mismatch")
    if frame[0] != unit:
        raise ModbusFrameError("unit mismatch")
    if frame[1] == FC_READ_HOLDING_EXC:
        if len(frame) != RTU_EXCEPTION_LEN:
            raise ModbusFrameError("bad exception frame length")
        raise ModbusExceptionResponse(frame[2])
    if frame[1] != FC_READ_HOLDING:
        raise ModbusFrameError(f"unexpected function code 0x{frame[1]:02x}")
    return _registers(frame[2:-2], count)


def parse_mbap_response(frame: bytes, transaction_id: int, unit: int, count: int) -> list[int]:
    """Parse one complete MBAP response. Raises on any deviation."""
    if len(frame) < MBAP_EXCEPTION_LEN:
        raise ModbusFrameError("frame too short")
    if int.from_bytes(frame[0:2], "big") != transaction_id:
        raise ModbusFrameError("transaction id mismatch")
    if frame[2:4] != b"\x00\x00":
        raise ModbusFrameError("protocol id not 0")
    if int.from_bytes(frame[4:6], "big") != len(frame) - 6:
        raise ModbusFrameError("length field mismatch")
    if frame[6] != unit:
        raise ModbusFrameError("unit mismatch")
    if frame[7] == FC_READ_HOLDING_EXC:
        if len(frame) != MBAP_EXCEPTION_LEN:
            raise ModbusFrameError("bad exception frame length")
        raise ModbusExceptionResponse(frame[8])
    if frame[7] != FC_READ_HOLDING:
        raise ModbusFrameError(f"unexpected function code 0x{frame[7]:02x}")
    return _registers(frame[8:], count)


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """Decoded FC03 read request."""

    unit: int
    start: int
    count: int


# -- FC16, exactly one register (write layer v1) ----------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteAck:
    """Echo of a single-register FC16: the device repeats start address and count."""

    unit: int
    start: int
    count: int
    raw: bytes = field(default=b"", compare=False)  # the complete response frame (audit)


def _check_write(start: int, value: int) -> None:
    if not 0 <= start < MAX_ADDRESS_SPACE:
        raise ValueError(f"start address out of range: {start}")
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        raise ValueError(f"register word out of range: {value!r}")


def build_fc16_pdu(start: int, value: int) -> bytes:
    """FC16 PDU for ONE register: fc, start, count=1, byte count=2, value (8 bytes)."""
    _check_write(start, value)
    return (
        bytes([FC_WRITE_MULTIPLE])
        + start.to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + bytes([2])
        + value.to_bytes(2, "big")
    )


def build_rtu_write(unit: int, start: int, value: int) -> bytes:
    _check_unit(unit)
    return append_crc(bytes([unit]) + build_fc16_pdu(start, value))


def build_mbap_write(transaction_id: int, unit: int, start: int, value: int) -> bytes:
    if not 0 <= transaction_id <= 0xFFFF:
        raise ValueError(f"transaction id out of range: {transaction_id}")
    _check_unit(unit)
    pdu = build_fc16_pdu(start, value)
    header = (
        transaction_id.to_bytes(2, "big")
        + (0).to_bytes(2, "big")
        + (len(pdu) + 1).to_bytes(2, "big")
        + bytes([unit])
    )
    return header + pdu


def _exception(code: int, raw: bytes) -> ModbusExceptionResponse:
    exc = ModbusExceptionResponse(code)
    exc.raw = raw
    return exc


def _ack(unit: int, pdu: bytes, start: int, raw: bytes) -> WriteAck:
    if len(pdu) != 5 or pdu[0] != FC_WRITE_MULTIPLE:
        raise ModbusFrameError("malformed write acknowledgement")
    ack = WriteAck(unit, int.from_bytes(pdu[1:3], "big"), int.from_bytes(pdu[3:5], "big"), raw)
    if ack.start != start or ack.count != 1:
        raise ModbusFrameError("write acknowledgement does not echo start and count")
    return ack


def parse_rtu_write_response(frame: bytes, unit: int, start: int) -> WriteAck:
    """One complete RTU FC16 response: echo (8 bytes) or exception 0x90 (5 bytes)."""
    if len(frame) < RTU_EXCEPTION_LEN:
        raise ModbusFrameError("frame too short")
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ModbusFrameError("crc mismatch")
    if frame[0] != unit:
        raise ModbusFrameError("unit mismatch")
    if frame[1] == FC_WRITE_MULTIPLE_EXC:
        if len(frame) != RTU_EXCEPTION_LEN:
            raise ModbusFrameError("bad exception frame length")
        raise _exception(frame[2], frame)
    if len(frame) != RTU_WRITE_ACK_LEN:
        raise ModbusFrameError("bad write acknowledgement length")
    return _ack(unit, frame[1:-2], start, frame)


def parse_mbap_write_response(frame: bytes, transaction_id: int, unit: int, start: int) -> WriteAck:
    """One complete MBAP FC16 response: echo (12 bytes) or exception 0x90 (9 bytes)."""
    if len(frame) < MBAP_EXCEPTION_LEN:
        raise ModbusFrameError("frame too short")
    if int.from_bytes(frame[0:2], "big") != transaction_id:
        raise ModbusFrameError("transaction id mismatch")
    if frame[2:4] != b"\x00\x00":
        raise ModbusFrameError("protocol id not 0")
    if int.from_bytes(frame[4:6], "big") != len(frame) - 6:
        raise ModbusFrameError("length field mismatch")
    if frame[6] != unit:
        raise ModbusFrameError("unit mismatch")
    if frame[7] == FC_WRITE_MULTIPLE_EXC:
        if len(frame) != MBAP_EXCEPTION_LEN:
            raise ModbusFrameError("bad exception frame length")
        raise _exception(frame[8], frame)
    if len(frame) != MBAP_WRITE_ACK_LEN:
        raise ModbusFrameError("bad write acknowledgement length")
    return _ack(unit, frame[7:], start, frame)
