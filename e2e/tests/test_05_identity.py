"""Device replacement: a different serial number quarantines the inverter; nothing of the new
device is published under the old identity (broker messages are recorded without gaps from before
the swap, and Home Assistant's own history is checked, not a polled sample)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from conftest import INSTANCE
from harness.fakes import serial_words
from harness.stack import MessageLog
from tests.helpers import entity_id, inverter_states, until
from tests.test_02_availability import history

pytestmark = pytest.mark.usefixtures("running")


async def test_a_replaced_device_is_quarantined_and_publishes_nothing_under_the_old_serial(running):
    ha, app, fake = running.ha, running.app, running.fakes["deye_tcp"]
    original = {a: fake.regs[a] for a in serial_words("2401000001")}
    original_soc = fake.regs[588]
    log = await MessageLog(running.broker, f"deye_monitor/{INSTANCE}/deye_tcp/#").start()
    t0 = datetime.now(UTC)
    try:
        # a different device answers on the same address: other serial number, other values
        fake.regs.update(serial_words("2499999999"))
        fake.regs[588] = 99
        fake.drop_connections()  # the reconnect re-identifies

        async def quarantined():
            inv = (await app.inverters())["deye_tcp"]
            got = inverter_states(await ha.states(), INSTANCE, "deye_tcp")
            return inv["status"]["state"] == "quarantined" and all(v == "unavailable" for v in got.values())

        await until(quarantined, "the inverter is quarantined and its entities are unavailable", 150, 1)
        await asyncio.sleep(10)  # more cycles of the foreign device: still nothing
        other = inverter_states(await ha.states(), INSTANCE, "deye_v5")
        assert other["battery_soc"] == "55", "other inverters are unaffected"
        # complete record of everything published for this inverter since before the swap
        assert 99 not in log.json_values("battery_soc"), "a foreign value was published"
        assert not [v for v in log.json_values("serial_number") if v not in (None, "2401000001")]
        # Home Assistant itself never saw a foreign value
        seen = await history(ha, entity_id(INSTANCE, "deye_tcp", "battery_soc"), t0, datetime.now(UTC))
        assert "99" not in seen and seen, seen
    finally:
        await log.stop()
    # the original device is back: the quarantine ends by itself
    fake.regs.update(original)
    fake.regs[588] = original_soc

    async def recovered():
        return inverter_states(await ha.states(), INSTANCE, "deye_tcp").get("battery_soc") == "55"

    await until(recovered, "the inverter recovers when the bound serial number answers again", 180, 2)
