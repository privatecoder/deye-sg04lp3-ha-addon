"""Connection epoch (T7-01) and lifecycle token (T7-02): nothing of a stale or foreign cycle is
bound, persisted or published."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.device import InverterState
from tests.device_helpers import (
    Rig,
    device_regs,
    ep,
    install_gate,
    inverter,
    rig,
    serial_words,
    state_of,
)

A = "2401234567"
B = "9999999999"


def identity_addresses(r: Rig, inverter_id: str) -> list[list[int]]:
    plan = r.inv(inverter_id).current_plan()
    return [list(b.needed) for b in plan.blocks if b.group == "identity"]


def sn_block_count(blocks: list[list[int]]) -> int:
    """Number of identity requests up to and including the last serial-number block."""
    return max(i for i, needed in enumerate(blocks) if set(needed) & set(range(3, 8))) + 1


async def foreign_device_after_serial_block(r: Rig, inverter_id: str, exceptions_only: bool) -> int:
    """Arm: after the request that answered R3..R7 the connection is dropped (as a failing
    neighbour or a cancellation would) and the fake becomes device B. Returns the request count
    of the identity cycle at which this happens."""
    blocks = identity_addresses(r, inverter_id)
    trigger = sn_block_count(blocks)
    remaining = {a for needed in blocks[trigger:] for a in needed}
    proxy = install_gate(r, inverter_id)
    base = len(proxy.requests)

    def hook(n: int, start: int) -> None:
        if n == base + trigger:
            r.servers["gw1"].responder.regs.update(device_regs(B, r588=88))
            if exceptions_only:
                r.servers["gw1"].responder.fail_addresses = remaining
            proxy.inner._drop()  # the next request opens a new connection

    proxy.after_request.append(hook)
    return trigger


@pytest.mark.parametrize("exceptions_only", [True, False])
@pytest.mark.parametrize("max_read", [60, 6])
async def test_reconnect_inside_the_identity_cycle_never_releases_foreign_data(
    tmp_path: Path, exceptions_only: bool, max_read: int
) -> None:
    endpoints = [ep("gw1", max_read_registers=max_read)]
    async with rig(
        endpoints, [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING
        published = len(r.sink.states)
        await foreign_device_after_serial_block(r, "deye1", exceptions_only)
        r.clock.offset += 4000  # identity refresh
        await inv.step()
        assert len(r.sink.states) == published  # nothing of the mixed cycle was released
        assert r.runtime.book.bound("deye1") == A and inv.seen_serial == A
        assert inv.needs_identity  # the serial number must be read completely on the new connection
        for _ in range(3):
            await inv.step()
        assert state_of(inv) is InverterState.QUARANTINED  # B is read as B, never accepted as A
        assert 88 not in r.sink.values("deye1", "battery_soc")
        assert not [s for _, s, _ in r.sink.states[published:]]


async def test_same_device_after_a_reconnect_is_accepted_after_a_full_reread(
    tmp_path: Path,
) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        proxy = install_gate(r, "deye1")
        proxy.inner._drop()
        r.clock.offset += 11
        before = len(r.sink.states)
        await inv.step()  # data cycle on a new connection: discarded
        assert len(r.sink.states) == before and inv.needs_identity
        for _ in range(3):
            await inv.step()
        assert state_of(inv) is InverterState.RUNNING
        assert r.sink.values("deye1", "battery_soc")[-1] == 55  # data resumes after the re-read


async def test_a_reconnect_caused_by_another_inverter_on_a_shared_endpoint(tmp_path: Path) -> None:
    endpoints = [ep("gw1")]
    inverters = [inverter("deye1", "gw1"), {**inverter("deye2", "gw1"), "unit_id": 2}]
    async with rig(endpoints, inverters, {"gw1": device_regs(A, r588=55)}, tmp_path) as r:
        r.servers["gw1"].responder.any_unit = True
        await r.inv("deye1").step()
        await r.inv("deye2").step()
        published = len(r.sink.values("deye1", "battery_soc"))
        r.runtime.transports["gw1"]._drop()  # e.g. a failed request of the neighbour
        r.servers["gw1"].responder.regs.update(
            device_regs(B, r588=88)
        )  # a different device answers
        await r.inv("deye2").step()  # deye2 reconnects and re-identifies: it sees B
        r.clock.offset += 11
        await r.inv("deye1").step()  # its first request now runs on the new connection
        assert len(r.sink.values("deye1", "battery_soc")) == published  # discarded
        for _ in range(3):
            await r.inv("deye1").step()
        assert state_of(r.inv("deye1")) is InverterState.QUARANTINED
        assert 88 not in r.sink.values("deye1", "battery_soc")
        assert 88 not in r.sink.values("deye2", "battery_soc")


# -- T7-02: lifecycle token ---------------------------------------------------------------------


async def running(r: Rig) -> Any:
    inv = r.inv("deye1")
    await inv.step()
    assert state_of(inv) is InverterState.RUNNING
    r.clock.offset += 11
    return inv


async def test_selection_revision_invalidates_inflight_group(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = await running(r)
        proxy = install_gate(r, "deye1")
        entered, release = proxy.gate(586)  # the battery group is in flight under the old plan
        task = asyncio.create_task(inv.step())
        await asyncio.wait_for(entered.wait(), 2)
        before = len(r.sink.states)
        inv.set_selection({"battery_soc"}, revision=7)
        release.set()
        await task
        assert not [s for _, s, _ in r.sink.states[before:] if s.selection_revision != 7]
        assert all(set(v) <= {"battery_soc"} for _, _, v in r.sink.states[before:])
        proxy.gates.clear()
        for _ in range(3):
            await inv.step()
            r.clock.offset += 11
        fresh = [(s, v) for _, s, v in r.sink.states[before:] if s.group == "battery"]
        assert fresh and all(
            s.selection_revision == 7 and set(v) == {"battery_soc"} for s, v in fresh
        )
        assert all(s.lifecycle_epoch >= 1 for s, _ in fresh)


async def test_deselect_and_reselect_does_not_revive_an_old_answer(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = await running(r)
        old = inv.selection
        proxy = install_gate(r, "deye1")
        entered, release = proxy.gate(586)
        task = asyncio.create_task(inv.step())
        await asyncio.wait_for(entered.wait(), 2)
        before = len(r.sink.states)
        inv.set_selection(old - {"battery_soc"}, revision=1)
        inv.set_selection(old, revision=2)  # the same keys again: still a new revision
        release.set()
        await task
        assert not [
            s
            for _, s, _ in r.sink.states[before:]
            if s.group == "battery" and s.selection_revision < 2
        ]


async def test_rebind_invalidates_inflight_identity(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = await running(r)
        proxy = install_gate(r, "deye1")
        entered, release = proxy.gate(3)  # the answer of device A is held back
        r.clock.offset += 4000
        task = asyncio.create_task(inv.step())
        await asyncio.wait_for(entered.wait(), 2)
        r.servers["gw1"].responder.regs.update(device_regs(B, r588=88))
        assert r.runtime.rebind("deye1") is True
        assert r.sink.availability[-1] == ("deye1", InverterState.OFFLINE, "rebind requested")
        release.set()
        await task
        assert r.runtime.book.bound("deye1") is None  # the old answer did not bind A again
        assert state_of(inv) is not InverterState.RUNNING
        proxy.gates.clear()
        await inv.step()
        assert r.runtime.book.bound("deye1") == B  # the explicit re-binding wins
        assert state_of(inv) is InverterState.RUNNING


async def test_rebind_during_a_data_read_publishes_nothing_of_the_old_cycle(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = await running(r)
        proxy = install_gate(r, "deye1")
        entered, release = proxy.gate(586)
        task = asyncio.create_task(inv.step())
        await asyncio.wait_for(entered.wait(), 2)
        before = len(r.sink.states)
        inv.rebind()
        release.set()
        await task
        assert len(r.sink.states) == before
        assert r.sink.availability[-1][1] is InverterState.OFFLINE


async def test_selection_change_during_failback_does_not_publish_stale_results(
    tmp_path: Path,
) -> None:
    endpoints = [ep("gw1"), ep("logger1", "solarman_v5_tcp")]
    inverters = [inverter("deye1", "gw1", fallback_endpoint="logger1")]
    banks = {"gw1": device_regs(A, r588=55), "logger1": device_regs(A, r588=66)}
    async with rig(endpoints, inverters, banks, tmp_path) as r:
        inv = await running(r)
        before = len(r.sink.states)
        inv.set_selection({"battery_soc"}, revision=3)
        assert inv.selection_revision == 3 and inv.lifecycle_epoch >= 1
        for _ in range(3):
            await inv.step()
            r.clock.offset += 11
        assert all(s.selection_revision == 3 for _, s, _ in r.sink.states[before:])


async def test_snapshot_carries_the_immutable_publisher_context(tmp_path: Path) -> None:
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A)}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        snap = r.sink.states[-1][1]
        assert (snap.selection_revision, snap.lifecycle_epoch, snap.energy_generation) == (0, 0, 0)
        assert snap.failed is False
        with pytest.raises(AttributeError):
            snap.lifecycle_epoch = 9  # type: ignore[misc]
        inv.rebind()
        await inv.step()
        later = r.sink.states[-1][1]
        assert later.lifecycle_epoch > snap.lifecycle_epoch  # T8 can reject what was queued before
        assert snap.lifecycle_epoch == 0  # an emitted snapshot never changes afterwards
        assert "lifecycle_epoch" in inv.diagnostics()


def test_unused_helper_keeps_imports_honest() -> None:
    assert serial_words(A)[3] == 0x3234
