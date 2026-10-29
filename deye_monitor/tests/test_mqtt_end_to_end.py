"""Device loop (fake inverter) -> publisher -> fake broker: values and availability."""

from __future__ import annotations

from pathlib import Path

from deye_monitor.app import build_publisher
from deye_monitor.device import InverterState, RelaySink
from deye_monitor.mqtt import PublisherSettings
from tests.device_helpers import device_regs, ep, inverter, rig
from tests.fakes.mqtt import FakeBroker, settle

FAST = PublisherSettings(reconnect_min_s=0.01, reconnect_max_s=0.04)


async def test_running_inverter_publishes_values_and_goes_online(tmp_path: Path) -> None:
    relay = RelaySink()
    banks = {"gw1": device_regs("2401234567", r588=55, r500=2)}
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], banks, tmp_path, sink=relay) as r:  # type: ignore[arg-type]
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
        import asyncio

        task = asyncio.create_task(pub.run())
        conn = await broker.wait_connections(1)
        await settle(lambda: conn.on(pub.ns.app_status_topic))
        inv = r.inv("deye1")
        await r.step_until("deye1", lambda i: i.state is InverterState.RUNNING)
        for _ in range(3):
            await inv.step()
            r.clock.pass_cooldown()
        avail = pub.ns.inverter_availability_topic("deye1")
        await settle(lambda: conn.on(avail) and conn.last(avail).text == "online")
        groups = {s.topic.rsplit("/", 1)[1] for s in conn.sent if "/deye1/" in s.topic}
        assert {"status", "battery", "ac", "pv", "energy"} <= groups
        body = conn.last(pub.ns.group_topic("deye1", "status")).json()
        assert body["operating_state"] == "normal" and body["cycle_id"] >= 1
        assert not conn.last(pub.ns.group_topic("deye1", "status")).retain
        # every discovered key is a selected one; mandatory/internal reads never appear
        doc = conn.on(pub.ns.discovery_topic(pub.ns.inverter_device_id("deye1")))[-1].json()
        assert not {k for k in doc["components"] if k.startswith("id_")}
        pub.request_stop()
        await asyncio.wait_for(task, 5)
