"""Selection store and manager (SPEC §9.6, DR2-05/07): templates, atomic change, migration,
fault injection and live adoption."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.app import build_publisher
from deye_monitor.device import InverterState, RelaySink
from deye_monitor.mqtt import PublisherSettings
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.plan import template_selection
from deye_monitor.profiles.schema import load_profile
from deye_monitor.selection import (
    ChangeError,
    Edit,
    InverterSelection,
    Record,
    SelectionManager,
    SelectionStore,
    specs_from_config,
)
from deye_monitor.store import LoadStatus
from tests.device_helpers import config, device_regs, install_gate, rig
from tests.fakes.mqtt import FakeBroker, settle
from tests.selection_helpers import (
    A,
    newest_body,
    newest_slot,
    no_selection_files,
    plant,
    two_inverters,
)

FAST = PublisherSettings(reconnect_min_s=0.01, reconnect_max_s=0.04)
PROFILE = load_profile("sg04lp3_lv_v105_8")


def specs() -> Any:
    eps, invs = two_inverters()
    return specs_from_config(config(eps, invs))


def manager_at(directory: Path) -> SelectionManager:
    return SelectionManager(SelectionStore(directory, specs()))


def file_doc(directory: Path) -> dict[str, Any]:
    return newest_body(directory)


# -- template / effective selection (DR2-05) ----------------------------------------------------


def test_missing_file_is_the_template_with_revision_zero(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    assert m.revision == 0 and m.store.status is LoadStatus.MISSING and m.store.problems == []
    sel = m.selection("deye1")
    expected = template_selection(PROFILE, m.specs["deye1"].hardware, Features())
    assert sel.effective == sel.template == expected and "battery_soc" in expected
    assert not (tmp_path / "selection.json").exists()  # nothing is written by reading


async def test_effective_is_template_plus_added_minus_removed(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(
        0, {"deye1": Edit(add=frozenset({"gen_voltage_a"}), remove=frozenset({"battery_soc"}))}
    )
    sel = m.selection("deye1")
    assert sel.added == {"gen_voltage_a"} and sel.removed == {"battery_soc"}
    assert sel.effective == (sel.template | {"gen_voltage_a"}) - {"battery_soc"}
    assert sel.deviations == {
        "added": ["gen_voltage_a"],
        "removed": ["battery_soc"],
        "redundant": [],
    }
    assert m.selection("deye2").effective == m.selection("deye2").template  # untouched


async def test_templates_follow_features_deviations_survive(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(
        0, {"deye1": Edit(remove=frozenset({"pv1_power"}), add=frozenset({"gen_voltage_a"}))}
    )
    await m.change(1, {"deye1": Edit(features={"battery": False, "mppt2_used": False})})
    sel = m.selection("deye1")
    assert "battery_soc" not in sel.template and "pv2_power" not in sel.template
    assert "pv1_power" not in sel.effective  # the user's removal still applies
    assert "gen_voltage_a" in sel.effective
    await m.change(2, {"deye1": Edit(features={"battery": True, "mppt2_used": True})})
    assert "battery_soc" in m.selection("deye1").effective


async def test_requires_does_not_limit_access_only_the_default(tmp_path: Path) -> None:
    m = manager_at(tmp_path)  # gen_port_mode is "unused": GEN voltage is not in the template ...
    assert "gen_voltage_a" not in m.selection("deye1").template
    await m.change(0, {"deye1": Edit(add=frozenset({"gen_voltage_a"}))})  # ... but may be chosen
    assert "gen_voltage_a" in m.selection("deye1").effective
    await m.change(1, {"deye1": Edit(features={"gen_port_mode": "generator"})})
    sel = m.selection("deye1")
    assert "gen_voltage_a" in sel.template and sel.deviations["redundant"] == ["gen_voltage_a"]


async def test_reset_and_restore_template(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(
        0, {"deye1": Edit(remove=frozenset({"battery_soc"}), add=frozenset({"gen_voltage_a"}))}
    )
    await m.change(1, {"deye1": Edit(reset=frozenset({"battery_soc"}))})
    sel = m.selection("deye1")
    assert "battery_soc" in sel.effective and sel.removed == frozenset()
    assert sel.added == {"gen_voltage_a"}
    await m.change(2, {"deye1": Edit(features={"battery": False}, restore_template=True)})
    sel = m.selection("deye1")
    assert sel.added == sel.removed == frozenset() and sel.effective == sel.template
    assert sel.features.battery is False  # restoring the template keeps the plant features


async def test_placeholders_unsupported_and_internal_can_never_be_enabled(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    placeholder = next(e.key for e in PROFILE.entries if e.support_status == "placeholder")
    internal = next((e.key for e in PROFILE.entries if e.internal), None)
    for key in [placeholder, *([internal] if internal else [])]:
        with pytest.raises(ChangeError) as err:
            await m.change(0, {"deye1": Edit(add=frozenset({key}))})
        assert err.value.key == "unknown_entry"  # not even offered
    # a record that smuggles one in (hand-edited document) is rejected by resolve()
    with pytest.raises(ChangeError) as err:
        m.dry_run(
            {"deye1": Edit(replace=Record(Features(), frozenset({placeholder}), frozenset()))}
        )
    assert err.value.key == "unknown_entry" or err.value.key == "entry_not_selectable"
    assert m.revision == 0 and m.selection("deye1").effective == m.selection("deye1").template


async def test_unresolved_only_as_raw_diagnostic(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    raw = next(e for e in PROFILE.entries if e.is_raw_diagnostic and not e.default_enabled)
    await m.change(0, {"deye1": Edit(add=frozenset({raw.key}))})
    assert raw.key in m.selection("deye1").effective
    assert raw.unit is None and raw.state_class is None  # clearly a raw value, no physical unit


async def test_unknown_and_malformed_edits_change_nothing(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    for edit in (
        Edit(add=frozenset({"no_such_sensor"})),
        Edit(remove=frozenset({"battery_soc", "no_such_sensor"})),
        Edit(features={"allow_writes": True}),
        Edit(features={"battery": "yes"}),
        Edit(features={"mppt1_used": 1}),  # type: ignore[dict-item]  # 1 == True, but not a bool
        Edit(features={"gen_port_mode": "nuclear"}),
    ):
        with pytest.raises(ChangeError):
            await m.change(0, {"deye1": edit})
    with pytest.raises(ChangeError) as err:
        await m.change(0, {"ghost": Edit(restore_template=True)})
    assert err.value.key == "unknown_inverter"
    assert m.revision == 0 and not (tmp_path / "selection.json").exists()


async def test_no_op_change_keeps_the_revision(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    result = await m.change(0, {"deye1": Edit(restore_template=True)})
    assert result.revision == 0 and result.changed == ()
    assert not (tmp_path / "selection.json").exists()


# -- revisions, concurrency -----------------------------------------------------------------


async def test_stale_revision_is_rejected_and_revision_is_monotone(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    assert (await m.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})).revision == 1
    with pytest.raises(ChangeError) as err:
        await m.change(0, {"deye1": Edit(remove=frozenset({"pv1_power"}))})
    assert err.value.key == "stale_revision" and err.value.status == 409
    assert err.value.params == {"current": 1}
    assert (await m.change(1, {"deye2": Edit(remove=frozenset({"pv1_power"}))})).revision == 2
    assert file_doc(tmp_path)["revision"] == 2


async def test_concurrent_changes_exactly_one_wins(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    results = await asyncio.gather(
        *(
            m.change(0, {"deye1": Edit(remove=frozenset({k}))})
            for k in ("battery_soc", "pv1_power")
        ),
        return_exceptions=True,
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    stale = [r for r in results if isinstance(r, ChangeError)]
    assert len(wins) == 1 and len(stale) == 1 and stale[0].key == "stale_revision"
    assert m.revision == 1
    assert len(m.selection("deye1").removed) == 1


# -- persistence / fault injection ----------------------------------------------------------


async def test_restart_restores_the_selection(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(
        0, {"deye1": Edit(remove=frozenset({"battery_soc"}), features={"parallel": True})}
    )
    again = manager_at(tmp_path)
    assert again.revision == 1 and again.store.problems == [] and again.store.source == "file"
    assert again.selection("deye1") == m.selection("deye1")
    assert again.selection("deye2").effective == again.selection("deye2").template


class Boom(BaseException):
    """Stands for the end of the process (not an Exception: nothing may swallow it)."""


@pytest.mark.parametrize("fail_on", [1, 2, 3, 4, 5, 6, 7, 8])
async def test_process_end_in_any_step_leaves_old_or_new_never_a_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on: int
) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(add=frozenset({"gen_voltage_a"}))})  # a valid file exists
    old = m.selection("deye1")
    calls = {"n": 0}

    def tick(real: Any) -> Any:
        def wrapper(*a: Any, **kw: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == fail_on:
                raise Boom
            return real(*a, **kw)

        return wrapper

    monkeypatch.setattr("deye_monitor.store.os.fsync", tick(os.fsync))
    monkeypatch.setattr("deye_monitor.store.os.replace", tick(os.replace))
    monkeypatch.setattr("deye_monitor.store.tempfile.mkstemp", tick(tempfile.mkstemp))
    with contextlib.suppress(Boom):
        await m.change(1, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
    monkeypatch.undo()
    reloaded = manager_at(tmp_path)  # the next process start
    sel = reloaded.selection("deye1")
    new_expected = (old.effective) - {"battery_soc"}
    assert (reloaded.revision, sel.effective) in (
        (1, old.effective),
        (2, new_expected),
    )
    # an unfinished transaction is rolled back to the accepted state and reported; a finished one
    # (marker closed) loads without any finding
    codes = [p.code for p in reloaded.store.problems]
    assert codes in ([], ["selection_recovered_interrupted_change"])
    assert reloaded.revision == 1 or codes == []
    assert not list(tmp_path.glob(".selection.json.*.*")) or reloaded.store.problems == []


async def test_write_failure_keeps_old_selection_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant(tmp_path) as p:
        m, inv = p.manager, p.rig.inv("deye1")
        before = (m.revision, m.selection("deye1"), inv.selection, inv.selection_revision)

        def refuse(*_: Any, **__: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("deye_monitor.store.os.replace", refuse)
        with pytest.raises(ChangeError) as err:
            await m.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
        assert err.value.key == "persist_failed"
        assert (m.revision, m.selection("deye1"), inv.selection, inv.selection_revision) == before
        monkeypatch.undo()
        assert no_selection_files(tmp_path)  # nothing written, no temporary file left behind
        await m.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})  # recovers
        assert "battery_soc" not in inv.selection and inv.selection_revision == 1


def test_stale_temporary_files_are_removed(tmp_path: Path) -> None:
    stale = tmp_path / ".selection.json.abc123"
    stale.write_text("{")
    manager_at(tmp_path)
    assert not stale.exists()


# -- damaged / old documents ----------------------------------------------------------------


async def test_corrupt_file_reports_uses_template_and_is_kept_on_next_change(
    tmp_path: Path,
) -> None:
    (tmp_path / "selection.a.json").write_text("{not json")
    m = manager_at(tmp_path)
    codes = [p.code for p in m.store.problems]
    assert m.store.status is LoadStatus.CORRUPT and m.store.source == "template"
    assert "selection_store_corrupt" in codes and "selection_store_template_used" in codes
    assert m.selection("deye1").effective == m.selection("deye1").template
    assert (tmp_path / "selection.a.json").read_text() == "{not json"  # reading never replaces it
    await m.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
    assert (tmp_path / "selection.a.json.corrupt").read_text() == "{not json"
    assert manager_at(tmp_path).store.problems == []


async def test_a_damaged_newest_slot_falls_back_to_the_last_accepted_document(
    tmp_path: Path,
) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
    await m.change(1, {"deye1": Edit(remove=frozenset({"pv1_power"}))})
    (tmp_path / newest_slot(tmp_path)).write_text("garbage")
    again = manager_at(tmp_path)
    assert again.store.source == "recovered_previous"
    assert "selection_store_recovered_previous" in [p.code for p in again.store.problems]
    assert again.selection("deye1").removed == {"battery_soc"} and again.revision == 1


async def test_a_slot_with_a_wrong_checksum_is_never_used(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
    path = tmp_path / newest_slot(tmp_path)
    doc = json.loads(path.read_text())
    doc["data"]["body"]["revision"] = 7  # content changed behind the checksum
    path.write_text(json.dumps(doc))
    again = manager_at(tmp_path)
    assert again.store.status is LoadStatus.CORRUPT and again.revision == 0


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"schema_version": 2, "data": {"revision": 1, "inverters": {}}}),
        json.dumps({"schema_version": 1, "data": {"revision": -1, "inverters": {}}}),
        json.dumps({"schema_version": 1, "data": {"revision": True, "inverters": {}}}),
        json.dumps({"schema_version": 1, "data": {"revision": 1, "inverters": []}}),
        json.dumps([1, 2]),
        "",
    ],
)
def test_unusable_documents_are_corrupt_never_silently_dropped(tmp_path: Path, text: str) -> None:
    (tmp_path / "selection.json").write_text(text)
    m = manager_at(tmp_path)
    assert m.store.status is LoadStatus.CORRUPT
    assert {"selection_store_corrupt", "selection_store_template_used"} <= {
        p.code for p in m.store.problems
    }


def test_migration_reports_and_discards_unknown_keys(tmp_path: Path) -> None:
    placeholder = next(e.key for e in PROFILE.entries if e.support_status == "placeholder")
    doc = {
        "revision": 4,
        "inverters": {
            "deye1": {
                "features": {"battery": False, "legacy_flag": True, "bms": "maybe"},
                "added": ["gen_voltage_a", "retired_sensor", placeholder, 7],
                "removed": ["battery_soc", "gen_voltage_a"],
                "old_field": 1,
            },
            "gone": {"features": {}, "added": [], "removed": []},
        },
    }
    (tmp_path / "selection.json").write_text(json.dumps({"schema_version": 1, "data": doc}))
    m = manager_at(tmp_path)
    codes = {(p.code, p.subject) for p in m.store.problems}
    assert ("unknown_feature", "deye1.legacy_flag") in codes
    assert ("invalid_feature_value", "deye1.bms") in codes
    assert ("unknown_entry", "deye1.retired_sensor") in codes
    assert ("unknown_entry", f"deye1.{placeholder}") in codes
    assert ("unknown_entry", "deye1.7") in codes
    assert ("conflicting_entries", "deye1") in codes
    assert ("unknown_field", "deye1.old_field") in codes
    assert ("unknown_inverter", "gone") in codes
    sel = m.selection("deye1")
    assert m.revision == 4 and sel.features.battery is False and sel.features.bms is False
    assert sel.added == frozenset() and sel.removed == {"battery_soc", "gen_voltage_a"}
    assert placeholder not in sel.effective


async def test_unsupported_status_is_not_stored_back(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(add=frozenset({"gen_voltage_a"}))})
    assert file_doc(tmp_path)["inverters"]["deye1"]["added"] == ["gen_voltage_a"]
    assert "allow_writes" not in json.dumps(file_doc(tmp_path))


# -- live adoption --------------------------------------------------------------------------


async def test_change_reaches_runtime_and_other_inverters_stay(tmp_path: Path) -> None:
    async with plant(tmp_path) as p:
        one, two = p.rig.inv("deye1"), p.rig.inv("deye2")
        epoch2 = two.lifecycle_epoch
        result = await p.manager.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
        assert result.selection_changed == ("deye1",)
        assert "battery_soc" not in one.selection and one.selection_revision == 1
        assert "battery_soc" in two.selection and two.selection_revision == 0
        assert two.lifecycle_epoch == epoch2  # an unchanged inverter is not disturbed
        await p.manager.change(1, {"deye1": Edit(features={"parallel": True})})
        assert one.features.parallel is True and one.selection_revision == 1  # features only
        assert p.rig.inv("deye1").current_plan().budget.utilisation > 0


async def test_runtime_starts_from_the_stored_selection(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye2": Edit(remove=frozenset({"battery_soc"}))})
    async with plant(tmp_path) as p:
        assert p.manager.revision == 1
        assert "battery_soc" not in p.rig.inv("deye2").selection
        assert p.rig.inv("deye2").selection_revision == 1
        assert "battery_soc" in p.rig.inv("deye1").selection


async def test_removal_during_a_running_read_never_publishes_the_old_value(tmp_path: Path) -> None:
    async with plant(tmp_path) as p:
        r, inv = p.rig, p.rig.inv("deye1")
        await inv.step()
        assert inv.state is InverterState.RUNNING
        r.clock.offset += 11
        proxy = install_gate(r, "deye1")
        entered, release = proxy.gate(586)  # the battery group is in flight
        task = asyncio.create_task(inv.step())
        await asyncio.wait_for(entered.wait(), 2)
        before = len(r.sink.states)
        await p.manager.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
        release.set()
        await task
        proxy.gates.clear()
        for _ in range(3):
            await inv.step()
            r.clock.offset += 11
        later = r.sink.states[before:]
        assert not [1 for _, _, v in later if "battery_soc" in v]
        assert all(s.selection_revision == 1 for _, s, _ in later)


async def test_publisher_follows_and_removes_only_its_own_component(tmp_path: Path) -> None:
    relay = RelaySink()
    eps, invs = two_inverters()
    store = SelectionStore(tmp_path, specs_from_config(config(eps, invs)))
    manager = SelectionManager(store)
    init = manager.initial()
    async with rig(
        eps,
        invs,
        {"gw1": device_regs(A, r588=55)},
        tmp_path,
        sink=relay,  # type: ignore[arg-type]
        runtime_kw={
            "features": {i: f for i, (f, _, _) in init.items()},
            "selections": {i: (k, rev) for i, (_, k, rev) in init.items()},
        },
    ) as r:
        broker = FakeBroker()
        pub = build_publisher(
            r.cfg,
            r.runtime,
            r.cfg.mqtt.model_copy(update={"host": "127.0.0.1"}),
            "ab12cd34",
            factory=broker,
            directory=tmp_path / "pub",
            settings=FAST,
        )
        relay.target = pub
        from deye_monitor.app import LiveApplier

        manager.attach(LiveApplier(r.runtime, pub))
        task = asyncio.create_task(pub.run())
        conn = await broker.wait_connections(1)
        topic1 = pub.ns.discovery_topic(pub.ns.inverter_device_id("deye1"))
        topic2 = pub.ns.discovery_topic(pub.ns.inverter_device_id("deye2"))
        await settle(lambda: conn.on(topic1) and conn.on(topic2))

        def keys(topic: str) -> set[str]:
            comps = conn.last(topic).json().get("components", {})
            return {k for k, v in comps.items() if set(v) != {"platform"}}  # not a tombstone

        assert "battery_soc" in keys(topic1) and "battery_soc" in keys(topic2)
        mark = len(conn.sent)
        await manager.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
        await settle(lambda: "battery_soc" not in keys(topic1))
        assert "pv1_power" in keys(topic1)
        assert "battery_soc" in keys(topic2)  # the neighbour keeps its component
        touched = {s.topic for s in conn.sent[mark:] if s.retain}
        assert all(
            t.startswith(("homeassistant/device/", "homeassistant/sensor/")) for t in touched
        )
        assert topic2 not in touched
        # empty selection: no inverter entities left, the status view data stays available
        sel = manager.selection("deye1")
        await manager.change(
            1, {"deye1": Edit(replace=Record(sel.features, frozenset(), sel.template))}
        )
        await settle(
            lambda: not {k for k in keys(topic1) if k not in {"inverter_state", "inverter_reason"}}
        )
        assert r.inv("deye1").selection == frozenset()
        assert r.inv("deye1").diagnostics()["selection_revision"] == 2
        pub.request_stop()
        await asyncio.wait_for(task, 5)


def test_selection_dataclass_exports() -> None:
    sel = InverterSelection(Features(), frozenset(), frozenset(), frozenset(), frozenset())
    assert sel.deviations == {"added": [], "removed": [], "redundant": []}


# -- confirmed features (R7-04) -----------------------------------------------------------------


async def test_an_answer_equal_to_the_default_is_stored_as_confirmed(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    assert m.selection("deye1").confirmed == frozenset()
    result = await m.change(0, {"deye1": Edit(confirm=frozenset({"battery"}))})
    assert result.revision == 1  # a confirmation alone is a change
    assert m.selection("deye1").confirmed == frozenset({"battery"})
    assert m.selection("deye1").features == Features()
    assert file_doc(tmp_path)["inverters"]["deye1"]["confirmed"] == ["battery"]
    again = manager_at(tmp_path)
    assert again.selection("deye1").confirmed == frozenset({"battery"})
    assert again.store.problems == []


async def test_changed_features_are_confirmed_and_transfer_keeps_the_targets_answers(
    tmp_path: Path,
) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(features={"parallel": True})})
    assert m.selection("deye1").confirmed == frozenset({"parallel"})
    source = m.selection("deye1")
    await m.change(
        1, {"deye2": Edit(replace=Record(source.features, source.added, source.removed))}
    )
    assert m.selection("deye2").features.parallel is True
    assert m.selection("deye2").confirmed == frozenset()  # the answers of the source are not copied


async def test_unknown_confirmation_is_rejected_and_changes_nothing(tmp_path: Path) -> None:
    m = manager_at(tmp_path)
    with pytest.raises(ChangeError) as caught:
        await m.change(0, {"deye1": Edit(confirm=frozenset({"nonsense"}))})
    assert caught.value.key == "unknown_feature" and m.revision == 0


async def test_old_files_without_confirmations_treat_non_default_values_as_answered(
    tmp_path: Path,
) -> None:
    m = manager_at(tmp_path)
    await m.change(0, {"deye1": Edit(features={"battery": False})})
    body = newest_body(tmp_path)
    del body["inverters"]["deye1"]["confirmed"]  # a file from before the confirmations
    for slot in tmp_path.glob("selection.*.json"):
        slot.unlink()
    (tmp_path / "selection.json").write_text(json.dumps({"schema_version": 1, "data": body}))
    loaded = manager_at(tmp_path)
    assert loaded.store.problems == [], loaded.store.problems
    assert loaded.selection("deye1").confirmed == frozenset({"battery"})
    assert loaded.selection("deye2").confirmed == frozenset()
