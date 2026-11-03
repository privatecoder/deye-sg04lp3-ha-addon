"""0.5.0 sensors (coverage-1 sensors.json): decode goldens, derived values, preselection that
follows the installation description only, and the new questions."""

from __future__ import annotations

import pytest

from deye_monitor.device import BOOTSTRAP_KEYS
from deye_monitor.profiles.decode import decode
from deye_monitor.profiles.features import Feature, Features
from deye_monitor.profiles.hardware import load_hardware
from deye_monitor.profiles.plan import build_read_plan, template_selection
from deye_monitor.profiles.schema import Profile, load_profile
from deye_monitor.profiles.setting_presets import suggested_operations
from deye_monitor.selection import ChangeError, make_features
from deye_monitor.write_ops import BY_KEY

P: Profile = load_profile("sg04lp3_lv_v105_8")
HW = load_hardware("sg04lp3_eu")


def sel(**features: object) -> frozenset[str]:
    return template_selection(P, HW, Features(**features))  # type: ignore[arg-type]


# -- decode goldens ------------------------------------------------------------------------------


def test_gen_port_power_is_a_signed_32_bit_watt_value() -> None:
    e = P.entry("gen_power_raw_a")
    assert (e.low, e.high, e.unit, e.device_class, e.release) == (
        664,
        668,
        "W",
        "power",
        "profile_dependent",
    )
    assert decode(e, {664: 0xFFFE, 668: 0xFFFF}) == -2
    assert decode(e, {664: 0x1000, 668: 0x0001}) == 0x11000
    assert decode(e, {664: 5}) is None  # a missing partner word is unknown
    assert (P.entry("gen_power_raw_total").low, P.entry("gen_power_raw_total").high) == (667, 671)


def test_gen_energy_today_is_tenths_of_a_kwh() -> None:
    e = P.entry("gen_energy_today_r536")
    assert (e.unit, e.state_class) == ("kWh", "total_increasing")
    assert decode(e, {536: 1234}) == pytest.approx(123.4)
    for key in ("gen_energy_total_r537", "gen_runtime_total_r512", "gen_runtime_today_r539"):
        assert P.entry(key).release == "unresolved"  # source scope and resolution still open


@pytest.mark.parametrize(
    ("key", "word", "expected"),
    [
        ("external_signal_reported", 0x0000, "off"),
        ("external_signal_reported", 0x0001, "grid"),
        ("external_signal_reported", 0x0002, "gen"),
        ("external_signal_reported", 0x0003, "both"),  # an incumbent "both" is shown as such
        ("external_signal_reported", 0xFFFD, "grid"),  # the other bits do not matter
        ("external_signal_reported", 0xFFFF, None),  # the placeholder word is not a request
        ("signal_island_mode_reported", 0x2BAA, "on"),  # the owner's field word
        ("signal_island_mode_reported", 0x2AAA, "off"),
        ("signal_island_mode_reported", 0x2900, None),  # pair 01: unknown
        ("signal_island_mode_reported", 0x2800, None),  # pair 00: no action
        ("signal_island_mode_reported", 0xFFFF, None),
        ("force_off_grid_reported", 0, "auto"),
        ("force_off_grid_reported", 1, "force_off_grid"),
        ("force_off_grid_reported", 2, None),
        ("mppt_scanning_reported", 0x0064, True),
        ("mppt_scanning_reported", 0x0060, False),
        ("mppt_scanning_reported", 0xFFFF, None),
        ("drm3_reported", 0x0008, True),
        ("drm8_reported", 0x0100, True),
        ("drm0_reported", 0x0100, False),
        ("bms_force_charge", 0x0002, True),
        ("bms_sleep", 0x0008, True),
        ("bms_alarm_word", 0x0042, 0x42),
        ("bms_fault_word", 0, 0),
        ("ct_a_selfcheck_success", 1, True),
        ("ct_c_selfcheck_success", 3, False),
        ("ct_phase_sequence_result", 0b101000, 5),
        ("ct_a_direction_reversed", 0x40, True),
        ("export_mode_setting", 2, "external_zero_export"),
        ("export_mode_setting", 3, None),
    ],
)
def test_new_sensor_decode_goldens(key: str, word: int, expected: object) -> None:
    e = P.entry(key)
    assert decode(e, {e.addresses[0]: word}) == expected


