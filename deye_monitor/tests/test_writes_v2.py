"""Write layer v2, part A: the R111/R133/R146 gates, protection triplet, ToU slot order, rated
power, whole-word R130, foreign-bit preservation on the wire and the transport-independent
``submit`` path with its execution-time guards. Fake devices only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deye_monitor.write_ops import BY_KEY, Refused
from tests.plant_helpers import PlantRig, plant_rig
from tests.write_helpers import (
    audit_lines,
    build_plant,
    confirm,
    err,
    fc16,
    preview,
    svc_of,
    write,
)

SOC_ONLY = [
    "battery_shutdown_soc",
    "battery_low_soc",
    "battery_restart_soc",
    "grid_charge_start_soc",
    "smart_load_off_soc",
    "smart_load_on_soc",
    *[f"tou_slot{n}_soc" for n in range(1, 7)],
]
VOLTAGE_ONLY = ["smart_load_off_voltage", "smart_load_on_voltage"]
TOU = [d.key for d in BY_KEY.values() if d.tou]


async def rows(rig: PlantRig, inverter: str = "deye1") -> dict[str, dict[str, Any]]:
    status, body = await rig.call("POST", f"/api/inverters/{inverter}/writes/current", {})
    assert status == 200, body
    return {r["key"]: r for r in body["operations"]}


# -- R111 / R133 gates ------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0, 2, 3, 0xFFFF])
async def test_soc_settings_are_offered_only_in_soc_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r111": mode}})
        table = await rows(rig)
        for key in SOC_ONLY:
            assert (table[key]["state"], table[key]["reason"]) == ("locked", "soc_mode_required")
            status, body = await preview(rig, "deye1", key, 50)
            assert (status, err(body)) == (409, "operation_locked"), key
            assert body["error"]["params"]["reason"] == "soc_mode_required"
        if mode == 0:
            assert all(table[k]["state"] == "ready" for k in VOLTAGE_ONLY)
        assert rig.servers["gw1"].responder.writes == []


async def test_voltage_settings_are_offered_only_in_voltage_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))  # R111 = 1 (SOC)
        table = await rows(rig)
        for key in VOLTAGE_ONLY:
            assert (table[key]["state"], table[key]["reason"]) == (
                "locked",
                "voltage_mode_required",
            )
            status, body = await preview(rig, "deye1", key, 50)
            assert (status, err(body)) == (409, "operation_locked")
        for key in SOC_ONLY:
            assert table[key]["state"] == "ready", key
        assert rig.servers["gw1"].responder.writes == []


async def test_smart_load_still_needs_the_gen_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 0}})
        table = await rows(rig)
        for key in ("smart_load_off_soc", "smart_load_on_soc"):
            assert table[key]["reason"] == "gen_not_smart_load"
        assert table["battery_shutdown_soc"]["state"] == "ready"  # not a Smart Load setting


# -- protection triplet -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "ok"),
    [
        ("battery_shutdown_soc", 34, True),  # < low 35
        ("battery_shutdown_soc", 35, False),  # == low
        ("battery_shutdown_soc", 60, False),
        ("battery_shutdown_soc", 0, True),
        ("battery_low_soc", 26, True),  # between shutdown 25 and restart 50
        ("battery_low_soc", 49, True),
        ("battery_low_soc", 25, False),
        ("battery_low_soc", 50, False),
        ("battery_low_soc", 10, False),
        ("battery_restart_soc", 36, True),  # > low 35
        ("battery_restart_soc", 100, True),
        ("battery_restart_soc", 35, False),
        ("battery_restart_soc", 20, False),
    ],
)
async def test_protection_thresholds_keep_shutdown_below_low_below_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: int, ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", key, value)
        if ok:
            assert status == 200, body
        else:
            assert (status, err(body)) == (422, "threshold_order")
            assert body["error"]["params"]["ordering"] in ("below", "above")
        assert rig.servers["gw1"].responder.writes == []


@pytest.mark.parametrize("key", ["battery_shutdown_soc", "battery_low_soc", "battery_restart_soc"])
@pytest.mark.parametrize("value", [-1, 101, 1.5, "abc", True, None])
async def test_protection_thresholds_reject_bad_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: Any
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", key, value)
        assert status == 422, (key, value, body)
        assert err(body) in ("value_out_of_range", "value_not_exact", "invalid_value")
        assert rig.servers["gw1"].responder.writes == []


async def test_a_companion_changed_after_the_preview_is_a_conflict_and_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", "battery_shutdown_soc", 30)
        assert status == 200
        rig.servers["gw1"].responder.regs[117] = 20  # low SOC moved below the new shutdown
        status, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "conflict"
        assert rig.servers["gw1"].responder.writes == []


async def test_grid_charge_start_soc_range_is_10_to_90(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        for value in (9, 91, 0, 100):
            status, body = await preview(rig, "deye1", "grid_charge_start_soc", value)
            assert (status, err(body)) == (422, "value_out_of_range"), value
        for value in (10, 90):
            status, body = await preview(rig, "deye1", "grid_charge_start_soc", value)
            assert status == 200, (value, body)
        assert rig.servers["gw1"].responder.writes == []


# -- ToU: Spain mode, enable switch, weekday info ----------------------------------------------


@pytest.mark.parametrize("word", [0x01FE, 0x0100, 0x0101, 0xFFFF])
async def test_spain_mode_closes_every_tou_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r146": word}})
        table = await rows(rig)
        for key in TOU:
            assert (table[key]["state"], table[key]["reason"]) == ("locked", "spain_mode"), key
        assert table["battery_charge_current"]["state"] == "ready"
        for key, value in (("tou_enable", 1), ("tou_slot1_start", "00:05"), ("tou_slot2_power", 5)):
            status, body = await preview(rig, "deye1", key, value)
            assert (status, err(body)) == (409, "operation_locked")
            assert body["error"]["params"]["reason"] == "spain_mode"
        assert rig.servers["gw1"].responder.writes == []


async def test_tou_enable_sets_bit_zero_and_keeps_weekdays_and_every_other_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r146": 0x00A4}})
        server = rig.servers["gw1"]
        op = await write(rig, "deye1", "tou_enable", 1)
        assert op["status"] == "readback_confirmed"
        assert server.responder.regs[146] == 0x00A5
        assert fc16(server)[0][-2:] == bytes.fromhex("00a5")
        op = await write(rig, "deye1", "tou_enable", 0)
        assert server.responder.regs[146] == 0x00A4
        assert len(fc16(server)) == 2


async def test_slot_grid_charge_keeps_the_generator_bit_and_other_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r174": 0xA50E}})
        server = rig.servers["gw1"]
        op = await write(rig, "deye1", "tou_slot3_grid_charge", 1)
        assert op["status"] == "readback_confirmed"
        assert server.responder.regs[174] == 0xA50F
        assert (op["old_word"], op["new_word"]) == (0xA50E, 0xA50F)
        assert [r.start for r in server.responder.writes] == [174]
        await write(rig, "deye1", "tou_slot3_grid_charge", 0)
        assert server.responder.regs[174] == 0xA50E


async def test_current_rows_carry_the_weekday_mask_and_the_inverter_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        # Monday, Wednesday, Sunday (bits 1, 3, 7) and ToU on
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r146": 0x008B}})
        _status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        info = body["info"]["tou"]
        assert info["enabled"] is True and info["spain_mode"] is False
        assert info["weekday_mask"] == 0b1000101 and info["weekday_filter"] is True
        assert info["weekdays"] == [True, False, True, False, False, False, True]
        assert info["clock"] == "2026-11-01 12:30:15"
        assert info["clock_words"] == [(26 << 8) | 11, (1 << 8) | 12, (30 << 8) | 15]


async def test_weekday_bits_all_zero_means_no_filter_and_a_bad_clock_is_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r146": 0x0001, "r63": 0xFFFF}}
        )
        _status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        info = body["info"]["tou"]
        assert info["weekday_mask"] == 0 and info["weekday_filter"] is False
        assert info["clock"] is None


async def test_no_tou_info_without_a_tou_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), operations=["battery_charge_current"])
        _status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert body["info"] == {}
        reads = [f for f in rig.servers["gw1"].frames if len(f) == 12]
        assert all(f[8:10] != bytes([0, 62]) for f in reads)  # the clock is not even read


# -- ToU slot start order ----------------------------------------------------------------------

SLOTS = ["00:00", "05:00", "10:00", "15:00", "20:00", "23:00"]


def shift(hhmm: str, minutes: int) -> str:
    total = int(hhmm[:2]) * 60 + int(hhmm[3:]) + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


@pytest.mark.parametrize("n", range(1, 7))
async def test_slot_start_must_stay_between_its_neighbours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        key = f"tou_slot{n}_start"
        prev = SLOTS[n - 2] if n > 1 else None
        nxt = SLOTS[n] if n < 6 else None
        bad = [v for v in (prev, nxt) if v]  # equal to a neighbour is not strictly ascending
        bad += [shift(prev, -5)] if prev and prev != "00:00" else []
        bad += [shift(nxt, 5)] if nxt and nxt != "23:55" else []
        for value in bad:
            status, body = await preview(rig, "deye1", key, value)
            assert (status, err(body)) == (422, "tou_order"), (key, value, body)
        good = [shift(prev, 5)] if prev else ["00:05"]
        good += [shift(nxt, -5)] if nxt else ["23:55"]
        for value in good:
            status, body = await preview(rig, "deye1", key, value)
            assert status == 200, (key, value, body)
        assert rig.servers["gw1"].responder.writes == []


async def test_a_crossing_edit_is_refused_and_no_other_slot_is_rearranged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", "tou_slot2_start", "11:00")
        assert (status, err(body)) == (422, "tou_order")
        assert body["error"]["params"]["companion"] == "tou_slot3_start"
        assert body["error"]["params"]["companion_value"] == "10:00"
        op = await write(rig, "deye1", "tou_slot2_start", "09:55")
        assert op["status"] == "readback_confirmed"
        regs = rig.servers["gw1"].responder.regs
        assert [regs[a] for a in range(148, 154)] == [0, 955, 1000, 1500, 2000, 2300]
        assert [r.start for r in rig.servers["gw1"].responder.writes] == [149]


async def test_slot_start_edit_reads_all_six_times_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", "tou_slot4_start", "14:00")
        assert status == 200
        rig.servers["gw1"].responder.regs[152] = 1400  # slot 5 moved up to the new slot 4 value
        status, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "conflict"
        status, body = await preview(rig, "deye1", "tou_slot4_start", "14:00")
        assert (status, err(body)) == (422, "tou_order")
        assert rig.servers["gw1"].responder.writes == []


@pytest.mark.parametrize("value", ["12:31", "24:00", "12:60", "1230", 1230, "12:30:30", None])
async def test_slot_start_rejects_bad_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", "tou_slot3_start", value)
        assert status == 422, (value, body)
        assert rig.servers["gw1"].responder.writes == []


async def test_an_unreadable_stored_slot_time_locks_that_slot_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r150": 9999}})
        table = await rows(rig)
        assert table["tou_slot3_start"]["reason"] == "existing_value_outside_range"
        assert table["tou_slot2_start"]["state"] == "ready"


# -- ToU power / SOC / battery -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("r20", "r21", "limit"),
    [
        (0xC350, 0, 5000),
        (0xEA60, 0, 6000),
        (0x3880, 1, 8000),
        (0x86A0, 1, 10000),
        (0xD4C0, 1, 12000),
    ],
)
async def test_slot_power_is_bounded_by_the_rated_power(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r20: int, r21: int, limit: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r20": r20, "r21": r21}})
        status, body = await preview(rig, "deye1", "tou_slot1_power", limit + 1)
        assert (status, err(body)) == (422, "value_out_of_range")
        assert body["error"]["params"]["high"] == limit
        status, body = await preview(rig, "deye1", "tou_slot1_power", limit)
        assert status == 200, body
        assert body["preview"]["cap"] == limit
        assert rig.servers["gw1"].responder.writes == []


async def test_unknown_model_locks_the_slot_power(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r20": 70000 & 0xFFFF, "r21": 0}}
        )
        table = await rows(rig)
        for n in range(1, 7):
            assert table[f"tou_slot{n}_power"]["reason"] == "model_unknown"
        assert table["tou_slot1_soc"]["state"] == "ready"
        status, body = await preview(rig, "deye1", "tou_slot1_power", 100)
        assert (status, err(body)) == (409, "operation_locked")


@pytest.mark.parametrize("value", [-1, 12001, 1.5, "x", True])
async def test_slot_power_rejects_bad_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", "tou_slot1_power", value)
        assert status == 422, (value, body)


async def test_new_settings_need_a_battery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        inv = svc_of(rig).runtime.inverters["deye1"]
        inv.features = inv.features.model_copy(update={"battery": False})
        for key, value in (("tou_enable", 1), ("battery_low_soc", 40), ("tou_slot1_power", 5)):
            status, body = await preview(rig, "deye1", key, value)
            assert (status, err(body)) == (409, "no_battery"), key
        assert rig.servers["gw1"].responder.writes == []


# -- transport-independent submit with execution-time guards -----------------------------------


async def ready(rig: PlantRig) -> Any:
    await rows(rig)  # the web call wires the plant revision into the service
    return svc_of(rig)


async def prepare_direct(svc: Any, key: str, value: Any) -> Any:
    """A request accepted by ``submit`` up to the send point (what a queued command would be)."""
    return await svc._prepare(
        "deye1",
        key,
        value,
        source="mqtt",
        user_id="",
        plant_revision=svc.plant_revision(),
        direct=True,
    )


async def test_submit_writes_without_a_preview_and_records_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1", "deye2"))
        svc = await ready(rig)
        op = await svc.submit("deye1", "battery_low_soc", 40, source="mqtt")
        assert op["status"] == "readback_confirmed" and op["source"] == "mqtt"
        assert (op["old_word"], op["new_word"]) == (35, 40)
        assert (op["old_word_hex"], op["new_word_hex"]) == ("0x0023", "0x0028")
        assert [(p["id"], p["state"]) for p in op["peers"]] == [("deye2", "different")]
        server = rig.servers["gw1"]
        assert len(fc16(server)) == 1 and rig.servers["gw2"].responder.writes == []
        lines = audit_lines(rig)
        assert [x["event"] for x in lines][:2] == ["intent", "result"]
        assert lines[0]["record"]["source"] == "mqtt" and lines[0]["record"]["new_word"] == 40
        assert lines[0]["record"]["status"] == "sent"


async def test_submit_applies_the_same_value_and_order_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        svc = await ready(rig)
        for key, value, refusal in (
            ("battery_low_soc", 25, "threshold_order"),
            ("tou_slot2_start", "11:00", "tou_order"),
            ("tou_slot1_power", 9000, "value_out_of_range"),
            ("tou_slot1_power", 8001, "value_out_of_range"),
            ("grid_charge_start_soc", 5, "value_out_of_range"),
            ("smart_load_on_voltage", 50, "operation_locked"),
        ):
            with pytest.raises(Refused) as info:
                await svc.submit("deye1", key, value, source="mqtt")
            assert info.value.key == refusal, (key, value, info.value.key)
        assert rig.servers["gw1"].responder.writes == []
        assert audit_lines(rig) == []  # a refused request never reached the send point


async def test_submit_of_the_current_value_is_an_audited_noop_without_a_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        svc = await ready(rig)
        op = await svc.submit("deye1", "battery_low_soc", 35, source="mqtt")  # R117 holds 35
        assert op["status"] == "noop" and op["reason"] == "no_change" and op["source"] == "mqtt"
        assert op["old_word"] == op["new_word"] == 35
        assert rig.servers["gw1"].responder.writes == []
        lines = audit_lines(rig)
        assert [x["event"] for x in lines] == ["result"]  # no intent: nothing was sent
        assert lines[0]["record"]["status"] == "noop"
        # the Web path still refuses it before a permit exists
        with pytest.raises(Refused) as info:
            await svc.preview("deye1", "battery_low_soc", 35, user_id="u", plant_revision=0)
        assert info.value.key == "no_change"


async def test_submit_needs_the_release_and_a_plant_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1", "deye2"), enable=("deye1",))
        svc = await ready(rig)
        with pytest.raises(Refused) as info:
            await svc.submit("deye2", "battery_low_soc", 40, source="mqtt")
        assert info.value.key == "writes_not_enabled"
        with pytest.raises(Refused) as info:
            await svc.submit("deye1", "no_such_setting", 40, source="mqtt")
        assert info.value.key == "unknown_operation"
        svc.plant_revision = lambda: None
        with pytest.raises(Refused) as info:
            await svc.submit("deye1", "battery_low_soc", 40, source="mqtt")
        assert info.value.key == "writes_stopped"
        assert all(s.responder.writes == [] for s in rig.servers.values())


@pytest.mark.parametrize(
    ("key", "value", "change", "reason"),
    [
        ("battery_low_soc", 40, {111: 0}, "soc_mode_required"),
        ("smart_load_off_soc", 15, {133: 0}, "gen_not_smart_load"),
        ("tou_slot1_power", 100, {146: 0x01FE}, "spain_mode"),
        ("tou_enable", 1, {146: 0x01FE}, "spain_mode"),
        ("tou_slot3_start", "09:00", {149: 930}, "tou_order"),  # slot 2 moved after the new value
        ("battery_low_soc", 40, {116: 38}, "threshold_order"),  # restart moved below the new low
        ("battery_low_soc", 40, {117: 40}, "no_change"),  # somebody else already wrote it
        ("grid_charge_enable", 1, {130: 4}, "unexpected_bits"),
        ("tou_slot1_power", 100, {20: 0xE0, 21: 0}, "model_unknown"),
        ("battery_low_soc", 40, {336: 0x0001}, "parallel_slave"),
    ],
)
async def test_submit_decides_every_guard_again_from_the_reads_of_the_send_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: Any,
    change: dict[int, int],
    reason: str,
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        svc = await ready(rig)
        permit, _, _ = await prepare_direct(svc, key, value)  # the request is accepted ...
        rig.servers["gw1"].responder.regs.update(change)  # ... the device changes before the send
        op = await svc._run(permit)
        assert (op["status"], op["reason"]) == ("rejected", reason), key
        assert rig.servers["gw1"].responder.writes == []
        assert not [x for x in audit_lines(rig) if x["event"] == "intent"]  # intent only at send


async def test_submit_voltage_gate_is_decided_again_at_the_send_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r111": 0}})
        svc = await ready(rig)
        permit, _, _ = await prepare_direct(svc, "smart_load_off_voltage", 40)
        rig.servers["gw1"].responder.regs[111] = 1
        op = await svc._run(permit)
        assert (op["status"], op["reason"]) == ("rejected", "voltage_mode_required")
        assert rig.servers["gw1"].responder.writes == []


async def test_submit_fails_on_a_changed_firmware_or_serial_at_the_send_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        svc = await ready(rig)
        for change, reason in (
            ({18: 0x0071}, "firmware_changed"),
            ({5: 0x3333}, "identity_mismatch"),
        ):
            regs = rig.servers["gw1"].responder.regs
            saved = {a: regs[a] for a in change}
            permit, _, _ = await prepare_direct(svc, "battery_low_soc", 40)
            regs.update(change)
            op = await svc._run(permit)
            regs.update(saved)
            assert (op["status"], op["reason"]) == ("rejected", reason)
        assert rig.servers["gw1"].responder.writes == []


async def test_submit_is_paced_and_serialised_like_every_other_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        svc = await ready(rig)
        waits: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            waits.append(seconds)

        svc.sleep = fake_sleep
        svc.settings = type(svc.settings)(observe_delay_s=0.0, pacing_s=5.0, permit_s=30.0)
        first = await svc.submit("deye1", "battery_low_soc", 40, source="mqtt")
        second = await svc.submit("deye1", "battery_low_soc", 45, source="mqtt")
        assert first["status"] == second["status"] == "readback_confirmed"
        assert any(w > 4 for w in waits)  # the second write waited for the 5 s pacing
        assert len(fc16(rig.servers["gw1"])) == 2
