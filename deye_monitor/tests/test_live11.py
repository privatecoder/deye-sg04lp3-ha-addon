"""Live-11 (0.2.8): named warnings and faults from the vendor alarm list."""

from __future__ import annotations

import json

import pytest

from deye_monitor.config import PollConfig
from deye_monitor.ha_discovery import Namespace, inverter_component, state_payload
from deye_monitor.profiles.alarms import STATE_LIMIT, active_codes, active_items, describe
from deye_monitor.profiles.decode import decode
from deye_monitor.profiles.schema import load_profile

PROFILE = load_profile("sg04lp3_lv_v105_8")
WARN = PROFILE.entry("active_warnings")
FAULT = PROFILE.entry("active_faults")
NS = Namespace("ab12cd34", "homeassistant", "deye")


def test_w4_is_a_binary_sensor_on_bit_3_off_by_default() -> None:
    e = PROFILE.entry("alarm_meter_comm")
    assert (e.addr, e.mask, e.platform, e.default_enabled) == (553, 8, "binary_sensor", False)
    assert decode(e, {553: 8}) is True and decode(e, {553: 0xFFF7}) is False


def test_nothing_active_reads_none() -> None:
    assert decode(WARN, {553: 0, 554: 0}) == "none"
    assert decode(FAULT, {555: 0, 556: 0, 557: 0, 558: 0}) == "none"


def test_active_faults_are_named_in_bit_order() -> None:
    words = {555: 0, 556: 1 << 12, 557: 0, 558: 0}
    assert decode(FAULT, words) == "F29 Parallel CANBus fault"
    words[556] |= 1 << 8  # F25
    words[557] = 1 << 8  # F41
    assert decode(FAULT, words) == (
        "F25 DC feedback fault, F29 Parallel CANBus fault, F41 Parallel system stop"
    )


def test_active_warnings_span_both_words() -> None:
    assert decode(WARN, {553: 0b1110, 554: 0xC000}) == (
        "W2 Fan warning, W3 Grid phase wrong, W4 Meter communication fail, "
        "W31 Battery communication warning, W32 Parallel communication warning"
    )


def test_the_word_boundaries_map_to_f1_f16_f17_f64() -> None:
    assert active_codes("faults", [1, 0, 0, 0]) == ["F1"]
    assert active_codes("faults", [0x8000, 1, 0, 0]) == ["F16", "F17"]
    assert active_codes("faults", [0, 0, 0, 0x8000]) == ["F64"]


def test_the_bits_that_match_the_single_entries() -> None:
    assert active_codes("faults", [1 << 12, 0, 0, 0]) == ["F13"]  # fault_f13
    assert active_codes("warnings", [0, 1 << 14]) == ["W31"]  # alarm_battery_missing


def test_an_unnamed_bit_shows_its_code_and_a_missing_word_is_unknown() -> None:
    assert decode(WARN, {553: 1 << 5, 554: 0}) == "W6"
    assert describe("W6") == {"code": "W6", "name": "Unknown", "remedy": ""}
    assert decode(WARN, {553: 0}) is None


def test_all_faults_set_stays_within_the_state_limit() -> None:
    text = decode(FAULT, {555: 0xFFFF, 556: 0xFFFF, 557: 0xFFFF, 558: 0xFFFF})
    assert isinstance(text, str) and len(text) <= STATE_LIMIT
    assert text.startswith("F1, F2,") and text.endswith("…")  # cut after a whole code


@pytest.mark.parametrize("kind,count", [("warnings", 5), ("faults", 64)])
def test_the_table_is_complete(kind: str, count: int) -> None:
    from deye_monitor.profiles.alarms import _table

    table = _table()[kind]
    assert len(table) == count
    assert all(v["name"] and v["remedy"] for v in table.values())
    assert 31 in _table()["warnings"] and 4 in _table()["warnings"]


def test_the_attribute_list_carries_code_name_and_remedy() -> None:
    items = active_items("F29 Parallel CANBus fault, F41 Parallel system stop")
    assert [i["code"] for i in items] == ["F29", "F41"]
    assert all(i["name"] and i["remedy"] for i in items)
    assert active_items("none") == [] and active_items(None) == []


def test_the_payload_and_the_discovery_publish_text_and_attributes() -> None:
    raw, replaced = state_payload(
        cycle_id=1,
        wall_ts=0.0,
        energy_generation=0,
        energy_series="g0",
        values={"active_faults": "F29 Parallel CANBus fault", "active_warnings": None},
        entries={"active_faults": FAULT, "active_warnings": WARN},
    )
    body = json.loads(raw)
    assert replaced == 0
    assert body["active_faults"] == "F29 Parallel CANBus fault"
    assert body["active_faults_list"][0]["code"] == "F29"
    assert body["active_warnings"] is None and body["active_warnings_list"] is None
    _, comp = inverter_component(NS, "inv1", FAULT, series="g0", poll=PollConfig(), profile=PROFILE)
    assert comp["platform"] == "sensor" and comp["entity_category"] == "diagnostic"
    assert "active_faults_list" in comp["json_attributes_template"]
    assert "device_class" not in comp and "unit_of_measurement" not in comp


def test_both_text_sensors_are_default_and_read_the_right_words() -> None:
    assert WARN.default_enabled and FAULT.default_enabled
    assert WARN.addresses == (553, 554) and FAULT.addresses == (555, 556, 557, 558)
