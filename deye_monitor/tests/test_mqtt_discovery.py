"""Discovery inventory and cleanup state machine (contract §5, §6.3, §9)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import PollConfig
from deye_monitor.ha_discovery import (
    DeviceMeta,
    Namespace,
    app_components,
    inverter_component,
)
from deye_monitor.mqtt_discovery import (
    DeviceTarget,
    DiscoveryEngine,
    DiscoveryStoreError,
    valid_discovery_document,
)
from deye_monitor.profiles.schema import load_profile
from deye_monitor.store import JsonStore

PROFILE = load_profile("sg04lp3_lv_v105_8")
NS = Namespace("ab12cd34", "homeassistant", "deye")
APP_TOPIC = NS.discovery_topic(NS.app_device_id)
INV_TOPIC = NS.discovery_topic(NS.inverter_device_id("deye1"))
STATES = ("starting", "running", "offline")


@dataclass
class Wire:
    sent: list[tuple[str, bytes, bool]] = field(default_factory=list)
    fail_at: int | None = None

    async def __call__(self, topic: str, payload: bytes, retain: bool) -> None:
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise ConnectionError("broker gone")
        self.sent.append((topic, payload, retain))

    def docs(self, topic: str) -> list[tuple[dict[str, Any], bool]]:
        return [(json.loads(p), r) for t, p, r in self.sent if t == topic and p]

    def retained(self, topic: str) -> dict[str, Any] | None:
        """What the broker would hold: the last retained message of the topic."""
        last = [p for t, p, r in self.sent if t == topic and r]
        return json.loads(last[-1]) if last and last[-1] else None


def inverter_target(
    keys: tuple[str, ...], series: str = "g0", serial: str | None = None
) -> DeviceTarget:
    comps = dict(
        inverter_component(
            NS, "deye1", PROFILE.entry(k), series=series, poll=PollConfig(), profile=PROFILE
        )
        for k in keys
    )
    meta = DeviceMeta(NS.inverter_device_id("deye1"), "Deye 1", "inverter", "deye1", serial)
    return DeviceTarget(meta, comps)


def targets(keys: tuple[str, ...], series: str = "g0") -> dict[str, DeviceTarget]:
    app = DeviceTarget(
        DeviceMeta(NS.app_device_id, "Deye Monitor", "app"),
        app_components(NS, {"deye1": "Deye 1"}, STATES),
    )
    return {APP_TOPIC: app, INV_TOPIC: inverter_target(keys, series)}


def engine(tmp_path: Path, wire: Wire) -> DiscoveryEngine:
    store = JsonStore(tmp_path / "discovery.json", validate=valid_discovery_document)
    assert not store.corrupt
    return DiscoveryEngine(NS, store, wire)


KEYS = ("battery_voltage", "battery_soc", "pv1_power")


async def test_first_sync_publishes_app_before_inverter_retained(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    report = await eng.sync(targets(KEYS), replay=True)
    assert [t for t, _, _ in wire.sent] == [APP_TOPIC, INV_TOPIC]  # via_device needs the app first
    assert all(retain for _, _, retain in wire.sent)
    assert report.documents == 2 and report.replays == 0
    doc = wire.retained(INV_TOPIC)
    assert doc is not None and sorted(doc["components"]) == sorted(KEYS)


async def test_empty_selection_creates_no_inverter_device(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(()), replay=True)
    assert [t for t, _, _ in wire.sent] == [APP_TOPIC]  # only the app diagnostics exist
    assert eng.topics() == [APP_TOPIC]


async def test_unchanged_sync_without_replay_publishes_nothing(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    wire.sent.clear()
    await eng.sync(targets(KEYS), replay=False)
    assert wire.sent == []


async def test_deselect_publishes_platform_only_tombstone_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    wire.sent.clear()
    await eng.sync(targets(("battery_voltage", "battery_soc")), replay=False)
    doc = wire.retained(INV_TOPIC)
    assert doc is not None
    assert doc["components"]["pv1_power"] == {"platform": "sensor"}  # nothing but the platform
    assert doc["components"]["battery_voltage"]["unique_id"].endswith("battery_voltage")
    assert wire.sent[0][0] == INV_TOPIC and len(wire.sent) == 1  # only the changed device
    assert "deye/ab12cd34/cleanup" not in [t for t, _, _ in wire.sent]


async def test_cold_start_cleanup_sequence_per_retired_key(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    await eng.sync(targets(("battery_soc",)), replay=False)  # retire two keys
    wire.sent.clear()
    await eng.sync(targets(("battery_soc",)), replay=True)  # reconnect
    topics = [(t, r) for t, _, r in wire.sent]
    assert topics[0] == (NS.cleanup_topic, True)  # cleanup availability: retained offline
    assert wire.sent[0][1] == b"offline"
    # app device retained, inverter retained, then per retired key: replay, tombstone, then final
    inverter = [(t, r) for t, r in topics if t == INV_TOPIC]
    assert inverter == [
        (INV_TOPIC, True),  # retained document (active + tombstones)
        (INV_TOPIC, False),  # key 1 replay (old component, template none)
        (INV_TOPIC, False),  # key 1 tombstone
        (INV_TOPIC, False),  # key 2 replay
        (INV_TOPIC, False),  # key 2 tombstone
        (INV_TOPIC, True),  # complete retained document again
    ]
    docs = wire.docs(INV_TOPIC)
    replay1, tomb1 = docs[1][0], docs[2][0]
    (key1,) = replay1["components"]
    assert list(tomb1["components"]) == [key1]  # exactly the same key
    assert tomb1["components"][key1] == {"platform": "sensor"}
    rc = replay1["components"][key1]
    assert rc["value_template"] == "{{ none }}" and rc["unique_id"].endswith(key1)
    assert {"topic": NS.cleanup_topic} in replay1["availability"]
    assert replay1["availability_mode"] == "all"
    assert "device" in replay1 and "origin" in replay1  # root fields on every replay document
    final = docs[-1][0]
    assert final["components"]["pv1_power"] == {"platform": "sensor"}
    assert final["components"]["battery_voltage"] == {"platform": "sensor"}
    assert final["components"]["battery_soc"]["unique_id"]


async def test_birth_is_a_replay_trigger_never_a_deletion_confirmation(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    await eng.sync(targets(("battery_soc",)), replay=False)
    assert eng.retired_count() == 2
    for _ in range(3):  # reconnects
        await eng.sync(targets(("battery_soc",)), replay=True, birth=False)
    for _ in range(10):  # duplicate / repeated births: no processing acknowledgement
        report = await eng.sync(targets(("battery_soc",)), replay=True, birth=True)
        assert report.replays == 2
    assert eng.retired_count() == 2
    final = wire.retained(INV_TOPIC)
    assert final is not None and final["components"]["battery_voltage"] == {"platform": "sensor"}
    # a restart still has everything to replay
    again = Wire()
    eng2 = engine(tmp_path, again)
    assert eng2.retired_count() == 2
    await eng2.sync(targets(("battery_soc",)), replay=True)
    assert any(r is False for _, _, r in again.sent)


async def test_purge_is_the_only_way_to_forget_and_is_scoped(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    await eng.sync(targets(("battery_soc",)), replay=False)
    assert eng.purge("other") == 0 and eng.retired_count() == 2  # another inverter: untouched
    assert eng.purge("deye1") == 2 and eng.retired_count() == 0
    wire.sent.clear()
    await eng.sync(targets(("battery_soc",)), replay=False, republish=True)
    doc = wire.retained(INV_TOPIC)
    assert doc is not None and list(doc["components"]) == ["battery_soc"]  # tombstones are gone
    assert eng.purge(None) == 0


async def test_ledger_limit_is_a_visible_error_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import deye_monitor.mqtt_discovery as module

    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    monkeypatch.setattr(module, "MAX_RETIRED", 1)
    before = eng.store.data
    wire.sent.clear()
    with pytest.raises(module.LedgerFullError):
        await eng.sync(targets(("battery_soc",)), replay=False)  # would retire 2 keys
    assert eng.store.data == before and wire.sent == []
    assert eng.purge(None) == 0
    await eng.sync(targets(("battery_soc", "battery_voltage")), replay=False)  # 1 retired fits


async def test_inventory_survives_restart_and_cleanup_continues(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    await eng.sync(targets(("battery_soc",)), replay=False)
    again = Wire()
    eng2 = engine(tmp_path, again)  # new process
    assert eng2.retired_count() == 2
    await eng2.sync(targets(("battery_soc",)), replay=True, birth=True)
    assert any(r is False for _, _, r in again.sent)  # replays came from the stored inventory
    doc = again.retained(INV_TOPIC)
    assert doc is not None and doc["components"]["battery_voltage"] == {"platform": "sensor"}


async def test_crash_between_cleanup_phases_repeats_idempotently(tmp_path: Path) -> None:
    ref = Wire()
    eng = engine(tmp_path / "ref", ref)
    await eng.sync(targets(KEYS), replay=True)
    await eng.sync(targets(("battery_soc",)), replay=False)
    ref.sent.clear()
    await eng.sync(targets(("battery_soc",)), replay=True, birth=True)
    full = list(ref.sent)
    # crash at every possible message of the cleanup, then restart and finish
    for crash_at in range(len(full)):
        base = tmp_path / f"c{crash_at}"
        w1 = Wire()
        e1 = engine(base, w1)
        await e1.sync(targets(KEYS), replay=True)
        await e1.sync(targets(("battery_soc",)), replay=False)
        w1.sent.clear()
        w1.fail_at = crash_at
        with pytest.raises(ConnectionError):
            await e1.sync(targets(("battery_soc",)), replay=True, birth=True)
        w2 = Wire()
        e2 = engine(base, w2)
        await e2.sync(targets(("battery_soc",)), replay=True, birth=True)
        final = w2.retained(INV_TOPIC)
        assert final is not None
        assert final["components"]["battery_soc"]["unique_id"]
        # the second run always completes the sequence for both keys
        assert sum(1 for t, _, r in w2.sent if t == INV_TOPIC and not r) == 4


async def test_unwritable_inventory_publishes_nothing(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    wire.sent.clear()
    os.chmod(tmp_path, 0o500)
    try:
        if os.access(tmp_path, os.W_OK):
            pytest.skip("directory permissions are not enforced here")
        with pytest.raises(DiscoveryStoreError):
            await eng.sync(targets(("battery_soc",)), replay=False)
    finally:
        os.chmod(tmp_path, 0o700)
    assert wire.sent == []  # persisted BEFORE publishing: no persistence, no publish
    assert eng.retired_count() == 0


async def test_reselected_key_replaces_its_tombstone(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    await eng.sync(targets(("battery_soc",)), replay=False)
    wire.sent.clear()
    await eng.sync(targets(KEYS), replay=False)
    doc = wire.retained(INV_TOPIC)
    assert doc is not None
    assert doc["components"]["pv1_power"]["unique_id"]  # active again, no tombstone
    assert eng.retired_count() == 0


async def test_removed_inverter_keeps_tombstones_until_purged_then_clears(
    tmp_path: Path,
) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    only_app = {APP_TOPIC: targets(KEYS)[APP_TOPIC]}
    wire.sent.clear()
    await eng.sync(only_app, replay=False)
    doc = wire.retained(INV_TOPIC)
    assert doc is not None and all(c == {"platform": "sensor"} for c in doc["components"].values())
    assert set(doc["components"]) == set(KEYS)  # not an empty payload: HA may be offline
    for _ in range(5):  # births never clear it
        await eng.sync(only_app, replay=True, birth=True)
    assert (INV_TOPIC, b"", True) not in wire.sent and INV_TOPIC in eng.topics()
    assert eng.purge("deye1") == len(KEYS)
    await eng.sync(only_app, replay=False)
    assert (INV_TOPIC, b"", True) in wire.sent  # cleared only after the explicit purge
    assert INV_TOPIC not in eng.topics()
    assert wire.retained(INV_TOPIC) is None


async def test_energy_generation_change_retires_old_counters_and_never_reuses_them(
    tmp_path: Path,
) -> None:
    keys = ("battery_energy_charge_total", "battery_voltage")
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(keys, "g0"), replay=True)
    wire.sent.clear()
    await eng.sync(targets(keys, "g1"), replay=False)
    doc = wire.retained(INV_TOPIC)
    assert doc is not None
    assert doc["components"]["battery_energy_charge_total_g0"] == {"platform": "sensor"}
    new = doc["components"]["battery_energy_charge_total_g1"]
    assert new["unique_id"].endswith("_g1") and "== 'g1'" in new["value_template"]
    assert doc["components"]["battery_voltage"]["unique_id"].endswith("battery_voltage")
    # the stored old counter keeps its original unique id for the cold-start replay
    stored = eng.store.data["devices"][INV_TOPIC]["retired"]["battery_energy_charge_total_g0"]
    assert stored["component"]["unique_id"].endswith("_g0")


async def test_nothing_foreign_is_ever_touched(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    other = Namespace("ff00ee11", "homeassistant", "deye")
    foreign = other.discovery_topic(other.inverter_device_id("deye1"))
    await eng.sync(targets(KEYS), replay=True)
    await eng.sync({APP_TOPIC: targets(KEYS)[APP_TOPIC]}, replay=True, birth=True)
    await eng.sync({APP_TOPIC: targets(KEYS)[APP_TOPIC]}, replay=True, birth=True)
    allowed = {APP_TOPIC, INV_TOPIC, NS.cleanup_topic}
    assert {t for t, _, _ in wire.sent} <= allowed
    assert foreign not in {t for t, _, _ in wire.sent}


async def test_prefix_change_cleans_the_old_topic_from_the_inventory(tmp_path: Path) -> None:
    wire = Wire()
    eng = engine(tmp_path, wire)
    await eng.sync(targets(KEYS), replay=True)
    moved = Namespace("ab12cd34", "ha2", "deye")
    eng2 = DiscoveryEngine(moved, eng.store, wire)
    new_app = DeviceTarget(
        DeviceMeta(moved.app_device_id, "Deye Monitor", "app"),
        app_components(moved, {"deye1": "Deye 1"}, STATES),
    )
    wire.sent.clear()
    await eng2.sync({moved.discovery_topic(moved.app_device_id): new_app}, replay=True)
    topics = {t for t, _, _ in wire.sent}
    assert APP_TOPIC in topics and INV_TOPIC in topics  # the old, stored topics are cleaned
    assert moved.discovery_topic(moved.app_device_id) in topics


def test_store_validator_rejects_foreign_content(tmp_path: Path) -> None:
    good: dict[str, Any] = {"devices": {}}
    assert valid_discovery_document(good)
    bad_command: dict[str, Any] = {
        "devices": {
            "t": {
                "meta": {"device_id": "d", "name": "n", "kind": "app"},
                "components": {
                    "x": {
                        "platform": "sensor",
                        "unique_id": "u",
                        "default_entity_id": "sensor.u",
                        "state_topic": "s",
                        "value_template": "v",
                        "command_topic": "c",
                    }
                },
                "retired": {},
                "removing": False,
            }
        }
    }
    assert not valid_discovery_document(bad_command)
    assert not valid_discovery_document({"devices": [], "x": 1})
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({"schema_version": 1, "data": bad_command}))
    assert JsonStore(path, validate=valid_discovery_document).corrupt
