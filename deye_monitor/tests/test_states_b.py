"""Plain-language primary states, part B (Review33 mapping rows 100..199)."""

from __future__ import annotations

import pytest

from deye_monitor.profiles.schema import Entry, load_profile
from deye_monitor.profiles.state_rules.b import B_KEYS
from deye_monitor.profiles.states import STATES, present

P = load_profile("sg04lp3_lv_v105_8")


def en(key: str, **words: int) -> str | None:
    entry = P.entry(key)
    got = present(entry, {a: w for a, w in zip(entry.addresses, words.values(), strict=True)})
    return None if got is None else got.label.en


def label(key: str, word: int) -> str | None:
    entry = P.entry(key)
    got = present(entry, {entry.addresses[0]: word})
    return None if got is None else got.label.en


def test_every_row_of_part_b_has_a_rule() -> None:
    assert len(B_KEYS) == 100
    assert all(k in STATES for k in B_KEYS)


def _drm(word: int, r178: int | None) -> tuple[str, dict[str, object]]:
    entry = P.entry("drm_status_raw")
    words = {544: word} if r178 is None else {544: word, 178: r178}
    got = present(entry, words)
    assert got is not None
    return got.label.en, got.attributes


@pytest.mark.parametrize(
    ("word", "r178", "expected"),
    [
        (0, 0x2FAA, "DRM: Off (setting)"),  # the owner's reading: setting Off, nothing reported
        (0b101, 0x2FAA, "DRM: Off (setting)"),  # the discrepancy stays in the attributes
        (0, 0x3000, "No DRM flags reported"),
        (0b1001, 0x3000, "Reported: DRM0/DRM3"),
        (0x200, 0x3000, "Additional DRM flags reported"),
        (0x201, 0x3000, "Reported: DRM0 + additional flags"),
        (
            0xFFFF,
            0x3000,
            "Reported: DRM0/DRM1/DRM2/DRM3/DRM4/DRM5/DRM6/DRM7/DRM8 + additional flags",
        ),
        (0, 0x1000, "DRM enable state unknown"),  # pair 1 is no explicit setting
        (0b1, None, "DRM enable state unknown"),  # R178 absent or stale
    ],
)
def test_the_drm_aggregate_is_qualified_by_the_setting(
    word: int, r178: int | None, expected: str
) -> None:
    text, attrs = _drm(word, r178)
    assert text == expected
    assert attrs["unknown_bits"] == word & 0xFE00
    assert attrs["reported_modes"] == [f"DRM{i}" for i in range(9) if word >> i & 1]


def test_each_drm_bit_maps_to_its_zero_based_mask() -> None:
    for n in range(9):
        assert P.entry(f"drm{n}_reported").mask == 1 << n
        assert f"bit {n}," in (P.entry(f"drm{n}_reported").notes or "")


def test_a_missing_r544_is_unavailable_not_no_drm() -> None:
    assert present(P.entry("drm_status_raw"), {178: 0x3000}) is None


def test_missing_word_is_unavailable_not_a_label() -> None:
    assert present(P.entry("anti_islanding_enable_raw"), {}) is None


def test_fixed_option_lists_contain_every_label() -> None:
    for key in B_KEYS:
        spec = STATES[key]
        entry = P.entry(key)
        if spec.options is None or spec.mode != "text":
            continue
        shown = {o.en for o in spec.options}
        for word in (0, 1, 2, 3, 4, 5, 7, 100, 0xFFFF, 0x0100, 0x0200, 0x0300, 0x0004):
            got = present(entry, {entry.addresses[0]: word})
            assert got is not None and got.label.en in shown, (key, word)


def test_attributes_carry_the_raw_word_and_german_label() -> None:
    got = present(P.entry("rcd_enable_raw"), {86: 0xFFFF})
    assert got is not None
    assert got.attributes["raw_hex"] == ["0xFFFF"]
    assert got.attributes["raw_decimal"] == [65535]
    assert got.attributes["registers"] == [86]
    assert got.attributes["state_de"] == "Interner RCD: An"
    assert got.attributes["scope"] == "C070/1175"


