"""Live-43: override 0.1 with R65 = 0xFFFF must publish raw * 0.1."""

from __future__ import annotations

from pathlib import Path

from tests.device_helpers import device_regs, ep, inverter, rig
from tests.fakes.servers import value_of

SN = "2401234567"
RAW = value_of(514)


async def run(tmp_path: Path, override: str) -> tuple[object, object]:
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1", energy_resolution=override)],
        {"gw1": device_regs(SN, r65=0xFFFF)},
        tmp_path,
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        return inv.energy.resolution, r.sink.values("deye1", "battery_energy_charge_today")[-1]


async def test_override_tenth_with_invalid_r65_publishes_tenths(tmp_path: Path) -> None:
    assert await run(tmp_path, "1") == (1.0, RAW)  # the earlier series
    assert await run(tmp_path, "0.1") == (0.1, round(RAW * 0.1, 6))


async def test_diagnostics_carry_resolution_source_r65_and_plausibility(tmp_path: Path) -> None:
    from deye_monitor.webapi import status_view

    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1", energy_resolution="0.1")],
        {"gw1": device_regs(SN, r65=0xFFFF)},
        tmp_path,
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        status = status_view(inv, None)
        assert status["energy"]["effective_resolution"] == 0.1
        assert status["energy"]["source"] == "override"
        assert status["energy"]["plausibility"]["state"] == "unknown"
        assert "raw" in status["r65"]


async def test_counted_power_keys_need_every_active_mppt_and_no_ac_coupling(tmp_path: Path) -> None:
    """R39-02: the plausibility check only integrates a proven common measuring boundary."""
    from deye_monitor.profiles.features import Features

    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN, r65=0xFFFF)},
        tmp_path,
    ) as r:
        inv = r.inv("deye1")
        both = frozenset({"pv1_power", "pv2_power"})
        inv.features = Features(bms=False)
        inv.selection = frozenset(inv.selection) | both
        assert inv._counted_power_keys() == both
        inv.selection = frozenset(inv.selection) - {"pv2_power"}  # only PV1 is selected
        assert inv._counted_power_keys() is None
        inv.features = Features(bms=False, mppt2_used=False)  # one MPPT in use: PV1 is enough
        assert inv._counted_power_keys() == frozenset({"pv1_power"})
        inv.selection = frozenset(inv.selection) | both
        inv.features = Features(bms=False, gen_port_mode="ac_coupled")
        assert inv._counted_power_keys() is None
        inv.features = Features(bms=False, mppt1_used=False, mppt2_used=False)
        assert inv._counted_power_keys() is None