def test_derived_powers() -> None:
    both = Features()
    dc = P.entry("dc_input_power_total")
    assert decode(dc, {672: 1200, 673: 800}, features=both) == 2000
    assert decode(dc, {672: 1200, 673: 800}, features=Features(mppt2_used=False)) == 1200
    assert (
        decode(dc, {672: 1200, 673: 800}, features=Features(mppt1_used=False, mppt2_used=False))
        is None
    )
    assert decode(dc, {672: 1200}, features=both) is None  # a missing word is unknown
    battery = P.entry("battery_power_derived")  # 52.00 V x -1.50 A
    assert decode(battery, {587: 5200, 591: 0xFF6A}) == -78
    assert decode(battery, {587: 5200}) is None
    bms = P.entry("bms_power_derived")  # negative product by this app's sign policy
    assert decode(bms, {215: 5120, 216: 10}) == -512
    assert decode(bms, {215: 5120, 216: 0xFFF6}) == 512


def test_derived_entries_read_only_words_of_their_own_group() -> None:
    for e in P.entries:
        if e.derive is not None:
            group_words = {
                a for o in P.entries if o.derive is None and o.group == e.group for a in o.addresses
            }
            assert set(e.addresses) <= group_words


def test_relay_grid_carries_the_off_grid_hint() -> None:
    d = P.entry("relay_grid").description
    assert d is not None
    assert "Off grid" in d.en and "not physical isolation" in d.en
    assert "Off grid" in d.de and "keine physische Trennung" in d.de


def test_no_new_sensor_exposes_a_register_number_in_its_description() -> None:
    import re

    for e in P.entries:
        if e.description is not None and e.source.find("coverage-1") >= 0:
            assert not re.search(
                r"\bR\d{2,3}\b|0x[0-9a-fA-F]+", e.description.en + e.description.de
            ), e.key


# -- preselection follows the installation description only --------------------------------------


def test_default_selection_of_the_always_relevant_reports() -> None:
    s = sel()
    # 0.5.2 (task 43): the signal report needs a described signal use, DRM and the optional
    # request reports are opt-in
    assert not {"external_signal_reported", "force_off_grid_reported"} & s
    assert not ({f"drm{i}_reported" for i in range(9)} | {"drm_status_raw"}) & s
    assert {"mppt_scanning_reported", "signal_island_mode_reported", "dc_input_power_total"} <= s
    assert {"battery_power_derived", "bms_power_derived", "bms_alarm_word", "bms_fault_word"} <= s
    assert not {"grid_monitoring_source", "meter_manufacturer_reported"} & s  # need E / M
    assert {"grid_monitoring_source"} <= sel(external_measurement="ct")
    assert "meter_manufacturer_reported" not in sel(external_measurement="ct")
    assert "meter_manufacturer_reported" in sel(external_measurement="meter")
    raw = {k for k in s if P.entry(k).release == "unresolved"}
    assert not raw & {"bms_soh_raw", "bms_software_version", "ct_ratio_reported"}


def test_signal_island_mode_follows_battery_or_off_grid() -> None:
    key = "signal_island_mode_reported"
    assert key in sel()
    assert key in sel(battery=False, bms=False, utility_grid=False, grid_port_use="unused")
    assert key not in sel(battery=False, bms=False)  # grid-tied without a battery


def test_bms_reports_need_a_battery_with_bms() -> None:
    for key in ("bms_alarm_word", "bms_fault_word", "bms_power_derived"):
        assert key in sel(battery=True, bms=True)
        assert key not in sel(battery=True, bms=False)
        assert key not in sel(battery=False)
    assert "battery_power_derived" in sel(bms=False) and "battery_power_derived" not in sel(
        battery=False
    )


def test_mppt_rows_follow_the_used_inputs() -> None:
    for key in ("dc_input_power_total", "mppt_scanning_reported"):
        assert key in sel(mppt2_used=False) and key in sel(mppt1_used=False)
        assert key not in sel(mppt1_used=False, mppt2_used=False)


