"""First start (SPEC §9.3): an inverter whose selection was never saved reads only the mandatory
identity/status and bootstrap registers and publishes no sensor until the user chose."""

from __future__ import annotations

import json
from pathlib import Path

from deye_monitor.device import BOOTSTRAP_KEYS
from deye_monitor.profiles.plan import build_read_plan
from deye_monitor.selection import Edit, SelectionManager, SelectionStore
from tests.selection_helpers import newest_body, plant
from tests.test_selection import specs
from tests.test_webapi import client

# registers that only template sensors need (battery, AC, PV, energy, R65)
TEMPLATE_ONLY = {65, 514, 516, 587, 588, 598, 625, 638, 672, 673}


def manager_requiring_choice(directory: Path) -> SelectionManager:
    return SelectionManager(SelectionStore(directory, specs(), require_choice=True))


def test_a_fresh_installation_is_awaiting_and_activates_nothing(tmp_path: Path) -> None:
    m = manager_requiring_choice(tmp_path)
    for iid in ("deye1", "deye2"):
        sel = m.selection(iid)
        assert not sel.chosen and sel.active == frozenset()
        assert sel.effective == sel.template and sel.effective  # the template is only the proposal
    assert all(keys == frozenset() for _, keys, _ in m.initial().values())
    plan = m.plan("deye1")
    assert not (plan.addresses & TEMPLATE_ONLY)
    assert plan.addresses >= {500, 336, 111, 133, 189}  # mandatory identity/status and bootstrap
    assert not plan.reads_r65


def test_without_require_choice_nothing_changes(tmp_path: Path) -> None:
    m = SelectionManager(SelectionStore(tmp_path, specs()))
    assert (
        m.selection("deye1").chosen
        and m.selection("deye1").active == m.selection("deye1").effective
    )


async def test_any_change_and_the_plain_acceptance_choose_and_persist(tmp_path: Path) -> None:
    m = manager_requiring_choice(tmp_path)
    await m.change(0, {"deye1": Edit(choose=True)})  # accept the template as it is
    assert m.selection("deye1").chosen and not m.selection("deye2").chosen
    await m.change(1, {"deye2": Edit(remove=frozenset({"battery_soc"}))})
    assert m.selection("deye2").chosen
    doc = newest_body(tmp_path)
    assert (
        doc["inverters"]["deye1"]["chosen"] is True and doc["inverters"]["deye2"]["chosen"] is True
    )
    again = manager_requiring_choice(tmp_path)
    assert again.selection("deye1").chosen and again.selection("deye2").chosen


async def test_a_newly_added_inverter_awaits_while_existing_selections_stay(tmp_path: Path) -> None:
    m = manager_requiring_choice(tmp_path)
    await m.change(0, {"deye1": Edit(choose=True)})
    # a store written before the flag existed and containing only deye1: deye1 counts as chosen
    body = newest_body(tmp_path)
    del body["inverters"]["deye2"]
    del body["inverters"]["deye1"]["chosen"]
    for slot in tmp_path.glob("selection.*.json"):  # noqa: ASYNC240
        slot.unlink()
    (tmp_path / "selection.json").write_text(json.dumps({"schema_version": 1, "data": body}))
    legacy = manager_requiring_choice(tmp_path)
    assert legacy.selection("deye1").chosen is True  # existing selections count as confirmed
    assert legacy.selection("deye2").chosen is False  # the inverter without a record awaits


async def test_runtime_reads_only_the_basics_until_the_choice_and_then_the_template(
    tmp_path: Path,
) -> None:
    async with plant(tmp_path, require_choice=True) as p:
        inv = p.rig.inv("deye1")
        assert inv.awaiting_selection and inv.selection == frozenset()
        await inv.step()
        assert p.rig.sink.values("deye1", "battery_soc") == []  # no sensor value published
        plan_addresses = inv.current_plan().addresses
        assert not (plan_addresses & TEMPLATE_ONLY)
        expected = build_read_plan(
            inv.profile,
            frozenset(BOOTSTRAP_KEYS),
            inv.features,
            max_read_registers=inv.route.max_read_registers,
            energy_auto=True,
        ).addresses
        assert plan_addresses == expected
        await p.manager.change(0, {"deye1": Edit(choose=True)})
        assert (
            not inv.awaiting_selection and inv.selection == p.manager.selection("deye1").effective
        )
        assert "battery_soc" in inv.selection and not p.rig.inv("deye2").selection
        for _ in range(4):
            p.rig.clock.offset += 11
            await inv.step()
        assert p.rig.sink.values("deye1", "battery_soc")  # normal polling started


async def test_api_exposes_the_state_and_accepts_the_template(tmp_path: Path) -> None:
    async with client(tmp_path, require_choice=True) as c:
        status, body = await c.get("/api/inverters")
        deye1 = next(i for i in body["inverters"] if i["id"] == "deye1")
        assert (
            deye1["selection"]["chosen"] is False and deye1["status"]["awaiting_selection"] is True
        )
        status, prev = await c.post("/api/inverters/deye1/sensors/preview", {"choose": True})
        assert status == 200 and set(prev["preview"]["adds"]) == set(
            deye1["selection"]["effective"]
        )
        status, body = await c.post("/api/inverters/deye1/sensors", {"choose": True})
        assert status == 200 and body["revision"] == 1
        _, body = await c.get("/api/inverters")
        deye1 = next(i for i in body["inverters"] if i["id"] == "deye1")
        assert (
            deye1["selection"]["chosen"] is True and deye1["status"]["awaiting_selection"] is False
        )
        status, _ = await c.post("/api/inverters/deye1/sensors", {})
        assert status == 400  # an empty edit is still refused
