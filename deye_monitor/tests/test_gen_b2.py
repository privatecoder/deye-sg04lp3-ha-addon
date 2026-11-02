"""0.5.0 part B2, domain 2: GEN port settings (R121-R124, R129, R131-R133)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.protocol.guard import GuardViolation, WriteGrant, check_grant
from deye_monitor.write_ops import BY_KEY, Refused, check_range, parse_value
from tests.plant_helpers import plant_rig
from tests.write_helpers import build_plant, err, preview, svc_of, write


def test_hours_are_half_hour_steps_of_a_ten_times_register() -> None:
    d = BY_KEY["generator_max_run_time"]
    assert parse_value(d, "24") == 240 and parse_value(d, "0.5") == 5
    for bad in ("0.25", "24.5"):
        with pytest.raises(Refused):
            check_range(d, parse_value(d, bad), None)


def test_gen_texts_name_the_lcd_companion_gap() -> None:
    risk = BY_KEY["gen_port_usage"].risk
    assert "FC16" in risk.en and "FC16" in risk.de and "R129" in risk.en


@pytest.mark.parametrize("old,new", [(0, 3), (3, 0), (0, 0xFFFF)])
def test_the_guard_admits_only_0_1_2_for_the_port_usage(old: int, new: int) -> None:
    with pytest.raises(GuardViolation):
        check_grant(WriteGrant(1, 133, 0xFFFF, old, new))
    check_grant(WriteGrant(1, 133, 0xFFFF, 0, 2))


def test_whole_word_switches_refuse_other_words() -> None:
    for reg in (129, 132):
        with pytest.raises(GuardViolation):
            check_grant(WriteGrant(1, reg, 0x0001, 0, 3))


async def test_frequency_range_follows_the_fresh_nominal_frequency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 2, "r183": 0}})
        status, body = await preview(rig, "deye1", "ac_coupling_upper_frequency", "55.01")
        assert (status, err(body)) == (422, "value_out_of_range")
        assert (await preview(rig, "deye1", "ac_coupling_upper_frequency", "55.00"))[0] == 200
        rig.servers["gw1"].responder.regs[183] = 1  # 60 Hz
        status, body = await preview(rig, "deye1", "ac_coupling_upper_frequency", "52")
        assert (status, err(body)) == (422, "value_out_of_range")
        assert (await preview(rig, "deye1", "ac_coupling_upper_frequency", "61"))[0] == 200
        rig.servers["gw1"].responder.regs[183] = 7  # unknown nominal frequency
        status, body = await preview(rig, "deye1", "ac_coupling_upper_frequency", "61")
        assert body["error"]["params"]["reason"] == "nominal_frequency_unknown"


async def test_the_port_role_needs_the_description_and_the_generator_settings_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 1}})
        _status, body = await preview(rig, "deye1", "gen_port_usage", "generator")
        assert body["error"]["params"]["reason"] == "port_role_not_described"
        _status, body = await preview(rig, "deye1", "generator_max_run_time", 2)
        assert body["error"]["params"]["reason"] == "gen_port_not_generator"
        assert rig.servers["gw1"].responder.writes == []
        assert svc_of(rig) is not None


async def test_an_invalid_port_incumbent_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 5}})
        inv = svc_of(rig).runtime.inverters["deye1"]
        inv.features = inv.features.model_copy(update={"gen_port_mode": "generator"})
        status, body = await preview(rig, "deye1", "gen_port_usage", "generator")
        assert body["error"]["params"]["reason"] == "existing_value_outside_range"
        assert write is not None and status == 409