def test_gen_port_rows_follow_the_gen_port_answer() -> None:
    assert "gen_power_raw_total" not in sel()
    for mode in ("generator", "smart_load", "ac_coupled"):
        assert "gen_power_raw_total" in sel(gen_port_mode=mode)
    assert "gen_energy_today_r536" in sel(gen_port_mode="generator")
    assert "gen_energy_today_r536" not in sel(gen_port_mode="smart_load")


@pytest.mark.parametrize(
    ("answer", "ct", "external"),
    [
        ("unknown", False, False),
        ("none", False, False),
        ("ct", True, True),
        ("meter", False, True),
        ("ct_and_second_meter", True, True),
        ("meter_and_second_meter", False, True),
    ],
)
def test_measurement_answer_selects_ct_checks_and_external_values(
    answer: str, ct: bool, external: bool
) -> None:
    s = sel(external_measurement=answer)
    assert ("ct_selfcheck_result_raw" in s) is ct
    assert ("grid_external_power_total" in s) is external
    # detailed per-phase and CT bit entities are optional (the aggregates stay default)
    assert not {"grid_external_current_a", "ct_a_selfcheck_success", "ct_phase_sequence_result"} & s
    assert "ct_ratio_reported" not in s and "ct_direction_bit7_raw" not in s  # raw: never


def test_measurement_answer_needs_a_grid_terminal() -> None:
    off_grid = {"utility_grid": False, "grid_port_use": "unused"}
    assert "grid_external_power_total" not in sel(external_measurement="ct", **off_grid)
    assert "ct_selfcheck_result_raw" not in sel(external_measurement="ct", **off_grid)


def test_tou_answer_selects_the_schedule_sensors() -> None:
    keys = {"tou_enable", "tou_weekdays", "tou_slot1_start", "tou_slot6_power"}
    assert not keys & sel()
    assert keys <= sel(tou_used=True)
    assert not keys & sel(tou_used=True, battery=False, bms=False)
    # the voltage targets follow a described voltage control basis; Spain mode is manual only
    assert "tou_slot3_voltage" not in sel(tou_used=True)
    assert "tou_slot3_voltage" not in sel(tou_used=True, battery_control_basis="soc")
    assert "tou_slot3_voltage" in sel(tou_used=True, battery_control_basis="voltage")
    assert "tou_spain_mode" not in sel(tou_used=True, battery_control_basis="voltage")
    assert "tou_slot1_soc_raw" not in sel(tou_used=True)  # raw words stay manual


def test_other_answers_do_not_change_the_sensor_template() -> None:
    base = sel()
    for name, value in (
        ("export_policy", "zero_export_external"),
        ("battery_control_basis", "soc"),
    ):
        assert sel(**{name: value}) == base


def test_the_signal_report_follows_a_described_signal_use() -> None:
    for use in ("grid_contactor", "generator_start", "shared_grid_and_generator_logic", "other"):
        assert "external_signal_reported" in sel(external_signal_use=use)
    for use in ("none", "unknown"):
        assert "external_signal_reported" not in sel(external_signal_use=use)
    # nothing but the description selects it (and the signal answer adds nothing else)
    assert sel(external_signal_use="grid_contactor") - sel() == {"external_signal_reported"}


def test_a_description_without_drm_selects_no_drm_entity_whatever_the_other_answers() -> None:
    drm = {f"drm{i}_reported" for i in range(9)} | {"drm_status_raw"}
    for kw in ({}, {"utility_grid": False, "grid_port_use": "unused"}, {"parallel": True}):
        assert not drm & sel(**kw)


def test_the_optional_entities_stay_selectable_and_decode() -> None:
    from deye_monitor.profiles.plan import validate_selection

    optional = {
        "mppt_count", "phase_count", "battery_input_code", "alarm_fan", "fault_f13",
        "drm_status_raw", "drm0_reported", "force_off_grid_reported", "battery_temperature",
        "grid_external_current_a", "gen_power_raw_a", "bms_sleep", "ct_a_selfcheck_success",
        "tou_spain_mode",
    }  # fmt: skip
    validate_selection(P, optional)


def test_new_questions_default_to_no_preselection() -> None:
    f = Features()
    assert (f.export_policy, f.external_measurement, f.external_signal_use) == ("unknown",) * 3
    assert (f.battery_control_basis, f.tou_used) == ("unknown", False)


