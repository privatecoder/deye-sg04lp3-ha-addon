"""The target plant (4 gateways + 4 V5 loggers as fallback), shutdown, bus budgets, run loop."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from deye_monitor.device import InverterState
from tests.device_helpers import device_regs, ep, inverter, rig, state_of
from tests.fakes.servers import Behavior

SERIALS = {1: "2401230001", 2: "2401230002", 3: "2401230003", 4: "2401230004"}


def plant() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[int, int]]]:
    endpoints: list[dict[str, Any]] = []
    inverters: list[dict[str, Any]] = []
    banks: dict[str, dict[int, int]] = {}
    for n in range(1, 5):
        # logical addresses (documentation range); the rig connects the endpoints to local fakes
        endpoints += [
            ep(f"gw{n}", host=f"192.0.2.{10 + n}"),
            ep(f"logger{n}", "solarman_v5_tcp", host=f"192.0.2.{20 + n}"),
        ]
        inverters.append(
            inverter(f"deye{n}", f"gw{n}", fallback_endpoint=f"logger{n}")
        )  # all unit 1
        banks[f"gw{n}"] = device_regs(SERIALS[n], soc=10 * n)
        banks[f"logger{n}"] = device_regs(SERIALS[n], soc=10 * n + 5)
    return endpoints, inverters, banks


async def test_four_inverters_run_in_parallel_without_bus_collisions(tmp_path: Path) -> None:
    endpoints, inverters, banks = plant()
    async with rig(endpoints, inverters, banks, tmp_path) as r:
        assert len(r.runtime.schedulers) == 8  # one scheduler per canonical bus (host:port)
        for _ in range(3):
            await asyncio.gather(*(inv.step() for inv in r.runtime.inverters.values()))
            r.clock.offset += 11
        for n in range(1, 5):
            inv = r.inv(f"deye{n}")
            assert state_of(inv) is InverterState.RUNNING and inv.active == 0
            assert r.runtime.book.bound(f"deye{n}") == SERIALS[n]
            assert set(r.sink.values(f"deye{n}", "battery_soc")) == {10 * n}  # its own device only
            snaps = [s for i, s, _ in r.sink.states if i == f"deye{n}"]
            assert {s.endpoint_id for s in snaps} == {f"gw{n}"}
        for n in range(1, 5):  # no gateway ever saw two requests at once
            assert r.servers[f"gw{n}"].max_active == 1
            assert not r.servers[f"logger{n}"].frames  # fallbacks stay idle
        utilisation = r.runtime.bus_utilisation()
        busy = sorted(v for v in utilisation.values() if v > 0)
        assert len(busy) == 4 and busy[0] == busy[-1]  # four separate, equal budgets
        assert all(
            utilisation[bus] == 0
            for bus in utilisation
            if bus not in {r.cfg.endpoint(f"gw{n}").bus for n in range(1, 5)}
        )
        diag = r.inv("deye1").diagnostics()
        assert diag["bus_utilisation"] == busy[0]
        assert r.runtime.diagnostics()["bus_utilisation"] == utilisation


async def test_one_gateway_failing_does_not_disturb_the_others(tmp_path: Path) -> None:
    endpoints, inverters, banks = plant()
    async with rig(endpoints, inverters, banks, tmp_path) as r:
        await asyncio.gather(*(inv.step() for inv in r.runtime.inverters.values()))
        r.servers["gw2"].default = Behavior(silent=True)
        for _ in range(8):
            await asyncio.gather(*(inv.step() for inv in r.runtime.inverters.values()))
            r.clock.pass_cooldown()
        assert r.inv("deye2").active == 1 and state_of(r.inv("deye2")) is InverterState.RUNNING
        for n in (1, 3, 4):
            assert r.inv(f"deye{n}").active == 0
            assert state_of(r.inv(f"deye{n}")) is InverterState.RUNNING
        soc2 = set(r.sink.values("deye2", "battery_soc")) - {None}  # None = failure snapshots
        assert soc2 <= {20, 25}  # the gateway's and the logger's own value, nothing else
        assert 25 in soc2


async def test_shutdown_under_load_is_prompt_and_clean(tmp_path: Path) -> None:
    endpoints, inverters, banks = plant()
    async with rig(endpoints, inverters, banks, tmp_path) as r:
        for server in r.servers.values():
            server.default = Behavior(silent=True)  # every request hangs until its deadline
        await r.runtime.start()
        await asyncio.sleep(0.15)  # requests are in flight everywhere
        started = time.monotonic()
        await asyncio.wait_for(r.runtime.stop(), 3)
        assert time.monotonic() - started < 1.5
        assert r.runtime._tasks == []
        for transport in r.runtime.transports.values():
            assert getattr(transport, "_writer", None) is None


async def test_run_loop_publishes_and_stops(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SERIALS[1])}, tmp_path
    ) as r:
        await r.runtime.start()
        for _ in range(60):
            if any(s.group == "battery" for _, s, _ in r.sink.states):
                break
            await asyncio.sleep(0.05)
        assert any(s.group == "battery" for _, s, _ in r.sink.states)
        assert r.inv("deye1").state is InverterState.RUNNING
        await r.runtime.stop()
        count = len(r.sink.states)
        await asyncio.sleep(0.1)
        assert len(r.sink.states) == count  # nothing runs after stop


async def test_overloaded_bus_is_reported_and_served_fairly(tmp_path: Path) -> None:
    fast = {"poll_fast_s": 1, "poll_energy_s": 1, "poll_settings_s": 1}
    inverters = [
        inverter("deye1", "gw1", **fast),
        {**inverter("deye2", "gw1", **fast), "unit_id": 2},
    ]
    async with rig([ep("gw1")], inverters, {"gw1": device_regs(SERIALS[1])}, tmp_path) as r:
        r.servers["gw1"].responder.any_unit = True  # one gateway, two units
        util = r.runtime.bus_utilisation()
        assert max(util.values()) > 1  # two inverters with 1 s intervals cannot be sustained
        assert r.inv("deye1").diagnostics()["bus_utilisation"] > 1
        rounds = 4
        for _ in range(rounds):
            await asyncio.gather(r.inv("deye1").step(), r.inv("deye2").step())
            r.clock.offset += 7  # several intervals were missed
        # both inverters are served (FIFO on the shared bus), none is starved ...
        per_inverter = {i: [s for n, s, _ in r.sink.states if n == i] for i in ("deye1", "deye2")}
        assert all(len(snaps) >= rounds for snaps in per_inverter.values())
        assert abs(len(per_inverter["deye1"]) - len(per_inverter["deye2"])) <= 2
        # ... and missed ticks are merged instead of replayed
        for i in ("deye1", "deye2"):
            assert r.inv(i).coalesced_ticks > 0
            groups = [s.group for s in per_inverter[i] if s.group == "battery"]
            assert len(groups) <= rounds + 1  # one battery cycle per round at most