@pytest.mark.parametrize(
    ("key", "word", "expected"),
    [
        ("anti_islanding_enable_raw", 1, "Anti-Island Mode: On"),
        ("anti_islanding_enable_raw", 0, "Anti-Island Mode: Off"),
        ("anti_islanding_enable_raw", 0xFFFF, "Anti-Island Mode: Off"),
        ("rcd_enable_raw", 0, "Internal RCD: Off"),
        ("rcd_enable_raw", 1, "Internal RCD: On"),
        ("rcd_enable_raw", 0xFFFF, "Internal RCD: On"),  # R86 differs from R83
        ("track_grid_phase_raw", 0, "Track grid phase: Off (default)"),
        ("track_grid_phase_raw", 1, "Track grid phase: On"),
        ("track_grid_phase_raw", 0xFFFF, "Track grid phase: Off"),
        ("riso_enable_raw", 1, "Insulation check (Riso): On"),
        ("riso_enable_raw", 0xFFFF, "Not set (effect unknown)"),
        ("riso_enable_raw", 7, "Unknown"),
        ("reactive_pf_option_raw", 1, "PF"),
        ("reactive_pf_option_raw", 2, "Q"),
        ("reactive_pf_option_raw", 0xFFFF, "Not set (effect unknown)"),
        ("it_system_raw", 1, "On"),
        ("it_system_raw", 0xFFFF, "Unknown"),  # R236 is never "Not set"
        ("it_system_raw", 5, "Unknown"),
        ("self_check_time_setting", 0xFFFF, "Not set (effect unknown)"),
        ("self_check_time_setting", 60, "Delay interpretation unconfirmed"),
        ("self_check_time_setting", 1000, "Unknown value"),
    ],
)
def test_setting_words(key: str, word: int, expected: str) -> None:
    assert label(key, word) == expected


@pytest.mark.parametrize(
    ("key", "word", "expected"),
    [
        ("battery_operating_basis", 0, "Voltage"),
        ("battery_operating_basis", 1, "SOC"),
        ("battery_operating_basis", 2, "No Battery"),
        ("battery_operating_basis", 3, "Unknown"),
        ("export_mode_setting", 0, "Selling First"),
        ("export_mode_setting", 1, "Zero Export to Load"),
        ("export_mode_setting", 2, "Zero Export to CT"),
        ("export_mode_setting", 3, "Unknown"),
        ("gen_port_usage", 2, "AC coupling"),
        ("remote_run_enable", 0, "Off"),
        ("battery_chemistry", 1, "Lithium/BMS"),
        ("force_off_grid_reported", 0, "Auto"),
        ("force_off_grid_reported", 1, "On"),
        ("force_off_grid_reported", 2, "Unknown"),
        ("external_signal_reported", 1, "Grid Signal"),
        ("external_signal_reported", 2, "Gen Signal"),
        ("external_signal_reported", 3, "Both requests"),
        ("external_signal_reported", 0xFFFF, "Unknown"),
        ("signal_island_mode_reported", 0x0200, "Off"),
        ("signal_island_mode_reported", 0x0300, "On"),
        ("signal_island_mode_reported", 0x0100, "Unknown"),
        ("signal_island_mode_reported", 0xFFFF, "Unknown"),
        ("grid_monitoring_source", 0, "CT"),
        ("grid_monitoring_source", 1, "Meter"),
        ("meter_manufacturer_reported", 1, "Eastron"),
        ("meter_manufacturer_reported", 4, "Documented selection: CHNT / Deye-3P / AccuEnergy"),
        ("meter_manufacturer_reported", 9, "Unknown"),
        ("ct_ratio_reported", 2000, "2000:1"),
        ("ct_ratio_reported", 250, "250:1 (outside editor range)"),
        ("ct_ratio_reported", 0xFFFF, "Unknown"),
        ("communication_address_r74", 3, "Address 3"),
        ("communication_address_r74", 0, "Unknown"),
    ],
)
def test_report_words(key: str, word: int, expected: str) -> None:
    assert label(key, word) == expected


def test_unresolved_words_are_unconfirmed_including_r335() -> None:
    for key in ("parallel_balancing_stop_soc_raw", "parallel_config_2_raw", "bms_soh_raw"):
        assert label(key, 0) == "Unconfirmed"
        assert label(key, 0xFFFF) == "Unconfirmed"


@pytest.mark.parametrize(
    ("key", "word", "expected"),
    [
        ("bms_alarm_word", 0, "No alarm reported"),
        ("bms_alarm_word", 4, "Alarm reported"),
        ("bms_fault_word", 0, "No fault reported"),
        ("bms_fault_word", 1, "Fault reported"),
        ("bms_flags_raw", 0, "No known status flags"),
        ("bms_flags_raw", 0b0010, "Reported: Force Charge"),
        ("bms_flags_raw", 0b1000, "Reported: Sleep"),
        ("bms_flags_raw", 0b1010, "Reported: Force Charge and Sleep"),
        ("bms_flags_raw", 0x0100, "No known status flags"),  # high byte = brand metadata
        ("bms_flags_raw", 0x0104, "Additional status flags reported"),
        ("bms_protocol_reported", 0, "Generic CAN (PYLON/SOLAX protocol)"),
        ("bms_protocol_reported", 5, "Documented: Pylon RS485"),
        ("bms_protocol_reported", 42, "Protocol 42 (name unconfirmed)"),
        ("bms_protocol_reported", 0xFFFF, "Unknown"),
    ],
)
def test_bms_words(key: str, word: int, expected: str) -> None:
    assert label(key, word) == expected


