"""The nine v1 write descriptors: encoding, boundaries, enums, masks, model cap."""

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
    parse_value,
)


def rated(watts: int) -> tuple[int, int]:
    raw = watts * 10
    return raw & 0xFFFF, raw >> 16  # R20 (low word), R21 (high word)


def test_exactly_the_nine_released_descriptors() -> None:
    assert [d.register for d in DESCRIPTORS] == [108, 109, 125, 128, 130, 134, 135, 136, 137]
    assert len(OPERATION_KEYS) == len(set(OPERATION_KEYS)) == 9
    for d in DESCRIPTORS:
        assert d.risk.de and d.risk.en and d.name.de and d.name.en and d.source


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


def test_r130_encodes_bit_zero_and_preserves_every_other_bit() -> None:
    d = BY_KEY["grid_charge_enable"]
    assert d.mask == 0x0001 and d.shift == 0
    for fresh in (0x0000, 0x0006, 0xFFFE, 0x1234, 0xFFFF, 0x0001):
        for bit in (0, 1):
            new = d.encode(fresh, bit)
            assert new & ~1 & 0xFFFF == fresh & ~1 & 0xFFFF
            assert new & 1 == bit
    with pytest.raises(Refused):
        d.encode(0x0004, 2)
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
    excluded = {80, 81, 91, 92, 22, 23, 24, 98, 111, 336, 341, 115, 116, 117, 141, 142, 146}
    excluded |= set(range(148, 178)) | set(range(1100, 1122))
    assert not registers & excluded


def test_smart_load_descriptors_pair_up_with_ordering() -> None:
    assert BY_KEY["smart_load_off_voltage"].companion == "smart_load_on_voltage"
    assert BY_KEY["smart_load_on_voltage"].companion == "smart_load_off_voltage"
    assert BY_KEY["smart_load_off_soc"].ordering == "below"
    assert BY_KEY["smart_load_on_soc"].ordering == "above"
    assert all(BY_KEY[k].needs_smart_load for k in OPERATION_KEYS if k.startswith("smart_load"))
    assert not any(BY_KEY[k].needs_smart_load for k in OPERATION_KEYS if "smart" not in k)


def test_inverter_config_defaults_and_validation() -> None:
    base: dict[str, Any] = {
        "endpoints": [{"id": "gw", "transport": "modbus_tcp", "host": "192.0.2.1", "port": 502}],
        "inverters": [{"id": "a", "name": "A", "endpoint": "gw", "unit_id": 1}],
    }
    inv = parse_config(base).inverters[0]
    assert inv.allow_writes is False and inv.enabled_write_operations == ()
