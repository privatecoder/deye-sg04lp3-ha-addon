from __future__ import annotations

import pytest

from deye_monitor.config import EnergyResolution
from deye_monitor.profiles.decode import decode
from deye_monitor.profiles.energy import effective_resolution, resolution_from_r65
from deye_monitor.profiles.schema import Entry, Profile, load_profile


@pytest.fixture(scope="module")
def p() -> Profile:
    return load_profile("sg04lp3_lv_v105_8")


def d(p: Profile, key: str, words: dict[int, int], res: float | None = None) -> object:
    return decode(p.entry(key), words, energy_resolution=res)


def test_s16_values(p: Profile) -> None:
    assert d(p, "battery_current", {591: 0x0001}) == 0.01
    assert d(p, "battery_current", {591: 0xFFFF}) == -0.01
    assert d(p, "battery_current", {591: 0x8000}) == -327.68
    assert d(p, "battery_current", {591: 0x7FFF}) == 327.67
    assert d(p, "battery_power_raw", {590: 0xFFFF}) == -1  # unresolved: raw signed value, no scale


def test_s32_pairs_combine_before_sign(p: Profile) -> None:
    words = {650: 0xFFFF, 656: 0x0000}
    assert d(p, "load_power_a", words) == 65535
    assert d(p, "load_power_a", {650: 0xFFFF, 656: 0xFFFF}) == -1
    assert d(p, "load_power_a", {650: 0x0000, 656: 0x8000}) == -2147483648
    assert d(p, "load_power_a", {650: 0x0000, 656: 0x7FFF}) == 0x7FFF0000
    assert d(p, "load_power_a", {650: 0x0001, 656: 0x0001}) == 65537
    assert d(p, "load_power_total", {653: 100, 659: 0}) == 100
    # the high word of phase A is not the partner of phase B
    assert d(p, "load_power_b", {651: 7, 657: 1}) == 65543


def test_ups_power_is_unsigned(p: Profile) -> None:
    assert d(p, "ups_power_a", {640: 0xFFFF, 696: 0xFFFF}) == 0xFFFFFFFF


def test_u32_rated_power_scale(p: Profile) -> None:
    assert d(p, "rated_power", {20: 100000 & 0xFFFF, 21: 100000 >> 16}) == 10000.0


def test_missing_partner_or_word_is_unknown(p: Profile) -> None:
    assert d(p, "load_power_a", {650: 5}) is None
    assert d(p, "load_power_a", {656: 5}) is None
    assert d(p, "load_power_a", {}) is None
    assert d(p, "battery_soc", {}) is None
    assert d(p, "serial_number", {3: 0x3234, 4: 0x3031, 5: 0x3233, 6: 0x3435}) is None  # R7 missing
    assert d(p, "load_power_a", {650: 5, 656: 0, 999: 1}) == 5


def test_temperature_offsets(p: Profile) -> None:
    assert d(p, "bms_temperature", {217: 1200}) == 20.0
    assert d(p, "bms_temperature", {217: 1000}) == 0.0
    assert d(p, "bms_temperature", {217: 800}) == -20.0
    assert d(p, "temp_heatsink", {541: 1200}) == 20.0
    assert d(p, "temp_dc_transformer", {540: 1000}) == 0.0


def fw(p: Profile, raw: int, main_firmware: int | None) -> object:
    return decode(p.entry("battery_temperature"), {586: raw}, main_firmware=main_firmware)


def test_battery_temperature_offset_depends_on_the_main_firmware(p: Profile) -> None:
    # 1151 (direct MCU2 path): s16/10 without offset
    assert fw(p, 1200, 0x1151) == 120.0
    assert fw(p, 0xFFF6, 0x1151) == -1.0
    assert fw(p, 250, 0x1151) == 25.0
    # 1175 (live, logger): (raw - 1000) / 10; raw 1176 is the observed 17.6 C
    assert fw(p, 1176, 0x1175) == 17.6
    assert fw(p, 1000, 0x1175) == 0.0
    # unknown firmware: the offset applies only when raw/10 is implausibly hot and the offset
    # value plausible
    assert fw(p, 1176, None) == 17.6
    assert fw(p, 1176, 0x9999) == 17.6
    assert fw(p, 250, 0x9999) == 25.0
    assert fw(p, 800, 0x9999) == 80.0  # not above 80 C: no offset
    assert fw(p, 3000, 0x9999) == 300.0  # also implausible with the offset: left as it is


def test_plain_scales(p: Profile) -> None:
    assert d(p, "grid_voltage_a", {598: 2305}) == 230.5
    assert d(p, "grid_frequency", {609: 5001}) == 50.01
    assert d(p, "battery_voltage", {587: 5120}) == 51.2
    assert d(p, "battery_soc", {588: 87}) == 87
    assert d(p, "pv1_power", {672: 3000}) == 3000
    assert d(p, "pv1_voltage", {676: 3500}) == 350.0
    assert d(p, "pv2_current", {679: 85}) == 8.5


