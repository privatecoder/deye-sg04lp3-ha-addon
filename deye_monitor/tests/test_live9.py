"""Live-9 (0.2.6): zero heuristic per entry, parallel slaves without BMS link, firmware words."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.device import InverterState
from deye_monitor.observation import Observation, ObservationState
from deye_monitor.profiles.schema import load_profile
from deye_monitor.webapi import status_view
from tests.device_helpers import device_regs, ep, inverter, rig

SN = "2401234567"
PROFILE = load_profile("sg04lp3_lv_v105_8")
MASTER, SLAVE = 0x0003, 0x0001  # R336: bit0 parallel enabled, bit1 master
BMS_KEYS = {e.key for e in PROFILE.entries if e.point == "bms" and not e.internal}
ZEROS = {f"r{a}": 0 for a in range(210, 220)}


# -- zero heuristic ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "battery_voltage",
        "battery_soc",
        "battery_temperature",
        "grid_voltage_a",
        "grid_frequency",
        "inverter_frequency",
        "load_voltage_a",
        "pv1_power",
        "pv2_voltage",
        "load_power_total",  # explicit: 0 W with load voltage present is worth a look
    ],
)
def test_physical_measurements_expected_non_zero_are_watched(key: str) -> None:
    assert PROFILE.entry(key).observes_zero


@pytest.mark.parametrize(
    "key",
    [
        "alarm_word_1",
        "alarm_word_2",
        "fault_word_1",
        "fault_word_3",
        "operating_state",  # enum
        "power_switch",
        "relay_state_raw",
        "relay_inverter",  # binary sensor
        "parallel_enable",
        "battery_energy_charge_today",  # "today" counters are 0 after midnight
        "pv1_energy_today_r530",
        "gen_voltage_a",  # an unused gen port reads 0
        "battery_current",  # idle battery
        "grid_selected_power_total",  # zero export
        "tou_slot1_power",  # settings
        "serial_number",
        "mppt_count",
    ],
)
def test_zero_is_normal_for_status_words_flags_counters_and_settings(key: str) -> None:
    assert not PROFILE.entry(key).observes_zero


def test_placeholders_and_second_battery_are_never_watched() -> None:
    for e in PROFILE.entries:
        if e.support_status != "supported" or e.release == "unresolved":
            assert not e.observes_zero, e.key
        if e.key.startswith(("battery2_", "pv3_", "pv4_")):
            assert not e.observes_zero, e.key


def test_an_entry_can_override_the_category_default() -> None:
    entry = PROFILE.entry("load_power_total")
    assert entry.observe_zero is True
    assert PROFILE.entry("grid_voltage_a").observe_zero is None  # follows the default


def test_observation_without_watch_never_reports_zero_only() -> None:
    plain = Observation()
    watched = Observation(watch_zero=True)
    for _ in range(40):
        plain.record(0)
        watched.record(0)
    assert plain.state is ObservationState.OBSERVED_ACTIVE
    assert watched.state is ObservationState.ZERO_ONLY_INCONCLUSIVE


def test_pv_zeros_count_only_by_day() -> None:
    night = Observation(watch_zero=True, daylight_only=True)
    for _ in range(60):
        night.record(0, daylight=False)
    assert night.state is ObservationState.OBSERVED_ACTIVE  # 0 at night is normal
    day = Observation(watch_zero=True, daylight_only=True)
    for _ in range(40):
        day.record(0, daylight=True)
    assert day.state is ObservationState.ZERO_ONLY_INCONCLUSIVE
    day.record(120, daylight=True)
    assert day.state.value == "observed_active"


async def test_status_words_at_zero_give_no_hint_but_a_dead_load_power_does(
    tmp_path: Path,
) -> None:
    regs = device_regs(SN, r336=0, r588=55, r653=0, r659=0)
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        for _ in range(35):
            await inv.step()
            r.clock.offset += 11
        keys = {h["key"] for h in status_view(inv, None)["hints"]}
        assert not keys & {"fault_word_1", "alarm_word_1", "operating_state"}
        assert inv.observations["load_power_total"].watch_zero is True


# -- parallel slave ------------------------------------------------------------------------------


async def _run(tmp_path: Path, **regs: int):  # type: ignore[no-untyped-def]
    banks = {"gw1": device_regs(SN, **regs)}
    return rig([ep("gw1")], [inverter("deye1", "gw1")], banks, tmp_path)


async def test_a_slave_without_bms_link_drops_the_bms_block_and_the_temperature(
    tmp_path: Path,
) -> None:
    async with await _run(tmp_path, r336=SLAVE, r586=180 + 1000, **ZEROS) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.state is InverterState.RUNNING
        assert set(inv.inactive) == BMS_KEYS | {"battery_temperature"}
        assert inv.inactive["battery_temperature"] == "parallel_slave_temperature"
        assert {inv.inactive[k] for k in BMS_KEYS} == {"parallel_slave_no_bms"}
        assert not inv.current_plan().addresses & {586, *range(210, 220)}  # not polled
        view = status_view(inv, None)
        assert view["inactive"]["bms_soc"] == {"reason": "parallel_slave_no_bms"}
        r.clock.offset += 11
        await inv.step()
        values = {k for _, s, v in r.sink.states for k in v}
        assert not values & (BMS_KEYS | {"battery_temperature"})  # no entity value either


async def test_a_slave_with_a_working_bms_link_keeps_the_bms_but_not_the_temperature(
    tmp_path: Path,
) -> None:
    async with await _run(tmp_path, r336=SLAVE, r214=77) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert set(inv.inactive) == {"battery_temperature"}


@pytest.mark.parametrize(("role", "name"), [(MASTER, "master"), (0, "independent")])
async def test_master_and_independent_inverters_keep_everything(
    tmp_path: Path, role: int, name: str
) -> None:
    async with await _run(tmp_path, r336=role, r586=1000 + 250, **ZEROS) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.inactive == {}, name  # an all-zero BMS block on the master is a real fault
        assert inv.current_plan().addresses >= {586}


async def test_role_change_is_picked_up_at_the_identity_pass(tmp_path: Path) -> None:
    async with await _run(tmp_path, r336=SLAVE, **ZEROS) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.inactive
        r.servers["gw1"].responder.regs[336] = MASTER  # the unit becomes the master
        r.clock.offset += 4000
        await inv.step()
        assert inv.inactive == {}
        assert inv.current_plan().addresses >= {586}


async def test_firmware_words_include_the_control_board(tmp_path: Path) -> None:
    async with await _run(tmp_path, r14=0x2005, r15=0x1175, r11=0x1807) as r:
        inv = r.inv("deye1")
        await inv.step()
        words = inv.diagnostics()["identity"]["firmware_words"]
        assert (words[14], words[15], words[11]) == (0x2005, 0x1175, 0x1807)
