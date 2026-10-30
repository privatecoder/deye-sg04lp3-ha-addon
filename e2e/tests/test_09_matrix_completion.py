"""The four HA scenarios that completed the acceptance matrix (R8-06):

1. only the SLOW group is suppressed (timeouts on the energy addresses): it runs out in Home Assistant
   while the fast group stays valid without a gap;
2. the broker restarts WITH persistence together with a real Home Assistant: registry, availability and
   retained state are right afterwards and no old measurement is shown as fresh;
3. Home Assistant's MQTT integration is set up only AFTER the app published its first (non-retained)
   values: the slow identity value is fresh afterwards;
4. a read that is in flight across a selection change and across a lifecycle change (rebind) is
   released late: neither the broker record nor Home Assistant's history shows it."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime

import pytest

from harness.devices import registers
from harness.fakes import FakeInverter
from harness.ha import HomeAssistant
from harness.stack import Broker, MessageLog, ProcessApp, options
from tests.helpers import entity_id, inverter_states, ours, quiesce_and_check, until
from tests.test_02_availability import history

pytestmark = pytest.mark.usefixtures("running")


def endpoint(fake: FakeInverter, timeout_s: float = 2.0) -> dict:
    return {
        "id": "gw",
        "transport": "modbus_tcp",
        "host": "127.0.0.1",
        "port": fake.port,
        "timeout_s": timeout_s,
        "request_gap_ms": 20,
    }


def inverter(iid: str, **extra) -> dict:
    return {
        "id": iid,
        "name": iid,
        "endpoint": "gw",
        "unit_id": 1,
        "poll_fast_s": 2,
        "poll_energy_s": 10,
        **extra,
    }


async def valid(ha, instance: str, iid: str, key: str, expected: str) -> bool:
    return inverter_states(await ha.states(), instance, iid).get(key) == expected


# -- 1. only the slow group is suppressed ---------------------------------------------------------


@pytest.fixture(scope="module")
async def slow_instance(running):
    async with AsyncExitStack() as ex:
        fake = FakeInverter("slow", "mbap", regs=registers("2406000001", soc=52))
        ex.push_async_callback(fake.stop)
        await fake.start()
        app = ProcessApp(
            "slow",
            options(
                "127.0.0.1",
                running.broker.port,
                [endpoint(fake, 1.0)],
                [inverter("s_inv")],
                instance_id="e2eslow01",
            ),
        )
        ex.callback(app.cleanup)
        app.start()
        await app.wait_api(60)
        await app.choose_all(["s_inv"])
        await until(
            lambda: valid(running.ha, "e2eslow01", "s_inv", "battery_soc", "52"),
            "the slow-group instance delivers data",
            120,
            2,
        )
        yield app, fake
        await quiesce_and_check(app, {"slow": fake})


async def test_only_the_slow_group_runs_out_while_the_fast_group_stays_valid(
    running, slow_instance
):
    """The slow (energy) group answers every read with a Modbus exception: its entities become
    unknown and stay so, the fast group is valid at every sample, in the broker record and in Home
    Assistant's history. (A group that does not answer at all behaves differently, see below.)"""
    ha = running.ha
    _, fake = slow_instance
    inst = "e2eslow01"
    eid_fast = entity_id(inst, "s_inv", "battery_soc")
    eid_slow = entity_id(inst, "s_inv", "pv_energy_total_g0")
    assert (await ha.states())[eid_slow]["state"] not in ("unknown", "unavailable")
    log = await MessageLog(running.broker, f"deye_monitor/{inst}/s_inv/#").start()
    t0 = datetime.now(UTC)
    try:
        fake.fail_ranges.append((514, 536))  # energy addresses only

        async def slow_gone():
            return (await ha.states())[eid_slow]["state"] == "unknown"

        await until(slow_gone, "the energy entity becomes unknown", 90, 1)
        for _ in range(20):  # the fast group is valid at every sample, the slow one never again
            states = await ha.states()
            assert states[eid_fast]["state"] == "52", states[eid_fast]["state"]
            assert states[eid_slow]["state"] == "unknown", states[eid_slow]["state"]
            await asyncio.sleep(1)
        t1 = datetime.now(UTC)
        fake.fail_ranges.clear()

        async def slow_back():
            return (await ha.states())[eid_slow]["state"] not in ("unknown", "unavailable")

        await until(slow_back, "the energy entity recovers when the group answers again", 90, 1)
    finally:
        await log.stop()
    seen = await history(ha, eid_fast, t0, t1)
    assert seen and set(seen) == {"52"}, set(seen)  # Home Assistant's own history: never invalid
    times = [t for t, topic, _, _ in log.messages if topic.endswith("/battery")]
    assert times and max(b - a for a, b in zip(times, times[1:], strict=False)) < 6.0, (
        "a gap longer than expire_after"
    )
    assert fake.hits.get(514, 0) > 0  # the energy group was really asked and refused


