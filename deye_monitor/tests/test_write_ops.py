"""The 38 write descriptors: encoding, boundaries, gates, ordering, masks, model limits."""

from __future__ import annotations

import math
from typing import Any

import pytest

from deye_monitor.config import parse_config
from deye_monitor.write_ops import (
    BY_KEY,
    CURRENT_CAP_BY_RATED_W,
    DESCRIPTORS,
    OPERATION_KEYS,
    Refused,
    check_range,
    model_cap,
    model_limit,
    parse_time,
    parse_value,
    rated_watts,
)


def rated(watts: int) -> tuple[int, int]:
    raw = watts * 10
    return raw & 0xFFFF, raw >> 16  # R20 (low word), R21 (high word)


def test_exactly_the_released_descriptors() -> None:
    registers = [d.register for d in DESCRIPTORS]
    assert len(OPERATION_KEYS) == len(set(OPERATION_KEYS)) == len(set(registers)) == 38
    assert set(registers) == {
        108, 109, 115, 116, 117, 125, 127, 128, 130, 134, 135, 136, 137, 146,
        *range(148, 160), *range(166, 178),
    }  # fmt: skip
    for d in DESCRIPTORS:
        assert d.risk.de and d.risk.en and d.name.de and d.name.en and d.source
        assert d.group in {"battery", "grid_charge", "smart_load", "tou"}
        assert "test" not in d.risk.en.lower() and "Test" not in d.risk.de


def test_model_cap_comes_from_the_vendor_datasheet_per_model() -> None:
    assert CURRENT_CAP_BY_RATED_W == {5000: 120, 6000: 150, 8000: 190, 10000: 210, 12000: 240}
    for watts, cap in CURRENT_CAP_BY_RATED_W.items():
        assert model_cap(*rated(watts)) == cap
    for watts in (0, 4999, 7000, 9000, 15000, 80000):
        assert model_cap(*rated(watts)) is None
    assert model_cap(*rated(8000)) == 190
    assert model_cap(0x3881, 1) is None  # 80001 in 0.1 W is not a whole watt
    assert "Datenblatt SUN-5-12k-SG04LP3.pdf" in BY_KEY["battery_charge_current"].source


@pytest.mark.parametrize("key", ["battery_charge_current", "battery_discharge_current"])
@pytest.mark.parametrize(("watts", "cap"), sorted(CURRENT_CAP_BY_RATED_W.items()))
def test_current_operations_follow_the_model_cap(key: str, watts: int, cap: int) -> None:
    d = BY_KEY[key]
    check_range(d, 0, cap)
    check_range(d, cap, cap)
    with pytest.raises(Refused) as info:
        check_range(d, cap + 1, cap)
    assert info.value.key == "value_out_of_range"
    # the generic 185 A of the document does not apply any more: the cap is the limit
    assert d.limits(cap) == (0, cap)


def test_unknown_model_locks_current_operations_but_not_the_others() -> None:
    with pytest.raises(Refused) as info:
        check_range(BY_KEY["grid_charge_current"], 10, None)
    assert info.value.key == "model_unknown"
    check_range(BY_KEY["smart_load_off_soc"], 10, None)
    check_range(BY_KEY["grid_charge_enable"], 1, None)
    with pytest.raises(Refused) as info:  # the ToU power is bounded by the rated power too
        check_range(BY_KEY["tou_slot1_power"], 10, None)
    assert info.value.key == "model_unknown"


def test_current_operations_all_use_the_cap() -> None:
    capped = {d.key for d in DESCRIPTORS if d.current_cap}
    assert capped == {
        "battery_charge_current",
        "battery_discharge_current",
        "generator_charge_current",
        "grid_charge_current",
    }


@pytest.mark.parametrize(
    ("value", "raw"), [(0, 0), (1, 1), (50, 50), ("50", 50), (" 7 ", 7), (50.0, 50), ("0", 0)]
)
def test_integer_values(value: Any, raw: int) -> None:
    assert parse_value(BY_KEY["grid_charge_current"], value) == raw


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        float("nan"),
        float("inf"),
        -float("inf"),
        1.5,
        "1.5",
        "1e2",
        "0x10",
        "",
        "  ",
        "five",
        "1,5",
        [1],
        {"v": 1},
        b"1",
        "١٢",  # non-ASCII digits
        "9" * 20,
    ],
)
def test_integer_values_refuse_everything_that_is_not_exact(value: Any) -> None:
    with pytest.raises(Refused):
        parse_value(BY_KEY["grid_charge_current"], value)


