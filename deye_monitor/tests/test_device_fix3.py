"""T7-Fix 3 (Codex verification 3, N3-01..N3-03): ended series stay ended, a failing notification
cannot leave a reset half done, every outcome of a request compares the connection epoch."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deye_monitor.config import EnergyResolution
from deye_monitor.device import InverterState
from deye_monitor.transport.base import RequestTimeout
from tests.device_helpers import Recorder, device_regs, ep, install_gate, inverter, rig
from tests.test_device_fix2 import A, B, trackers

# -- N3-01 -----------------------------------------------------------------------------------


def used_g0_and_g1_then_restore_main(tmp_path: Path) -> None:
    (first,) = trackers(tmp_path, "i")
    first.observe(None, A)
    old = (tmp_path / "energy.json").read_bytes()
    first._override = EnergyResolution.TENTH
    first.observe(None, A)
    assert first.series == "g1"
    (tmp_path / "energy.json").write_bytes(old)


def test_main_rollback_same_sn_same_factor_must_not_revive_g0(tmp_path: Path) -> None:
    used_g0_and_g1_then_restore_main(tmp_path)
    (again,) = trackers(tmp_path, "i", override=EnergyResolution.ONE)
    again.observe(None, A)
    assert again.resolution == 1.0
    assert again.series == "g2"


def test_latest_same_context_reservation_can_continue_idempotently(tmp_path: Path) -> None:
    used_g0_and_g1_then_restore_main(tmp_path)
    (again,) = trackers(tmp_path, "i", override=EnergyResolution.TENTH)
    again.observe(None, A)
    assert again.resolution == 0.1 and again.series == "g1"


def test_current_series_continues_across_restarts(tmp_path: Path) -> None:
    (first,) = trackers(tmp_path, "i")
    first.observe(None, A)
    for _ in range(3):
        (again,) = trackers(tmp_path, "i")
        again.observe(None, A)
        assert again.series == "g0" and again.resolution == 1.0


def test_rollback_in_auto_mode_needs_confirmation_then_gets_a_new_series(tmp_path: Path) -> None:
    (first,) = trackers(tmp_path, "i", override=EnergyResolution.AUTO)
    first.observe(0x0105, A)  # 1 kWh, g0
    old = (tmp_path / "energy.json").read_bytes()
    first.observe(0x0104, A)
    first.observe(0x0104, A)  # confirmed change to 0.1: g1
    assert first.series == "g1"
    (tmp_path / "energy.json").write_bytes(old)
    (again,) = trackers(tmp_path, "i", override=EnergyResolution.AUTO)
    again.observe(0x0105, A)
    assert again.resolution is None  # not released on one reading
    again.observe(0x0105, A)
    assert again.resolution == 1.0 and again.series == "g2"


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
        {"ga": device_regs(A), "gb": device_regs(B, r588=55)},
        tmp_path,
        sink=sink,
    ) as r:
        a, b = r.inv("a"), r.inv("b")
        await a.step()
        await b.step()
        proxy = install_gate(r, "b")
        entered, release = proxy.gate(586)
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


# -- N3-03 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("pending", [False, True])
async def test_r65_reconnect_exception_revokes_release_immediately(
    tmp_path: Path, pending: bool
) -> None:
    async with rig([ep("g")], [inverter("i", "g")], {"g": device_regs(A)}, tmp_path) as r:
        inv = r.inv("i")
        await inv.step()
        if pending:
            inv._observe_energy(0x0105, A, inv.clock())
            assert inv.energy.pending and inv.energy.resolution is None
        epoch = inv.lifecycle_epoch
        r.servers["g"].responder.fail_addresses.add(65)
        await inv.route.transport.close()
        await inv._confirm_r65()  # the new connection answers FC03 with exception 2
        assert inv.lifecycle_epoch > epoch and inv.needs_identity
        assert inv.state is InverterState.OFFLINE and inv.energy.resolution is None


async def test_r65_reconnect_transport_error_revokes_release_immediately(
    tmp_path: Path,
) -> None:
    async with rig([ep("g")], [inverter("i", "g")], {"g": device_regs(A)}, tmp_path) as r:
        inv = r.inv("i")
        await inv.step()
        inv._observe_energy(0x0105, A, inv.clock())
        epoch = inv.lifecycle_epoch
        proxy = install_gate(r, "i")

        def fail_after_reopen(_n: int, _start: int) -> None:
            raise RequestTimeout(
                "late"
            )  # the request opened the new connection, then the transport failed

        proxy.after_request.append(fail_after_reopen)
        await proxy.close()
        await inv._confirm_r65()
        assert inv.lifecycle_epoch > epoch and inv.needs_identity
        assert inv.state is InverterState.OFFLINE
