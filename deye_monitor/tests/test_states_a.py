"""Plain-language states, part A (mapping rows 0-99): every branch of the rules."""

from __future__ import annotations

import pytest

from deye_monitor.profiles.schema import load_profile
from deye_monitor.profiles.state_rules import a as rules_a
from deye_monitor.profiles.states import STATES, present

P = load_profile("sg04lp3_lv_v105_8")
KEYS_A = (
    "device_type",
    "id_device_type",
    "modbus_address",
    "protocol_version",
    "hw_platform",
    "id_hw_platform",
    "id_fw_control_r11",
    "id_gap_r12",
    "id_gap_r13",
    "id_gap_r16",
    "fw_main_version",
    "id_fw_main_version",
    "fw_comm_version",
    "mppt_count",
    "phase_count",
    "id_r22",
    "model_selector",
    "battery_input_code",
    "id_battery_input_code",
    "output_phase_arrangement",
    "grid_standard_region",
    "fan_config",
    "inverter_generation",
    "parallel_enable",
    "parallel_master",
    "parallel_phase",
    "parallel_sn",
    "operating_state",
    "power_switch",
    "relay_inverter",
    "relay_grid",
    "relay_generator",
    "relay_grid_supply",
    "dry_contact_1",
    "dry_contact_2",
    "relay_state_raw",
    "alarm_word_1",
    "alarm_word_2",
    "fault_word_1",
    "fault_word_2",
    "fault_word_3",
    "fault_word_4",
    "alarm_fan",
    "alarm_grid_phase",
    "alarm_battery_missing",
    "alarm_parallel_comm",
    "alarm_meter_comm",
    "active_warnings",
    "active_faults",
    "fault_f13",
    "fault_f29",
    "fault_f41",
    "battery_power_raw",
    "battery2_current",
    "grid_power_factor",
    "inverter_apparent_r637",
    "load_apparent_r654",
    "ac_solar_power_raw_a",
    "ac_solar_power_raw_b",
    "ac_solar_power_raw_c",
    "ev_charge_power_r709",
    "pv_ext_r722",
    "pv_ext_r723",
    "pv_ext_r724",
    "pv_ext_r725",
    "pv_ext_r726",
    "pv_ext_r727",
    "pv_ext_r728",
    "pv_ext_r729",
    "pv_ext_r730",
    "pv_ext_r731",
    "pv_ext_r732",
)


def state(key: str, *words: int) -> str | None:
    entry = P.entry(key)
    p = present(entry, dict(zip(entry.addresses, words, strict=True)))
    return p.label.en if p else None


def state_de(key: str, *words: int) -> str | None:
    entry = P.entry(key)
    p = present(entry, dict(zip(entry.addresses, words, strict=True)))
    return p.label.de if p else None


def test_every_row_is_registered_once() -> None:
    assert len(KEYS_A) == len(set(KEYS_A)) == 72
    assert [k for k in KEYS_A if k not in STATES] == []


def test_mode_attributes_exactly_for_binary_and_unit_entries() -> None:
    for key in KEYS_A:
        e = P.entry(key)
        keeps_value = e.platform == "binary_sensor" or bool(e.unit or e.device_class)
        assert (STATES[key].mode == "attributes") == (keeps_value and e.device_class != "enum"), key


@pytest.mark.parametrize("key", KEYS_A)
def test_missing_word_is_unavailable_and_attributes_carry_the_raw_word(key: str) -> None:
    e = P.entry(key)
    assert present(e, {}) is None
    words = {a: 0 for a in e.addresses}
    p = present(e, words)
    assert p is not None
    assert p.attributes["raw_decimal"] == [0] * len(e.addresses)
    assert p.attributes["raw_hex"] == ["0x0000"] * len(e.addresses)
    assert p.attributes["registers"] == list(e.addresses)
    assert p.attributes["state_de"] == p.label.de


@pytest.mark.parametrize("key", [k for k in KEYS_A if STATES[k].options])
def test_enum_options_cover_every_label(key: str) -> None:
    e = P.entry(key)
    options = {o.en for o in STATES[key].options or ()}
    seen = set()
    for word in (0, 1, 2, 3, 4, 5, 0x0500, 0x0100, 512, 768, 1250, 0xFFFF, 7, 99, 1280):
        words = {a: word for a in e.addresses}
        p = present(e, words)
        assert p is not None
        seen.add(p.label.en)
    assert seen <= options


def test_device_type_and_family() -> None:
    assert state("device_type", 512) == "String inverter"
    assert state("device_type", 5) == state("device_type", 1280) == "Three-phase LV hybrid"
    assert state("device_type", 1536) == state("device_type", 6) == "Three-phase HV hybrid"
    assert state("device_type", 1537) == "Three-phase HV hybrid (20–50 kW)"  # noqa: RUF001
    assert state_de("device_type", 1537) == "Dreiphasiger HV-Hybrid (20–50 kW)"  # noqa: RUF001
    assert state("device_type", 9999) == "Unknown"
    assert state("id_device_type", 0x0500) == state("id_device_type", 5) == "Three-phase LV hybrid"
    assert state("id_device_type", 7) == "Other device family / unknown"
    assert state_de("device_type", 2304) == "Balkonspeicher"


