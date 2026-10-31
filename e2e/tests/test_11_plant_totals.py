"""Plant totals on the app device against a real Home Assistant (0.2.7): two inverters in a
parallel pair (master and slave) give the plant sensors on the app device, the membership setting
removes and restores them, and the battery state comes from the master only. The calls are the ones
the Ingress UI makes."""

from __future__ import annotations

import pytest

from harness.devices import registers
from harness.fakes import FakeInverter
from harness.stack import ProcessApp, options
from tests.helpers import entity_id, quiesce_and_check, registry, state_of, until

Q = "e2eqqqq4"
SERIAL_M, SERIAL_S = "2404000001", "2404000002"
MASTER, SLAVE = 0x0003, 0x0001  # R336: bit0 parallel enabled, bit1 master
PICK = ["pv1_power", "battery_soc"]
APP = f"deye_{Q}_app"


def plant_entity(key: str) -> str:
    return f"sensor.{APP}_{key}"


@pytest.fixture(scope="module")
async def instance_q(running):
    master = FakeInverter("q_m", "mbap", regs=registers(SERIAL_M, soc=61, pv1=4200, role=MASTER))
    slave = FakeInverter("q_s", "mbap", regs=registers(SERIAL_S, soc=0, pv1=1000, role=SLAVE))
    app = ProcessApp("q", options("127.0.0.1", running.broker.port, [], [], instance_id=Q))
    try:
        await master.start()
        await slave.start()
        app.start()
        await app.wait_api(60)
        yield app, master, slave
        await quiesce_and_check(app, {"q_m": master, "q_s": slave})
    finally:
        try:
            await master.stop()
            await slave.stop()
        finally:
            app.cleanup()


async def test_two_inverters_create_the_plant_entities_on_the_app_device(running, instance_q):
    app, master, slave = instance_q
    ha = running.ha
    for name, fake in (("m", master), ("s", slave)):
        status, body = await app.plant_call(
            "POST",
            "api/plant/endpoints",
            {
                "id": f"gw_{name}",
                "transport": "modbus_tcp",
                "host": "127.0.0.1",
                "port": fake.port,
                "timeout_s": 2.0,
                "request_gap_ms": 20,
            },
        )
        assert status == 200, body
        status, body = await app.plant_call(
            "POST",
            "api/plant/inverters",
            {
                "id": f"q_{name}",
                "name": f"Deye {name.upper()}",
                "endpoint": f"gw_{name}",
                "unit_id": 1,
                "poll_fast_s": 2,
                "poll_energy_s": 10,
            },
        )
        assert status == 200, body
    status, plant = await app.api("GET", "api/plant")
    assert plant["totals"]["members"] == ["q_m", "q_s"] and plant["totals"]["active"] is True

    async def identified():
        invs = await app.inverters()
        return all(invs.get(i, {}).get("status", {}).get("serial") for i in ("q_m", "q_s"))

    await until(identified, "both inverters are identified", 90, 1)
    for inverter in ("q_m", "q_s"):
        effective = (await app.inverters())[inverter]["selection"]["effective"]
        status, body = await app.sensors(
            inverter,
            remove=[k for k in effective if k not in PICK],
            add=[k for k in PICK if k not in effective],
        )
        assert status == 200, body

    async def pv_total():
        got = await state_of(ha, plant_entity("plant_pv_power"))
        from tests.helpers import inverter_states

        seen = {i: inverter_states(await ha.states(), Q, i) for i in ("q_m", "q_s")}
        assert got == "5200.0", (got, seen)
        return True

    await until(pv_total, "the plant PV power is the sum of both inverters", 90, 1)
    registry_ids = {e["unique_id"] for e in await registry(ha)}
    for key in ("plant_pv_power", "plant_load_power", "plant_battery_soc", "plant_members"):
        assert f"{APP}_{key}" in registry_ids, key

    async def soc():
        return await state_of(ha, plant_entity("plant_battery_soc")) in ("61", "61.0")

    await until(soc, "the plant battery SOC is the master's, not an average", 60, 1)
    # the plant entities live on the app device, not on an inverter device
    assert entity_id(Q, "q_m", "plant_pv_power") not in await ha.states()


async def test_the_membership_removes_and_restores_the_plant_entities(running, instance_q):
    app, master, slave = instance_q
    ha = running.ha
    status, body = await app.plant_call("PUT", "api/plant/totals", {"members": ["q_m"]})
    assert status == 200 and body["changed"] is True, body

    async def gone():
        ids = {e["unique_id"] for e in await registry(ha)}
        return f"{APP}_plant_pv_power" not in ids

    await until(gone, "fewer than two members: the plant entities are removed", 90, 1)
    status, body = await app.plant_call("PUT", "api/plant/totals", {"members": ["q_m", "q_s"]})
    assert status == 200, body

    async def back():
        return await state_of(ha, plant_entity("plant_pv_power")) == "5200.0"

    await until(back, "both members again: the plant entities return with their values", 120, 1)
