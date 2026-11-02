"""Codex review 31 (part B2a): R31-01 R133 transition, R31-02 reverse bounds, R31-03 positive
gates, R31-04 companion dependencies, R31-05 GEN presets, R31-06 mixed role sets."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.profiles.features import Features
from deye_monitor.profiles.setting_presets import suggested_operations
from deye_monitor.write_ops import BOUND_CHILDREN, BY_KEY
from deye_monitor.writes import WriteService
from tests.plant_helpers import plant_rig
from tests.test_writes_v2 import prepare_direct, ready
from tests.write_helpers import build_plant, err, preview, svc_of, write

MASTER = {"r336": 0x0003}
SLAVE = {"r336": 0x0001}


async def _described(rig: object, **update: object) -> None:
    inv = svc_of(rig).runtime.inverters["deye1"]  # type: ignore[arg-type]
    inv.features = inv.features.model_copy(update={"bms": False, **update})


# -- R31-01 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "regs", "ok"),
    [
        ("smart_load", {"r135": 80, "r137": 20}, False),  # SOC pair inverted for Smart Load
        ("smart_load", {"r135": 20, "r137": 24}, False),  # ON only 4 above OFF (margin 5)
        ("smart_load", {"r135": 20, "r137": 25}, True),
        ("ac_coupled", {"r135": 20, "r137": 60}, False),  # AC coupling needs the reversed order
        ("ac_coupled", {"r135": 65, "r137": 60}, True),
    ],
)
async def test_r133_transition_validates_the_thresholds_of_the_new_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, regs: dict[str, int], ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 0, "r111": 1, **regs}})
        await _described(rig, gen_port_mode=target)
        status, body = await preview(rig, "deye1", "gen_port_usage", target)
        if ok:
            assert status == 200, body
            registers = {c["register"] for c in body["preview"]["companions"]}
            assert {111, 129, 132, 134, 135, 136, 137, 140, 178, 189} <= registers
        else:
            assert (status, err(body)) == (422, "mode_threshold_order")
        assert rig.servers["gw1"].responder.writes == []  # nothing is repaired or written


async def test_r133_voltage_basis_checks_the_voltage_pair_and_an_unknown_basis_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig,
            inverters=("deye1",),
            regs={"deye1": {"r133": 0, "r111": 0, "r134": 5200, "r136": 4500}},
        )
        await _described(rig, gen_port_mode="smart_load")
        status, body = await preview(rig, "deye1", "gen_port_usage", "smart_load")
        assert (status, err(body)) == (422, "mode_threshold_order")
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 0, "r111": 2}})
        await _described(rig, gen_port_mode="smart_load")
        status, body = await preview(rig, "deye1", "gen_port_usage", "smart_load")
        assert (status, err(body)) == (409, "mode_companions_invalid")
    async with plant_rig(tmp_path / "c", monkeypatch) as rig:  # a companion that is no legal word
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r133": 0, "r111": 1, "r137": 0xFFFF}}
        )
        await _described(rig, gen_port_mode="smart_load")
        status, body = await preview(rig, "deye1", "gen_port_usage", "smart_load")
        assert (status, err(body)) == (409, "mode_companions_invalid")


async def test_r133_is_validated_again_at_the_send_point_for_a_direct_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 0, "r111": 1}})
        await _described(rig, gen_port_mode="smart_load")
        svc = await ready(rig)
        permit, _, _ = await prepare_direct(svc, "gen_port_usage", "smart_load")
        rig.servers["gw1"].responder.regs[137] = 10  # ON now below OFF + margin
        op = await svc._run(permit)
        assert op["status"] == "rejected" and op["reason"] == "mode_threshold_order"
        assert rig.servers["gw1"].responder.writes == []


# -- R31-02 / R31-04 -----------------------------------------------------------------------------


async def test_a_parent_cannot_leave_a_child_outside_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r111": 0, "r108": 100, "r128": 40}}
        )
        await _described(rig)
        status, body = await preview(rig, "deye1", "battery_charge_current", 30)
        assert (status, err(body)) == (422, "bound_child")
        assert body["error"]["params"]["child"] == "grid_charge_current"
        assert (await preview(rig, "deye1", "battery_charge_current", 40))[0] == 200
        # R101 against the GRID charge start voltage R126 and the generator start R123 (R101 - 2 V)
        status, body = await preview(rig, "deye1", "battery_float_voltage", "48.5")
        assert (status, err(body)) == (422, "bound_child")
        assert (await preview(rig, "deye1", "battery_float_voltage", "50.0"))[0] == 200
        # the lower bound R120 against the ToU slot voltages
        status, body = await preview(rig, "deye1", "battery_low_voltage", "50.1")
        assert (status, err(body)) == (422, "bound_child")
        assert rig.servers["gw1"].responder.writes == []


def test_every_bound_has_a_reverse_check() -> None:
    for d in BY_KEY.values():
        for bound in d.upper + d.lower:
            assert any(c is d for c, _, _ in BOUND_CHILDREN[bound.key]), (d.key, bound.key)


async def test_r128_alone_validates_a_10kw_r108_with_its_own_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ten_kw = {"r20": 0x86A0, "r21": 1, "r14": 1, "r28": 5, "r108": 210, "r128": 40}
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": ten_kw}, operations=["grid_charge_current"]
        )
        status, body = await preview(rig, "deye1", "grid_charge_current", 100)
        assert status == 200, body
        assert 28 in WriteService._addresses(BY_KEY["grid_charge_current"])
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:  # R108 above the ceiling: refused
        await build_plant(
            rig,
            inverters=("deye1",),
            regs={"deye1": {**ten_kw, "r108": 221}},
            operations=["grid_charge_current"],
        )
        status, body = await preview(rig, "deye1", "grid_charge_current", 100)
        assert (status, err(body)) == (409, "companion_invalid")


# -- R31-03 --------------------------------------------------------------------------------------


@pytest.mark.parametrize("word", [1, 2, 0x00FF, 0xFFFF])
async def test_an_unsupported_battery_type_word_locks_the_curve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r98": word}})
        await _described(rig)
        status, body = await preview(rig, "deye1", "battery_float_voltage", "52.0")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "bms_curve_skipped"


@pytest.mark.parametrize("word", [0, 2001, 0x8000, 0xFFFF])
async def test_r114_needs_a_valid_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r102": word}})
        await _described(rig)
        status, body = await preview(rig, "deye1", "battery_charge_efficiency", "98.0")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "capacity_not_positive"
        status, _ = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert status == 200


# -- R31-05 --------------------------------------------------------------------------------------


def test_gen_terminal_controls_are_suggested_for_a_generator_on_the_gen_port_only() -> None:
    gen_controls = {
        "generator_charge_enable",
        "generator_charge_start_voltage",
        "generator_charge_soc",
    }
    on_grid_terminal = suggested_operations(
        Features(
            utility_grid=False,
            grid_port_use="generator",
            gen_port_mode="unused",
            battery_control_basis="voltage",
        )
    )
    assert not gen_controls & set(on_grid_terminal)
    assert {"grid_charge_enable", "grid_charge_start_voltage"} <= set(on_grid_terminal)
    soc = suggested_operations(
        Features(
            utility_grid=False,
            grid_port_use="generator",
            gen_port_mode="unused",
            battery_control_basis="soc",
        )
    )
    assert not gen_controls & set(soc)
    on_gen_port = suggested_operations(
        Features(gen_port_mode="generator", battery_control_basis="voltage")
    )
    assert {"generator_charge_enable", "generator_charge_start_voltage"} <= set(on_gen_port)
    none = suggested_operations(Features(gen_port_mode="unused", battery_control_basis="voltage"))
    assert not gen_controls & set(none)


# -- R31-06 --------------------------------------------------------------------------------------


async def test_role_loss_at_the_send_point_refuses_the_synchronized_one_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {**MASTER, "r35": 0}})
        svc = await ready(rig)
        synced, _, _ = await prepare_direct(svc, "grid_charge_current", 50)
        per_unit, _, _ = await prepare_direct(svc, "low_noise", 1)
        rig.servers["gw1"].responder.regs[336] = SLAVE["r336"]  # the master role is lost
        op = await svc._run(synced)
        assert (op["status"], op["reason"]) == ("rejected", "parallel_slave")
        op = await svc._run(per_unit)
        assert op["status"] == "readback_confirmed"  # a per-unit register needs no master role


async def test_a_mixed_snapshot_exposes_only_the_per_unit_descriptors_on_a_slave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": SLAVE})
        snap = await svc_of(rig).snapshot("deye1")
        assert snap.status == "ok"
        assert set(snap.settings) == {k for k, d in BY_KEY.items() if not d.synchronized}
        await write(rig, "deye1", "low_noise", 1)
