"""Guard (SPEC §1.2, §8.1, §10): the single gate for every byte sent to a device.

``guard_frame`` checks exactly one complete read frame: only FC03 passes, FC16 and FC06 never.
``guard_write_frame`` is the only write gate: exactly one FC16 with count 1 for a register of the
fixed table, carrying exactly the word a single-use ``WriteGrant`` names (foreign bits
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
    MBAP_COMMAND_REQUEST_LEN,
    MBAP_REQUEST_LEN,
    MBAP_WRITE_REQUEST_LEN,
    RTU_COMMAND_REQUEST_LEN,
    RTU_REQUEST_LEN,
    RTU_WRITE_REQUEST_LEN,
    ReadRequest,
    crc16,
)
from deye_monitor.protocol.solarman_v5 import (
    COMMAND_REQUEST_FRAME_LEN,
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

# The complete write whitelist: register -> the writable masks, one per contracted field (V105.8
# R/W, SPEC §10). A grant names exactly one of them. Nothing else can ever pass the write guard.
# Descriptors in ``write_ops.py`` must agree with this table.
_SINGLE: dict[int, int] = {
    108: 0xFFFF,  # maximum battery charge current
    109: 0xFFFF,  # maximum battery discharge current
    **{r: 0xFFFF for r in (99, 100, 101, 102, 103)},  # charge stage voltages, capacity, empty V
    **{r: 0xFFFF for r in (105, 106, 107)},  # equalization interval / duration, temp. coefficient
    112: 0x0001,  # battery activation (the word is exactly 0 or 1)
    113: 0xFFFF,  # battery internal resistance
    114: 0xFFFF,  # battery charge efficiency
    **{r: 0xFFFF for r in (118, 119, 120)},  # battery shutdown / restart / low voltage
    115: 0xFFFF,  # battery shutdown SOC
    116: 0xFFFF,  # battery restart SOC
    117: 0xFFFF,  # battery low SOC
    **{r: 0xFFFF for r in (121, 122, 123, 124)},  # generator run / cooling time, start V / SOC
    125: 0xFFFF,  # generator charge current
    127: 0xFFFF,  # GRID charge start SOC
    126: 0xFFFF,  # GRID charge start voltage
    128: 0xFFFF,  # GRID charge current
    129: 0x0001,  # generator charging enable (the word is exactly 0 or 1)
    130: 0x0001,  # GRID charge enable (the word is exactly 0 or 1, enforced by the descriptor)
    131: 0xFFFF,  # AC coupling upper frequency limit (range by the fresh R183)
    132: 0x0001,  # force Smart Load (the word is exactly 0 or 1)
    133: 0xFFFF,  # GEN port usage (exactly 0, 1 or 2)
    134: 0xFFFF,  # Smart Load OFF voltage
    135: 0xFFFF,  # Smart Load OFF SOC
    136: 0xFFFF,  # Smart Load ON voltage
    137: 0xFFFF,  # Smart Load ON SOC
    104: 0xFFFF,  # zero-export minimum limit (raw 20-500)
    140: 0x0003,  # external signal select (Off / Grid / Gen; bits 2-15 untouched)
    **{r: 0xFFFF for r in range(160, 166)},  # ToU slot target voltages (bounds by fresh R120/R101)
    141: 0xFFFF,  # energy pattern (exactly 0 Batt First or 1 Load First)
    142: 0xFFFF,  # export mode (exactly 0, 1 or 2)
    143: 0xFFFF,  # maximum power sold
    145: 0xFFFF,  # PV power sales enable (the word is exactly 0 or 1)
    **{148 + i: 0xFFFF for i in range(6)},  # ToU slot start times (HHMM)
    **{154 + i: 0xFFFF for i in range(6)},  # ToU slot discharge power
    **{166 + i: 0xFFFF for i in range(6)},  # ToU slot target SOC
    180: 0xFFFF,  # grid reconnection time (10-900 s, step 10)
    190: 0xFFFF,  # generator peak-shaving power
    191: 0xFFFF,  # GRID peak-shaving power
    237: 0xFFFF,  # active unbalanced load (the word is exactly 0 or 1)
    260: 0xFFFF,  # maximum EV charge power
    340: 0xFFFF,  # maximum PV power sold
    347: 0xFFFF,  # external CT ratio
    179: 0x0001,  # force off-grid (the word is exactly 0 or 1)
    181: 0xFFFF,  # AFCI enable (0 / 1) and the one-shot clear command 2 (see TRANSITIONS)
}
_MULTI: dict[int, tuple[int, ...]] = {
    # EV: off-grid SOC (bits 8-15), location (4-5), free work (0-1); bits 2-3 / 6-7 never owned
    259: (0xFF00, 0x0030, 0x0003),
    # R178 selective pairs (each 10 = off, 11 = on; 00 / 01 mean "no action" and are never held or
    # written): microinverter cutoff, GEN / GRID peak shaving, on-grid Smart Load, Signal Island
    # Mode, DRM; bits 14-15 are never owned
    178: (0x0003, 0x000C, 0x0030, 0x00C0, 0x0300, 0x0C00, 0x3000),
    # R341: MPPT scanning (bit 2), bypass of the minimum fan request (bit 4); bits 5-6 never
    341: (0x0004, 0x0010),
    # ToU enable (bit 0), weekday allow mask (bits 1-7), Spain mode (bit 8); the rest untouched
    146: (0x0001, 0x00FE, 0x0100),
    # ToU slot flags: GRID charge (bit 0), generator charge (1), Spain bits 2 / 3 / 4; bit 5 (sell)
    # and the unknown bits are never owned
    **{172 + i: (0x0001, 0x0002, 0x0004, 0x0008, 0x0010) for i in range(6)},
}
WRITABLE_FIELDS: dict[int, frozenset[int]] = {
    **{r: frozenset({m}) for r, m in _SINGLE.items()},
    **{r: frozenset(m) for r, m in _MULTI.items()},
}
# The special two-word commands: register -> the fixed second word. FC16 start R, count 2, data
# [target, word]. A count-1 write of such a register is refused (it is not in ``WRITABLE_FIELDS``);
# nothing but these exact shapes ever carries a second word. R35 Low_Noise: the setter is reached
# only by [target, 100]; the target is exactly 0 or 1 (old and new).
COMMAND_WORDS: dict[int, int] = {35: 100}
# Registers whose whole word may change only along these (old, new) pairs: R181 AFCI is switched
# 0 <-> 1 and cleared by the command 1 -> 2; 0 -> 2 would enable it implicitly and is refused.
TRANSITIONS: dict[tuple[int, int], frozenset[tuple[int, int]]] = {
    (181, 0xFFFF): frozenset({(0, 1), (1, 0), (1, 2)}),
}
# Registers whose OLD and NEW word must carry a known signature in a masked part: R259 low byte
# DD / DE / ED / EE (the only encodings the EV flags exist in; an unknown one is never touched).
WORD_SIGNATURES: dict[int, tuple[int, frozenset[int]]] = {
    259: (0x00FF, frozenset({0xDD, 0xDE, 0xED, 0xEE})),
}
# Words that are switches as a whole: the guard refuses any old or new word other than 0 / 1.
WHOLE_WORD_BOOLS: frozenset[int] = frozenset({112, 129, 132, 145, 179, 237})
# Words in which 0xFFFF is the firmware's "unset" value: neither it nor a result of it may be sent.
SENTINEL_REGISTERS: frozenset[int] = frozenset(
    {104, 140, 141, 142, 143, 178, 180, 181, 190, 191, 340, 341, 347, 237, 259, 260}
)
# Registers that must never be written, listed explicitly on top of "not on the whitelist".
DENIED_REGISTERS: frozenset[int] = frozenset(
    {22, 23, 24, 80, 81, 91, 92, 98, 111, 147, 336} | set(range(1100, 1122))
)
# Registers whose masked field may only hold the listed values, before and after the write.
# Asymmetric: the INCUMBENT may be wider (display-only states), the TARGET only these values.
TARGET_DOMAINS: dict[tuple[int, int], frozenset[int]] = {
    (140, 0x0003): frozenset(
        {0, 1, 2}
    ),  # external signal: an incumbent "Both" (3) can be replaced, not set
}
FIELD_DOMAINS: dict[tuple[int, int], frozenset[int]] = {
    (133, 0xFFFF): frozenset({0, 1, 2}),  # GEN port usage: generator, Smart Load, AC coupling
    (141, 0xFFFF): frozenset({0, 1}),  # energy pattern: Batt First / Load First
    (142, 0xFFFF): frozenset({0, 1, 2}),  # export mode: sales, internal / external zero-export
    (259, 0x0003): frozenset({1, 2}),  # EV free work: off / on (0 and 3 are no states)
    (259, 0x0030): frozenset({1, 2}),  # EV location: GRID / LOAD
    (259, 0xFF00): frozenset(range(0, 101, 10)),  # EV off-grid SOC in steps of 10
    # every R178 pair: 10 = off, 11 = on; 00 / 01 mean no action
    **{
        (178, m): frozenset({2, 3})
        for m in (0x0003, 0x000C, 0x0030, 0x00C0, 0x0300, 0x0C00, 0x3000)
    },
}


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
    command_word: int | None = None  # the fixed second word of a special command (count 2)

    @property
    def count(self) -> int:
        return 1 if self.command_word is None else 2


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
    if grant.command_word is not None:
        _check_command_grant(grant)
        return
    if grant.register in DENIED_REGISTERS or grant.register not in WRITABLE_FIELDS:
        raise GuardViolation(f"register {grant.register} is not writable")
    if grant.mask not in WRITABLE_FIELDS[grant.register]:
        raise GuardViolation("mask is not one of the register's writable fields")
    for word in (grant.old_word, grant.new_word):
        if type(word) is not int or not 0 <= word <= 0xFFFF:
            raise GuardViolation("register word out of range")
    if (grant.new_word ^ grant.old_word) & ~grant.mask & 0xFFFF:
        raise GuardViolation("the write would change bits outside the writable mask")
    if grant.new_word == grant.old_word:
        raise GuardViolation("an unchanged word is not written")
    if grant.register in WHOLE_WORD_BOOLS and not {grant.old_word, grant.new_word} <= {0, 1}:
        raise GuardViolation("a whole-word switch is exactly 0 or 1")
    if grant.register in SENTINEL_REGISTERS and 0xFFFF in (grant.old_word, grant.new_word):
        raise GuardViolation("the placeholder word 0xFFFF is neither replaced nor produced")
    target = TARGET_DOMAINS.get((grant.register, grant.mask))
    if target is not None:
        shift = (grant.mask & -grant.mask).bit_length() - 1
        if (grant.new_word & grant.mask) >> shift not in target:
            raise GuardViolation("the target field is not a value this register may be set to")
    signature = WORD_SIGNATURES.get(grant.register)
    if signature is not None:
        sig_mask, known = signature
        for word in (grant.old_word, grant.new_word):
            if word & sig_mask not in known:
                raise GuardViolation("the word carries no known signature")
    transitions = TRANSITIONS.get((grant.register, grant.mask))
    if transitions is not None and (grant.old_word, grant.new_word) not in transitions:
        raise GuardViolation("this change of the word is not an allowed transition")
    domain = FIELD_DOMAINS.get((grant.register, grant.mask))
    if domain is not None:
        shift = (grant.mask & -grant.mask).bit_length() - 1
        for word in (grant.old_word, grant.new_word):
            if (word & grant.mask) >> shift not in domain:
                raise GuardViolation("the field is outside the values this register may hold")


def _check_command_grant(grant: WriteGrant) -> None:
    """The one exact special command: a registered register, its fixed second word, the whole word
    as the field, old and new exactly 0 / 1 and different."""
    if (
        grant.register in DENIED_REGISTERS
        or COMMAND_WORDS.get(grant.register) != grant.command_word
    ):
        raise GuardViolation("not a registered special command")
    if grant.mask != 0xFFFF:
        raise GuardViolation("a special command owns the whole word")
    for word in (grant.old_word, grant.new_word):
        if type(word) is not int or word not in (0, 1):
            raise GuardViolation("a special command target is exactly 0 or 1")
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
        start, words = _check_rtu_write(frame, grant.unit, grant.count)
    elif framing is Framing.MBAP:
        start, words = _check_mbap_write(frame, grant.unit, grant.count)
    else:
        if logger_serial is None or not 1 <= logger_serial <= 0xFFFFFFFF:
            raise GuardViolation("solarman_v5 requires the configured logger serial")
        if grant.unit != 1:
            raise GuardViolation("a Solarman V5 logger is written on its inner unit 1 only")
        start, words = _check_v5_write(frame, grant.unit, logger_serial, grant.count)
    if start != grant.register:
        raise GuardViolation(f"register {start} is not the granted register {grant.register}")
    expected = (
        (grant.new_word,) if grant.command_word is None else (grant.new_word, grant.command_word)
    )
    if words != expected:
        raise GuardViolation("the word differs from the granted word")
    return GuardedWrite(frame=frame, grant=grant)


def _decode_write_pdu(
    unit: int, pdu: bytes, expected_unit: int, count: int
) -> tuple[int, tuple[int, ...]]:
    if unit != expected_unit:
        raise GuardViolation(f"unit {unit} is not the configured target {expected_unit}")
    if len(pdu) != 6 + 2 * count:
        raise GuardViolation(f"write PDU must be exactly {6 + 2 * count} bytes")
    if pdu[0] != FC_WRITE_MULTIPLE:
        raise GuardViolation(f"function code 0x{pdu[0]:02x} not allowed for a write")
    if int.from_bytes(pdu[3:5], "big") != count or pdu[5] != 2 * count:
        raise GuardViolation(f"a write must be exactly {count} register(s) as granted")
    words = tuple(int.from_bytes(pdu[6 + 2 * i : 8 + 2 * i], "big") for i in range(count))
    return int.from_bytes(pdu[1:3], "big"), words


def _check_rtu_write(frame: bytes, unit_id: int, count: int) -> tuple[int, tuple[int, ...]]:
    if len(frame) != (RTU_WRITE_REQUEST_LEN if count == 1 else RTU_COMMAND_REQUEST_LEN):
        raise GuardViolation("RTU write request has the wrong length for the grant")
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise GuardViolation("RTU crc mismatch")
    return _decode_write_pdu(frame[0], frame[1:-2], unit_id, count)


def _check_mbap_write(frame: bytes, unit_id: int, count: int) -> tuple[int, tuple[int, ...]]:
    if len(frame) != (MBAP_WRITE_REQUEST_LEN if count == 1 else MBAP_COMMAND_REQUEST_LEN):
        raise GuardViolation("MBAP write request has the wrong length for the grant")
    if frame[2:4] != b"\x00\x00":
        raise GuardViolation("MBAP protocol id must be 0")
    if int.from_bytes(frame[4:6], "big") != len(frame) - 6:
        raise GuardViolation("MBAP length field mismatch")
    return _decode_write_pdu(frame[6], frame[7:], unit_id, count)


def _check_v5_write(
    frame: bytes, unit_id: int, logger_serial: int, count: int
) -> tuple[int, tuple[int, ...]]:
    expected_len = WRITE_REQUEST_FRAME_LEN if count == 1 else COMMAND_REQUEST_FRAME_LEN
    if len(frame) != expected_len:
        raise GuardViolation(f"V5 write request must be exactly {expected_len} bytes")
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
    return _check_rtu_write(frame[meta_end:-TRAILER_LEN], unit_id, count)
