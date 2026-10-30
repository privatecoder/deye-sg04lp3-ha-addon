"""Energy resolution (R65 / override) and the statistics generation (SPEC §5, DR2-02)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deye_monitor.config import EnergyResolution
from deye_monitor.energy_state import EnergyTracker
from deye_monitor.store import JsonStore
from tests.device_helpers import Recorder, device_regs, ep, inverter, rig
from tests.fakes.servers import value_of

SN = "2401234567"
RAW = value_of(514)  # battery_energy_charge_today is read as this raw word


def energy_values(sink: Recorder) -> list[Any]:
    return sink.values("deye1", "battery_energy_charge_today")


def persisted(tmp_path: Path) -> dict[str, Any]:
    record = dict(json.loads((tmp_path / "energy.json").read_text())["data"]["deye1"])
    assert record.pop("serial") == SN  # the energy epoch carries the serial number
    return record


async def test_r65_boundary_selects_the_resolution(tmp_path: Path) -> None:
    for r65, factor, sub in (
        (0x0104, 0.1, "a"),
        (0x0105, 1.0, "b"),
    ):
        async with rig(
            [ep("gw1")],
            [inverter("deye1", "gw1")],
            {"gw1": device_regs(SN, r65=r65)},
            tmp_path / sub,
        ) as r:
            inv = r.inv("deye1")
            await inv.step()
            assert inv.current_plan().reads_r65
            assert energy_values(r.sink)[-1] == round(RAW * factor, 6)
            assert inv.energy.resolution == factor
            assert persisted(tmp_path / sub) == {
                "effective_resolution": factor,
                "source": "r65",
                "generation": 0,
            }


async def test_an_unusable_r65_derives_nothing_and_asks_for_a_choice(tmp_path: Path) -> None:
    for r65, sub in ((0xFFFF, "a"), (0x0000, "b")):
        async with rig(
            [ep("gw1")],
            [inverter("deye1", "gw1")],
            {"gw1": device_regs(SN, r65=r65)},
            tmp_path / sub,
        ) as r:
            inv = r.inv("deye1")
            await inv.step()
            assert inv.state.value == "running"
            assert energy_values(r.sink) and set(energy_values(r.sink)) == {None}
            assert inv.energy.resolution is None and inv.energy.needs_choice
            snap = inv.diagnostics()["energy"]
            assert snap["source"] == "unknown" and snap["unknown_reason"] == "r65_invalid"
            assert any(d["event"] == "energy_resolution_unknown" for _, d in r.sink.diagnostics)
            assert not (tmp_path / sub / "energy.json").exists()  # no default factor persisted
            assert r.sink.values("deye1", "battery_soc")[-1] is not None


async def test_a_factor_confirmed_from_r65_ffff_is_unknown_until_the_user_chooses(
    tmp_path: Path,
) -> None:
    # an installation that took 1 kWh from R65 = 0xFFFF (series generation 0, source r65)
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN, r65=0x0105)}, tmp_path
    ) as r:
        await r.inv("deye1").step()
        assert energy_values(r.sink)[-1] == RAW
    bad = device_regs(SN, r65=0xFFFF)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": bad}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.energy.resolution is None and inv.energy.needs_choice
        assert set(energy_values(r.sink)) == {None}
    assert persisted(tmp_path)["generation"] == 0  # nothing new before the choice
    # choosing 0.1 kWh starts a NEW series, the old one is never continued with another factor
    chosen = inverter("deye1", "gw1", energy_resolution="0.1")
    async with rig([ep("gw1")], [chosen], {"gw1": bad}, tmp_path) as r:
        await r.inv("deye1").step()
        assert energy_values(r.sink)[-1] == round(RAW * 0.1, 6)
    assert persisted(tmp_path) == {
        "effective_resolution": 0.1,
        "source": "override",
        "generation": 1,
    }


async def test_energy_is_unknown_without_a_valid_r65_and_never_defaults(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        r.servers["gw1"].responder.fail_addresses = {65}  # R65 cannot be read
        inv = r.inv("deye1")
        await inv.step()
        assert inv.state.value == "running"  # identity works without R65
        assert energy_values(r.sink) and set(energy_values(r.sink)) == {None}
        assert inv.energy.resolution is None
        assert not (tmp_path / "energy.json").exists()  # nothing persisted, no default factor
        assert r.sink.values("deye1", "battery_soc")[-1] is not None  # other values unaffected
        r.servers["gw1"].responder.fail_addresses = set()
        r.clock.offset += 4000  # identity refresh reads R65 again
        await inv.step()
        r.clock.offset += 61
        await inv.step()
        assert energy_values(r.sink)[-1] == round(RAW * 0.1, 6)


async def test_override_needs_no_r65(tmp_path: Path) -> None:
    inv_cfg = inverter("deye1", "gw1", energy_resolution="1")
    async with rig([ep("gw1")], [inv_cfg], {"gw1": device_regs(SN, r65=0x0100)}, tmp_path) as r:
        inv = r.inv("deye1")
        assert inv.cfg.energy_resolution is EnergyResolution.ONE
        await inv.step()
        # no extra R65 read for the resolution (R65 itself is only read because the diagnostic
        # entity "dsp_protocol_version" is selected by default)
        assert not inv.current_plan().reads_r65
        assert energy_values(r.sink)[-1] == RAW  # 1 kWh per count although R65 would say 0.1
        assert persisted(tmp_path) == {
            "effective_resolution": 1.0,
            "source": "override",
            "generation": 0,
        }


async def test_override_change_bumps_the_generation_once(tmp_path: Path) -> None:
    regs = device_regs(SN, r65=0x0104)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        await r.inv("deye1").step()
    assert persisted(tmp_path)["generation"] == 0
    for _ in range(2):  # restarts with an unchanged factor do not change anything
        async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
            await r.inv("deye1").step()
        assert persisted(tmp_path) == {
            "effective_resolution": 0.1,
            "source": "r65",
            "generation": 0,
        }
    # the user fixes the resolution at 1 kWh: a new effective factor, a new series
    changed = inverter("deye1", "gw1", energy_resolution="1")
    for _ in range(2):  # idempotent across restarts in the middle of / after the change
        async with rig([ep("gw1")], [changed], {"gw1": regs}, tmp_path) as r:
            await r.inv("deye1").step()
            assert energy_values(r.sink)[-1] == RAW
        assert persisted(tmp_path) == {
            "effective_resolution": 1.0,
            "source": "override",
            "generation": 1,
        }
    # an override that equals the factor R65 would give is no new series, only a new source
    same = inverter("deye1", "gw1", energy_resolution="0.1")
    async with rig([ep("gw1")], [same], {"gw1": device_regs(SN, r65=0x0104)}, tmp_path) as r:
        await r.inv("deye1").step()
    assert persisted(tmp_path)["generation"] == 2  # 1.0 -> 0.1 is a change of the factor
    auto = inverter("deye1", "gw1")
    async with rig([ep("gw1")], [auto], {"gw1": regs}, tmp_path) as r:
        await r.inv("deye1").step()
    assert persisted(tmp_path) == {"effective_resolution": 0.1, "source": "r65", "generation": 2}


async def test_r65_change_needs_two_readings_and_bumps_the_generation(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN, r65=0x0104)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert persisted(tmp_path)["generation"] == 0
        r.servers["gw1"].responder.regs[65] = 0x0105  # firmware update changes R65
        r.clock.offset += 4000
        await inv.step()
        assert inv.energy.resolution is None  # first reading of the new value: not confirmed
        assert r.sink.events("energy_change_pending")
        r.clock.offset += 11
        await inv.step()  # data cycle during the pending change: energy stays unknown
        assert set(energy_values(r.sink)[-1:]) <= {None}
        assert persisted(tmp_path)["generation"] == 0  # nothing committed yet
        # a restart in the middle of the change continues idempotently from the old state
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN, r65=0x0105)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.energy.resolution is None  # pending again after the restart
        r.clock.offset += 4000
        await inv.step()  # second identical reading confirms
        assert inv.energy.resolution == 1.0
        assert persisted(tmp_path) == {
            "effective_resolution": 1.0,
            "source": "r65",
            "generation": 1,
        }
        events = r.sink.events("energy_generation_changed")
        assert len(events) == 1 and events[0]["generation"] == 1 and events[0]["resolution"] == 1.0
        r.clock.offset += 4000
        await inv.step()
        assert persisted(tmp_path)["generation"] == 1  # further identical readings change nothing


async def test_a_flapping_r65_never_switches(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN, r65=0x0104)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        for value in (0x0105, 0x0104, 0x0105, 0x0104):
            r.servers["gw1"].responder.regs[65] = value
            r.clock.offset += 4000
            await inv.step()
        assert persisted(tmp_path)["generation"] == 0


def test_tracker_invalidate_and_unreadable_r65(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "energy.json")
    tracker = EnergyTracker(store, "a", EnergyResolution.AUTO)
    assert tracker.observe(None) == [] and tracker.resolution is None
    assert tracker.observe(0x0104)[0].kind == "initialised" and tracker.resolution == 0.1
    tracker.invalidate()  # new path / identity: confirm again for this device
    assert tracker.resolution is None
    assert tracker.observe(0x0104) == [] and tracker.resolution == 0.1
    assert tracker.generation == 0