def test_negative_values_do_not_fit() -> None:
    d = BY_KEY["battery_charge_current"]
    raw = parse_value(d, -1)
    assert raw == -1
    with pytest.raises(Refused):
        check_range(d, raw, 190)


@pytest.mark.parametrize(
    ("value", "raw"),
    [
        (38, 3800),
        ("38.00", 3800),
        (48.5, 4850),
        ("48.55", 4855),
        (63, 6300),
        ("63.0", 6300),
        (50.01, 5001),
    ],
)
def test_voltage_values_are_exact_hundredths(value: Any, raw: int) -> None:
    d = BY_KEY["smart_load_off_voltage"]
    assert parse_value(d, value) == raw
    check_range(d, raw, None)
    assert d.display(raw) == pytest.approx(raw / 100)


@pytest.mark.parametrize("value", ["48.555", 48.001, "37.99", "63.01", math.nan, "48.5.1", "٤٨"])
def test_voltage_values_without_rounding_and_inside_the_range(value: Any) -> None:
    d = BY_KEY["smart_load_on_voltage"]
    try:
        raw = parse_value(d, value)
    except Refused:
        return
    with pytest.raises(Refused):
        check_range(d, raw, None)


def test_voltage_boundaries() -> None:
    d = BY_KEY["smart_load_on_voltage"]
    check_range(d, 3800, None)
    check_range(d, 6300, None)
    for raw in (3799, 6301, 0, -1, 0x10000):
        with pytest.raises(Refused):
            check_range(d, raw, None)


@pytest.mark.parametrize("key", ["smart_load_off_soc", "smart_load_on_soc"])
def test_soc_boundaries(key: str) -> None:
    d = BY_KEY[key]
    check_range(d, 0, None)
    check_range(d, 100, None)
    for raw in (101, -1, 255, 0xFFFF):
        with pytest.raises(Refused):
            check_range(d, raw, None)


def test_r130_is_a_whole_word_switch_that_refuses_foreign_bits() -> None:
    d = BY_KEY["grid_charge_enable"]
    assert d.mask == 0x0001 and d.shift == 0 and d.whole_word_bool
    assert d.encode(0, 1) == 1 and d.encode(1, 0) == 0
    assert d.encode(0, 0) == 0 and d.encode(1, 1) == 1
    for fresh in (2, 3, 4, 5, 0x0006, 0xFFFE, 0x1234, 0xFFFF):
        for bit in (0, 1):
            with pytest.raises(Refused) as info:
                d.encode(fresh, bit)
            assert info.value.key == "unexpected_bits"
    for bad in (2, -1, 255):
        with pytest.raises(Refused) as info:
            d.encode(0, bad)
        assert info.value.key == "value_out_of_range"


@pytest.mark.parametrize("key", ["tou_enable", *[f"tou_slot{n}_grid_charge" for n in range(1, 7)]])
def test_bit_zero_switches_preserve_every_other_bit(key: str) -> None:
    d = BY_KEY[key]
    assert d.mask == 0x0001 and d.shift == 0 and not d.whole_word_bool
    for fresh in (0x0000, 0x0006, 0xFFFE, 0x1234, 0xFFFF, 0x0001, 0x01FE, 0x0002):
        for bit in (0, 1):
            new = d.encode(fresh, bit)
            assert new & ~1 & 0xFFFF == fresh & ~1 & 0xFFFF
            assert new & 1 == bit
    for bad in (2, -1, 255):
        with pytest.raises(Refused):
            d.encode(0x0004, bad)
    assert d.field_of(0xFFFE) == 0 and d.field_of(0x0001) == 1


def test_full_word_descriptors_replace_the_whole_scalar() -> None:
    d = BY_KEY["grid_charge_current"]
    assert d.encode(0xBEEF, 50) == 50
    with pytest.raises(Refused):
        d.encode(0, 0x10000)