def test_identifiers() -> None:
    assert state("modbus_address", 1) == "Address 1"
    assert state("modbus_address", 0) == "Unknown"
    assert state("modbus_address", 300) == "Unknown"
    assert state("parallel_sn", 3 << 10) == "Node 3"
    assert state("parallel_sn", 0) == "Unknown"
    assert state("protocol_version", 0x0A1B) == "Version/ID: 0A1B"
    assert state_de("protocol_version", 0x0A1B) == "Version/Kennung: 0A1B"
    assert state("fw_comm_version", 0x1111, 0x2222) == "Version/ID: 1111-2222"
    assert state("id_gap_r12", 0x0102) == "Module version reported"
    assert state("id_gap_r12", 0) == state("id_gap_r12", 0xFFFF) == "Module status unknown"
    assert state("id_gap_r13", 5) == "Unconfirmed"
    assert state("pv_ext_r730", 0xFFFF) == "Unconfirmed"


def test_counts_and_codes() -> None:
    assert state("mppt_count", 0x0200) == "2 MPPT"
    assert state("mppt_count", 0xFF00) == "Not reported"
    assert state("phase_count", 0x0003) == "3 phases"
    assert state_de("phase_count", 0x00FF) == "Nicht gemeldet"
    assert state("battery_input_code", 0) == state("battery_input_code", 1) == "1 battery input"
    assert state("battery_input_code", 2) == "2 battery inputs (documented)"
    assert state("id_battery_input_code", 5) == "Unmapped"
    assert state("output_phase_arrangement", 1) == "Split phase"
    assert state("grid_standard_region", 2) == "JP"
    assert state("grid_standard_region", 3) == "Unknown"


def test_topology_and_status() -> None:
    assert state("parallel_enable", 0) == "Standalone"
    assert state("parallel_enable", 1) == "Parallel"
    assert state("parallel_master", 0) == "Standalone"
    assert state("parallel_master", 3) == "Master"
    assert state("parallel_master", 1) == "Slave"
    assert state("parallel_phase", 0x100) == "Three-phase"
    assert state("operating_state", 2) == "Normal (reported)"
    assert state("operating_state", 9) == "Unknown"
    assert state("power_switch", 0) == "Off"
    assert state("power_switch", 1) == "On"
    assert state("power_switch", 0xF) == "Unknown"


def test_relays() -> None:
    assert state("relay_inverter", 0) == "Off"
    assert state("relay_inverter", 1) == "On"
    assert state("relay_grid", 0x4) == "On"
    assert state("relay_grid", 0x1) == "Off"
    assert state("dry_contact_2", 0x100) == "On"
    assert state("relay_state_raw", 0) == "All reported relays off"
    assert state("relay_state_raw", 0x1 | 0x4) == "On: inverter/grid"
    assert state_de("relay_state_raw", 0x80) == "An: Kontakt 1"
    assert state("relay_state_raw", 0x2) == "Additional reported bits"
    assert state("relay_state_raw", 0x3) == "On: inverter + Additional reported bits"
    p = present(P.entry("relay_state_raw"), {552: 0x3})
    assert p is not None and p.attributes["unknown_bits"] == "0x0002"


def test_alarm_words() -> None:
    assert state("alarm_word_1", 0) == "None reported"
    assert state_de("alarm_word_1", 0) == "Keine gemeldet"
    assert state("alarm_word_1", 0b10) == "W2 Fan warning"
    assert state("alarm_word_2", 1 << 14) == "W31 Battery communication warning"
    fault = state("fault_word_2", 1 << 12)
    assert fault is not None and fault.startswith("F29")
    assert state("alarm_word_1", 1 << 15) == "Unknown report"
    assert state("alarm_word_1", 0b11) == "W1, W2 Fan warning"
    p = present(P.entry("alarm_word_1"), {553: 0b10})
    assert p is not None
    assert p.attributes["active_codes"] == ["W2"]
    assert p.attributes["unknown_bits"] == []


def test_aggregates_and_single_reports() -> None:
    assert state("active_warnings", 0, 0) == "None reported"
    warnings = state("active_warnings", 0b10, 1 << 14)
    assert warnings is not None and warnings.startswith("W2 Fan warning, W31")
    p = present(P.entry("active_faults"), {555: 0, 556: 1 << 12, 557: 0, 558: 0})
    assert p is not None
    assert p.attributes["active_codes"] == ["F29"]
    assert p.attributes["alarms"][0]["code"] == "F29"
    assert state("active_faults", 0, 0, 0, 0) == "None reported"
    assert state("alarm_fan", 0) == "Not reported"
    assert state_de("alarm_fan", 2) == "Gemeldet"
    assert state("alarm_meter_comm", 8) == "Reported"
    assert state("fault_f13", 0x1000) == "Reported"
    assert state("fault_f41", 0) == "Not reported"


def test_placeholders() -> None:
    n = len(P.entry("inverter_apparent_r637").addresses)
    assert state("inverter_apparent_r637", *([0] * n)) == "Not provided"
    assert state("inverter_apparent_r637", *([5] * n)) == "Unconfirmed value"
    assert state_de(
        "load_apparent_r654", *([0] * len(P.entry("load_apparent_r654").addresses))
    ) == ("Nicht bereitgestellt")
    assert state("ac_solar_power_raw_a", 25) == "AC-Solar: magnitude 250 W"
    assert state_de("ac_solar_power_raw_b", 0) == "AC-Solar: Betrag 0 W"


def test_module_registers_rules_only_through_the_public_api() -> None:
    assert rules_a.NOT_PROVIDED.en == "Not provided"
