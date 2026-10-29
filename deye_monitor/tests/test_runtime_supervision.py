"""Task supervision: an unexpected exception restarts one inverter with backoff (T7 hardening)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.device import InverterState, Snapshot
from deye_monitor.profiles.decode import Value
from tests.device_helpers import Recorder, device_regs, ep, inverter, rig

SN = "2401234567"


class Exploding(Recorder):
    """A sink that raises (an unexpected bug) for the first ``times`` state callbacks."""

    def __init__(self, times: int, only: str | None = None) -> None:
        super().__init__()
        self.times = times
        self.only = only
        self.raised = 0

    def on_state(self, inverter_id: str, snapshot: Snapshot, values: Mapping[str, Value]) -> None:
        if self.raised < self.times and self.only in (None, inverter_id):
            self.raised += 1
            raise RuntimeError("boom with a secret detail")
        super().on_state(inverter_id, snapshot, values)


def recorder_sleep(delays: list[float]) -> Any:
    async def sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0.01)  # let the loop breathe, do not really wait

    return sleep


async def wait_for(predicate: Any, limit: float = 3.0) -> None:
    for _ in range(int(limit / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_crashing_task_is_restarted_with_exponential_backoff(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    delays: list[float] = []
    sink = Exploding(times=3)
    caplog.set_level(logging.DEBUG)
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        sink=sink,
        runtime_kw={"restart_sleep": recorder_sleep(delays)},
    ) as r:
        await r.runtime.start()
        await wait_for(lambda: sink.states)  # it recovered after three crashes
        inv = r.inv("deye1")
        assert delays[:3] == [5.0, 10.0, 20.0]  # 5 s doubling, no jitter with rand() == 0
        assert inv.internal_errors == 3 and inv.last_internal_error == "RuntimeError"
        assert inv.diagnostics()["internal_errors"] == 3
        offline = [
            x
            for x in sink.availability
            if x[1] is InverterState.OFFLINE and x[2] == "internal_error"
        ]
        assert len(offline) >= 1
        assert len(sink.events("internal_error")) == 3
        await wait_for(lambda: inv.state is InverterState.RUNNING)  # identified again, not stuck
        assert "boom" not in caplog.text and "secret detail" not in caplog.text  # type only
        assert "boom" not in str(sink.diagnostics)


async def test_backoff_is_capped(tmp_path: Path) -> None:
    delays: list[float] = []
    sink = Exploding(times=5)
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        sink=sink,
        runtime_kw={
            "restart_sleep": recorder_sleep(delays),
            "restart_min_s": 100.0,
            "restart_max_s": 300.0,
        },
    ) as r:
        await r.runtime.start()
        await wait_for(lambda: sink.states)
        assert delays[:5] == [100.0, 200.0, 300.0, 300.0, 300.0]


async def test_jitter_is_applied_and_bounded(tmp_path: Path) -> None:
    delays: list[float] = []
    sink = Exploding(times=2)
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        sink=sink,
        runtime_kw={"restart_sleep": recorder_sleep(delays), "rand": lambda: 1.0},
    ) as r:
        await r.runtime.start()
        await wait_for(lambda: sink.states)
        assert delays[:2] == [5.0 * 1.25, 10.0 * 1.25]


async def test_one_inverter_crashing_does_not_touch_the_others(tmp_path: Path) -> None:
    delays: list[float] = []
    sink = Exploding(times=2, only="deye2")
    endpoints = [ep("gw1", host="192.0.2.11"), ep("gw2", host="192.0.2.12")]
    inverters = [inverter("deye1", "gw1"), inverter("deye2", "gw2")]
    banks = {"gw1": device_regs("2401230001"), "gw2": device_regs("2401230002")}
    async with rig(
        endpoints,
        inverters,
        banks,
        tmp_path,
        sink=sink,
        runtime_kw={"restart_sleep": recorder_sleep(delays)},
    ) as r:
        await r.runtime.start()
        await wait_for(
            lambda: sink.values("deye1", "battery_soc") and sink.values("deye2", "battery_soc")
        )
        assert r.inv("deye1").internal_errors == 0
        assert r.inv("deye2").internal_errors == 2
        assert all(not t.done() for t in r.runtime._tasks)  # the application keeps running


async def test_shutdown_cancels_a_waiting_restart(tmp_path: Path) -> None:
    waiting = asyncio.Event()

    async def hang(delay: float) -> None:
        waiting.set()
        await asyncio.sleep(3600)

    sink = Exploding(times=100)
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        sink=sink,
        runtime_kw={"restart_sleep": hang},
    ) as r:
        await r.runtime.start()
        await asyncio.wait_for(waiting.wait(), 3)  # crashed and now waiting for the restart
        await asyncio.wait_for(r.runtime.stop(), 2)
        assert r.runtime._tasks == []
        assert (
            r.inv("deye1").state is InverterState.OFFLINE
            and r.inv("deye1").reason == "internal_error"
        )


async def test_a_long_healthy_run_resets_the_backoff(tmp_path: Path) -> None:
    delays: list[float] = []

    class Stub:
        id = "stub"
        state = InverterState.OFFLINE

        def __init__(self, clock: Any) -> None:
            self.clock = clock
            self.crashes = 0

        async def run(self) -> None:
            if self.crashes >= 3:
                await asyncio.sleep(3600)
            self.crashes += 1
            self.clock.offset += self.advance  # type: ignore[attr-defined]
            raise ValueError("x")

        def mark_internal_error(self, exc: BaseException) -> None:
            pass

    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        runtime_kw={"restart_sleep": recorder_sleep(delays)},
    ) as r:
        quick, stable = Stub(r.clock), Stub(r.clock)
        quick.advance, stable.advance = 0.0, 700.0  # type: ignore[attr-defined]
        for stub, expected in ((quick, [5.0, 10.0, 20.0]), (stable, [5.0, 5.0, 5.0])):
            delays.clear()
            task = asyncio.create_task(r.runtime._supervise(stub))  # type: ignore[arg-type]
            await wait_for(lambda: len(delays) >= 3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert delays[:3] == expected


async def test_normal_operation_is_not_disturbed(tmp_path: Path) -> None:
    delays: list[float] = []
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        runtime_kw={"restart_sleep": recorder_sleep(delays)},
    ) as r:
        await r.runtime.start()
        await wait_for(lambda: r.sink.states)
        assert delays == [] and r.inv("deye1").internal_errors == 0


# -- T7-07: immediate offline, fail-closed persistence, plant-wide behaviour ---------------------


async def test_a_crash_sets_offline_at_once_and_invalidates_the_epoch(tmp_path: Path) -> None:
    waiting = asyncio.Event()

    async def hang(delay: float) -> None:
        waiting.set()
        await asyncio.sleep(3600)

    sink = Exploding(times=100)
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        sink=sink,
        runtime_kw={"restart_sleep": hang},
    ) as r:
        inv = r.inv("deye1")
        epoch = inv.lifecycle_epoch
        await r.runtime.start()
        await asyncio.wait_for(waiting.wait(), 3)  # crashed, restart is still waiting
        assert sink.availability[-1] == ("deye1", InverterState.OFFLINE, "internal_error")
        assert inv.lifecycle_epoch > epoch  # results of the dead task can no longer be released
        assert inv.needs_identity
        assert sink.events("internal_error")[-1]["internal_errors"] == 1


async def test_a_blocked_inverter_waits_for_the_release_instead_of_looping(
    tmp_path: Path,
) -> None:
    delays: list[float] = []
    runs: list[int] = []

    class Stub:
        id = "stub"
        state = InverterState.BLOCKED  # a critical store is damaged: user action needed

        def __init__(self) -> None:
            self.released = asyncio.Event()

        async def run(self) -> None:
            runs.append(1)
            if len(runs) == 1:
                raise OSError("identity store unwritable")
            await asyncio.sleep(3600)

        def mark_internal_error(self, exc: BaseException) -> None:
            pass

    stub = Stub()
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        runtime_kw={"restart_sleep": recorder_sleep(delays)},
    ) as r:
        task = asyncio.create_task(r.runtime._supervise(stub))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)
        assert not task.done() and runs == [1] and delays == []  # waiting: no run, no sleep
        stub.state = InverterState.OFFLINE
        stub.released.set()  # the explicit reset
        await asyncio.sleep(0.05)
        assert runs == [1, 1] and delays == [] and not task.done()  # restarted exactly once
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_one_crashed_task_in_the_four_inverter_plant(tmp_path: Path) -> None:
    from tests.test_runtime_plant import plant

    endpoints, inverters, banks = plant()
    sink = Exploding(times=1000, only="deye2")
    delays: list[float] = []
    async with rig(
        endpoints,
        inverters,
        banks,
        tmp_path,
        sink=sink,
        runtime_kw={"restart_sleep": recorder_sleep(delays)},
    ) as r:
        await r.runtime.start()
        await wait_for(lambda: all(sink.values(f"deye{n}", "battery_soc") for n in (1, 3, 4)))
        assert r.inv("deye2").internal_errors >= 1 and not sink.values("deye2", "battery_soc")
        assert r.inv("deye2").state is InverterState.OFFLINE or r.inv("deye2").internal_errors
        for n in (1, 3, 4):
            assert r.inv(f"deye{n}").internal_errors == 0
            assert r.inv(f"deye{n}").state is InverterState.RUNNING
        await asyncio.wait_for(r.runtime.stop(), 3)
        assert r.runtime._tasks == []


async def test_all_tasks_crashing_still_shuts_down_cleanly(tmp_path: Path) -> None:
    from tests.test_runtime_plant import plant

    endpoints, inverters, banks = plant()
    sink = Exploding(times=10_000)
    delays: list[float] = []
    async with rig(
        endpoints,
        inverters,
        banks,
        tmp_path,
        sink=sink,
        runtime_kw={"restart_sleep": recorder_sleep(delays)},
    ) as r:
        await r.runtime.start()
        await wait_for(lambda: all(r.inv(f"deye{n}").internal_errors >= 1 for n in range(1, 5)))
        await asyncio.wait_for(r.runtime.stop(), 3)
        assert r.runtime._tasks == []
        assert not any(sink.values(f"deye{n}", "battery_soc") for n in range(1, 5))
