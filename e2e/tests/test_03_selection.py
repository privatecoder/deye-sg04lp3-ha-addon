"""Selection changes through the ingress API against a real Home Assistant: online removal, cold
start, re-adding, and the offline-HA cleanup (HA stopped while sensors and energy series go)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from conftest import INSTANCE
from tests.helpers import INV, entity_id, inverter_states, ours, uid, until

pytestmark = pytest.mark.usefixtures("running")


async def in_registry(ha, inverter, key) -> bool:
    return uid(INSTANCE, inverter, key) in await ours(ha, INSTANCE)


async def state_value(ha, inverter, key):
    return inverter_states(await ha.states(), INSTANCE, inverter).get(key)


async def test_deselect_removes_the_entity_from_the_registry_and_neighbours_stay(running):
    ha, app = running.ha, running.app
    status, body = await app.sensors("deye_tcp", remove=["pv2_power"])
    assert status == 200, body

    async def gone():
        return not await in_registry(ha, "deye_tcp", "pv2_power")

    await until(gone, "pv2_power disappears from the Home Assistant entity registry", 60, 1)
    assert entity_id(INSTANCE, "deye_tcp", "pv2_power") not in await ha.states()
    assert await state_value(ha, "deye_tcp", "pv1_power") == "4200"  # a neighbour is untouched
    assert await in_registry(ha, "deye_v5", "pv2_power")  # other inverters are untouched


async def test_a_cold_start_of_home_assistant_keeps_it_removed_and_reselecting_brings_it_back(
    running,
):
    ha, app = running.ha, running.app
    await ha.stop()
    await ha.cold_start()

    async def settled():
        return (await state_value(ha, "deye_tcp", "pv1_power")) == "4200"

    started = datetime.now(UTC) - timedelta(seconds=ha.last_start_s + 5)
    await until(settled, "Home Assistant restarted and the app's entities are back", 180, 2)
    fresh = (await ha.states())[entity_id(INSTANCE, "deye_tcp", "pv1_power")]
    # a value after the birth message: published after the restart began, no cache replay
    assert datetime.fromisoformat(fresh["last_updated"]) >= started, fresh["last_updated"]
    assert not await in_registry(ha, "deye_tcp", "pv2_power"), (
        "the removed component must stay removed"
    )
    assert entity_id(INSTANCE, "deye_tcp", "pv2_power") not in await ha.states()

    status, body = await app.sensors("deye_tcp", add=["pv2_power"])
    assert status == 200, body

    async def back():
        return await state_value(ha, "deye_tcp", "pv2_power") == "1800"

    await until(back, "pv2_power appears again with its value", 60, 1)


async def drop_and_restore(running, barrier: str | None):
    """HA is stopped; with the app running several sensors and a whole energy series go. With a
    ``barrier`` the app blocks right after that persistence phase (proven) and is killed there;
    otherwise it is killed after the discovery sync. The app restarts, HA starts cold."""
    ha, app = running.ha, running.app
    victims = [
        "pv1_voltage",
        "pv1_current",
        "battery_energy_charge_today_g0",
        "battery_energy_charge_total_g0",
    ]
    keys = [
        "pv1_voltage",
        "pv1_current",
        "battery_energy_charge_today",
        "battery_energy_charge_total",
    ]
    await ha.stop()
    if barrier is not None:
        await app.arm_barrier(barrier)
    if barrier is None:
        status, body = await app.sensors("deye_v5", remove=keys)
        assert status == 200, body
    else:
        # the request may never be answered: the process blocks at the barrier
        request = asyncio.ensure_future(app.sensors("deye_v5", remove=keys))
        await app.barrier_reached(barrier)  # the exact phase, proven; the process is blocked there
        request.cancel()
        selection_files = [f for f in app.dir.glob("selection.*.json")]
        assert selection_files, "the selection is durable at every phase of the cleanup"
        if barrier == "ledger_written":
            ledger = (app.dir / "discovery.json").read_text()
            assert "battery_energy_charge_total" in ledger  # the retired series is in the inventory
    if barrier is None:
        await asyncio.sleep(3)  # after the discovery sync
    app.kill()
    app.disarm_barrier()
    app.start()
    await app.wait_api(60)
    status, body = await app.sensors("deye_rtu", remove=["grid_frequency"])
    assert status == 200, body
    await ha.cold_start()

    async def cleaned():
        entries = await ours(ha, INSTANCE)
        return (
            all(uid(INSTANCE, "deye_v5", k) not in entries for k in victims)
            and uid(INSTANCE, "deye_rtu", "grid_frequency") not in entries
        )

    await until(
        cleaned, "the offline removals reach the Home Assistant registry after a cold start", 240, 3
    )
    states = await ha.states()
    for k in victims:
        assert entity_id(INSTANCE, "deye_v5", k) not in states, k

    async def neighbours():
        got = inverter_states(await ha.states(), INSTANCE, "deye_v5")
        return got.get("pv1_power") == "4200" and got.get(
            "battery_energy_discharge_today_g0"
        ) not in (None, "unavailable", "unknown")

    await until(neighbours, "neighbours of the removed sensors keep working", 120, 2)
    assert await in_registry(ha, "deye_tcp", "battery_energy_charge_today_g0")
    # restore the selection for the following tests
    status, body = await app.sensors("deye_v5", reset=keys)
    assert status == 200, body
    status, body = await app.sensors("deye_rtu", reset=["grid_frequency"])
    assert status == 200, body

    async def restored():
        got = inverter_states(await ha.states(), INSTANCE, "deye_v5")
        return got.get("pv1_voltage") == "360.5" and got.get(
            "battery_energy_charge_total_g0"
        ) not in (None, "unavailable", "unknown")

    await until(restored, "the reselected sensors come back", 120, 2)


@pytest.mark.parametrize("barrier", [None, "selection_durable", "ledger_written"])
async def test_offline_home_assistant_cleanup(running, barrier):
    await drop_and_restore(running, barrier)
