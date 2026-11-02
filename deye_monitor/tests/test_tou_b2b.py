"""0.5.0 B2b domain 4: ToU slot voltages, weekday switches, Spain mode and the slot flag bits."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.protocol.guard import Framing, GuardViolation, WriteGrant, guard_write_frame
from deye_monitor.write_ops import BY_KEY
from tests.plant_helpers import plant_rig
from tests.test_write_protocol import rtu_write
from tests.write_helpers import build_plant, err, preview, write


async def test_a_weekday_switch_off_from_every_day_writes_the_other_six(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r146": 0x0001}})
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        rows = {r["key"]: r for r in body["operations"]}
        assert all(rows[f"tou_weekday_{d}"]["value"] == 1 for d in ("mon", "sun"))
        status, body = await preview(rig, "deye1", "tou_weekday_mon", 1)  # already "every day"
        assert (status, err(body)) == (409, "no_change")
        op = await write(rig, "deye1", "tou_weekday_tue", 0)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[146] == 0x00FB  # six days + the enable bit


async def test_the_last_weekday_cannot_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r146": 0x0005}})
        status, body = await preview(rig, "deye1", "tou_weekday_tue", 0)
        assert (status, err(body)) == (422, "weekday_none")
        assert rig.servers["gw1"].responder.writes == []


@pytest.mark.parametrize(
    ("value", "ok"),
    [("47.9", False), ("48.0", True), ("53.0", True), ("53.1", False), ("53.15", False)],
)
async def test_slot_voltage_stays_between_fresh_r120_and_r101(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r111": 0}})
        status, body = await preview(rig, "deye1", "tou_slot3_voltage", value)
        if ok:
            assert status == 200, body
        else:
            assert status == 422 and err(body) in ("bound_min", "bound_max", "value_not_exact")
        assert rig.servers["gw1"].responder.writes == []


async def test_slot_voltage_needs_voltage_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))  # R111 = 1
        status, body = await preview(rig, "deye1", "tou_slot1_voltage", "50.5")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "voltage_mode_required"


async def test_spain_sub_mode_bits_are_locked_without_spain_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        for tag in ("gm", "bu", "ch"):
            status, body = await preview(rig, "deye1", f"tou_slot2_spain_{tag}", 1)
            assert (status, err(body)) == (409, "operation_locked")
            assert body["error"]["params"]["reason"] == "spain_mode_off"
        assert rig.servers["gw1"].responder.writes == []


async def test_sell_and_unknown_flag_bits_keep_their_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r172": 0xFFE0 | 0x0002}})
        op = await write(rig, "deye1", "tou_slot1_grid_charge", 1)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[172] == 0xFFE3


def _grant(register: int, mask: int, old: int, new: int) -> WriteGrant:
    return WriteGrant(unit=1, register=register, mask=mask, old_word=old, new_word=new)


@pytest.mark.parametrize(
    ("register", "mask", "old", "new", "ok"),
    [
        (146, 0x00FE, 0x0001, 0x00FF, True),
        (146, 0x0100, 0x0001, 0x0101, True),
        (146, 0x0001, 0x0100, 0x0101, True),
        (146, 0x00FE, 0x0001, 0x0101, False),  # touches the Spain bit
        (146, 0x0200, 0x0000, 0x0200, False),  # not an owned field
        (146, 0x01FF, 0x0000, 0x0001, False),  # a union is not a contracted field
        (172, 0x0010, 0x0000, 0x0010, True),
        (172, 0x0020, 0x0000, 0x0020, False),  # sell bit is never owned
        (172, 0x0002, 0x0000, 0x0022, False),
        (165, 0xFFFF, 4800, 4900, True),
    ],
)
def test_guard_owns_exactly_the_contracted_fields(
    register: int, mask: int, old: int, new: int, ok: bool
) -> None:
    g = _grant(register, mask, old, new)
    if ok:
        assert guard_write_frame(rtu_write(1, register, new), Framing.RTU, grant=g)
    else:
        with pytest.raises(GuardViolation):
            guard_write_frame(rtu_write(1, register, new), Framing.RTU, grant=g)


def test_weekday_descriptors_share_one_guard_field() -> None:
    days = [d for d in BY_KEY.values() if d.weekday is not None]
    assert len(days) == 7 and {(d.register, d.mask) for d in days} == {(146, 0x00FE)}
    assert sorted(d.weekday for d in days if d.weekday is not None) == list(range(7))
