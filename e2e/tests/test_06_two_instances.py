"""Two app instances at one broker and one Home Assistant: disjoint identities, and changes at
instance A (deselect, generation switch, feature change) have no effect on instance B. Instance B
also carries an island inverter and configured parallel roles."""

from __future__ import annotations

import asyncio
import re
from contextlib import AsyncExitStack

import pytest

from conftest import INSTANCE
from harness.devices import registers
from harness.fakes import FakeInverter, serial_words
from harness.stack import MessageLog, ProcessApp, options
from tests.helpers import inverter_states, ours, quiesce_and_check, until

pytestmark = pytest.mark.usefixtures("running")

B = "e2ebbbb2"
BS = {"b_tcp": "2402000001", "b_rtu": "2402000002"}
MASTER_PHASE_B = 0x0103  # parallel enabled, master, phase B
SLAVE_PHASE_C = 0x0201  # parallel enabled, slave, phase C


@pytest.fixture(scope="module")
async def instance_b(running):
    async with AsyncExitStack() as ex:
        fakes = {
            "b_tcp": FakeInverter(
                "b_tcp", "mbap", regs=registers(BS["b_tcp"], soc=70, role=MASTER_PHASE_B)
            ),
            "b_rtu": FakeInverter(
                "b_rtu", "rtu", regs=registers(BS["b_rtu"], soc=71, role=SLAVE_PHASE_C)
            ),
        }
        for f in fakes.values():
            ex.push_async_callback(f.stop)
            await f.start()

        def endpoint(eid, kind, fake):
            return {
                "id": eid,
                "transport": kind,
                "host": "127.0.0.1",
                "port": fake.port,
                "timeout_s": 2.0,
                "request_gap_ms": 20,
            }

        endpoints = [
            endpoint("gw_a", "modbus_tcp", fakes["b_tcp"]),
            endpoint("gw_b", "rtu_over_tcp", fakes["b_rtu"]),
        ]
        inverters = [
            {
                "id": "b_tcp",
                "name": "b_tcp",
                "endpoint": "gw_a",
                "unit_id": 1,
                "poll_fast_s": 2,
                "poll_energy_s": 10,
            },
            {
                "id": "b_rtu",
                "name": "b_rtu",
                "endpoint": "gw_b",
                "unit_id": 1,
                "poll_fast_s": 2,
                "poll_energy_s": 10,
            },
        ]
        # the same default topic prefix as instance A on purpose: only the instance id separates them
        app = ProcessApp(
            "b", options("127.0.0.1", running.broker.port, endpoints, inverters, instance_id=B)
        )
        ex.callback(app.cleanup)
        app.start()
        await app.wait_api(60)
        await app.choose_all(["b_tcp", "b_rtu"])
        # b_tcp is an island inverter: no grid, load and inverter values instead
        status, body = await app.sensors(
            "b_tcp", features={"utility_grid": False, "grid_port_use": "unused"}
        )
        assert status == 200, body

        async def data():
            states = await running.ha.states()
            return all(
                inverter_states(states, B, i).get("battery_soc") == str(v)
                for i, v in (("b_tcp", 70), ("b_rtu", 71))
            )

        await until(data, "instance B delivers data", 120, 2)
        yield app, fakes
        await quiesce_and_check(app, fakes)


async def inverter_snapshot(running):
    """Everything B's inverters show in Home Assistant (energy included) and its retained documents."""
    states = await running.ha.states()
    mine = {
        e: s["state"]
        for e, s in states.items()
        if re.match(rf"^(sensor|binary_sensor)\.deye_{B}_b_(tcp|rtu)_", e)
    }
    registry = sorted(u for u in await ours(running.ha, B) if "_app_" not in u)
    docs = {t: p for t, p in (await running.broker.retained("homeassistant/#")).items() if B in t}
    return mine, registry, docs


async def test_island_inverter_and_configured_parallel_roles(running, instance_b):
    entries = await ours(running.ha, B)
    assert not [u for u in entries if u.endswith("b_tcp_grid_voltage_a")], (
        "an island inverter has no grid values"
    )
    assert [u for u in entries if u.endswith("b_rtu_grid_voltage_a")]
    assert [u for u in entries if u.endswith("b_tcp_load_power_total")]
    keys = ("parallel_enable", "parallel_master", "parallel_phase")

    async def roles():
        # a selection change makes the inverter identify again for a moment (entities unavailable)
        states = await running.ha.states()
        tcp, rtu = inverter_states(states, B, "b_tcp"), inverter_states(states, B, "b_rtu")
        got = (tuple(tcp.get(k) for k in keys), tuple(rtu.get(k) for k in keys))
        return got == ((("on", "on", "B")), ("on", "off", "C")) or None

    await until(
        roles, "the configured parallel roles are shown (master/phase B and slave/phase C)", 60, 1
    )


