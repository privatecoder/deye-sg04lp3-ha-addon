import asyncio

from deye_monitor.scheduler import BusScheduler
from deye_monitor.transport import ModbusTcpTransport, TransportSettings
from deye_monitor.transport.modbus_tcp import tcp_connector
from tests.fakes.servers import Behavior, FakeServer


async def _run(use_scheduler: bool) -> int:
    servers = [await FakeServer("mbap").start() for _ in range(2)]
    for s in servers:
        s.default = Behavior(delay_s=0.03)
    transports = [
        ModbusTcpTransport(
            connector=lambda s=s: tcp_connector("127.0.0.1", s.port),  # type: ignore[misc]
            settings=TransportSettings(timeout_s=1),
        )
        for s in servers
    ]
    inflight = 0
    peak = 0
    sched = BusScheduler(gap_s=0)

    async def call(t: ModbusTcpTransport) -> list[int]:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            return await t.read_holding(1, 0, 1)
        finally:
            inflight -= 1

    try:
        coros = [
            (sched.run(lambda t=t: call(t)) if use_scheduler else call(t))  # type: ignore[misc]
            for t in transports * 2
        ]
        await asyncio.gather(*coros)
    finally:
        for t in transports:
            await t.close()
        for s in servers:
            await s.stop()
    return peak


async def test_two_clients_on_one_bus_never_in_flight_together() -> None:
    assert await _run(use_scheduler=False) > 1  # control: without the scheduler they overlap
    assert await _run(use_scheduler=True) == 1
