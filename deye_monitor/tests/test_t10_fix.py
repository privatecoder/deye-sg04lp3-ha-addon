"""T10-Fix (code review 6, R6-01..07): recovery commit, strict adoption, action counter, shared
bus budget, diagnostics provenance, orderly shutdown under cancellation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.mqtt import PublisherSettings
from deye_monitor.selection import ChangeError, Edit
from tests.device_helpers import device_regs, ep, inverter, rig, serial_words
from tests.selection_helpers import newest_slot, no_selection_files, plant
from tests.test_selection import manager_at
from tests.test_webapi import client

# -- R6-01 (two-slot store) -----------------------------------------------------------------


async def damaged_newest_slot_with_good_previous(tmp_path: Path) -> str:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(remove=frozenset({"bms_soc"}))})
    await m.change(1, {"deye1": Edit(remove=frozenset({"pv1_power"}))})
    name = newest_slot(tmp_path)
    (tmp_path / name).write_text("{broken")
    return name


def assert_last_good(tmp_path: Path) -> None:
    again = manager_at(tmp_path)
    assert "bms_soc" not in again.selection("deye1").effective
    assert again.revision == 1
    assert again.store.source == "recovered_previous"
    assert again.store.problems


async def test_failed_save_during_corrupt_file_recovery_keeps_last_good_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = await damaged_newest_slot_with_good_previous(tmp_path)
    recovered = manager_at(tmp_path)
    assert recovered.revision == 1

    def fail(_: Any) -> None:
        raise OSError("disk full during recovery commit")

    for slot in recovered.store._slots:
        monkeypatch.setattr(slot, "commit", fail)
    with pytest.raises(ChangeError, match="persist_failed"):
        await recovered.change(1, {"deye1": Edit(remove=frozenset({"pv2_power"}))})
    assert (tmp_path / broken).read_text() == "{broken"
    assert_last_good(tmp_path)


@pytest.mark.parametrize("step", ["copyfile", "mkstemp", "fsync", "replace"])
async def test_recovery_commit_fault_injection_keeps_recoverable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    await damaged_newest_slot_with_good_previous(tmp_path)
    recovered = manager_at(tmp_path)

    def fail(*_: Any, **__: Any) -> None:
        raise OSError(f"injected at {step}")

    target = {
        "copyfile": "deye_monitor.selection.shutil.copyfile",
        "mkstemp": "deye_monitor.store.tempfile.mkstemp",
        "fsync": "deye_monitor.store.os.fsync",
        "replace": "deye_monitor.store.os.replace",
    }[step]
    monkeypatch.setattr(target, fail)
    with pytest.raises(ChangeError, match="persist_failed"):
        await recovered.change(1, {"deye1": Edit(remove=frozenset({"pv2_power"}))})
    monkeypatch.undo()
    assert not list(tmp_path.glob(".selection.*"))  # noqa: ASYNC240
    assert_last_good(tmp_path)


async def test_successful_recovery_commit_keeps_a_copy_of_the_damaged_file(tmp_path: Path) -> None:
    broken = await damaged_newest_slot_with_good_previous(tmp_path)
    recovered = manager_at(tmp_path)
    await recovered.change(1, {"deye1": Edit(remove=frozenset({"pv2_power"}))})
    assert (tmp_path / (broken + ".corrupt")).read_text() == "{broken"
    again = manager_at(tmp_path)
    assert again.revision == 2 and again.store.problems == []


# -- migration of the earlier schema (selection.json + .prev + .recovery.json) -------------------
# -- R6-02 -----------------------------------------------------------------------------------


async def test_failed_live_adoption_is_not_reported_as_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with client(tmp_path) as c:
        inv = c.plant.rig.inv("deye1")
        before = (inv.selection, inv.selection_revision)
        real = inv.set_selection
        calls = {"n": 0}

        def fail_once(*args: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("injected live adoption failure")
            real(*args, **kwargs)

        monkeypatch.setattr(inv, "set_selection", fail_once)
        status, body = await c.post("/api/inverters/deye1/sensors", {"remove": ["bms_soc"]})
        assert status == 500 and body["error"]["key"] == "adoption_failed"
        assert c.plant.manager.revision == 0
        assert "bms_soc" in c.plant.manager.selection("deye1").effective
        assert (inv.selection, inv.selection_revision) == before  # old coherent revision
        assert manager_at(tmp_path).revision == 0  # the file is back on the previous revision
        status, _ = await c.post("/api/inverters/deye1/sensors", {"remove": ["bms_soc"]})
        assert status == 200 and "bms_soc" not in inv.selection


async def test_unprepareable_selection_is_refused_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant(tmp_path) as p:
        inv = p.rig.inv("deye1")

        def refuse(*_: Any) -> None:
            raise RuntimeError("cannot build")

        monkeypatch.setattr(inv, "check_selection", refuse)
        with pytest.raises(ChangeError) as err:
            await p.manager.change(0, {"deye1": Edit(remove=frozenset({"bms_soc"}))})
        assert err.value.key == "adoption_failed"
        assert not (tmp_path / "selection.json").exists() and p.manager.revision == 0


# -- R6-03 -----------------------------------------------------------------------------------

ACTIONS = [
    ("/api/inverters/deye1/rebind", {"confirm": True}),
    (
        "/api/stores/reset",
        {"kind": "energy", "scope": "inverter", "inverter": "deye1", "confirm": True},
    ),
]


@pytest.mark.parametrize(("route", "body"), ACTIONS)
@pytest.mark.parametrize(("revision", "expected"), [(None, 428), (0, 409)])
async def test_identity_and_energy_actions_require_current_counter(
    tmp_path: Path, route: str, body: dict[str, Any], revision: int | None, expected: int
) -> None:
    async with client(tmp_path) as c:
        await c.plant.rig.inv("deye1").step()
        await c.plant.manager.change(0, {"deye1": Edit(remove=frozenset({"bms_soc"}))})
        status, _ = await c.post(route, body, revision=revision)
        assert status == expected


async def test_successful_action_raises_the_counter_and_old_confirmation_is_stale(
    tmp_path: Path,
) -> None:
    async with client(tmp_path) as c:
        await c.plant.rig.inv("deye1").step()
        _, session = await c.get("/api/session", csrf=None)
        counter = session["action_counter"]
        route, body = ACTIONS[1]
        status, result = await c.post(route, body, revision=counter)
        assert status == 200 and result["action_counter"] == counter + 1
        status, again = await c.post(route, body, revision=counter)  # the same confirmation
        assert status == 409 and again["error"]["params"]["current"] == counter + 1
        _, session = await c.get("/api/session", csrf=None)
        assert session["action_counter"] == counter + 1


# -- R6-04 -----------------------------------------------------------------------------------


async def test_candidate_budget_includes_actual_shared_bus_gap(tmp_path: Path) -> None:
    eps = [
        ep("ga", port=25001, bus_id="shared", request_gap_ms=0),
        ep("gb", port=25002, bus_id="shared", request_gap_ms=3000),
    ]
    invs = [inverter("deye1", "ga"), inverter("deye2", "gb", unit_id=2)]
    async with plant(tmp_path, endpoints=eps, inverters=invs) as p:
        ui = p.manager.plan("deye1").budget
        actual = p.rig.inv("deye1").current_plan().budget
        assert ui.utilisation == pytest.approx(actual.utilisation)
        assert ui.overloaded and actual.overloaded


async def test_preview_budget_uses_runtime_route_and_reports_total_bus_load(
    tmp_path: Path,
) -> None:
    eps = [
        ep("ga", port=25001, bus_id="shared", request_gap_ms=0),
        ep("gb", port=25002, bus_id="shared", request_gap_ms=3000),
    ]
    invs = [inverter("deye1", "ga"), inverter("deye2", "gb", unit_id=2)]
    async with client(tmp_path, endpoints=eps, inverters=invs) as c:
        runtime = c.plant.rig.runtime
        status, body = await c.post("/api/inverters/deye1/sensors/preview", {"remove": ["bms_soc"]})
        assert status == 200
        view = body["preview"]["selection"]["budget"]
        plan = runtime.inverters["deye1"].candidate_plan(
            view and body["preview"]["selection"]["effective"]
        )
        assert view["utilisation"] == pytest.approx(plan.budget.utilisation)
        # the bus total includes the other inverter on the same bus
        other = runtime.inverters["deye2"].current_plan().budget.utilisation
        assert view["bus_utilisation"] >= view["utilisation"] + other
        status, body = await c.post("/api/transfer/preview", {"source": "deye1"})
        target = body["targets"]["deye2"]["selection"]["budget"]
        assert target["bus_utilisation"] >= target["utilisation"]


# -- R6-05 -----------------------------------------------------------------------------------


async def test_diagnostics_do_not_reuse_previous_inverter_r65(tmp_path: Path) -> None:
    from deye_monitor.ha_discovery import inverter_diagnostic_values

    a, b = "2401234567", "9999999999"
    async with rig([ep("g")], [inverter("i", "g")], {"g": device_regs(a)}, tmp_path) as r:
        inv = r.inv("i")
        await inv.step()
        assert inv.diagnostics()["r65"] == {"raw": 260, "last_read_ok": True}
        inv.set_selection(set(), 1)  # R65 is no longer required
        assert inv.rebind()
        assert inv.diagnostics()["r65"] == {"raw": None, "last_read_ok": None}
        r.servers["g"].responder.regs.update(serial_words(b))
        await inv.step()
        d = inv.diagnostics()
        out = inverter_diagnostic_values(d)
        assert d["bound_serial"] == b
        assert out["r65_status"] is None and out["r65_raw"] is None


async def test_explicit_missing_binding_is_not_filled_from_seen_serial(tmp_path: Path) -> None:
    from tests.fakes.mqtt import settle
    from tests.test_mqtt_publisher import NS, make

    async with make(
        tmp_path,
        serial_of=lambda _: "2401234567",
        diagnostics_of=lambda _: {"bound_serial": None},
    ) as r:
        conn = await r.ready()
        topic = NS.inverter_diagnostics_topic("deye1")
        before = len(conn.on(topic))
        r.pub.on_diagnostics("deye1", {"bound_serial": None})
        await settle(lambda: len(conn.on(topic)) > before)
        assert conn.last(topic).json()["serial"] is None


# -- R6-06 -----------------------------------------------------------------------------------


async def test_outer_shutdown_deadline_preserves_offline_or_abort(tmp_path: Path) -> None:
    from tests.test_mqtt_publisher import NS, make

    cfg = PublisherSettings(
        reconnect_min_s=0.01, reconnect_max_s=0.04, shutdown_deadline_s=0.2, publish_timeout_s=10
    )
    async with make(tmp_path, settings=cfg) as r:
        conn = await r.ready()
        original = conn.publish

        async def block_offline(topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
            if payload == b"offline":
                await asyncio.Event().wait()
            await original(topic, payload, qos=qos, retain=retain)

        conn.publish = block_offline  # type: ignore[method-assign]
        r.pub.request_stop()
        # an outer deadline shorter than the inner budgets (the old serve() arithmetic)
        with pytest.raises(TimeoutError):
            assert r.task is not None
            await asyncio.wait_for(r.task, cfg.shutdown_deadline_s + 0.1)
        assert conn.aborted or conn.last(NS.app_status_topic).text == "offline"
        assert conn.aborted  # the app's offline was never confirmed


def test_stop_budget_is_derived_from_the_inner_budgets() -> None:
    from deye_monitor.mqtt import STOP_RESERVE_S, MqttPublisher

    for settings in (
        PublisherSettings(),
        PublisherSettings(shutdown_deadline_s=9, publish_timeout_s=30),
    ):
        budget = MqttPublisher.stop_budget_s.fget  # type: ignore[attr-defined]
        holder = type("H", (), {"settings": settings})()
        assert (
            budget(holder)
            == settings.publish_timeout_s + 2 * settings.shutdown_deadline_s + STOP_RESERVE_S
        )


# -- R6-07 -----------------------------------------------------------------------------------


async def test_catalog_ships_descriptions_in_both_languages(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        status, body = await c.get("/api/catalog", csrf=None)
        assert status == 200
        entries = [e for p in body["profiles"].values() for e in p["entries"]]
        assert entries
        for e in entries:
            assert e["description"]["de"] and e["description"]["en"], e["key"]


# -- N4-01 / N5-01..03: nothing is reported as saved that is not durable -----------------------


async def test_a_change_that_cannot_be_made_durable_is_rolled_back_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with client(tmp_path) as c:
        inv = c.plant.rig.inv("deye1")
        for slot in c.plant.manager.store._slots:
            monkeypatch.setattr(slot, "commit", _refuse)
        status, body = await c.post("/api/inverters/deye1/sensors", {"remove": ["bms_soc"]})
        assert status == 500 and body["error"]["key"] == "persist_failed"
        assert c.plant.manager.revision == inv.selection_revision == 0
        assert "bms_soc" in inv.selection  # the running application is back on the old state
        monkeypatch.undo()
        restart = manager_at(tmp_path)
        assert restart.revision == 0 and "bms_soc" in restart.selection("deye1").effective
        status, _ = await c.post("/api/inverters/deye1/sensors", {"remove": ["bms_soc"]})
        assert status == 200 and manager_at(tmp_path).revision == 1


def _refuse(*_: Any, **__: Any) -> None:
    raise OSError("disk unavailable")


async def test_failed_adoption_leaves_the_accepted_state_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with client(tmp_path) as c:
        inv = c.plant.rig.inv("deye1")
        real, calls = inv.set_selection, {"n": 0}

        def fail_once(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("forward adoption failed")
            return real(*args, **kwargs)

        monkeypatch.setattr(inv, "set_selection", fail_once)
        status, body = await c.post("/api/inverters/deye1/sensors", {"remove": ["bms_soc"]})
        assert status == 500 and body["error"]["key"] == "adoption_failed"
        assert no_selection_files(tmp_path)  # nothing was ever made durable
        assert manager_at(tmp_path).revision == 0


async def test_startup_with_unwritable_slots_keeps_the_accepted_state_through_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(remove=frozenset({"bms_soc"}))})
    broken = manager_at(tmp_path)
    for slot in broken.store._slots:
        monkeypatch.setattr(slot, "commit", _refuse)
    for _ in range(2):  # a failing retry loses nothing
        with pytest.raises(ChangeError, match="persist_failed"):
            await broken.change(1, {"deye1": Edit(remove=frozenset({"pv1_power"}))})
    monkeypatch.undo()
    again = manager_at(tmp_path)
    assert again.revision == 1 and "pv1_power" in again.selection("deye1").effective
    assert "bms_soc" not in again.selection("deye1").effective


async def test_a_half_written_newer_slot_is_never_used(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(remove=frozenset({"bms_soc"}))})
    newest = newest_slot(tmp_path)
    other = "selection.b.json" if newest == "selection.a.json" else "selection.a.json"
    text = (tmp_path / newest).read_text()
    (tmp_path / other).write_text(text[: len(text) // 2])  # a truncated later write
    again = manager_at(tmp_path)
    assert again.revision == 1 and "bms_soc" not in again.selection("deye1").effective
    assert "selection_store_corrupt" in [p.code for p in again.store.problems]


async def test_failed_rollback_never_leaves_a_running_peer_on_rejected_selection(
    tmp_path: Path,
) -> None:
    async with plant(
        tmp_path,
        endpoints=[ep("g1", port=25001), ep("g2", port=25002)],
        inverters=[inverter("deye1", "g1"), inverter("deye2", "g2")],
        banks={"g1": device_regs("2401234567"), "g2": device_regs("2401234568")},
    ) as p:
        for inv in p.rig.runtime.inverters.values():
            await inv.step()
        original = p.applier

        class FailingApplier:
            def prepare(self, *args: Any, **kw: Any) -> None:
                original.prepare(*args, **kw)

            def apply(self, iid: str, selection: Any, revision: int, **kw: Any) -> None:
                if (iid == "deye2" and revision == 1) or (iid == "deye1" and revision == 0):
                    raise RuntimeError("forward peer / rollback peer failure")
                original.apply(iid, selection, revision, **kw)

            def revoke(self, iid: str, reason: str) -> None:
                original.revoke(iid, reason)

        p.manager.attach(FailingApplier())  # type: ignore[arg-type]
        with pytest.raises(ChangeError) as caught:
            await p.manager.change(
                0, {i: Edit(remove=frozenset({"bms_soc"})) for i in ("deye1", "deye2")}
            )
        assert caught.value.key == "adoption_failed"
        first = p.rig.inv("deye1")
        for _ in range(3):
            p.rig.clock.offset += 11
            await first.step()
        assert first.state.value == "blocked" and first.reason == "selection_recovery_required"
        assert p.manager.recovery_required
        # no further change until the restart evaluates the marker
        with pytest.raises(ChangeError) as refused:
            await p.manager.change(0, {"deye2": Edit(remove=frozenset({"pv1_power"}))})
        assert refused.value.key == "selection_recovery_required"
        restart = manager_at(tmp_path)
        assert restart.revision == 0 and "bms_soc" in restart.selection("deye1").effective


# -- N4-03: every action with a possible side effect uses up the confirmation -------------------


async def test_rebind_error_after_revocation_consumes_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with client(tmp_path) as c:
        inv = c.plant.rig.inv("deye1")
        await inv.step()
        counter = c.api.action_counter
        before = inv.lifecycle_epoch
        real, calls = inv._announce, {"n": 0}

        def fail_once(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("notification failed after revocation")
            return real(*args, **kwargs)

        monkeypatch.setattr(inv, "_announce", fail_once)
        status, _ = await c.post("/api/inverters/deye1/rebind", {"confirm": True}, revision=counter)
        assert status == 500 and inv.lifecycle_epoch > before
        assert c.api.action_counter > counter
        status2, _ = await c.post(
            "/api/inverters/deye1/rebind", {"confirm": True}, revision=counter
        )
        assert status2 == 409


async def test_a_refused_rebind_keeps_the_confirmation(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        counter = c.api.action_counter
        status, _ = await c.post(
            "/api/inverters/deye1/rebind", {"confirm": False}, revision=counter
        )
        assert status == 400 and c.api.action_counter == counter


# -- N6-01..03 and the synchrony contract (follow-up of code review 6) ---------------------------


async def test_sequence_bit_corruption_is_not_silently_trusted(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(remove=frozenset({"bms_soc"}))})
    older = tmp_path / newest_slot(tmp_path)
    await m.change(1, {"deye1": Edit(remove=frozenset({"pv1_power"}))})
    doc = json.loads(older.read_text())
    doc["data"]["seq"] |= 1 << 20  # metadata corruption: payload unchanged, checksum covers seq
    older.write_text(json.dumps(doc))
    again = manager_at(tmp_path)
    assert again.revision == 2 and "pv1_power" not in again.selection("deye1").effective
    assert "selection_store_corrupt" in [p.code for p in again.store.problems]  # reported


async def test_conflicting_slots_prefer_the_higher_revision(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(remove=frozenset({"bms_soc"}))})
    await m.change(1, {"deye1": Edit(remove=frozenset({"pv1_power"}))})
    assert manager_at(tmp_path).revision == 2


def test_adoption_and_the_durable_commit_stay_synchronous() -> None:
    """Contract (code review 6 §5): between 'adopt' and 'commit or roll back' there is no await, so
    the publisher cannot send an unaccepted selection. A move to an awaited commit/applier has to
    tie the publication release to the durable acceptance point first."""
    import inspect

    from deye_monitor.app import LiveApplier
    from deye_monitor.mqtt import MqttPublisher
    from deye_monitor.selection import SelectionStore

    assert not inspect.iscoroutinefunction(SelectionStore.commit)
    assert not inspect.iscoroutinefunction(LiveApplier.apply)
    assert not inspect.iscoroutinefunction(LiveApplier.revoke)
    assert not inspect.iscoroutinefunction(MqttPublisher.set_selection)
