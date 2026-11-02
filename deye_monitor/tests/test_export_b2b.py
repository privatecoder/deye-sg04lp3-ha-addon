"""0.5.0 B2b domain 5: export mode / limits, peak-shaving powers, zero-export bias."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deye_monitor.protocol.guard import Framing, GuardViolation, WriteGrant, guard_write_frame
from tests.plant_helpers import plant_rig
from tests.test_write_protocol import rtu_write
from tests.write_helpers import build_plant, err, preview, svc_of


async def _plant(rig: Any, regs: dict[str, int], **described: Any) -> None:
    await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r178": 0x2AAA, **regs}})
    inv = svc_of(rig).runtime.inverters["deye1"]
    inv.features = inv.features.model_copy(update={"utility_grid": True, **described})


async def _lock(rig: Any, key: str, value: Any) -> str:
    status, body = await preview(rig, "deye1", key, value)
    assert (status, err(body)) == (409, "operation_locked"), body
    return str(body["error"]["params"]["reason"])


async def test_export_mode_options_follow_the_described_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 1}, export_policy="zero_export_load")
        assert await _lock(rig, "export_mode", "sell") == "export_policy_mismatch"
        assert await _lock(rig, "export_mode", "external_zero") == "export_policy_mismatch"
        status, body = await preview(rig, "deye1", "export_mode", "internal_zero")
        assert (status, err(body)) == (409, "no_change")
        assert rig.servers["gw1"].responder.writes == []


async def test_export_mode_is_locked_without_a_utility_grid_and_for_a_foreign_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {}, utility_grid=False, export_policy="export_allowed")
        assert await _lock(rig, "export_mode", "sell") == "no_utility_grid"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r142": 3}, export_policy="export_allowed")
        assert await _lock(rig, "export_mode", "sell") == "existing_value_outside_range"


async def test_limits_need_the_matching_export_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 2}, export_policy="zero_export_external")
        assert await _lock(rig, "export_max_power", 3500) == "export_not_enabled"
        status, body = await preview(
            rig, "deye1", "pv_sale_enable", 1
        )  # Solar Sell: R142 in {1, 2}
        assert status == 200, body
        status, body = await preview(rig, "deye1", "zero_export_bias", 120)
        assert status == 200, body
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r142": 0}, export_policy="export_allowed")
        assert await _lock(rig, "zero_export_bias", 120) == "zero_export_mode_required"
        assert await _lock(rig, "pv_sale_enable", 1) == "solar_sell_mode_required"


@pytest.mark.parametrize(
    ("key", "value", "ok"),
    [
        ("export_max_power", 3500, True),
        ("export_max_power", 3550, False),  # not a 100 W step
        ("export_max_power", 13000, False),  # above the model rated power / 12000
        ("pv_sale_max_power", 300, False),
        ("pv_sale_max_power", 400, True),
        ("zero_export_bias", 15, False),
        ("zero_export_bias", 25, False),
        ("zero_export_bias", 500, True),
        ("zero_export_bias", 510, False),
    ],
)
async def test_export_ranges_and_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: int, ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 0}, export_policy="export_allowed")
        if key == "zero_export_bias":
            rig.servers["gw1"].responder.regs[142] = 1
        status, body = await preview(rig, "deye1", key, value)
        assert (status == 200) is ok, (key, value, body)


async def test_peak_shaving_power_needs_its_r178_pair_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r178": 0x2A8A})  # both peak-shaving pairs 10 = off
        assert await _lock(rig, "generator_peak_shaving_power", 6000) == "gen_peak_shaving_off"
        assert await _lock(rig, "grid_peak_shaving_power", 6000) == "grid_peak_shaving_off"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r178": 0x2AAE, "r133": 0}, gen_port_mode="generator")  # GEN pair 11
        status, body = await preview(rig, "deye1", "generator_peak_shaving_power", 6000)
        assert status == 200, body
        assert await _lock(rig, "grid_peak_shaving_power", 6000) == "grid_peak_shaving_off"
        for value in (490, 6005, 16010):
            status, body = await preview(rig, "deye1", "generator_peak_shaving_power", value)
            assert status == 422, value


def _grant(register: int, old: int, new: int) -> WriteGrant:
    return WriteGrant(unit=1, register=register, mask=0xFFFF, old_word=old, new_word=new)


@pytest.mark.parametrize(
    ("register", "old", "new", "ok"),
    [(142, 3, 0, False), (142, 2, 0, True), (142, 0, 3, False), (142, 0, 0xFFFF, False),
     (145, 0, 2, False),
     (145, 0, 1, True), (143, 3000, 3500, True), (340, 0xFFFF, 400, False), (141, 0, 1, True),
     (141, 1, 2, False), (141, 2, 0, False), (141, 0xFFFF, 0, False)],
)  # fmt: skip
def test_guard_for_the_export_registers(register: int, old: int, new: int, ok: bool) -> None:
    g = _grant(register, old, new)
    if ok:
        assert guard_write_frame(rtu_write(1, register, new), Framing.RTU, grant=g)
    else:
        with pytest.raises(GuardViolation):
            guard_write_frame(rtu_write(1, register, new), Framing.RTU, grant=g)