async def test_a_group_that_does_not_answer_at_all_takes_the_whole_endpoint_down(
    running, slow_instance
):
    """Documented behaviour (finding of the E2E run): timeouts are accounted per endpoint. Three
    failed cycles put the inverter offline, so the fast group's entities go unavailable too; this is
    not a per-group expiry. The inverter recovers when the group answers again."""
    ha = running.ha
    _, fake = slow_instance
    inst = "e2eslow01"
    fake.silent_ranges.append((514, 536))

    async def everything_unavailable():
        got = inverter_states(await ha.states(), inst, "s_inv")
        return got and all(v == "unavailable" for v in got.values())

    await until(
        everything_unavailable,
        "an unanswered group takes the inverter offline (all entities)",
        120,
        1,
    )
    fake.silent_ranges.clear()
    await until(
        lambda: valid(ha, inst, "s_inv", "battery_soc", "52"),
        "recovery after the group answers again",
        180,
        2,
    )


# -- 2./3. delayed Home Assistant initialisation and a broker with persistence --------------------


@pytest.fixture(scope="module")
async def persistent_world(running):
    """Own persistent broker, own Home Assistant WITHOUT the MQTT integration, one app instance."""
    async with AsyncExitStack() as ex:
        broker = Broker(running.net.name, persistence=True)
        ex.callback(broker.remove)
        broker.start()
        await broker.wait_ready()
        ha = HomeAssistant(running.net.name)
        ex.callback(ha.remove)
        ex.push_async_callback(ha.close)
        ha.start()
        fake = FakeInverter("pers", "mbap", regs=registers("2407000001", soc=66))
        ex.push_async_callback(fake.stop)
        await fake.start()
        await ha.wait_ready()
        await ha.onboard()
        app = ProcessApp(
            "pers",
            options(
                "127.0.0.1",
                broker.port,
                [endpoint(fake)],
                [inverter("p_inv")],
                instance_id="e2epers01",
            ),
        )
        ex.callback(app.cleanup)
        app.start()
        await app.wait_api(60)
        yield broker, ha, fake, app
        await quiesce_and_check(app, {"pers": fake})


async def test_delayed_ha_initialisation_gets_fresh_values(persistent_world):
    broker, ha, fake, app = persistent_world
    inst = "e2epers01"

    async def identified():
        inv = (await app.inverters())["p_inv"]
        return inv if inv["status"]["serial"] == "2407000001" else None

    inv = await until(
        identified, "identified before Home Assistant's MQTT integration exists", 60, 1
    )
    keep = {"serial_number", "battery_soc"}
    status, body = await app.sensors(
        "p_inv", remove=[k for k in inv["selection"]["effective"] if k not in keep]
    )
    assert status == 200, body
    await asyncio.sleep(9)  # the first publish and the discovery-settle refresh have both happened
    before = len(fake.wire.reads)
    assert not await ours(ha, inst), "no entity can exist without the MQTT integration"
    await ha.setup_mqtt(broker.name)
    await until(
        lambda: valid(ha, inst, "p_inv", "serial_number", "2407000001"),
        "the slow identity value is fresh after the late initialisation",
        60,
        1,
    )
    await until(lambda: valid(ha, inst, "p_inv", "battery_soc", "66"), "the fast value, too", 60, 1)
    assert len(fake.wire.reads) > before  # fresh reads, not a cache


async def test_broker_with_persistence_and_home_assistant_restart_together(persistent_world):
    broker, ha, fake, app = persistent_world
    inst = "e2epers01"
    await until(
        lambda: valid(ha, inst, "p_inv", "battery_soc", "66"), "values before the restart", 60, 1
    )
    registry_before = sorted(await ours(ha, inst))
    docs_before = {t for t in await broker.retained("homeassistant/#") if inst in t}
    assert registry_before and docs_before
    # the app ends hard and the device changes its value while everything is down
    app.kill()
    await until(
        lambda: valid(ha, inst, "p_inv", "battery_soc", "unavailable"),
        "the will made the entities unavailable",
        60,
        1,
    )
    fake.regs[588] = 67
    t_restart = datetime.now(UTC)
    await ha.stop()
    await broker.restart()  # persistence: retained messages survive
    retained = await broker.retained("#")
    assert {t for t in retained if t.startswith("homeassistant/") and inst in t} >= docs_before
    assert retained[f"deye_monitor/{inst}/status"] == b"offline"
    states_topics = [
        t
        for t in retained
        if t.startswith(f"deye_monitor/{inst}/p_inv/") and not t.endswith("availability")
    ]
    assert states_topics == [], (
        states_topics
    )  # state topics are never retained: nothing old can come back
    await ha.cold_start()
    assert sorted(await ours(ha, inst)) == registry_before  # the registry survived

    async def restored_unavailable():
        got = inverter_states(await ha.states(), inst, "p_inv")
        return got if got and all(v == "unavailable" for v in got.values()) else None

    await until(
        restored_unavailable,
        "the entities are back from the persisted discovery, unavailable",
        90,
        1,
    )
    for _ in range(8):  # and they stay that way: never the old 66
        got = inverter_states(await ha.states(), inst, "p_inv")
        assert all(v == "unavailable" for v in got.values()), got
        await asyncio.sleep(1)
    app.start()
    await app.wait_api(60)
    await until(
        lambda: valid(ha, inst, "p_inv", "battery_soc", "67"),
        "fresh values after the app is back",
        90,
        1,
    )
    seen = await history(ha, entity_id(inst, "p_inv", "battery_soc"), t_restart, datetime.now(UTC))
    assert "66" not in seen, seen  # no old measurement was ever shown as fresh