async def test_identities_are_disjoint(running, instance_b):
    topics = await running.broker.retained("#")
    for topic in topics:
        assert not (INSTANCE in topic and B in topic), topic
    assert [t for t in topics if INSTANCE in t] and [t for t in topics if B in t]
    assert not set(await ours(running.ha, INSTANCE)) & set(await ours(running.ha, B))
    logs = running.broker.container.logs(8000)
    assert len(set(re.findall(r"as (deye[\w\-]*)", logs))) >= 2
    devices = {t.split("/")[2] for t in topics if t.startswith("homeassistant/device/")}
    assert all(INSTANCE in d or B in d for d in devices), devices


async def test_changes_at_instance_a_do_not_touch_instance_b(running, instance_b):
    ha, app = running.ha, running.app
    before = await inverter_snapshot(running)
    assert before[1], "instance B has registered entities"
    assert any("_energy_" in e for e in before[0]), (
        "B's energy availability and values are compared, too"
    )
    log = await MessageLog(running.broker, f"deye_monitor/{B}/#").start()
    try:
        # A: deselect a sensor, change the feature set (explicit deviation stays), switch the generation
        status, body = await app.sensors("deye_v5", remove=["pv2_power"])
        assert status == 200, body
        status, body = await app.sensors(
            "deye_ser", features={"battery": False}, add=["battery_soc"]
        )
        assert status == 200, body
        await app.terminate()
        app.edit_plant("deye_rtu", energy_resolution="1")
        app.start()
        await app.wait_api(60)

        async def a_done():
            entries = await ours(ha, INSTANCE)
            got = inverter_states(await ha.states(), INSTANCE, "deye_ser")
            return (
                not any(u.endswith("deye_v5_pv2_power") for u in entries)
                and any(u.endswith("deye_rtu_battery_energy_charge_total_g1") for u in entries)
                and got.get("battery_soc") == "55"  # the explicit add survives the feature change
                and "battery_voltage" not in got  # the template dropped it
            )

        await until(
            a_done,
            "instance A shows the deselection, the new generation and the explicit deviation",
            180,
            2,
        )
        await asyncio.sleep(3)
    finally:
        await log.stop()
    after = await inverter_snapshot(running)
    assert after[0] == before[0], "no state of B changed"
    assert after[1] == before[1], "no registry entry of B appeared or vanished"
    assert after[2] == before[2], "no retained document of B was touched"
    # B never published a different energy series than the one it started with
    series = {
        m
        for m in (re.findall(r"_energy_[a-z_]+_(g\d+)", t) for _, t, _, _ in log.messages)
        for m in m
    }
    assert series <= {"g0"}


async def test_empty_selection_publishes_no_inverter_entities_and_restores(running, instance_b):
    app_b, _ = instance_b
    ha = running.ha
    effective = (await app_b.inverters())["b_rtu"]["selection"]["effective"]
    status, body = await app_b.sensors("b_rtu", remove=effective)
    assert status == 200, body

    async def empty():
        return not inverter_states(await ha.states(), B, "b_rtu")

    await until(empty, "an empty selection leaves no inverter entities", 90, 2)
    assert inverter_states(await ha.states(), B, "b_tcp").get("battery_soc") == "70"
    status, body = await app_b.sensors("b_rtu", restore_template=True)
    assert status == 200, body

    async def back():
        return inverter_states(await ha.states(), B, "b_rtu").get("battery_soc") == "71"

    await until(back, "restoring the template brings the entities back", 90, 2)


async def test_a_replaced_device_at_b_with_first_seen_binding_is_withdrawn(running, instance_b):
    app_b, fakes = instance_b
    fake = fakes["b_rtu"]
    original = {a: fake.regs[a] for a in serial_words(BS["b_rtu"])}
    log = await MessageLog(running.broker, f"deye_monitor/{B}/b_rtu/#").start()
    try:
        fake.regs.update(serial_words("2499999998"))
        fake.regs[588] = 99
        fake.drop_connections()

        async def withdrawn():
            status = (await app_b.inverters())["b_rtu"]["status"]
            got = inverter_states(await running.ha.states(), B, "b_rtu").get("battery_soc", "")
            # RTU framing needs a successful resynchronisation probe first, which fails for a foreign
            # serial number: offline or quarantined, either way without any data
            return status["state"] in ("quarantined", "offline") and got == "unavailable"

        await until(withdrawn, "a different serial number withdraws the inverter (no data)", 150, 1)
        await asyncio.sleep(8)
        assert 99 not in log.json_values("battery_soc"), "a foreign value was published"
    finally:
        await log.stop()
    fake.regs.update(original)
    fake.regs[588] = 71
