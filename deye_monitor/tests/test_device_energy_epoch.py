"""Energy epoch with the serial number (T7-08); R65 no longer selects the factor (0.5.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deye_monitor.device import InverterState
from tests.device_helpers import (
    Rig,
    device_regs,
    ep,
    inverter,
    rig,
    state_of,
)

A = "2401234567"
B = "9999999999"


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
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert record(tmp_path)["generation"] == 0 and record(tmp_path)["serial"] == A
        r.servers["gw1"].responder.regs.update(
            device_regs(B, r65=0x0104)
        )  # same factor, new device
        assert inv.rebind() is True
        await inv.step()
        assert inv.energy.resolution == 0.1  # a fixed factor: nothing to confirm
        assert record(tmp_path) == {
            "effective_resolution": 0.1,
            "source": "profile",
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


async def test_a_new_serial_at_the_profile_default_migrates_once_whatever_r65_says(
    tmp_path: Path,
) -> None:
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(A, r65=0x0104)},
        tmp_path,
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].responder.regs.update(device_regs(B, r65=0x0105))
        inv.rebind()
        await inv.step()
        await inv.step()
        assert record(tmp_path) == {
            "effective_resolution": 0.1,
            "source": "profile",
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
    assert record(tmp_path) == {  # the pre-0.5.1 source "r65" becomes "profile", same series
        "effective_resolution": 0.1,
        "source": "profile",
        "generation": 3,
        "serial": A,
    }


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
