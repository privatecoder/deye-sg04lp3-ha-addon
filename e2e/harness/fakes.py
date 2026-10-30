"""Fake inverters for the E2E run: Modbus TCP, RTU over TCP, Solarman V5 and a PTY serial peer.

Deliberately independent of the app's own protocol code: every received byte is kept and
decomposed completely by a separate, strict parser (``decompose``). Anything that is not exactly
one valid FC03 read request (RTU CRC, MBAP header, V5 envelope with the embedded RTU frame) is a
*violation*; the acceptance fails on the first one. Replies carry the registers of ``regs``."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tty
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["mbap", "rtu", "v5", "serial"]
LOGGER_SERIAL = 2_700_000_001


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(data: bytes) -> bytes:
    return data + crc16(data).to_bytes(2, "little")


def serial_words(serial: str) -> dict[int, int]:
    assert len(serial) == 10
    return {3 + i: (ord(serial[2 * i]) << 8) | ord(serial[2 * i + 1]) for i in range(5)}


@dataclass(frozen=True)
class Read:
    """One decomposed, valid FC03 request."""

    kind: str
    unit: int
    start: int
    count: int
    tid: int = 0
    seq: int = 0
    serial: int = 0


@dataclass
class Wire:
    """The record of one fake: raw bytes, the frames they were cut into, the violations."""

    kind: Kind
    logger_serial: int = LOGGER_SERIAL
    raw: bytearray = field(default_factory=bytearray)
    reads: list[Read] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    frames_bytes: int = 0
    pending_bytes: int = 0  # bytes of incomplete frames left when a stream ended
    streams_closed: int = 0
    streams_opened: int = 0

    def stream_closed(self, buf: bytearray) -> None:
        """A stream ended: leftover bytes are a violation and are counted."""
        self.pending_bytes += len(buf)
        self.decompose(buf, closing=True)
        self.streams_closed += 1

    def assert_clean(self, *, name: str = "", expect_traffic: bool = True) -> None:
        """Every stream finished, every byte belongs to a complete valid frame, none is left over."""
        label = name or self.kind
        assert self.violations == [], (label, self.violations[:3])
        assert self.pending_bytes == 0, (label, "unparsed tail bytes", self.pending_bytes)
        assert self.streams_closed >= self.streams_opened, (label, "a stream is still open")
        assert self.frames_bytes == len(self.raw), (label, self.frames_bytes, len(self.raw))
        if expect_traffic:
            assert self.reads, (label, "no traffic at all")

    def decompose(self, buf: bytearray, *, closing: bool = False) -> list[Read]:
        """Cut ``buf`` into complete frames (consuming them). Incomplete tails stay in ``buf``;
        at ``closing`` a non-empty tail is a violation."""
        out: list[Read] = []
        while buf:
            parsed = self._one(bytes(buf))
            if parsed is None:
                break
            size, read = parsed
            if read is None:
                self.violations.append(f"{self.kind}: invalid frame {bytes(buf[:size]).hex()}")
            else:
                out.append(read)
                self.reads.append(read)
            self.frames_bytes += size
            del buf[:size]
        if closing and buf:
            self.violations.append(f"{self.kind}: truncated frame at close {bytes(buf).hex()}")
        return out

    def _one(self, b: bytes) -> tuple[int, Read | None] | None:
        if self.kind in ("rtu", "serial"):
            if len(b) < 8:
                return None
            return 8, self._rtu(b[:8])
        if self.kind == "mbap":
            if len(b) < 6:
                return None
            size = 6 + int.from_bytes(b[4:6], "big")
            if size > 6 + 253 or size < 7:
                return len(b), None  # an absurd length field: everything pending is garbage
            if len(b) < size:
                return None
            return size, self._mbap(b[:size])
        if len(b) < 3:
            return None
        size = 13 + int.from_bytes(b[1:3], "little")
        if size > 13 + 512:
            return len(b), None
        if len(b) < size:
            return None
        return size, self._v5(b[:size])

    @staticmethod
    def _fc03(unit: int, pdu: bytes) -> tuple[int, int] | None:
        if len(pdu) != 5 or pdu[0] != 0x03 or not 1 <= unit <= 247:
            return None
        start, count = int.from_bytes(pdu[1:3], "big"), int.from_bytes(pdu[3:5], "big")
        if not 1 <= count <= 125 or start + count > 0x10000:
            return None
        return start, count

    def _rtu(self, f: bytes) -> Read | None:
        if len(f) != 8 or crc16(f[:6]) != int.from_bytes(f[6:8], "little"):
            return None
        sc = self._fc03(f[0], f[1:6])
        return Read(self.kind, f[0], sc[0], sc[1]) if sc else None

    def _mbap(self, f: bytes) -> Read | None:
        if len(f) != 12 or f[2:4] != b"\x00\x00" or int.from_bytes(f[4:6], "big") != 6:
            return None
        sc = self._fc03(f[6], f[7:12])
        return Read("mbap", f[6], sc[0], sc[1], tid=int.from_bytes(f[0:2], "big")) if sc else None

    def _v5(self, f: bytes) -> Read | None:
        # A5 | len(2 LE)=23 | control 0x4510 (10 45) | seq(2 LE) | logger serial(4 LE)
        #    | 15 bytes metadata | RTU request (8) | checksum | 15
        if len(f) != 36 or f[0] != 0xA5 or f[-1] != 0x15:
            return None
        if int.from_bytes(f[1:3], "little") != 23 or f[3:5] != b"\x10\x45":
            return None
        if sum(f[1:-2]) & 0xFF != f[-2]:
            return None
        serial = int.from_bytes(f[7:11], "little")
        if serial != self.logger_serial:
            return None
        inner = self._rtu(f[26:34])
        if inner is None:
            return None
        return Read(
            "v5",
            inner.unit,
            inner.start,
            inner.count,
            seq=int.from_bytes(f[5:7], "little"),
            serial=serial,
        )


@dataclass
class Gate:
    """Holds ONE reply: the first read that touches [lo, hi) is received at once, its answer (built
    from the registers at that moment, with ``values`` laid over them) is only sent after
    ``release()``. The device loop of the app therefore has a read in flight."""

    lo: int
    hi: int
    values: dict[int, int] = field(default_factory=dict)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    used: bool = False

    def release(self) -> None:
        self.released.set()


@dataclass
class FakeInverter:
    """One fake device behind one transport."""

    name: str
    kind: Kind
    unit: int = 1
    regs: dict[int, int] = field(default_factory=dict)
    logger_serial: int = LOGGER_SERIAL
    fail_ranges: list[tuple[int, int]] = field(
        default_factory=list
    )  # reads touching these -> exception
    silent_ranges: list[tuple[int, int]] = field(
        default_factory=list
    )  # reads touching these -> no answer
    gates: list[Gate] = field(default_factory=list)
    wire: Wire = field(init=False)
    port: int = 0
    path: str | None = None  # PTY slave path (serial)
    _server: asyncio.AbstractServer | None = field(default=None, init=False, repr=False)
    _writers: set[asyncio.StreamWriter] = field(default_factory=set, init=False, repr=False)
    _master: int | None = field(default=None, init=False, repr=False)
    _pty_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    down: bool = False
    hits: dict[int, int] = field(default_factory=dict, repr=False)  # fail-range hits by address
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _slave: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.wire = Wire(self.kind, self.logger_serial)

    # -- replies -----------------------------------------------------------------------------

    def _reply(self, read: Read, overrides: dict[int, int] | None = None) -> bytes | None:
        if self.down or read.unit != self.unit:
            return None
        end = read.start + read.count
        if any(read.start < hi and lo < end for lo, hi in self.silent_ranges):
            return None  # the request is received and counted, nothing comes back (a timeout)
        if any(read.start < hi and lo < end for lo, hi in self.fail_ranges):
            for a in range(read.start, end):
                if any(lo <= a < hi for lo, hi in self.fail_ranges):
                    self.hits[a] = self.hits.get(a, 0) + 1
            pdu = bytes([0x83, 0x02])
        else:
            words = [
                (overrides or {}).get(a, self.regs.get(a, 0)) & 0xFFFF
                for a in range(read.start, end)
            ]
            pdu = bytes([3, 2 * len(words)]) + b"".join(w.to_bytes(2, "big") for w in words)
        if read.kind == "mbap":
            return (
                read.tid.to_bytes(2, "big")
                + b"\x00\x00"
                + (len(pdu) + 1).to_bytes(2, "big")
                + bytes([read.unit])
                + pdu
            )
        rtu = with_crc(bytes([read.unit]) + pdu)
        if read.kind in ("rtu", "serial"):
            return rtu
        payload = bytes([0x02, 0x01]) + bytes(12) + rtu
        body = (
            len(payload).to_bytes(2, "little")
            + b"\x10\x15"
            + read.seq.to_bytes(2, "little")
            + read.serial.to_bytes(4, "little")
            + payload
        )
        return b"\xa5" + body + bytes([sum(body) & 0xFF, 0x15])

    # -- TCP -----------------------------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        if self.kind == "serial":
            self._start_pty()
            return
        self._server = await asyncio.start_server(self._client, host, port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.down:
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self.wire.streams_opened += 1
        buf = bytearray()
        try:
            while data := await reader.read(4096):
                self.wire.raw.extend(data)
                buf.extend(data)
                for read in self.wire.decompose(buf):
                    if self.down:
                        continue
                    gate = self._gate_for(read)
                    reply = self._reply(read, gate.values if gate else None)
                    if gate is not None:
                        gate.used = True
                        gate.entered.set()
                        await gate.released.wait()  # the answer is held back, the read is in flight
                    if reply is not None:
                        writer.write(reply)
                        await writer.drain()
        except ConnectionError, asyncio.IncompleteReadError:
            pass
        finally:
            self.wire.stream_closed(buf)
            self._writers.discard(writer)
            writer.close()
            if task is not None:
                self._tasks.discard(task)

    def _gate_for(self, read: Read) -> Gate | None:
        end = read.start + read.count
        for gate in self.gates:
            if not gate.used and read.start < gate.hi and gate.lo < end:
                return gate
        return None

    def hold_next_read(self, lo: int, hi: int, values: dict[int, int]) -> Gate:
        gate = Gate(lo, hi, dict(values))
        self.gates.append(gate)
        return gate

    def drop_connections(self) -> None:
        for w in list(self._writers):
            w.close()

    async def set_down(self, down: bool) -> None:
        """A stopped device: new connections are closed at once, open ones are dropped."""
        self.down = down
        if down:
            self.drop_connections()

    async def stop(self) -> None:
        """Close everything and wait until every stream has been finalised (idempotent)."""
        if self._server is not None:
            self._server.close()
            self.drop_connections()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)
            pending = list(self._tasks)
            if pending:
                await asyncio.wait(pending, timeout=5)
            self._server = None
        if self._pty_task is not None:
            self._pty_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pty_task
            self._pty_task = None
        if self._master is not None:
            os.close(self._master)
            self._master = None
        if self._slave is not None:  # both ends of the pseudo terminal
            with contextlib.suppress(OSError):
                os.close(self._slave)
            self._slave = None

    # -- PTY serial ---------------------------------------------------------------------------

    def _start_pty(self) -> None:
        master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave  # kept open: the peer must exist for the app's whole life
        self._master = master
        os.set_blocking(master, False)
        self._pty_task = asyncio.get_running_loop().create_task(self._pty_loop(master))

    async def _pty_loop(self, master: int) -> None:
        loop = asyncio.get_running_loop()
        buf = bytearray()
        self.wire.streams_opened += 1
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        loop.add_reader(master, lambda: self._pty_read(master, queue))
        try:
            while True:
                data = await queue.get()
                self.wire.raw.extend(data)
                buf.extend(data)
                for read in self.wire.decompose(buf):
                    reply = self._reply(read)
                    if reply is not None:
                        os.write(master, reply)
        finally:
            loop.remove_reader(master)
            while not queue.empty():  # bytes that arrived before the end are still accounted for
                data = queue.get_nowait()
                self.wire.raw.extend(data)
                buf.extend(data)
                self.wire.decompose(buf)
            self.wire.stream_closed(buf)

    @staticmethod
    def _pty_read(master: int, queue: asyncio.Queue[bytes]) -> None:
        with contextlib.suppress(BlockingIOError, OSError):
            data = os.read(master, 4096)
            if data:
                queue.put_nowait(data)
