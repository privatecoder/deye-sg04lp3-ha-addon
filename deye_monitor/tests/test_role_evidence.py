"""Review 43: the GRID reference needs fresh, current R336 evidence (R43-01, R43-02) and the GEN
sum does not depend on the caller dropping its cache (N43-01)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.device_helpers import device_regs, ep, inverter, rig
from tests.test_totals import Rig

SN = "2401234567"
MASTER, SLAVE, STANDALONE = 0x0003, 0x0001, 0x0000
WINDOW = 3 * 3600.0  # three identity-group intervals


async def test_a_successful_r336_read_gives_the_role_and_other_reads_never_refresh_it(
    tmp_path: Path,
) -> None:
    regs = device_regs(SN, r336=MASTER)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        assert inv.current_role(WINDOW) is None  # nothing read yet
        await inv.step()
        assert inv.current_role(WINDOW) == "master"
        seen = inv._role_evidence
        r.clock.offset += WINDOW + 1  # the role is older than the identity TTL ...
        assert inv.current_role(WINDOW) is None  # ... even though the device is still there
        assert inv._role_evidence == seen  # no other read refreshed it


async def test_a_role_that_cannot_be_read_again_expires_with_fresh_ac_traffic(
    tmp_path: Path,
) -> None:
    regs = device_regs(SN, r336=MASTER)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.current_role(WINDOW) == "master"
        r.servers["gw1"].responder.regs[336] = SLAVE  # changed on the device ...
        r.servers["gw1"].responder.fail_addresses = {336}  # ... but R336 answers with an exception
        for _ in range(3):
            r.clock.offset += WINDOW / 2
            await inv.step()
        assert inv.current_role(WINDOW) is None  # the old "master" is no longer authoritative


async def test_a_fresh_read_recovers_the_role_and_a_rebind_revokes_it(tmp_path: Path) -> None:
    regs = device_regs(SN, r336=SLAVE)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.current_role(WINDOW) == "slave"
        r.servers["gw1"].responder.regs[336] = STANDALONE
        r.clock.offset += WINDOW + 1
        assert inv.current_role(WINDOW) is None
        await r.step_until("deye1", lambda i: i.current_role(WINDOW) == "standalone")
        assert inv.rebind()
        assert inv.current_role(WINDOW) is None  # a new lifecycle never inherits the role


# -- the totals side: fresh role evidence only ---------------------------------------------------


def _ref_rig(tmp_path: Path, ids: tuple[str, ...], roles: dict[str, str | None]) -> Rig:
    rig_ = Rig(tmp_path, ids=ids)
    rig_.grid = set(ids)
    rig_.roles = roles
    for i in ids:
        rig_.keys[i] |= {"grid_voltage_a"}
        rig_.feed(i, grid_voltage_a=230.0)
    return rig_


def test_a_single_member_is_a_reference_only_as_verified_standalone_or_master(
    tmp_path: Path,
) -> None:
    for role, expected in (
        ("standalone", "only"),
        ("master", "only"),
        ("slave", None),
        (None, None),
    ):
        rig_ = _ref_rig(tmp_path, ("only",), {"only": role})
        assert rig_.totals.reference() == expected, role
        assert rig_.value("plant_grid_voltage_a") == (230.0 if expected else None)


def test_a_stale_role_of_another_member_does_not_suppress_a_qualified_reference(
    tmp_path: Path,
) -> None:
    rig_ = _ref_rig(tmp_path, ("m", "s"), {"m": "master", "s": None})  # s: stale / unknown role
    assert rig_.totals.reference() == "m"
    assert rig_.value("plant_grid_voltage_a") == 230.0


def test_a_master_whose_role_went_stale_is_no_reference(tmp_path: Path) -> None:
    rig_ = _ref_rig(tmp_path, ("m", "s"), {"m": "master", "s": "slave"})
    assert rig_.value("plant_grid_voltage_a") == 230.0
    rig_.roles["m"] = None  # the role evidence expired: unknown, not the last known master
    assert rig_.value("plant_grid_voltage_a") is None


# -- N43-01: a used GEN input needs a current selection and an online member ---------------------


def test_gen_power_with_a_cached_value_is_unknown_after_unselect_or_offline(tmp_path: Path) -> None:
    rig_ = Rig(tmp_path, ids=("m", "s"))
    rig_.gen = {"m", "s"}
    for i in ("m", "s"):
        rig_.keys[i].add("gen_power_raw_total")
        rig_.feed(i, gen_power_raw_total=350)
    assert rig_.value("plant_gen_power") == 700
    rig_.keys["s"].discard("gen_power_raw_total")  # the cache still holds 350
    assert rig_.value("plant_gen_power") is None
    rig_.keys["s"].add("gen_power_raw_total")
    assert rig_.value("plant_gen_power") == 700
    rig_.totals.set_online("s", False)
    assert rig_.value("plant_gen_power") is None


@pytest.mark.parametrize("role", ["standalone", "master"])
def test_the_reference_survives_a_stale_unrelated_slave(tmp_path: Path, role: str) -> None:
    ids = ("a", "b") if role == "master" else ("a",)
    roles: dict[str, str | None] = {"a": role, "b": "slave"}
    rig_ = _ref_rig(tmp_path, ids, roles)
    rig_.now += 0
    assert rig_.value("plant_grid_voltage_a") == 230.0