def test_r336_components(p: Profile) -> None:
    raw = (3 << 10) | (1 << 8) | 0b11
    words = {336: raw}
    assert d(p, "parallel_enable", words) is True
    assert d(p, "parallel_master", words) is True
    assert d(p, "parallel_phase", words) == "B"
    assert d(p, "parallel_sn", words) == 3
    slave = {336: (63 << 10) | (2 << 8) | 1}
    assert d(p, "parallel_master", slave) is False
    assert d(p, "parallel_phase", slave) == "C"
    assert d(p, "parallel_sn", slave) == 63
    assert d(p, "parallel_sn", {336: 4 << 10}) == 4  # not 4096 (kellerza read-side pitfall)
    assert d(p, "parallel_phase", {336: 3 << 8}) == "void"
    assert d(p, "parallel_enable", {336: 0}) is False


def test_r22_and_r24(p: Profile) -> None:
    assert d(p, "mppt_count", {22: 0x0203}) == 2
    assert d(p, "phase_count", {22: 0x0203}) == 3
    assert d(p, "mppt_count", {22: 0x0503}) == 5
    assert d(p, "battery_input_code", {24: 0}) == "one_input_default"
    assert d(p, "battery_input_code", {24: 2}) == "two_inputs"
    assert d(p, "battery_input_code", {24: 4}) == "unknown_4"


def test_status_words_and_bits(p: Profile) -> None:
    assert d(p, "fault_word_1", {555: 0x1234}) == 0x1234
    assert d(p, "fault_word_4", {558: 0xFFFF}) == 0xFFFF
    assert d(p, "fault_f13", {555: 1 << 12}) is True
    assert d(p, "fault_f13", {555: 0xEFFF}) is False
    assert d(p, "fault_f29", {556: 1 << 12}) is True
    assert d(p, "fault_f41", {557: 1 << 8}) is True
    assert d(p, "fault_f41", {557: 1 << 12}) is False
    assert d(p, "relay_grid", {552: 0b100}) is True
    assert d(p, "relay_inverter", {552: 0b100}) is False
    assert d(p, "relay_state_raw", {552: 0x0185}) == 0x0185
    assert d(p, "power_switch", {551: 0xAB01}) == "on"
    assert d(p, "power_switch", {551: 0xAB00}) == "off"
    assert d(p, "operating_state", {500: 2}) == "normal"
    assert d(p, "operating_state", {500: 6}) == "unknown_6"


def test_ascii_serial(p: Profile) -> None:
    words = {
        3 + i: (ord(a) << 8) | ord(b)
        for i, (a, b) in enumerate(zip("2401234567"[::2], "2401234567"[1::2], strict=True))
    }
    assert d(p, "serial_number", words) == "2401234567"
    assert d(p, "serial_number", {**words, 5: 0x0001}) is None  # non-printable
    assert d(p, "serial_number", {**words, 5: 0x7F41}) is None
    assert d(p, "serial_number", {**words, 6: 0x2020}) == "240123  67"


def test_hhmm_enum_raw(p: Profile) -> None:
    assert d(p, "tou_slot1_start", {148: 2359}) == "23:59"
    assert d(p, "tou_slot1_start", {148: 0}) == "00:00"
    assert d(p, "tou_slot1_start", {148: 905}) == "09:05"
    assert d(p, "tou_slot1_start", {148: 2400}) is None
    assert d(p, "tou_slot1_start", {148: 1260}) is None
    assert d(p, "gen_port_usage", {133: 1}) == "smart_load"
    assert d(p, "gen_min_power_retention", {189: 0x0300}) == "50_percent"
    assert d(p, "gen_on_grid_input", {189: 0x0301}) is True
    assert d(p, "tou_weekdays", {146: 0b1111_1110}) == 0b1111111
    assert d(p, "tou_slot1_soc_raw", {166: 0x1234}) == 0x1234  # R166-R171: raw only
    assert d(p, "fw_comm_version", {17: 0x1000, 18: 0x0070}) == "0x10000070"  # shown as hex
    assert d(p, "fw_main_version", {15: 0x1175}) == "0x1175"
    assert d(p, "id_fw_main_version", {15: 0x1175}) == 0x1175  # the internal key stays numeric


def test_energy_requires_known_resolution(p: Profile) -> None:
    assert d(p, "battery_energy_charge_today", {514: 123}) is None
    assert d(p, "battery_energy_charge_today", {514: 123}, 0.1) == 12.3
    assert d(p, "battery_energy_charge_today", {514: 123}, 1.0) == 123
    assert d(p, "battery_energy_charge_today", {514: 0}, None) is None  # unknown is never 0


