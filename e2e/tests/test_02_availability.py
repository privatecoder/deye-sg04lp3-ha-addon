"""Availability and data-state matrix (T8 contract §9) against a real Home Assistant: every
property has its own assertion (unknown vs unavailable are told apart)."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime, timedelta

import pytest

from conftest import INSTANCE
from tests.helpers import INV, entity_id, inverter_states, until

pytestmark = pytest.mark.usefixtures("running")


async def inv_state(ha, inverter):
    return inverter_states(await ha.states(), INSTANCE, inverter)


async def all_have_data(ha) -> bool:
    states = await ha.states()
    return all(inverter_states(states, INSTANCE, i).get("battery_soc") == "55" for i in INV)


async def history(ha, eid: str, start: datetime, end: datetime) -> list[str]:
    """The states Home Assistant recorded for ``eid`` in [start, end]."""
    from urllib.parse import quote

    path = (
        f"/api/history/period/{quote(start.isoformat())}?end_time={quote(end.isoformat())}"
        f"&filter_entity_id={eid}&minimal_response&no_attributes"
    )
    data = await ha.get(path)
    return [s["state"] for s in (data[0] if data else [])]


async def test_a_stopped_inverter_makes_only_its_entities_unavailable(running):
    ha, fake = running.ha, running.fakes["deye_rtu"]
    await fake.set_down(True)

    async def gone():
        got = await inv_state(ha, "deye_rtu")
        return got and all(v == "unavailable" for v in got.values())

    await until(gone, "every entity of the stopped inverter is unavailable", 120, 2)
    for other in ("deye_tcp", "deye_v5", "deye_ser"):
        got = await inv_state(ha, other)
        assert got["battery_soc"] == "55", (other, got["battery_soc"])
    await fake.set_down(False)

    async def back():
        return (await inv_state(ha, "deye_rtu")).get("battery_soc") == "55"

    await until(back, "the inverter is available again after the device returns", 180, 2)


async def test_a_failing_group_is_unknown_never_zero_never_unavailable(running):
    """The group answers with a Modbus exception: the app publishes null at once (unknown); the
    entities stay unknown (they are not 'unavailable' and never 0); other groups stay valid."""
    ha, fake = running.ha, running.fakes["deye_tcp"]
    fake.fail_ranges.append((672, 680))
    pv = [f"{k}" for k in ("pv1_power", "pv1_voltage", "pv2_power", "pv2_current")]

    async def unknown():
        got = await inv_state(ha, "deye_tcp")
        return all(got[k] == "unknown" for k in pv)

    await until(
        unknown, "PV entities are 'unknown' (not unavailable) while their group fails", 60, 1
    )
    for _ in range(12):  # it stays that way: no 0, no unavailable, other groups untouched
        got = await inv_state(ha, "deye_tcp")
        assert all(got[k] == "unknown" for k in pv), {k: got[k] for k in pv}
        assert got["battery_soc"] == "55" and got["grid_voltage_a"] == "230.0"
        await asyncio.sleep(1)
    fake.fail_ranges.clear()

    async def back():
        return (await inv_state(ha, "deye_tcp")).get("pv1_power") == "4200"

    await until(back, "PV entities recover with the group", 60, 1)


async def test_the_same_entity_goes_number_unknown_real_zero_number(running):
    ha, fake = running.ha, running.fakes["deye_v5"]
    eid = entity_id(INSTANCE, "deye_v5", "pv1_power")
    t0 = datetime.now(UTC)
    assert (await inv_state(ha, "deye_v5"))["pv1_power"] == "4200"
    fake.fail_ranges.append((672, 680))

    async def unknown():
        return (await inv_state(ha, "deye_v5"))["pv1_power"] == "unknown"

    await until(unknown, "number -> unknown", 60, 1)
    await asyncio.sleep(6)  # several unknown cycles: a zero must never be invented
    t_unknown_end = datetime.now(UTC)
    fake.fail_ranges.clear()
    fake.regs[672] = 0

    async def zero():
        return (await inv_state(ha, "deye_v5"))["pv1_power"] == "0"

    await until(zero, "unknown -> a real 0 W", 60, 1)
    t_zero = datetime.now(UTC)
    fake.regs[672] = 4200

    async def number():
        return (await inv_state(ha, "deye_v5"))["pv1_power"] == "4200"

    await until(number, "0 -> number", 60, 1)
    await asyncio.sleep(1.5)  # the recorder commits every second
    during_unknown = await history(ha, eid, t0 + timedelta(seconds=1), t_unknown_end)
    assert "unknown" in during_unknown
    assert "0" not in during_unknown and "0.0" not in during_unknown, during_unknown
    after = await history(ha, eid, t_zero - timedelta(seconds=2), datetime.now(UTC))
    assert "0" in after, after  # the real zero is recorded as a value


async def test_binary_sensor_true_false_unknown_and_unknown_enum_code(running):
    ha, fake = running.ha, running.fakes["deye_tcp"]
    f13 = 4096  # mask of fault_f13 in R555
    eid = entity_id(INSTANCE, "deye_tcp", "fault_f13", "binary_sensor")

    async def binary(expect: str):
        return (await ha.states()).get(eid, {}).get("state") == expect

    fake.regs[555] = 0
    await until(lambda: binary("off"), "bit clear -> off", 60, 1)
    fake.regs[555] = f13
    await until(lambda: binary("on"), "bit set -> on", 60, 1)
    fake.fail_ranges.append((550, 560))  # the status group fails
    await until(lambda: binary("unknown"), "failing group -> unknown (not a stale on/off)", 60, 1)
    fake.fail_ranges.clear()
    fake.regs[555] = 0x0001
    await until(lambda: binary("off"), "recovered", 60, 1)
    # an enum code the profile does not know: the physical state is unknown, the old text must not stay
    fake.regs[500] = 99
    await until(lambda: _enum(ha, "unknown"), "unknown enum code -> unknown", 60, 1)
    fake.regs[500] = 2
    await until(lambda: _enum(ha, "normal"), "known code again", 60, 1)


async def _enum(ha, expect: str) -> bool:
    return (await inv_state(ha, "deye_tcp")).get("operating_state") == expect


async def test_missing_word_partner_makes_only_that_entity_unknown(running):
    ha, fake = running.ha, running.fakes["deye_ser"]
    fake.fail_ranges.append((690, 691))  # the high word of grid_selected_power_total

    async def partner():
        return (await inv_state(ha, "deye_ser"))["grid_selected_power_total"] == "unknown"

    await until(partner, "an s32 whose partner word cannot be read is unknown", 60, 1)
    for _ in range(6):
        got = await inv_state(ha, "deye_ser")
        assert got["grid_selected_power_total"] == "unknown"
        # independent entities of the same group and of other groups stay valid
        assert got["grid_voltage_a"] == "230.0" and got["inverter_power_total"] == "3200"
        assert got["pv1_power"] == "4200" and got["battery_soc"] == "55"
        await asyncio.sleep(1)
    fake.fail_ranges.clear()

    async def back():
        return (await inv_state(ha, "deye_ser")).get("grid_selected_power_total") == "-1500"

    await until(back, "the pair is complete again", 60, 1)


async def test_groups_expire_independently_after_three_intervals(running):
    """The whole app is frozen: the fast group (2 s interval) expires after about 6 s, the slow
    energy group (10 s interval) is still valid then. Nothing is invented when it resumes."""
    ha, app = running.ha, running.app
    assert app.proc is not None
    app.proc.send_signal(signal.SIGSTOP)
    try:

        async def fast_expired():
            return (await inv_state(ha, "deye_v5"))["battery_soc"] == "unavailable"

        await until(fast_expired, "the fast group expires", 40, 0.5)
        got = await inv_state(ha, "deye_v5")
        assert got["pv_energy_total_g0"] not in ("unavailable", "unknown"), got[
            "pv_energy_total_g0"
        ]
    finally:
        app.proc.send_signal(signal.SIGCONT)

    async def back():
        return await all_have_data(ha)

    await until(back, "everything is valid again after the app continues", 120, 1)


async def test_hard_kill_of_the_app_sets_everything_unavailable_and_restart_recovers(running):
    ha, app = running.ha, running.app
    app.kill()

    async def all_gone():
        states = await ha.states()
        mine = [s["state"] for e, s in states.items() if f"deye_{INSTANCE}_" in e]
        return mine and all(v == "unavailable" for v in mine)

    await until(all_gone, "the last will makes every entity of the instance unavailable", 60, 1)
    app.start()
    await app.wait_api(60)
    await until(lambda: all_have_data(ha), "all inverters are back after the restart", 120, 2)


async def test_orderly_stop_publishes_offline_and_restart_recovers(running):
    ha, app = running.ha, running.app
    await app.terminate()

    async def all_gone():
        states = await ha.states()
        mine = [s["state"] for e, s in states.items() if f"deye_{INSTANCE}_" in e]
        return mine and all(v == "unavailable" for v in mine)

    await until(all_gone, "an orderly stop publishes offline for every entity", 60, 1)
    app.start()
    await app.wait_api(60)
    await until(lambda: all_have_data(ha), "all inverters are back after the orderly stop", 120, 2)


async def test_broker_restart_without_persistence_rediscovers_and_recovers(running):
    ha = running.ha
    await running.broker.restart()
    await until(
        lambda: all_have_data(ha),
        "after a broker restart the app reconnects and publishes again",
        180,
        2,
    )
    docs = await running.broker.retained("homeassistant/device/#")
    assert any(INSTANCE in t for t in docs), "discovery is retained again after the broker restart"
