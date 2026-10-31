from __future__ import annotations

import itertools

import pytest

from deye_monitor.config import PollConfig
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.hardware import HardwareProfile, load_hardware
from deye_monitor.profiles.plan import (
    ReadPlan,
    SelectionError,
    build_read_plan,
    selection_problems,
    template_selection,
    validate_selection,
)
from deye_monitor.profiles.schema import Profile, load_profile

DEFAULT = Features()


@pytest.fixture(scope="module")
def p() -> Profile:
    return load_profile("sg04lp3_lv_v105_8")


@pytest.fixture(scope="module")
def hw() -> HardwareProfile:
    return load_hardware("sg04lp3_eu")


def plan(
    p: Profile,
    keys: set[str],
    *,
    max_read_registers: int = 60,
    energy_auto: bool = True,
    max_gap: int = 0,
    poll: PollConfig | None = None,
    response_time_s: float = 0.25,
) -> ReadPlan:
    return build_read_plan(
        p,
        keys,
        DEFAULT,
        max_read_registers=max_read_registers,
        energy_auto=energy_auto,
        max_gap=max_gap,
        poll=poll,
        response_time_s=response_time_s,
    )


def blocks_of(pl: ReadPlan, group: str) -> list[tuple[int, int]]:
    return [(b.start, b.count) for b in pl.blocks if b.group == group]


# -- read plan ------------------------------------------------------------------------------------


def test_empty_selection_reads_only_mandatory_identity_and_health(p: Profile) -> None:
    pl = plan(p, set())
    assert {b.group for b in pl.blocks} == {"identity", "status"}
    assert pl.keys == pl.mandatory_keys
    assert not pl.reads_r65
    assert 65 not in pl.addresses
    assert {3, 4, 5, 6, 7, 336, 500, 551, 552, 553, 554, 555, 556, 557, 558} <= pl.addresses
    assert not pl.addresses & {598, 650, 672, 514, 586}


def test_single_multiword_sensor_pulls_exactly_its_partner(p: Profile) -> None:
    pl = plan(p, {"load_power_a"})
    assert pl.dependencies["load_power_a"] == (650, 656)
    assert blocks_of(pl, "ac") == [(650, 1), (656, 1)]  # no bridging of 651..655
    assert {b.group for b in pl.blocks} == {"identity", "status", "ac"}
    plan_total = plan(p, {"grid_selected_power_total"})
    assert plan_total.dependencies["grid_selected_power_total"] == (625, 690)
    assert blocks_of(plan_total, "ac") == [(625, 1), (690, 1)]


def test_non_adjacent_pairs_are_planned_in_the_same_cycle(p: Profile) -> None:
    pl = plan(p, {"inverter_power_total", "load_power_total"})
    assert set(pl.dependencies["inverter_power_total"]) == {636, 694}
    assert set(pl.dependencies["load_power_total"]) == {653, 659}
    assert pl.addresses >= {636, 694, 653, 659}


def test_energy_without_auto_does_not_read_r65(p: Profile) -> None:
    keys = {"battery_energy_charge_today", "pv_energy_total"}
    off = plan(p, keys, energy_auto=False)
    assert not off.reads_r65
    assert 65 not in off.addresses
    assert "dsp_protocol_version" not in off.keys
    on = plan(p, keys, energy_auto=True)
    assert on.reads_r65
    assert 65 in on.addresses
    assert "dsp_protocol_version" in on.keys
    # auto without any energy entity: no R65 either
    assert 65 not in plan(p, {"battery_soc"}, energy_auto=True).addresses


def test_blocks_never_bridge_unknown_addresses(p: Profile) -> None:
    keys = {"grid_voltage_a", "grid_frequency"}  # 598 .. 609, R601 is not in the profile
    for gap in (0, 3, 20, 100):
        assert blocks_of(plan(p, keys, max_gap=gap), "ac") == [(598, 1), (609, 1)]
    known = plan(p, {"pv1_power", "pv1_voltage"}, max_gap=3)
    assert blocks_of(known, "pv") == [(672, 5)]  # 673..675 are known profile addresses
    assert blocks_of(plan(p, {"pv1_power", "pv1_voltage"}), "pv") == [(672, 1), (676, 1)]
    for b in known.blocks:
        # every address in a block is either needed or known to the profile
        assert all(
            a in b.needed or a in p.known_addresses for a in range(b.start, b.start + b.count)
        )


def test_blocks_respect_the_register_limit(p: Profile, hw: HardwareProfile) -> None:
    keys = set(template_selection(p, hw, DEFAULT))
    needed = {a for k in keys | {"dsp_protocol_version"} for a in p.entry(k).addresses}
    for limit in (1, 2, 7, 60, 125):
        pl = plan(p, keys, max_read_registers=limit, max_gap=10)
        assert all(1 <= b.count <= limit for b in pl.blocks)
        covered = {a for b in pl.blocks for a in range(b.start, b.start + b.count)}
        assert needed <= covered
        assert pl.addresses == needed | {a for k in pl.mandatory_keys for a in p.entry(k).addresses}
        assert all(set(b.needed) <= set(range(b.start, b.start + b.count)) for b in pl.blocks)
    tight = plan(
        p,
        {"energy_dummy"} - {"energy_dummy"} | {"battery_energy_charge_today"},
        max_read_registers=1,
    )
    assert all(b.count == 1 for b in tight.blocks)


