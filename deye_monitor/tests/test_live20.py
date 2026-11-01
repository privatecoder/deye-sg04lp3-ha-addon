"""Owner collection round 0.3.2: sensors not offered, picker notices, the R0 family words."""

from __future__ import annotations

from deye_monitor.profiles.hardware import capability_conflicts, load_hardware
from deye_monitor.profiles.schema import load_profile
from deye_monitor.webapi import catalog_view

PROFILE = load_profile("sg04lp3_lv_v105_8")
NOT_OFFERED = (
    "temp_dc_transformer",
    "bms_offgrid_charge_limit",
    "bms_offgrid_discharge_limit_raw",
    "load_frequency",
)


def test_firmware_placeholders_and_the_frequency_duplicate_are_not_offered() -> None:
    offered = {e["key"] for e in catalog_view(PROFILE)["entries"]}
    for key in NOT_OFFERED:
        entry = PROFILE.entry(key)
        assert not entry.selectable and not entry.default_enabled and key not in offered, key
        assert entry.notes and "Not offered" in entry.notes, key
    assert PROFILE.entry("inverter_frequency").selectable


def test_the_picker_notices_are_on_the_documented_entries_only() -> None:
    views = {e["key"]: e for e in catalog_view(PROFILE)["entries"]}
    with_notice = {k: v["notice"] for k, v in views.items() if v["notice"]}
    assert with_notice == {
        "bms_discharge_voltage": "bms_protocol",
        "grid_selected_power_a": "grid_copy",
        "grid_selected_power_b": "grid_copy",
        "grid_selected_power_c": "grid_copy",
        "grid_selected_power_total": "grid_copy",
    }
    assert PROFILE.entry("bms_discharge_voltage").addr == 211
    assert PROFILE.entry("grid_selected_power_total").addresses == (625, 690)


def test_r0_is_one_family_with_two_equal_words() -> None:
    hw = load_hardware("sg04lp3_eu")
    assert hw.device_types == [0x0500, 0x0005]
    assert all(capability_conflicts(hw, {0: word}) == [] for word in hw.device_types)
    conflict = capability_conflicts(hw, {0: 0x0006})[0]
    assert conflict.code == "device_type" and "expected 0x0500 or 0x0005" in conflict.detail