# -- 4. an old in-flight read released late -------------------------------------------------------


@pytest.fixture(scope="module")
async def old_cycle_instance(running):
    async with AsyncExitStack() as ex:
        fake = FakeInverter("old", "mbap", regs=registers("2408000001", soc=52))
        ex.push_async_callback(fake.stop)
        await fake.start()
        # a long deadline: the held read must still be in flight when the selection/lifecycle changes
        app = ProcessApp(
            "old",
            options(
                "127.0.0.1",
                running.broker.port,
                [endpoint(fake, 60.0)],
                [inverter("o_inv")],
                instance_id="e2eold001",
            ),
        )
        ex.callback(app.cleanup)
        app.start()
        await app.wait_api(60)
        await app.choose_all(["o_inv"])
        await until(
            lambda: valid(running.ha, "e2eold001", "o_inv", "battery_soc", "52"),
            "the old-cycle instance delivers data",
            120,
            2,
        )
        yield app, fake
        await quiesce_and_check(app, {"old": fake})


async def test_a_late_result_across_a_selection_change_is_never_published(
    running, old_cycle_instance
):
    ha = running.ha
    app, fake = old_cycle_instance
    inst = "e2eold001"
    log = await MessageLog(running.broker, f"deye_monitor/{inst}/o_inv/#").start()
    t0 = datetime.now(UTC)
    try:
        gate = fake.hold_next_read(586, 593, {588: 77})  # the next battery read answers 77, late
        await asyncio.wait_for(gate.entered.wait(), 30)  # a read is in flight now
        status, body = await app.sensors("o_inv", remove=["battery_soc"])  # selection revision +1
        assert status == 200, body
        await asyncio.sleep(3)
        gate.release()  # the old result arrives
        await asyncio.sleep(10)
        assert (await app.inverters())["o_inv"]["status"]["state"] == "running"
    finally:
        await log.stop()
    assert 77 not in log.json_values("battery_soc"), "the stale value was published"
    seen = await history(ha, entity_id(inst, "o_inv", "battery_soc"), t0, datetime.now(UTC))
    assert "77" not in seen, seen
    status, body = await app.sensors("o_inv", reset=["battery_soc"])
    assert status == 200, body
    await until(
        lambda: valid(ha, inst, "o_inv", "battery_soc", "52"),
        "the sensor is back with the real value",
        60,
        1,
    )


async def test_a_late_result_across_a_lifecycle_change_is_never_published(
    running, old_cycle_instance
):
    ha = running.ha
    app, fake = old_cycle_instance
    inst = "e2eold001"
    log = await MessageLog(running.broker, f"deye_monitor/{inst}/o_inv/#").start()
    t0 = datetime.now(UTC)
    try:
        gate = fake.hold_next_read(586, 593, {588: 78})
        await asyncio.wait_for(gate.entered.wait(), 30)
        counter = await app.action_counter()
        status, body = await app.api(
            "POST", "api/inverters/o_inv/rebind", {"confirm": True}, revision=counter
        )
        assert status == 200, body  # the binding is gone, the lifecycle epoch changed
        await asyncio.sleep(3)
        gate.release()
        await asyncio.sleep(10)
    finally:
        await log.stop()
    assert 78 not in log.json_values("battery_soc"), "the stale value was published"
    seen = await history(ha, entity_id(inst, "o_inv", "battery_soc"), t0, datetime.now(UTC))
    assert "78" not in seen, seen
    await until(
        lambda: valid(ha, inst, "o_inv", "battery_soc", "52"),
        "the inverter identifies again and shows the real value",
        90,
        1,
    )
