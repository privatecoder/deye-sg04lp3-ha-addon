"""Guard (SPEC §1.2, §8.1, §10): the single gate for every byte sent to a device.

``guard_frame`` checks exactly one complete read frame: only FC03 passes, FC16 and FC06 never.
``guard_write_frame`` is the only write gate: exactly one FC16 with count 1 for a register of the
fixed v1 table, carrying exactly the word a single-use ``WriteGrant`` names (foreign bits
excluded by the register's mask). Solarman V5 requests must match the one fixed layout built by
``build_v5_request`` / ``build_v5_write_request``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from deye_monitor.protocol.modbus import (
    FC_READ_HOLDING,
    FC_WRITE_MULTIPLE,
    MAX_ADDRESS_SPACE,
    MAX_READ_REGISTERS,
    MBAP_REQUEST_LEN,
    MBAP_WRITE_REQUEST_LEN,
    RTU_REQUEST_LEN,
    RTU_WRITE_REQUEST_LEN,
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
    WRITE_REQUEST_FRAME_LEN,
    v5_checksum,
)

# The complete v1 write whitelist: register -> writable mask (V105.8 R/W, SPEC §10). Nothing else
# can ever pass the write guard. Descriptors in ``writes.py`` must agree with this table.
WRITABLE_MASKS: dict[int, int] = {
    108: 0xFFFF,  # maximum battery charge current
    109: 0xFFFF,  # maximum battery discharge current
    125: 0xFFFF,  # generator charge current
    128: 0xFFFF,  # GRID charge current
    130: 0x0001,  # GRID charge enable (bit 0 only)
    134: 0xFFFF,  # Smart Load OFF voltage
    135: 0xFFFF,  # Smart Load OFF SOC
    136: 0xFFFF,  # Smart Load ON voltage
    137: 0xFFFF,  # Smart Load ON SOC
}
# Registers that must never be written, listed explicitly on top of "not on the whitelist".
DENIED_REGISTERS: frozenset[int] = frozenset(
    {22, 23, 24, 80, 81, 91, 92, 98, 111, 115, 116, 117, 141, 142, 336, 341}
    | set(range(146, 178))
    | set(range(1100, 1122))
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


@dataclass(frozen=True, slots=True)
class WriteGrant:
    """What one single-use operation may put on the wire: one register, the word read just
    before (``old_word``) and the word to send (``new_word``). Only bits of ``mask`` may differ."""

    unit: int
    register: int
    mask: int
    old_word: int
    new_word: int


@dataclass(frozen=True, slots=True)
class GuardedWrite:
    frame: bytes
    grant: WriteGrant


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


def check_grant(grant: WriteGrant) -> None:
    """The grant itself must be inside the fixed table (a second line of defence)."""
    if type(grant) is not WriteGrant:
        raise GuardViolation("not a write grant")
    if grant.register in DENIED_REGISTERS or grant.register not in WRITABLE_MASKS:
        raise GuardViolation(f"register {grant.register} is not writable")
    if grant.mask != WRITABLE_MASKS[grant.register]:
        raise GuardViolation("mask differs from the register's writable mask")
    for word in (grant.old_word, grant.new_word):
        if type(word) is not int or not 0 <= word <= 0xFFFF:
            raise GuardViolation("register word out of range")
    if (grant.new_word ^ grant.old_word) & ~grant.mask & 0xFFFF:
        raise GuardViolation("the write would change bits outside the writable mask")
    if grant.new_word == grant.old_word:
        raise GuardViolation("an unchanged word is not written")


def guard_write_frame(
    frame: bytes,
    framing: Framing,
    *,
    grant: WriteGrant,
    logger_serial: int | None = None,
) -> GuardedWrite:
    """Validate one complete outgoing FC16 frame against ``grant`` and return it for sending."""
    if type(frame) is not bytes:
        raise GuardViolation("frame must be immutable bytes")
    check_grant(grant)
    if not 1 <= grant.unit <= 247:
        raise GuardViolation(f"configured unit id invalid: {grant.unit}")
    if framing is Framing.RTU:
        start, value = _check_rtu_write(frame, grant.unit)
    elif framing is Framing.MBAP:
        start, value = _check_mbap_write(frame, grant.unit)
    else:
        if logger_serial is None or not 1 <= logger_serial <= 0xFFFFFFFF:
            raise GuardViolation("solarman_v5 requires the configured logger serial")
        if grant.unit != 1:
            raise GuardViolation("a Solarman V5 logger is written on its inner unit 1 only")
        start, value = _check_v5_write(frame, grant.unit, logger_serial)
    if start != grant.register:
        raise GuardViolation(f"register {start} is not the granted register {grant.register}")
    if value != grant.new_word:
        raise GuardViolation("the word differs from the granted word")
    return GuardedWrite(frame=frame, grant=grant)


def _decode_write_pdu(unit: int, pdu: bytes, expected_unit: int) -> tuple[int, int]:
    if unit != expected_unit:
        raise GuardViolation(f"unit {unit} is not the configured target {expected_unit}")
    if len(pdu) != 8:
        raise GuardViolation("write PDU must be exactly 8 bytes")
    if pdu[0] != FC_WRITE_MULTIPLE:
        raise GuardViolation(f"function code 0x{pdu[0]:02x} not allowed for a write")
    if int.from_bytes(pdu[3:5], "big") != 1 or pdu[5] != 2:
        raise GuardViolation("a write must be exactly one register (count 1, byte count 2)")
    return int.from_bytes(pdu[1:3], "big"), int.from_bytes(pdu[6:8], "big")


def _check_rtu_write(frame: bytes, unit_id: int) -> tuple[int, int]:
    if len(frame) != RTU_WRITE_REQUEST_LEN:
        raise GuardViolation("RTU write request must be exactly 11 bytes")
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise GuardViolation("RTU crc mismatch")
    return _decode_write_pdu(frame[0], frame[1:9], unit_id)


def _check_mbap_write(frame: bytes, unit_id: int) -> tuple[int, int]:
    if len(frame) != MBAP_WRITE_REQUEST_LEN:
        raise GuardViolation("MBAP write request must be exactly 15 bytes")
    if frame[2:4] != b"\x00\x00":
        raise GuardViolation("MBAP protocol id must be 0")
    if int.from_bytes(frame[4:6], "big") != len(frame) - 6:
        raise GuardViolation("MBAP length field mismatch")
    return _decode_write_pdu(frame[6], frame[7:], unit_id)


def _check_v5_write(frame: bytes, unit_id: int, logger_serial: int) -> tuple[int, int]:
    if len(frame) != WRITE_REQUEST_FRAME_LEN:
        raise GuardViolation(f"V5 write request must be exactly {WRITE_REQUEST_FRAME_LEN} bytes")
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
    return _check_rtu_write(frame[meta_end:-TRAILER_LEN], unit_id)
