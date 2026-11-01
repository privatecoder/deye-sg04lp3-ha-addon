"""Whole-group validation of the ordered settings (review 23: R23-04/05): every member of the SOC
protection triplet and of the six ToU starts is read fresh and valid, and the resulting group is
strictly ascending. No neighbour-only repair. A stored 23:59 is a valid incumbent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.plant_helpers import plant_rig
from tests.write_helpers import build_plant, confirm, err, fast, fc16, preview, write


async def refused(rig, key: str, value: object) -> tuple[int, dict[str, Any]]:  # type: ignore[no-untyped-def]
    status, body = await preview(rig, "deye1", key, value)
    assert rig.servers["gw1"].responder.writes == [] and fc16(rig.servers["gw1"]) == []
    return status, body


async def test_a_disordered_start_group_is_not_repaired_by_a_neighbour_valid_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig,
            inverters=("deye1",),
            regs={"deye1": {"r148": 0, "r149": 500, "r150": 1000, "r151": 900, "r152": 2000}},
        )
        status, body = await refused(rig, "tou_slot2_start", "06:00")
        assert (status, err(body)) == (422, "tou_order")
        assert body["error"]["params"]["ordering"] == "group"
        assert body["error"]["params"]["companion"] == "tou_slot3_start"


async def test_an_invalid_companion_time_is_not_a_numerical_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r150": 1065}})
        status, body = await refused(rig, "tou_slot2_start", "06:00")
        assert (status, err(body)) == (409, "companion_invalid")
        assert body["error"]["params"]["companion"] == "tou_slot3_start"


async def test_a_protection_triplet_that_is_already_disordered_is_not_edited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r115": 25, "r117": 35, "r116": 20}}
        )
        status, body = await refused(rig, "battery_shutdown_soc", 24)
        assert (status, err(body)) == (422, "threshold_order")
        assert body["error"]["params"]["ordering"] == "group"


async def test_an_out_of_domain_low_soc_is_not_an_upper_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r117": 0xFFFF}})
        status, body = await refused(rig, "battery_shutdown_soc", 50)
        assert (status, err(body)) == (409, "companion_invalid")
        assert body["error"]["params"]["companion"] == "battery_low_soc"
        status, body = await refused(rig, "battery_restart_soc", 80)
        assert (status, err(body)) == (409, "companion_invalid")


async def test_a_valid_group_edit_reads_the_whole_group_and_writes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        op = await write(rig, "deye1", "battery_shutdown_soc", 30)
        assert op["status"] == "readback_confirmed"
        assert [(w.start, w.value) for w in rig.servers["gw1"].responder.writes] == [(115, 30)]
        status, body = await preview(rig, "deye1", "battery_shutdown_soc", 35)  # equal to low
        assert (status, err(body)) == (422, "threshold_order")


async def test_an_external_change_of_any_group_member_between_preview_and_send_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", "tou_slot2_start", "06:00")
        assert status == 200
        rig.servers["gw1"].responder.regs[152] = 700  # slot 5, not a neighbour of slot 2
        status, body = await confirm(rig, body["preview"]["operation_id"])
        assert status == 200 and body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "conflict"
        assert rig.servers["gw1"].responder.writes == []


async def test_a_stored_2359_is_replaced_by_a_valid_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r153": 2359}})
        _status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        rows = {r["key"]: r for r in body["operations"]}
        assert (
            rows["tou_slot6_start"]["state"] == "ready"
            and rows["tou_slot6_start"]["value"] == "23:59"
        )
        op = await write(rig, "deye1", "tou_slot6_start", "23:55")
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[153] == 2355
        # a neighbour edit against the stored 23:59 is judged on the valid incumbent as well
        fast(rig)


async def test_a_neighbour_edit_next_to_a_stored_2359_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r153": 2359}})
        op = await write(rig, "deye1", "tou_slot5_start", "22:00")
        assert op["status"] == "readback_confirmed"


async def test_an_invalid_stored_time_still_locks_its_own_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r153": 2360}})
        status, body = await refused(rig, "tou_slot6_start", "23:55")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "existing_value_outside_range"
