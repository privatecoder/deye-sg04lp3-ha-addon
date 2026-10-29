"""Energy epoch with the serial number (T7-08) and consecutive R65 confirmation (T7-09)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deye_monitor.config import EnergyResolution
from deye_monitor.device import InverterState, RuntimeSettings
from deye_monitor.energy_state import EnergyTracker, valid_energy_document
from deye_monitor.store import JsonStore
from tests.device_helpers import Rig, device_regs, ep, install_gate, inverter, rig, state_of

A = "2401234567"
B = "9999999999"
SETTINGS = RuntimeSettings(r65_confirm_s=60.0)


def record(tmp_path: Path) -> dict[str, Any]:
    return dict(json.loads((tmp_path / "energy.json").read_text())["data"]["deye1"])


def energy(r: Rig) -> list[Any]:
    return r.sink.values("deye1", "battery_energy_charge_today")


# -- T7-08 --------------------------------------------------------------------------------------


async def test_rebind_to_new_serial_starts_a_new_energy_series(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(A, r65=0x0104)},
        tmp_path,
        settings=SETTINGS,
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert record(tmp_path)["generation"] == 0 and record(tmp_path)["serial"] == A
        r.servers["gw1"].responder.regs.update(
            device_regs(B, r65=0x0104)
        )  # same factor, new device
        assert inv.rebind() is True
        await inv.step()
        assert inv.energy.resolution is None  # the new epoch is confirmed first
        assert record(tmp_path)["generation"] == 0  # nothing committed yet, nothing reused either
        r.clock.offset += 61  # the confirming read is due
        await inv.step()
        assert inv.energy.resolution == 0.1
        assert record(tmp_path) == {
            "effective_resolution": 0.1,
            "source": "r65",
            "generation": 1,
            "serial": B,
        }
        assert [s.energy_generation for _, s, _ in r.sink.states][-1] == 1
        assert r.sink.events("energy_generation_changed")[-1]["generation"] == 1


async def test_override_gives_the_new_series_at_once(tmp_path: Path) -> None:
    cfg = inverter("deye1", "gw1", energy_resolution="0.1")
    async with rig([ep("gw1")], [cfg], {"gw1": device_regs(A)}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].responder.regs.update(device_regs(B))
        inv.rebind()
        await inv.step()
        assert record(tmp_path)["generation"] == 1 and record(tmp_path)["serial"] == B


async def test_changed_expected_serial_starts_a_new_series_after_a_restart(tmp_path: Path) -> None:
    cfg_a = inverter("deye1", "gw1", energy_resolution="0.1", expected_serial=A)
    async with rig([ep("gw1")], [cfg_a], {"gw1": device_regs(A)}, tmp_path) as r:
        await r.inv("deye1").step()
    cfg_b = inverter("deye1", "gw1", energy_resolution="0.1", expected_serial=B)
    for _ in range(2):  # the second start must not migrate again
        async with rig([ep("gw1")], [cfg_b], {"gw1": device_regs(B)}, tmp_path) as r:
            await r.inv("deye1").step()
        assert record(tmp_path)["generation"] == 1 and record(tmp_path)["serial"] == B


async def test_the_same_physical_inverter_on_another_path_changes_nothing(tmp_path: Path) -> None:
    endpoints = [ep("gw1"), ep("logger1", "solarman_v5_tcp")]
    inverters = [inverter("deye1", "gw1", fallback_endpoint="logger1", energy_resolution="0.1")]
    banks = {"gw1": device_regs(A), "logger1": device_regs(A)}
    async with rig(endpoints, inverters, banks, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        inv._switch(1, r.clock(), "test")
        for _ in range(3):
            await inv.step()
        assert inv.route.name == "fallback" and state_of(inv) is InverterState.RUNNING
        assert record(tmp_path)["generation"] == 0  # reconnect and path change: same serial


async def test_crash_between_identity_and_energy_persistence_is_idempotent(tmp_path: Path) -> None:
    # state after a crash: the binding to B is on disk, the energy record still belongs to A
    (tmp_path / "identity.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "data": {"bindings": {"deye1": {"serial": B, "source": "first_seen"}}},
            }
        )
    )
    (tmp_path / "energy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "data": {
                    "deye1": {
                        "effective_resolution": 0.1,
                        "source": "override",
                        "generation": 0,
                        "serial": A,
                    }
                },
            }
        )
    )
    (tmp_path / "energy-generations.json").write_text(
        json.dumps({"schema_version": 1, "data": {"deye1": 0}})
    )
    cfg = inverter("deye1", "gw1", energy_resolution="0.1")
    for _ in range(3):
        async with rig([ep("gw1")], [cfg], {"gw1": device_regs(B)}, tmp_path) as r:
            await r.inv("deye1").step()
        assert record(tmp_path)["generation"] == 1 and record(tmp_path)["serial"] == B


async def test_simultaneous_serial_and_factor_change_migrates_once(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(A, r65=0x0104)},
        tmp_path,
        settings=SETTINGS,
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].responder.regs.update(device_regs(B, r65=0x0105))
        inv.rebind()
        await inv.step()
        r.clock.offset += 61
        await inv.step()
        assert record(tmp_path) == {
            "effective_resolution": 1.0,
            "source": "r65",
            "generation": 1,
            "serial": B,
        }


async def test_a_record_without_serial_is_migrated_without_a_new_series(tmp_path: Path) -> None:
    (tmp_path / "energy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "data": {"deye1": {"effective_resolution": 0.1, "source": "r65", "generation": 3}},
            }
        )
    )
    (tmp_path / "energy-generations.json").write_text(
        json.dumps({"schema_version": 1, "data": {"deye1": 3}})
    )
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r65=0x0104)}, tmp_path
    ) as r:
        await r.inv("deye1").step()
    assert record(tmp_path) == {
        "effective_resolution": 0.1,
        "source": "r65",
        "generation": 3,
        "serial": A,
    }


# -- T7-09 --------------------------------------------------------------------------------------


def tracker(tmp_path: Path, factor: int = 0x0104) -> EnergyTracker:
    store = JsonStore(tmp_path / "e.json", validate=valid_energy_document)
    t = EnergyTracker(store, "a", EnergyResolution.AUTO)
    t.observe(factor, A)
    return t


def test_a_missing_r65_breaks_the_consecutive_confirmation(tmp_path: Path) -> None:
    t = tracker(tmp_path)
    assert t.observe(0x0105, A)[0].kind == "change_pending"
    assert t.observe(None, A) == [] and not t.pending  # the gap resets the confirmation
    assert t.observe(0x0105, A)[0].kind == "change_pending"  # starts over
    assert t.resolution is None and t.generation == 0
    events = t.observe(0x0105, A)  # two CONSECUTIVE readings
    assert events and events[0].kind == "generation_changed" and t.generation == 1


def test_the_other_valid_value_in_between_also_breaks_it(tmp_path: Path) -> None:
    t = tracker(tmp_path)
    t.observe(0x0105, A)
    t.observe(0x0104, A)  # back to the confirmed factor: no change at all
    assert not t.pending and t.resolution == 0.1
    t.observe(0x0105, A)
    assert t.pending and t.generation == 0
    t.observe(0x0105, A)
    assert t.generation == 1  # B, A, B, B confirms only at the last pair


def test_invalidate_clears_a_pending_change(tmp_path: Path) -> None:
    t = tracker(tmp_path)
    t.observe(0x0105, A)
    t.invalidate()  # path switch / identity change
    assert not t.pending and t.resolution is None
    assert t.observe(0x0105, A)[0].kind == "change_pending"  # again the first of two


async def test_confirmation_read_comes_after_r65_confirm_s_on_the_same_scheduler(
    tmp_path: Path,
) -> None:
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(A, r65=0x0104)},
        tmp_path,
        settings=SETTINGS,
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        proxy = install_gate(r, "deye1")
        r.servers["gw1"].responder.regs[65] = 0x0105
        r.clock.offset += 4000
        await inv.step()  # identity refresh reads the new R65: pending
        assert inv.energy.pending and inv.energy.resolution is None
        count = sum(1 for req in proxy.requests if req[1] == 65)
        r.clock.offset += 30
        await inv.step()
        assert sum(1 for req in proxy.requests if req[1] == 65) == count  # not due yet
        r.clock.offset += 31
        await inv.step()
        assert sum(1 for req in proxy.requests if req[1] == 65) == count + 1  # one confirming read
        assert inv.energy.resolution == 1.0 and record(tmp_path)["generation"] == 1
        assert inv.diagnostics()["energy"]["generation"] == 1


async def test_a_failing_confirmation_read_resets_the_pending_change(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(A, r65=0x0104)},
        tmp_path,
        settings=SETTINGS,
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].responder.regs[65] = 0x0105
        r.clock.offset += 4000
        await inv.step()
        assert inv.energy.pending
        r.servers["gw1"].responder.fail_addresses = {65}  # the confirming read is refused
        r.clock.offset += 61
        await inv.step()
        assert not inv.energy.pending and record(tmp_path)["generation"] == 0
        assert set(r.sink.values("deye1", "battery_energy_charge_today")[-1:]) == {None}


def test_settings_validate_the_confirmation_delay() -> None:
    import pytest

    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            RuntimeSettings(r65_confirm_s=bad)
    assert RuntimeSettings().r65_confirm_s == 60.0


async def test_a_record_without_any_history_blocks_energy_until_an_explicit_reset(
    tmp_path: Path,
) -> None:
    """SPEC §5: only "both files missing" is a fresh installation."""
    (tmp_path / "energy.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "data": {"deye1": {"effective_resolution": 0.1, "source": "r65", "generation": 3}},
            }
        )
    )
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r65=0x0104)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.energy.blocked == "energy_history_missing"
        assert inv.energy.resolution is None
        await inv.step()
        assert record(tmp_path)["generation"] == 3  # nothing was rewritten or advanced
        r.runtime.reset_store("deye1", "energy")
        assert inv.energy.blocked is None
