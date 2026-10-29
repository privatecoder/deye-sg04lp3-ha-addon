"""MQTT publisher against a real local Mosquitto (SR-13). Run with ``pytest -m broker``.

Retain, will at a hard process end, broker restart (with and without persistence), HA birth,
cleanup after a restart and two instances on one broker. Skipped when Docker is unavailable."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import MqttConfig, PollConfig
from deye_monitor.device import InverterState, Snapshot
from deye_monitor.ha_discovery import Namespace
from deye_monitor.mqtt import InverterInfo, MqttPublisher, PublisherSettings, aiomqtt_factory
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.profiles.schema import load_profile
from deye_monitor.store import JsonStore
from tests.broker_helpers import (
    MosquittoContainer,
    Observer,
    docker_available,
    mosquitto,
    observing,
    retained_snapshot,
)

pytestmark = [
    pytest.mark.broker,
    pytest.mark.skipif(not docker_available(), reason="Docker is not available"),
]

PROFILE = load_profile("sg04lp3_lv_v105_8")
SETTINGS = PublisherSettings(
    reconnect_min_s=0.2, reconnect_max_s=0.5, publish_timeout_s=5.0, shutdown_deadline_s=3.0
)


@pytest.fixture(scope="module")
def broker() -> Iterator[MosquittoContainer]:
    try:
        with mosquitto(persistence=False) as box:
            yield box
    except RuntimeError as exc:  # image missing, no network ...
        pytest.skip(str(exc))


def publisher(
    port: int,
    directory: Path,
    *,
    instance: str = "ab12cd34",
    keys: tuple[str, ...] = ("battery_soc", "battery_voltage"),
    refreshed: list[str] | None = None,
) -> MqttPublisher:
    ns = Namespace(instance, "homeassistant", "deye")
    store = JsonStore(directory / "discovery.json", validate=valid_discovery_document)
    return MqttPublisher(
        ns,
        [InverterInfo("deye1", "Deye 1", PROFILE, PollConfig())],
        store,
        aiomqtt_factory(ns, MqttConfig(host="127.0.0.1", port=port), SETTINGS),
        selections={"deye1": (keys, 0)},
        request_refresh=refreshed.append if refreshed is not None else None,
        settings=SETTINGS,
    )


def _is_replay(payload: bytes) -> bool:
    """A cleanup replay document: the old component under its unique id with template ``none``."""
    if not payload:
        return False
    comp = json.loads(payload)["components"].get("battery_voltage", {})
    return bool(comp.get("value_template") == "{{ none }}")


def snapshot(cycle: int = 1) -> Snapshot:
    return Snapshot(
        cycle, "battery", 1.0, 1_700_000_000.0, "gw1", {}, frozenset(), 0, 1, 0, "g0", False
    )


async def running(pub: MqttPublisher) -> None:
    pub.on_availability("deye1", InverterState.RUNNING, "identified, data flowing")
    pub.on_state("deye1", snapshot(), {"battery_soc": 80, "battery_voltage": 52.0})


async def stop(pub: MqttPublisher, task: asyncio.Task[None]) -> None:
    pub.request_stop()
    await asyncio.wait_for(task, 10)


async def until_online(pub: MqttPublisher, obs: Observer, instance: str = "ab12cd34") -> None:
    topic = f"deye/{instance}/status"
    await obs.until(lambda: obs.on(topic) and obs.on(topic)[-1].payload == b"online")


async def test_retain_qos_and_the_only_subscription(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    ns = Namespace("ab12cd34", "homeassistant", "deye")
    async with observing(broker.port) as obs:
        pub = publisher(broker.port, tmp_path)
        task = asyncio.create_task(pub.run())
        await until_online(pub, obs)
        await running(pub)  # measurements before the connect are discarded by design
        await obs.until(lambda: obs.on(ns.group_topic("deye1", "battery")))
        await obs.until(
            lambda: (
                obs.on(ns.inverter_availability_topic("deye1"))[-1:]
                and obs.on(ns.inverter_availability_topic("deye1"))[-1].payload == b"online"
            )
        )
        # what a NEW subscriber receives: discovery, availability and app status are retained ...
        snap = await retained_snapshot(broker.port)
        assert snap[ns.app_status_topic] == b"online"
        assert snap[ns.inverter_availability_topic("deye1")] == b"online"
        doc = json.loads(snap[ns.discovery_topic(ns.inverter_device_id("deye1"))])
        assert sorted(doc["components"]) == ["battery_soc", "battery_voltage"]
        assert ns.discovery_topic(ns.app_device_id) in snap
        # ... but a measurement is not: no state is ever replayed to a late subscriber
        assert ns.group_topic("deye1", "battery") not in snap
        await stop(pub, task)
        after = await retained_snapshot(broker.port)
        assert after[ns.app_status_topic] == b"offline"  # orderly end: explicit offline
        assert after[ns.inverter_availability_topic("deye1")] == b"offline"


async def test_will_fires_at_a_hard_process_end(broker: MosquittoContainer, tmp_path: Path) -> None:
    ns = Namespace("c0ffee01", "homeassistant", "deye")
    async with observing(broker.port) as obs:
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.broker_child", "127.0.0.1", str(broker.port),
            "c0ffee01", str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
        )  # fmt: skip
        assert child.stdout is not None
        line = await asyncio.wait_for(child.stdout.readline(), 30)
        assert line.strip() == b"ready"
        await obs.until(
            lambda: (
                obs.on(ns.app_status_topic)[-1:]
                and obs.on(ns.app_status_topic)[-1].payload == b"online"
            )
        )
        child.send_signal(signal.SIGKILL)  # no orderly shutdown, no DISCONNECT
        await child.wait()
        await obs.until(lambda: obs.on(ns.app_status_topic)[-1].payload == b"offline")
        will = obs.on(ns.app_status_topic)[-1]
        assert will.payload == b"offline"  # (retain is checked below on a new subscriber)
        assert (await retained_snapshot(broker.port, ns.app_status_topic))[ns.app_status_topic] == (
            b"offline"
        )


async def test_ha_birth_republishes_discovery_and_asks_for_fresh_cycles(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    import aiomqtt

    ns = Namespace("beef0001", "homeassistant", "deye")
    refreshed: list[str] = []
    topic = ns.discovery_topic(ns.inverter_device_id("deye1"))
    async with observing(broker.port) as obs:
        pub = publisher(broker.port, tmp_path, instance="beef0001", refreshed=refreshed)
        task = asyncio.create_task(pub.run())
        await until_online(pub, obs, "beef0001")
        await running(pub)
        await obs.until(lambda: obs.on(ns.group_topic("deye1", "battery")))
        docs, states = len(obs.on(topic)), len(obs.on(ns.group_topic("deye1", "battery")))
        async with aiomqtt.Client("127.0.0.1", broker.port) as ha:
            await ha.publish("homeassistant/status", b"online", qos=1)  # HA restarted
        await obs.until(lambda: len(obs.on(topic)) > docs)
        await obs.until(lambda: len(refreshed) >= 2)  # connect + birth
        await asyncio.sleep(0.3)
        assert len(obs.on(ns.group_topic("deye1", "battery"))) == states  # no cached replay
        pub.on_state("deye1", snapshot(5), {"battery_soc": 79})  # the fresh cycle after the birth
        await obs.until(lambda: len(obs.on(ns.group_topic("deye1", "battery"))) == states + 1)
        await stop(pub, task)


@pytest.mark.parametrize("persistence", [False, True])
async def test_broker_restart_reconnects_and_resets_state(
    tmp_path: Path, persistence: bool
) -> None:
    ns = Namespace("ab12cd34", "homeassistant", "deye")
    with mosquitto(persistence=persistence) as box:
        pub = publisher(box.port, tmp_path)
        task = asyncio.create_task(pub.run())
        async with observing(box.port) as obs:
            await until_online(pub, obs)
            await running(pub)
            await obs.until(lambda: obs.on(ns.group_topic("deye1", "battery")))
        sessions = pub.sessions
        box.stop_broker()
        await asyncio.sleep(0.5)
        pub.on_state("deye1", snapshot(2), {"battery_soc": 11})  # produced while the broker is down
        box.start_broker()
        async with asyncio.timeout(30):
            while pub.sessions == sessions or not pub.connected:
                await asyncio.sleep(0.1)
        async with observing(box.port) as obs2:
            await until_online(pub, obs2)
            retained = await retained_snapshot(box.port)
            # the app re-published its inventory whether or not the broker kept anything
            assert ns.discovery_topic(ns.inverter_device_id("deye1")) in retained
            assert retained[ns.app_status_topic] == b"online"
            # the inverter was reset to offline and returns only with a fresh measurement
            assert retained[ns.inverter_availability_topic("deye1")] in (b"offline", b"online")
            await asyncio.sleep(0.5)
            assert not [
                s for s in obs2.on(ns.group_topic("deye1", "battery"))
                if json.loads(s.payload).get("battery_soc") == 11
            ]  # fmt: skip  # the old measurement is never replayed
            pub.on_state("deye1", snapshot(3), {"battery_soc": 77})
            await obs2.until(
                lambda: (
                    obs2.on(ns.inverter_availability_topic("deye1"))[-1:]
                    and obs2.on(ns.inverter_availability_topic("deye1"))[-1].payload == b"online"
                )
            )
        await stop(pub, task)


async def test_cleanup_after_app_restart_survives_births_until_purged(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    import aiomqtt

    ns = Namespace("dead0001", "homeassistant", "deye")
    topic = ns.discovery_topic(ns.inverter_device_id("deye1"))
    async with observing(broker.port) as obs:
        pub = publisher(broker.port, tmp_path, instance="dead0001")
        task = asyncio.create_task(pub.run())
        await until_online(pub, obs, "dead0001")
        pub.set_selection("deye1", ("battery_soc",), 1)  # deselect battery_voltage
        await obs.until(
            lambda: any(
                json.loads(s.payload)["components"].get("battery_voltage") == {"platform": "sensor"}
                for s in obs.on(topic)
                if s.payload
            )
        )
        await stop(pub, task)
        obs.seen.clear()
        # the application restarts (new process state, same /data): cold-start cleanup
        pub2 = publisher(broker.port, tmp_path, instance="dead0001", keys=("battery_soc",))
        task2 = asyncio.create_task(pub2.run())
        await until_online(pub2, obs, "dead0001")
        await obs.until(lambda: any(_is_replay(s.payload) for s in obs.on(topic)))
        await obs.until(lambda: obs.on(ns.cleanup_topic))
        assert obs.on(ns.cleanup_topic)[-1].payload == b"offline"
        docs = [json.loads(s.payload) for s in obs.on(topic) if s.payload and not s.retain]
        replay = next(
            d for d in docs if "value_template" in d["components"].get("battery_voltage", {})
        )
        old = replay["components"]["battery_voltage"]
        assert old["unique_id"] == "deye_dead0001_deye1_battery_voltage"
        assert old["value_template"] == "{{ none }}"
        assert {"topic": ns.cleanup_topic} in replay["availability"]
        retained = json.loads((await retained_snapshot(broker.port, topic))[topic])
        assert retained["components"]["battery_voltage"] == {"platform": "sensor"}
        # Home Assistant births replay the cleanup but never confirm it: the key stays
        for _ in range(3):
            before = len([s for s in obs.on(topic) if _is_replay(s.payload)])
            async with aiomqtt.Client("127.0.0.1", broker.port) as ha:
                await ha.publish("homeassistant/status", b"online", qos=1)
            await obs.until(
                lambda b=before: len([s for s in obs.on(topic) if _is_replay(s.payload)]) > b
            )
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.5)
        final = json.loads((await retained_snapshot(broker.port, topic))[topic])
        assert final["components"]["battery_voltage"] == {"platform": "sensor"}
        assert pub2.diagnostics()["retired_components"] == 1
        # only the explicit user action forgets it
        assert pub2.purge_cleanup_ledger("deye1") == 1
        await obs.until(lambda: pub2.diagnostics()["retired_components"] == 0)
        await asyncio.sleep(0.4)
        final = json.loads((await retained_snapshot(broker.port, topic))[topic])
        assert "battery_voltage" not in final["components"]
        await stop(pub2, task2)


async def test_two_instances_share_a_broker_without_touching_each_other(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    a = Namespace("aaaa0001", "homeassistant", "deye")
    b = Namespace("bbbb0001", "homeassistant", "deye")
    async with observing(broker.port) as obs:
        pa = publisher(broker.port, tmp_path / "a", instance="aaaa0001")
        pb = publisher(broker.port, tmp_path / "b", instance="bbbb0001", keys=("battery_voltage",))
        ta, tb = asyncio.create_task(pa.run()), asyncio.create_task(pb.run())
        await until_online(pa, obs, "aaaa0001")
        await until_online(pb, obs, "bbbb0001")
        pb.set_selection("deye1", ("battery_voltage",), 1)
        await asyncio.sleep(0.5)
        by_a = {s.topic for s in obs.seen if "aaaa0001" in s.topic}
        by_b = {s.topic for s in obs.seen if "bbbb0001" in s.topic}
        assert by_a and by_b
        assert not {s.topic for s in obs.seen if s.topic.startswith("deye/aaaa0001")} & by_b
        # stopping one instance leaves the other online
        await stop(pa, ta)
        assert (await retained_snapshot(broker.port, b.app_status_topic))[b.app_status_topic] == (
            b"online"
        )
        assert (await retained_snapshot(broker.port, a.app_status_topic))[a.app_status_topic] == (
            b"offline"
        )
        await stop(pb, tb)


async def test_discovery_error_with_live_connection_withdraws_online(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    ns = Namespace("fa11ed01", "homeassistant", "deye")
    async with observing(broker.port) as obs:
        pub = publisher(broker.port, tmp_path, instance="fa11ed01")
        task = asyncio.create_task(pub.run())
        await until_online(pub, obs, "fa11ed01")
        await running(pub)
        await obs.until(lambda: obs.on(ns.group_topic("deye1", "battery")))
        snap = await retained_snapshot(broker.port)
        assert snap[ns.app_status_topic] == b"online"

        def unwritable(_new: object) -> None:
            raise OSError("disk full")

        pub._store.commit = unwritable  # type: ignore[method-assign,assignment]
        pub.set_selection("deye1", ("battery_soc",), 1)  # the inventory cannot be persisted
        await obs.until(lambda: obs.on(ns.app_status_topic)[-1].payload == b"offline")
        # the failure persists: every following attempt keeps app and inverter offline
        for _ in range(4):
            await asyncio.sleep(0.3)
            snap = await retained_snapshot(broker.port)
            assert snap[ns.app_status_topic] == b"offline"
            assert snap[ns.inverter_availability_topic("deye1")] == b"offline"
        assert pub.last_discovery_error is not None
        assert all(s.payload != b"online" for s in obs.on(ns.app_status_topic)[-3:])
        await stop(pub, task)


async def test_abort_cuts_the_connection_without_disconnect_so_the_will_fires(
    broker: MosquittoContainer,
) -> None:
    ns = Namespace("ab027e01", "homeassistant", "deye")
    factory = aiomqtt_factory(ns, MqttConfig(host="127.0.0.1", port=broker.port), SETTINGS)
    async with observing(broker.port) as obs:
        async with factory() as conn:
            await conn.publish(ns.app_status_topic, b"online", qos=1, retain=True)
            await obs.until(lambda: obs.on(ns.app_status_topic))
            await conn.abort()
        await obs.until(lambda: obs.on(ns.app_status_topic)[-1].payload == b"offline")
        assert (await retained_snapshot(broker.port, ns.app_status_topic))[
            ns.app_status_topic
        ] == b"offline"


async def test_purge_through_a_real_broker_forgets_only_on_request(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    ns = Namespace("9e9e0001", "homeassistant", "deye")
    topic = ns.discovery_topic(ns.inverter_device_id("deye1"))
    async with observing(broker.port) as obs:
        pub = publisher(broker.port, tmp_path, instance="9e9e0001")
        task = asyncio.create_task(pub.run())
        await until_online(pub, obs, "9e9e0001")
        pub.set_selection("deye1", ("battery_soc",), 1)
        await obs.until(lambda: pub.diagnostics()["retired_components"] == 1)
        await stop(pub, task)
        for _ in range(2):  # restarts keep replaying it
            again = publisher(broker.port, tmp_path, instance="9e9e0001", keys=("battery_soc",))
            task = asyncio.create_task(again.run())
            await until_online(again, obs, "9e9e0001")
            assert again.diagnostics()["retired_components"] == 1
            await stop(again, task)
        last = publisher(broker.port, tmp_path, instance="9e9e0001", keys=("battery_soc",))
        task = asyncio.create_task(last.run())
        await until_online(last, obs, "9e9e0001")
        assert last.purge_cleanup_ledger("deye1") == 1
        await obs.until(lambda: last.diagnostics()["retired_components"] == 0)
        await asyncio.sleep(0.4)
        doc = json.loads((await retained_snapshot(broker.port, topic))[topic])
        assert "battery_voltage" not in doc["components"]
        await stop(last, task)


class _BlockingOffline:
    """A real aiomqtt connection whose ``offline`` publishes never complete (a stalled broker link
    during shutdown); everything else, including ``abort()``, is the real thing."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.block = False

    async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
        if self.block and payload == b"offline":
            await asyncio.Event().wait()
        await self.inner.publish(topic, payload, qos=qos, retain=retain)

    async def subscribe(self, topic: str, *, qos: int) -> None:
        await self.inner.subscribe(topic, qos=qos)

    def messages(self) -> Any:
        return self.inner.messages()

    async def abort(self) -> None:
        await self.inner.abort()


