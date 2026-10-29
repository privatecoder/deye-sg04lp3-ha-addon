"""Child process for the hard-kill test: a publisher that connects and then waits for SIGKILL.

Usage: python -m tests.broker_child HOST PORT INSTANCE DATA_DIR
Prints ``ready`` once the app status is ``online``."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from deye_monitor.config import MqttConfig, PollConfig
from deye_monitor.ha_discovery import Namespace
from deye_monitor.mqtt import InverterInfo, MqttPublisher, PublisherSettings, aiomqtt_factory
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.profiles.schema import load_profile
from deye_monitor.store import JsonStore


async def main(host: str, port: int, instance: str, directory: str) -> None:
    profile = load_profile("sg04lp3_lv_v105_8")
    ns = Namespace(instance, "homeassistant", "deye")
    settings = PublisherSettings()
    store = JsonStore(Path(directory) / "discovery.json", validate=valid_discovery_document)
    pub = MqttPublisher(
        ns,
        [InverterInfo("deye1", "Deye 1", profile, PollConfig())],
        store,
        aiomqtt_factory(ns, MqttConfig(host=host, port=port), settings),
        selections={"deye1": (("battery_soc",), 0)},
        settings=settings,
    )
    task = asyncio.create_task(pub.run())
    while not pub.connected:
        await asyncio.sleep(0.05)
    print("ready", flush=True)
    await task


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]))