def test_limit_arguments_are_validated(p: Profile) -> None:
    for limit in (0, -1, 126):
        with pytest.raises(ValueError):
            plan(p, set(), max_read_registers=limit)
    with pytest.raises(ValueError):
        plan(p, set(), max_gap=-1)


def test_plan_is_deterministic_and_group_ordered(p: Profile, hw: HardwareProfile) -> None:
    keys = set(template_selection(p, hw, DEFAULT))
    a = plan(p, keys)
    b = plan(p, set(reversed(sorted(keys))))
    assert a.blocks == b.blocks
    order: list[str] = list(p.groups)
    groups = [bl.group for bl in a.blocks]
    assert groups == sorted(groups, key=order.index)


def test_budget(p: Profile, hw: HardwareProfile) -> None:
    pl = plan(p, {"pv1_power", "pv1_voltage"})
    pv = pl.budget.per_group["pv"]
    assert pv.blocks == 2
    assert pv.cycle_s == pytest.approx(2 * 0.30)
    assert pv.interval_s == 10
    assert pv.utilisation == pytest.approx(0.06)
    assert not pl.budget.overloaded
    assert pl.budget.utilisation == pytest.approx(
        sum(g.utilisation for g in pl.budget.per_group.values())
    )
    keys = set(template_selection(p, hw, DEFAULT))
    heavy = plan(p, keys, poll=PollConfig(fast_s=1, energy_s=1, settings_s=1, identity_s=1))
    assert heavy.budget.overloaded
    relaxed = plan(
        p, keys, poll=PollConfig(fast_s=60, energy_s=60, settings_s=300, identity_s=3600)
    )
    assert not relaxed.budget.overloaded
    slow_device = plan(p, keys, response_time_s=2.0)
    assert slow_device.budget.utilisation > plan(p, keys).budget.utilisation


# -- selection enforcement ------------------------------------------------------------------------


def test_validate_selection_rejects_pv3_and_other_placeholders(p: Profile) -> None:
    for key in (
        "pv3_power",
        "pv4_voltage",
        "battery2_soc",
        "grid_apparent_internal",
        "pv1_energy_today_r530",
    ):
        with pytest.raises(SelectionError) as exc:
            validate_selection(p, {"pv1_power", key})
        assert exc.value.rejected == {key: "placeholder"}
    for key in ("battery2_current", "battery2_negative_current"):
        with pytest.raises(SelectionError) as exc:
            validate_selection(p, {key})
        assert exc.value.rejected == {key: "unsupported_for_profile"}
    with pytest.raises(SelectionError, match="pv3_power"):
        plan(p, {"pv3_power"})  # the plan builder enforces it too


def test_validate_selection_unknown_and_internal(p: Profile) -> None:
    assert selection_problems(p, {"nope"}) == {"nope": "unknown entry"}
    assert selection_problems(p, {"id_serial"}) == {"id_serial": "internal entry"}
    assert selection_problems(p, {"R650"}) == {
        "R650": "unknown entry"
    }  # no free register selection


def test_unresolved_only_as_raw_diagnostic(p: Profile) -> None:
    validate_selection(
        p, {"battery_power_raw", "tou_slot1_soc_raw", "pv_ext_r722", "ev_charge_power_r709"}
    )
    for key in ("battery_power_raw", "tou_slot1_soc_raw", "pv_ext_r722"):
        e = p.entry(key)
        assert e.is_raw_diagnostic and not e.is_physical and e.unit is None
    raw_plan = plan(p, {"battery_power_raw"})
    assert raw_plan.dependencies["battery_power_raw"] == (590,)


def test_validate_selection_accepts_the_whole_template(p: Profile, hw: HardwareProfile) -> None:
    validate_selection(p, template_selection(p, hw, DEFAULT))
    validate_selection(p, set())


# -- templates for plant features (REVIEW2 DR2-05) -------------------------------------------------


def tpl(p: Profile, hw: HardwareProfile, **features: object) -> frozenset[str]:
    return template_selection(p, hw, Features(**features))  # type: ignore[arg-type]


DIAGNOSTICS = {
    "serial_number",
    "operating_state",
    "active_warnings",
    "active_faults",
    "power_switch",
    "remote_run_enable",
    "parallel_enable",
}  # fw_main_version (R15) is part of the MCU2 diagnostic since 0.2.7


def test_diagnostics_are_available_for_every_feature_combination(
    p: Profile, hw: HardwareProfile
) -> None:
    combos = itertools.product(
        [True, False],
        ["utility", "generator", "unused"],
        [True, False],
        [True, False],
        ["unused", "generator", "smart_load", "ac_coupled"],
        [True, False],
        [True, False],
        [True, False],
    )
    for utility, port, battery, bms, gen, m1, m2, parallel in combos:
        t = tpl(
            p,
            hw,
            utility_grid=utility,
            grid_port_use=port,
            battery=battery,
            bms=bms,
            gen_port_mode=gen,
            mppt1_used=m1,
            mppt2_used=m2,
            parallel=parallel,
        )
        assert t >= DIAGNOSTICS


