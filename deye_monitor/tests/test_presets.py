"""Template presets for every wizard combination (preset design, plus the owner's overrides)."""

from __future__ import annotations

import itertools

import pytest

from deye_monitor.profiles.features import FEATURE_VALUES, Features
from deye_monitor.profiles.hardware import HardwareProfile, load_hardware
from deye_monitor.profiles.plan import template_selection
from deye_monitor.profiles.schema import Profile, load_profile
from deye_monitor.profiles.setting_presets import suggested_operations

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


BASE_FEATURES = tuple(list(FEATURE_VALUES)[:8])  # the 0.4 plant features; the 0.5.0 questions
# (export policy, measurement, signal use, control basis, ToU) have their own tests below


def combinations() -> list[Features]:
    names = list(BASE_FEATURES)
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
    out["grid_internal_power_total"] = terminal
    out["relay_grid"] = terminal
    out |= {k: True for k in ("load_voltage_a", "load_voltage_b", "load_voltage_c")}
    out |= {"load_power_total": True, "inverter_power_total": True}
    out["inverter_frequency"] = bool(f.battery) or f.grid_port_use in ("unused", "generator")
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
    for key in RAW_WORDS | {"bms_discharge_voltage", "ups_power_total"}:
        assert p.entry(key).selectable  # still selectable by hand
    for key in ("bms_offgrid_charge_limit", "bms_offgrid_discharge_limit_raw", "load_frequency"):
        assert not p.entry(key).selectable  # firmware placeholders and duplicates: not offered
    assert not p.entry("temp_dc_transformer").selectable


def _ops(**kw: object) -> set[str]:
    base: dict[str, object] = {"battery": True, "battery_control_basis": "soc"}
    return set(suggested_operations(Features(**{**base, **kw})))  # type: ignore[arg-type]


def test_a_generator_on_the_grid_terminal_suggests_the_grid_terminal_charging_settings() -> None:
    ops = _ops(utility_grid=False, grid_port_use="generator", gen_port_mode="unused")
    assert {"grid_charge_enable", "grid_charge_current", "grid_charge_start_soc"} <= ops
    assert "generator_charge_current" not in ops


def test_a_generator_on_the_gen_terminal_suggests_r125_only() -> None:
    ops = _ops(utility_grid=False, grid_port_use="unused", gen_port_mode="generator")
    assert "generator_charge_current" in ops
    assert not ops & {"grid_charge_enable", "grid_charge_current", "grid_charge_start_soc"}


def test_no_ac_source_suggests_no_charging_source_settings() -> None:
    ops = _ops(utility_grid=False, grid_port_use="unused", gen_port_mode="unused")
    assert not ops & {
        "grid_charge_enable",
        "grid_charge_current",
        "grid_charge_start_soc",
        "generator_charge_current",
    }


# -- 0.5.2: GRID source wording, presets and sensor defaults (design 41) ------------------------

CHARGING_ONLY = {
    "utility_grid": True,
    "grid_port_use": "utility",
    "battery": True,
    "bms": True,
    "battery_control_basis": "soc",
    "gen_port_mode": "smart_load",
    "mppt1_used": True,
    "export_policy": "zero_export_load",
}


def _suggest(**over: object) -> tuple[str, ...]:
    return suggested_operations(Features.model_validate({**CHARGING_ONLY, **over}))


@pytest.mark.parametrize("signal", ["none", "grid_contactor", "other", "unknown"])
def test_the_signal_answer_never_changes_the_grid_suggestions(signal: str) -> None:
    base = set(_suggest(external_signal_use="none"))
    got = set(_suggest(external_signal_use=signal))
    assert got - {"external_signal"} == base - {"external_signal"}
    assert {"grid_reconnection_time", "grid_charge_enable", "force_off_grid"} <= got


def test_the_charging_only_island_suggests_grid_charging_and_never_generator_charging() -> None:
    got = set(_suggest(export_limit_w=0))
    assert {"grid_reconnection_time", "grid_charge_enable", "grid_charge_start_soc"} <= got
    assert {"force_off_grid", "signal_island_mode", "smart_load_force"} <= got
    assert not got & {"generator_charge_enable", "generator_charge_soc"}
    assert not got & {"export_max_power", "pv_sale_max_power", "pv_sale_enable"}


def test_solar_sell_needs_a_confirmed_positive_allowance() -> None:
    assert "pv_sale_enable" not in _suggest()  # unanswered
    assert "pv_sale_enable" not in _suggest(export_limit_w=0)
    assert "pv_sale_enable" in _suggest(export_limit_w=1000)


def test_a_zero_allowance_suggests_no_sale_power_rows_for_selling_first() -> None:
    allowed = {"export_policy": "export_allowed"}
    assert {"export_max_power", "pv_sale_max_power"} <= set(_suggest(**allowed, export_limit_w=500))
    assert not set(_suggest(**allowed, export_limit_w=0)) & {
        "export_max_power",
        "pv_sale_max_power",
    }


def test_the_template_deltas_of_a_battery_utility_plant(p: Profile, hw: HardwareProfile) -> None:
    f = Features.model_validate(CHARGING_ONLY)
    template = template_selection(p, hw, f)
    assert {"inverter_frequency", "relay_grid", "grid_internal_power_total"} <= template
    assert {"grid_energy_export_today", "grid_energy_export_total"} <= template
    assert "load_frequency" not in template
    off = template_selection(
        p, hw, f.model_copy(update={"grid_port_use": "unused", "utility_grid": False})
    )
    assert "relay_grid" not in off and "grid_internal_power_total" not in off
    assert "inverter_frequency" in off


def test_the_signal_answer_adds_no_sensor(p: Profile, hw: HardwareProfile) -> None:
    f = Features.model_validate(CHARGING_ONLY)
    a = template_selection(p, hw, f.model_copy(update={"external_signal_use": "none"}))
    b = template_selection(p, hw, f.model_copy(update={"external_signal_use": "grid_contactor"}))
    assert a == b


def test_the_grid_and_ev_texts_use_the_design_41_wording() -> None:
    from pathlib import Path

    js = (Path(__file__).parents[1] / "src/deye_monitor/web/static/i18n.js").read_text(
        encoding="utf-8"
    )
    for needle in (
        "Welche Quelle kann am GRID-Anschluss anliegen?",
        "What can supply the GRID port?",
        "Öffentliches Stromnetz – dauerhaft oder zeitweise",  # noqa: RUF001
        "zeitweise über ein Schütz",
        "Nichts – GRID bleibt ungenutzt",  # noqa: RUF001
        "Nothing – GRID unused",  # noqa: RUF001
        "Was steuert der externe Signalausgang dieses Wechselrichters?",
        "Kein vom Wechselrichter gesteuerter Fahrzeuglader",
        "directly with the inverter",
    ):
        assert needle in js, needle
