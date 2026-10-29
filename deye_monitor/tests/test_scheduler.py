import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from deye_monitor.scheduler import BusScheduler, QueueFull, canonical_bus_key, normalize_host


def test_canonical_keys() -> None:
    assert canonical_bus_key(host="GW1.Example.", port=502) == "tcp:gw1.example:502"
    assert canonical_bus_key(host="192.0.2.054".replace("054", "54"), port=502) == (
        "tcp:192.0.2.54:502"
    )
    a = canonical_bus_key(host="::1", port=502)
    b = canonical_bus_key(host="[0:0:0:0:0:0:0:1]", port=502)
    assert a == b == "tcp:[::1]:502"
    assert canonical_bus_key(host="x", port=502) != canonical_bus_key(host="x", port=8899)
    with pytest.raises(ValueError):
        canonical_bus_key(host="x")
    with pytest.raises(ValueError):
        canonical_bus_key(host="x", port=1, serial_device="/dev/x")


def test_canonical_serial_resolves_symlinks(tmp_path: Path) -> None:
    dev = tmp_path / "ttyUSB0"
    dev.touch()
    link = tmp_path / "by-id"
    os.symlink(dev, link)
    assert canonical_bus_key(serial_device=str(link)) == canonical_bus_key(serial_device=str(dev))


async def test_fifo_and_single_in_flight() -> None:
    sched = BusScheduler(gap_s=0)
    order: list[int] = []
    active = 0
    peak = 0

    async def job(i: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        order.append(i)
        active -= 1
        return i

    def make(i: int) -> Callable[[], Awaitable[int]]:
        return lambda: job(i)

    results = await asyncio.gather(*(sched.run(make(i)) for i in range(10)))
    assert results == order == list(range(10))
    assert peak == 1
    assert sched.pending == 0


async def test_gap_between_requests() -> None:
    now = 100.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    async def sleep(s: float) -> None:
        nonlocal now
        sleeps.append(s)
        now += s

    sched = BusScheduler(gap_s=0.05, sleep=sleep, clock=clock)

    async def job() -> None:
        nonlocal now
        now += 0.01

    for _ in range(3):
        await sched.run(job)
    assert sleeps == pytest.approx([0.05, 0.05])  # first request starts immediately


async def test_queue_full_does_not_block() -> None:
    sched = BusScheduler(gap_s=0, max_queue=2)
    gate = asyncio.Event()

    async def blocked() -> None:
        await gate.wait()

    t1 = asyncio.create_task(sched.run(blocked))
    t2 = asyncio.create_task(sched.run(blocked))
    await asyncio.sleep(0)
    with pytest.raises(QueueFull):
        await sched.run(blocked)
    gate.set()
    await asyncio.gather(t1, t2)
    assert sched.pending == 0
    await sched.run(blocked)


async def test_cancellation_waiting_and_running() -> None:
    sched = BusScheduler(gap_s=0, max_queue=4)
    gate = asyncio.Event()
    ran: list[str] = []

    async def blocked() -> None:
        await gate.wait()

    async def mark(name: str) -> None:
        ran.append(name)

    running = asyncio.create_task(sched.run(blocked))
    waiting = asyncio.create_task(sched.run(lambda: mark("waiting")))
    later = asyncio.create_task(sched.run(lambda: mark("later")))
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.sleep(0)
    running.cancel()
    await asyncio.gather(running, waiting, return_exceptions=True)
    await later
    assert ran == ["later"]
    assert sched.pending == 0


async def test_exception_propagates_and_releases() -> None:
    sched = BusScheduler(gap_s=0)

    async def boom() -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await sched.run(boom)
    assert await sched.run(lambda: asyncio.sleep(0, "ok")) == "ok"
    assert sched.pending == 0


def test_invalid_scheduler_args() -> None:
    for kw in ({"gap_s": -1}, {"max_queue": 0}):
        with pytest.raises(ValueError):
            BusScheduler(**kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("Host.", "host"),
        ("[::1]", "::1"),
        ("0:0:0:0:0:0:0:1", "::1"),
        ("::FFFF:7f00:1", "127.0.0.1"),
        ("::ffff:192.0.2.1", "192.0.2.1"),
        ("2001:0DB8::1", "2001:db8::1"),
        ("192.0.2.1", "192.0.2.1"),
    ],
)
def test_normalize_host(raw: str, norm: str) -> None:
    assert normalize_host(raw) == norm
    assert normalize_host(norm) == norm  # idempotent
    assert canonical_bus_key(host=raw, port=502) == canonical_bus_key(host=norm, port=502)


@pytest.mark.parametrize("raw", ["", " ", "[::1", "::1]", "a b", "h/x", "u@h", "[]", "."])
def test_normalize_host_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_host(raw)


async def test_connector_uses_the_same_normal_form(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from deye_monitor.transport.modbus_tcp import tcp_connector

    seen: list[tuple[str, int]] = []

    async def fake_open(host: str, port: int) -> tuple[None, None]:
        seen.append((host, port))
        return None, None

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    await tcp_connector("[::FFFF:7F00:1]", 502)
    await tcp_connector("Gateway.Example.", 8899)
    assert seen == [("127.0.0.1", 502), ("gateway.example", 8899)]