def test_daily_counters_stay_separate(p: Profile) -> None:
    words = {514: 100, 515: 200, 520: 300, 521: 400}
    assert d(p, "battery_energy_charge_today", words, 0.1) == 10.0
    assert d(p, "battery_energy_discharge_today", words, 0.1) == 20.0
    assert d(p, "grid_energy_import_today", words, 0.1) == 30.0
    assert d(p, "grid_energy_export_today", words, 0.1) == 40.0
    # a missing neighbour never affects a single-word counter
    assert d(p, "battery_energy_charge_today", {514: 100}, 0.1) == 10.0


def test_r526_is_not_part_of_r527_r528(p: Profile) -> None:
    words = {526: 5, 527: 1, 528: 0}
    assert d(p, "load_energy_today", words, 0.1) == 0.5
    assert d(p, "load_energy_total", words, 0.1) == 0.1
    assert d(p, "load_energy_total", {526: 5, 527: 1}, 0.1) is None
    assert d(p, "load_energy_total", {527: 1, 528: 1}, 1.0) == 65537


def test_energy_totals_low_high_roles(p: Profile) -> None:
    for key, low, high in (
        ("battery_energy_charge_total", 516, 517),
        ("battery_energy_discharge_total", 518, 519),
        ("grid_energy_import_total", 522, 523),
        ("grid_energy_export_total", 524, 525),
        ("load_energy_total", 527, 528),
        ("pv_energy_total", 534, 535),
    ):
        assert d(p, key, {low: 0x0001, high: 0x0002}, 0.1) == 13107.3
        assert d(p, key, {low: 0x0001, high: 0x0002}, 1.0) == 131073
        assert d(p, key, {low: 0x0002, high: 0x0001}, 1.0) == 65538


def test_no_value_is_scaled_without_a_scale(p: Profile) -> None:
    raw_numeric = [
        e for e in p.entries if e.is_numeric and e.scale is None and e.scale_source is None
    ]
    assert raw_numeric
    assert all(e.release == "unresolved" for e in raw_numeric)
    assert d(p, "gen_power_raw_a", {664: 0xFFFF, 668: 0xFFFF}) == 0xFFFFFFFF


def test_out_of_range_words_are_rejected(p: Profile) -> None:
    for bad in (-1, 0x10000):
        with pytest.raises(ValueError):
            d(p, "battery_soc", {588: bad})


def test_every_entry_decodes_to_something_for_zero_words(p: Profile) -> None:
    for e in p.entries:
        words = dict.fromkeys(e.addresses, 0)
        value = decode(e, words, energy_resolution=0.1)
        if e.type != "ascii":
            assert value is not None, e.key
        else:
            assert value is None


def test_r65_resolution_and_overrides() -> None:
    assert resolution_from_r65(0x0000) is None  # not a protocol version: nothing is derived
    assert resolution_from_r65(0x0103) == 0.1
    assert resolution_from_r65(0x0104) == 0.1
    assert resolution_from_r65(0x0105) == 1.0
    assert resolution_from_r65(0x0001) == 0.1
    assert resolution_from_r65(0xFFFE) == 1.0
    assert resolution_from_r65(0xFFFF) is None
    for bad in (-1, 0x10000):
        with pytest.raises(ValueError):
            resolution_from_r65(bad)
    auto = EnergyResolution.AUTO
    assert effective_resolution(auto, None) is None  # missing/timeout is not R65=0
    assert effective_resolution(auto, 0x0104) == 0.1
    assert effective_resolution(auto, 0x0105) == 1.0
    assert effective_resolution(auto, 0xFFFF) is None and effective_resolution(auto, 0) is None
    assert effective_resolution(EnergyResolution.TENTH, 0xFFFF) == 0.1  # an override wins
    assert effective_resolution(EnergyResolution.TENTH, 0x0105) == 0.1
    assert effective_resolution(EnergyResolution.ONE, 0x0100) == 1.0
    assert effective_resolution(EnergyResolution.TENTH, None) == 0.1
    assert effective_resolution(EnergyResolution.ONE, None) == 1.0


def test_decode_is_independent_of_entry_object_identity(p: Profile) -> None:
    clone = Entry.model_validate(p.entry("battery_soc").model_dump())
    assert decode(clone, {588: 50}) == 50


def test_not_reported_words_are_unknown_per_field(p: Profile) -> None:
    assert d(p, "device_type", {0: 0xFFFF}) is None
    assert d(p, "mppt_count", {22: 0xFFFF}) is None
    assert d(p, "phase_count", {22: 0xFFFF}) is None
    assert d(p, "battery_input_code", {24: 0xFFFF}) is None
    assert d(p, "mppt_count", {22: 0x0203}) == 2 and d(p, "phase_count", {22: 0x0203}) == 3
    assert d(p, "device_type", {0: 0x0500}) == "three_phase_lv_hybrid"
    assert d(p, "device_type", {0: 0x0005}) == "three_phase_lv_hybrid"
    assert d(p, "id_r22", {22: 0xFFFF}) == 0xFFFF  # the raw word stays available