@pytest.mark.parametrize(
    ("key", "word", "expected"),
    [
        ("ct_selfcheck_result_raw", 0, "No successful check reported"),
        ("ct_selfcheck_result_raw", 0b101, "Successful check reported: L1/L3"),
        ("ct_phase_sequence_result", 8, "Unconfirmed"),
    ],
)
def test_drm_and_ct_words(key: str, word: int, expected: str) -> None:
    assert label(key, word) == expected


def test_binary_sensors_keep_their_value_and_only_get_attributes() -> None:
    entry = P.entry("ct_b_selfcheck_success")
    assert STATES[entry.key].mode == "attributes"
    off = present(entry, {597: 0b001})
    on = present(entry, {597: 0b010})
    assert off is not None and on is not None
    assert off.label.en == "No successful check reported"
    assert on.label.en == "Successful check reported"
    reversed_ = present(P.entry("ct_a_direction_reversed"), {597: 64})
    assert reversed_ is not None and reversed_.label.en == "Reversal reported"
    assert label("mppt_scanning_reported", 0xFFFF) == "Unknown"
    assert label("mppt_scanning_reported", 4) == "On"


def test_time_of_use_states() -> None:
    assert label("tou_enable", 1) == "On"
    assert label("tou_weekdays", 0) == "Every day"
    assert label("tou_weekdays", 0xFE) == "Every day"
    assert label("tou_weekdays", 0b0000_0010) == "List: Mon"
    assert label("tou_weekdays", 0b0001_0010) == "List: Mon/Thu"
    for n in range(1, 7):
        assert label(f"tou_slot{n}_soc_raw", 80) == "SOC target: 80 %"
        assert label(f"tou_slot{n}_soc_raw", 101) == "Unconfirmed encoding"
        flags = f"tou_slot{n}_flags_raw"
        entry = P.entry(flags)
        assert 146 in STATES[flags].context

        def tou(word: int, r146: int | None, entry: Entry = entry) -> str | None:
            words = {entry.addresses[0]: word, **({} if r146 is None else {146: r146})}
            got = present(entry, words)
            return None if got is None else got.label.en

        assert tou(0, 0) == "GRID/GEN charging not allowed"
        assert tou(1, 0) == "Allowed: GRID"
        assert tou(2, 0) == "Allowed: GEN"
        assert tou(3, 0) == "Allowed: GRID+GEN"
        assert tou(0b11100, 256) == "Spain Mode: CH / BU / GM (priority 4>3>2)"
        assert tou(0b00100, 256) == "Spain Mode: GM (priority 4>3>2)"
        # the selector decides: stored Spain bits without the branch are not an active mode,
        # GRID/GEN bits with the branch leave the submode unresolved
        assert tou(4, 0) == "GRID/GEN charging not allowed"
        assert tou(1, 256) == "Spain Mode: submode unresolved"
        # no R146 in the read: stored bits only, the effective mode stays unresolved
        assert tou(4, None) == "Stored bits: GM (mode unresolved)"
        assert tou(0, None) == "Stored bits: none (mode unresolved)"


def test_bms_flags_high_byte_is_metadata_and_unknown_low_bits_survive() -> None:
    entry = P.entry("bms_flags_raw")
    got = present(entry, {222: 0x0B0A})
    assert got is not None
    assert got.label.en == "Reported: Force Charge and Sleep"
    assert got.attributes["high_byte_metadata"] == 0x0B
    got = present(entry, {222: 0x0112})
    assert got is not None and got.attributes["unknown_status_bits"] == "0x10"


def test_gen_on_grid_primary_is_null_outside_the_whole_words_0_and_1() -> None:
    from deye_monitor.profiles.states import primary_valid

    entry = P.entry("gen_on_grid_input")
    assert [primary_valid(entry, {189: w}) for w in (0, 1, 2, 3, 0xFFFF)] == [True, True] + [
        False
    ] * 3
    got = present(entry, {189: 3})
    assert got is not None and got.label.en == "Unconfirmed encoding"
    assert got.attributes["raw_decimal"] == [3]


def test_a_selected_slot_flags_entry_reads_the_branch_register() -> None:
    from deye_monitor.profiles.features import Features
    from deye_monitor.profiles.plan import build_read_plan

    plan = build_read_plan(
        P, {"tou_slot3_flags_raw"}, Features(), max_read_registers=60, energy_auto=False
    )
    assert 146 in plan.addresses and 174 in plan.addresses
    assert plan.dependencies["tou_slot3_flags_raw"] == (174, 146)
