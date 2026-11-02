"""0.5.0 B2b domain 8: EV flags / SOC (R259, signature gate), R260 power and R237, all per unit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deye_monitor.protocol.guard import Framing, GuardViolation, WriteGrant, guard_write_frame
from deye_monitor.write_ops import BY_KEY
from tests.plant_helpers import plant_rig
from tests.test_write_protocol import rtu_write
from tests.write_helpers import build_plant, err, preview, svc_of, write


def _describe(rig: Any, inverter: str = "deye1", **features: Any) -> None:
    inv = svc_of(rig).runtime.inverters[inverter]
    inv.features = inv.features.model_copy(update=features)


SLAVE = {"r336": 0x0001}
MASTER = {"r336": 0x0003}


@pytest.mark.parametrize("word", [0x32DD, 0x32DE, 0x32ED, 0x32EE])
async def test_the_four_known_signatures_admit_the_ev_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r259": word}})
        _describe(rig, ev_charger="load_port" if word & 0x30 == 0x20 else "grid_port")
        for key, value in (("ev_free_work", 0 if word & 3 == 2 else 1), ("ev_max_power", 3100)):
            status, body = await preview(rig, "deye1", key, value)
            assert status == 200, (key, body)


@pytest.mark.parametrize("word", [0x3200, 0x32D0, 0x32DF, 0x32CD, 0x32FF, 0xFFFF, 0x0000])
async def test_any_other_low_byte_locks_every_ev_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r259": word}})
        for key, value in (
            ("ev_free_work", 1),
            ("ev_location", "load"),
            ("ev_offgrid_soc", 60),
            ("ev_max_power", 3100),
        ):
            status, body = await preview(rig, "deye1", key, value)
            assert (status, err(body)) == (409, "operation_locked"), key
            assert body["error"]["params"]["reason"] == "ev_signature_unknown"
        assert rig.servers["gw1"].responder.writes == []


async def test_each_ev_field_changes_only_its_own_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r259": 0x32DD}})
        regs = rig.servers["gw1"].responder.regs
        await write(rig, "deye1", "ev_offgrid_soc", 100)
        assert regs[259] == 0x64DD
        _describe(rig, ev_charger="load_port")  # the location is written to what is described
        await write(rig, "deye1", "ev_location", "load")
        assert regs[259] == 0x64ED
        await write(rig, "deye1", "ev_free_work", 1)
        assert regs[259] == 0x64EE
        await write(rig, "deye1", "ev_free_work", 0)
        assert regs[259] == 0x64ED


@pytest.mark.parametrize(
    ("soc", "ok"),
    [
        (0, False),
        (10, False),
        (30, False),
        (40, True),
        (100, True),
        (55, False),
        (110, False),
        (-10, False),
    ],
)  # fmt: skip  (the reserve floor is R117 = 35)
async def test_ev_soc_is_a_step_of_ten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, soc: int, ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r259": 0x32DD}})
        status, body = await preview(rig, "deye1", "ev_offgrid_soc", soc)
        assert (status == 200) is ok, body


@pytest.mark.parametrize(("watts", "ok"), [(0, True), (100, True), (22000, True), (22100, False),
                                           (150, False), (-100, False)])  # fmt: skip
async def test_ev_power_range_and_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watts: int, ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r260": 5000}})
        status, body = await preview(rig, "deye1", "ev_max_power", watts)
        assert (status == 200) is ok, body


async def test_an_ev_power_word_above_the_range_is_shown_but_not_overwritten_blindly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r260": 40000}})
        status, body = await preview(rig, "deye1", "ev_max_power", 5000)
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "existing_value_outside_range"


async def test_per_unit_settings_are_written_on_a_slave_and_the_hint_covers_r237(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig,
            inverters=("deye1", "deye2"),
            enable=("deye1", "deye2"),
            regs={"deye1": MASTER, "deye2": {**SLAVE, "r259": 0x32DD}},
        )
        op = await write(rig, "deye2", "ev_max_power", 6000)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw2"].responder.regs[260] == 6000
        assert rig.servers["gw1"].responder.regs[260] == 3000  # nothing fans out
        op = await write(rig, "deye2", "unbalanced_load", 1)
        assert op["status"] == "readback_confirmed"
        _s, body = await rig.call("POST", "/api/inverters/deye2/writes/current", {})
        hint = body["info"]["equal"]["unbalanced_load"]
        assert hint["values"] == {"deye1": 0, "deye2": 1} and hint["equal"] is False
        assert "ev_max_power" not in body["info"]["equal"]  # no blanket equality check


def test_descriptors_are_per_unit_and_share_the_register_without_overlap() -> None:
    fields = [BY_KEY[k] for k in ("ev_free_work", "ev_location", "ev_offgrid_soc")]
    assert all(not d.synchronized for d in fields)
    assert sorted(d.mask for d in fields) == [0x0003, 0x0030, 0xFF00]
    assert BY_KEY["unbalanced_load"].whole_word_bool and BY_KEY["unbalanced_load"].equal_hint
    assert not BY_KEY["ev_max_power"].equal_hint


@pytest.mark.parametrize(
    ("mask", "old", "new", "ok"),
    [
        (0x0003, 0x32DD, 0x32DE, True),
        (0x0003, 0x32DD, 0x32DC, False),  # a free-work value of 0 / 3 is no state
        (0x0003, 0x32DC, 0x32DD, False),  # unknown signature in the old word
        (0x0030, 0x32DD, 0x32ED, True),
        (0x0030, 0x32DD, 0x32CD, False),
        (0xFF00, 0x32DD, 0x3CDD, True),  # 0x3C = 60 %
        (0xFF00, 0x32DD, 0x3DDD, False),
        (0xFF00, 0x32DD, 0x46DD, True),
        (0xFF00, 0xFFFF, 0x46DD, False),
        (0xFF00, 0x32DD, 0x32DE, False),  # the low byte may not change under the SOC mask
    ],
)
def test_guard_for_r259(mask: int, old: int, new: int, ok: bool) -> None:
    g = WriteGrant(1, 259, mask, old, new)
    if ok:
        assert guard_write_frame(rtu_write(1, 259, new), Framing.RTU, grant=g)
    else:
        with pytest.raises(GuardViolation):
            guard_write_frame(rtu_write(1, 259, new), Framing.RTU, grant=g)
