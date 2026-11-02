"""Codex review 34 (live 39): R34-01 commissioned EV reserve, R34-02 external zero-export policy,
R34-03 activation controls validate the armed parameter, R34-04 real calendar dates, R34-05 an
explicit zero export allowance, R34-06 R190 generator source selector R189.

Positive and negative case per finding; the contextual ones also run through the late (direct /
MQTT-style) send point, and the activation controls additionally through a real MQTT bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deye_monitor.ha_discovery import Namespace, setting_component
from deye_monitor.selection import ChangeError, Edit, make_features
from deye_monitor.write_ops import BY_KEY
from tests.fakes.mqtt import settle
from tests.plant_helpers import plant_rig
from tests.test_mqtt_writes_plant import MASTER, attach
from tests.test_review32_b2b import _describe, _late, _lock, _plant
from tests.test_selection import manager_at
from tests.test_writes_v2 import prepare_direct, ready, rows
from tests.write_helpers import err, fast, preview, svc_of, write

NS = Namespace("ab12cd34", "homeassistant", "deye")
GEN_ON, GRID_ON, BOTH_OFF = 0x2AAE, 0x2ABA, 0x2AAA  # R178 with the generator / grid pair 11

# -- R34-01 ----------------------------------------------------------------------------------


async def test_the_ev_off_grid_soc_stays_unavailable_without_the_commissioned_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r259": 0x32DD, "r111": 1}, ev_offgrid_reserve_soc=None)
        assert await _lock(rig, "ev_offgrid_soc", 20) == "ev_reserve_missing"
        assert await _lock(rig, "ev_offgrid_soc", 90) == "ev_reserve_missing"
        row = (await rows(rig))["ev_offgrid_soc"]
        assert (row["state"], row["reason"], row["floor"]) == ("locked", "ev_reserve_missing", None)
        assert rig.servers["gw1"].responder.writes == []


async def test_the_target_must_reach_the_reserve_and_the_battery_low_soc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:  # R117 = 35
        await _plant(rig, {"r259": 0x32DD, "r111": 1}, ev_offgrid_reserve_soc=60)
        status, body = await preview(rig, "deye1", "ev_offgrid_soc", 50)
        params = body["error"]["params"]
        assert (status, err(body)) == (422, "ev_soc_below_reserve")
        assert (params["floor"], params["reserve"], params["battery_low"]) == (60, 60, 35)
        assert (await rows(rig))["ev_offgrid_soc"]["floor"] == 60  # shown, never applied silently
        op = await write(rig, "deye1", "ev_offgrid_soc", 60)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[259] >> 8 == 60  # exactly the request
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:  # the reserve alone is not enough
        await _plant(rig, {"r259": 0x32DD, "r111": 1}, ev_offgrid_reserve_soc=20)
        status, body = await preview(rig, "deye1", "ev_offgrid_soc", 30)
        assert (status, err(body)) == (422, "ev_soc_below_reserve")
        assert body["error"]["params"]["floor"] == 35  # R117 is a stated additional bound
        assert (await preview(rig, "deye1", "ev_offgrid_soc", 40))[0] == 200
    async with plant_rig(tmp_path / "c", monkeypatch) as rig:  # a zero reserve is a stated policy
        await _plant(rig, {"r259": 0x32DD, "r111": 1, "r117": 0}, ev_offgrid_reserve_soc=0)
        assert (await preview(rig, "deye1", "ev_offgrid_soc", 10))[0] == 200


async def test_an_unreadable_battery_low_soc_refuses_and_the_reserve_is_rechecked_at_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r259": 0x32DD, "r111": 1}, ev_offgrid_reserve_soc=20)
        op = await _late(rig, "ev_offgrid_soc", 40, {117: 0xFFFF})
        assert op["reason"] == "companion_invalid"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r259": 0x32DD, "r111": 1}, ev_offgrid_reserve_soc=20)
        svc = await ready(rig)
        permit, _, _ = await prepare_direct(svc, "ev_offgrid_soc", 40)
        _describe(rig, ev_offgrid_reserve_soc=70)  # the description changes before the send point
        op = await svc._run(permit)
        assert (op["status"], op["reason"]) == ("rejected", "ev_soc_below_reserve")
        assert rig.servers["gw1"].responder.writes == []
        _describe(rig, ev_offgrid_reserve_soc=None)
        assert (await _lock(rig, "ev_offgrid_soc", 90)) == "ev_reserve_missing"


@pytest.mark.parametrize(("reserve", "low", "minimum"), [(55, 35, 60), (60, 35, 60), (0, 35, 40)])
async def test_the_effective_floor_reaches_the_snapshot_and_the_ha_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reserve: int, low: int, minimum: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r259": 0x32DD, "r111": 1, "r117": low}, ev_offgrid_reserve_soc=reserve)
        snap = await svc_of(rig).snapshot("deye1")
        state = snap.settings["ev_offgrid_soc"]
        assert state.floor == max(reserve, low)
        _, comp = setting_component(
            NS, "deye1", BY_KEY["ev_offgrid_soc"], "t1", state.cap, state.floor
        )
        assert (comp["min"], comp["max"], comp["step"]) == (minimum, 100, 10)
        # the descriptors without a floor keep the document minimum
        _, other = setting_component(NS, "deye1", BY_KEY["ev_max_power"], "t1", 8000, None)
        assert other["min"] == 0


def test_the_ev_reserve_is_a_validated_optional_installation_number() -> None:
    assert make_features({"ev_offgrid_reserve_soc": 0}).ev_offgrid_reserve_soc == 0
    assert make_features({"ev_offgrid_reserve_soc": 100}).ev_offgrid_reserve_soc == 100
    assert make_features({"ev_offgrid_reserve_soc": None}).ev_offgrid_reserve_soc is None
    for bad in (-1, 101, True, "50", 5.5):
        with pytest.raises(ChangeError):
            make_features({"ev_offgrid_reserve_soc": bad})


# -- R34-02 ----------------------------------------------------------------------------------

EXTERNAL = {"export_policy": "zero_export_external", "external_measurement": "meter"}


async def test_an_active_meter_communication_warning_blocks_external_zero_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 1, "r344": 1, "r553": 0x0008}, **EXTERNAL)  # W4 = bit 3
        assert await _lock(rig, "export_mode", "external_zero") == "measurement_fault_active"
        assert (await rows(rig))["export_mode"]["options"]["external_zero"] == (
            "measurement_fault_active"
        )
        assert rig.servers["gw1"].responder.writes == []
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:  # other warnings do not apply
        await _plant(rig, {"r142": 1, "r344": 1, "r553": 0xFFF7}, **EXTERNAL)
        assert (await write(rig, "deye1", "export_mode", "external_zero"))["status"] == (
            "readback_confirmed"
        )
    async with plant_rig(tmp_path / "c", monkeypatch) as rig:  # internal zero-export is not gated
        await _plant(
            rig, {"r142": 2, "r344": 1, "r553": 0x0008},
            export_policy="zero_export_load", external_measurement="meter",
        )  # fmt: skip
        assert (await write(rig, "deye1", "export_mode", "internal_zero"))["status"] == (
            "readback_confirmed"
        )


async def test_an_unreadable_measurement_status_and_a_late_warning_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 1, "r344": 1, "r553": 0xFFFF}, **EXTERNAL)
        assert await _lock(rig, "export_mode", "external_zero") == "measurement_status_unreadable"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r142": 1, "r344": 1}, **EXTERNAL)
        op = await _late(rig, "export_mode", "external_zero", {553: 0x0008})
        assert op["reason"] == "measurement_fault_active"


@pytest.mark.parametrize(
    ("measurement", "r344", "reason"),
    [
        ("meter", 4, "measurement_path_unsupported"),  # bit 2: a layout not established here
        ("meter", 5, "measurement_path_unsupported"),
        ("meter_and_second_meter", 7, "measurement_path_unsupported"),
        ("ct_and_second_meter", 4, "measurement_path_unsupported"),
        ("ct_and_second_meter", 0, "measurement_path_mismatch"),
        ("meter", 3, "measurement_path_mismatch"),  # a second meter configured, not described
    ],
)
async def test_only_the_established_r344_layout_counts_as_the_described_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, measurement: str, r344: int, reason: str
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r142": 1, "r344": r344},
            export_policy="zero_export_external", external_measurement=measurement,
        )  # fmt: skip
        assert await _lock(rig, "export_mode", "external_zero") == reason


# -- R34-03 ----------------------------------------------------------------------------------


def _peak(rig: Any, **regs: int) -> None:
    rig.servers["gw1"].responder.regs.update({int(k[1:]): v for k, v in regs.items()})


async def test_a_mode_change_cannot_arm_a_peak_power_over_the_installation_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = {"r142": 1, "r178": GEN_ON, "r190": 7000, "r133": 0}
    async with plant_rig(tmp_path, monkeypatch) as rig:  # 8 kW model cap, generator rated 3 kW
        await _plant(
            rig, gen, export_policy="export_allowed",
            gen_port_mode="generator", generator_capacity_w=3000,
        )  # fmt: skip
        assert await _lock(rig, "export_mode", "sell") == "peak_power_exceeds_envelope"
        assert rig.servers["gw1"].responder.writes == []
        _peak(rig, r190=3000)  # lowered: readable, now within the envelope
        assert (await write(rig, "deye1", "export_mode", "sell"))["status"] == "readback_confirmed"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:  # the grid pair, GRID envelope
        await _plant(
            rig, {"r142": 1, "r178": GRID_ON, "r191": 7000},
            export_policy="export_allowed", grid_limit_w=3000,
        )  # fmt: skip
        assert await _lock(rig, "export_mode", "sell") == "peak_power_exceeds_envelope"
    async with plant_rig(tmp_path / "c", monkeypatch) as rig:  # OFF pairs arm nothing
        await _plant(
            rig, {"r142": 1, "r178": BOTH_OFF, "r190": 7000, "r191": 7000},
            export_policy="export_allowed", generator_capacity_w=3000, grid_limit_w=3000,
        )  # fmt: skip
        assert (await write(rig, "deye1", "export_mode", "sell"))["status"] == "readback_confirmed"


@pytest.mark.parametrize(
    ("regs", "described", "reason"),
    [
        ({"r178": 0xFFFF}, {}, "peak_context_unknown"),
        ({"r178": GEN_ON, "r190": 0xFFFF, "r133": 0}, {}, "companion_invalid"),
        ({"r178": GEN_ON, "r190": 5000, "r133": 0, "r189": 1}, {}, "gen_source_on_grid"),
        (
            {"r178": GEN_ON, "r190": 5000, "r133": 0, "r189": 0xFFFF},
            {},
            "gen_source_selector_unknown",
        ),
        ({"r178": GEN_ON, "r190": 5000, "r133": 1}, {}, "gen_port_not_generator"),
        (
            {"r178": GEN_ON, "r190": 5000, "r133": 0},
            {"generator_capacity_w": None},
            ("envelope_missing"),
        ),
        ({"r178": GRID_ON, "r191": 5000}, {"grid_limit_w": None}, "envelope_missing"),
    ],
)
async def test_unknown_or_unsupported_peak_context_is_not_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    regs: dict[str, int], described: dict[str, Any], reason: str,
) -> None:  # fmt: skip
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r142": 1, **regs},
            export_policy="export_allowed", gen_port_mode="generator", **described,
        )  # fmt: skip
        assert await _lock(rig, "export_mode", "sell") == reason
        assert rig.servers["gw1"].responder.writes == []


async def test_the_peak_shaving_switches_validate_the_power_they_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r178": BOTH_OFF, "r133": 0, "r190": 7000, "r191": 7000},
            gen_port_mode="generator", generator_capacity_w=3000, grid_limit_w=3000,
        )  # fmt: skip
        assert await _lock(rig, "generator_peak_shaving", 1) == "peak_power_exceeds_envelope"
        assert await _lock(rig, "grid_peak_shaving", 1) == "peak_power_exceeds_envelope"
        assert rig.servers["gw1"].responder.writes == []
        _peak(rig, r190=3000, r191=3000)  # lowering is a direct write elsewhere; here the words
        assert (await write(rig, "deye1", "generator_peak_shaving", 1))["status"] == (
            "readback_confirmed"
        )
        assert (await write(rig, "deye1", "grid_peak_shaving", 1))["status"] == (
            "readback_confirmed"
        )
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        # OFF is always available, also over a stored over-limit value, a lost source and no limit
        await _plant(
            rig, {"r178": GEN_ON, "r133": 1, "r190": 7000, "r189": 1},
            gen_port_mode="generator", generator_capacity_w=None,
        )  # fmt: skip
        assert (await write(rig, "deye1", "generator_peak_shaving", 0))["status"] == (
            "readback_confirmed"
        )


async def test_the_peak_shaving_switch_is_checked_again_at_the_late_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r178": BOTH_OFF, "r133": 0, "r190": 3000}, gen_port_mode="generator",
            generator_capacity_w=3000,
        )  # fmt: skip
        op = await _late(rig, "generator_peak_shaving", 1, {190: 7000})
        assert op["reason"] == "peak_power_exceeds_envelope"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r142": 1, "r178": GEN_ON, "r133": 0}, export_policy="export_allowed",
                     gen_port_mode="generator")  # fmt: skip
        op = await _late(rig, "export_mode", "sell", {178: 0xFFFF})
        assert op["reason"] == "peak_context_unknown"


async def test_mqtt_commands_cannot_arm_over_the_envelope_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        regs = {"r336": MASTER, "r142": 1, "r178": BOTH_OFF, "r133": 0, "r190": 7000}
        await _plant(
            rig, regs, export_policy="export_allowed", gen_port_mode="generator",
            generator_capacity_w=3000,
        )  # fmt: skip
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("generator_peak_shaving", "ON")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["event_type"] == "write_rejected"
            _peak(rig, r178=GEN_ON)  # armed elsewhere (LCD): the mode change must not arm over it
            bridge.send("export_mode", "Selling First")
            await settle(lambda: len(bridge.events()) == 2)
            assert bridge.events()[1]["event_type"] == "write_rejected"
            assert rig.servers["gw1"].responder.writes == []
            bridge.send("generator_peak_shaving", "OFF")  # OFF stays possible
            await settle(lambda: len(bridge.events()) == 3)
            assert bridge.events()[2]["event_type"] == "write_readback_confirmed"


# -- R34-04 ----------------------------------------------------------------------------------


def _clock(year: int, month: int, day: int, hour: int = 12, minute: int = 30, sec: int = 15) -> Any:
    return {
        "r62": ((year - 2000) << 8) | month,
        "r63": (day << 8) | hour,
        "r64": (minute << 8) | sec,
    }


@pytest.mark.parametrize(
    ("date", "valid"),
    [
        ((2026, 2, 31), False),
        ((2027, 2, 29), False),
        ((2028, 2, 29), True),  # leap year
        ((2000, 2, 29), True),
        ((2026, 4, 31), False),
        ((2026, 4, 30), True),
        ((2026, 12, 31), True),
        ((2099, 12, 31), True),
        ((2255, 1, 1), False),  # outside the supported 2000..2099
        ((2100, 1, 1), False),
        ((2026, 0, 1), False),
        ((2026, 13, 1), False),
        ((2026, 1, 0), False),
    ],
)
async def test_the_clock_is_a_real_gregorian_date_in_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, date: tuple[int, int, int], valid: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, _clock(*date))
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        shown = body["info"]["tou"]["clock"]
        assert (shown is not None) is valid, shown  # never an impossible date formatted as valid
        if valid:
            assert shown.startswith(f"{date[0]:04d}-{date[1]:02d}-{date[2]:02d} ")
        status, body = await preview(rig, "deye1", "tou_spain_mode", 1)
        if valid:
            assert status == 200
        else:
            assert (status, err(body)) == (409, "operation_locked")
            assert body["error"]["params"]["reason"] == "tou_clock_invalid"
        snap = await svc_of(rig).snapshot("deye1")
        assert (snap.settings["tou_weekday_mon"].reason is None) is valid


async def test_a_bad_clock_still_allows_spain_off_and_blocks_the_late_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r146": 0x01FE, **_clock(2026, 2, 31)})
        assert (await write(rig, "deye1", "tou_spain_mode", 0))["status"] == "readback_confirmed"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, _clock(2026, 2, 28))
        op = await _late(rig, "tou_spain_mode", 1, _clock_words(2026, 2, 31))
        assert op["reason"] == "tou_clock_invalid"


def _clock_words(year: int, month: int, day: int) -> dict[int, int]:
    regs = _clock(year, month, day)
    return {62: regs["r62"], 63: regs["r63"], 64: regs["r64"]}


# -- R34-05 ----------------------------------------------------------------------------------


def test_an_explicit_zero_export_allowance_differs_from_not_entered() -> None:
    assert make_features({"export_limit_w": 0}).export_limit_w == 0
    assert make_features({"export_limit_w": None}).export_limit_w is None
    assert make_features({}).export_limit_w is None
    assert make_features({"export_limit_w": 100}).export_limit_w == 100
    for bad in (-1, False, "0", 0.0, 100_001):
        with pytest.raises(ChangeError):
            make_features({"export_limit_w": bad})
    for name in ("grid_limit_w", "generator_capacity_w", "ev_charger_limit_w"):
        for bad in (0, 99):
            with pytest.raises(ChangeError):
                make_features({name: bad})


async def test_zero_allowance_limits_r143_to_zero_and_never_arms_selling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 0, "r143": 3000, "r145": 0}, export_limit_w=0)
        table = await rows(rig)
        assert (table["export_max_power"]["state"], table["export_max_power"]["cap"]) == (
            "ready",
            0,
        )  # the empty domain {0}: lowering to 0 stays possible
        assert table["pv_sale_max_power"]["reason"] == "envelope_too_low"  # positive minimum 400
        status, body = await preview(rig, "deye1", "export_max_power", 100)
        assert (status, err(body)) == (422, "value_out_of_range")
        assert (await write(rig, "deye1", "export_max_power", 0))["status"] == "readback_confirmed"
        snap = await svc_of(rig).snapshot("deye1")  # HA: a number with min = max = 0
        _, comp = setting_component(
            NS, "deye1", BY_KEY["export_max_power"], "t1", snap.settings["export_max_power"].cap
        )
        assert (comp["min"], comp["max"]) == (0, 0)
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:  # zero never arms Solar Sell
        await _plant(
            rig, {"r142": 1, "r143": 0, "r145": 0}, export_limit_w=0,
            export_policy="zero_export_load",
        )  # fmt: skip
        assert await _lock(rig, "pv_sale_enable", 1) == "export_allowance_zero"
    async with plant_rig(tmp_path / "c", monkeypatch) as rig:  # nor selling first
        await _plant(rig, {"r142": 1, "r143": 0}, export_limit_w=0, export_policy="export_allowed")
        assert await _lock(rig, "export_mode", "sell") == "export_allowance_zero"
        _describe(rig, export_limit_w=None)  # "not entered" is a different state
        assert await _lock(rig, "export_mode", "sell") == "envelope_missing"


async def test_a_zero_allowance_survives_save_and_reload_and_blank_clears_it(
    tmp_path: Path,
) -> None:
    m = manager_at(tmp_path)
    edit = Edit(features={"export_limit_w": 0, "ev_offgrid_reserve_soc": 0, "grid_limit_w": 6000})
    await m.change(0, {"deye1": edit})
    again = manager_at(tmp_path)
    features = again.selection("deye1").features
    assert (features.export_limit_w, features.ev_offgrid_reserve_soc) == (0, 0)  # not None
    assert features.grid_limit_w == 6000
    await again.change(1, {"deye1": Edit(features={"export_limit_w": None})})
    assert manager_at(tmp_path).selection("deye1").features.export_limit_w is None


# -- R34-06 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("r189", "reason"),
    [
        (1, "gen_source_on_grid"),
        (0xFFFF, "gen_source_selector_unknown"),
        (2, "gen_source_selector_unknown"),
    ],
)
async def test_r190_needs_the_generator_input_selector_on_the_gen_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r189: int, reason: str
) -> None:
    regs = {"r178": GEN_ON, "r133": 0, "r190": 5000}
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {**regs, "r189": r189}, gen_port_mode="generator")
        assert await _lock(rig, "generator_peak_shaving_power", 4000) == reason
        assert (await rows(rig))["generator_peak_shaving_power"]["reason"] == reason
        assert rig.servers["gw1"].responder.writes == []
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {**regs, "r189": 0}, gen_port_mode="generator")
        assert (await write(rig, "deye1", "generator_peak_shaving_power", 4000))["status"] == (
            "readback_confirmed"
        )
    async with plant_rig(tmp_path / "c", monkeypatch) as rig:  # the selector moves before send
        await _plant(rig, {**regs, "r189": 0}, gen_port_mode="generator")
        op = await _late(rig, "generator_peak_shaving_power", 4000, {189: 1})
        assert op["reason"] == "gen_source_on_grid"
