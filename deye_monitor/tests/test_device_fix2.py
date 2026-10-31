"""T7-Fix 2 (Codex verification 2, N2-01..N2-04): energy series recovery, isolated identity reset,
supervisor release and the single "connection not identified" transition."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import EnergyResolution
from deye_monitor.device import InverterState
from deye_monitor.energy_state import (
    SERIES_RE,
    EnergyTracker,
    valid_energy_document,
    valid_generation_document,
)
from deye_monitor.store import JsonStore
from tests.device_helpers import Recorder, device_regs, ep, install_gate, inverter, rig

A = "2401234567"
B = "9999999999"


async def wait_for(predicate: Any, seconds: float = 3.0) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.005)


def trackers(
    tmp_path: Path, *ids: str, override: EnergyResolution = EnergyResolution.ONE, tokens: Any = None
) -> list[EnergyTracker]:
    main = JsonStore(tmp_path / "energy.json", validate=valid_energy_document)
    hist = JsonStore(tmp_path / "energy-generations.json", validate=valid_generation_document)
    kw = {"namespace_source": tokens} if tokens else {}
    return [EnergyTracker(main, i, override, high_water=hist, **kw) for i in ids]


def history(tmp_path: Path, inverter_id: str = "i") -> dict[str, Any]:
    doc = json.loads((tmp_path / "energy-generations.json").read_text())["data"][inverter_id]
    assert isinstance(doc, dict)
    return doc


# -- N2-01 -----------------------------------------------------------------------------------


async def test_missing_main_and_corrupt_history_do_not_reuse_g0(tmp_path: Path) -> None:
    async with rig(
        [ep("g")], [inverter("i", "g", energy_resolution="1")], {"g": device_regs(A)}, tmp_path
    ) as r:
        await r.inv("i").step()
        assert r.inv("i").energy.series == "g0"
    (tmp_path / "energy.json").unlink()
    (tmp_path / "energy-generations.json").write_text("{broken history")
    async with rig(
        [ep("g")], [inverter("i", "g", energy_resolution="0.1")], {"g": device_regs(A)}, tmp_path
    ) as r:
        await r.inv("i").step()
        tracker = r.inv("i").energy
        assert tracker.blocked == "energy_store_corrupt" and tracker.resolution is None
        assert (tmp_path / "energy-generations.json").read_text() == "{broken history"  # untouched
        assert not (tmp_path / "energy.json").exists()


def test_only_both_files_missing_is_a_first_installation(tmp_path: Path) -> None:
    (tracker,) = trackers(tmp_path, "i")
    assert tracker.blocked is None
    tracker.observe(None, A)
    assert tracker.series == "g0" and SERIES_RE.match(tracker.series)


@pytest.mark.parametrize("damaged", ["main", "history"])
def test_either_damaged_file_blocks_every_combination(tmp_path: Path, damaged: str) -> None:
    (first,) = trackers(tmp_path, "i")
    first.observe(None, A)
    (tmp_path / ("energy.json" if damaged == "main" else "energy-generations.json")).write_text("{")
    (second,) = trackers(tmp_path, "i")
    assert second.blocked == "energy_store_corrupt" and second.observe(None, A) == []


def test_inconsistent_history_blocks(tmp_path: Path) -> None:
    (first,) = trackers(tmp_path, "i")
    first.observe(None, A)
    hist = tmp_path / "energy-generations.json"
    for entry in (
        {"namespace": "", "high_water": -1, "pending": None},  # record above the high-water mark
        {"namespace": "abcdefgh", "high_water": 5, "pending": None},  # other namespace
    ):
        hist.write_text(json.dumps({"schema_version": 1, "data": {"i": entry}}))
        (blocked,) = trackers(tmp_path, "i")
        assert blocked.blocked == "energy_history_inconsistent"
        assert blocked.observe(None, A) == []


def test_two_history_loss_resets_get_different_namespaces(tmp_path: Path) -> None:
    series = []
    for _ in range(2):
        (tmp_path / "energy.json").write_text("{lost")
        (tmp_path / "energy-generations.json").write_text("{lost")
        (tracker,) = trackers(tmp_path, "i")
        tracker.reset()
        tracker.observe(None, A)
        series.append(tracker.series)
    assert series[0] != series[1]
    for item in series:
        assert re.fullmatch(r"[a-z0-9]{8}g0", item) and SERIES_RE.match(item)


def test_clock_is_irrelevant_and_a_repeated_token_is_redrawn(tmp_path: Path) -> None:
    draws = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    (tmp_path / "energy.json").write_text("{lost")
    (tmp_path / "energy-generations.json").write_text("{lost")
    (tracker,) = trackers(tmp_path, "i", tokens=lambda: next(draws))
    assert tracker.reset() == "aaaaaaaag0"
    tracker.observe(None, A)
    # history lost again: the source offers the namespace in use first, it is not accepted
    (tmp_path / "energy.json").write_text("{lost")
    (tmp_path / "energy-generations.json").write_text("{lost")
    (again,) = trackers(tmp_path, "i", tokens=lambda: next(draws))
    assert again.reset() in ("aaaaaaaag0", "bbbbbbbbg0")


def test_reset_with_intact_history_continues_the_namespace_above_the_high_water(
    tmp_path: Path,
) -> None:
    (t,) = trackers(tmp_path, "i")
    t.observe(None, A)
    t._override = EnergyResolution.TENTH
    t.observe(None, A)  # generation 1
    assert t.series == "g1"
    (tmp_path / "energy.json").write_text("{lost")
    (t2,) = trackers(tmp_path, "i")
    assert t2.blocked == "energy_store_corrupt"
    assert t2.reset() == "g2"  # same namespace, above everything used
    assert history(tmp_path)["high_water"] == 1


def test_reset_repeated_after_a_crash_keeps_the_token(tmp_path: Path) -> None:
    (tmp_path / "energy.json").write_text("{lost")
    (tmp_path / "energy-generations.json").write_text("{lost")
    (t,) = trackers(tmp_path, "i")
    real = t._store.commit

    def crash(_doc: dict[str, Any]) -> None:
        raise OSError("crash between the two commits")

    t._store.commit = crash  # type: ignore[method-assign,assignment]
    t._store.reset = crash  # type: ignore[method-assign,assignment]
    with pytest.raises(OSError):
        t.reset()  # history written, record store not
    t._store.commit = real  # type: ignore[method-assign]
    token = history(tmp_path)["namespace"]
    (t2,) = trackers(tmp_path, "i")  # main still corrupt
    assert t2.reset() == f"{token}g0"  # the same namespace, no second token


def test_record_rolled_back_below_the_high_water_never_reuses_a_generation(tmp_path: Path) -> None:
    (t,) = trackers(tmp_path, "i")
    t.observe(None, A)
    old = (tmp_path / "energy.json").read_bytes()
    t._override = EnergyResolution.TENTH
    t.observe(None, A)
    assert t.series == "g1"
    (tmp_path / "energy.json").write_bytes(old)  # valid, but older than the history
    (t2,) = trackers(tmp_path, "i")
    assert t2.blocked is None and t2.series == "g0"
    t2.observe(None, B)  # a new inverter at the old factor
    assert t2.series == "g2"  # above the high-water mark 1, not g1 again


def test_crash_between_history_and_record_continues_with_the_same_generation(
    tmp_path: Path,
) -> None:
    (t,) = trackers(tmp_path, "i")
    t.observe(None, A)
    t._override = EnergyResolution.TENTH
    t._store.commit = lambda _d: (_ for _ in ()).throw(OSError("crash"))  # type: ignore[method-assign,assignment]
    t.observe(None, A)  # history says generation 1 (reserved), the record stays at 0
    assert t.blocked == "energy_store_unwritable"
    assert history(tmp_path)["high_water"] == 1 and history(tmp_path)["pending"]["generation"] == 1
    (t2,) = trackers(tmp_path, "i", override=EnergyResolution.TENTH)
    assert t2.series == "g0" and t2.blocked is None
    t2.observe(None, A)
    assert t2.series == "g1"  # the reserved generation, not g2
    (t3,) = trackers(tmp_path, "i", override=EnergyResolution.TENTH)
    t3.observe(None, A)
    assert t3.series == "g1" and history(tmp_path)["high_water"] == 1


def test_crash_before_the_history_commit_loses_nothing(tmp_path: Path) -> None:
    (t,) = trackers(tmp_path, "i")
    t.observe(None, A)
    t._override = EnergyResolution.TENTH
    t._hw.commit = lambda _d: (_ for _ in ()).throw(OSError("crash"))  # type: ignore[union-attr,method-assign]
    t.observe(None, A)
    assert t.blocked == "energy_store_unwritable"
    (t2,) = trackers(tmp_path, "i", override=EnergyResolution.TENTH)
    t2.observe(None, A)
    assert t2.series == "g1"


def test_several_inverters_keep_separate_series(tmp_path: Path) -> None:
    a, b = trackers(tmp_path, "a", "b")
    a.observe(None, A)
    b.observe(None, B)
    a._override = EnergyResolution.TENTH
    a.observe(None, A)
    assert (a.series, b.series) == ("g1", "g0")
    (tmp_path / "energy.json").write_text("{lost")
    a2, b2 = trackers(tmp_path, "a", "b")
    b2.reset()  # b's record is gone with the file; a's history stays intact
    a2.reset()
    assert history(tmp_path, "a")["namespace"] == "" and history(tmp_path, "b")["namespace"] == ""
    assert (a2.series, b2.series) == ("g2", "g1")


def test_legacy_integer_history_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "energy-generations.json").write_text(
        json.dumps({"schema_version": 1, "data": {"i": 4}})
    )
    (t,) = trackers(tmp_path, "i")
    assert t.blocked is None
    t.observe(None, A)
    assert t.series == "g5"


async def test_device_layer_delivers_the_series_to_the_sink(tmp_path: Path) -> None:
    async with rig(
        [ep("g")], [inverter("i", "g", energy_resolution="1")], {"g": device_regs(A)}, tmp_path
    ) as r:
        await r.inv("i").step()
        snaps = [s for who, s, _ in r.sink.states if who == "i" and s.group == "energy"]
        assert snaps and snaps[-1].energy_series == "g0" and snaps[-1].energy_generation == 0


# -- N2-02 -----------------------------------------------------------------------------------


async def two_inverters(tmp_path: Path) -> Any:
    return rig(
        [ep("ga", port=25001), ep("gb", port=25002)],
        [inverter("a", "ga"), inverter("b", "gb")],
        {"ga": device_regs(A), "gb": device_regs(B, soc=55)},
        tmp_path,
    )


async def block_a(r: Any) -> None:
    store = r.runtime.book._store
    original = store.commit

    def deny(_doc: Any) -> None:
        raise OSError("disk full while binding A")

    store.commit = deny
    try:
        await r.inv("a").step()
    finally:
        store.commit = original
    assert r.inv("a").state is InverterState.BLOCKED


async def test_shared_identity_reset_invalidates_running_peer(tmp_path: Path) -> None:
    async with await two_inverters(tmp_path) as r:
        a, b = r.inv("a"), r.inv("b")
        await b.step()
        await block_a(r)
        assert b.state is InverterState.RUNNING
        gate = install_gate(r, "b")
        entered, release = gate.gate(586)
        old_epoch, before = b.lifecycle_epoch, len(r.sink.states)
        task = asyncio.create_task(b._run_group("battery"))
        await asyncio.wait_for(entered.wait(), 1)
        r.runtime.reset_store(None, "identity")  # global: the shared file is gone
        release.set()
        await task
        assert b.lifecycle_epoch > old_epoch
        assert not [v for who, _, v in r.sink.states[before:] if who == "b"]  # nothing published
        assert b.state is not InverterState.RUNNING and b.needs_identity
        assert ("b", InverterState.OFFLINE, "store reset") in r.sink.availability
        assert a.state is InverterState.STARTING


async def test_isolated_identity_reset_leaves_the_neighbour_untouched(tmp_path: Path) -> None:
    async with await two_inverters(tmp_path) as r:
        a, b = r.inv("a"), r.inv("b")
        await a.step()
        await b.step()
        assert r.runtime.book.bound("a") == A and r.runtime.book.bound("b") == B
        epoch, avail = b.lifecycle_epoch, len(r.sink.availability)
        r.runtime.reset_store("a", "identity")
        assert r.runtime.book.bound("a") is None and r.runtime.book.bound("b") == B
        assert b.lifecycle_epoch == epoch and b.state is InverterState.RUNNING
        assert not [x for x in r.sink.availability[avail:] if x[0] == "b"]
        assert a.state is InverterState.STARTING and a.lifecycle_epoch > 0
        await a.step()
        assert r.runtime.book.bound("a") == A  # bound again by the first valid serial


async def test_isolated_reset_with_expected_serial_reapplies_the_option(tmp_path: Path) -> None:
    async with rig(
        [ep("g")],
        [inverter("i", "g", expected_serial=A)],
        {"g": device_regs(A)},
        tmp_path,
    ) as r:
        await r.inv("i").step()
        r.runtime.reset_store("i", "identity")
        assert r.runtime.book.bound("i") == A and r.runtime.book.source("i") == "expected"


async def test_isolated_reset_after_unwritable_file_unblocks_everyone(tmp_path: Path) -> None:
    async with await two_inverters(tmp_path) as r:
        await block_a(r)
        r.runtime.reset_store("a", "identity")
        assert r.inv("a").state is InverterState.STARTING
        await r.inv("a").step()
        assert r.runtime.book.bound("a") == A


# -- N2-03 -----------------------------------------------------------------------------------


class BlockAnnouncementError(Recorder):
    raised = False

    def on_availability(self, who: str, state: InverterState, reason: str) -> None:
        super().on_availability(who, state, reason)
        if state is InverterState.BLOCKED and not self.raised:
            self.raised = True
            raise RuntimeError("unexpected sink error while blocked")


async def test_reset_revives_supervisor_that_waited_while_blocked(tmp_path: Path) -> None:
    (tmp_path / "identity.json").write_text("{broken")
    async with rig(
        [ep("g")], [inverter("i", "g")], {"g": device_regs(A, soc=55)}, tmp_path,
        sink=BlockAnnouncementError(),
    ) as r:  # fmt: skip
        await r.runtime.start()
        await wait_for(
            lambda: r.inv("i").state is InverterState.BLOCKED and r.inv("i").internal_errors >= 1
        )
        await asyncio.sleep(0.03)
        assert len(r.runtime._tasks) == 1 and not r.runtime._tasks[0].done()  # waiting, no reads
        assert not r.sink.states
        r.runtime.reset_store(None, "identity")
        await wait_for(lambda: bool(r.sink.states))
        assert len(r.runtime._tasks) == 1  # still the one supervisor: no second loop
        assert r.runtime.book.bound("i") == A
        # an error and a reset once more: it keeps working, shutdown stays clean
        r.inv("i").mark_internal_error(RuntimeError("again"))
        await r.runtime.stop()


# -- N2-04 -----------------------------------------------------------------------------------


async def test_reconnect_immediately_invalidates_runtime_release(tmp_path: Path) -> None:
    async with rig([ep("g")], [inverter("i", "g")], {"g": device_regs(A)}, tmp_path) as r:
        inv = r.inv("i")
        await inv.step()
        assert inv.state is InverterState.RUNNING and inv.energy.resolution is not None
        old_epoch, before = inv.lifecycle_epoch, len(r.sink.states)
        await inv.route.transport.close()
        await inv._run_group("battery")  # the connection reopens, the result is discarded
        assert inv.needs_identity and len(r.sink.states) == before
        assert inv.energy.resolution is None and inv.lifecycle_epoch > old_epoch
        assert inv.state is InverterState.OFFLINE
        assert r.sink.availability[-1] == ("i", InverterState.OFFLINE, "connection changed")
        await inv.step()  # immediate identification, online again only after data release
        assert inv.state is InverterState.RUNNING and not inv.needs_identity


async def test_reconnect_during_the_confirming_read_uses_the_same_transition(
    tmp_path: Path,
) -> None:
    async with rig([ep("g")], [inverter("i", "g")], {"g": device_regs(A)}, tmp_path) as r:
        inv = r.inv("i")
        await inv.step()
        epoch = inv.lifecycle_epoch
        await inv.route.transport.close()
        await inv._confirm_r65()
        assert inv.lifecycle_epoch > epoch and inv.needs_identity
        assert inv.state is InverterState.OFFLINE


async def test_reconnect_by_a_peer_on_a_shared_endpoint_invalidates_both(tmp_path: Path) -> None:
    async with rig(
        [ep("g")],
        [inverter("a", "g"), {**inverter("b", "g"), "unit_id": 2}],
        {"g": device_regs(A)},
        tmp_path,
    ) as r:
        r.servers["g"].responder.any_unit = True  # a gateway in front of both units
        a, b = r.inv("a"), r.inv("b")
        await a.step()
        await b.step()
        ea, eb = a.lifecycle_epoch, b.lifecycle_epoch
        await a.route.transport.close()  # the shared connection is reopened by whoever reads next
        await a._run_group("battery")
        await b.step()  # b notices the new connection epoch before its next read
        assert a.lifecycle_epoch > ea and b.lifecycle_epoch > eb
        assert a.needs_identity  # a identifies on its next step
        assert b.state is InverterState.RUNNING and not b.needs_identity  # b did it at once
        assert ("b", InverterState.OFFLINE, "connection changed") in r.sink.availability


async def test_reconnect_inside_the_identification_cycle_restarts_it(tmp_path: Path) -> None:
    async with rig([ep("g")], [inverter("i", "g")], {"g": device_regs(A)}, tmp_path) as r:
        inv = r.inv("i")
        proxy = install_gate(r, "i")
        proxy.after_request.append(
            lambda n, start: n == 1 and asyncio.get_running_loop().create_task(proxy.close())
        )
        await inv.step()
        assert inv.state is not InverterState.RUNNING or inv.lifecycle_epoch > 0
