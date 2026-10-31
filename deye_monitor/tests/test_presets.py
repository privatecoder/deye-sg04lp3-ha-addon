"""Template presets for every wizard combination (preset design, plus the owner's overrides)."""

from __future__ import annotations

import itertools

import pytest

from deye_monitor.profiles.features import FEATURE_VALUES, Features
from deye_monitor.profiles.hardware import HardwareProfile, load_hardware
from deye_monitor.profiles.plan import template_selection
from deye_monitor.profiles.schema import Profile, load_profile

RAW_WORDS = {
    "alarm_word_1",
    "alarm_word_2",
    "fault_word_1",
    "fault_word_2",
    "fault_word_3",
    "fault_word_4",
}
ALARM_BITS = {
    "alarm_fan",
    "alarm_grid_phase",
    "alarm_battery_missing",
    "alarm_parallel_comm",
    "fault_f13",
    "fault_f29",
    "fault_f41",
}
BMS_ON = {
    "bms_soc",
    "bms_temperature",
    "bms_voltage",
    "bms_current",
    "bms_charge_current_limit",
    "bms_discharge_current_limit",
    "bms_charge_voltage",
}
BMS_OFF = {"bms_discharge_voltage", "bms_offgrid_charge_limit", "bms_offgrid_discharge_limit_raw"}
NEVER = RAW_WORDS | BMS_OFF | {"ups_power_total", "ups_power_a", "inverter_voltage_a"}


@pytest.fixture(scope="module")
def p() -> Profile:
    return load_profile("sg04lp3_lv_v105_8")


@pytest.fixture(scope="module")
def hw() -> HardwareProfile:
    return load_hardware("sg04lp3_eu")


def combinations() -> list[Features]:
    names = list(FEATURE_VALUES)
    return [
        Features.model_validate(dict(zip((n.value for n in names), values, strict=True)))
        for values in itertools.product(*(FEATURE_VALUES[n] for n in names))
    ]


def expected(f: Features) -> dict[str, bool]:
    """The documented rule per entry, written down independently of the profile."""
    battery_no_bms = f.battery and not f.bms
    battery_bms = f.battery and f.bms
    terminal = f.grid_port_use in ("utility", "generator")
    utility = f.utility_grid and f.grid_port_use == "utility"
    out = {k: True for k in ALARM_BITS}
    out |= {k: False for k in NEVER}
    out |= {k: battery_bms for k in BMS_ON}
    out |= {"battery_soc": battery_no_bms, "battery_temperature": battery_no_bms}
    out |= {"battery_voltage": f.battery, "battery_current": f.battery}
    out |= {k: terminal for k in ("grid_voltage_a", "grid_voltage_b", "grid_voltage_c")}
    out["grid_frequency"] = terminal
    out["grid_selected_power_total"] = utility
    out["grid_internal_power_total"] = f.grid_port_use == "generator"
    out |= {k: True for k in ("load_voltage_a", "load_voltage_b", "load_voltage_c")}
    out |= {"load_power_total": True, "inverter_power_total": True}
    out["inverter_frequency"] = f.grid_port_use in ("unused", "generator")
    out |= {
        k: utility
        for k in (
            "grid_energy_import_today",
            "grid_energy_export_today",
            "grid_energy_import_total",
            "grid_energy_export_total",
        )
    }
    out |= {"parallel_master": f.parallel, "parallel_sn": f.parallel, "parallel_enable": True}
    return out


def test_the_template_follows_the_documented_rules_for_every_combination(
    p: Profile, hw: HardwareProfile
) -> None:
    for f in combinations():
        template = template_selection(p, hw, f)
        for key, want in expected(f).items():
            assert (key in template) is want, (key, f)


def test_the_bms_answer_selects_one_source_for_soc_and_temperature(
    p: Profile, hw: HardwareProfile
) -> None:
    with_bms = template_selection(p, hw, Features(bms=True))
    assert with_bms >= BMS_ON and not with_bms & BMS_OFF
    assert not with_bms & {"battery_soc", "battery_temperature"}
    # the inverter's own measurements stay: slaves need them
    assert {"battery_voltage", "battery_current", "battery_energy_charge_total"} <= with_bms
    without = template_selection(p, hw, Features(bms=False))
    assert {"battery_soc", "battery_temperature"} <= without and not without & BMS_ON


def test_the_island_selects_load_and_inverter_output_but_not_ups(
    p: Profile, hw: HardwareProfile
) -> None:
    island = template_selection(
        p, hw, Features(utility_grid=False, grid_port_use="unused", parallel=True)
    )
    assert {"load_power_total", "load_voltage_a", "inverter_power_total"} <= island
    assert not island & {"ups_power_total", "ups_power_a", "ups_power_b", "ups_power_c"}


def test_the_raw_words_are_off_but_the_alarm_bits_and_decoded_views_stay(
    p: Profile, hw: HardwareProfile
) -> None:
    template = template_selection(p, hw, Features())
    assert not template & RAW_WORDS
    assert template >= ALARM_BITS and {"active_warnings", "active_faults"} <= template
    for key in RAW_WORDS | BMS_OFF | {"ups_power_total"}:
        assert p.entry(key).selectable  # still selectable by hand