def test_excluded_registers_have_no_descriptor() -> None:
    registers = {d.register for d in DESCRIPTORS}
    excluded = {80, 81, 91, 92, 22, 23, 24, 98, 111, 336, 341, 141, 142, 147, 133}
    excluded |= set(range(160, 166)) | set(range(1100, 1122))
    assert not registers & excluded


def test_smart_load_descriptors_pair_up_with_ordering() -> None:
    assert BY_KEY["smart_load_off_voltage"].below == ("smart_load_on_voltage",)
    assert BY_KEY["smart_load_on_voltage"].above == ("smart_load_off_voltage",)
    assert BY_KEY["smart_load_off_soc"].below == ("smart_load_on_soc",)
    assert BY_KEY["smart_load_on_soc"].above == ("smart_load_off_soc",)
    smart = [k for k in OPERATION_KEYS if k.startswith("smart_load")]
    assert all(BY_KEY[k].needs_smart_load for k in smart)
    assert not any(BY_KEY[k].needs_smart_load for k in OPERATION_KEYS if k not in smart)
    # voltage controls need R111 == 0, SOC controls R111 == 1
    assert {k: BY_KEY[k].requires_r111 for k in smart} == {
        "smart_load_off_voltage": 0,
        "smart_load_on_voltage": 0,
        "smart_load_off_soc": 1,
        "smart_load_on_soc": 1,
    }


def test_r111_gates_are_declared_on_every_soc_setting() -> None:
    soc_mode = {d.key for d in DESCRIPTORS if d.requires_r111 == 1}
    assert soc_mode == {
        "battery_shutdown_soc",
        "battery_low_soc",
        "battery_restart_soc",
        "grid_charge_start_soc",
        "smart_load_off_soc",
        "smart_load_on_soc",
        *[f"tou_slot{n}_soc" for n in range(1, 7)],
    }
    assert {d.key for d in DESCRIPTORS if d.requires_r111 == 0} == {
        "smart_load_off_voltage",
        "smart_load_on_voltage",
    }


def test_every_tou_setting_is_gated_on_spain_mode() -> None:
    tou = {d.key for d in DESCRIPTORS if d.tou}
    assert tou == {d.key for d in DESCRIPTORS if d.group == "tou"}
    assert len(tou) == 25 and "tou_enable" in tou
    assert not any(d.tou for d in DESCRIPTORS if d.group != "tou")


def test_protection_triplet_orders_shutdown_below_low_below_restart() -> None:
    s, low, r = (
        BY_KEY[k] for k in ("battery_shutdown_soc", "battery_low_soc", "battery_restart_soc")
    )
    assert (s.register, low.register, r.register) == (115, 117, 116)
    assert s.below == ("battery_low_soc",) and not s.above
    assert low.above == ("battery_shutdown_soc",) and low.below == ("battery_restart_soc",)
    assert r.above == ("battery_low_soc",) and not r.below
    for d in (s, low, r):
        assert (d.min_raw, d.max_raw) == (0, 100) and d.unit == "%"


def test_grid_charge_start_soc_is_clamped_by_the_firmware_range() -> None:
    d = BY_KEY["grid_charge_start_soc"]
    check_range(d, 10, None)
    check_range(d, 90, None)
    for raw in (9, 91, 0, 100, -1):
        with pytest.raises(Refused):
            check_range(d, raw, None)


@pytest.mark.parametrize("n", range(1, 7))
def test_slot_start_times_are_ordered_against_their_neighbours(n: int) -> None:
    d = BY_KEY[f"tou_slot{n}_start"]
    assert d.register == 147 + n and d.order_error == "tou_order"
    assert d.above == ((f"tou_slot{n - 1}_start",) if n > 1 else ())
    assert d.below == ((f"tou_slot{n + 1}_start",) if n < 6 else ())


@pytest.mark.parametrize("n", range(1, 7))
def test_slot_registers(n: int) -> None:
    assert BY_KEY[f"tou_slot{n}_power"].register == 153 + n
    assert BY_KEY[f"tou_slot{n}_soc"].register == 165 + n
    assert BY_KEY[f"tou_slot{n}_grid_charge"].register == 171 + n
    assert BY_KEY[f"tou_slot{n}_grid_charge"].mask == 1
    assert "allow GRID charging" in BY_KEY[f"tou_slot{n}_grid_charge"].name.en
    assert "force" not in BY_KEY[f"tou_slot{n}_grid_charge"].name.en.lower()


