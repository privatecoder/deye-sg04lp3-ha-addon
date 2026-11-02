"""0.5.0 part B2, domain 1: battery / BMS settings (R99-R103, R105-R107, R112-R114, R118-R120,
R126): exact encodings (step, negated R107, half-hour R106), the weak stage chain, the strict
voltage-protection chain, dynamic bounds from fresh companions, curve/mode gates, guard, HA."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.ha_discovery import setting_component, validate_component
from deye_monitor.mqtt_settings import parse_command
from deye_monitor.profiles.features import Features
from deye_monitor.protocol.guard import WHOLE_WORD_BOOLS, WRITABLE_FIELDS
from deye_monitor.write_ops import BY_KEY, Kind, Refused, check_range, parse_value
from tests.plant_helpers import plant_rig
from tests.test_ha_discovery import NS
from tests.write_helpers import build_plant, err, preview, svc_of, write

NEW = {
    "battery_equalization_voltage": 99,
    "battery_absorption_voltage": 100,
    "battery_float_voltage": 101,
    "battery_capacity": 102,
    "battery_empty_voltage": 103,
    "battery_equalization_interval": 105,
    "battery_equalization_duration": 106,
    "battery_temperature_compensation": 107,
    "battery_activation": 112,
    "battery_internal_resistance": 113,
    "battery_charge_efficiency": 114,
    "battery_shutdown_voltage": 118,
    "battery_restart_voltage": 119,
    "battery_low_voltage": 120,
    "grid_charge_start_voltage": 126,
}


def test_every_new_setting_has_a_descriptor_a_guard_mask_and_both_texts() -> None:
    for key, register in NEW.items():
        d = BY_KEY[key]
        assert d.register == register and d.needs_battery
        assert (
            d.risk.de and d.risk.en and f"R{register}" in d.name.en and f"R{register}" in d.name.de
        )
        assert d.mask in WRITABLE_FIELDS[register]  # the guard allows exactly the contracted field
    assert (WRITABLE_FIELDS[112], 112 in WHOLE_WORD_BOOLS) == (frozenset({0x0001}), True)
    assert BY_KEY["battery_activation"].whole_word_bool


@pytest.mark.parametrize(
    ("key", "value", "raw"),
    [
        ("battery_float_voltage", "41.0", 4100),
        ("battery_float_voltage", 58.8, 5880),
        ("battery_empty_voltage", "52", 5200),
        ("battery_shutdown_voltage", 60, 6000),
        ("battery_capacity", 2000, 2000),
        ("battery_equalization_interval", 0, 0),
        ("battery_equalization_duration", "0.5", 1),  # R106 raw = 2 x hours
        ("battery_equalization_duration", 10, 20),
        ("battery_temperature_compensation", -20, 20),  # R107 raw = magnitude, value negative
        ("battery_temperature_compensation", 0, 0),
        ("battery_internal_resistance", 200, 200),
        ("battery_charge_efficiency", "0.1", 1),
        ("battery_charge_efficiency", "100.0", 1000),
        ("grid_charge_start_voltage", "41", 4100),
    ],
)
def test_values_encode_exactly(key: str, value: object, raw: int) -> None:
    d = BY_KEY[key]
    field = parse_value(d, value)
    assert field == raw
    check_range(d, field, None)
    assert d.display(field) == float(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("battery_float_voltage", "40.9", "value_out_of_range"),
        ("battery_float_voltage", "58.9", "value_out_of_range"),
        ("battery_float_voltage", "50.05", "value_not_exact"),  # off the 0.1 V step
        ("battery_shutdown_voltage", "60.1", "value_out_of_range"),
        ("battery_empty_voltage", "52.1", "value_out_of_range"),
        ("battery_capacity", 0, "value_out_of_range"),
        ("battery_equalization_duration", "0.25", "value_not_exact"),
        ("battery_equalization_duration", "10.5", "value_out_of_range"),
        ("battery_temperature_compensation", 3, "value_out_of_range"),  # positive: not a legal sign
        ("battery_temperature_compensation", -21, "value_out_of_range"),
        ("battery_temperature_compensation", "-2.5", "value_not_exact"),
        ("battery_equalization_interval", 91, "value_out_of_range"),
        ("battery_charge_efficiency", 0, "value_out_of_range"),
        ("battery_charge_efficiency", "100.1", "value_out_of_range"),
        ("battery_charge_efficiency", "98.05", "value_not_exact"),
        ("battery_internal_resistance", 201, "value_out_of_range"),
        ("grid_charge_start_voltage", "58.1", "value_out_of_range"),
    ],
)
def test_values_are_refused_never_rounded_or_clamped(key: str, value: object, reason: str) -> None:
    d = BY_KEY[key]
    with pytest.raises(Refused) as caught:
        check_range(d, parse_value(d, value), None)
    assert caught.value.key == reason


def test_the_negated_coefficient_reads_and_lists_in_engineering_order() -> None:
    d = BY_KEY["battery_temperature_compensation"]
    assert d.display(0) == 0.0 and str(d.display(0)) == "0.0"  # never "-0.0"
    assert d.display(6) == -6
    view = d.view()
    assert (view["min"], view["max"], view["step"]) == (-20, 0, 1)


def test_the_half_hour_register_shows_hours() -> None:
    d = BY_KEY["battery_equalization_duration"]
    view = d.view()
    assert (view["min"], view["max"], view["step"], view["unit"]) == (0.0, 10.0, 0.5, "h")


def test_chains_are_declared_in_the_contracted_order() -> None:
    stage = ("battery_float_voltage", "battery_absorption_voltage", "battery_equalization_voltage")
    for key in stage:
        assert BY_KEY[key].chain == stage and not BY_KEY[key].chain_strict
    protect = ("battery_shutdown_voltage", "battery_low_voltage", "battery_restart_voltage")
    for key in protect:
        d = BY_KEY[key]
        assert d.chain == protect and d.chain_strict and d.requires_r111 == 0
    assert BY_KEY["grid_charge_start_voltage"].upper[0].key == "battery_float_voltage"
    assert BY_KEY["grid_charge_start_voltage"].upper[0].offset == -200
    assert BY_KEY["grid_charge_current"].upper[0].key == "battery_charge_current"


def test_the_curve_settings_are_gated_on_a_bms_battery_and_the_type_word() -> None:
    for key in (
        "battery_float_voltage",
        "battery_absorption_voltage",
        "battery_equalization_voltage",
        "battery_equalization_interval",
        "battery_equalization_duration",
        "battery_temperature_compensation",
    ):
        d = BY_KEY[key]
        assert d.group == "battery_curve"
        assert d.feature_gates[0].feature == "bms" and d.feature_gates[0].blocked == {True}
        assert d.gates[0].register == 98
        assert d.gates[0].allowed == frozenset({0})  # positive: lead / internal only


@pytest.mark.parametrize("key", sorted(NEW))
def test_ha_entity_is_a_valid_component_with_the_contracted_limits(key: str) -> None:
    d = BY_KEY[key]
    ns = NS
    ckey, comp = setting_component(ns, "deye1", d, "tok", None)
    validate_component(ckey, comp)
    if d.kind is Kind.BIT:
        assert comp["platform"] == "switch"
        return
    assert comp["platform"] == "number" and comp["mode"] == "box"
    assert comp["min"] < comp["max"] or (comp["min"], comp["max"]) == (0, 0)
    assert comp["step"] > 0
    if key == "battery_charge_efficiency":
        assert "device_class" not in comp
    if key == "battery_temperature_compensation":
        assert (comp["min"], comp["max"]) == (-20, 0)


def test_mqtt_payloads_are_strict_for_the_new_numbers() -> None:
    assert parse_command(BY_KEY["battery_temperature_compensation"], b"-5")[1] == 5
    assert parse_command(BY_KEY["battery_equalization_duration"], b"1.5")[1] == 3
    for bad in (b"1e1", b"nan", b"5 ", b"+5"):
        with pytest.raises(Refused):
            parse_command(BY_KEY["battery_temperature_compensation"], bad)


# -- through the write service (fresh reads, companions, gates) ---------------------------------


async def _rig_plant(rig, **regs: int):  # type: ignore[no-untyped-def]
    await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r111": 0, **regs}})
    inv = svc_of(rig).runtime.inverters["deye1"]
    inv.features = inv.features.model_copy(update={"bms": False})
    return inv


async def test_stage_voltages_keep_float_le_absorption_le_equalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _rig_plant(rig)  # float 53.0 <= absorption 54.0 <= equalization 55.0
        status, body = await preview(rig, "deye1", "battery_float_voltage", "54.1")
        assert (status, err(body)) == (422, "threshold_order")
        assert (await preview(rig, "deye1", "battery_float_voltage", "54.0"))[0] == 200  # equal ok
        status, body = await preview(rig, "deye1", "battery_equalization_voltage", "53.9")
        assert (status, err(body)) == (422, "threshold_order")
        assert rig.servers["gw1"].responder.writes == []


async def test_protection_voltages_are_strictly_ordered_and_need_voltage_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _rig_plant(rig)  # shutdown 45.0 < low 48.0 < restart 52.0
        status, body = await preview(rig, "deye1", "battery_shutdown_voltage", "48.0")
        assert (status, err(body)) == (422, "threshold_order")
        status, body = await preview(rig, "deye1", "battery_restart_voltage", "48.0")
        assert (status, err(body)) == (422, "threshold_order")
        assert (await preview(rig, "deye1", "battery_low_voltage", "49.9"))[0] == 200
    async with plant_rig(tmp_path / "soc", monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r111": 1}})
        status, body = await preview(rig, "deye1", "battery_low_voltage", "49")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "voltage_mode_required"


async def test_the_start_voltage_stays_two_volts_below_the_fresh_float(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _rig_plant(rig)  # float 53.00 V -> at most 51.00 V
        status, body = await preview(rig, "deye1", "grid_charge_start_voltage", "51.1")
        assert (status, err(body)) == (422, "bound_max")
        assert body["error"]["params"]["limit"] == 51.0
        assert (await write(rig, "deye1", "grid_charge_start_voltage", "51.0"))[
            "status"
        ] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[101] == 5300  # the float is never touched


async def test_a_corrupt_float_companion_never_raises_the_start_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _rig_plant(rig, r101=0xFFFF)
        status, body = await preview(rig, "deye1", "grid_charge_start_voltage", "50")
        assert (status, err(body)) == (409, "companion_invalid")


async def test_the_grid_charge_current_never_exceeds_the_fresh_charge_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _rig_plant(rig, r108=40, r111=1)
        status, body = await preview(rig, "deye1", "grid_charge_current", 41)
        assert (status, err(body)) == (422, "bound_max")
        assert (await preview(rig, "deye1", "grid_charge_current", 40))[0] == 200


async def test_curve_settings_are_locked_by_the_bms_description_and_the_type_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        inv = await _rig_plant(rig)
        inv.features = inv.features.model_copy(update={"bms": True})
        _status, body = await preview(rig, "deye1", "battery_float_voltage", "52")
        assert body["error"]["params"]["reason"] == "bms_battery"
        _, cur = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        row = next(r for r in cur["operations"] if r["key"] == "battery_float_voltage")
        assert (row["state"], row["reason"]) == ("locked", "bms_battery")  # shown, not hidden
        inv.features = inv.features.model_copy(update={"bms": False})
        rig.servers["gw1"].responder.regs[98] = 1
        _status, body = await preview(rig, "deye1", "battery_float_voltage", "52")
        assert body["error"]["params"]["reason"] == "bms_curve_skipped"
        rig.servers["gw1"].responder.regs[98] = 0
        assert (await preview(rig, "deye1", "battery_float_voltage", "52"))[0] == 200


async def test_the_efficiency_is_locked_while_the_capacity_is_not_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _rig_plant(rig, r102=0)
        _status, body = await preview(rig, "deye1", "battery_charge_efficiency", "98.0")
        assert body["error"]["params"]["reason"] == "capacity_not_positive"


async def test_the_activation_switch_writes_the_whole_word_and_refuses_foreign_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _rig_plant(rig)
        op = await write(rig, "deye1", "battery_activation", 1)
        assert rig.servers["gw1"].responder.regs[112] == 1 and op["status"] == "readback_confirmed"
    async with plant_rig(tmp_path / "x", monkeypatch) as rig:
        await _rig_plant(rig, r112=0x0100)
        status, body = await preview(rig, "deye1", "battery_activation", 1)
        assert (status, body["error"]["params"]["reason"]) == (409, "unexpected_bits")


async def test_the_negated_coefficient_is_stored_as_its_magnitude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _rig_plant(rig, r107=5)
        status, body = await preview(rig, "deye1", "battery_temperature_compensation", 4)
        assert (status, err(body)) == (422, "value_out_of_range")
        await write(rig, "deye1", "battery_temperature_compensation", -12)
        assert rig.servers["gw1"].responder.regs[107] == 12
        _, cur = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        row = next(r for r in cur["operations"] if r["key"] == "battery_temperature_compensation")
        assert row["value"] == -12


def test_the_description_does_not_preselect_curve_settings_with_a_bms_battery() -> None:
    from deye_monitor.profiles.setting_presets import suggested_operations

    with_bms = suggested_operations(Features(bms=True, battery_control_basis="voltage"))
    assert not {
        k for k in with_bms if k in ("battery_float_voltage", "battery_equalization_interval")
    }
    no_bms = suggested_operations(Features(bms=False, battery_control_basis="voltage"))
    assert {"battery_float_voltage", "battery_shutdown_voltage", "battery_capacity"} <= set(no_bms)
    soc = suggested_operations(Features(bms=False, battery_control_basis="soc"))
    assert "battery_shutdown_voltage" not in soc