def test_off_grid_with_generator_on_the_grid_port(p: Profile, hw: HardwareProfile) -> None:
    t = tpl(p, hw, utility_grid=False, grid_port_use="generator")
    assert {"grid_voltage_a", "grid_internal_power_total", "grid_frequency"} <= t  # still measured
    assert "grid_selected_power_total" not in t  # the utility view is not the generator terminal
    assert not t & {"grid_energy_import_today", "grid_energy_export_total"}  # no utility energy
    off = tpl(p, hw, utility_grid=False, grid_port_use="unused")
    assert not off & {"grid_voltage_a", "grid_selected_power_total", "grid_energy_import_today"}
    utility = tpl(p, hw)
    assert {"grid_voltage_a", "grid_energy_import_today", "grid_energy_export_total"} <= utility
    assert {
        "inverter_power_total",
        "load_power_total",
        "load_energy_today",
    } <= off  # island needs these


def test_ac_coupling_at_the_gen_port_without_generator(p: Profile, hw: HardwareProfile) -> None:
    ac = tpl(p, hw, gen_port_mode="ac_coupled")
    assert {"gen_voltage_a", "gen_voltage_b", "gen_voltage_c"} <= ac  # measurement place stays
    assert tpl(p, hw, gen_port_mode="generator") >= ac & {"gen_voltage_a"}
    assert tpl(p, hw, gen_port_mode="smart_load") >= {"gen_voltage_a"}
    none = tpl(p, hw, gen_port_mode="unused")
    assert not none & {"gen_voltage_a", "gen_voltage_b"}
    # AC coupling never creates PV channels (no PV3 from the GEN port)
    assert not [k for k in ac if k.startswith(("pv3", "pv4"))]


def test_battery_without_bms(p: Profile, hw: HardwareProfile) -> None:
    no_bms = tpl(p, hw, battery=True, bms=False)
    assert {
        "battery_soc",
        "battery_voltage",
        "battery_current",
        "battery_energy_charge_total",
    } <= no_bms
    assert not [k for k in no_bms if k.startswith("bms_")]
    with_bms = tpl(p, hw, battery=True, bms=True)
    assert {"bms_soc", "bms_temperature", "bms_charge_current_limit"} <= with_bms
    none = tpl(p, hw, battery=False, bms=True)
    assert not none & {
        "battery_soc",
        "battery_temperature",
        "bms_soc",
        "battery_energy_charge_today",
        "battery_chemistry",
    }
    assert "battery_power_raw" not in with_bms  # raw diagnostics are never default


def test_parallel_slave_without_pv(p: Profile, hw: HardwareProfile) -> None:
    slave = tpl(p, hw, parallel=True, mppt1_used=False, mppt2_used=False)
    assert not [k for k in slave if k.startswith(("pv1_", "pv2_", "pv_energy"))]
    assert {"parallel_enable", "parallel_master", "parallel_sn"} <= slave
    assert "parallel_phase" not in slave  # three-phase model: every unit reads "A"
    assert {"load_power_total", "bms_voltage"} <= slave
    one = tpl(p, hw, mppt2_used=False)
    assert "pv1_power" in one and "pv2_power" not in one
    assert "pv_energy_today" in one


def test_ten_kw_unit_with_three_strings_has_two_measurement_channels(
    p: Profile, hw: HardwareProfile
) -> None:
    t = tpl(p, hw)
    pv_keys = {
        k for k in t if k.startswith("pv") and k.endswith(("_power", "_voltage", "_current"))
    }
    assert pv_keys == {
        "pv1_power",
        "pv1_voltage",
        "pv1_current",
        "pv2_power",
        "pv2_voltage",
        "pv2_current",
    }
    assert sum(hw.strings_by_rated_kw[10].model_dump().values()) == 3
    assert hw.mppt_channels == 2


def test_template_contains_only_selectable_default_entries(p: Profile, hw: HardwareProfile) -> None:
    t = tpl(p, hw)
    by_key = {e.key: e for e in p.entries}
    assert all(
        by_key[k].default_enabled and by_key[k].is_physical and by_key[k].requires.evaluate(DEFAULT)
        for k in t
    )
    assert not [k for k in t if by_key[k].release == "unresolved" or by_key[k].internal]
    foreign = hw.model_copy(update={"name": "other_hw"})
    assert template_selection(p, foreign, DEFAULT) == frozenset()


def test_template_only_changes_defaults_never_rights(p: Profile, hw: HardwareProfile) -> None:
    # a key excluded from the template (PV3, placeholder) stays rejected for every feature set
    for feats in (Features(), Features(gen_port_mode="ac_coupled"), Features(mppt1_used=False)):
        assert "pv3_power" not in template_selection(p, hw, feats)
        assert selection_problems(p, {"pv3_power"})
    # a physical key excluded by features is still selectable (user may deviate from the template)
    off = tpl(p, hw, battery=False)
    assert "battery_soc" not in off
    validate_selection(p, {"battery_soc"})