async def test_outer_deadline_cancelling_the_withdrawal_still_fires_the_will(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    ns = Namespace("c0de0d01", "homeassistant", "deye")
    real = aiomqtt_factory(ns, MqttConfig(host="127.0.0.1", port=broker.port), SETTINGS)
    proxies: list[_BlockingOffline] = []

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[_BlockingOffline]:
        async with real() as conn:
            proxy = _BlockingOffline(conn)
            proxies.append(proxy)
            yield proxy

    store = JsonStore(tmp_path / "discovery.json", validate=valid_discovery_document)
    pub = MqttPublisher(
        ns,
        [InverterInfo("deye1", "Deye 1", PROFILE, PollConfig())],
        store,
        factory,
        selections={"deye1": (("battery_soc",), 0)},
        settings=SETTINGS,
    )
    async with observing(broker.port) as obs:
        task = asyncio.create_task(pub.run())
        await until_online(pub, obs, "c0de0d01")
        proxies[0].block = True
        pub.request_stop()
        # the outer deadline is shorter than the inner offline budgets
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, SETTINGS.shutdown_deadline_s / 2)
        await obs.until(lambda: obs.on(ns.app_status_topic)[-1].payload == b"offline")
        assert (await retained_snapshot(broker.port, ns.app_status_topic))[
            ns.app_status_topic
        ] == b"offline"
