"""Serial RTU (8N1) transport using pyserial-asyncio-fast."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import serial_asyncio_fast

from deye_monitor.protocol.guard import Framing
from deye_monitor.protocol.modbus import build_rtu_request
from deye_monitor.transport.base import (
    IdentityCheck,
    StreamTransport,
    TransportSettings,
    default_identity_check,
)
from deye_monitor.transport.rtu_stream import rtu_exchange


@dataclass(slots=True)
class SerialRtuTransport(StreamTransport):
    """After a timeout, error or cancellation the line is ``uncertain``: before the next request
    the input is drained until quiet (bounded by a total budget and byte limit) and an identity
    probe must succeed (SPEC §8.1 SR-12, see ``base``). The port stays open on timeouts; a
    cancelled or failed resynchronisation drops it so the next attempt starts from scratch."""

    framing: Framing = Framing.RTU
    baudrate: int = 9600
    needs_resync = True
    drop_on_frame_error = False
    # A graceful close of the port flushes (tcdrain) in a worker thread, which never returns when
    # the peer does not read: the shutdown would wait for that thread. A dropped stream is
    # aborted instead (nothing of value is in flight: only read requests are ever written).
    abort_on_drop = True

    async def _request(self, unit: int, start: int, count: int) -> list[int]:
        await self._write(build_rtu_request(unit, start, count), unit)
        return await rtu_exchange(self, self._stream, unit, count)


def serial_rtu(
    device: str,
    baudrate: int = 9600,
    settings: TransportSettings | None = None,
    identity_check: IdentityCheck = default_identity_check,
) -> SerialRtuTransport:
    async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await serial_asyncio_fast.open_serial_connection(
            url=device, baudrate=baudrate, bytesize=8, parity="N", stopbits=1
        )

    return SerialRtuTransport(
        connector=connect,
        framing=Framing.RTU,
        settings=settings or TransportSettings(),
        identity_check=identity_check,
        baudrate=baudrate,
    )
