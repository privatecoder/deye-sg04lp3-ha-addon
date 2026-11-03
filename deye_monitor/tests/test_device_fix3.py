"""T7-Fix 3 (Codex verification 3, N3-01..N3-03): ended series stay ended, a failing notification
cannot leave a reset half done, every outcome of a request compares the connection epoch."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deye_monitor.config import EnergyResolution
from deye_monitor.device import InverterState
from tests.device_helpers import (
    Recorder,
    device_regs,
    ep,
    install_gate,
    inverter,
    rig,
)
from tests.test_device_fix2 import A, B, trackers

# -- N3-01 -----------------------------------------------------------------------------------


def used_g0_and_g1_then_restore_main(tmp_path: Path) -> None:
    (first,) = trackers(tmp_path, "i")
    first.observe(A)
    old = (tmp_path / "energy.json").read_bytes()
    first._override = EnergyResolution.TENTH
    first.observe(A)
    assert first.series == "g1"
    (tmp_path / "energy.json").write_bytes(old)


def test_main_rollback_same_sn_same_factor_must_not_revive_g0(tmp_path: Path) -> None:
    used_g0_and_g1_then_restore_main(tmp_path)
    (again,) = trackers(tmp_path, "i", override=EnergyResolution.ONE)
    again.observe(A)
    assert again.resolution == 1.0
    assert again.series == "g2"


def test_latest_same_context_reservation_can_continue_idempotently(tmp_path: Path) -> None:
    used_g0_and_g1_then_restore_main(tmp_path)
    (again,) = trackers(tmp_path, "i", override=EnergyResolution.TENTH)
    again.observe(A)
    assert again.resolution == 0.1 and again.series == "g1"


def test_current_series_continues_across_restarts(tmp_path: Path) -> None:
    (first,) = trackers(tmp_path, "i")
    first.observe(A)
    for _ in range(3):
        (again,) = trackers(tmp_path, "i")
        again.observe(A)
        assert again.series == "g0" and again.resolution == 1.0


def test_rollback_in_auto_mode_never_revives_an_ended_series(tmp_path: Path) -> None:
    (first,) = trackers(tmp_path, "i", override=EnergyResolution.ONE)
    first.observe(A)  # 1 kWh, g0
    old = (tmp_path / "energy.json").read_bytes()
    first._override = EnergyResolution.AUTO
    first.observe(A)  # the profile default 0.1 kWh, g1 at once (R65 is not consulted)
    assert first.series == "g1" and first.resolution == 0.1
    (tmp_path / "energy.json").write_bytes(old)
    (again,) = trackers(tmp_path, "i", override=EnergyResolution.AUTO)
    again.observe(A)
    # the reserved change to 0.1 continues as g1; the ended 1 kWh series g0 is never revived
    assert again.resolution == 0.1 and again.series == "g1"


# -- N3-02 -----------------------------------------------------------------------------------


class NotifyFailure(Recorder):
    enabled = False

    def on_availability(self, who: str, state: InverterState, reason: str) -> None:
        super().on_availability(who, state, reason)
        if self.enabled and who == "a" and state is InverterState.OFFLINE:
            raise RuntimeError("unexpected observer failure during global reset")


async def test_global_reset_invalidates_every_peer_despite_notification_error(
    tmp_path: Path,
) -> None:
    sink = NotifyFailure()
    async with rig(
        [ep("ga", port=25001), ep("gb", port=25002)],
        [inverter("a", "ga"), inverter("b", "gb")],
        {"ga": device_regs(A), "gb": device_regs(B, soc=55)},
        tmp_path,
        sink=sink,
    ) as r:
        a, b = r.inv("a"), r.inv("b")
        await a.step()
        await b.step()
        proxy = install_gate(r, "b")
        entered, release = proxy.gate(587)
        before, epoch_a, epoch_b = len(sink.states), a.lifecycle_epoch, b.lifecycle_epoch
        task = asyncio.create_task(b._run_group("battery"))
        await asyncio.wait_for(entered.wait(), 1)
        sink.enabled = True
        error: str | None = None
        try:
            try:
                r.runtime.reset_store(None, "identity")
            except RuntimeError as exc:
                error = str(exc)  # the caller may be told, but every peer must be revoked
        finally:
            release.set()
            await task
            sink.enabled = False
        assert error is not None
        assert a.lifecycle_epoch > epoch_a and b.lifecycle_epoch > epoch_b
        assert r.runtime.book.bound("b") is None
        assert not [v for who, _, v in sink.states[before:] if who == "b"]
        assert a.released.is_set() and b.released.is_set()
        assert b.needs_identity and b.state is InverterState.STARTING


async def test_rebind_wakes_and_bumps_before_a_failing_notification(tmp_path: Path) -> None:
    sink = NotifyFailure()
    async with rig(
        [ep("g")], [inverter("a", "g")], {"g": device_regs(A)}, tmp_path, sink=sink
    ) as r:
        a = r.inv("a")
        await a.step()
        epoch = a.lifecycle_epoch
        sink.enabled = True
        with pytest.raises(RuntimeError):
            a.rebind()
        sink.enabled = False
        assert a.lifecycle_epoch > epoch and a.needs_identity and a._wake.is_set()
