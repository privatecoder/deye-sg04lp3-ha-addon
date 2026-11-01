"""MQTT write entities on top of the REAL write service and fake inverters: a command from Home
Assistant ends in exactly the guarded FC16 of the Web path (or in none), and the entity state is
always a fresh read-back. Fake devices and an in-memory connection only."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.device import InverterState
from deye_monitor.ha_discovery import Namespace
from deye_monitor.mqtt import IncomingMessage, InverterInfo, MqttPublisher, PublisherSettings
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.mqtt_settings import SettingsPlane
from deye_monitor.store import JsonStore
from tests.fakes.mqtt import FakeBroker, FakeConnection, settle
from tests.plant_helpers import PlantRig, plant_rig
from tests.write_helpers import audit_lines, build_plant, fast, fc16

NS = Namespace("ab12cd34", "homeassistant", "deye")
FAST = PublisherSettings(
    reconnect_min_s=0.01, reconnect_max_s=0.04, publish_timeout_s=1.0, shutdown_deadline_s=1.0
)
MASTER, SLAVE = 0x0003, 0x0001  # R336: bit 0 parallel enable, bit 1 master


@dataclass
class Bridge:
    rig: PlantRig
    pub: MqttPublisher
    plane: SettingsPlane
    broker: FakeBroker

    @property
    def conn(self) -> FakeConnection:
        return self.broker.current

    def components(self, inverter: str = "deye1") -> dict[str, Any]:
        doc = json.loads(
            self.conn.last(NS.discovery_topic(NS.inverter_device_id(inverter))).payload
        )
        return {k: c for k, c in doc["components"].items() if set(c) != {"platform"}}

    def send(self, key: str, payload: str, inverter: str = "deye1") -> None:
        topic = self.components(inverter)[f"set_{key}"]["command_topic"]
        self.conn.incoming.put_nowait(IncomingMessage(topic, payload.encode(), False))

    def events(self, inverter: str = "deye1") -> list[dict[str, Any]]:
        return [json.loads(m.payload) for m in self.conn.on(NS.write_result_topic(inverter))]

    def state(self, inverter: str = "deye1") -> dict[str, Any]:
        return dict(json.loads(self.conn.last(NS.write_state_topic(inverter)).payload))

    async def refresh(self, inverter: str = "deye1") -> None:
        self.plane.request_refresh(inverter)
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def attach(rig: PlantRig) -> AsyncIterator[Bridge]:
    runtime = rig.app.runtime
    assert runtime is not None
    runtime.writes.plant_revision = lambda: rig.service.revision
    cfg = rig.app.cfg
    plane = SettingsPlane(NS, {i.id: i.poll for i in cfg.inverters}, runtime.writes)
    broker = FakeBroker()
    store = JsonStore(rig.directory / "discovery.json", validate=valid_discovery_document)
    pub = MqttPublisher(
        NS,
        [
            InverterInfo(i.id, i.name, runtime.inverters[i.id].profile, i.poll)
            for i in cfg.inverters
        ],
        store,
        broker,
        selections={i.id: (("battery_soc",), 0) for i in cfg.inverters},
        settings=FAST,
        plane=plane,
    )
    for i in cfg.inverters:
        pub.on_availability(i.id, InverterState.RUNNING, "identified, data flowing")
    task = asyncio.create_task(pub.run())
    bridge = Bridge(rig, pub, plane, broker)
    conn = await broker.wait_connections(1)
    await settle(lambda: bool(conn.on(NS.app_status_topic)))
    await settle(lambda: bool(conn.on(NS.write_state_topic("deye1"))))
    try:
        yield bridge
    finally:
        pub.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 5)


async def test_a_command_ends_in_one_guarded_write_with_a_confirmed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r336": MASTER}})
        fast(rig)
        async with attach(rig) as bridge:
            assert bridge.state()["battery_charge_current"] == 50
            bridge.send("battery_charge_current", "40")
            await settle(lambda: len(bridge.events()) == 1)
            event = bridge.events()[0]
            assert event["event_type"] == "write_readback_confirmed" and event["reason"] is None
            assert rig.servers["gw1"].responder.regs[108] == 40
            assert len(fc16(rig.servers["gw1"])) == 1
            lines = audit_lines(rig)
            assert lines[0]["event"] == "intent" and lines[0]["record"]["source"] == "mqtt"
            assert lines[0]["record"]["user"] == ""  # no invented user
            assert event["audit_id"] == lines[0]["record"]["operation_id"]
            assert bridge.state()["battery_charge_current"] == 40  # the read-back


async def test_off_edit_on_reaches_the_wire_in_order_and_keeps_every_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r336": MASTER, "r146": 0x00FF}}
        )
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("tou_enable", "OFF")
            bridge.send("tou_slot2_start", "06:00:00")
            bridge.send("tou_enable", "ON")
            await settle(lambda: len(bridge.events()) == 3, timeout=10)
            assert [e["event_type"] for e in bridge.events()] == ["write_readback_confirmed"] * 3
            written = [(w.start, w.value) for w in rig.servers["gw1"].responder.writes]
            assert written == [(146, 0x00FE), (149, 600), (146, 0x00FF)]
            assert bridge.state()["tou_enable"] is True
            assert bridge.state()["tou_slot2_start"] == "06:00:00"


async def test_a_command_equal_to_the_read_back_is_audited_and_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r336": MASTER}})
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("battery_low_soc", "35")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["event_type"] == "write_noop"
            assert rig.servers["gw1"].responder.writes == []
            record = audit_lines(rig)[-1]["record"]
            assert record["status"] == "noop" and record["source"] == "mqtt"


async def test_a_refused_command_publishes_the_value_the_unit_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r336": MASTER}})
        fast(rig)
        async with attach(rig) as bridge:
            bridge.send("tou_slot2_start", "10:00:00")  # crosses slot 3 (10:00 is its start)
            bridge.send("battery_charge_current", "999")  # above the 190 A cap of 8 kW
            bridge.send("battery_low_soc", "20")  # below the shutdown SOC 25
            await settle(lambda: len(bridge.events()) == 3)
            assert [(e["event_type"], e["reason"]) for e in bridge.events()] == [
                ("write_rejected", "tou_order"),
                ("write_rejected", "value_out_of_range"),
                ("write_rejected", "threshold_order"),
            ]
            assert rig.servers["gw1"].responder.writes == [] and audit_lines(rig) == []
            state = bridge.state()
            assert state["tou_slot2_start"] == "05:00:00" and state["battery_charge_current"] == 50
            assert state["battery_low_soc"] == 35


async def test_a_gated_setting_is_unavailable_and_its_command_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r336": MASTER, "r111": 0, "r146": 0x01FE}}
        )
        fast(rig)
        async with attach(rig) as bridge:
            avail = bridge.conn.last(NS.write_availability_topic("deye1", "battery_low_soc"))
            assert avail.text == "offline" and "set_battery_low_soc" in bridge.components()
            tou = bridge.conn.last(NS.write_availability_topic("deye1", "tou_slot1_power"))
            assert tou.text == "offline"  # Spain mode closes the ToU family
            ok = bridge.conn.last(NS.write_availability_topic("deye1", "battery_charge_current"))
            assert ok.text == "online"
            bridge.send("battery_low_soc", "40")
            await settle(lambda: len(bridge.events()) == 1)
            assert bridge.events()[0]["reason"] == "soc_mode_required"
            assert rig.servers["gw1"].responder.writes == []


async def test_only_the_master_of_a_parallel_system_has_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig,
            inverters=("deye1", "deye2"),
            enable=("deye1", "deye2"),
            regs={"deye1": {"r336": MASTER}, "deye2": {"r336": SLAVE}},
        )
        fast(rig)
        async with attach(rig) as bridge:
            assert "set_battery_charge_current" in bridge.components("deye1")
            await bridge.refresh("deye2")
            assert not [k for k in bridge.components("deye2") if k.startswith("set_")]
            assert "write_result" not in bridge.components("deye2")
            assert all("deye2" not in t for t in bridge.plane.topics())
            # the master loses its role: its entities go through the cleanup ledger
            rig.servers["gw1"].responder.regs[336] = SLAVE
            await bridge.refresh("deye1")
            await settle(lambda: "set_battery_charge_current" not in bridge.components("deye1"))
            entry = bridge.pub._store.data["devices"][
                NS.discovery_topic(NS.inverter_device_id("deye1"))
            ]
            assert "set_battery_charge_current" in entry["retired"]


async def test_a_firmware_change_withdraws_the_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r336": MASTER}})
        fast(rig)
        async with attach(rig) as bridge:
            assert "set_battery_charge_current" in bridge.components()
            rig.servers["gw1"].responder.regs[17] = 0x1234  # another MCU1 firmware word
            await bridge.refresh()
            await settle(lambda: "set_battery_charge_current" not in bridge.components())
            assert bridge.plane.topics() == set()


async def test_the_plant_revision_is_wired_without_a_web_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command must not depend on a web call having wired the revision first."""
    async with plant_rig(tmp_path, monkeypatch) as rig:
        assert rig.app.runtime is not None
        rig.app.plant_revision = lambda: 42
        assert rig.app.runtime.writes.plant_revision() == 42
