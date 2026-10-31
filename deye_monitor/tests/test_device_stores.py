"""Fail-closed persistence (T7-03): damaged critical stores never lead to a silent re-binding or
to a reused statistics generation."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import EnergyResolution
from deye_monitor.device import InverterState
from deye_monitor.energy_state import EnergyTracker, valid_energy_document
from deye_monitor.identity import IdentityBook, identity_store
from deye_monitor.store import JsonStore, LoadStatus
from tests.device_helpers import device_regs, ep, install_gate, inverter, rig, state_of

A = "2401234567"
B = "9999999999"


def write(path: Path, text: str) -> bytes:
    path.write_text(text)
    return path.read_bytes()


# -- JsonStore -------------------------------------------------------------------------------------


def test_store_distinguishes_missing_valid_and_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    assert JsonStore(path).status is LoadStatus.MISSING
    store = JsonStore(path)
    store.commit({"k": 1})
    assert JsonStore(path).status is LoadStatus.VALID and JsonStore(path).data == {"k": 1}
    for text, problem in (
        ("{not json", "invalid JSON"),
        ("[]", "unknown schema"),
        (json.dumps({"schema_version": 99, "data": {}}), "unknown schema"),
        (json.dumps({"schema_version": 1, "data": []}), "invalid content"),
        (json.dumps({"schema_version": 1}), "invalid content"),
    ):
        path.write_text(text)
        bad = JsonStore(path)
        assert (bad.status, bad.problem, bad.data) == (LoadStatus.CORRUPT, problem, {})
    path.write_bytes(b"\xff\xfe\x00")
    assert JsonStore(path).corrupt
    path.write_text(json.dumps({"schema_version": 1, "data": {"x": 1}}))
    assert JsonStore(
        path, validate=lambda d: "x" not in d
    ).corrupt  # a validator can reject content


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not apply to root")
def test_unreadable_store_is_corrupt_not_empty(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    JsonStore(path).commit({"k": 1})
    path.chmod(0)
    try:
        assert JsonStore(path).corrupt
    finally:
        path.chmod(0o600)


def test_failed_write_keeps_memory_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonStore(tmp_path / "s.json")
    store.commit({"k": 1})
    before = (tmp_path / "s.json").read_bytes()
    with monkeypatch.context() as m:
        m.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
        with pytest.raises(PermissionError):
            store.commit({"k": 2})
    assert store.data == {"k": 1} and (tmp_path / "s.json").read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]  # no temporary files


def test_directory_is_synced_after_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    JsonStore(tmp_path / "s.json").commit({"k": 1})
    assert len(calls) == 2  # the file and its directory


def test_reset_keeps_a_backup_of_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    original = write(path, "{broken")
    store = JsonStore(path)
    assert store.corrupt
    store.reset({"fresh": True})
    assert store.status is LoadStatus.VALID and JsonStore(path).data == {"fresh": True}
    assert (tmp_path / "s.json.corrupt").read_bytes() == original


# -- identity ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"schema_version": 99, "data": {"bindings": {}}}),
        json.dumps({"schema_version": 1, "data": {"bindings": []}}),
        json.dumps(
            {
                "schema_version": 1,
                "data": {"bindings": {"deye1": {"serial": "short", "source": "first_seen"}}},
            }
        ),
        json.dumps(
            {"schema_version": 1, "data": {"bindings": {"deye1": {"serial": A, "source": "magic"}}}}
        ),
        json.dumps({"schema_version": 1, "data": {"bindings": {"deye1": {"serial": A}}}}),
    ],
)
async def test_corrupt_identity_never_rebinds_without_user_action(
    tmp_path: Path, content: str
) -> None:
    original = write(tmp_path / "identity.json", content)
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(B, soc=88)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        proxy = install_gate(r, "deye1")
        for _ in range(3):
            await inv.step()
        assert state_of(inv) is InverterState.BLOCKED
        assert inv.reason == "identity_store_corrupt"
        assert r.runtime.book.bound("deye1") is None  # the foreign device was not adopted
        assert not r.sink.states and proxy.requests == []  # no data, not even an identity read
        assert (tmp_path / "identity.json").read_bytes() == original  # the evidence is untouched
        assert r.sink.events("identity_store_corrupt")
        assert r.sink.availability[-1][1] is InverterState.BLOCKED


async def test_identity_reset_is_explicit_and_keeps_the_old_file(tmp_path: Path) -> None:
    original = write(tmp_path / "identity.json", "{broken")
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(B, soc=88)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.BLOCKED
        with pytest.raises(ValueError, match="global"):
            r.runtime.reset_store("deye1", "identity")  # a damaged file can only be reset globally
        r.runtime.reset_store(None, "identity")
        assert (tmp_path / "identity.json.corrupt").read_bytes() == original
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING
        assert (
            r.runtime.book.bound("deye1") == B
        )  # bound by the user's explicit reset, not silently
        with pytest.raises(ValueError):
            r.runtime.reset_store("deye1", "nonsense")


async def test_unwritable_identity_store_blocks_without_a_retry_loop(tmp_path: Path) -> None:
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A)}, tmp_path) as r:

        def deny(doc: dict[str, Any]) -> None:
            raise PermissionError("read-only file system")

        r.runtime.book._store.commit = deny  # type: ignore[method-assign,assignment]
        inv = r.inv("deye1")
        proxy = install_gate(r, "deye1")
        await inv.step()
        assert state_of(inv) is InverterState.BLOCKED and inv.reason == "identity_store_unwritable"
        assert r.runtime.book.bound("deye1") is None  # the in-memory binding follows the file
        count = len(proxy.requests)
        for _ in range(3):
            assert await inv.step() == 60.0  # no retry loop, no reads
        assert len(proxy.requests) == count and not r.sink.states


async def test_expected_serial_write_failure_blocks_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = JsonStore.commit

    def deny(self: JsonStore, new: dict[str, Any]) -> None:
        if self.path.name == "identity.json":
            raise PermissionError("denied")
        real(self, new)

    monkeypatch.setattr(JsonStore, "commit", deny)
    cfg_inv = inverter("deye1", "gw1", expected_serial=A)
    async with rig([ep("gw1")], [cfg_inv], {"gw1": device_regs(A)}, tmp_path) as r:
        await r.inv("deye1").step()
        assert state_of(r.inv("deye1")) is InverterState.BLOCKED
        assert not r.sink.states


def test_identity_book_validation_and_reset(tmp_path: Path) -> None:
    write(tmp_path / "identity.json", "{broken")
    book = IdentityBook(identity_store(tmp_path / "identity.json"), {"a": A})
    assert book.problem == "identity_store_corrupt" and book.bound("a") is None
    assert book.rebind("a") is False
    with pytest.raises(ValueError):
        book.bind_first_seen("b", B)
    book.reset()
    assert book.problem is None and book.bound("a") == A  # expected_serial is applied again


# -- energy ---------------------------------------------------------------------------


def energy_doc(tmp_path: Path) -> dict[str, Any]:
    return dict(json.loads((tmp_path / "energy.json").read_text())["data"]["deye1"])


async def test_corrupt_energy_blocks_publication_and_never_restarts_at_generation_zero(
    tmp_path: Path,
) -> None:
    inv_cfg = inverter("deye1", "gw1", energy_resolution="0.1")
    regs = device_regs(A)
    async with rig([ep("gw1")], [inv_cfg], {"gw1": regs}, tmp_path) as r:
        await r.inv("deye1").step()
    changed = inverter("deye1", "gw1", energy_resolution="1")
    async with rig([ep("gw1")], [changed], {"gw1": regs}, tmp_path) as r:
        await r.inv("deye1").step()
    assert energy_doc(tmp_path)["generation"] == 1
    original = write(tmp_path / "energy.json", "{broken")
    async with rig([ep("gw1")], [changed], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.clock.offset += 61
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING  # identity and the other values keep working
        assert set(r.sink.values("deye1", "battery_energy_charge_today")) == {None}
        assert inv.energy.blocked == "energy_store_corrupt" and r.sink.events(
            "energy_store_corrupt"
        )
        assert (tmp_path / "energy.json").read_bytes() == original  # not re-initialised as _g0
        assert r.sink.values("deye1", "battery_soc")[-1] is not None
        # the explicit reset opens a NEW series above everything seen before
        r.runtime.reset_store("deye1", "energy")
        await inv.step()
        assert inv.energy.blocked is None
        assert energy_doc(tmp_path)["generation"] == 2  # high-water 1 + 1, never 0 or 1 again
        assert (tmp_path / "energy.json.corrupt").read_bytes() == original
        assert r.sink.values("deye1", "battery_energy_charge_today")[-1] is not None


async def test_energy_reset_without_any_history_starts_a_random_namespace(
    tmp_path: Path,
) -> None:
    changed = inverter("deye1", "gw1", energy_resolution="1")
    regs = device_regs(A)
    async with rig([ep("gw1")], [changed], {"gw1": regs}, tmp_path) as r:
        await r.inv("deye1").step()
    write(tmp_path / "energy.json", "{broken")
    write(tmp_path / "energy-generations.json", "{broken too")
    async with rig([ep("gw1")], [changed], {"gw1": regs}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert inv.energy.blocked == "energy_store_corrupt"
        r.runtime.reset_store("deye1", "energy")
        await inv.step()
        record = energy_doc(tmp_path)
        namespace = record["namespace"]
        assert re.fullmatch(r"[a-z0-9]{8}", namespace) and record["generation"] == 0
        assert inv.energy.series == f"{namespace}g0"
        history = json.loads((tmp_path / "energy-generations.json").read_text())["data"]["deye1"]
        assert history["namespace"] == namespace and history["high_water"] == 0
        # the token is persisted before release: a restart keeps the very same series
    async with rig([ep("gw1")], [changed], {"gw1": regs}, tmp_path) as r:
        await r.inv("deye1").step()
        assert r.inv("deye1").energy.series == f"{namespace}g0"


async def test_unwritable_energy_store_blocks_energy_only(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1", energy_resolution="1")],
        {"gw1": device_regs(A)},
        tmp_path,
    ) as r:

        def deny(doc: dict[str, Any]) -> None:
            raise PermissionError("read-only")

        r.runtime._energy.commit = deny  # type: ignore[method-assign,assignment]
        inv = r.inv("deye1")
        await inv.step()
        r.clock.offset += 61
        await inv.step()
        assert inv.energy.blocked == "energy_store_unwritable"
        assert state_of(inv) is InverterState.RUNNING
        assert set(r.sink.values("deye1", "battery_energy_charge_today")) == {None}


def test_energy_documents_are_type_checked() -> None:
    good = {"effective_resolution": 0.1, "source": "r65", "generation": 0, "serial": A}
    assert valid_energy_document({"deye1": good})
    for bad in (
        {**good, "effective_resolution": 0.5},
        {**good, "effective_resolution": True},
        {**good, "source": "guess"},
        {**good, "generation": -1},
        {**good, "generation": True},
        {**good, "generation": 1.5},
        {**good, "serial": "x"},
        {**good, "extra": 1},
        "not a record",
    ):
        assert not valid_energy_document({"deye1": bad})
    assert valid_energy_document({"deye1": {k: v for k, v in good.items() if k != "serial"}})


def test_tracker_without_high_water_and_blocked_store(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "e.json", validate=valid_energy_document)
    tracker = EnergyTracker(store, "a", EnergyResolution.AUTO)
    assert tracker.blocked is None and tracker.observe(0x0104, A)[0].kind == "initialised"
    write(tmp_path / "bad.json", "{x")
    blocked = EnergyTracker(JsonStore(tmp_path / "bad.json"), "a", EnergyResolution.AUTO)
    assert blocked.blocked == "energy_store_corrupt"
    assert blocked.observe(0x0104, A) == [] and blocked.resolution is None


# -- capability cache (a cache: rebuilt, never fatal) ------------------------------------


async def test_capability_store_failure_is_a_diagnostic_not_a_crash(tmp_path: Path) -> None:
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A)}, tmp_path) as r:

        def deny(doc: dict[str, Any]) -> None:
            raise OSError("disk full")

        r.runtime._capabilities.commit = deny  # type: ignore[method-assign,assignment]
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING and r.sink.states
        assert r.sink.events("capability_store_unwritable")


async def test_corrupt_capability_cache_is_rebuilt_with_a_diagnostic(tmp_path: Path) -> None:
    write(tmp_path / "capabilities.json", "{broken")
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A)}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING
        assert r.sink.events("capability_store_rebuilt")
        assert (
            json.loads((tmp_path / "capabilities.json").read_text())["data"]["deye1"]["key"][0] == A
        )
