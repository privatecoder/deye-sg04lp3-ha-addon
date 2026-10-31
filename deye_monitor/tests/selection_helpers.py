"""Rig for the selection backend and the web API: runtime (loops not started) + manager."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deye_monitor.app import LiveApplier
from deye_monitor.selection import SelectionManager, SelectionStore, specs_from_config
from tests.device_helpers import Rig, config, device_regs, ep, inverter, rig

A = "2401234567"
B = "2409876543"


@dataclass
class Plant:
    rig: Rig
    manager: SelectionManager
    applier: LiveApplier

    @property
    def directory(self) -> Path:
        return self.rig.directory


def two_inverters(**extra_second: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [ep("gw1")],
        [inverter("deye1", "gw1"), inverter("deye2", "gw1", unit_id=2, **extra_second)],
    )


@contextlib.asynccontextmanager
async def plant(
    directory: Path,
    *,
    endpoints: list[dict[str, Any]] | None = None,
    inverters: list[dict[str, Any]] | None = None,
    banks: Mapping[str, Mapping[int, int]] | None = None,
    require_choice: bool = False,
    **extra: Any,
) -> AsyncIterator[Plant]:
    eps, invs = (endpoints, inverters) if endpoints and inverters else two_inverters()
    store = SelectionStore(
        directory, specs_from_config(config(eps, invs, **extra)), require_choice=require_choice
    )
    manager = SelectionManager(store)
    initial = manager.initial()
    async with rig(
        eps,
        invs,
        banks or {"gw1": device_regs(A, soc=55)},
        directory,
        runtime_kw={
            "features": {i: f for i, (f, _, _) in initial.items()},
            "selections": {i: (keys, rev) for i, (_, keys, rev) in initial.items()},
        },
        **extra,
    ) as r:
        for inverter_id, inv in r.runtime.inverters.items():
            inv.awaiting_selection = not manager.selection(inverter_id).chosen
        applier = LiveApplier(r.runtime, None)
        manager.attach(applier)
        yield Plant(r, manager, applier)


def slot_bodies(directory: Path) -> list[tuple[int, str, dict[str, Any]]]:
    """(seq, slot name, document) of every valid selection slot, newest last."""
    import json

    out = []
    for name in ("selection.a.json", "selection.b.json"):
        path = directory / name
        if path.exists():
            try:
                data = json.loads(path.read_text())["data"]
            except ValueError, KeyError:
                continue
            out.append((data["seq"], name, data["body"]))
    return sorted(out, key=lambda t: t[0])


def newest_body(directory: Path) -> dict[str, Any]:
    return slot_bodies(directory)[-1][2]


def newest_slot(directory: Path) -> str:
    return slot_bodies(directory)[-1][1]


def no_selection_files(directory: Path) -> bool:
    return not list(directory.glob("selection.*.json")) and not list(directory.glob(".selection.*"))
