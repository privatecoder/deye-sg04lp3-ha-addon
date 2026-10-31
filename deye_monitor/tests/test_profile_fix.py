"""Findings T5-01 .. T5-04 and the producer evidence additions (Code-Review 3)."""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from deye_monitor.config import PollConfig
from deye_monitor.profiles.decode import decode
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.hardware import HardwareProfile, load_hardware
from deye_monitor.profiles.plan import build_read_plan, template_selection, validate_selection
from deye_monitor.profiles.schema import Entry, Profile, load_profile
from tests.test_profile_schema import SHA_1151, SHA_1175, entry_dict


@pytest.fixture(scope="module")
def p() -> Profile:
    return load_profile("sg04lp3_lv_v105_8")


@pytest.fixture(scope="module")
def hw() -> HardwareProfile:
    return load_hardware("sg04lp3_eu")


ISLAND = Features(utility_grid=False, grid_port_use="unused", battery=True, parallel=True)
GRID = Features()
ISLAND_KEYS = {
    "load_voltage_a",
    "load_voltage_b",
    "load_voltage_c",
    "inverter_frequency",
}
ISLAND_ADDRESSES = {644, 645, 646, 638}


# -- T5-01 -----------------------------------------------------------------------------------------


def test_island_template_has_ac_voltage_frequency_and_eps_power(
    p: Profile, hw: HardwareProfile
) -> None:
    template = template_selection(p, hw, ISLAND)
    assert template >= ISLAND_KEYS
    plan = build_read_plan(p, template, ISLAND, max_read_registers=60, energy_auto=True)
    assert plan.addresses >= ISLAND_ADDRESSES
    # measurement points keep their names: nothing is equated or added
    assert p.entry("load_voltage_a").point == "ups_load"
    assert p.entry("inverter_frequency").point == "inverter_output"
    assert p.entry("ups_power_total").point == "ups_load"
    assert p.entry("inverter_power_total").point == "inverter_output"
    assert p.entry("load_power_total").point == "ups_load"
    # UPS power equals the load power off-grid (field evidence): selectable, not preselected
    assert {"inverter_power_total", "load_power_total"} <= template
    assert "ups_power_total" not in template
    validate_selection(p, {"ups_power_total", "ups_power_a"})


def test_slave_without_pv_keeps_the_island_ac_basics(p: Profile, hw: HardwareProfile) -> None:
    slave = Features(
        utility_grid=False,
        grid_port_use="unused",
        parallel=True,
        mppt1_used=False,
        mppt2_used=False,
    )
    template = template_selection(p, hw, slave)
    assert template >= ISLAND_KEYS
    assert not [k for k in template if k.startswith(("pv1_", "pv2_", "pv_energy"))]


def test_grid_mode_template_is_unchanged(p: Profile, hw: HardwareProfile) -> None:
    template = template_selection(p, hw, GRID)
    assert "inverter_frequency" not in template  # the grid sets the frequency
    assert {"grid_voltage_a", "grid_frequency", "load_voltage_a"} <= template  # LOAD is measured
    # the island entries stay selectable by hand: the template changes defaults, not rights
    validate_selection(p, ISLAND_KEYS)


def test_island_budget_is_reported_per_inverter(p: Profile, hw: HardwareProfile) -> None:
    island = build_read_plan(
        p, template_selection(p, hw, ISLAND), ISLAND, max_read_registers=60, energy_auto=True
    )
    grid = build_read_plan(
        p, template_selection(p, hw, GRID), GRID, max_read_registers=60, energy_auto=True
    )
    assert island.budget.per_group["ac"].blocks > 0
    assert not island.budget.overloaded
    # the island template has its own cost, reported per inverter (never aggregated here)
    assert 0 < island.budget.utilisation < 1
    assert island.budget.utilisation != grid.budget.utilisation
    # four separate gateways are four separate budgets: no aggregation inside one plan
    assert island.budget.utilisation == pytest.approx(
        sum(g.utilisation for g in island.budget.per_group.values())
    )


