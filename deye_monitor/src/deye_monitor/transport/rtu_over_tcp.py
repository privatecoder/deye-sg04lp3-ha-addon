"""RTU ADUs tunnelled over a plain TCP stream."""

from __future__ import annotations

from dataclasses import dataclass

from deye_monitor.protocol.guard import Framing, WriteGrant
from deye_monitor.protocol.modbus import WriteAck, build_rtu_request, build_rtu_write
from deye_monitor.transport.base import (
    IdentityCheck,
    StreamTransport,
    TransportSettings,
    default_identity_check,
)
from deye_monitor.transport.modbus_tcp import tcp_connector
from deye_monitor.transport.rtu_stream import rtu_exchange, rtu_write_exchange


@dataclass(slots=True)
class RtuOverTcpTransport(StreamTransport):
    """RTU has no transaction ids: after any failure the connection is rebuilt and the line is
    resynchronised with an identity probe before data is trusted again (see ``base``). No
    guarantee is claimed about how a gateway maps old RS485 answers to new TCP connections."""

    framing: Framing = Framing.RTU
    needs_resync = True
    drop_on_frame_error = True

    async def _request(self, unit: int, start: int, count: int) -> list[int]:
        await self._write(build_rtu_request(unit, start, count), unit)
        return await rtu_exchange(self, self._stream, unit, count)

    async def _write_request(self, grant: WriteGrant) -> WriteAck:
        frame = build_rtu_write(grant.unit, grant.register, grant.new_word)
        await self._write_checked(frame, grant)
        return await rtu_write_exchange(self._stream, grant.unit, grant.register)

    def _partial_response_pending(self) -> bool:
        return True  # RTU framing is lost after any timeout


def rtu_over_tcp(
    host: str,
    port: int = 502,
    settings: TransportSettings | None = None,
    identity_check: IdentityCheck = default_identity_check,
) -> RtuOverTcpTransport:
    return RtuOverTcpTransport(
        connector=lambda: tcp_connector(host, port),
        framing=Framing.RTU,
        settings=settings or TransportSettings(),
        identity_check=identity_check,
    )