def test_the_selection_parser_knows_the_new_questions() -> None:
    f = make_features(
        {"external_measurement": "ct", "tou_used": True, "battery_control_basis": "soc"}
    )
    assert (f.external_measurement, f.tou_used, f.battery_control_basis) == ("ct", True, "soc")
    for bad in ({"tou_used": "yes"}, {"export_policy": "maybe"}, {"tou_used": 1}):
        with pytest.raises(ChangeError):
            make_features(bad)
    assert {Feature.EXPORT_POLICY, Feature.TOU_USED} <= set(Feature)


# -- bus budget and read plan --------------------------------------------------------------------


def test_the_hint_reads_are_released_entries_in_the_slow_group() -> None:
    for key in (
        "export_mode_setting",
        "grid_monitoring_source",
        "external_signal_reported",
        "tou_enable",
    ):
        assert key in BOOTSTRAP_KEYS and P.entry(key).selectable
        assert P.entry(key).group == "settings"


def test_the_default_plan_stays_inside_the_bus_budget() -> None:
    f = Features()
    plan = build_read_plan(P, sel() | BOOTSTRAP_KEYS, f, max_read_registers=60, energy_auto=True)
    assert not plan.budget.overloaded and plan.budget.utilisation < 0.7
    plan = build_read_plan(
        P,
        sel(gen_port_mode="ac_coupled", external_measurement="ct", tou_used=True) | BOOTSTRAP_KEYS,
        Features(),
        max_read_registers=20,
        energy_auto=True,
    )
    assert not plan.budget.overloaded


# -- settings suggested by the description -------------------------------------------------------


def test_setting_suggestions_follow_the_answers() -> None:
    assert suggested_operations(Features()) == (
        "force_off_grid",
        "signal_island_mode",
        "mppt_scan",
        "grid_reconnection_time",
        "bms_loss_fault",
    )
    soc = suggested_operations(Features(battery_control_basis="soc"))
    assert {"battery_shutdown_soc", "grid_charge_start_soc", "grid_charge_enable"} <= set(soc)
    assert "smart_load_on_soc" not in soc and not any(k.startswith("tou_") for k in soc)
    assert "smart_load_on_soc" in suggested_operations(
        Features(battery_control_basis="soc", gen_port_mode="smart_load")
    )
    voltage = suggested_operations(
        Features(battery_control_basis="voltage", gen_port_mode="smart_load")
    )
    assert "smart_load_on_voltage" in voltage and "battery_shutdown_soc" not in voltage
    assert "tou_slot1_start" in suggested_operations(Features(tou_used=True))
    assert not any(
        k.startswith("tou_")
        for k in suggested_operations(Features(tou_used=True, battery=False, bms=False))
    )
    island = suggested_operations(
        Features(battery=False, bms=False, utility_grid=False, grid_port_use="unused")
    )
    assert island == ("force_off_grid", "signal_island_mode", "mppt_scan")
    grid_tied = suggested_operations(Features(battery=False, bms=False))
    assert grid_tied == ("mppt_scan", "grid_reconnection_time")
    assert all(k in BY_KEY for k in soc)
    assert "generator_charge_current" in suggested_operations(
        Features(gen_port_mode="generator", utility_grid=False, battery_control_basis="soc")
    )
    assert suggested_operations(
        Features(mppt1_used=False, mppt2_used=False, battery=False, bms=False)
    ) == ("grid_reconnection_time",)


def test_an_inactive_mppt_term_is_not_a_read_dependency() -> None:
    both = Features()
    single = Features(mppt2_used=False)
    entry = P.entry("dc_input_power_total")
    assert set(entry.addresses_for(both)) == set(entry.addresses)
    assert set(entry.addresses_for(single)) < set(entry.addresses)
    dropped = set(entry.addresses) - set(entry.addresses_for(single))
    plans = {
        name: build_read_plan(
            P, {"dc_input_power_total"}, f, max_read_registers=125, energy_auto=False
        )
        for name, f in (("both", both), ("single", single))
    }
    assert dropped
    needed = {name: {a for b in p.blocks for a in b.needed} for name, p in plans.items()}
    assert dropped <= needed["both"] and not dropped & needed["single"]
    assert set(plans["single"].dependencies["dc_input_power_total"]) == set(
        entry.addresses_for(single)
    )


