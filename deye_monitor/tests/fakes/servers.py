"""Local asyncio fakes for Modbus TCP, RTU-over-TCP, Solarman V5 and a PTY serial peer.

Every received byte is recorded in ``raw`` so tests can assert what was on the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tty
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from deye_monitor.protocol.modbus import append_crc

Kind = Literal["mbap", "rtu", "v5"]
SERIAL = 0x12345678
# Serial number words served at R3..R8 so that the RTU resync probe sees printable ASCII.
SERIAL_REGS = {3: 0x3234, 4: 0x3031, 5: 0x3233, 6: 0x3435, 7: 0x3637, 8: 0x3839}


@dataclass(slots=True)
class Behavior:
    delay_s: float = 0.0
    dribble_gap_s: float | None = None  # send the reply one byte at a time
    silent: bool = False  # never reply
    close: bool = False  # close the connection instead of replying
    exception_code: int | None = None
    heartbeat: bool = False  # V5: heartbeat frames around the reply
    double_crc: bool = False  # V5: two extra zero bytes after the RTU frame
    garbage_before: bytes = b""
    split_at: int | None = None  # send this many reply bytes, pause, then the rest
    split_pause_s: float = 0.0
    drop_rest: bool = False  # with split_at: never send the rest of the reply
    close_after: bool = False  # close the connection after delivering (possibly partial) reply
    v5_frame_type: int = 0x02
    v5_status: int = 0x01
    v5_seq_high: int = 0  # high byte of the echoed V5 sequence (only the low byte is echoed)
    bad_inner_crc: bool = False  # V5: corrupt the inner RTU CRC
    stale_frame_first: bool = False  # MBAP: precede the reply by a complete frame of tid-1
    raw_reply: bytes | None = None  # send exactly these bytes instead of a computed reply
    write_applied: bool = True  # FC16: the register store takes the word (also without a reply)


@dataclass(slots=True)
class Request:
    unit: int
    start: int
    count: int
    tid: int = 0
    seq: int = 0
    serial: int = 0
    fc: int = 3
    value: int = 0  # FC16 (one register): the word
    fcount: int = 1  # FC16: the count field as sent


def value_of(addr: int) -> int:
    return (addr * 3 + 1) & 0xFFFF


def v5_frame(control: int, seq: int, serial: int, payload: bytes) -> bytes:
    body = (
        len(payload).to_bytes(2, "little")
        + control.to_bytes(2, "little")
        + seq.to_bytes(2, "little")
        + serial.to_bytes(4, "little")
        + payload
    )
    return b"\xa5" + body + bytes([sum(body) & 0xFF, 0x15])


@dataclass(slots=True)
class Responder:
    """Reply building and the shared wire record; parser state lives in per-connection
    ``FrameExtractor`` objects."""

    kind: Kind
    unit: int = 1
    logger_serial: int = SERIAL
    regs: dict[int, int] = field(default_factory=lambda: dict(SERIAL_REGS))
    any_unit: bool = False  # answer every unit id (a gateway in front of several inverters)
    fail_addresses: set[int] = field(default_factory=set)  # reads touching these get exception 2
    writes: list[Request] = field(default_factory=list)  # every FC16 that arrived
    write_exception: int | None = None  # FC16 answers with this exception (nothing is stored)
    other_fc: list[Request] = field(default_factory=list)  # anything that is neither FC03 nor FC16
    raw: bytearray = field(default_factory=bytearray)
    frames: list[bytes] = field(default_factory=list)

    def new_extractor(self) -> FrameExtractor:
        return FrameExtractor(self)

    def reply(self, req: Request, b: Behavior) -> bytes | None:
        unit_ok = req.unit == self.unit or self.any_unit
        if req.fc == 0x10:
            self.writes.append(req)
            if (
                unit_ok
                and b.write_applied
                and self.write_exception is None
                and b.exception_code is None
            ):
                self.regs[req.start] = req.value
        elif req.fc != 3:
            self.other_fc.append(req)
        if b.silent or b.close or not unit_ok:
            return None
        if b.raw_reply is not None:
            return b.raw_reply
        touched = self.fail_addresses & set(range(req.start, req.start + req.count))
        if req.fc == 0x10:
            code = self.write_exception if self.write_exception is not None else b.exception_code
            if code is not None:
                pdu = bytes([0x90, code])
            else:
                pdu = bytes([0x10]) + req.start.to_bytes(2, "big") + b"\x00\x01"
        elif touched and b.exception_code is None:
            b = Behavior(exception_code=2)
            pdu = bytes([0x83, b.exception_code or 2])
        elif b.exception_code is not None:
            pdu = bytes([0x83, b.exception_code])
        else:
            words = [self.regs.get(a, value_of(a)) for a in range(req.start, req.start + req.count)]
            pdu = bytes([3, 2 * len(words)]) + b"".join(w.to_bytes(2, "big") for w in words)
        if self.kind == "mbap":

            def mbap(tid: int) -> bytes:
                head = tid.to_bytes(2, "big") + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big")
                return head + bytes([req.unit]) + pdu

            stale = mbap((req.tid - 1) & 0xFFFF) if b.stale_frame_first else b""
            return stale + mbap(req.tid)
        rtu = append_crc(bytes([req.unit]) + pdu)
        if self.kind == "rtu":
            return b.garbage_before + rtu
        if b.double_crc:
            rtu += b"\x00\x00"
        if b.bad_inner_crc:
            rtu = rtu[:-1] + bytes([rtu[-1] ^ 0xFF])
        meta = bytes([b.v5_frame_type, b.v5_status]) + bytes(12)
        data = v5_frame(0x1510, req.seq | (b.v5_seq_high << 8), req.serial, meta + rtu)
        if b.heartbeat:
            hb = v5_frame(0x4710, 0x0001, req.serial, bytes(1))
            data = hb + data + hb
        return b.garbage_before + data


@dataclass(slots=True)
class FrameExtractor:
    """Request stream parser of ONE connection (an aborted partial request never leaks into
    the next connection)."""

    owner: Responder
    _buf: bytearray = field(default_factory=bytearray)

    @property
    def kind(self) -> Kind:
        return self.owner.kind

    def feed(self, data: bytes) -> list[tuple[bytes, Request]]:
        self.owner.raw.extend(data)
        self._buf.extend(data)
        out: list[tuple[bytes, Request]] = []
        while True:
            size = self._frame_size()
            if size is None or len(self._buf) < size:
                return out
            frame = bytes(self._buf[:size])
            del self._buf[:size]
            self.owner.frames.append(frame)
            out.append((frame, self._parse(frame)))

    def _frame_size(self) -> int | None:
        if self.kind == "rtu":
            if len(self._buf) < 2:
                return None
            return 11 if self._buf[1] == 0x10 else 8
        if self.kind == "mbap":
            return 6 + int.from_bytes(self._buf[4:6], "big") if len(self._buf) >= 6 else None
        return 13 + int.from_bytes(self._buf[1:3], "little") if len(self._buf) >= 3 else None

    @staticmethod
    def _rtu_request(rtu: bytes) -> Request:
        if rtu[1] == 0x10:
            return Request(
                rtu[0],
                int.from_bytes(rtu[2:4], "big"),
                1,
                fc=0x10,
                value=int.from_bytes(rtu[7:9], "big"),
                fcount=int.from_bytes(rtu[4:6], "big"),
            )
        return Request(
            rtu[0],
            int.from_bytes(rtu[2:4], "big"),
            int.from_bytes(rtu[4:6], "big"),
            fc=rtu[1],
        )

    def _parse(self, frame: bytes) -> Request:
        if self.kind == "rtu":
            return self._rtu_request(frame)
        if self.kind == "mbap":
            pdu = frame[7:]
            if pdu[0] == 0x10:
                req = self._rtu_request(frame[6:7] + pdu)  # same field layout after the unit
            else:
                req = Request(
                    frame[6],
                    int.from_bytes(frame[8:10], "big"),
                    int.from_bytes(frame[10:12], "big"),
                    fc=pdu[0],
                )
            req.tid = int.from_bytes(frame[0:2], "big")
            return req
        req = self._rtu_request(frame[26:-2])
        req.seq = int.from_bytes(frame[5:7], "little")
        req.serial = int.from_bytes(frame[7:11], "little")
        return req


async def deliver(write: Callable[[bytes], None], data: bytes, b: Behavior) -> None:
    if b.delay_s:
        await asyncio.sleep(b.delay_s)
    if b.split_at is not None:
        write(data[: b.split_at])
        await asyncio.sleep(b.split_pause_s)
        if b.drop_rest:
            return
        data = data[b.split_at :]
    if b.dribble_gap_s is None:
        write(data)
        return
    for i in range(len(data)):
        write(data[i : i + 1])
        await asyncio.sleep(b.dribble_gap_s)


class FakeServer:
    """TCP fake. ``script`` holds per-request behaviours (consumed in order), then ``default``."""

    def __init__(self, kind: Kind, **kw: object) -> None:
        self.responder = Responder(kind, **kw)  # type: ignore[arg-type]
        self.default = Behavior()
        self.script: list[Behavior] = []
        self.write_script: list[Behavior] = []  # behaviours of FC16 requests only (consumed first)
        self.on_connect = b""  # sent to every new connection (old downstream frames of a gateway)
        self.connections = 0
        self.active = 0
        self.max_active = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._writers: list[asyncio.StreamWriter] = []
        self._noise: asyncio.Task[None] | None = None

    @property
    def raw(self) -> bytearray:
        return self.responder.raw

    @property
    def frames(self) -> list[bytes]:
        return self.responder.frames

    def start_noise(self, interval_s: float, byte: bytes = b"\x55") -> None:
        """Send one noise byte to every client every ``interval_s`` until stopped."""

        async def run() -> None:
            while True:
                await asyncio.sleep(interval_s)
                for w in self._writers:
                    if not w.is_closing():
                        w.write(byte)

        self._noise = asyncio.ensure_future(run())

    def stop_noise(self) -> None:
        if self._noise is not None:
            self._noise.cancel()
            self._noise = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> FakeServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def stop(self) -> None:
        if self._noise is not None:
            self._noise.cancel()
        for w in self._writers:
            w.close()
        for t in list(self._tasks):
            t.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def kick(self) -> None:
        """Close every open client connection (simulates EOF)."""
        for w in self._writers:
            w.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        self._writers.append(writer)
        self.connections += 1
        extractor = self.responder.new_extractor()
        if self.on_connect:
            writer.write(self.on_connect)
        try:
            while data := await reader.read(4096):
                for _, req in extractor.feed(data):
                    if req.fc == 0x10 and self.write_script:
                        b = self.write_script.pop(0)
                    else:
                        b = self.script.pop(0) if self.script else self.default
                    if b.close:
                        writer.close()
                        return
                    reply = self.responder.reply(req, b)
                    if reply is not None:
                        self.active += 1
                        self.max_active = max(self.max_active, self.active)
                        try:
                            await deliver(writer.write, reply, b)
                            await writer.drain()
                        finally:
                            self.active -= 1
                        if b.close_after:
                            writer.close()
                            return
        except ConnectionError, asyncio.CancelledError:
            pass
        finally:
            writer.close()
            self._tasks.discard(task)


class FakePty:
    """PTY pair: ``path`` is the slave side for the transport, the master side is the fake."""

    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.responder = Responder("rtu")
        self._extractor = self.responder.new_extractor()
        self.default = Behavior()
        self.script: list[Behavior] = []
        self.write_script: list[Behavior] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._noise: asyncio.Task[None] | None = None

    def start_noise(self, interval_s: float, byte: bytes = b"\x55") -> None:
        async def run() -> None:
            while True:
                await asyncio.sleep(interval_s)
                self._write(byte)

        self._noise = asyncio.ensure_future(run())

    def stop_noise(self) -> None:
        if self._noise is not None:
            self._noise.cancel()
            self._noise = None

    @property
    def raw(self) -> bytearray:
        return self.responder.raw

    def start(self) -> None:
        asyncio.get_running_loop().add_reader(self.master, self._on_readable)

    def _write(self, data: bytes) -> None:
        with contextlib.suppress(OSError):
            os.write(self.master, data)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master, 4096)
        except OSError:
            data = b""
        for _, req in self._extractor.feed(data):
            if req.fc == 0x10 and self.write_script:
                b = self.write_script.pop(0)
            else:
                b = self.script.pop(0) if self.script else self.default
            reply = self.responder.reply(req, b)
            if reply is not None:
                task = asyncio.ensure_future(deliver(self._write, reply, b))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def send(self, data: bytes) -> None:
        self._write(data)

    def hang_up(self) -> None:
        """Close the master side (hot-unplug)."""
        if not self._closed:
            self._closed = True
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)

    def close(self) -> None:
        self.stop_noise()
        self.hang_up()
        for t in self._tasks:
            t.cancel()
        with contextlib.suppress(OSError):
            os.close(self.slave)
