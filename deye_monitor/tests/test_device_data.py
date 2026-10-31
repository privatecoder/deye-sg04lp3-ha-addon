"""Snapshots, selection vs plan, bootstrap reads, capabilities, ticks, observations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deye_monitor.device import InverterState
from deye_monitor.observation import ObservationState
from deye_monitor.profiles.plan import SelectionError
from tests.device_helpers import device_regs, ep, inverter, rig, state_of
from tests.fakes.servers import Behavior, value_of

SN = "2401234567"


async def test_missing_partner_makes_only_that_entity_unknown(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert r.sink.values("deye1", "load_power_total")[-1] is not None
        count = len(r.sink.states)
        r.servers["gw1"].responder.fail_addresses = {659}  # the high word of load_power_total
        r.clock.offset += 11
        await inv.step()
        ac = [(s, v) for _, s, v in r.sink.states[count:] if s.group == "ac"]
        assert ac, "the ac group was published again"
        snapshot, values = ac[-1]
        assert values["load_power_total"] is None  # no carry-forward of the previous value
        assert 659 in snapshot.missing and 659 not in snapshot.words
        assert values["inverter_power_total"] is not None  # other entities of the group are fine
        assert values["grid_voltage_a"] == value_of(598) / 10
        assert inv.observations["load_power_total"].state is ObservationState.READ_FAILED


async def test_each_snapshot_holds_only_words_of_its_own_group_and_cycle(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.clock.offset += 11
        await inv.step()
        plan = inv.current_plan()
        ids = [s.cycle_id for _, s, _ in r.sink.states]
        assert ids == sorted(set(ids))  # unique, increasing
        for _, snap, _ in r.sink.states:
            allowed = {
                a
                for b in plan.blocks
                if b.group == snap.group
                for a in range(b.start, b.start + b.count)
            }
            assert set(snap.words) <= allowed
            assert snap.endpoint_id == "gw1"
            assert not set(snap.words) & snap.missing
        groups = {s.group for _, s, _ in r.sink.states}
        assert {"identity", "status", "battery", "ac", "pv", "energy", "settings"} <= groups


async def test_empty_selection_creates_no_entities_but_keeps_identity_and_health(
    tmp_path: Path,
) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        inv.set_selection(set(), revision=7)
        await inv.step()
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING  # identity binding and health still run
        assert r.runtime.book.bound("deye1") == SN
        assert r.sink.states == []  # no inverter entities at all
        assert inv.diagnostics()["selection_revision"] == 7
        plan = inv.current_plan()
        assert {500, 555, 556, 557, 558} <= plan.addresses  # mandatory reads are still planned
        assert {"operating_state", "fault_word_1"} <= plan.keys


async def test_explicitly_selected_mandatory_word_is_published_and_only_it(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN, r555=0x1000)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        inv.set_selection({"fault_word_1"}, revision=2)
        await inv.step()
        assert r.sink.states
        for _, snap, values in r.sink.states:
            assert set(values) == {"fault_word_1"}
            assert snap.group == "status"
        assert r.sink.values("deye1", "fault_word_1")[-1] == 0x1000
        assert "operating_state" not in r.sink.states[-1][2]  # read, but not an entity


async def test_set_selection_rejects_placeholders_and_keeps_the_old_selection(
    tmp_path: Path,
) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        before = inv.selection
        with pytest.raises(SelectionError):
            inv.set_selection({"pv3_power", "battery_soc"}, revision=3)
        assert inv.selection == before and inv.selection_revision == 0


async def test_bootstrap_reads_provide_suggested_features(tmp_path: Path) -> None:
    regs = device_regs(SN, r111=2, r133=2, r189=1, r336=0x0001)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        suggestion = inv.diagnostics()["suggested_features"]
        assert suggestion["battery"] is False  # R111 = 2: no battery
        assert suggestion["gen_port_mode"] == "ac_coupled"  # R133 = 2
        assert suggestion["grid_port_use"] == "generator"  # R189 bit 0
        assert suggestion["parallel"] is True  # R336 bit 0
        assert set(suggestion["_basis"]) == {
            "battery",
            "gen_port_mode",
            "grid_port_use",
            "parallel",
        }
        published = {k for _, _, v in r.sink.states for k in v}
        assert "battery_operating_basis" not in published  # internal read, no entity
        assert {111, 133, 189} <= inv.current_plan().addresses
        # re-read in the settings interval
        regs_server = r.servers["gw1"].responder.regs
        regs_server.update({111: 0, 189: 0})
        r.clock.offset += 301
        await inv.step()
        again = inv.diagnostics()["suggested_features"]
        assert again["battery"] is True and again["grid_port_use"] == "utility"


@pytest.mark.parametrize(("r98", "expected"), [(1, True), (0, False)])
async def test_battery_type_suggests_the_bms_answer(
    tmp_path: Path, r98: int, expected: bool
) -> None:
    regs = device_regs(SN, r98=r98)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        suggestion = inv.diagnostics()["suggested_features"]
        assert suggestion["bms"] is expected
        assert "R98" in suggestion["_basis"]["bms"]


async def test_unknown_battery_type_suggests_no_bms_answer(tmp_path: Path) -> None:
    regs = device_regs(SN, r98=7)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert "bms" not in inv.diagnostics()["suggested_features"]


async def test_capability_conflict_is_reported_and_unlocks_nothing(tmp_path: Path) -> None:
    regs = device_regs(SN, r22=0x0503)  # the document example: five MPPT, not SG04
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        selection_before = inv.selection
        await inv.step()
        codes = {c["code"] for c in inv.diagnostics()["capability_conflicts"]}
        assert codes == {"mppt_count"}
        assert any(d["event"] == "capability_conflict" for _, d in r.sink.diagnostics)
        assert inv.selection == selection_before  # nothing switched on or off
        published = {k for _, _, v in r.sink.states for k in v}
        assert not [k for k in published if k.startswith(("pv3", "pv4", "pv5"))]
        assert inv.observations["pv1_power"].state is ObservationState.CAPABILITY_CONFLICT
        assert inv.observations["battery_soc"].state is not ObservationState.CAPABILITY_CONFLICT
        assert state_of(inv) is InverterState.RUNNING  # a conflict is a diagnostic, not an error


async def test_capability_cache_key_and_invalidation(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        stored = json.loads((tmp_path / "capabilities.json").read_text())["data"]["deye1"]
        assert stored["key"][:4] == [SN, 0x1001, 0x1000, 0x0070]
        assert stored["key"][4] == inv.profile.revision
        assert len(r.sink.events("capabilities_cached")) == 1
        r.clock.offset += 4000  # identity refresh with unchanged identity: same cache entry
        await inv.step()
        assert len(r.sink.events("capabilities_cached")) == 1 and not r.sink.events(
            "capabilities_updated"
        )
        r.servers["gw1"].responder.regs[15] = 0x1002  # firmware change invalidates the cache
        r.clock.offset += 4000
        await inv.step()
        assert len(r.sink.events("capabilities_updated")) == 1
        assert (
            json.loads((tmp_path / "capabilities.json").read_text())["data"]["deye1"]["key"][1]
            == 0x1002
        )


async def test_overload_coalesces_ticks_instead_of_catching_up(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1", poll_fast_s=10)], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        battery = [s for _, s, _ in r.sink.states if s.group == "battery"]
        assert len(battery) == 1
        r.clock.offset += 55  # five intervals were missed
        await inv.step()
        battery = [s for _, s, _ in r.sink.states if s.group == "battery"]
        assert len(battery) == 2  # one cycle, not five
        assert inv.coalesced_ticks >= 4
        count = len(r.sink.states)
        await inv.step()
        assert len(r.sink.states) == count  # nothing is due: no burst afterwards
        assert 0 < await inv.step() <= 10.0


async def test_observation_counters(tmp_path: Path) -> None:
    regs = device_regs(SN, r672=0, r676=3500)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        inv.daylight = lambda _ts: True  # PV zeros only count by day
        for _ in range(32):
            await inv.step()
            r.clock.offset += 11
        zero = inv.observations["pv1_power"]
        assert zero.responses >= 30 and zero.zeros == zero.responses
        assert zero.state is ObservationState.ZERO_ONLY_INCONCLUSIVE  # never "unsupported"
        volt = inv.observations["pv1_voltage"]
        assert volt.state is ObservationState.OBSERVED_ACTIVE
        assert (volt.minimum, volt.maximum, volt.variability) == (350.0, 350.0, 1)
        assert zero.daylight_samples >= 0
        assert (
            "pv1_power" in inv.profile.entry("pv1_power").key
        )  # status data only: nothing changed
        assert inv.selection >= {"pv1_power"}


async def test_old_response_after_timeout_never_becomes_a_value_end_to_end(tmp_path: Path) -> None:
    regs = device_regs(SN)
    endpoint = ep("gw1", "rtu_over_tcp", timeout_s=0.2, rtu_late_window_s=0.4)
    async with rig([endpoint], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        r.servers["gw1"].script = [Behavior(), Behavior(), Behavior(delay_s=0.3)]  # a late answer
        for _ in range(8):
            await inv.step()
            r.clock.pass_cooldown()
        assert state_of(inv) is InverterState.RUNNING
        assert r.sink.states
        # every address answers value_of(address) (or the fixed identity words): any answer that
        # was assigned to the wrong request would show up as a wrong word
        for _, snap, _ in r.sink.states:
            for addr, word in snap.words.items():
                assert word == regs.get(addr, value_of(addr)), (snap.group, addr)
        assert any(t.counters.timeouts for t in r.runtime.transports.values())


async def test_battery_temperature_follows_the_main_firmware_of_the_identity(
    tmp_path: Path,
) -> None:
    for fw, expected, sub in ((0x1175, 17.6, "a"), (0x1151, 117.6, "b")):
        regs = device_regs(SN, r586=1176, r15=fw, r111=1)
        async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path / sub) as r:
            inv = r.inv("deye1")
            await inv.step()
            assert r.sink.values("deye1", "battery_temperature")[-1] == expected
            applied = inv.diagnostics()["identity"]["firmware_offsets"]["battery_temperature"]
            assert applied["basis"] == "firmware"


async def test_unreported_capability_words_are_no_conflict_and_stay_visible(
    tmp_path: Path,
) -> None:
    regs = device_regs(SN, r0=0xFFFF, r22=0xFFFF, r24=0xFFFF)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        diag = inv.diagnostics()
        assert diag["capability_conflicts"] == []
        assert diag["identity"]["capabilities_unreported"] == [
            "device_type",
            "mppt_phases",
            "battery_input_code",
        ]
        words = diag["identity"]["capability_words"]
        assert (words[0], words[22], words[24]) == (0xFFFF, 0xFFFF, 0xFFFF)  # raw values kept
