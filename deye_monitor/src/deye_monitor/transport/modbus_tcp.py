"""Modbus TCP (MBAP) transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from deye_monitor.protocol.guard import Framing, WriteGrant
from deye_monitor.protocol.modbus import (
    MAX_READ_REGISTERS,
    ModbusFrameError,
    WriteAck,
    build_mbap_request,
    build_mbap_write,
    parse_mbap_response,
    parse_mbap_write_response,
)
from deye_monitor.scheduler import normalize_host
from deye_monitor.transport.base import READ_CHUNK, StreamTransport, TransportSettings

_MAX_MBAP_LENGTH = 1 + 2 + 1 + 2 * MAX_READ_REGISTERS  # unit + fc + bytecount + data
_HEADER_LEN = 7


async def tcp_connector(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the canonical form of ``host`` (the same one the bus key is built from)."""
    return await asyncio.open_connection(normalize_host(host), port)


@dataclass(slots=True)
class ModbusTcpTransport(StreamTransport):
    """MBAP over TCP. Responses are matched by transaction id; complete stale frames are
    dropped. Every timeout drops the connection: a half-read header (even bytes still in the
    stream buffer) means the framing boundary is unknown, so no "nothing read yet" exception is
    made."""

    framing: Framing = Framing.MBAP
    drop_on_frame_error = True
    _tid: int = 0
    _rx: bytearray = field(default_factory=bytearray)

    async def _request(self, unit: int, start: int, count: int) -> list[int]:
        self._tid = (self._tid + 1) & 0xFFFF
        tid = self._tid
        await self._write(build_mbap_request(tid, unit, start, count), unit)
        while True:
            frame = self._take_frame()
            if frame is None:
                data = await self._stream.read(READ_CHUNK)
                if not data:
                    raise EOFError("connection closed by peer")
                self._rx.extend(data)
                continue
            if int.from_bytes(frame[0:2], "big") != tid:
                self.counters.stale_frames += 1
                continue
            return parse_mbap_response(frame, tid, unit, count)

    async def _write_request(self, grant: WriteGrant) -> WriteAck:
        self._tid = (self._tid + 1) & 0xFFFF
        tid = self._tid
        frame = build_mbap_write(tid, grant.unit, grant.register, grant.new_word)
        await self._write_checked(frame, grant)
        while True:
            reply = self._take_frame()
            if reply is None:
                data = await self._stream.read(READ_CHUNK)
                if not data:
                    raise EOFError("connection closed by peer")
                self._rx.extend(data)
                continue
            if int.from_bytes(reply[0:2], "big") != tid:
                self.counters.stale_frames += 1
                continue
            return parse_mbap_write_response(reply, tid, grant.unit, grant.register)

    def _take_frame(self) -> bytes | None:
        rx = self._rx
        if len(rx) < _HEADER_LEN:
            return None
        length = int.from_bytes(rx[4:6], "big")
        if not 2 <= length <= _MAX_MBAP_LENGTH:
            raise ModbusFrameError(f"implausible MBAP length {length}")
        total = 6 + length
        if len(rx) < total:
            return None
        frame = bytes(rx[:total])
        del rx[:total]
        return frame

    def _partial_response_pending(self) -> bool:
        return True

    def _on_connection_dropped(self) -> None:
        self._rx.clear()


def modbus_tcp(
    host: str, port: int = 502, settings: TransportSettings | None = None
) -> ModbusTcpTransport:
    return ModbusTcpTransport(
        connector=lambda: tcp_connector(host, port),
        framing=Framing.MBAP,
        settings=settings or TransportSettings(),
    )
