"""RTU response reading shared by RTU-over-TCP and serial RTU."""

from __future__ import annotations

import asyncio

from deye_monitor.protocol.modbus import (
    FC_READ_HOLDING,
    FC_READ_HOLDING_EXC,
    FC_WRITE_MULTIPLE,
    FC_WRITE_MULTIPLE_EXC,
    ModbusFrameError,
    WriteAck,
    parse_rtu_response,
    parse_rtu_write_response,
)
from deye_monitor.transport.base import StreamTransport, read_exactly


async def rtu_exchange(
    transport: StreamTransport, reader: asyncio.StreamReader, unit: int, count: int
) -> list[int]:
    """Read one RTU response; the length is derived from the function code and byte count."""
    head = await read_exactly(reader, 2)
    if head[1] == FC_READ_HOLDING_EXC:
        frame = head + await read_exactly(reader, 3)
    elif head[1] == FC_READ_HOLDING:
        byte_count = await read_exactly(reader, 1)
        if byte_count[0] != 2 * count:
            raise ModbusFrameError("byte count mismatch")
        frame = head + byte_count + await read_exactly(reader, 2 * count + 2)
    else:
        raise ModbusFrameError(f"unexpected function code 0x{head[1]:02x}")
    return parse_rtu_response(frame, unit, count)


async def rtu_write_exchange(reader: asyncio.StreamReader, unit: int, start: int) -> WriteAck:
    """Read one RTU FC16 response: echo (8 bytes) or exception (5 bytes)."""
    head = await read_exactly(reader, 2)
    if head[1] == FC_WRITE_MULTIPLE_EXC:
        frame = head + await read_exactly(reader, 3)
    elif head[1] == FC_WRITE_MULTIPLE:
        frame = head + await read_exactly(reader, 6)
    else:
        raise ModbusFrameError(f"unexpected function code 0x{head[1]:02x}")
    return parse_rtu_write_response(frame, unit, start)
