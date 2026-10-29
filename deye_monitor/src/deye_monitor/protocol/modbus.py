"""Modbus FC03 framing: request building, CRC16, RTU and MBAP parsers.

Only the read path (FC03) exists. Frame limits are application limits, not Deye limits.
"""

from __future__ import annotations

from dataclasses import dataclass

FC_READ_HOLDING = 0x03
FC_READ_HOLDING_EXC = 0x83
MAX_READ_REGISTERS = 125
MAX_ADDRESS_SPACE = 0x10000

RTU_REQUEST_LEN = 8
MBAP_REQUEST_LEN = 12
RTU_EXCEPTION_LEN = 5
MBAP_EXCEPTION_LEN = 9


class ModbusError(Exception):
    """Base class for protocol errors."""


class ModbusFrameError(ModbusError):
    """Malformed frame: length, byte count, CRC, unit, function code, transaction id."""


class ModbusExceptionResponse(ModbusError):
    """Device answered with a Modbus exception (function code | 0x80)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"modbus exception 0x{code:02x}")
        self.code = code


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
