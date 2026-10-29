"""Read-only guard (SPEC §1.2, §8.1): the single gate for every byte sent to a device.

One call checks exactly one complete frame. Only FC03 is allowed; there is no write path.
Solarman V5 requests must match the one fixed layout built by ``build_v5_request``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from deye_monitor.protocol.modbus import (
    FC_READ_HOLDING,
    MAX_ADDRESS_SPACE,
    MAX_READ_REGISTERS,
    MBAP_REQUEST_LEN,
    RTU_REQUEST_LEN,
    ReadRequest,
    crc16,
)
from deye_monitor.protocol.solarman_v5 import (
    CONTROL_REQUEST,
    HEADER_LEN,
    REQUEST_FRAME_LEN,
    REQUEST_METADATA,
    REQUEST_METADATA_LEN,
    TRAILER_LEN,
    V5_END,
    V5_START,
    v5_checksum,
)


class GuardViolation(Exception):
    """A frame was refused; nothing may be sent."""


class Framing(StrEnum):
    RTU = "rtu"
    MBAP = "mbap"
    SOLARMAN_V5 = "solarman_v5"


@dataclass(frozen=True, slots=True)
class GuardedFrame:
    """Checked, immutable bytes. Transports send ``frame`` and nothing else."""

    frame: bytes
    request: ReadRequest


def guard_frame(
    frame: bytes,
    framing: Framing,
    *,
    unit_id: int,
    max_read_registers: int,
    logger_serial: int | None = None,
) -> GuardedFrame:
    """Validate one complete outgoing frame and return it for sending."""
    if type(frame) is not bytes:
        raise GuardViolation("frame must be immutable bytes")
    if not 1 <= unit_id <= 247:
        raise GuardViolation(f"configured unit id invalid: {unit_id}")
    if not 1 <= max_read_registers <= MAX_READ_REGISTERS:
        raise GuardViolation(f"configured max_read_registers invalid: {max_read_registers}")

    if framing is Framing.RTU:
        request = _check_rtu(frame, unit_id)
    elif framing is Framing.MBAP:
        request = _check_mbap(frame, unit_id)
    else:
        if logger_serial is None or not 1 <= logger_serial <= 0xFFFFFFFF:
            raise GuardViolation("solarman_v5 requires the configured logger serial")
        request = _check_v5(frame, unit_id, logger_serial)

    if not 1 <= request.count <= max_read_registers:
        raise GuardViolation(f"register count out of range: {request.count}")
    if request.start + request.count > MAX_ADDRESS_SPACE:
        raise GuardViolation("read range exceeds address space")
    return GuardedFrame(frame=frame, request=request)


def _decode_pdu(unit: int, pdu: bytes, expected_unit: int) -> ReadRequest:
    if unit != expected_unit:
        raise GuardViolation(f"unit {unit} is not the configured target {expected_unit}")
    if len(pdu) != 5:
        raise GuardViolation("PDU must be exactly 5 bytes")
    if pdu[0] != FC_READ_HOLDING:
        raise GuardViolation(f"function code 0x{pdu[0]:02x} not allowed")
    return ReadRequest(
        unit=unit,
        start=int.from_bytes(pdu[1:3], "big"),
        count=int.from_bytes(pdu[3:5], "big"),
    )


def _check_rtu(frame: bytes, unit_id: int) -> ReadRequest:
    if len(frame) != RTU_REQUEST_LEN:
        raise GuardViolation("RTU request must be exactly 8 bytes")
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise GuardViolation("RTU crc mismatch")
    return _decode_pdu(frame[0], frame[1:6], unit_id)


def _check_mbap(frame: bytes, unit_id: int) -> ReadRequest:
    if len(frame) != MBAP_REQUEST_LEN:
        raise GuardViolation("MBAP request must be exactly 12 bytes")
    if frame[2:4] != b"\x00\x00":
        raise GuardViolation("MBAP protocol id must be 0")
    if int.from_bytes(frame[4:6], "big") != len(frame) - 6:
        raise GuardViolation("MBAP length field mismatch")
    return _decode_pdu(frame[6], frame[7:], unit_id)


def _check_v5(frame: bytes, unit_id: int, logger_serial: int) -> ReadRequest:
    if len(frame) != REQUEST_FRAME_LEN:
        raise GuardViolation(f"V5 request must be exactly {REQUEST_FRAME_LEN} bytes")
    if frame[0] != V5_START or frame[-1] != V5_END:
        raise GuardViolation("V5 start/end byte mismatch")
    if int.from_bytes(frame[1:3], "little") != len(frame) - HEADER_LEN - TRAILER_LEN:
        raise GuardViolation("V5 length field mismatch")
    if int.from_bytes(frame[3:5], "little") != CONTROL_REQUEST:
        raise GuardViolation("V5 control code must be 0x4510")
    if int.from_bytes(frame[7:11], "little") != logger_serial:
        raise GuardViolation("V5 logger serial is not the configured one")
    if v5_checksum(frame[1:-2]) != frame[-2]:
        raise GuardViolation("V5 checksum mismatch")
    meta_end = HEADER_LEN + REQUEST_METADATA_LEN
    if frame[HEADER_LEN:meta_end] != REQUEST_METADATA:
        raise GuardViolation("V5 metadata must be the fixed constants")
    return _check_rtu(frame[meta_end:-TRAILER_LEN], unit_id)