# -- T5-02 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 0, -1.0])
def test_scale_must_be_finite_and_positive(bad: float) -> None:
    with pytest.raises(ValidationError):
        Entry.model_validate(entry_dict(scale=bad))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_raw_offset_must_be_finite(bad: float) -> None:
    with pytest.raises(ValidationError):
        Entry.model_validate(entry_dict(raw_offset=bad))
    Entry.model_validate(entry_dict(raw_offset=-1000.0, scale=0.1))  # a normal offset still works


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 0.0, 0.5, 10.0, -0.1, "0.1", True])
def test_decoder_accepts_only_the_validated_energy_factors(p: Profile, bad: Any) -> None:
    with pytest.raises(ValueError):
        decode(p.entry("battery_energy_charge_today"), {514: 100}, energy_resolution=bad)
    for good in (0.1, 1.0, None):
        decode(p.entry("battery_energy_charge_today"), {514: 100}, energy_resolution=good)
    assert decode(p.entry("battery_energy_charge_today"), {514: 100}, energy_resolution=0.1) == 10.0
    assert decode(p.entry("battery_soc"), {588: 50}) == 50  # normal values are unchanged


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 0.0, -0.25])
def test_budget_response_time_must_be_finite_and_positive(p: Profile, bad: float) -> None:
    with pytest.raises(ValueError, match="response_time_s"):
        build_read_plan(
            p, set(), GRID, max_read_registers=60, energy_auto=True, response_time_s=bad
        )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -0.01])
def test_budget_gap_must_be_finite_and_not_negative(p: Profile, bad: float) -> None:
    with pytest.raises(ValueError, match="gap_s"):
        build_read_plan(p, set(), GRID, max_read_registers=60, energy_auto=True, gap_s=bad)


def test_budget_gap_zero_is_valid_and_never_hides_an_overload(
    p: Profile, hw: HardwareProfile
) -> None:
    keys = template_selection(p, hw, GRID)
    plan = build_read_plan(p, keys, GRID, max_read_registers=60, energy_auto=True, gap_s=0.0)
    assert math.isfinite(plan.budget.utilisation) and not plan.budget.overloaded
    heavy = build_read_plan(
        p,
        keys,
        GRID,
        max_read_registers=60,
        energy_auto=True,
        gap_s=0.0,
        response_time_s=5.0,
        poll=PollConfig(fast_s=1, energy_s=1, settings_s=1, identity_s=1),
    )
    assert heavy.budget.overloaded and math.isfinite(heavy.budget.utilisation)


# -- T5-03 -----------------------------------------------------------------------------------------


def plan_for(p: Profile, keys: set[str], *, auto: bool) -> Any:
    return build_read_plan(p, keys, GRID, max_read_registers=60, energy_auto=auto)


def test_r65_is_no_longer_a_default_diagnostic(p: Profile, hw: HardwareProfile) -> None:
    assert not p.entry("dsp_protocol_version").default_enabled
    assert "dsp_protocol_version" not in template_selection(p, hw, GRID)
    assert "dsp_protocol_version" not in template_selection(p, hw, ISLAND)
    assert "R65" in (p.entry("dsp_protocol_version").notes or "")


def test_r65_flags_follow_the_actual_read_set(p: Profile) -> None:
    energy = {"battery_energy_charge_today"}
    # automatic energy without an explicit R65 choice: pulled in as a dependency
    plan = plan_for(p, energy, auto=True)
    assert plan.reads_r65 and plan.r65_required_for_energy and 65 in plan.addresses
    assert "dsp_protocol_version" in plan.keys
    # manual override: not required, not read
    plan = plan_for(p, energy, auto=False)
    assert not plan.reads_r65 and not plan.r65_required_for_energy and 65 not in plan.addresses
    # only the R65 diagnostic chosen (override or not): read, but never required for energy
    for auto in (True, False):
        plan = plan_for(p, {"dsp_protocol_version"}, auto=auto)
        assert plan.reads_r65 and not plan.r65_required_for_energy and 65 in plan.addresses
    # explicit R65 plus manual override with energy: read because chosen, not required
    plan = plan_for(p, energy | {"dsp_protocol_version"}, auto=False)
    assert plan.reads_r65 and not plan.r65_required_for_energy
    # no energy at all, auto: neither
    plan = plan_for(p, {"battery_soc"}, auto=True)
    assert not plan.reads_r65 and not plan.r65_required_for_energy
    # empty selection
    plan = plan_for(p, set(), auto=True)
    assert not plan.reads_r65 and not plan.r65_required_for_energy and 65 not in plan.addresses


