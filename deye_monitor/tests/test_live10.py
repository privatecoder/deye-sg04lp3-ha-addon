"""Live-10 (0.2.7): BMS bank current, W31 decoding, firmware boards, plant sensor keys."""

from __future__ import annotations

from deye_monitor.profiles.decode import decode
from deye_monitor.profiles.schema import load_profile
from deye_monitor.totals import METRICS

PROFILE = load_profile("sg04lp3_lv_v105_8")


def test_the_bms_current_is_the_signed_bank_total_in_one_ampere() -> None:
    e = PROFILE.entry("bms_current")
    assert e.name.en == "BMS current (bank)" and e.addr == 216 and e.unit == "A"
    assert e.default_enabled is True
    assert decode(e, {216: 86}) == 86  # positive = charging
    assert decode(e, {216: 0xFFF8}) == -8  # the cloud shows -8 A at night: discharging
    assert "bms_current_raw" not in {x.key for x in PROFILE.entries}
    assert "charging" in (e.notes or "").lower()


def test_warning_w31_is_decoded_from_the_second_alarm_word() -> None:
    e = PROFILE.entry("alarm_battery_missing")
    assert e.addr == 554 and e.mask == 1 << 14  # the 31st warning bit
    assert "W31" in e.name.en and "communication" in str(e.name.en).lower()
    assert decode(e, {554: 1 << 14}) is True
    assert decode(e, {554: 1 << 15}) is False  # bit 15 is the parallel communication warning


def test_the_plant_sensors_use_keys_the_profile_has() -> None:
    keys = {e.key for e in PROFILE.entries}
    for metric in METRICS:
        assert set(metric.keys) <= keys, metric.key


def test_main_firmware_is_not_a_default_sensor_any_more() -> None:
    assert PROFILE.entry("fw_main_version").default_enabled is False  # part of MCU2 (R14-R16)
