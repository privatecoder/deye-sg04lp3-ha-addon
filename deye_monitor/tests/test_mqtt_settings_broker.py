"""The write entities against a real local Mosquitto (``pytest -m broker``): commands travel over
the wire, a retained command is dropped by its flag, results and state are not retained.
Skipped when Docker is unavailable. Never a plant broker."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aiomqtt
import pytest

from deye_monitor.config import MqttConfig, PollConfig
from deye_monitor.device import InverterState
from deye_monitor.ha_discovery import Namespace
from deye_monitor.mqtt import InverterInfo, MqttPublisher, PublisherSettings, aiomqtt_factory
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.mqtt_settings import SettingsPlane
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
from tests.test_mqtt_settings import FakeSource

pytestmark = [
    pytest.mark.broker,
    pytest.mark.skipif(not docker_available(), reason="Docker is not available"),
]

PROFILE = load_profile("sg04lp3_lv_v105_8")
NS = Namespace("ab12cd34", "homeassistant", "deye")
SETTINGS = PublisherSettings(
    reconnect_min_s=0.2, reconnect_max_s=0.5, publish_timeout_s=5.0, shutdown_deadline_s=3.0
)
DEVICE = NS.discovery_topic(NS.inverter_device_id("deye1"))


@pytest.fixture(scope="module")
def broker() -> Iterator[MosquittoContainer]:
    try:
        with mosquitto(persistence=False) as box:
            yield box
    except RuntimeError as exc:
        pytest.skip(str(exc))


@contextlib.asynccontextmanager
async def running(
    port: int, directory: Path, source: FakeSource, *, nonce: str = "t1"
) -> AsyncIterator[tuple[MqttPublisher, SettingsPlane]]:
    store = JsonStore(directory / "discovery.json", validate=valid_discovery_document)
    plane = SettingsPlane(NS, {"deye1": PollConfig()}, source, nonce=nonce)
    pub = MqttPublisher(
        NS,
        [InverterInfo("deye1", "Deye 1", PROFILE, PollConfig())],
        store,
        aiomqtt_factory(NS, MqttConfig(host="127.0.0.1", port=port), SETTINGS),
        selections={"deye1": (("battery_soc",), 0)},
        settings=SETTINGS,
        plane=plane,
    )
    pub.on_availability("deye1", InverterState.RUNNING, "identified, data flowing")
    task = asyncio.create_task(pub.run())
    try:
        yield pub, plane
    finally:
        pub.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 10)


async def command(port: int, topic: str, payload: str, *, retain: bool = False) -> None:
    async with aiomqtt.Client("127.0.0.1", port, identifier="test-ha") as client:
        await client.publish(topic, payload.encode(), qos=1, retain=retain)


def command_topic(obs: Observer, key: str) -> str:
    doc = json.loads(obs.on(DEVICE)[-1].payload)
    return str(doc["components"][f"set_{key}"]["command_topic"])


async def exposed(obs: Observer) -> None:
    await obs.until(
        lambda: (
            bool(obs.on(DEVICE))
            and "set_battery_charge_current" in json.loads(obs.on(DEVICE)[-1].payload)["components"]
            and obs.on(NS.write_state_topic("deye1"))
        )
    )


async def test_a_command_over_the_wire_is_executed_and_reported(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    source = FakeSource()
    async with observing(broker.port) as obs, running(broker.port, tmp_path, source):
        await exposed(obs)
        await command(broker.port, command_topic(obs, "battery_charge_current"), "40")
        result = NS.write_result_topic("deye1")
        await obs.until(lambda: bool(obs.on(result)))
        event = json.loads(obs.on(result)[-1].payload)
        assert event["event_type"] == "write_readback_confirmed" and event["value"] == 40
        assert source.submitted == [("battery_charge_current", "40")]
        await obs.until(
            lambda: (
                json.loads(obs.on(NS.write_state_topic("deye1"))[-1].payload)[
                    "battery_charge_current"
                ]
                == 40
            )
        )
        # what a new subscriber finds: discovery and availability are retained; the state, the
        # results and any command are not
        snap = await retained_snapshot(broker.port)
        assert DEVICE in snap
        avail = NS.write_availability_topic("deye1", "battery_charge_current")
        assert snap[avail] == b"online"
        assert NS.write_state_topic("deye1") not in snap and result not in snap
        assert not [t for t in snap if "/write/set/" in t]  # no command is ever retained by us


async def test_a_malformed_command_over_the_wire_is_only_an_event(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    source = FakeSource()
    async with observing(broker.port) as obs, running(broker.port, tmp_path, source):
        await exposed(obs)
        await command(broker.port, command_topic(obs, "tou_slot2_start"), "21:00")
        result = NS.write_result_topic("deye1")
        await obs.until(lambda: bool(obs.on(result)))
        event = json.loads(obs.on(result)[-1].payload)
        assert (event["event_type"], event["reason"]) == ("write_rejected", "invalid_payload")
        assert source.submitted == []


async def test_a_retained_command_is_dropped_when_the_broker_replays_it(
    broker: MosquittoContainer, tmp_path: Path
) -> None:
    """Retained while the app is away: on the next session the broker hands it to the new
    subscription with the retain flag set, and it is not executed. (The generation token is
    deterministic here only because the test fixes the nonce.)"""
    directory = tmp_path / "first"
    directory.mkdir()
    first = FakeSource()
    async with observing(broker.port) as obs:
        async with running(broker.port, directory, first, nonce="t2"):
            await exposed(obs)
            topic = command_topic(obs, "battery_charge_current")
        await obs.until(
            lambda: obs.on(NS.app_status_topic)[-1].payload == b"offline"
        )  # the app is gone
        await command(broker.port, topic, "33", retain=True)
        second = FakeSource()
        async with running(broker.port, directory, second, nonce="t2") as (_, plane):
            await obs.until(lambda: second.snapshots >= 1)
            await obs.until(lambda: plane.dropped_retained == 1)
            await asyncio.sleep(0.5)
            assert second.submitted == [] and plane.executed == 0
            assert command_topic(obs, "battery_charge_current") == topic  # same generation
    # clean the retained command away (the test broker is shared)
    await command(broker.port, topic, "", retain=True)