def test_every_flag_agrees_with_the_blocks(p: Profile, hw: HardwareProfile) -> None:
    for keys in (
        set(),
        {"dsp_protocol_version"},
        {"pv_energy_total"},
        template_selection(p, hw, GRID),
    ):
        for auto in (True, False):
            plan = plan_for(p, set(keys), auto=auto)
            assert plan.reads_r65 == (65 in plan.addresses)
            assert plan.r65_required_for_energy <= plan.reads_r65


def test_a_missing_r65_is_never_read_as_zero(p: Profile) -> None:
    entry = p.entry("battery_energy_charge_today")
    assert decode(entry, {514: 100}, energy_resolution=None) is None
    assert decode(p.entry("dsp_protocol_version"), {}) is None


# -- T5-04 -----------------------------------------------------------------------------------------


def test_r111_is_a_raw_word_with_the_documented_hint_kept_apart(p: Profile) -> None:
    e = p.entry("battery_operating_basis")
    assert (e.type, e.release, e.unit, e.scale, e.enum, e.default_enabled) == (
        "raw",
        "unresolved",
        None,
        None,
        None,
        False,
    )
    assert "2 = no battery" in (e.notes or "")
    for code in (0, 1, 2, 3, 7, 0xFFFF):
        assert decode(e, {111: code}) == code  # the raw value, never a made-up label
    assert e.selectable and not e.is_physical  # a raw diagnostic, no physical sensor


# -- producer evidence ----------------------------------------------------------------------


def test_r621_evidence_is_version_bound_and_stays_unresolved(p: Profile) -> None:
    e = p.entry("grid_power_factor")
    assert (e.addr, e.support_status, e.release, e.default_enabled) == (
        621,
        "unverified",
        "unresolved",
        False,
    )
    by_build = {ev.mcu2: ev for ev in e.producer_evidence}
    assert by_build["1151"].effect == "zero_fill" and by_build["1151"].store_byte == 0x20438
    assert by_build["1151"].image_sha256 == SHA_1151
    copy = by_build["1175"]
    assert (copy.effect, copy.load_byte, copy.store_byte, copy.ram_word) == (
        "copy",
        0x22F8E,
        0x22F94,
        0xD674,
    )
    assert copy.image_sha256 == SHA_1175


def test_r638_and_r655_share_one_source(p: Profile) -> None:
    expected = {
        "inverter_frequency": {
            "1151": (0x2055A, 0x20560, 0xD1D2),
            "1175": (0x230B6, 0x230BC, 0xD1D8),
        },
        "load_frequency": {"1151": (0x20612, 0x20618, 0xD1D2), "1175": (0x2316E, 0x23174, 0xD1D8)},
    }
    for key, builds in expected.items():
        e = p.entry(key)
        got = {ev.mcu2: (ev.load_byte, ev.store_byte, ev.ram_word) for ev in e.producer_evidence}
        assert got == builds, key
        assert {ev.effect for ev in e.producer_evidence} == {"copy"}
    rams = {k: {ev.ram_word for ev in p.entry(k).producer_evidence} for k in expected}
    assert rams["inverter_frequency"] == rams["load_frequency"] == {0xD1D2, 0xD1D8}
    assert "no independent" in (p.entry("load_frequency").notes or "")
    assert not p.entry("load_frequency").default_enabled  # R638 stays the clearer default


def test_evidence_schema_accepts_copy_details_and_rejects_nonsense() -> None:
    from deye_monitor.profiles.schema import ProducerEvidence

    ProducerEvidence.model_validate(
        {
            "mcu2": "1175",
            "image_sha256": SHA_1175,
            "store_byte": 5,
            "effect": "copy",
            "ram_word": 0xD1D8,
            "load_byte": 3,
        }
    )
    for bad in ({"ram_word": 0x10000}, {"load_byte": -1}, {"effect": "invented"}):
        with pytest.raises(ValidationError):
            ProducerEvidence.model_validate(
                {"mcu2": "1175", "image_sha256": SHA_1175, "store_byte": 5, "effect": "copy", **bad}
            )
