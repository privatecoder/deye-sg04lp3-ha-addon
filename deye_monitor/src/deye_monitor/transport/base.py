"""Shared transport machinery: persistent stream, total deadline, failure cooldown, guarded sink.

``StreamTransport._write`` is the only place in the package that writes to a device stream. It
runs the production guard itself and sends exactly the bytes the guard returns (SPEC §1.2).

Failure model (SPEC §8.1, SR-11/SR-12):

* connect error, connection loss, malformed frame, request timeout and logger error are
  *failures*: they start an endpoint cooldown (exponential backoff, capped) that also applies to
  an existing connection. It is reset only after a successful exchange.
* a legitimate Modbus exception response is a successful exchange, not a failure.
* RTU has no transaction id. After any failure or cancellation an RTU transport is
  ``uncertain``: late answers of the failed request could still arrive and look like the answer
  to the next one. Recovery (all inside the held lock, bounded by ``resync_budget_s``):
  (1) wait at least ``rtu_late_window_s`` after the failed send and discard everything that
  arrives, (2) then require ``resync_quiet_ms`` of silence, (3) read an identity *probe* (FC03
  R3..R7, through the same sink guard) that ``identity_check`` must accept. Nothing read while
  ``uncertain`` is ever returned.

  ASSUMPTION (not a Deye guarantee): no device or gateway answers later than
  ``rtu_late_window_s`` after the request it belongs to. An answer that arrives after the window
  and after the probe, with the same unit and length and a valid CRC, cannot be told apart from
  a fresh one; RTU has no transaction id and none is invented. The window is an application
  limit that the operator can widen; the probe is an additional binding check, not the proof.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from deye_monitor.protocol.guard import Framing, guard_frame
from deye_monitor.protocol.modbus import ModbusExceptionResponse, ModbusFrameError
from deye_monitor.protocol.solarman_v5 import V5LoggerError

Connector = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
IdentityCheck = Callable[[list[int]], bool]

PROBE_START = 3  # R3..R7: inverter serial number (ASCII)
PROBE_COUNT = 5
MIN_READ_REGISTERS = PROBE_COUNT + 1  # the count-6 probe must always be possible
READ_CHUNK = 4096


class TransportError(Exception):
    """Base class for transport failures."""


class RequestTimeout(TransportError):
    """The total request deadline expired."""


class ConnectionLost(TransportError):
    """The peer closed the connection or the stream failed."""


class ConnectBackoff(TransportError):
    """The endpoint is in failure cooldown; nothing was sent."""


class ResyncFailed(TransportError):
    """The line could not be brought into a known state (noise, wrong or missing probe answer)."""


def default_identity_check(words: list[int]) -> bool:
    """Without a bound serial number: EXACTLY five words (ten characters) of printable ASCII.
    Missing characters are never legitimised by a partial syntactic check."""
    if len(words) != PROBE_COUNT:
        return False
    return all(0x20 <= b < 0x7F for w in words for b in (w >> 8, w & 0xFF))


class ReadTransport(Protocol):
    """Read-only device access. There is deliberately no other public send method."""

    @property
    def counters(self) -> TransportCounters: ...

    @property
    def connection_epoch(self) -> int:
        """Monotonic: +1 for every new connection (also the first one)."""
        ...

    async def read_holding(self, unit: int, start: int, count: int) -> list[int]: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class TransportCounters:
    requests: int = 0
    successes: int = 0
    timeouts: int = 0
    connect_failures: int = 0
    connection_lost: int = 0
    reconnects: int = 0
    frame_errors: int = 0
    modbus_exceptions: int = 0
    logger_errors: int = 0
    stale_frames: int = 0
    resyncs: int = 0
    resync_failures: int = 0
    cooldown_rejections: int = 0


@dataclass(frozen=True, slots=True)
class TransportSettings:
    """Application limits (SPEC §8.1 SR-11), not Deye limits."""

    timeout_s: float = 5.0
    max_read_registers: int = 60
    backoff_min_s: float = 2.0
    backoff_max_s: float = 60.0
    resync_quiet_ms: float = 200.0  # required silence before an uncertain RTU line is trusted
    resync_max_bytes: int = 8192  # more noise than this fails the resynchronisation
    # Assumed upper bound for a late RTU answer, counted from the failed send (default
    # max(1 s, 2 x timeout_s)). Documented assumption, see the module docstring.
    rtu_late_window_s: float | None = None
    # Total time for window + quiet + probe of one resynchronisation (default: window + quiet +
    # probe deadline + 1 s). Must cover at least window + quiet + probe deadline.
    resync_budget_s: float | None = None

    @property
    def late_window_s(self) -> float:
        if self.rtu_late_window_s is not None:
            return self.rtu_late_window_s
        return max(1.0, 2 * self.timeout_s)

    @property
    def budget_s(self) -> float:
        if self.resync_budget_s is not None:
            return self.resync_budget_s
        return self.late_window_s + self.resync_quiet_ms / 1000 + self.timeout_s + 1.0

    def __post_init__(self) -> None:
        for name in ("timeout_s", "backoff_min_s", "backoff_max_s", "resync_quiet_ms"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and > 0")
        for name in ("rtu_late_window_s", "resync_budget_s"):
            opt = getattr(self, name)
            if opt is not None and (not math.isfinite(float(opt)) or float(opt) <= 0):
                raise ValueError(f"{name} must be finite and > 0")
        if self.backoff_max_s < self.backoff_min_s:
            raise ValueError("backoff_max_s must be >= backoff_min_s")
        if not MIN_READ_REGISTERS <= self.max_read_registers <= 125:
            raise ValueError(
                f"max_read_registers must be {MIN_READ_REGISTERS}..125 "
                "(the RTU identity probe reads 5 or 6 registers and is never shortened)"
            )
        if self.resync_max_bytes < 1:
            raise ValueError("resync_max_bytes must be >= 1")
        needed = self.late_window_s + self.resync_quiet_ms / 1000 + self.timeout_s
        if self.budget_s < needed:
            raise ValueError("resync_budget_s must cover rtu_late_window_s + quiet + timeout_s")


@dataclass(slots=True)
class Backoff:
    """Exponential backoff with jitter: min*2**n (+0..25 %), capped at max."""

    minimum: float
    maximum: float
    rand: Callable[[], float] = random.random
    failures: int = 0

    def next_delay(self) -> float:
        delay = min(self.maximum, self.minimum * float(2 ** min(self.failures, 30)))
        self.failures += 1
        return min(self.maximum, delay * (1 + 0.25 * self.rand()))

    def reset(self) -> None:
        self.failures = 0


@dataclass(slots=True)
class StreamTransport(ABC):
    """Persistent stream transport with lazy connect and one request in flight."""

    connector: Connector
    framing: Framing
    settings: TransportSettings = field(default_factory=TransportSettings)
    clock: Callable[[], float] = time.monotonic
    rand: Callable[[], float] = random.random
    logger_serial: int | None = None
    identity_check: IdentityCheck = default_identity_check
    counters: TransportCounters = field(default_factory=TransportCounters)
    _reader: asyncio.StreamReader | None = None
    _writer: asyncio.StreamWriter | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _backoff: Backoff = field(init=False)
    _not_before: float = 0.0
    _ever_connected: bool = False
    _uncertain: bool = False
    _uncertain_count: int | None = None
    _sent_at: float = 0.0
    _late_until: float = 0.0
    # Unit of the request whose recovery is running: lets a shared ``identity_check`` find the
    # inverter (and its bound serial number) that is addressed on this endpoint.
    probe_unit: int | None = None
    connection_epoch: int = 0

    # Class-level policy, overridden by the concrete transports.
    needs_resync = False  # RTU framing: no transaction id, see module docstring
    abort_on_drop = False  # serial ports: abort instead of a flushing close
    drop_on_frame_error = True

    def __post_init__(self) -> None:
        self._backoff = Backoff(self.settings.backoff_min_s, self.settings.backoff_max_s, self.rand)

    # -- public ---------------------------------------------------------------------------

    async def read_holding(self, unit: int, start: int, count: int) -> list[int]:
        async with self._lock:
            self.counters.requests += 1
            try:
                self._check_cooldown()
                await self._recover(unit)
                async with asyncio.timeout(self.settings.timeout_s):
                    await self._ensure_connected()
                    result = await self._request(unit, start, count)
            except ConnectBackoff:
                raise
            except asyncio.CancelledError:
                self._interrupted(count)
                raise
            except ResyncFailed:
                self.counters.resync_failures += 1
                self._failed(count, drop=True)
                raise
            except TimeoutError:
                self.counters.timeouts += 1
                self._failed(count, drop=self._partial_response_pending())
                raise RequestTimeout("request deadline exceeded") from None
            except ConnectionLost:
                raise  # connect failure, already accounted for in _ensure_connected
            except (ConnectionError, OSError, EOFError) as exc:
                self.counters.connection_lost += 1
                self._failed(count, drop=True)
                raise ConnectionLost(str(exc) or type(exc).__name__) from exc
            except ModbusFrameError:
                self.counters.frame_errors += 1
                self._failed(count, drop=self.drop_on_frame_error)
                raise
            except V5LoggerError:
                self.counters.logger_errors += 1
                self._failed(count, drop=False)
                raise
            except ModbusExceptionResponse:
                self.counters.modbus_exceptions += 1
                self._exchange_ok()
                raise
            self.counters.successes += 1
            self._exchange_ok()
            return result

    async def close(self) -> None:
        writer = self._writer
        self._drop()
        if writer is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(1):
                    await writer.wait_closed()

    # -- to implement ---------------------------------------------------------------------

    @abstractmethod
    async def _request(self, unit: int, start: int, count: int) -> list[int]: ...

    def _partial_response_pending(self) -> bool:
        """After a timeout: has a response been started? Then the framing is unknown and the
        connection must be dropped (default: no, keep it)."""
        return False

    def _on_connection_dropped(self) -> None:
        """Hook called by every drop (EOF, error, cancellation, close): clear stream state."""
        return None

    # -- failure accounting (synchronous: nothing here can be cancelled half-way) ----------

    @property
    def cooldown_active(self) -> bool:
        return self.clock() < self._not_before

    def _check_cooldown(self) -> None:
        now = self.clock()
        if now < self._not_before:
            self.counters.cooldown_rejections += 1
            raise ConnectBackoff(f"endpoint in cooldown for {self._not_before - now:.1f}s")

    def _failed(self, count: int, *, drop: bool) -> None:
        self._not_before = self.clock() + self._backoff.next_delay()
        if self.needs_resync:
            self._mark_uncertain(count)
            drop = drop or self.drop_on_frame_error
        if drop:
            self._drop()

    def _interrupted(self, count: int) -> None:
        """Cancellation: the exchange state is unknown, never trust the stream afterwards."""
        if self.needs_resync:
            self._mark_uncertain(count)
        self._drop()

    def _mark_uncertain(self, count: int) -> None:
        self._uncertain = True
        self._uncertain_count = count
        # a late answer may arrive until the assumed window after the last send has passed
        self._late_until = max(self._late_until, self._sent_at + self.settings.late_window_s)

    def _exchange_ok(self) -> None:
        self._backoff.reset()
        self._not_before = 0.0

    # -- guarded sink ---------------------------------------------------------------------

    async def _write(self, frame: bytes, unit: int) -> None:
        """The single device write path.

        The production guard checks the complete frame against the target policy here, at the
        sink, and exactly the bytes it returns are written. A rejected frame sends nothing.
        """
        guarded = guard_frame(
            frame,
            self.framing,
            unit_id=unit,
            max_read_registers=self.settings.max_read_registers,
            logger_serial=self.logger_serial,
        )
        writer = self._writer
        if writer is None:
            raise ConnectionLost("not connected")
        self._sent_at = self.clock()
        writer.write(guarded.frame)
        await writer.drain()

    @property
    def _stream(self) -> asyncio.StreamReader:
        if self._reader is None:
            raise ConnectionLost("not connected")
        return self._reader

    # -- connection -----------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await self.connector()
        except OSError as exc:
            self.counters.connect_failures += 1
            self._not_before = self.clock() + self._backoff.next_delay()
            raise ConnectionLost(f"connect failed: {exc}") from exc
        self.connection_epoch += 1
        if self._ever_connected:
            self.counters.reconnects += 1
        self._ever_connected = True

    def _drop(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        self._on_connection_dropped()
        if writer is not None:
            with contextlib.suppress(Exception):
                if self.abort_on_drop:
                    writer.transport.abort()  # discard unsent output, never wait for a drain
                else:
                    writer.close()

    # -- RTU resynchronisation ------------------------------------------------------------

    async def _recover(self, unit: int) -> None:
        """Leave the ``uncertain`` state: late window, silence, identity probe (see module doc).

        Runs inside the lock held by ``read_holding`` and does not call it. Cancellation, EOF and
        errors leave the state ``uncertain`` (``read_holding`` drops the stream)."""
        if not self._uncertain:
            return
        self.counters.resyncs += 1
        try:
            async with asyncio.timeout(self.settings.budget_s):
                await self._ensure_connected()
                await self._drain()
                words = await self._probe(unit)
        except TimeoutError:
            raise ResyncFailed("resynchronisation exceeded its budget") from None
        # R8 (probe of 6 words) is a separate identity field: only R3..R7 are the serial number
        self.probe_unit = unit
        if not self.identity_check(words[:PROBE_COUNT]):
            raise ResyncFailed("resync probe answer is not the expected identity")
        self._uncertain = False
        self._uncertain_count = None

    async def _drain(self) -> None:
        """Discard input until the late window is over, then until the line was quiet."""
        quiet = self.settings.resync_quiet_ms / 1000
        total = 0
        while True:
            remaining = self._late_until - self.clock()
            try:
                async with asyncio.timeout(remaining if remaining > 0 else quiet):
                    data = await self._stream.read(READ_CHUNK)
            except TimeoutError:
                if remaining > 0:
                    continue  # window still open
                return
            if not data:
                raise EOFError("connection closed during resync")
            total += len(data)
            if total > self.settings.resync_max_bytes:
                raise ResyncFailed("too much input noise during resync")

    async def _probe(self, unit: int) -> list[int]:
        # The probe reads R3..R7 (serial number). When the failed request itself had count 5 the
        # probe uses count 6 (R3..R8) so that a late answer of the failed request can never have
        # the probe's length. R8 is catalog entry ``inverter:8`` ("Communication protocol
        # version", registers.json V105.8), a separate identity field and NOT a profile entry;
        # its word is read only to change the length and is never interpreted.
        count = PROBE_COUNT + 1 if self._uncertain_count == PROBE_COUNT else PROBE_COUNT
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                return await self._request(unit, PROBE_START, count)
        except TimeoutError:
            raise ResyncFailed("no answer to the resync probe") from None
        except ModbusFrameError as exc:
            raise ResyncFailed(f"malformed answer to the resync probe: {exc}") from None
        except ModbusExceptionResponse as exc:
            raise ResyncFailed(f"resync probe rejected by the device: {exc}") from None


async def read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    """Read exactly ``n`` bytes (n is bounded by the caller).

    EOF raises ``asyncio.IncompleteReadError`` (an ``EOFError``), which ``read_holding`` turns
    into ``ConnectionLost`` with connection teardown and cooldown.
    """
    return await reader.readexactly(n)
