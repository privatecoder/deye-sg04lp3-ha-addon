"""MQTT intents on the REAL write service (review 23: R23-01/02/03): the service is blocked before
transmission (the plant write lock, the pacing sleep, the bus scheduler or the preparation reads)
while the world changes; the intent must end in a rejection with zero FC16 and an audit record,
never in a write."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.device import InverterState
from deye_monitor.writes import SettingsSnapshot, WriteService
from tests.device_helpers import serial_words
from tests.fakes.mqtt import settle
from tests.plant_helpers import PlantRig, plant_rig
from tests.test_mqtt_writes_plant import MASTER, SLAVE, Bridge, attach
from tests.write_helpers import audit_lines, build_plant, fast, fc16, svc_of

OTHER_SERIAL = "2411111111"


async def park(rig: PlantRig, bridge: Bridge, key: str = "battery_charge_current") -> WriteService:
    """Hold the plant write lock, send a command and wait until the service has accepted it and
    waits for the lock: nothing has been transmitted."""
    svc = svc_of(rig)
    await svc._lock.acquire()
    bridge.send(key, "40")
    await settle(lambda: len(svc._tasks) == 1)
    assert rig.servers["gw1"].responder.writes == []
    return svc


async def release(rig: PlantRig, svc: WriteService, bridge: Bridge) -> dict[str, Any]:
    svc._lock.release()
    await settle(lambda: not svc._tasks)
    ops = svc.operations()
    assert len(ops) == 1
    assert rig.servers["gw1"].responder.writes == [] and fc16(rig.servers["gw1"]) == []
    last = audit_lines(rig)[-1]["record"]
    assert last["status"] == "rejected" and last["reason"] == ops[0]["reason"]
    return ops[0]


async def rig_and_bridge(rig: PlantRig, **regs: int) -> None:
    await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r336": MASTER, **regs}})
    fast(rig)


async def test_a_disconnect_revokes_the_unsent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = await park(rig, bridge)
            bridge.conn.drop()  # the session ends: plane.stop(), then a new session
            await bridge.broker.wait_connections(2)
            op = await release(rig, svc, bridge)
            assert op["reason"] == "superseded_context" and op["source"] == "mqtt"


async def test_stopping_the_plane_revokes_the_unsent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = await park(rig, bridge)
            await bridge.plane.stop()
            bridge.plane.begin_session()
            op = await release(rig, svc, bridge)
            assert op["reason"] == "superseded_context"


async def test_an_exposure_withdrawal_revokes_the_unsent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = await park(rig, bridge)
            bridge.plane._apply("deye1", SettingsSnapshot("withdrawn", "not_released", {}))
            op = await release(rig, svc, bridge)
            assert op["reason"] == "superseded_context"


async def test_a_role_loss_stops_the_unsent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = await park(rig, bridge)
            rig.servers["gw1"].responder.regs[336] = SLAVE
            op = await release(rig, svc, bridge)
            assert op["reason"] == "parallel_slave"


async def test_a_role_change_that_is_still_writable_is_a_new_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = await park(rig, bridge)
            rig.servers["gw1"].responder.regs[336] = 0  # master -> not in a parallel system
            op = await release(rig, svc, bridge)
            assert op["reason"] == "superseded_context"


async def test_a_plant_generation_change_stops_the_unsent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = await park(rig, bridge)
            svc.plant_revision = lambda: 10_000
            op = await release(rig, svc, bridge)
            assert op["reason"] == "superseded_context"


@pytest.mark.parametrize("new_serial", [None, OTHER_SERIAL])
async def test_a_rebind_of_the_same_logical_inverter_cannot_receive_the_old_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_serial: str | None
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = await park(rig, bridge)
            runtime = rig.app.runtime
            assert runtime is not None
            inv = runtime.inverters["deye1"]
            before = inv.lifecycle_epoch
            if new_serial is not None:  # another physical unit behind the same id
                rig.servers["gw1"].responder.regs.update(serial_words(new_serial))
            assert runtime.rebind("deye1") is True
            assert inv.lifecycle_epoch > before
            await rig.wait_state("deye1", InverterState.RUNNING)  # identified again
            fast(rig)
            if new_serial is not None:
                assert runtime.book.bound("deye1") == new_serial
            op = await release(rig, svc, bridge)
            assert op["reason"] == "superseded_context"
            # the replacement keeps working for a NEW command (exposure refreshed)
            await bridge.refresh()


async def test_an_identity_reset_cannot_receive_the_old_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = await park(rig, bridge)
            runtime = rig.app.runtime
            assert runtime is not None
            runtime.reset_store("deye1", "identity")
            await rig.wait_state("deye1", InverterState.RUNNING)
            fast(rig)
            op = await release(rig, svc, bridge)
            assert op["reason"] == "superseded_context"


async def test_a_command_is_refused_at_receipt_while_the_unit_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            bridge.plane._plans["deye1"].reachable = False
            bridge.send("battery_charge_current", "40")
            await settle(lambda: len(bridge.events()) == 1)
            event = bridge.events()[0]
            assert (event["event_type"], event["reason"]) == ("write_rejected", "unavailable")
            assert svc_of(rig).operations() == [] and rig.servers["gw1"].responder.writes == []


# -- the 60 s deadline ends at the send point, whatever delayed the write ---------------------


class Clock:
    def __init__(self, svc: WriteService) -> None:
        self.now = svc.clock()
        svc.clock = self

    def __call__(self) -> float:
        return self.now


async def test_the_deadline_holds_over_a_wait_for_the_plant_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            clock = Clock(svc_of(rig))
            svc = await park(rig, bridge)
            clock.now += 61.0  # receipt at t=0, the lock is free at t=61
            op = await release(rig, svc, bridge)
            assert op["reason"] == "expired"
            assert bridge.events()[-1]["reason"] == "expired"


async def test_the_deadline_holds_over_a_slow_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = svc_of(rig)
            clock = Clock(svc)
            original = svc._peer_baselines

            async def slow(*args: Any, **kw: Any) -> Any:
                out = await original(*args, **kw)
                clock.now += 61.0  # the peer reads of the preparation took a minute
                return out

            monkeypatch.setattr(svc, "_peer_baselines", slow)
            bridge.send("battery_charge_current", "40")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["reason"] == "expired"
            assert rig.servers["gw1"].responder.writes == [] and audit_lines(rig) == []


async def test_the_deadline_holds_over_the_pacing_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = svc_of(rig)
            clock = Clock(svc)
            svc.settings = type(svc.settings)(
                observe_delay_s=0.0, pacing_s=5.0, permit_s=500.0, late_observe_delay_s=30.0
            )
            svc._last_send = clock.now  # a write has just been sent: the next one must wait

            async def sleep(delay: float) -> None:
                clock.now += 61.0  # the pacing wait ends a minute later

            svc.sleep = sleep
            bridge.send("battery_charge_current", "40")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["reason"] == "expired"
            assert rig.servers["gw1"].responder.writes == []


async def test_the_deadline_holds_over_a_wait_for_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig_and_bridge(rig)
        async with attach(rig) as bridge:
            svc = svc_of(rig)
            clock = Clock(svc)
            svc.settings = type(svc.settings)(
                observe_delay_s=0.0, pacing_s=0.0, permit_s=500.0, late_observe_delay_s=30.0
            )
            runtime = rig.app.runtime
            assert runtime is not None
            scheduler = runtime.inverters["deye1"].routes[0].scheduler
            await svc._lock.acquire()
            bridge.send("battery_charge_current", "40")
            await settle(lambda: len(svc._tasks) == 1)  # prepared, waits for the plant lock
            gate = asyncio.Event()
            holder = asyncio.create_task(scheduler.run(gate.wait))  # somebody else owns the bus
            await settle(lambda: scheduler.pending == 1)
            svc._lock.release()  # the write proceeds to the bus and waits there
            await settle(lambda: scheduler.pending == 2)
            clock.now += 61.0
            gate.set()
            await holder
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["reason"] == "expired"
            assert rig.servers["gw1"].responder.writes == []
            assert audit_lines(rig)[-1]["record"]["reason"] == "expired"
