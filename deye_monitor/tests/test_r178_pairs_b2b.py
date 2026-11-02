"""0.5.0 B2b domain 6: the other R178 pairs (wire guard per field) and R341 bit 4."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.protocol.guard import Framing, GuardViolation, WriteGrant, guard_write_frame
from tests.plant_helpers import plant_rig
from tests.test_write_protocol import rtu_write
from tests.write_helpers import build_plant, err, preview, write

PAIRS = {
    "microinverter_export_cutoff": 0x0003,
    "generator_peak_shaving": 0x000C,
    "grid_peak_shaving": 0x0030,
    "on_grid_smart_load_always_on": 0x00C0,
    "signal_island_mode": 0x0300,
    "bms_loss_fault": 0x0C00,
    "drm_enable": 0x3000,
}


@pytest.mark.parametrize(("mask", "old_pair", "new_pair", "ok"),
    [(m, o, n, ok) for m in PAIRS.values()
     for o, n, ok in ((2, 3, True), (3, 2, True), (0, 3, False), (1, 3, False), (3, 1, False),
                      (2, 0, False))])  # fmt: skip
def test_every_pair_is_guarded_on_the_wire(
    mask: int, old_pair: int, new_pair: int, ok: bool
) -> None:
    shift = (mask & -mask).bit_length() - 1
    base = 0x2AAA & ~mask
    old, new = base | old_pair << shift, base | new_pair << shift
    g = WriteGrant(unit=1, register=178, mask=mask, old_word=old, new_word=new)
    if ok:
        assert guard_write_frame(rtu_write(1, 178, new), Framing.RTU, grant=g)
    else:
        with pytest.raises(GuardViolation):
            guard_write_frame(rtu_write(1, 178, new), Framing.RTU, grant=g)


def test_a_pair_write_cannot_touch_another_pair_or_the_sentinel() -> None:
    g = WriteGrant(1, 178, 0x000C, 0x2AAA, 0x2AAB)  # also changes bit 0
    with pytest.raises(GuardViolation):
        guard_write_frame(rtu_write(1, 178, 0x2AAB), Framing.RTU, grant=g)
    g = WriteGrant(1, 178, 0x000C, 0xFFFF, 0xFFFB)
    with pytest.raises(GuardViolation):
        guard_write_frame(rtu_write(1, 178, 0xFFFB), Framing.RTU, grant=g)
    for mask in (0xC000, 0x0007):  # bits 14-15 are never owned; no odd masks
        g = WriteGrant(1, 178, mask, 0x2AAA, 0x2AAA ^ mask)
        with pytest.raises(GuardViolation):
            guard_write_frame(rtu_write(1, 178, g.new_word), Framing.RTU, grant=g)


async def test_one_pair_changes_and_the_others_keep_their_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r178": 0x2AAA}})
        op = await write(rig, "deye1", "drm_enable", 1)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[178] == 0x3AAA
        op = await write(rig, "deye1", "drm_enable", 0)
        assert rig.servers["gw1"].responder.regs[178] == 0x2AAA


@pytest.mark.parametrize("word", [0x2AA9, 0x2AA5, 0xFFFF])
async def test_a_no_action_pair_or_sentinel_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r178": word}})
        status, body = await preview(rig, "deye1", "microinverter_export_cutoff", 1)
        assert status == 409 and err(body) in ("operation_locked", "unexpected_bits")
        assert rig.servers["gw1"].responder.writes == []


async def test_the_fan_bit_keeps_the_limiter_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r341": 0x0060}})
        op = await write(rig, "deye1", "bypass_fan_minimum_request", 1)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[341] == 0x0070
