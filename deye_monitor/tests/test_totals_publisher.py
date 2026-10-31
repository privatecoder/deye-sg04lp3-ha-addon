"""Plant totals through the publisher: discovery on the app device, state topic, series, cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from deye_monitor.config import PollConfig
from deye_monitor.device import InverterState
from deye_monitor.ha_discovery import DeviceMeta, app_components, device_document, plant_components
from deye_monitor.mqtt import InverterInfo, MqttPublisher
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.store import JsonStore
from deye_monitor.totals import METRICS, PlantSeries, PlantTotals, TotalsMember
from tests.fakes.mqtt import FakeBroker, settle
from tests.test_ha_discovery import GOLDEN, golden
from tests.test_mqtt_publisher import FAST, NS, PROFILE, snap

KEYS = (
    "load_power_total",
    "inverter_power_total",
    "pv1_power",
    "battery_voltage",
    "battery_current",
    "load_energy_total",
    "bms_soc",
    "battery_soc",
)
IDS = ("deye1", "deye2")


class Plant:
    def __init__(self, pub: MqttPublisher, broker: FakeBroker, series: dict[str, str]) -> None:
        self.pub, self.broker, self.series = pub, broker, series

    def plant_messages(self) -> list[dict[str, Any]]:
        conn = self.broker.current
        return [json.loads(s.text) for s in conn.on(NS.plant_state_topic)]

    def app_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads(
            self.broker.current.last(NS.discovery_topic(NS.app_device_id)).text
        )
        return doc


@contextlib.asynccontextmanager
async def make(tmp_path: Path, *, ids: tuple[str, ...] = IDS) -> AsyncIterator[Plant]:
    broker = FakeBroker()
    store = JsonStore(tmp_path / "discovery.json", validate=valid_discovery_document)
    series = {i: "g0" for i in ids}
    roles = {ids[0]: "master", **dict.fromkeys(ids[1:], "slave")}
    totals = PlantTotals(
        [TotalsMember(i, f"Inverter {i}", PROFILE, PollConfig()) for i in ids],
        PlantSeries(tmp_path),
        keys_of=lambda i: set(KEYS),
        role_of=roles.get,
        member_series_of=series.__getitem__,
    )
    pub = MqttPublisher(
        NS,
        [InverterInfo(i, i.upper(), PROFILE, PollConfig()) for i in ids],
        store,
        broker,
        selections={i: (KEYS, 0) for i in ids},
        series_of=series.__getitem__,
        settings=FAST,
        rand=lambda: 0.0,
        totals=totals,
    )
    task = asyncio.create_task(pub.run())
    try:
        conn = await broker.wait_connections(1)
        await settle(lambda: bool(conn.on(NS.app_status_topic)))
        yield Plant(pub, broker, series)
    finally:
        pub.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 5)


def feed(plant: Plant, inverter: str, load: float, *, cycle: int = 1, **extra: Any) -> None:
    plant.pub.on_state(
        inverter,
        snap("ac", cycle=cycle),
        {
            "load_power_total": load,
            "inverter_power_total": load,
            "pv1_power": 10.0,
            "battery_voltage": 50.0,
            "battery_current": -1.0,
            "bms_soc": 77,
            "battery_soc": 76,
            **extra,
        },
    )


async def idle() -> None:
    for _ in range(30):
        await asyncio.sleep(0.002)


async def test_plant_entities_are_on_the_app_device_with_stable_ids(tmp_path: Path) -> None:
    async with make(tmp_path) as plant:
        comps = plant.app_doc()["components"]
        for key in ("plant_load_power", "plant_pv_power", "plant_battery_soc", "plant_members"):
            assert comps[key]["unique_id"] == f"{NS.app_device_id}_{key}"
        assert "plant_load_energy_total_g0" in comps
        assert all(k in comps for k in app_components(NS))  # app diagnostics stay
        inverter = json.loads(
            plant.broker.current.last(NS.discovery_topic(NS.inverter_device_id("deye1"))).text
        )
        assert not any(k.startswith("plant_") for k in inverter["components"])


async def test_totals_are_published_and_unknown_while_a_member_is_missing(tmp_path: Path) -> None:
    async with make(tmp_path) as plant:
        for inverter in IDS:
            plant.pub.on_availability(inverter, InverterState.RUNNING, "ok")
        feed(plant, "deye1", 0.0)
        await idle()
        assert plant.plant_messages()[-1]["plant_load_power"] is None  # deye2 missing
        feed(plant, "deye2", 120.0, cycle=2)
        await asyncio.sleep(1.1)  # the plant topic is throttled to once per second
        feed(plant, "deye2", 120.0, cycle=3)
        await settle(lambda: plant.plant_messages()[-1]["plant_load_power"] == 120.0, timeout=3)
        last = plant.plant_messages()[-1]
        assert (
            last["plant_battery_soc"] == 76
            and last["plant_bms_soc"] == 77
            and last["plant_master"] == "Inverter deye1"
            and last["plant_master_id"] == "deye1"
        )
        assert last["energy_series"] == "g0"
        plant.pub.on_availability("deye2", InverterState.OFFLINE, "gone")
        await settle(lambda: plant.plant_messages()[-1]["plant_load_power"] is None, timeout=3)
        assert "deye2" in plant.plant_messages()[-1]["plant_members_stale_list"]


async def test_a_member_series_change_moves_the_plant_energy_entities_to_a_new_series(
    tmp_path: Path,
) -> None:
    async with make(tmp_path) as plant:
        conn = plant.broker.current
        plant.series["deye2"] = "g1"
        plant.pub.on_state(
            "deye2", snap("energy", cycle=5, series="g1"), {"load_energy_total": 1.0}
        )
        await settle(lambda: "plant_load_energy_total_g1" in plant.app_doc()["components"])
        comps = plant.app_doc()["components"]
        assert comps["plant_load_energy_total_g0"] == {"platform": "sensor"}  # retired, cleaned up
        assert plant.plant_messages() is not None and conn is plant.broker.current


async def test_a_plant_without_totals_has_no_plant_entities(tmp_path: Path) -> None:
    broker = FakeBroker()
    store = JsonStore(tmp_path / "discovery.json", validate=valid_discovery_document)
    pub = MqttPublisher(
        NS,
        [InverterInfo("deye1", "D1", PROFILE, PollConfig())],
        store,
        broker,
        selections={"deye1": (KEYS, 0)},
        settings=FAST,
        rand=lambda: 0.0,
    )
    task = asyncio.create_task(pub.run())
    try:
        conn = await broker.wait_connections(1)
        await settle(lambda: bool(conn.on(NS.app_status_topic)))
        doc = json.loads(conn.last(NS.discovery_topic(NS.app_device_id)).text)
        assert not any(k.startswith("plant_") for k in doc["components"])
        assert pub.totals_view() == {"active": False}
    finally:
        pub.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 5)


async def test_removing_the_totals_retires_every_plant_entity(tmp_path: Path) -> None:
    async with make(tmp_path) as plant:
        keys = {k for k in plant.app_doc()["components"] if k.startswith("plant_")}
        assert len(keys) > 10
    # same discovery inventory, a publisher without totals
    broker = FakeBroker()
    store = JsonStore(tmp_path / "discovery.json", validate=valid_discovery_document)
    pub = MqttPublisher(
        NS,
        [InverterInfo(i, i, PROFILE, PollConfig()) for i in IDS],
        store,
        broker,
        selections={i: (KEYS, 0) for i in IDS},
        settings=FAST,
        rand=lambda: 0.0,
    )
    task = asyncio.create_task(pub.run())
    try:
        conn = await broker.wait_connections(1)
        await settle(lambda: bool(conn.on(NS.app_status_topic)))
        doc = json.loads(conn.last(NS.discovery_topic(NS.app_device_id)).text)
        assert {k for k, v in doc["components"].items() if v != {"platform": "sensor"}}.isdisjoint(
            keys
        )
        assert keys <= set(doc["components"])  # tombstones until the cleanup is complete
    finally:
        pub.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 5)


def test_app_document_with_plant_totals_golden() -> None:
    meta = DeviceMeta(NS.app_device_id, "Deye Monitor", "app")
    comps = {**app_components(NS), **plant_components(NS, "g0", {m.key: 30 for m in METRICS})}
    golden("discovery_app_plant.json", device_document(NS, meta, comps))
    assert (GOLDEN / "discovery_app_plant.json").exists()
