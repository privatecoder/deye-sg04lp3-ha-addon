"""Solarman V5 over TCP. Only the guarded FC03 request is ever sent."""

from __future__ import annotations

from dataclasses import dataclass, field

from deye_monitor.protocol.guard import Framing
from deye_monitor.protocol.modbus import build_rtu_request
from deye_monitor.protocol.solarman_v5 import (
    MAX_FRAME_LEN,
    V5Counters,
    V5StreamParser,
    build_v5_request,
    decode_read_response,
)
from deye_monitor.transport.base import StreamTransport, TransportSettings
from deye_monitor.transport.modbus_tcp import tcp_connector


@dataclass(slots=True)
class SolarmanV5Transport(StreamTransport):
    """The sequence low byte rotates per request so late responses are rejected. Heartbeat and
    management frames from the logger are counted by the parser and never answered."""

    framing: Framing = Framing.SOLARMAN_V5
    drop_on_frame_error = False  # a bad inner RTU frame leaves the V5 stream intact
    v5: V5StreamParser = field(init=False)
    _seq: int = 0

    def __post_init__(self) -> None:
        StreamTransport.__post_init__(self)
        if self.logger_serial is None:
            raise ValueError("logger_serial is required")
        self.v5 = V5StreamParser(self.logger_serial)

    @property
    def v5_counters(self) -> V5Counters:
        return self.v5.counters

    async def _request(self, unit: int, start: int, count: int) -> list[int]:
        assert self.logger_serial is not None
        self._seq = (self._seq + 1) & 0xFF
        rtu = build_rtu_request(unit, start, count)
        await self._write(build_v5_request(self.logger_serial, self._seq, rtu), unit)
        while True:
            data = await self._stream.read(MAX_FRAME_LEN)
            if not data:
                raise EOFError("connection closed by peer")
            for response in self.v5.feed(data, self._seq):
                return decode_read_response(response, unit, count, self.v5.counters)

    def _partial_response_pending(self) -> bool:
        """An incomplete frame that outlives the request deadline is dropped with the
        connection instead of blocking later requests behind an old prefix."""
        return self.v5.buffered > 0

    def _on_connection_dropped(self) -> None:
        self.v5.reset()  # counters and the sequence counter survive


def solarman_v5_tcp(
    host: str, logger_serial: int, port: int = 8899, settings: TransportSettings | None = None
) -> SolarmanV5Transport:
    return SolarmanV5Transport(
        connector=lambda: tcp_connector(host, port),
        framing=Framing.SOLARMAN_V5,
        settings=settings or TransportSettings(),
        logger_serial=logger_serial,
    )
