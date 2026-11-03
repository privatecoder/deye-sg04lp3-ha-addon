"""Codex review 32 (B2b): R32-01 Solar Sell mode gate, R32-02 BMS mode gate, R32-03 R142 transition,
R32-04 EV applicability, R32-05 installation envelopes, R32-06 AFCI verdicts, R32-07 R190 source,
R32-08 preset provenance, R32-09 ToU branch prerequisites, R32-10 R141, R32-11 context reads.

Every finding has a positive and a negative case; the contextual ones also run through the late
(direct / MQTT-style) send point, where the words of the critical section decide again."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deye_monitor.ha_discovery import Namespace, setting_component
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.setting_presets import suggested_operations
from deye_monitor.selection import ChangeError, make_features
from deye_monitor.write_ops import BY_KEY
from deye_monitor.writes import WriteService, settings_read_requests
from tests.plant_helpers import plant_rig
from tests.test_writes_v2 import prepare_direct, ready, rows
from tests.write_helpers import build_plant, err, preview, svc_of, write


def _describe(rig: Any, **update: Any) -> None:
    inv = svc_of(rig).runtime.inverters["deye1"]
    inv.features = inv.features.model_copy(update={"utility_grid": True, **update})


async def _plant(rig: Any, regs: dict[str, int], **described: Any) -> None:
    await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r178": 0x2AAA, **regs}})
    _describe(rig, **described)


async def _lock(rig: Any, key: str, value: Any) -> str:
    status, body = await preview(rig, "deye1", key, value)
    assert (status, err(body)) == (409, "operation_locked"), (key, value, body)
    return str(body["error"]["params"]["reason"])


async def _late(rig: Any, key: str, value: Any, mutate: dict[int, int]) -> dict[str, Any]:
    """A direct request accepted while the context was fine; the registers change before the
    send point. Returns the operation record: it must be a rejection and nothing is written."""
    svc = await ready(rig)
    permit, _, _ = await prepare_direct(svc, key, value)
    rig.servers["gw1"].responder.regs.update(mutate)
    op: dict[str, Any] = await svc._run(permit)
    assert op["status"] == "rejected", op
    assert rig.servers["gw1"].responder.writes == []
    return op


# -- R32-01 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [1, 2])
async def test_solar_sell_is_offered_with_zero_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": mode, "r145": 0}, export_policy="zero_export_load")
        table = await rows(rig)
        assert table["pv_sale_enable"]["state"] == "ready"
        op = await write(rig, "deye1", "pv_sale_enable", 1)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[145] == 1


async def test_solar_sell_is_closed_in_the_selling_mode_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 0, "r145": 0}, export_policy="export_allowed")
        assert (await rows(rig))["pv_sale_enable"]["reason"] == "solar_sell_mode_required"
        assert await _lock(rig, "pv_sale_enable", 1) == "solar_sell_mode_required"
        snap = await svc_of(rig).snapshot("deye1")
        assert snap.settings["pv_sale_enable"].ready is False
        assert snap.settings["pv_sale_enable"].reason == "solar_sell_mode_required"
        assert rig.servers["gw1"].responder.writes == []


async def test_solar_sell_on_needs_the_allowance_and_a_valid_limit_but_off_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 1, "r145": 0, "r143": 3000}, export_limit_w=None)
        assert await _lock(rig, "pv_sale_enable", 1) == "envelope_missing"
        _describe(rig, export_limit_w=2000)  # R143 = 3000 above the entered allowance
        assert await _lock(rig, "pv_sale_enable", 1) == "export_limit_exceeded"
        _describe(rig, export_limit_w=12000)
        rig.servers["gw1"].responder.regs[143] = 0xFFFF
        assert await _lock(rig, "pv_sale_enable", 1) == "export_limit_invalid"
        rig.servers["gw1"].responder.regs[143] = 3000
        assert (await preview(rig, "deye1", "pv_sale_enable", 1))[0] == 200
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r142": 1, "r145": 1, "r143": 0xFFFF}, export_limit_w=None)
        op = await write(rig, "deye1", "pv_sale_enable", 0)  # switching OFF stays possible
        assert op["status"] == "readback_confirmed"
        rig.servers["gw1"].responder.regs[145] = 2
        assert await _lock(rig, "pv_sale_enable", 0) == "unexpected_bits"  # whole word 0 / 1 only


async def test_solar_sell_is_checked_again_at_the_late_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r142": 1, "r145": 0}, export_policy="zero_export_load")
        op = await _late(rig, "pv_sale_enable", 1, {142: 0})
        assert op["reason"] == "solar_sell_mode_required"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r142": 2, "r145": 0})
        op = await _late(rig, "pv_sale_enable", 1, {143: 0xFFFF})
        assert op["reason"] == "export_limit_invalid"


def test_solar_sell_is_suggested_for_the_zero_export_modes_only() -> None:
    zero = Features(export_policy="zero_export_external", export_limit_w=3000)
    assert "pv_sale_enable" in suggested_operations(zero)
    assert "pv_sale_enable" not in suggested_operations(Features(export_policy="export_allowed"))
    assert "pv_sale_max_power" in suggested_operations(Features(export_policy="export_allowed"))


# -- R32-02 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0, 0xFFFF, 2])
async def test_bms_err_stop_needs_the_fresh_lithium_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r178": 0x2AAA, "r98": mode}, battery=True, bms=True)
        assert await _lock(rig, "bms_loss_fault", 1) == "bms_mode_required"
        assert await _lock(rig, "bms_loss_fault", 0) == "bms_mode_required"
        snap = await svc_of(rig).snapshot("deye1")
        assert snap.settings["bms_loss_fault"].reason == "bms_mode_required"
        assert rig.servers["gw1"].responder.writes == []


async def test_bms_err_stop_off_stays_possible_without_healthy_communication_and_rechecks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r178": 0x2EAA, "r98": 1}, battery=True, bms=True)
        op = await write(
            rig, "deye1", "bms_loss_fault", 0
        )  # a deliberate OFF (alarm words ignored)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[178] == 0x2AAA
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r178": 0x2AAA, "r98": 1}, battery=True, bms=True)
        op = await _late(rig, "bms_loss_fault", 1, {98: 0})
        assert op["reason"] == "bms_mode_required"


# -- R32-03 ----------------------------------------------------------------------------------


async def test_selling_needs_a_valid_export_limit_within_the_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r142": 1, "r143": 0xFFFF, "r145": 0xFFFF, "r190": 0xFFFF},
            export_policy="export_allowed",
        )  # fmt: skip
        assert await _lock(rig, "export_mode", "sell") == "companion_invalid"  # R145
        rig.servers["gw1"].responder.regs[145] = 0
        assert await _lock(rig, "export_mode", "sell") == "export_limit_invalid"  # R143
        rig.servers["gw1"].responder.regs[143] = 3000
        status, body = await preview(rig, "deye1", "export_mode", "sell")
        assert status == 200, body
        shown = {c["register"]: c["word"] for c in body["preview"]["companions"]}
        assert {143, 145, 190, 191, 344} <= set(shown) and shown[143] == 3000
        _describe(rig, export_limit_w=None)
        assert await _lock(rig, "export_mode", "sell") == "envelope_missing"
        _describe(rig, export_limit_w=2500)
        assert await _lock(rig, "export_mode", "sell") == "export_limit_exceeded"
        assert rig.servers["gw1"].responder.writes == []  # nothing is repaired


async def test_an_enabled_peak_shaving_power_must_be_valid_for_a_mode_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r142": 0, "r178": 0x2ABE, "r190": 0xFFFF, "r133": 0},
            export_policy="zero_export_load", gen_port_mode="generator",
        )  # fmt: skip
        assert await _lock(rig, "export_mode", "internal_zero") == "companion_invalid"
        rig.servers["gw1"].responder.regs[190] = 5000
        assert (await preview(rig, "deye1", "export_mode", "internal_zero"))[0] == 200


@pytest.mark.parametrize(
    ("measurement", "r344", "reason"),
    [
        ("none", 0, "measurement_not_described"),
        ("unknown", 0, "measurement_not_described"),
        ("ct", 1, "measurement_path_mismatch"),  # configured meter, described CT
        ("meter", 0, "measurement_path_mismatch"),
        ("ct", 2, "measurement_path_mismatch"),  # a second meter is configured, not described
        ("ct", 0xFFFF, "measurement_path_unreadable"),
    ],
)
async def test_external_zero_export_needs_the_described_and_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, measurement: str, r344: int, reason: str
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r142": 1, "r344": r344},
            export_policy="zero_export_external", external_measurement=measurement,
        )  # fmt: skip
        assert await _lock(rig, "export_mode", "external_zero") == reason
        row = (await rows(rig))["export_mode"]
        assert row["options"]["external_zero"] == reason
        assert rig.servers["gw1"].responder.writes == []


@pytest.mark.parametrize(
    ("measurement", "r344"),
    [("ct", 0), ("meter", 1), ("ct_and_second_meter", 2), ("meter_and_second_meter", 3)],
)
async def test_external_zero_export_is_offered_with_a_matching_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, measurement: str, r344: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r142": 1, "r344": r344},
            export_policy="zero_export_external", external_measurement=measurement,
        )  # fmt: skip
        op = await write(rig, "deye1", "export_mode", "external_zero")
        assert op["status"] == "readback_confirmed"


async def test_the_export_mode_is_checked_again_at_the_late_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(
            rig, {"r142": 1, "r344": 0},
            export_policy="zero_export_external", external_measurement="ct",
        )  # fmt: skip
        op = await _late(rig, "export_mode", "external_zero", {344: 1})
        assert op["reason"] == "measurement_path_mismatch"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r142": 1, "r143": 3000}, export_policy="export_allowed")
        op = await _late(rig, "export_mode", "sell", {143: 0xFFFF})
        assert op["reason"] == "export_limit_invalid"


# -- R32-04 ----------------------------------------------------------------------------------

EV_KEYS = (
    ("ev_free_work", 1),
    ("ev_location", "load"),
    ("ev_offgrid_soc", 60),
    ("ev_max_power", 3100),
)


@pytest.mark.parametrize("basis", [0, 2, 0xFFFF])
async def test_the_ev_soc_threshold_needs_the_soc_basis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, basis: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r111": basis, "r259": 0x32DD})
        assert await _lock(rig, "ev_offgrid_soc", 60) == "soc_mode_required"
        status, body = await preview(rig, "deye1", "ev_max_power", 3100)  # the power does not
        assert status == 200, body


@pytest.mark.parametrize("word", [0xFFDD, 0x65DD, 0x6EDD])
async def test_an_invalid_soc_byte_blocks_every_ev_change_including_the_power(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r259": word})
        for key, value in EV_KEYS:
            assert await _lock(rig, key, value) == "ev_soc_word_invalid", key
        assert rig.servers["gw1"].responder.writes == []


async def test_ev_settings_need_a_described_charger_on_the_configured_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r259": 0x32DD}, ev_charger="unknown")
        for key, value in EV_KEYS:
            assert await _lock(rig, key, value) == "ev_charger_not_described", key
        _describe(rig, ev_charger="load_port")  # described LOAD, configured GRID
        for key, value in (EV_KEYS[0], EV_KEYS[2], EV_KEYS[3]):
            assert await _lock(rig, key, value) == "ev_location_mismatch", key
        status, body = await preview(rig, "deye1", "ev_location", "load")  # repairing it is allowed
        assert status == 200, body
        assert await _lock(rig, "ev_location", "grid") == "ev_port_not_described"
        assert rig.servers["gw1"].responder.writes == []


async def test_the_ev_soc_respects_the_reserve_floor_and_never_coerces_a_bad_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r259": 0x32DD, "r117": 35})
        status, body = await preview(rig, "deye1", "ev_offgrid_soc", 30)
        assert (status, err(body)) == (422, "ev_soc_below_reserve")
        assert (await preview(rig, "deye1", "ev_offgrid_soc", 40))[0] == 200
        rig.servers["gw1"].responder.regs[117] = 0xFFFF
        status, body = await preview(rig, "deye1", "ev_offgrid_soc", 40)
        assert (status, err(body)) == (409, "companion_invalid")


async def test_the_ev_context_is_checked_again_at_the_late_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r259": 0x32DD})
        op = await _late(rig, "ev_max_power", 3100, {259: 0xFFDD})
        assert op["reason"] == "ev_soc_word_invalid"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r259": 0x32DD, "r111": 1})
        op = await _late(rig, "ev_offgrid_soc", 60, {111: 0})
        assert op["reason"] == "soc_mode_required"
        op = await _late(rig, "ev_free_work", 1, {259: 0x32ED})  # moved to the LOAD port
        assert op["reason"] == "ev_location_mismatch"


# -- R32-05 ----------------------------------------------------------------------------------

ENVELOPES = (
    # key, feature, register, step, value above the entered limit, value within it
    ("export_max_power", "export_limit_w", 143, 100, 3600, 3500),
    ("pv_sale_max_power", "export_limit_w", 340, 100, 3600, 3500),
    ("grid_peak_shaving_power", "grid_limit_w", 191, 100, 3600, 3500),
    ("generator_peak_shaving_power", "generator_capacity_w", 190, 10, 3600, 3500),
    ("ev_max_power", "ev_charger_limit_w", 260, 100, 3600, 3500),
)


@pytest.mark.parametrize(("key", "feature", "register", "step", "above", "within"), ENVELOPES)
async def test_an_installation_limit_caps_the_maximum_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    key: str, feature: str, register: int, step: int, above: int, within: int,
) -> None:  # fmt: skip
    regs = {"r178": 0x2ABE, "r133": 0, "r142": 0, "r259": 0x32DD}
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, regs, gen_port_mode="generator", **{feature: None})
        assert await _lock(rig, key, within) == "envelope_missing"  # the missing input, named
        assert (await rows(rig))[key]["reason"] == "envelope_missing"
        _describe(rig, **{feature: 3550})  # aligned down to the step: 3500
        table = await rows(rig)
        desc = BY_KEY[key]
        cap = 3550 - (3550 - desc.min_raw) % desc.step_raw  # aligned down to the step
        assert table[key]["state"] == "ready" and table[key]["cap"] == cap
        status, body = await preview(rig, "deye1", key, above)
        assert (status, err(body)) == (422, "value_out_of_range")
        assert (await preview(rig, "deye1", key, within))[0] == 200
        snap = await svc_of(rig).snapshot("deye1")  # HA number: same maximum, cap in the discovery
        assert snap.settings[key].cap == cap
        ns = Namespace("ab12cd34", "homeassistant", "deye")
        _, comp = setting_component(ns, "deye1", BY_KEY[key], "t1", snap.settings[key].cap)
        assert comp["max"] == cap
        svc = await ready(rig)
        permit, _, _ = await prepare_direct(svc, key, within)
        _describe(rig, **{feature: 3400})  # the limit shrinks before the send point
        result = await svc._run(permit)
        assert result["status"] == "rejected" and result["reason"] == "value_out_of_range"
        assert rig.servers["gw1"].responder.writes == []


async def test_the_installation_limit_is_intersected_with_the_model_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:  # an 8 kW model
        await _plant(rig, {"r142": 0}, export_limit_w=50000)
        assert (await rows(rig))["export_max_power"]["cap"] == 8000
        _describe(rig, export_limit_w=100)
        status, body = await preview(rig, "deye1", "export_max_power", 200)
        assert (status, err(body)) == (422, "value_out_of_range")
        _describe(rig, export_limit_w=100, ev_charger_limit_w=100)  # below R190 / R191 minimum
        _describe(rig, grid_limit_w=500)
        assert await _lock(rig, "grid_peak_shaving_power", 1000) == "envelope_too_low"


def test_the_installation_numbers_are_strictly_validated() -> None:
    base = Features()
    assert make_features({"export_limit_w": 5000}, base).export_limit_w == 5000
    assert make_features({"export_limit_w": None}, base).export_limit_w is None
    for bad in (True, "5000", -1, 100_001, 12.5):
        with pytest.raises(ChangeError):
            make_features({"export_limit_w": bad}, base)
    for name in ("grid_limit_w", "generator_capacity_w", "ev_charger_limit_w"):
        for bad in (0, 99):  # source and charger ratings stay positive
            with pytest.raises(ChangeError):
                make_features({name: bad}, base)
    with pytest.raises(ChangeError):
        make_features({"ev_charger": "both"}, base)


# -- R32-06 ----------------------------------------------------------------------------------


def _consume(rig: Any, svc: WriteService, after: int, fault_after: int) -> None:
    """The fake firmware: the acknowledged command is normalised (or ignored) and the fault word
    is what it is by the time of the read-back."""

    async def pause(delay: float) -> None:
        regs = rig.servers["gw1"].responder.regs
        regs[181], regs[558] = after, fault_after

    svc.sleep = pause


@pytest.mark.parametrize(
    ("before", "fault_after", "verdict"),
    [(0x4000, 0, "no_longer_reported"), (0, 0, "not_reported"), (0x4000, 0x4000, "still_reported")],
)
async def test_afci_clear_reports_the_command_and_the_fault_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: int, fault_after: int, verdict: str
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r181": 1, "r558": before})
        svc = svc_of(rig)
        _consume(rig, svc, after=1, fault_after=fault_after)  # ACK, then the word reads 1 again
        op = await write(rig, "deye1", "afci_clear_fault", "press")
        # an unchanged 1 is also what an ignored command leaves: consumption is not established
        assert op["status"] == "outcome_unknown" and op["reason"] == "consumption_not_established"
        assert op["target"]["matches_request"] is None
        assert op["target"]["command_state"] == "consumption_unknown"
        assert op["fault"]["state"] == verdict and op["fault"]["word_before"] == before


async def test_afci_clear_with_the_request_still_stored_is_a_stored_request_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r181": 1, "r558": 0})
        op = await write(rig, "deye1", "afci_clear_fault", "press")  # the fake keeps the 2
        assert op["status"] == "readback_confirmed"
        assert op["target"]["command_state"] == "request_stored"
        assert op["fault"]["state"] == "not_reported"  # 0 to 0 is never "no longer reported"
        status, _body = await preview(rig, "deye1", "afci_clear_fault", "press")
        assert status == 409  # the pending 2 locks a further clear (no repeat)


async def test_afci_fault_verdict_is_unknown_without_a_read() -> None:
    verdict = WriteService._fault_verdict
    assert verdict(None, 0, 0x4000) == "unknown"
    assert verdict(0x4000, None, 0x4000) == "unknown"
    assert verdict(None, 0x4000, 0x4000) == "still_reported"
    assert verdict(0x4000, 0, 0x4000) == "no_longer_reported"
    assert verdict(0, 0, 0x4000) == "not_reported"


# -- R32-07 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("described", "r133", "reason"),
    [
        ({"gen_port_mode": "smart_load"}, 1, "gen_source_not_described"),
        ({"gen_port_mode": "unused"}, 1, "gen_source_not_described"),
        ({"gen_port_mode": "unused", "grid_port_use": "generator"}, 1, "gen_source_unresolved"),
        ({"gen_port_mode": "generator"}, 1, "gen_port_not_generator"),
        ({"gen_port_mode": "generator"}, 0xFFFF, "gen_port_not_generator"),
    ],
)
async def test_generator_peak_shaving_power_needs_a_mapped_generator_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    described: dict[str, str],
    r133: int,
    reason: str,
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r178": 0x2AAE, "r133": r133, "r189": 0xFFFF}, **described)
        assert await _lock(rig, "generator_peak_shaving_power", 6000) == reason
        assert rig.servers["gw1"].responder.writes == []


async def test_generator_peak_shaving_power_with_a_generator_on_the_gen_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r178": 0x2AAE, "r133": 0}, gen_port_mode="generator")
        row = (await rows(rig))["generator_peak_shaving_power"]
        assert row["state"] == "ready" and {c["register"] for c in row["companions"]} == {133, 189}
        op = await write(rig, "deye1", "generator_peak_shaving_power", 6000)
        assert op["status"] == "readback_confirmed"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r178": 0x2AAE, "r133": 0}, gen_port_mode="generator")
        op = await _late(rig, "generator_peak_shaving_power", 6000, {133: 1})
        assert op["reason"] == "gen_port_not_generator"


# -- R32-08 ----------------------------------------------------------------------------------


def test_presets_count_only_answered_questions() -> None:
    defaults = Features()
    assert suggested_operations(defaults, set()) == ()  # an untouched description
    assert "bms_loss_fault" not in suggested_operations(defaults, {"battery"})  # partly answered
    assert "bms_loss_fault" not in suggested_operations(defaults, {"bms"})
    assert "bms_loss_fault" in suggested_operations(defaults, {"battery", "bms"})  # accepted
    changed = Features(bms=False)
    assert "bms_loss_fault" not in suggested_operations(changed, {"battery", "bms"})
    assert "bms_loss_fault" not in suggested_operations(Features(battery=False), {"battery", "bms"})


def test_the_utility_grid_suggestion_of_the_reconnection_time_follows_a_saved_answer() -> None:
    assert "grid_reconnection_time" in suggested_operations(Features(), {"utility_grid"})
    assert "grid_reconnection_time" not in suggested_operations(Features(), set())
    island = Features(utility_grid=False, grid_port_use="unused")
    assert "grid_reconnection_time" not in suggested_operations(island, {"utility_grid"})


async def test_the_plant_view_derives_suggestions_from_the_saved_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {})
        status, body = await rig.call("GET", "/api/plant")
        assert body["writes"]["inverters"]["deye1"]["suggested_operations"] == []
        revision = rig.service.manager.revision
        status, body = await rig.call(
            "PUT",
            "/api/inverters/deye1/features",
            {"features": {"battery": True, "bms": True}},
            revision=revision,
        )
        assert status == 200, body
        status, body = await rig.call("GET", "/api/plant")
        suggested = body["writes"]["inverters"]["deye1"]["suggested_operations"]
        assert "bms_loss_fault" in suggested and "grid_reconnection_time" not in suggested


# -- R32-09 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("regs", "reason"),
    [
        ({"r62": 0xFFFF, "r63": 0xFFFF, "r64": 0xFFFF}, "tou_clock_invalid"),
        ({"r111": 0xFFFF}, "tou_basis_invalid"),
        ({"r148": 9999}, "tou_schedule_invalid"),
        ({"r150": 500}, "tou_schedule_invalid"),  # not strictly ascending
    ],
)
async def test_weekday_and_spain_on_need_a_valid_clock_basis_and_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, regs: dict[str, int], reason: str
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, regs)
        assert await _lock(rig, "tou_weekday_mon", 0) == reason
        assert await _lock(rig, "tou_weekday_mon", 1) == reason
        status, body = await preview(rig, "deye1", "tou_spain_mode", 1)
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == reason
        assert rig.servers["gw1"].responder.writes == []


async def test_spain_off_is_always_possible_and_a_valid_context_admits_the_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r146": 0x01FE, "r148": 9999, "r111": 0xFFFF})
        op = await write(rig, "deye1", "tou_spain_mode", 0)  # nothing traps an active Spain mode
        assert op["status"] == "readback_confirmed"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {})
        assert (await preview(rig, "deye1", "tou_weekday_mon", 0))[0] == 200
        op = await write(rig, "deye1", "tou_spain_mode", 1)
        assert op["status"] == "readback_confirmed"


async def test_the_tou_context_is_checked_again_at_the_late_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {})
        op = await _late(rig, "tou_spain_mode", 1, {148: 9999})
        assert op["reason"] == "tou_schedule_invalid"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {})
        op = await _late(rig, "tou_weekday_tue", 0, {111: 0xFFFF})
        assert op["reason"] == "tou_basis_invalid"


@pytest.mark.parametrize(
    ("described", "r133", "ok"),
    [
        ({"gen_port_mode": "generator"}, 0, True),
        ({"gen_port_mode": "unused"}, 0, False),  # not a positive generator declaration
        ({"gen_port_mode": "unused", "grid_port_use": "generator"}, 0, False),
        ({"gen_port_mode": "generator"}, 1, False),  # the actual R133 says Smart Load
        ({"gen_port_mode": "generator"}, 0xFFFF, False),
        ({"gen_port_mode": "smart_load"}, 1, False),
    ],
)
async def test_the_generator_charge_flag_needs_a_positive_source_correct_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, described: dict[str, str], r133: int, ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r133": r133, "r172": 0x0000}, **described)
        status, body = await preview(rig, "deye1", "tou_slot1_gen_charge", 1)
        assert (status == 200) is ok, body
        if not ok:
            assert body["error"]["params"]["reason"] == "gen_port_not_generator"


# -- R32-10 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("start", "target", "after"), [(0, "load_first", 1), (1, "batt_first", 0)])
async def test_the_energy_pattern_is_written_as_one_whole_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start: int, target: str, after: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r141": start})
        op = await write(rig, "deye1", "energy_pattern", target)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[141] == after
        assert [r.start for r in rig.servers["gw1"].responder.writes] == [141]


@pytest.mark.parametrize("word", [2, 3, 0x0100, 0xFFFF])
async def test_the_energy_pattern_rejects_every_other_incumbent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r141": word})
        await _lock(rig, "energy_pattern", "load_first")
        assert rig.servers["gw1"].responder.writes == []


async def test_the_energy_pattern_is_master_only_and_not_a_free_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desc = BY_KEY["energy_pattern"]
    assert desc.synchronized and [o.slug for o in desc.options] == ["batt_first", "load_first"]
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1", "deye2"), enable=("deye2",),
            regs={"deye1": {"r336": 0x0003}, "deye2": {"r336": 0x0001}},
        )  # fmt: skip
        status, body = await preview(rig, "deye2", "energy_pattern", "load_first")
        assert status == 409 and err(body) == "parallel_slave"
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await _plant(rig, {"r141": 0})
        status, body = await preview(rig, "deye1", "energy_pattern", 1)
        assert (status, err(body)) == (422, "invalid_value")


# -- R32-11 ----------------------------------------------------------------------------------


async def test_the_reconnection_time_and_the_ct_ratio_report_their_context_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {"r179": 1, "r344": 1})
        table = await rows(rig)
        assert table["grid_reconnection_time"]["companions"][0]["register"] == 179
        assert table["grid_reconnection_time"]["companions"][0]["word"] == 1
        assert table["external_ct_ratio"]["companions"][0]["register"] == 344
        for key, value, register in (
            ("grid_reconnection_time", 300, 179),
            ("external_ct_ratio", 3000, 344),
        ):
            status, body = await preview(rig, "deye1", key, value)
            assert status == 200, body  # context only: a forced off-grid / meter never blocks
            assert [c["register"] for c in body["preview"]["companions"]] == [register]
            assert body["preview"]["companions"][0]["word"] == 1
        assert rig.servers["gw1"].responder.writes == []  # nothing is toggled


def test_the_context_words_are_part_of_the_read_set_and_the_budget() -> None:
    assert 179 in WriteService._addresses(BY_KEY["grid_reconnection_time"])
    assert 344 in WriteService._addresses(BY_KEY["external_ct_ratio"])
    from deye_monitor.writes import IDENTITY_ADDRESSES

    for key, register in (("grid_reconnection_time", 179), ("external_ct_ratio", 344)):
        extra = WriteService._addresses(BY_KEY[key]) - set(IDENTITY_ADDRESSES)
        # block 1: the identity span (19 words), then one request per other word (context too)
        assert settings_read_requests([key], 1) == 19 + len(extra) and register in extra