@pytest.mark.parametrize(
    ("text", "raw"),
    [("00:00", 0), ("0:05", 5), ("23:55", 2355), (" 12:30 ", 1230), ("07:45:00", 745)],
)
def test_time_values(text: str, raw: int) -> None:
    d = BY_KEY["tou_slot3_start"]
    assert parse_value(d, text) == raw == parse_time(text)
    check_range(d, raw, None)
    assert d.display(raw) == f"{raw // 100:02d}:{raw % 100:02d}"


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ("12:31", "value_not_exact"),  # minutes in steps of 5
        ("12:59", "value_not_exact"),
        ("12:60", "invalid_value"),
        ("24:00", "value_out_of_range"),
        ("99:00", "value_out_of_range"),
        ("12:30:05", "invalid_value"),  # seconds must be 00
        ("12:30:00+01:00", "invalid_value"),  # no timezone
        ("1230", "invalid_value"),
        (1230, "invalid_value"),
        (12.5, "invalid_value"),
        (True, "invalid_value"),
        (None, "invalid_value"),
        ("", "invalid_value"),
        ("١٢:٣٠", "invalid_value"),
        ("1:2", "invalid_value"),
        ("-1:00", "invalid_value"),
    ],
)
def test_time_values_without_rounding(value: Any, key: str) -> None:
    with pytest.raises(Refused) as info:
        parse_value(BY_KEY["tou_slot1_start"], value)
    assert info.value.key == key


def test_slot_power_is_limited_by_the_rated_power() -> None:
    d = BY_KEY["tou_slot2_power"]
    assert d.power_cap and d.unit == "W"
    for watts in (5000, 6000, 8000, 10000, 12000):
        assert rated_watts(*rated(watts)) == watts
        assert model_limit(d, *rated(watts)) == watts
        assert d.limits(watts) == (0, watts)
        check_range(d, 0, watts)
        check_range(d, watts, watts)
        with pytest.raises(Refused):
            check_range(d, watts + 1, watts)
    for watts in (0, 4999, 7000, 15000):
        assert rated_watts(*rated(watts)) is None
        assert model_limit(d, *rated(watts)) is None
    assert rated_watts(0x3881, 1) is None  # not a whole watt
    assert model_limit(BY_KEY["grid_charge_current"], *rated(8000)) == 190
    assert model_limit(BY_KEY["tou_slot2_soc"], *rated(8000)) is None


@pytest.mark.parametrize("n", range(1, 7))
def test_slot_target_soc_range(n: int) -> None:
    d = BY_KEY[f"tou_slot{n}_soc"]
    check_range(d, 0, None)
    check_range(d, 100, None)
    for raw in (101, -1):
        with pytest.raises(Refused):
            check_range(d, raw, None)


@pytest.mark.parametrize(
    "key", ["battery_shutdown_soc", "battery_low_soc", "battery_restart_soc", "tou_slot1_soc"]
)
@pytest.mark.parametrize("value", [1.5, "1.5", float("nan"), "5%", True, None, "1e1"])
def test_soc_settings_reject_fractional_and_malformed_values(key: str, value: Any) -> None:
    with pytest.raises(Refused):
        parse_value(BY_KEY[key], value)


def test_descriptor_views_are_json_ready() -> None:
    import json

    views = [d.view() for d in DESCRIPTORS]
    json.dumps(views)
    start = next(v for v in views if v["key"] == "tou_slot1_start")
    assert (start["kind"], start["min"], start["max"], start["step"]) == (
        "time",
        "00:00",
        "23:55",
        5,
    )
    assert next(v for v in views if v["key"] == "grid_charge_enable")["whole_word_bool"] is True


def test_inverter_config_defaults_and_validation() -> None:
    base: dict[str, Any] = {
        "endpoints": [{"id": "gw", "transport": "modbus_tcp", "host": "192.0.2.1", "port": 502}],
        "inverters": [{"id": "a", "name": "A", "endpoint": "gw", "unit_id": 1}],
    }
    inv = parse_config(base).inverters[0]
    assert inv.allow_writes is False and inv.enabled_write_operations == ()
