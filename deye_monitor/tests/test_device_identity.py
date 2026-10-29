"""Identity binding, quarantine, rebind (SPEC §8.1 SR-05/SR-09)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.device import InverterState
from deye_monitor.identity import IdentityBook, normalize_serial
from deye_monitor.store import JsonStore
from tests.device_helpers import device_regs, ep, inverter, rig, serial_words, state_of

SN = "2401234567"
OTHER = "9999999999"


def gw(**kw: Any) -> list[dict[str, Any]]:
    return [ep("gw1", **kw)]


async def test_first_serial_is_bound_and_persisted(tmp_path: Path) -> None:
    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING
        assert r.runtime.book.bound("deye1") == SN
        assert r.runtime.book.source("deye1") == "first_seen"
        data = json.loads((tmp_path / "identity.json").read_text())
        assert data["data"]["bindings"]["deye1"] == {"serial": SN, "source": "first_seen"}
        assert any(v.get("event") == "bound" for _, v in r.sink.diagnostics)


async def test_binding_survives_a_restart_and_rejects_another_device(tmp_path: Path) -> None:
    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path) as r:
        await r.inv("deye1").step()
    # restart with the same data directory: a different device now answers
    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": device_regs(OTHER)}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.QUARANTINED
        assert r.runtime.book.bound("deye1") == SN  # never adopted
        assert not [s for s in r.sink.states if s[0] == "deye1"]  # no operating data at all
        assert ("deye1", InverterState.QUARANTINED) in [(i, s) for i, s, _ in r.sink.availability]
    # and the original device is accepted again
    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path) as r:
        await r.inv("deye1").step()
        assert state_of(r.inv("deye1")) is InverterState.RUNNING


async def test_expected_serial_takes_precedence(tmp_path: Path) -> None:
    inv_cfg = inverter("deye1", "gw1", expected_serial=OTHER)
    async with rig(gw(), [inv_cfg], {"gw1": device_regs(SN)}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.QUARANTINED
        assert r.runtime.book.source("deye1") == "expected"
        assert r.runtime.book.bound("deye1") == OTHER
        assert not r.sink.states
        assert inv.rebind() is False  # only the option can change an expected serial
    async with rig(
        gw(), [inverter("deye1", "gw1", expected_serial=SN)], {"gw1": device_regs(SN)}, tmp_path
    ) as r:
        await r.inv("deye1").step()
        assert state_of(r.inv("deye1")) is InverterState.RUNNING  # the changed option re-binds
        assert r.runtime.book.bound("deye1") == SN


async def test_missing_or_invalid_serial_never_binds(tmp_path: Path) -> None:
    bad = device_regs(SN)
    bad[5] = 0x0001  # not printable
    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": bad}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.OFFLINE
        assert "serial" in inv.reason
        assert r.runtime.book.bound("deye1") is None
        assert not r.sink.states
    blank = device_regs(SN)
    blank.update(dict.fromkeys(range(3, 8), 0x2020))  # ten spaces
    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": blank}, tmp_path / "b") as r:
        await r.inv("deye1").step()
        assert state_of(r.inv("deye1")) is InverterState.OFFLINE
        assert r.runtime.book.bound("deye1") is None


async def test_no_answer_is_offline_not_a_binding(tmp_path: Path) -> None:
    from tests.fakes.servers import Behavior

    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path) as r:
        r.servers["gw1"].default = Behavior(silent=True)
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.OFFLINE
        assert r.runtime.book.bound("deye1") is None


async def test_device_swap_at_the_primary_path_quarantines(tmp_path: Path) -> None:
    async with rig(
        gw(), [inverter("deye1", "gw1")], {"gw1": device_regs(SN, r588=55)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING
        published = len(r.sink.states)
        assert published > 1
        r.servers["gw1"].responder.regs.update(serial_words(OTHER))  # someone swapped the device
        r.clock.offset += 4000  # identity refresh is due (poll_identity_s = 3600)
        await inv.step()
        assert state_of(inv) is InverterState.QUARANTINED
        after = len(r.sink.states)
        r.clock.offset += 100
        await inv.step()  # quarantined: re-identification only, still no data
        assert len(r.sink.states) == after
        assert inv.diagnostics()["serial_matches"] is False


async def test_quarantine_clears_when_the_bound_device_returns(tmp_path: Path) -> None:
    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].responder.regs.update(serial_words(OTHER))
        r.clock.offset += 4000
        await inv.step()
        assert state_of(inv) is InverterState.QUARANTINED
        r.servers["gw1"].responder.regs.update(serial_words(SN))
        r.clock.offset += 100  # retry interval passed
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING


async def test_rebind_binds_the_new_device_and_invalidates(tmp_path: Path) -> None:
    async with rig(gw(), [inverter("deye1", "gw1")], {"gw1": device_regs(SN)}, tmp_path) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].responder.regs.update(serial_words(OTHER))
        r.clock.offset += 4000
        await inv.step()
        assert state_of(inv) is InverterState.QUARANTINED
        before_caps = json.loads((tmp_path / "capabilities.json").read_text())["data"]["deye1"][
            "key"
        ]
        assert r.runtime.rebind("deye1") is True
        assert state_of(inv) is InverterState.STARTING
        assert r.runtime.book.bound("deye1") is None
        assert "deye1" not in json.loads((tmp_path / "capabilities.json").read_text())["data"]
        assert inv.energy.resolution is None and not inv.observations
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING
        assert r.runtime.book.bound("deye1") == OTHER
        after = json.loads((tmp_path / "capabilities.json").read_text())["data"]["deye1"]["key"]
        assert before_caps[0] == SN and after[0] == OTHER


def test_normalize_serial() -> None:
    words = [serial_words(SN)[a] for a in range(3, 8)]
    assert normalize_serial(words) == SN
    assert normalize_serial([*words, 0x1234]) == SN  # only R3..R7 count
    assert normalize_serial(words[:4]) is None
    assert normalize_serial([0x0000] * 5) is None
    assert normalize_serial([0x2020] * 5) is None
    assert normalize_serial([0x7F41] * 5) is None
    assert normalize_serial([0x4120] * 5) is None  # trailing blank: not a clean serial


def test_identity_book_rules(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "identity.json")
    book = IdentityBook(store, {"a": SN, "b": None})
    assert book.bound("a") == SN and book.source("a") == "expected"
    assert book.bound("b") is None
    book.bind_first_seen("b", OTHER)
    with pytest.raises(ValueError):
        book.bind_first_seen("b", SN)
    assert book.rebind("a") is False
    assert book.rebind("b") is True
    assert book.bound("b") is None
    # persisted state is read back; an unreadable file starts empty and is not trusted
    again = IdentityBook(JsonStore(tmp_path / "identity.json"), {"a": SN})
    assert again.bound("a") == SN
    (tmp_path / "identity.json").write_text("{not json")
    assert IdentityBook(JsonStore(tmp_path / "identity.json"), {}).bound("a") is None


def test_json_store_atomic_and_versioned(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    store = JsonStore(path)
    store.data["k"] = 1
    store.save()
    assert JsonStore(path).data == {"k": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # no temp files
    path.write_text(json.dumps({"schema_version": 99, "data": {"k": 2}}))
    assert JsonStore(path).data == {}  # unknown layout is not trusted
