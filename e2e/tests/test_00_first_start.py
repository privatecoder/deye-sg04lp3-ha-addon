"""First start: until a selection was saved the app reads only the mandatory identity/status and
bootstrap registers (wire assertion) and creates no inverter entities."""

from __future__ import annotations

import asyncio

from conftest import INSTANCE, SERIALS
from harness.stack import MessageLog
from tests.helpers import INV, inverter_states, ours, until

BASIC = [
    "serial_number",
    "operating_state",
    "fault_word_1",
    "fault_word_2",
    "fault_word_3",
    "fault_word_4",
]


def allowed_blocks() -> set[tuple[int, int]]:
    """The blocks of the read plan of an EMPTY selection (mandatory + bootstrap reads) plus the
    identity probe of the RTU resynchronisation (R3..R7, one register more after a failure)."""
    from deye_monitor.device import BOOTSTRAP_KEYS
    from deye_monitor.profiles.features import Features
    from deye_monitor.profiles.plan import build_read_plan
    from deye_monitor.profiles.schema import load_profile

    plan = build_read_plan(
        load_profile("sg04lp3_lv_v105_8"),
        frozenset(BOOTSTRAP_KEYS),
        Features(),
        max_read_registers=60,
        energy_auto=True,
    )
    return {(b.start, b.count) for b in plan.blocks} | {(3, 5), (3, 6)}


async def test_first_start_reads_only_the_basics_until_the_selection_is_saved(stack):
    app, ha = stack.app, stack.ha
    log = await MessageLog(stack.broker, f"deye_monitor/{INSTANCE}/#").start()
    try:
        app.start()
        await app.wait_api(60)

        async def identified():
            invs = await app.inverters()
            return all(invs[i]["status"]["serial"] == SERIALS[i] for i in INV)

        await until(identified, "all four inverters are identified", 90, 1)
        await asyncio.sleep(10)  # several status cycles of the basic plan
        invs = await app.inverters()
        for iid in INV:
            assert invs[iid]["status"]["awaiting_selection"] is True
            assert invs[iid]["selection"]["chosen"] is False
            assert invs[iid]["status"]["state"] == "running"
        marks = {name: len(f.wire.reads) for name, f in stack.fakes.items()}
        allowed = allowed_blocks()
        for name, fake in stack.fakes.items():
            blocks = {(r.start, r.count) for r in fake.wire.reads[: marks[name]]}
            assert blocks and blocks <= allowed, (name, sorted(blocks - allowed))
        # no inverter entity exists, only the diagnostics of the app device
        entries = await ours(ha, INSTANCE)
        assert entries and all(u.startswith(f"deye_{INSTANCE}_app_") for u in entries), sorted(
            entries
        )[:5]
        groups = {m[1].rsplit("/", 1)[-1] for m in log.messages}
        assert not groups & {"battery", "ac", "pv", "energy"}, groups  # no measurement group yet

        # the first save ("Basics only"): exactly these sensors, and only then the normal polling
        for iid in INV:
            effective = invs[iid]["selection"]["effective"]
            status, body = await app.sensors(iid, remove=[k for k in effective if k not in BASIC])
            assert status == 200, body
        saved = {name: len(f.wire.reads) for name, f in stack.fakes.items()}

        async def basics_present():
            states = await ha.states()
            return all(
                inverter_states(states, INSTANCE, i).get("serial_number") == SERIALS[i] for i in INV
            )

        await until(basics_present, "the six basic sensors appear after the save", 60, 1)
        got = inverter_states(await ha.states(), INSTANCE, "deye_tcp")
        assert set(got) == set(BASIC), sorted(got)
        assert not log.json_values("battery_soc"), (
            "a template value was published before the template was chosen"
        )
        assert all(m >= 0 for m in saved.values())

        # back to the full template for the remaining scenarios
        for iid in INV:
            status, body = await app.sensors(iid, restore_template=True)
            assert status == 200, body
    finally:
        await log.stop()