# -- protection-word diagnostics (R82-R87, R235, R236) -------------------------------------------

PROTECTION_WORDS = {
    "self_check_time_setting": 82,
    "anti_islanding_enable_raw": 83,
    "reactive_pf_option_raw": 84,
    "rcd_enable_raw": 86,
    "riso_enable_raw": 87,
    "track_grid_phase_raw": 235,
    "it_system_raw": 236,
}


@pytest.mark.parametrize("key", sorted(PROTECTION_WORDS))
@pytest.mark.parametrize(
    ("word", "shown"), [(0, "0000"), (1, "0001"), (0xFFFF, "FFFF"), (0x1234, "1234")]
)
def test_protection_words_are_raw_hex_never_a_boolean(key: str, word: int, shown: str) -> None:
    e = P.entry(key)
    assert e.addr == PROTECTION_WORDS[key]
    assert (e.type, e.display, e.platform) == ("raw", "hex", "sensor")
    assert not e.declares_not_reported  # 0xFFFF is shown, not hidden
    assert decode(e, {e.addr: word}) == shown


@pytest.mark.parametrize("key", sorted(PROTECTION_WORDS))
def test_protection_words_are_default_off_diagnostics_with_help(key: str) -> None:
    from deye_monitor.help_texts import sensor_help

    e = P.entry(key)
    assert (e.default_enabled, e.category, e.entity_category) == (False, "diagnostic", "diagnostic")
    assert key not in sel() and key not in sel(battery=True, bms=True)
    help_ = sensor_help(key)
    assert help_ is not None and help_["de"] != help_["en"]


def test_protection_word_help_states_the_evidence_not_a_health_claim() -> None:
    from deye_monitor.help_texts import sensor_help

    r83 = sensor_help("anti_islanding_enable_raw")
    r86 = sensor_help("rcd_enable_raw")
    r235 = sensor_help("track_grid_phase_raw")
    assert r83 and r86 and r235
    assert (
        "exactly 1" in r83["en"]
        and "not enabled" in r83["en"]
        and "not a health value" in r83["en"]
    )
    assert "zero from nonzero" in r86["en"] and "not a healthy" in r86["en"]
    assert "default" in r235["en"] and "grid synchronization is unaffected" in r235["en"]
    assert "genau 1" in r83["de"] and "nicht null" in r86["de"]


def test_protection_words_are_planned_and_stay_in_budget() -> None:
    keys = sel() | BOOTSTRAP_KEYS | set(PROTECTION_WORDS)
    plan = build_read_plan(P, keys, Features(), max_read_registers=60, energy_auto=True)
    needed = {a for k in PROTECTION_WORDS for a in P.entry(k).addresses}
    planned = {a for b in plan.blocks for a in range(b.start, b.start + b.count)}
    assert needed <= planned
    assert not plan.budget.overloaded and plan.budget.utilisation < 0.7
    baseline = build_read_plan(
        P, sel() | BOOTSTRAP_KEYS, Features(), max_read_registers=60, energy_auto=True
    )
    assert plan.budget.utilisation >= baseline.budget.utilisation


def test_the_grid_telemetry_of_the_zero_only_hint_policy_is_an_exact_key_set() -> None:
    """Task 44: web/static/common.js hides the zero-only prompt for exactly these keys while GRID
    has a described source. Only the first five are zero-watched; the rest must stay unwatched."""
    import re
    from pathlib import Path

    js = (Path(__file__).parents[1] / "src/deye_monitor/web/static/common.js").read_text()
    block = js[js.index("const GRID_TELEMETRY") : js.index("]);", js.index("const GRID_TELEMETRY"))]
    keys = re.findall(r"'([a-z_]+)'", block)
    assert len(keys) == 24 and len(set(keys)) == 24
    watched = {
        "grid_voltage_a",
        "grid_voltage_b",
        "grid_voltage_c",
        "grid_line_voltage_ca",
        "grid_frequency",
    }
    for key in keys:
        assert P.entry(key).observes_zero is (key in watched), key
