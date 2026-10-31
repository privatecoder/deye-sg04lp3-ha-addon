"""Fields the device does not report: checked once, then not read, not published (0.2.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deye_monitor.config import EnergyResolution
from deye_monitor.device import InverterState
from deye_monitor.profiles.decode import Value
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.plan import build_read_plan, unreported_entries
from deye_monitor.profiles.schema import load_profile
from tests.device_helpers import Rig, device_regs, ep, inverter, rig

SN = "2401234567"
PROFILE = load_profile("sg04lp3_lv_v105_8")
LIVE = {"r22": 0xFFFF, "r24": 5, "r65": 0xFFFF}  # the live finding: FW 0x1175, master 1


def _values(r: Rig, key: str) -> list[Value]:
    return r.sink.values("deye1", key)


async def test_the_live_values_give_profile_values_with_source_profile(tmp_path: Path) -> None:
    regs = device_regs(SN, **LIVE)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.state is InverterState.RUNNING
        found = inv.diagnostics()["unreported"]
        assert {k: v["raw"] for k, v in found.items()} == {
            "mppt_count": 0xFFFF,
            "phase_count": 0xFFFF,
            "battery_input_code": 5,
            "dsp_protocol_version": 0xFFFF,
        }
        assert (
            found["mppt_count"]["register"] == 22 and found["battery_input_code"]["register"] == 24
        )
        assert (_values(r, "mppt_count")[-1], _values(r, "phase_count")[-1]) == (2, 3)
        assert _values(r, "battery_input_code")[-1] == 1
        snaps = [s for _, s, v in r.sink.states if "mppt_count" in v]
        assert snaps and snaps[-1].profile_keys == {
            "mppt_count",
            "phase_count",
            "battery_input_code",
        }
        assert "dsp_protocol_version" not in snaps[-1].profile_keys
        stored = json.loads((tmp_path / "capabilities.json").read_text())["data"]["deye1"]
        assert stored["unreported"]["battery_input_code"] == 5


async def test_reported_values_come_from_the_device(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.diagnostics()["unreported"] == {}
        assert _values(r, "mppt_count")[-1] == 2 and _values(r, "battery_input_code")[-1] == 1
        assert all(not s.profile_keys for _, s, _ in r.sink.states)


async def test_a_firmware_change_checks_every_field_again(tmp_path: Path) -> None:
    bad = device_regs(SN, **LIVE)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": bad}, tmp_path) as r:
        await r.inv("deye1").step()
        assert r.inv("deye1").unreported
    # restart: the stored result is used until the identity says otherwise
    good = device_regs(SN, r15=0x1176, r65=0x0104)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": good}, tmp_path) as r:
        inv = r.inv("deye1")
        assert inv.unreported  # loaded from the capability store
        await r.step_until("deye1", lambda i: i.state is InverterState.RUNNING)
        assert inv.unreported == {}
        assert inv.energy.resolution == 0.1


async def test_a_user_choice_stops_reading_r65(tmp_path: Path) -> None:
    regs = device_regs(SN, **LIVE)
    chosen = inverter("deye1", "gw1", energy_resolution="0.1")
    async with rig([ep("gw1")], [chosen], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert not inv.current_plan().reads_r65
        assert inv.diagnostics()["energy"]["source"] == "override"


def test_the_plan_leaves_unreported_entries_out() -> None:
    features = Features()
    selected = {"mppt_count", "dsp_protocol_version", "battery_soc"}
    full = build_read_plan(PROFILE, selected, features, max_read_registers=60, energy_auto=False)
    cut = build_read_plan(
        PROFILE,
        selected,
        features,
        max_read_registers=60,
        energy_auto=False,
        unreported={"dsp_protocol_version"},
    )
    assert 65 in full.addresses and 65 not in cut.addresses
    assert 22 in cut.addresses  # the internal mandatory identity word stays
    auto = build_read_plan(
        PROFILE,
        {"battery_soc", "grid_energy_import_today"},
        features,
        max_read_registers=60,
        energy_auto=True,
        unreported={"dsp_protocol_version"},
    )
    assert 65 in auto.addresses  # automatic energy resolution still needs R65


def test_unreported_entries_are_per_field() -> None:
    found = unreported_entries(PROFILE, {0: 0x0500, 22: 0xFFFF, 24: 2, 65: 0, 588: 0xFFFF})
    assert set(found) == {"mppt_count", "phase_count", "dsp_protocol_version"}
    assert unreported_entries(PROFILE, {}) == {}
    assert EnergyResolution.AUTO  # imported for the config contract


@pytest.mark.parametrize("changed", ["r15", "r17", "r18", "profile_revision", "serial"])
async def test_each_identity_version_resets_the_persisted_unreported_fields(
    tmp_path: Path, changed: str
) -> None:
    cfg = inverter("deye1", "gw1", energy_resolution="0.1")
    bad = device_regs(SN, r0=0xFFFF, r22=0xFFFF, r24=5, r65=0xFFFF)
    async with rig([ep("gw1")], [cfg], {"gw1": bad}, tmp_path) as r:
        inv = r.inv("deye1")
        inv.set_selection(set(inv.selection) | {"dsp_protocol_version"}, 1)
        await inv.step()
        assert inv.unreported and 65 not in inv.current_plan().addresses
    if changed == "profile_revision":
        # rewrite through the real store, so that its integrity wrapper stays valid
        r.runtime._capabilities.data["deye1"]["key"][-1] = "old-profile-revision"
        r.runtime._capabilities.save()
    good = device_regs("2409876543" if changed == "serial" else SN, r65=0x0104)
    if changed == "serial":
        cfg = inverter("deye1", "gw1", energy_resolution="0.1", expected_serial="2409876543")
    if changed not in ("profile_revision", "serial"):
        good[int(changed[1:])] += 1
    async with rig([ep("gw1")], [cfg], {"gw1": good}, tmp_path) as r:
        inv = r.inv("deye1")
        inv.set_selection(set(inv.selection) | {"dsp_protocol_version"}, 1)
        await r.step_until("deye1", lambda i: i.state is InverterState.RUNNING)
        assert inv.unreported == {} and 65 in inv.current_plan().addresses
        assert _values(r, "dsp_protocol_version")[-1] == 0x0104


async def test_the_periodic_identity_restores_a_field_on_the_same_connection(
    tmp_path: Path,
) -> None:
    cfg = inverter("deye1", "gw1", energy_resolution="0.1")
    async with rig([ep("gw1")], [cfg], {"gw1": device_regs(SN, r65=0xFFFF)}, tmp_path) as r:
        inv = r.inv("deye1")
        inv.set_selection(set(inv.selection) | {"dsp_protocol_version"}, 1)
        await inv.step()
        assert "dsp_protocol_version" in inv.unreported
        epoch = inv.route.transport.connection_epoch
        r.servers["gw1"].responder.regs.update({15: 0x1176, 65: 0x0104})
        r.clock.offset += inv.cfg.poll_identity_s + 1
        await inv.step()
        await inv.step()
        assert inv.route.transport.connection_epoch == epoch
        assert "dsp_protocol_version" not in inv.unreported
        assert _values(r, "dsp_protocol_version")[-1] == 0x0104
