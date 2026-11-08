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


async def test_auto_is_the_profile_default_whatever_r65_says(tmp_path: Path) -> None:
    # 0.5.1: every checked MCU2 build counts 0.1 kWh and none has an R65 selector
    for n, r65 in enumerate((0x0104, 0x0105, 0xFFFF, 0x0000)):
        async with rig(
            [ep("gw1")],
            [inverter("deye1", "gw1")],
            {"gw1": device_regs(SN, r65=r65)},
            tmp_path / str(n),
        ) as r:
            inv = r.inv("deye1")
            await inv.step()
            assert energy_values(r.sink)[-1] == round(RAW * 0.1, 6)
            assert inv.energy.resolution == 0.1
            snap = inv.diagnostics()["energy"]
            assert snap["source"] == "profile" and snap["effective_resolution"] == 0.1
            assert 65 not in inv.current_plan().addresses  # R65 is not read at all
            assert persisted(tmp_path / str(n)) == {
                "effective_resolution": 0.1,
                "source": "profile",
                "generation": 0,
            }


async def test_a_series_counted_at_one_kwh_moves_to_a_new_series_at_the_profile_default(
    tmp_path: Path,
) -> None:
    chosen = inverter("deye1", "gw1", energy_resolution="1")
    async with rig([ep("gw1")], [chosen], {"gw1": device_regs(SN, r65=0xFFFF)}, tmp_path) as r:
        await r.inv("deye1").step()
        assert energy_values(r.sink)[-1] == RAW
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN, r65=0x0105)}, tmp_path
    ) as r:
        await r.inv("deye1").step()
        assert energy_values(r.sink)[-1] == round(RAW * 0.1, 6)  # never continued at another factor
    assert persisted(tmp_path) == {
        "effective_resolution": 0.1,
        "source": "profile",
        "generation": 1,
    }


async def test_energy_does_not_wait_for_r65(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        r.servers["gw1"].responder.fail_addresses = {65}  # R65 cannot be read
        inv = r.inv("deye1")
        await inv.step()
        assert inv.state.value == "running"
        assert energy_values(r.sink)[-1] == round(RAW * 0.1, 6)
        assert inv.energy.resolution == 0.1


async def test_override_needs_no_r65(tmp_path: Path) -> None:
    inv_cfg = inverter("deye1", "gw1", energy_resolution="1")
    async with rig([ep("gw1")], [inv_cfg], {"gw1": device_regs(SN, r65=0x0100)}, tmp_path) as r:
        inv = r.inv("deye1")
        assert inv.cfg.energy_resolution is EnergyResolution.ONE
        await inv.step()
        assert 65 not in inv.current_plan().addresses
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
            "source": "profile",
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
    assert persisted(tmp_path) == {
        "effective_resolution": 0.1,
        "source": "profile",
        "generation": 2,
    }


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
    assert tracker.observe()[0].kind == "initialised" and tracker.resolution == 0.1
    tracker.invalidate()  # new path / identity: confirm again for this device
    assert tracker.resolution is None
    assert tracker.observe() == [] and tracker.resolution == 0.1
    assert tracker.generation == 0
