"""Review 36 (0.5.0): an empty permitted number domain, unknown R178 pairs, every activation path
checks the envelope of the power it arms, the rendered Web range."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deye_monitor.ha_discovery import Namespace, setting_component
from deye_monitor.write_ops import BY_KEY
from tests.fakes.mqtt import settle
from tests.plant_helpers import plant_rig
from tests.test_mqtt_writes_plant import MASTER, attach
from tests.test_review32_b2b import _lock, _plant
from tests.write_helpers import err, fast, preview, svc_of, write

NS = Namespace("ab12cd34", "homeassistant", "deye")
BOTH_OFF = 0x2AAA


# -- R36-01 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [0, 200, 399])
def test_an_empty_permitted_domain_is_a_valid_number_component(cap: int) -> None:
    desc = BY_KEY["pv_sale_max_power"]  # positive minimum 400
    _, comp = setting_component(NS, "deye1", desc, "t1", cap)
    assert comp["min"] <= comp["max"]
    assert (comp["min"], comp["max"]) == (desc.min_raw, desc.max_raw)


async def test_a_zero_export_allowance_with_r143_and_r340_released_keeps_the_session_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        regs = {"r336": MASTER, "r142": 0, "r143": 3000, "r340": 5000}
        await _plant(rig, regs, export_limit_w=0, export_policy="export_allowed")
        fast(rig)
        async with attach(rig) as bridge:
            comps = bridge.components()
            r340 = comps["set_pv_sale_max_power"]
            assert r340["min"] <= r340["max"]
            assert "set_export_max_power" in comps
            avail = bridge.conn.last(NS.write_availability_topic("deye1", "pv_sale_max_power"))
            assert avail.text == "offline"  # unavailable, the reason stays with the refusal


# -- R36-02 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("r178", [0x2AA2, 0x2AA6, 0x2A8A, 0x2A9A, 0x0000])
async def test_unknown_peak_pairs_refuse_the_export_mode_on_web_and_mqtt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r178: int
) -> None:
    regs = {"r336": MASTER, "r142": 1, "r178": r178, "r190": 0xFFFF, "r191": 0xFFFF, "r143": 3000}
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, regs, export_policy="export_allowed", export_limit_w=3000)
        assert await _lock(rig, "export_mode", "sell") == "peak_context_unknown"
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("export_mode", "Selling First")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["event_type"] == "write_rejected"
            assert bridge.events()[0]["reason"] == "peak_context_unknown"
        assert rig.servers["gw1"].responder.writes == []


# -- R36-03 ----------------------------------------------------------------------------------

EV = {"r336": MASTER, "r111": 1, "r259": 0x14DD, "r260": 7000}


async def test_ev_free_work_on_checks_the_charger_limit_and_the_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, EV, ev_charger="grid_port", ev_charger_limit_w=3000,
                     ev_offgrid_reserve_soc=80)  # fmt: skip
        assert await _lock(rig, "ev_free_work", 1) == "peak_power_exceeds_envelope"
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("ev_free_work", "ON")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["event_type"] == "write_rejected"
        assert rig.servers["gw1"].responder.writes == []
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:  # power fits, reserve above SOC
        await _plant(rig, {**EV, "r260": 2000}, ev_charger="grid_port", ev_charger_limit_w=3000,
                     ev_offgrid_reserve_soc=80)  # fmt: skip
        assert await _lock(rig, "ev_free_work", 1) == "ev_soc_below_reserve"
    async with plant_rig(tmp_path / "c", monkeypatch) as rig:  # reserve not commissioned
        await _plant(rig, {**EV, "r260": 2000}, ev_charger="grid_port", ev_charger_limit_w=3000,
                     ev_offgrid_reserve_soc=None)  # fmt: skip
        assert await _lock(rig, "ev_free_work", 1) == "ev_reserve_missing"
    async with plant_rig(tmp_path / "d", monkeypatch) as rig:  # all valid; OFF is always possible
        await _plant(rig, {**EV, "r260": 2000, "r259": 0x50DD}, ev_charger="grid_port",
                     ev_charger_limit_w=3000, ev_offgrid_reserve_soc=80)  # fmt: skip
        assert (await write(rig, "deye1", "ev_free_work", 1))["status"] == "readback_confirmed"
        assert (await write(rig, "deye1", "ev_free_work", 0))["status"] == "readback_confirmed"
    async with plant_rig(tmp_path / "e", monkeypatch) as rig:
        await _plant(rig, {**EV, "r259": 0x14DE}, ev_charger="grid_port",
                     ev_charger_limit_w=3000, ev_offgrid_reserve_soc=80)  # fmt: skip
        assert (await write(rig, "deye1", "ev_free_work", 0))["status"] == "readback_confirmed"


@pytest.mark.parametrize(
    ("r340", "reason"),
    [(7000, "peak_power_exceeds_envelope"), (0xFFFF, "companion_invalid")],
)
async def test_solar_sell_and_the_export_mode_check_the_sale_power_r340(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r340: int, reason: str
) -> None:
    regs = {"r336": MASTER, "r142": 1, "r143": 3000, "r145": 0, "r340": r340}
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, regs, export_policy="export_allowed", export_limit_w=3000)
        assert await _lock(rig, "pv_sale_enable", 1) == reason
        assert await _lock(rig, "export_mode", "sell") == reason
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("pv_sale_enable", "ON")
            bridge.send("export_mode", "Selling First")
            await settle(lambda: len(bridge.events()) == 2)
            assert [e["event_type"] for e in bridge.events()] == ["write_rejected"] * 2
        assert rig.servers["gw1"].responder.writes == []
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:  # within the envelope: accepted
        await _plant(rig, {**regs, "r340": 3000}, export_policy="export_allowed",
                     export_limit_w=3000)  # fmt: skip
        assert (await write(rig, "deye1", "pv_sale_enable", 1))["status"] == "readback_confirmed"


# -- R36-04 ----------------------------------------------------------------------------------


async def test_the_rows_carry_the_floor_and_a_zero_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {**EV, "r259": 0x32DD, "r142": 0}, export_limit_w=0,
                     ev_offgrid_reserve_soc=65)  # fmt: skip
        snap: Any = await svc_of(rig).snapshot("deye1")
        assert snap.settings["ev_offgrid_soc"].floor == 65
        assert snap.settings["export_max_power"].cap == 0
        status, body = await preview(rig, "deye1", "ev_offgrid_soc", 60)
        assert (status, err(body)) == (422, "ev_soc_below_reserve")
