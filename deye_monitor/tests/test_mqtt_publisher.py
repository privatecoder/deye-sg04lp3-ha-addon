"""MqttPublisher against an in-memory connection: ordering, availability, staleness, reconnect."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import PollConfig
from deye_monitor.device import InverterState, Snapshot
from deye_monitor.ha_discovery import Namespace
from deye_monitor.mqtt import InverterInfo, MqttPublisher, PublisherSettings
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.profiles.schema import load_profile
from deye_monitor.store import JsonStore
from tests.fakes.mqtt import FakeBroker, FakeConnection, mqtt_error, settle

PROFILE = load_profile("sg04lp3_lv_v105_8")
NS = Namespace("ab12cd34", "homeassistant", "deye")
KEYS = ("battery_voltage", "battery_soc", "operating_state", "alarm_fan")
ENERGY = "battery_energy_charge_total"
FAST = PublisherSettings(
    reconnect_min_s=0.01, reconnect_max_s=0.04, publish_timeout_s=1.0, shutdown_deadline_s=1.0
)


def snap(
    group: str = "battery",
    *,
    cycle: int = 1,
    revision: int = 0,
    epoch: int = 1,
    series: str = "g0",
    failed: bool = False,
) -> Snapshot:
    return Snapshot(
        cycle_id=cycle,
        group=group,
        monotonic_ts=1.0,
        wall_ts=1_700_000_000.0,
        endpoint_id="gw1",
        words={},
        missing=frozenset(),
        selection_revision=revision,
        lifecycle_epoch=epoch,
        energy_series=series,
        failed=failed,
    )


@dataclass
class Rig:
    pub: MqttPublisher
    broker: FakeBroker
    store: JsonStore
    sleeps: list[float] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None

    @property
    def conn(self) -> FakeConnection:
        return self.broker.current

    async def ready(self, n: int = 1) -> FakeConnection:
        conn = await self.broker.wait_connections(n)
        topic = self.pub.ns.app_status_topic
        await settle(lambda: bool(conn.on(topic)) and conn.last(topic).text == "online")
        return conn

    async def idle(self) -> None:
        """Let the pump drain everything that is pending."""
        for _ in range(20):
            await asyncio.sleep(0.002)


@contextlib.asynccontextmanager
async def make(
    tmp_path: Path,
    *,
    keys: tuple[str, ...] = KEYS,
    ns: Namespace = NS,
    broker: FakeBroker | None = None,
    start: bool = True,
    **kw: Any,
) -> AsyncIterator[Rig]:
    broker = broker or FakeBroker()
    store = JsonStore(tmp_path / "discovery.json", validate=valid_discovery_document)
    sleeps: list[float] = []
    refreshed: list[str] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        await asyncio.sleep(0)

    pub = MqttPublisher(
        ns,
        [InverterInfo("deye1", "Deye 1", PROFILE, PollConfig())],
        store,
        broker,
        selections={"deye1": (keys, 0)},
        request_refresh=refreshed.append,
        settings=kw.pop("settings", FAST),
        rand=kw.pop("rand", lambda: 0.0),
        sleep=sleep,
        **kw,
    )
    rig = Rig(pub, broker, store, sleeps, refreshed)
    if start:
        rig.task = asyncio.create_task(pub.run())
    try:
        yield rig
    finally:
        pub.request_stop()
        if rig.task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(rig.task, 5)


def running(rig: Rig) -> None:
    rig.pub.on_availability("deye1", InverterState.RUNNING, "identified, data flowing")


def state_topic(group: str) -> str:
    return NS.group_topic("deye1", group)


async def test_session_order_offline_subscribe_discovery_then_app_online(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        topics = conn.topics()
        avail = NS.inverter_availability_topic("deye1")
        assert topics[0] == avail and conn.sent[0].text == "offline" and conn.sent[0].retain
        app_doc = topics.index(NS.discovery_topic(NS.app_device_id))
        inv_doc = topics.index(NS.discovery_topic(NS.inverter_device_id("deye1")))
        online = topics.index(NS.app_status_topic)
        assert 0 < app_doc < inv_doc < online
        assert all(s.qos == 1 for s in conn.sent)
        docs = [s for s in conn.sent if "/config" in s.topic]
        assert docs and all(s.retain for s in docs)
        assert conn.subscriptions == [("homeassistant/status", 1)]  # the only subscription
        assert rig.refreshed == ["deye1"]


async def test_never_publishes_outside_its_own_topics_or_to_command_topics(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap(), {"battery_voltage": 52.1})
        await rig.idle()
        for sent in conn.sent:
            assert sent.topic.startswith(("deye/ab12cd34/", "homeassistant/device/deye_ab12cd34_"))
            assert not sent.topic.endswith(("/set", "/command", "/cmd"))
        assert all(topic == "homeassistant/status" for topic, _ in conn.subscriptions)


async def test_inverter_goes_online_only_after_identity_and_a_fresh_state(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        avail = NS.inverter_availability_topic("deye1")
        running(rig)
        await rig.idle()
        assert conn.last(avail).text == "offline"  # identified, but no data released yet
        rig.pub.on_state("deye1", snap(), {"battery_voltage": 52.1})
        await settle(lambda: conn.last(avail).text == "online")
        assert conn.last(avail).retain
        # the state precedes the availability that releases it
        assert conn.topics().index(state_topic("battery")) < len(conn.topics()) - 1


async def test_failed_snapshot_publishes_null_and_does_not_release_online(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        avail = NS.inverter_availability_topic("deye1")
        running(rig)
        rig.pub.on_state("deye1", snap(failed=True), {"battery_voltage": None, "battery_soc": None})
        await settle(lambda: conn.on(state_topic("battery")))
        body = conn.last(state_topic("battery")).json()
        assert body["battery_voltage"] is None and body["battery_soc"] is None  # timeout: unknown
        assert not conn.last(state_topic("battery")).retain
        assert conn.last(avail).text == "offline"


async def test_offline_event_drops_queued_states_and_withdraws_online(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        avail = NS.inverter_availability_topic("deye1")
        running(rig)
        rig.pub.on_state("deye1", snap(), {"battery_voltage": 52.1})
        await settle(lambda: conn.last(avail).text == "online")
        rig.pub.on_availability("deye1", InverterState.OFFLINE, "3 failed cycles")
        rig.pub.on_state("deye1", snap(cycle=2), {"battery_voltage": 99.0})  # queued, then...
        rig.pub.on_availability("deye1", InverterState.QUARANTINED, "serial differs")
        await settle(lambda: conn.last(avail).text == "offline")
        await rig.idle()
        assert [s.json()["battery_voltage"] for s in conn.on(state_topic("battery"))] == [52.1]


async def test_state_payload_is_json_with_cycle_timestamp_and_nulls(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state(
            "deye1",
            snap("status", cycle=9),
            {"operating_state": "unknown_99", "alarm_fan": False},
        )
        await settle(lambda: conn.on(state_topic("status")))
        sent = conn.last(state_topic("status"))
        assert sent.qos == 1 and not sent.retain
        body = sent.json()
        assert body["cycle_id"] == 9 and body["timestamp"] == "2023-11-14T22:13:20Z"
        assert body["operating_state"] is None and body["alarm_fan"] is False
        assert "battery_voltage" not in body  # other groups' keys are not carried over


async def test_unselected_keys_are_never_published(tmp_path: Path) -> None:
    async with make(tmp_path, keys=("battery_voltage",)) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap(), {"battery_voltage": 1.0, "battery_soc": 5})
        await settle(lambda: conn.on(state_topic("battery")))
        assert "battery_soc" not in conn.last(state_topic("battery")).json()
        doc = json.loads(conn.on(NS.discovery_topic(NS.inverter_device_id("deye1")))[-1].payload)
        assert list(doc["components"]) == ["battery_voltage"]


async def test_empty_selection_creates_no_inverter_entities_only_app_diagnostics(
    tmp_path: Path,
) -> None:
    async with make(tmp_path, keys=()) as rig:
        conn = await rig.ready()
        assert NS.discovery_topic(NS.inverter_device_id("deye1")) not in conn.topics()
        app = json.loads(conn.on(NS.discovery_topic(NS.app_device_id))[-1].payload)
        assert "inverter_deye1_state" in app["components"]
        running(rig)  # nothing selected: online follows identity alone
        await settle(lambda: conn.last(NS.inverter_availability_topic("deye1")).text == "online")


# -- stale rejection -------------------------------------------------------------------------------


async def test_old_selection_revision_is_rejected(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.set_selection("deye1", ("battery_voltage",), 3)
        rig.pub.on_state("deye1", snap(revision=2), {"battery_voltage": 1.0})  # queued before
        rig.pub.on_state("deye1", snap(cycle=2, revision=3), {"battery_voltage": 2.0})
        await settle(lambda: conn.on(state_topic("battery")))
        await rig.idle()
        assert [s.json()["battery_voltage"] for s in conn.on(state_topic("battery"))] == [2.0]
        assert rig.pub.dropped_stale == 1


async def test_a_discovery_change_is_followed_by_one_extra_refresh_after_the_settle_time(
    tmp_path: Path,
) -> None:
    """A slow group (identity, hourly) must not stay unknown because its first message was sent
    before Home Assistant had subscribed the new entity: once more, shortly after the discovery."""
    fast_settle = PublisherSettings(
        reconnect_min_s=0.01,
        reconnect_max_s=0.04,
        publish_timeout_s=1.0,
        shutdown_deadline_s=1.0,
        discovery_settle_s=0.05,
    )
    async with make(tmp_path, settings=fast_settle) as rig:
        await rig.ready()
        running(rig)
        await rig.idle()
        before = list(rig.refreshed)
        rig.pub.set_selection("deye1", ("battery_voltage", "serial_number"), 1)
        await settle(lambda: len(rig.refreshed) == len(before) + 1)
        await rig.idle()
        assert rig.refreshed == [*before, "deye1"]  # exactly one extra refresh, not a repeating one


async def test_deselection_publishes_a_tombstone_before_new_states(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.set_selection("deye1", ("battery_voltage",), 1)
        rig.pub.on_state("deye1", snap(revision=1), {"battery_voltage": 3.0})
        await settle(lambda: conn.on(state_topic("battery")))
        docs = conn.on(NS.discovery_topic(NS.inverter_device_id("deye1")))
        assert docs[-1].json()["components"]["battery_soc"] == {"platform": "sensor"}
        assert conn.topics().index(state_topic("battery")) > conn.topics().index(docs[-1].topic)


async def test_lower_lifecycle_epoch_is_rejected(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap(epoch=5), {"battery_voltage": 1.0})
        await settle(lambda: conn.on(state_topic("battery")))
        rig.pub.on_state("deye1", snap(cycle=2, epoch=4), {"battery_voltage": 9.0})  # old lifecycle
        rig.pub.on_diagnostics("deye1", {"lifecycle_epoch": 6})  # e.g. a rebind happened
        rig.pub.on_state("deye1", snap(cycle=3, epoch=5), {"battery_voltage": 8.0})  # in flight
        rig.pub.on_state("deye1", snap(cycle=4, epoch=6), {"battery_voltage": 7.0})
        await settle(lambda: len(conn.on(state_topic("battery"))) == 2)
        await rig.idle()
        values = [s.json()["battery_voltage"] for s in conn.on(state_topic("battery"))]
        assert values == [1.0, 7.0]
        assert rig.pub.dropped_stale == 2


async def test_energy_generation_change_publishes_discovery_before_the_new_values(
    tmp_path: Path,
) -> None:
    async with make(tmp_path, keys=(ENERGY, "battery_voltage")) as rig:
        conn = await rig.ready()
        running(rig)
        topic = NS.discovery_topic(NS.inverter_device_id("deye1"))
        first = json.loads(conn.on(topic)[-1].payload)
        assert f"{ENERGY}_g0" in first["components"]
        rig.pub.on_state("deye1", snap("energy", series="g1"), {ENERGY: 12.5})
        await settle(lambda: conn.on(state_topic("energy")))
        doc = conn.on(topic)[-1].json()
        assert doc["components"][f"{ENERGY}_g0"] == {"platform": "sensor"}
        assert "== 'g1'" in doc["components"][f"{ENERGY}_g1"]["value_template"]
        assert conn.topics().index(state_topic("energy")) > conn.topics().index(topic, 5)
        body = conn.last(state_topic("energy")).json()
        assert body["energy_series"] == "g1"
        assert body[ENERGY] == 12.5


async def test_energy_snapshot_of_an_older_generation_is_not_published(tmp_path: Path) -> None:
    async with make(tmp_path, keys=(ENERGY, "battery_voltage")) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap("energy", series="g2"), {ENERGY: 1.0})
        await settle(lambda: conn.on(state_topic("energy")))
        rig.pub.on_state("deye1", snap("energy", cycle=2, series="g0"), {ENERGY: 99.0})  # retired
        await rig.idle()
        assert [s.json()["energy_series"] for s in conn.on(state_topic("energy"))] == ["g2"]
        # other groups are not blocked by the energy generation
        rig.pub.on_state("deye1", snap("battery", cycle=3, series="g0"), {"battery_voltage": 5.0})
        await settle(lambda: conn.on(state_topic("battery")))


async def test_generation_published_before_is_never_used_again(tmp_path: Path) -> None:
    async with make(tmp_path, keys=(ENERGY,)) as rig:
        conn = await rig.ready()
        rig.pub.on_state("deye1", snap("energy", series="g3"), {ENERGY: 1.0})
        await settle(lambda: conn.on(state_topic("energy")))
    # restart: the energy store reports 0 (reset before confirmation) but g3 was published
    async with make(tmp_path, keys=(ENERGY,), series_of=lambda _i: "g0") as rig2:
        conn2 = await rig2.ready()
        topic = NS.discovery_topic(NS.inverter_device_id("deye1"))
        doc = conn2.on(topic)[0].json()
        assert f"{ENERGY}_g3" in doc["components"]
        assert f"{ENERGY}_g0" not in doc["components"] or doc["components"][f"{ENERGY}_g0"] == {
            "platform": "sensor"
        }


async def test_crash_after_generation_persist_before_discovery_continues_that_generation(
    tmp_path: Path,
) -> None:
    # energy.json already says generation 4 (persisted), discovery.json only knows g0
    async with make(tmp_path, keys=(ENERGY,)) as rig:
        await rig.ready()
    async with make(tmp_path, keys=(ENERGY,), series_of=lambda _i: "g4") as rig2:
        conn = await rig2.ready()
        topic = NS.discovery_topic(NS.inverter_device_id("deye1"))
        doc = conn.on(topic)[0].json()
        assert doc["components"][f"{ENERGY}_g4"]["unique_id"].endswith("_g4")
        assert doc["components"][f"{ENERGY}_g0"] == {"platform": "sensor"}  # never back to g0


# -- reconnect, birth, shutdown ------------------------------------------------------------------


async def test_reconnect_uses_a_new_connection_and_discards_old_measurements(
    tmp_path: Path,
) -> None:
    async with make(tmp_path) as rig:
        first = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap(), {"battery_voltage": 1.0})
        await settle(lambda: first.on(state_topic("battery")))
        first.fail_publish = lambda s: mqtt_error("gone") if s.topic.endswith("/battery") else None
        rig.pub.on_state("deye1", snap(cycle=2), {"battery_voltage": 2.0})  # triggers the failure
        second = await rig.ready(2)
        assert second is not first and second.number == 2 and first.closed
        await rig.idle()
        assert second.on(state_topic("battery")) == []  # the failed, older state is not replayed
        assert second.sent[0].topic == NS.inverter_availability_topic("deye1")
        assert second.sent[0].text == "offline"
        assert second.subscriptions == [("homeassistant/status", 1)]
        assert rig.refreshed == ["deye1", "deye1"]  # fresh cycles requested again
        assert rig.pub.reconnects == 1
        # online again only after a fresh measurement on the new connection
        avail = NS.inverter_availability_topic("deye1")
        assert second.last(avail).text == "offline"
        rig.pub.on_state("deye1", snap(cycle=3), {"battery_voltage": 3.0})
        await settle(lambda: second.last(avail).text == "online")


async def test_broker_drop_triggers_reconnect_with_growing_backoff(tmp_path: Path) -> None:
    broker = FakeBroker()
    broker.connect_errors = [mqtt_error("refused") for _ in range(3)]
    async with make(tmp_path, broker=broker) as rig:
        await rig.ready()
        assert rig.sleeps[:3] == [0.01, 0.02, 0.04]  # exponential, capped at reconnect_max_s
        assert rig.pub.reconnects == 3


async def test_backoff_has_jitter_and_a_cap(tmp_path: Path) -> None:
    broker = FakeBroker()
    broker.connect_errors = [mqtt_error("x") for _ in range(6)]
    async with make(tmp_path, broker=broker) as rig:
        await rig.ready()
        assert max(rig.sleeps) <= FAST.reconnect_max_s


async def test_dropped_message_stream_ends_the_session(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        first = await rig.ready()
        first.drop()
        second = await rig.ready(2)
        assert second.number == 2


async def test_ha_birth_republishes_discovery_requests_fresh_cycles_and_replays_no_cache(
    tmp_path: Path,
) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap(), {"battery_voltage": 1.0})
        await settle(lambda: conn.on(state_topic("battery")))
        docs_before = len(conn.on(NS.discovery_topic(NS.inverter_device_id("deye1"))))
        states_before = len(conn.on(state_topic("battery")))
        rig.pub._slots[("deye1", "battery")] = _pending_slot()  # an old measurement still queued
        conn.birth()
        await settle(
            lambda: len(conn.on(NS.discovery_topic(NS.inverter_device_id("deye1")))) > docs_before
        )
        await rig.idle()
        assert rig.refreshed == ["deye1", "deye1"]
        assert len(conn.on(state_topic("battery"))) == states_before  # nothing replayed
        rig.pub.on_state("deye1", snap(cycle=5), {"battery_voltage": 7.0})  # a fresh cycle
        await settle(lambda: len(conn.on(state_topic("battery"))) == states_before + 1)


def _pending_slot() -> Any:
    from deye_monitor.mqtt import _Slot

    return _Slot(snap(cycle=99), {"battery_voltage": 0.1})


async def test_foreign_status_messages_are_ignored(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        from deye_monitor.mqtt import IncomingMessage

        n = len(conn.sent)
        conn.incoming.put_nowait(IncomingMessage("homeassistant/status", b"offline"))
        conn.incoming.put_nowait(IncomingMessage("other/topic", b"online"))
        await rig.idle()
        assert len(conn.sent) == n  # neither triggered anything


async def test_configurable_birth_topic(tmp_path: Path) -> None:
    async with make(tmp_path, birth_topic="ha/custom/status") as rig:
        conn = await rig.ready()
        assert conn.subscriptions == [("ha/custom/status", 1)]
        before = len(conn.sent)
        conn.birth("ha/custom/status")
        await settle(lambda: len(conn.sent) > before)


async def test_orderly_shutdown_publishes_inverter_then_app_offline(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap(), {"battery_voltage": 1.0})
        await settle(lambda: conn.last(NS.inverter_availability_topic("deye1")).text == "online")
        rig.pub.request_stop()
        assert rig.task is not None
        await asyncio.wait_for(rig.task, 5)
        tail = conn.sent[-2:]
        assert [(s.topic, s.text, s.retain) for s in tail] == [
            (NS.inverter_availability_topic("deye1"), "offline", True),
            (NS.app_status_topic, "offline", True),
        ]
        assert conn.closed


async def test_shutdown_while_broker_is_gone_does_not_hang(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()

        def boom(sent: Any) -> Exception:
            return mqtt_error("gone")

        conn.fail_publish = boom
        rig.pub.request_stop()
        assert rig.task is not None
        await asyncio.wait_for(rig.task, 5)


async def test_unwritable_discovery_inventory_keeps_the_app_offline(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        before = len(conn.sent)
        import os

        os.chmod(tmp_path, 0o500)
        try:
            if os.access(tmp_path, os.W_OK):
                pytest.skip("directory permissions are not enforced here")
            rig.pub.set_selection("deye1", ("battery_voltage",), 1)
            await settle(lambda: rig.pub.last_discovery_error is not None)
            assert "unwritable" in str(rig.pub.last_discovery_error)
            assert not [s for s in conn.sent[before:] if "/config" in s.topic]  # no publish
        finally:
            os.chmod(tmp_path, 0o700)


async def test_app_diagnostics_heartbeat_and_inverter_state(tmp_path: Path) -> None:
    settings = PublisherSettings(
        reconnect_min_s=0.01, reconnect_max_s=0.04, heartbeat_s=0.05, publish_timeout_s=1.0
    )
    async with make(tmp_path, settings=settings) as rig:
        conn = await rig.ready()
        await settle(lambda: len(conn.on(NS.app_state_topic)) >= 2)  # periodic, fresh object
        running(rig)
        await settle(
            lambda: conn.last(NS.app_state_topic).json()["inverter_deye1_state"] == "running"
        )
        body = conn.last(NS.app_state_topic).json()
        assert body["inverters_configured"] == 1 and "timestamp" in body
        assert not conn.last(NS.app_state_topic).retain


async def test_serial_number_reaches_the_device_when_known(tmp_path: Path) -> None:
    serials: dict[str, str | None] = {"deye1": None}
    async with make(tmp_path, serial_of=lambda i: serials[i]) as rig:
        conn = await rig.ready()
        topic = NS.discovery_topic(NS.inverter_device_id("deye1"))
        assert "serial_number" not in conn.on(topic)[-1].json()["device"]
        serials["deye1"] = "2401234567"
        running(rig)
        await settle(lambda: "serial_number" in conn.on(topic)[-1].json()["device"])
        assert conn.on(topic)[-1].json()["device"]["serial_number"] == "2401234567"


async def test_two_instances_on_one_broker_use_disjoint_topics(tmp_path: Path) -> None:
    other = Namespace("ff00ee11", "homeassistant", "deye")
    async with (
        make(tmp_path / "a") as a,
        make(tmp_path / "b", ns=other) as b,
    ):
        ca, cb = await a.ready(), await b.ready()
        a.pub.on_availability("deye1", InverterState.RUNNING, "x")
        a.pub.on_state("deye1", snap(), {"battery_voltage": 1.0})
        await settle(lambda: ca.on(state_topic("battery")))
        topics_a, topics_b = {s.topic for s in ca.sent}, {s.topic for s in cb.sent}
        assert not topics_a & topics_b
        assert ca.last(NS.app_status_topic).text == "online"
        assert cb.on(other.app_status_topic)[-1].text == "online"


def test_settings_are_validated() -> None:
    with pytest.raises(ValueError):
        PublisherSettings(reconnect_min_s=0)
    with pytest.raises(ValueError):
        PublisherSettings(reconnect_min_s=5, reconnect_max_s=1)
    with pytest.raises(ValueError):
        PublisherSettings(heartbeat_s=float("inf"))


async def test_opaque_energy_series_from_the_device_layer_is_used_verbatim(tmp_path: Path) -> None:
    async with make(tmp_path, keys=(ENERGY,)) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap("energy", series="x7k2m9q4g0"), {ENERGY: 4.0})
        await settle(lambda: conn.on(state_topic("energy")))
        topic = NS.discovery_topic(NS.inverter_device_id("deye1"))
        doc = conn.on(topic)[-1].json()
        comp = doc["components"][f"{ENERGY}_x7k2m9q4g0"]
        assert comp["unique_id"].endswith("_x7k2m9q4g0")
        assert "== 'x7k2m9q4g0'" in comp["value_template"]
        assert doc["components"][f"{ENERGY}_g0"] == {"platform": "sensor"}
        assert conn.last(state_topic("energy")).json()["energy_series"] == "x7k2m9q4g0"
        # the old namespace's g0 is a known series: it is never switched back to
        rig.pub.on_state("deye1", snap("energy", cycle=2, series="g0"), {ENERGY: 99.0})
        await rig.idle()
        assert [s.json()["energy_series"] for s in conn.on(state_topic("energy"))] == ["x7k2m9q4g0"]
