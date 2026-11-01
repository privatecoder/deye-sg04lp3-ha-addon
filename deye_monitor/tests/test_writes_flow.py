"""Write layer v1 end to end on the real app generation and fake devices (never a real device):
enablement, preview, single-use permits, one guarded FC16, audit, read-back and peer observation."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.write_ops import OPERATION_KEYS
from deye_monitor.writes import AUDIT_FILE, AuditLog, WriteService, WriteSettings
from tests.fakes.servers import Behavior
from tests.plant_helpers import A, plant_rig
from tests.write_helpers import (
    ALL,
    FAST,
    audit_lines,
    build_plant,
    confirm,
    err,
    fast,
    fc16,
    preview,
    settle,
    svc_of,
    write,
)

# -- disabled by default ----------------------------------------------------------------------


async def test_nothing_is_writable_until_the_owner_enables_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, enable=())
        for op in OPERATION_KEYS:
            status, body = await preview(rig, "deye1", op, 1)
            assert (status, err(body)) == (403, "writes_not_enabled")
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert status == 200 and body["operations"] == []
        status, body = await confirm(rig, "0" * 32)
        assert (status, err(body)) == (404, "unknown_operation")
        # enabled operations without the master switch (and the reverse) stay closed
        status, _ = await rig.call(
            "PUT", "/api/plant/inverters/deye1/writes", {"allow_writes": False, "operations": ALL}
        )
        assert status == 200
        await settle(rig, ("deye1", "deye2"))
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert (status, err(body)) == (403, "writes_not_enabled")
        status, _ = await rig.call(
            "PUT", "/api/plant/inverters/deye1/writes", {"allow_writes": True, "operations": []}
        )
        await settle(rig, ("deye1", "deye2"))
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert (status, err(body)) == (403, "writes_not_enabled")
        for server in rig.servers.values():
            assert server.responder.writes == [] and server.responder.other_fc == []
        assert audit_lines(rig) == []


async def test_enablement_is_validated_and_kept_across_inverter_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, enable=("deye1",), operations=["grid_charge_current"])
        put = "/api/plant/inverters/deye1/writes"
        for body in (
            {"allow_writes": True, "operations": ["work_mode"]},
            {"allow_writes": True, "operations": ["grid_charge_current", "grid_charge_current"]},
            {"allow_writes": "yes", "operations": []},
            {"allow_writes": True},
            {"allow_writes": True, "operations": "all"},
            {"allow_writes": True, "operations": [], "extra": 1},
        ):
            status, data = await rig.call("PUT", put, body)
            assert status in (400, 422), (body, data)
        assert (await rig.call("PUT", put, {"allow_writes": True, "operations": []}, user=None))[
            0
        ] == 403
        assert (await rig.call("PUT", put, {"allow_writes": True, "operations": []}, csrf=None))[
            0
        ] == 403
        assert (
            await rig.call("PUT", put, {"allow_writes": True, "operations": []}, revision=None)
        )[0] == 428
        assert (await rig.call("PUT", put, {"allow_writes": True, "operations": []}, revision=1))[
            0
        ] == 409
        # a plain edit of the inverter keeps the enablement, a changed route drops it
        _, plant = await rig.call("GET", "/api/plant")
        edited = {**plant["inverters"][0], "name": "Renamed"}
        status, _ = await rig.call("PUT", "/api/plant/inverters/deye1", edited)
        assert status == 200
        cfg = {i.id: i for i in rig.service.plant.inverters}["deye1"]
        assert cfg.allow_writes is True and cfg.enabled_write_operations == ("grid_charge_current",)
        status, _ = await rig.call(
            "PUT", "/api/plant/inverters/deye1", {**edited, "expected_serial": A}
        )
        assert status == 200
        cfg = {i.id: i for i in rig.service.plant.inverters}["deye1"]
        assert cfg.allow_writes is False and cfg.enabled_write_operations == ()


# -- the whole flow ---------------------------------------------------------------------------


async def test_full_flow_for_one_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert status == 200
        rows = {r["key"]: r for r in body["operations"]}
        assert set(rows) == set(OPERATION_KEYS)
        assert rows["grid_charge_current"] == {
            "key": "grid_charge_current",
            "state": "ready",
            "word": 20,
            "field": 20,
            "value": 20,
            "cap": 190,
        }
        assert rows["grid_charge_enable"]["word"] == 4 and rows["grid_charge_enable"]["field"] == 0
        assert rows["smart_load_off_voltage"]["value"] == 45.0

        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert status == 200, body
        pre = body["preview"]
        assert re.fullmatch("[0-9a-f]{32}", pre["operation_id"])
        assert (pre["old_value"], pre["new_value"]) == (20, 50)
        assert (pre["old_word_hex"], pre["new_word_hex"], pre["mask_hex"]) == (
            "0x0014",
            "0x0032",
            "0xFFFF",
        )
        assert pre["inverter"]["serial"] == A and pre["cap"] == 190
        assert [(p["id"], p["baseline_word"], p["baseline_value"]) for p in pre["peers"]] == [
            ("deye2", 20, 20)
        ]
        assert all(
            f is not None for f in (pre["operation"]["risk"]["de"], pre["operation"]["risk"]["en"])
        )
        for server in rig.servers.values():
            assert server.responder.writes == []  # a preview never writes

        status, body = await confirm(rig, pre["operation_id"])
        assert status == 200, body
        op = body["operation"]
        assert op["status"] == "readback_confirmed" and op["reason"] is None
        assert op["target"]["word"] == 50 and op["target"]["matches_request"] is True
        assert op["response"] == {"kind": "ack", "start": 128, "count": 1}
        assert [(p["id"], p["state"], p["changed"]) for p in op["peers"]] == [
            ("deye2", "different", False)
        ]
        assert "raw" not in op["response"]
        # exactly one FC16 on the target, none on the peer
        s1, s2 = rig.servers["gw1"], rig.servers["gw2"]
        assert len(fc16(s1)) == 1 and s2.responder.writes == []
        assert s1.responder.regs[128] == 50 and s2.responder.regs[128] == 20
        frame = fc16(s1)[0]
        assert frame[2:7] == bytes.fromhex("0000000901") and frame[7:] == bytes.fromhex(
            "1000800001020032"
        )
        # audit: durable intent first, then the result
        lines = audit_lines(rig)
        assert [x["event"] for x in lines] == ["intent", "result", "result"]
        assert [x["record"]["status"] for x in lines] == ["sent", "sent", "readback_confirmed"]
        assert lines[0]["record"]["status"] == "sent"
        assert lines[0]["record"]["old_word"] == 20 and lines[0]["record"]["new_word"] == 50
        assert lines[0]["record"]["user"] == "u1" and lines[0]["record"]["serial"] == A
        assert lines[1]["record"]["response"]["kind"] == "ack"  # written before the observation
        # the raw acknowledgement is part of the audit record (MBAP echo: 12 bytes)
        assert len(lines[1]["record"]["response"]["raw"]) == 24
        # the record is listed afterwards
        status, body = await rig.call("GET", "/api/inverters/deye1/writes/operations")
        assert [o["operation_id"] for o in body["operations"]] == [pre["operation_id"]]
        status, body = await rig.call("GET", f"/api/writes/operations/{pre['operation_id']}")
        assert body["operation"]["status"] == "readback_confirmed"


async def test_every_released_operation_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases: list[tuple[str, Any, int, int, int]] = [
        ("battery_charge_current", 80, 108, 50, 80),
        ("battery_discharge_current", 70, 109, 60, 70),
        ("generator_charge_current", 40, 125, 30, 40),
        ("grid_charge_current", 60, 128, 20, 60),
        ("grid_charge_enable", 1, 130, 0x0004, 0x0005),
        ("smart_load_off_voltage", "46.5", 134, 4500, 4650),
        ("smart_load_off_soc", 25, 135, 20, 25),
        ("smart_load_on_voltage", 52, 136, 5000, 5200),
        ("smart_load_on_soc", 70, 137, 60, 70),
    ]
    assert {c[0] for c in cases} == set(OPERATION_KEYS)
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        server = rig.servers["gw1"]
        for key, value, register, before, after in cases:
            assert server.responder.regs[register] == before
            op = await write(rig, "deye1", key, value)
            assert op["status"] == "readback_confirmed", (key, op)
            assert server.responder.regs[register] == after
        assert [r.start for r in server.responder.writes] == [c[2] for c in cases]
        assert op["peers"] == []  # a single inverter has no peers


async def test_r130_preserves_every_other_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r130": 0xA5A6}})
        op = await write(rig, "deye1", "grid_charge_enable", 1)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.regs[130] == 0xA5A7
        op = await write(rig, "deye1", "grid_charge_enable", 0)
        assert rig.servers["gw1"].responder.regs[130] == 0xA5A6
        status, body = await preview(rig, "deye1", "grid_charge_enable", 2)
        assert (status, err(body)) == (422, "value_out_of_range")
        status, body = await preview(rig, "deye1", "grid_charge_enable", True)
        assert (status, err(body)) == (422, "invalid_value")
        status, body = await preview(rig, "deye1", "grid_charge_enable", 0)  # unchanged: no-op
        assert (status, err(body)) == (409, "no_change")
        assert len(rig.servers["gw1"].responder.writes) == 2


# -- gates: user, CSRF, revision, single use ---------------------------------------------------


async def test_changing_calls_need_user_csrf_and_the_plant_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        for kw, expected in (
            ({"user": None}, "identity_required"),
            ({"csrf": None}, "csrf_invalid"),
            ({"csrf": "x" * 64}, "csrf_invalid"),
            ({"revision": None}, "revision_required"),
            ({"revision": 999}, "stale_revision"),
        ):
            status, body = await preview(rig, "deye1", "grid_charge_current", 50, **kw)
            assert err(body) == expected and status in (403, 428, 409), (kw, body)
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        op_id = body["preview"]["operation_id"]
        for kw, expected in (
            ({"user": None}, "identity_required"),
            ({"csrf": None}, "csrf_invalid"),
            ({"revision": None}, "revision_required"),
            ({"revision": 999}, "stale_revision"),
        ):
            status, body = await confirm(rig, op_id, **kw)
            assert err(body) == expected, (kw, body)
        # another user cannot use the permit of this one
        other = {"X-Remote-User-Id": "u2", "X-Remote-User-Name": "bob"}
        status, body = await confirm(rig, op_id, user=other)
        assert (status, err(body)) == (403, "permit_user_mismatch")
        for server in rig.servers.values():
            assert server.responder.writes == []
        # the permit survived all refusals and works once
        status, body = await confirm(rig, op_id)
        assert status == 200 and body["operation"]["status"] == "readback_confirmed"
        status, body = await confirm(rig, op_id)
        assert (status, err(body)) == (409, "operation_already_used")
        assert len(fc16(rig.servers["gw1"])) == 1
        for bad_id in ("x", "../etc", "G" * 32, "a" * 31):
            status, _ = await confirm(rig, bad_id)
            assert status in (404, 405)


async def test_double_submit_runs_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        op_id = body["preview"]["operation_id"]
        results = await asyncio.gather(
            confirm(rig, op_id), confirm(rig, op_id), confirm(rig, op_id)
        )
        assert sorted(s for s, _ in results) == [200, 409, 409]
        assert len(fc16(rig.servers["gw1"])) == 1


async def test_a_new_preview_revokes_the_earlier_permit_of_that_inverter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, first = await preview(rig, "deye1", "grid_charge_current", 50)
        _, second = await preview(rig, "deye1", "grid_charge_current", 55)
        status, body = await confirm(rig, first["preview"]["operation_id"])
        assert (status, err(body)) == (404, "unknown_operation")
        status, body = await confirm(rig, second["preview"]["operation_id"])
        assert body["operation"]["target"]["word"] == 55


async def test_permit_expiry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        fast(rig, replace(FAST, permit_s=0.05))
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        op_id = body["preview"]["operation_id"]
        await asyncio.sleep(0.12)
        status, body = await confirm(rig, op_id)
        assert (status, err(body)) == (410, "permit_expired")
        status, body = await confirm(rig, op_id)  # and it stays expired
        assert (status, err(body)) == (410, "permit_expired")
        assert rig.servers["gw1"].responder.writes == []


async def test_permit_expires_while_waiting_for_the_pacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        svc = fast(rig, replace(FAST, permit_s=0.3, pacing_s=5.0))
        _, first = await preview(rig, "deye1", "grid_charge_current", 50)
        await confirm(rig, first["preview"]["operation_id"])
        _, second = await preview(rig, "deye1", "grid_charge_current", 55)

        async def slow(_: float) -> None:
            await asyncio.sleep(0.4)  # the pacing wait outlasts the permit

        svc.sleep = slow
        _status, body = await confirm(rig, second["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "permit_expired"
        assert len(fc16(rig.servers["gw1"])) == 1


async def test_revoking_enablement_voids_pending_permits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        op_id = body["preview"]["operation_id"]
        status, _ = await rig.call(
            "PUT", "/api/plant/inverters/deye1/writes", {"allow_writes": False, "operations": ALL}
        )
        assert status == 200
        await settle(rig, ("deye1", "deye2"))
        status, body = await confirm(rig, op_id)
        assert (status, err(body)) == (404, "unknown_operation")
        assert all(s.responder.writes == [] for s in rig.servers.values())


async def test_permit_service_revocation_and_stale_plant_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        svc = svc_of(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        svc.revoke_all()
        status, body2 = await confirm(rig, body["preview"]["operation_id"])
        assert (status, err(body2)) == (404, "unknown_operation")
        # a plant change that does not touch the inverter still ends the generation
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        status, _ = await rig.call(
            "PUT", "/api/plant/totals", {"members": ["deye1"], "allow_single": True}
        )
        assert status == 200
        await settle(rig, ("deye1", "deye2"))
        status, body = await confirm(rig, body["preview"]["operation_id"])
        assert (status, err(body)) == (404, "unknown_operation")


async def test_a_restart_never_replays_a_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        old = svc_of(rig)
        fresh = WriteService(old.runtime, directory=rig.directory)  # what a restart builds
        with pytest.raises(Exception) as info:
            await fresh.confirm(body["preview"]["operation_id"], user_id="u1", plant_revision=1)
        assert getattr(info.value, "key", "") == "unknown_operation"
        assert all(s.responder.writes == [] for s in rig.servers.values())


async def test_unfinished_intents_become_unknown_after_a_restart(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / AUDIT_FILE)
    log.append("intent", {"operation_id": "a" * 32, "status": "sent", "inverter": "x"}, 1.0)
    log.append("result", {"operation_id": "b" * 32, "status": "readback_confirmed"}, 2.0)
    service = WriteService(None, directory=tmp_path)  # type: ignore[arg-type]
    assert service.operation("a" * 32)["status"] == "outcome_unknown"  # type: ignore[index]
    assert service.operation("a" * 32)["reason"] == "restart"  # type: ignore[index]
    assert service.operation("b" * 32)["status"] == "readback_confirmed"  # type: ignore[index]
    assert log.load()["a" * 32]["status"] == "outcome_unknown"
    # the sent intent is never turned back into something that could be replayed
    assert not service._permits


# -- safety at the send point -----------------------------------------------------------------


async def test_an_unavailable_audit_log_blocks_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        svc = svc_of(rig)
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a directory")
        svc.audit.path = blocker / AUDIT_FILE
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        status, body = await confirm(rig, body["preview"]["operation_id"])
        assert status == 200
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "audit_unavailable"
        assert all(s.responder.writes == [] for s in rig.servers.values())
        assert rig.servers["gw1"].responder.regs[128] == 20


async def test_a_change_since_the_preview_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        rig.servers["gw1"].responder.regs[128] = 33  # somebody else (LCD/cloud) wrote meanwhile
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "conflict"
        assert (
            rig.servers["gw1"].responder.writes == []
            and rig.servers["gw1"].responder.regs[128] == 33
        )
        # a changed identity word (firmware) is a conflict as well
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        rig.servers["gw1"].responder.regs[15] = 0x2000
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["reason"] == "firmware_changed"
        assert rig.servers["gw1"].responder.writes == []


async def test_a_dropped_connection_between_preview_and_confirm_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        rig.servers["gw1"].kick()
        await asyncio.sleep(0.05)
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] in (
            "connection_changed",
            "read_failed",
            "inverter_not_ready",
            "binding_changed",
        )
        assert rig.servers["gw1"].responder.writes == []


async def test_a_foreign_serial_behind_the_route_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        for address, word in {3: 0x3939, 4: 0x3939}.items():
            rig.servers["gw1"].responder.regs[address] = word
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        assert rig.servers["gw1"].responder.writes == []


# -- ambiguous outcomes: exactly one FC16 ------------------------------------------------------


@pytest.mark.parametrize(
    ("behavior", "applied"),
    [
        (Behavior(silent=True), True),  # lost answer, device applied it
        (Behavior(silent=True, write_applied=False), False),  # lost answer, not applied
        (Behavior(close=True), False),  # connection closed
        (Behavior(raw_reply=b"\x01\x02\x03"), True),  # malformed answer
    ],
)
async def test_ambiguous_acknowledgement_is_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: Behavior, applied: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        rig.servers["gw1"].write_script = [behavior]
        status, body = await confirm(rig, body["preview"]["operation_id"])
        op = body["operation"]
        assert status == 200 and op["status"] == "outcome_unknown", op
        assert op["reason"].startswith("no_valid_acknowledgement")
        assert len(fc16(rig.servers["gw1"])) == 1  # no retry, no fallback, no rollback
        assert (
            rig.servers["gw1"].responder.writes == []
            or len(rig.servers["gw1"].responder.writes) == 1
        )
        # the later observation is recorded but does not attribute the value to this command
        assert op["attribution"] == "not_established"
        assert op["target"] is not None
        if applied and behavior.silent:
            assert op["target"]["word"] == 50
        status, body = await rig.call("GET", f"/api/writes/operations/{op['operation_id']}")
        assert body["operation"]["status"] == "outcome_unknown"
        lines = audit_lines(rig)
        assert lines[0]["event"] == "intent" and lines[-1]["record"]["status"] == "outcome_unknown"


async def test_device_exception_is_a_validated_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        rig.servers["gw1"].write_script = [Behavior(exception_code=3)]
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        op = body["operation"]
        assert op["status"] == "failed" and op["reason"] == "device_exception"
        assert op["response"] == {"kind": "exception", "exception_code": 3}
        assert audit_lines(rig)[-1]["record"]["response"]["raw"].endswith("9003")
        assert len(fc16(rig.servers["gw1"])) == 1
        assert op["target"]["word"] == 20  # observed, unchanged


async def test_an_acknowledged_write_that_reads_back_differently_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        svc = svc_of(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)

        async def tamper(seconds: float) -> None:
            rig.servers["gw1"].responder.regs[128] = 99  # the device ends up with another value

        svc.sleep = tamper
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        op = body["operation"]
        assert op["status"] == "outcome_unknown" and op["reason"] == "readback_missing_or_mismatch"
        assert op["target"]["word"] == 99 and op["target"]["matches_request"] is False
        assert len(fc16(rig.servers["gw1"])) == 1


# -- parallel plant ---------------------------------------------------------------------------


async def test_peers_are_observed_with_neutral_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig,
            inverters=("deye1", "deye2", "deye3"),
            regs={"deye2": {"r128": 50}, "deye3": {"r128": 20}},
        )
        svc = svc_of(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        pre = body["preview"]
        assert [(p["id"], p["baseline_value"]) for p in pre["peers"]] == [
            ("deye2", 50),
            ("deye3", 20),
        ]

        async def later(seconds: float) -> None:
            rig.servers["gw3"].responder.regs[128] = 50  # a peer changes by itself afterwards

        svc.sleep = later
        _status, body = await confirm(rig, pre["operation_id"])
        peers = {p["id"]: p for p in body["operation"]["peers"]}
        assert peers["deye2"]["state"] == "same" and peers["deye2"]["changed"] is False
        assert peers["deye3"]["state"] == "same" and peers["deye3"]["changed"] is True
        text = json.dumps(body)
        assert "propagat" not in text and "synchron" not in text
        # only the target received a write
        assert len(fc16(rig.servers["gw1"])) == 1
        assert (
            rig.servers["gw2"].responder.writes == [] and rig.servers["gw3"].responder.writes == []
        )


async def test_peer_unavailable_after_the_write_and_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        rig.servers["gw2"].default = Behavior(silent=True)  # the peer goes away after the preview
        status, body = await confirm(rig, body["preview"]["operation_id"])
        op = body["operation"]
        assert op["status"] == "readback_confirmed"
        assert [(p["id"], p["state"], p["later_word"]) for p in op["peers"]] == [
            ("deye2", "unavailable", None)
        ]
        # an unreachable peer blocks a new operation instead of implying a complete observation
        status, body = await preview(rig, "deye1", "grid_charge_current", 60)
        assert (status, err(body)) == (409, "peer_unavailable")
        assert body["error"]["params"]["peers"] == ["deye2"]
        assert len(fc16(rig.servers["gw1"])) == 1


async def test_a_peer_with_another_serial_is_unavailable_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        rig.servers["gw2"].responder.regs[3] = 0x3939  # another device answers behind the route
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert (status, err(body)) == (409, "peer_unavailable")


async def test_there_is_no_preselected_write_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, enable=("deye1", "deye2"))
        status, body = await rig.call("GET", "/api/plant")
        assert set(body["writes"]["inverters"]) == {"deye1", "deye2"}
        # the API has no call that writes "the plant": the inverter is part of every URL
        status, _ = await rig.call("POST", "/api/writes/preview", {"operation": "x", "value": 1})
        assert status in (404, 405)
        _, one = await preview(rig, "deye2", "grid_charge_current", 55)
        assert one["preview"]["inverter"]["id"] == "deye2"
        assert [p["id"] for p in one["preview"]["peers"]] == ["deye1"]


# -- pacing -----------------------------------------------------------------------------------


async def test_plant_writes_are_paced_and_the_observation_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, enable=("deye1", "deye2"))
        svc = fast(rig, WriteSettings())  # the contract defaults
        assert (
            svc.settings.observe_delay_s,
            svc.settings.late_observe_delay_s,
            svc.settings.pacing_s,
            svc.settings.permit_s,
        ) == (1.0, 30.0, 5.0, 30.0)
        waits: list[float] = []

        async def record(seconds: float) -> None:
            waits.append(seconds)

        svc.sleep = record
        await write(rig, "deye1", "grid_charge_current", 50)
        await asyncio.sleep(0.05)  # the later peer observation waits its remaining 29 s
        # no pacing before the first write, then the observation delay, then the later look
        assert waits == [1.0, 29.0]
        await write(rig, "deye2", "grid_charge_current", 55)
        await asyncio.sleep(0.05)
        assert len(waits) == 5 and 4.0 < waits[2] <= 5.0 and waits[3:] == [1.0, 29.0]


async def test_writes_are_serialised_across_the_plant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, enable=("deye1", "deye2"))
        _, one = await preview(rig, "deye1", "grid_charge_current", 50)
        _, two = await preview(rig, "deye2", "grid_charge_current", 55)
        results = await asyncio.gather(
            confirm(rig, one["preview"]["operation_id"]),
            confirm(rig, two["preview"]["operation_id"]),
        )
        assert [r[1]["operation"]["status"] for r in results] == ["readback_confirmed"] * 2
        assert len(rig.servers["gw1"].responder.writes) == 1
        assert len(rig.servers["gw2"].responder.writes) == 1


# -- preconditions ----------------------------------------------------------------------------


async def test_unknown_model_locks_the_current_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r20": 70000 & 0xFFFF, "r21": 0}}
        )
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        rows = {r["key"]: r for r in body["operations"]}
        for key in ("battery_charge_current", "grid_charge_current"):
            assert (rows[key]["state"], rows[key]["reason"]) == ("locked", "model_unknown")
            assert rows[key]["cap"] is None
        assert rows["grid_charge_enable"]["state"] == "ready"
        assert rows["smart_load_off_soc"]["state"] == "ready"
        status, body = await preview(rig, "deye1", "grid_charge_current", 10)
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "model_unknown"
        status, body = await preview(rig, "deye1", "smart_load_off_soc", 10)
        assert status == 200
        assert rig.servers["gw1"].responder.writes == []


@pytest.mark.parametrize(
    ("watts", "cap"), [(5000, 120), (6000, 150), (8000, 190), (10000, 210), (12000, 240)]
)
async def test_the_model_cap_limits_the_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watts: int, cap: int
) -> None:
    raw = watts * 10
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig,
            inverters=("deye1",),
            regs={"deye1": {"r20": raw & 0xFFFF, "r21": raw >> 16, "r108": cap - 5}},
        )
        status, body = await preview(rig, "deye1", "battery_charge_current", cap + 1)
        assert (status, err(body)) == (422, "value_out_of_range")
        assert body["error"]["params"]["high"] == cap
        status, body = await preview(rig, "deye1", "battery_charge_current", cap)
        assert status == 200 and body["preview"]["cap"] == cap
        status, body = await preview(rig, "deye1", "battery_charge_current", -1)
        assert (status, err(body)) == (422, "value_out_of_range")


async def test_an_existing_value_outside_the_envelope_blocks_the_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        # 8 kW model (190 A) with a stored 210 A: it could not be restored inside the envelope
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r108": 210, "r136": 9000}})
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        rows = {r["key"]: r for r in body["operations"]}
        assert rows["battery_charge_current"]["reason"] == "existing_value_outside_range"
        assert rows["smart_load_on_voltage"]["reason"] == "existing_value_outside_range"
        status, body = await preview(rig, "deye1", "battery_charge_current", 100)
        assert (status, err(body)) == (409, "operation_locked")
        assert rig.servers["gw1"].responder.writes == []


async def test_smart_load_needs_the_gen_port_to_be_smart_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 0}})
        for key in OPERATION_KEYS:
            if not key.startswith("smart_load"):
                continue
            status, body = await preview(rig, "deye1", key, 50)
            assert (status, err(body)) == (409, "operation_locked"), key
            assert body["error"]["params"]["reason"] == "gen_not_smart_load"
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert status == 200
        assert rig.servers["gw1"].responder.writes == []


async def test_smart_load_threshold_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        for key, value in (
            ("smart_load_off_voltage", 50),  # OFF equal to ON (50.00 V)
            ("smart_load_off_voltage", 55),
            ("smart_load_on_voltage", 45),  # ON equal to OFF
            ("smart_load_on_voltage", 40),
            ("smart_load_off_soc", 60),
            ("smart_load_off_soc", 99),
            ("smart_load_on_soc", 20),
            ("smart_load_on_soc", 5),
        ):
            status, body = await preview(rig, "deye1", key, value)
            assert (status, err(body)) == (422, "threshold_order"), (key, value)
        accepted: tuple[tuple[str, int | float], ...] = (
            ("smart_load_off_voltage", 49.99),
            ("smart_load_on_voltage", 45.01),
            ("smart_load_off_soc", 59),
            ("smart_load_on_soc", 21),
        )
        for key, number in accepted:
            status, body = await preview(rig, "deye1", key, number)
            assert status == 200, (key, number, body)
        status, body = await preview(rig, "deye1", "smart_load_off_voltage", "48.555")
        assert (status, err(body)) == (422, "value_not_exact")
        status, body = await preview(rig, "deye1", "smart_load_off_voltage", "48,5")
        assert (status, err(body)) == (422, "invalid_value")


async def test_unknown_operations_and_shapes_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        for operation in ("work_mode", "battery_shutdown_soc", "r80", "108", "../x", "", None, 5):
            status, body = await rig.call(
                "POST",
                "/api/inverters/deye1/writes/preview",
                {"operation": operation, "value": 1},
            )
            assert status in (400, 404) and err(body) in ("unknown_operation", "invalid_request")
        payloads: tuple[Any, ...] = (
            {"operation": "grid_charge_current"},
            {"value": 1},
            {"operation": "grid_charge_current", "value": 1, "register": 128},
            {"operation": "grid_charge_current", "value": 1, "frame": "00"},
            [],
        )
        for payload in payloads:
            status, body = await rig.call("POST", "/api/inverters/deye1/writes/preview", payload)
            assert status == 400, payload
        status, body = await preview(rig, "nobody", "grid_charge_current", 1)
        assert status == 404
        assert all(s.responder.writes == [] for s in rig.servers.values())


async def test_writing_waits_for_an_identified_inverter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        runtime = rig.app.runtime
        assert runtime is not None
        runtime.inverters["deye1"].active = 1 if len(runtime.inverters["deye1"].routes) > 1 else 0
        inv = runtime.inverters["deye1"]
        inv.needs_identity = True
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert (status, err(body)) == (409, "inverter_not_ready")
        assert body["error"]["params"]["reason"] == "identifying"


# -- owner verification and drift -------------------------------------------------------------


async def test_owner_verification_is_recorded_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        op = await write(rig, "deye1", "grid_charge_current", 50)
        url = f"/api/writes/operations/{op['operation_id']}/verification"
        status, body = await rig.call("POST", url, {"state": "confirmed", "note": "LCD shows 50"})
        assert status == 200
        assert body["operation"]["verification"]["state"] == "confirmed"
        assert body["operation"]["status"] == "readback_confirmed"  # the read-back is unchanged
        status, body = await rig.call("POST", url, {"state": "maybe"})
        assert status == 422
        assert (await rig.call("POST", url, {"state": "confirmed"}, csrf=None))[0] == 403
        lines = audit_lines(rig)
        assert lines[-1]["event"] == "verification"


async def test_later_drift_is_recorded_and_never_reasserted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        op = await write(rig, "deye1", "grid_charge_current", 50)
        rig.servers["gw1"].responder.regs[128] = 70  # changed later by somebody else
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert status == 200
        status, body = await rig.call("GET", f"/api/writes/operations/{op['operation_id']}")
        assert body["operation"]["drift"]["word"] == 70
        assert rig.servers["gw1"].responder.regs[128] == 70  # not reasserted
        assert len(rig.servers["gw1"].responder.writes) == 1
        assert audit_lines(rig)[-1]["event"] == "drift"


# -- the write layer has no other entrances ---------------------------------------------------


def test_no_mqtt_command_path_exists() -> None:
    root = Path(__file__).parents[1] / "src" / "deye_monitor"
    text = {p.name: p.read_text() for p in root.rglob("*.py")}
    for name, source in text.items():
        for forbidden in ("command_topic", "set_topic", "mqtt_command", "on_message"):
            assert forbidden not in source, (name, forbidden)
    # the publisher listens to the Home Assistant birth topic only
    subscriptions = re.findall(r"conn\.subscribe\(([^)]*)\)", text["mqtt.py"])
    assert subscriptions and all("birth" in s for s in subscriptions), subscriptions


def test_write_entrances_are_only_the_web_ui_calls() -> None:
    root = Path(__file__).parents[1] / "src" / "deye_monitor"
    senders = {p.name for p in root.rglob("*.py") if "write_register(" in p.read_text()}
    assert senders == {"base.py", "writes.py"}  # the transport method and its one caller
    confirmers = {p.name for p in root.rglob("*.py") if ".confirm(" in p.read_text()}
    assert confirmers == {"webapi.py"}


# -- firmware release (R17-01) ----------------------------------------------------------------


async def test_the_release_stores_the_firmware_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        cfg = rig.app.runtime.inverters["deye1"].cfg  # type: ignore[union-attr]
        release = cfg.write_release_firmware
        assert release is not None
        regs = rig.servers["gw1"].responder.regs
        assert release.words == tuple(
            regs.get(a, (a * 3 + 1) & 0xFFFF) for a in (11, 14, 15, 17, 18)
        )
        # releasing nothing drops the fingerprint
        status, body = await rig.call(
            "PUT", "/api/plant/inverters/deye1/writes", {"allow_writes": False, "operations": []}
        )
        assert status == 200, body
        assert rig.app.runtime.inverters["deye1"].cfg.write_release_firmware is None  # type: ignore[union-attr]


async def test_a_release_without_a_fresh_identity_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, enable=())
        rig.servers["gw1"].responder.fail_addresses.add(17)
        status, body = await rig.call(
            "PUT",
            "/api/plant/inverters/deye1/writes",
            {"allow_writes": True, "operations": ["grid_charge_current"]},
        )
        assert status == 409 and err(body) in {"read_failed", "inverter_not_ready"}, body
        cfg = rig.app.runtime.inverters["deye1"].cfg  # type: ignore[union-attr]
        assert not cfg.allow_writes and cfg.write_release_firmware is None


@pytest.mark.parametrize("address", [11, 14, 15, 17, 18])
async def test_changed_firmware_words_make_the_release_inert_until_released_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, address: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        server = rig.servers["gw1"]
        regs = server.responder.regs
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        permit = body["preview"]["operation_id"]
        regs[address] = 0x9999  # an upgrade (or another board) after the release
        # the permit issued before is rejected at the send point
        _, body = await confirm(rig, permit)
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "firmware_changed"
        # no new preview either
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert (status, err(body)) == (409, "firmware_changed")
        assert fc16(server) == [] and regs[128] == 20
        _, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert {r["reason"] for r in body["operations"]} == {"firmware_changed"}
        # the owner releases again: the new fingerprint replaces the old one
        status, body = await rig.call(
            "PUT", "/api/plant/inverters/deye1/writes", {"allow_writes": True, "operations": ALL}
        )
        assert status == 200, body
        await settle(rig, ("deye1", "deye2"))
        op = await write(rig, "deye1", "grid_charge_current", 50)
        assert op["status"] == "readback_confirmed"
        assert len(fc16(server)) == 1


async def test_an_unreleased_hardware_profile_name_in_the_fingerprint_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        svc = svc_of(rig)
        inv = rig.app.runtime.inverters["deye1"]  # type: ignore[union-attr]
        stored = inv.cfg.write_release_firmware
        assert stored is not None
        object.__setattr__(
            inv.cfg, "write_release_firmware", stored.model_copy(update={"profile": "other"})
        )
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert (status, err(body)) == (409, "firmware_changed")
        assert svc is not None


# -- audit completeness (R17-02) --------------------------------------------------------------


async def test_a_short_audit_write_is_completed_before_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.write

    def one_byte(fd: int, data: Any) -> int:
        return real(fd, bytes(data)[:1])

    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        monkeypatch.setattr("deye_monitor.writes.os.write", one_byte)
        op = await write(rig, "deye1", "grid_charge_current", 50)
        monkeypatch.undo()
        assert op["status"] == "readback_confirmed"
        recovered = AuditLog(rig.directory / AUDIT_FILE).load()
        assert recovered[op["operation_id"]]["status"] == "readback_confirmed"


async def test_a_zero_progress_audit_write_blocks_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        monkeypatch.setattr("deye_monitor.writes.os.write", lambda fd, data: 0)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        _, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "audit_unavailable"
        assert fc16(rig.servers["gw1"]) == []


def test_a_torn_trailing_line_does_not_corrupt_the_next_record(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / AUDIT_FILE)
    log.append("intent", {"operation_id": "a", "status": "sent"}, 1.0)
    with log.path.open("ab") as fh:
        fh.write(b'{"v":1,"ts":2.0,"event":"result","rec')  # interrupted, no newline
    log.append("intent", {"operation_id": "b", "status": "sent"}, 3.0)
    loaded = log.load()
    assert loaded["a"]["status"] == "sent" and loaded["b"]["status"] == "sent"


# -- delayed peer observation, read again ---------------------------------------------------------


async def test_the_later_peer_observation_is_a_second_read_only_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, regs={"deye2": {"r128": 20}})
        svc = fast(rig)
        gate = asyncio.Event()

        async def slow(seconds: float) -> None:
            if seconds >= 1:  # only the wait for the later look
                await gate.wait()

        svc.sleep = slow
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        op = body["operation"]
        assert op["status"] == "readback_confirmed"
        late = op["late_observation"]
        assert late["status"] == "pending" and late["after_s"] == 30.0 and late["in_s"] > 0
        assert op["peers"][0]["state"] == "different"
        assert "late_word" not in op["peers"][0]
        # the write serialisation is not held while the later look waits
        assert not svc._lock.locked()
        rig.servers["gw2"].responder.regs[128] = 50  # the peer caught up meanwhile
        gate.set()
        for _ in range(100):
            _status, body = await rig.call("GET", f"/api/writes/operations/{op['operation_id']}")
            if body["operation"]["late_observation"]["status"] == "done":
                break
            await asyncio.sleep(0.02)
        later = body["operation"]
        assert later["late_observation"]["status"] == "done"
        peer = later["peers"][0]
        assert (peer["state"], peer["late_state"], peer["late_word"]) == ("different", "same", 50)
        events = [(x["event"], x["record"]["operation_id"]) for x in audit_lines(rig)]
        assert ("late_observation", op["operation_id"]) in events
        assert "propagat" not in json.dumps(later) and "synchron" not in json.dumps(later)
        assert len(fc16(rig.servers["gw1"])) == 1 and fc16(rig.servers["gw2"]) == []


async def test_a_generation_change_cancels_the_pending_later_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        svc = fast(rig)
        never = asyncio.Event()

        async def slow(seconds: float) -> None:
            if seconds >= 1:
                await never.wait()

        svc.sleep = slow
        op = await write(rig, "deye1", "grid_charge_current", 50)
        assert op["late_observation"]["status"] == "pending"
        await svc.close()
        record = svc.operation(op["operation_id"])
        assert record is not None and record["late_observation"]["status"] == "interrupted"


async def test_the_owner_can_read_the_other_inverters_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, regs={"deye2": {"r128": 20}})
        fast(rig)
        op = await write(rig, "deye1", "grid_charge_current", 50)
        url = f"/api/writes/operations/{op['operation_id']}/peers"
        writes_before = len(fc16(rig.servers["gw1"]))
        rig.servers["gw2"].responder.regs[128] = 50
        status, body = await rig.call("POST", url, {})
        assert status == 200, body
        reread = body["operation"]["peer_rereads"]
        assert len(reread) == 1 and reread[0]["peers"][0]["state"] == "same"
        rig.servers["gw2"].responder.regs[128] = 7
        _status, body = await rig.call("POST", url, {})
        assert [r["peers"][0]["state"] for r in body["operation"]["peer_rereads"]] == [
            "same",
            "different",
        ]
        assert [x["event"] for x in audit_lines(rig)].count("peer_reread") == 2
        # reading only: no further frame with a write, none to the peer
        assert len(fc16(rig.servers["gw1"])) == writes_before and fc16(rig.servers["gw2"]) == []
        status, body = await rig.call("POST", "/api/writes/operations/" + "0" * 32 + "/peers", {})
        assert status == 404 and err(body) == "unknown_operation"


async def test_a_rejected_operation_has_nothing_to_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        fast(rig)
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        rig.servers["gw1"].responder.regs[128] = 33
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        status, body = await rig.call(
            "POST", f"/api/writes/operations/{body['operation']['operation_id']}/peers", {}
        )
        assert status == 409 and err(body) == "nothing_to_observe"


async def test_a_pending_later_look_is_interrupted_by_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        svc = fast(rig)
        never = asyncio.Event()

        async def slow(seconds: float) -> None:
            if seconds >= 1:
                await never.wait()

        svc.sleep = slow
        op = await write(rig, "deye1", "grid_charge_current", 50)
        again = WriteService(svc.runtime, directory=rig.directory)
        record = again.operation(op["operation_id"])
        assert record is not None and record["late_observation"]["status"] == "interrupted"
        assert (
            AuditLog(rig.directory / AUDIT_FILE).load()[op["operation_id"]]["late_observation"][
                "status"
            ]
            == "interrupted"
        )
        never.set()


# -- slaves are locked: the master distributes the settings --------------------------------

SLAVE = {"r336": 0x0001}  # parallel mode on, not the master
MASTER = {"r336": 0x0003}


async def test_a_slave_is_locked_for_preview_and_for_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, regs={"deye1": SLAVE})
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert status == 409 and err(body) == "parallel_slave"
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert status == 200
        assert {(r["state"], r["reason"]) for r in body["operations"]} == {
            ("locked", "parallel_slave")
        }
        assert fc16(rig.servers["gw1"]) == [] and rig.servers["gw1"].responder.writes == []


async def test_the_lock_follows_the_current_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, regs={"deye1": SLAVE})
        status, _body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert status == 409
        rig.servers["gw1"].responder.regs[336] = 0x0003  # another unit gave up, this one is master
        op = await write(rig, "deye1", "grid_charge_current", 50)
        assert op["status"] == "readback_confirmed"
        assert op["role"] == {"parallel": True, "master": True}
        assert len(fc16(rig.servers["gw1"])) == 1


async def test_a_role_change_between_preview_and_confirm_rejects_without_a_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, regs={"deye1": MASTER})
        _, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert body["preview"]["inverter"]["role"] == {"parallel": True, "master": True}
        rig.servers["gw1"].responder.regs[336] = 0x0001  # no longer the master
        _status, body = await confirm(rig, body["preview"]["operation_id"])
        assert body["operation"]["status"] == "rejected"
        assert body["operation"]["reason"] == "parallel_slave"
        assert fc16(rig.servers["gw1"]) == [] and rig.servers["gw1"].responder.writes == []


async def test_an_unreadable_role_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig)
        monkeypatch.setattr(
            WriteService,
            "_role",
            staticmethod(lambda inv, words: {"parallel": None, "master": None}),
        )
        status, body = await preview(rig, "deye1", "grid_charge_current", 50)
        assert status == 409 and err(body) == "parallel_slave"
        assert fc16(rig.servers["gw1"]) == []


def test_only_a_known_master_or_a_known_single_unit_may_be_written() -> None:
    locked = WriteService._slave_locked
    assert not locked({"parallel": False, "master": False})
    assert not locked({"parallel": False, "master": None})
    assert not locked({"parallel": True, "master": True})
    assert locked({"parallel": True, "master": False})
    assert locked({"parallel": True, "master": None})
    assert locked({"parallel": None, "master": None})
