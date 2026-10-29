"""Per-bus request scheduler: FIFO, one request in flight, gap between requests (SPEC §2)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import time
from collections.abc import Awaitable, Callable


class QueueFull(Exception):
    """The bus queue is full; the request was not enqueued."""


def normalize_host(host: str) -> str:
    """Canonical connect address: lower case, no trailing dot, IPv6 compressed without brackets,
    IPv4-mapped IPv6 reduced to IPv4. Host names are not resolved (DNS aliases of one device
    cannot be detected; use ``bus_id`` for a known shared bus)."""
    name = host.strip()
    if name.startswith("["):
        if not name.endswith("]"):
            raise ValueError("unbalanced brackets in host")
        name = name[1:-1]
    name = name.rstrip(".").lower()
    if not name or any(c.isspace() or c in "/@[]" for c in name):
        raise ValueError("must be a bare host name or IP address")
    with contextlib.suppress(ValueError):
        addr = ipaddress.ip_address(name)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(addr)
    return name


def canonical_bus_key(
    *, host: str | None = None, port: int | None = None, serial_device: str | None = None
) -> str:
    """Stable key for an endpoint: normalised host:port, or the resolved serial path."""
    if serial_device is not None:
        if host is not None or port is not None:
            raise ValueError("serial_device excludes host/port")
        return f"serial:{os.path.realpath(serial_device)}"
    if host is None or port is None:
        raise ValueError("host and port are required")
    name = normalize_host(host)
    return f"tcp:[{name}]:{port}" if ":" in name else f"tcp:{name}:{port}"


class BusScheduler:
    """Serialises requests on one bus. The queue counts waiting plus running requests."""

    def __init__(
        self,
        *,
        gap_s: float = 0.05,
        max_queue: int = 32,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if gap_s < 0 or max_queue < 1:
            raise ValueError("gap_s must be >= 0 and max_queue >= 1")
        self._gap_s = gap_s
        self._max_queue = max_queue
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()  # asyncio.Lock wakes waiters in FIFO order
        self._pending = 0
        self._last_end: float | None = None

    @property
    def gap_s(self) -> float:
        return self._gap_s

    @property
    def pending(self) -> int:
        return self._pending

    async def run[T](self, request: Callable[[], Awaitable[T]]) -> T:
        if self._pending >= self._max_queue:
            raise QueueFull(f"bus queue full ({self._max_queue})")
        self._pending += 1
        try:
            async with self._lock:
                if self._last_end is not None:
                    wait = self._gap_s - (self._clock() - self._last_end)
                    if wait > 0:
                        await self._sleep(wait)
                try:
                    return await request()
                finally:
                    self._last_end = self._clock()
        finally:
            self._pending -= 1
