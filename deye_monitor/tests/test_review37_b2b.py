"""Review 37 (0.5.0): EV activation validates every activated companion, also when the port is
redirected while free work is ON."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes.mqtt import settle
from tests.plant_helpers import plant_rig
from tests.test_mqtt_writes_plant import MASTER, attach
from tests.test_review32_b2b import _late, _lock, _plant
from tests.write_helpers import fast, write

EV = {"r336": MASTER, "r111": 1, "r259": 0x50DD, "r260": 2000}
DESCRIBED = {"ev_charger": "grid_port", "ev_charger_limit_w": 3000, "ev_offgrid_reserve_soc": 50}


@pytest.mark.parametrize("r117", [0xFFFF, 101])
async def test_free_work_on_refuses_an_invalid_battery_low_soc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r117: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {**EV, "r117": r117}, **DESCRIBED)
        assert await _lock(rig, "ev_free_work", 1) == "companion_invalid"
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("ev_free_work", "ON")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["event_type"] == "write_rejected"
        assert rig.servers["gw1"].responder.writes == []


@pytest.mark.parametrize("r117", [0xFFFF, 101])
async def test_a_late_invalid_battery_low_soc_refuses_at_the_send_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, r117: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, {**EV, "r117": 20}, **DESCRIBED)
        await _late(rig, "ev_free_work", 1, {117: r117})


async def test_redirecting_the_port_with_free_work_on_checks_the_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    over = {**EV, "r259": 0x50EE, "r260": 7000}  # free work ON, LOAD port, power over the cap
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await _plant(rig, over, **DESCRIBED)
        assert await _lock(rig, "ev_location", "grid") == "peak_power_exceeds_envelope"
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("ev_location", "GRID port")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["event_type"] == "write_rejected"
        assert rig.servers["gw1"].responder.writes == []
    async with plant_rig(tmp_path / "b", monkeypatch) as rig:  # SOC below the reserve
        await _plant(rig, {**over, "r260": 2000}, **{**DESCRIBED, "ev_offgrid_reserve_soc": 90})
        assert await _lock(rig, "ev_location", "grid") == "ev_soc_below_reserve"
    async with plant_rig(tmp_path / "c", monkeypatch) as rig:  # free work OFF: nothing is armed
        await _plant(rig, {**over, "r259": 0x50ED}, **DESCRIBED)
        assert (await write(rig, "deye1", "ev_location", "grid"))["status"] == "readback_confirmed"
    async with plant_rig(tmp_path / "d", monkeypatch) as rig:  # everything valid
        await _plant(rig, {**over, "r260": 2000}, **DESCRIBED)
        assert (await write(rig, "deye1", "ev_location", "grid"))["status"] == "readback_confirmed"


# -- R37-03 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected", "de"),
    [("mppt_count", 2, "2 MPPT"), ("phase_count", 3, "3 Phasen"), ("battery_input_code", 1, None)],
)
@pytest.mark.parametrize("raw", [0xFFFF, None])
def test_the_profile_fallback_carries_the_german_state_of_the_presented_label(
    key: str, expected: int, de: str | None, raw: int | None
) -> None:
    from deye_monitor.ha_discovery import _profile_presented
    from deye_monitor.profiles.schema import load_profile
    from deye_monitor.profiles.states import present

    entry = load_profile("sg04lp3_lv_v105_8").entry(key)
    reported = present(entry, {a: raw for a in entry.addresses}) if raw is not None else None
    got = _profile_presented(entry, reported, expected)
    assert got.attributes["source"] == "profile"
    assert got.attributes["state_de"] == got.label.de != "Nicht gemeldet"
    if de is not None:
        assert got.attributes["state_de"] == de
    assert ("raw_hex" in got.attributes) == (raw is not None)  # nothing is made up
