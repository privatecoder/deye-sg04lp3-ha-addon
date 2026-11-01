"""Home Assistant entities for the write layer (0.4.0 part B): exposure, strict command parsing,
the bounded FIFO, expiry, generation-scoped topics and the result events - on an in-memory
connection with a scripted settings source (the real service is exercised in the plant tests)."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest

from deye_monitor.config import PollConfig
from deye_monitor.device import InverterState
from deye_monitor.ha_discovery import EVENT_TYPES, Namespace
from deye_monitor.mqtt import IncomingMessage, InverterInfo, MqttPublisher, PublisherSettings
from deye_monitor.mqtt_discovery import valid_discovery_document
from deye_monitor.mqtt_settings import (
    COMMAND_TTL_S,
    QUEUE_LIMIT,
    SettingsPlane,
    parse_command,
)
from deye_monitor.profiles.schema import load_profile
from deye_monitor.store import JsonStore
from deye_monitor.write_ops import BY_KEY, Refused
from deye_monitor.writes import Binding, Intent, SettingsSnapshot, SettingState
from tests.fakes.mqtt import FakeBroker, FakeConnection, settle


@pytest.fixture(autouse=True)
def _short_recovery_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forced-refresh spacing (RETRY_FIRST_S, 5 s in service) is off here: the rig's clock
    stands still. The tests of the spacing itself set their own."""
    monkeypatch.setattr("deye_monitor.mqtt_settings.RETRY_FIRST_S", 0.0)


PROFILE = load_profile("sg04lp3_lv_v105_8")
NS = Namespace("ab12cd34", "homeassistant", "deye")
FAST = PublisherSettings(
    reconnect_min_s=0.01, reconnect_max_s=0.04, publish_timeout_s=1.0, shutdown_deadline_s=1.0
)
DEVICE = NS.discovery_topic(NS.inverter_device_id("deye1"))
RESULT = NS.write_result_topic("deye1")
STATE = NS.write_state_topic("deye1")

START_FIELDS: dict[str, int] = {
    "battery_charge_current": 50,
    "battery_discharge_current": 60,
    "battery_shutdown_soc": 25,
    "battery_low_soc": 35,
    "battery_restart_soc": 50,
    "grid_charge_enable": 0,
    "grid_charge_start_soc": 30,
    "smart_load_on_voltage": 5000,
    "tou_enable": 0,
    "tou_slot1_start": 0,
    "tou_slot2_start": 500,
    "tou_slot1_power": 1000,
    "tou_slot1_soc": 30,
    "tou_slot1_grid_charge": 0,
}


class FakeSource:
    """The write service as the entities see it: scripted snapshots, recorded submits."""

    def __init__(self, fields: dict[str, int] | None = None) -> None:
        self.fields = dict(START_FIELDS if fields is None else fields)
        self.released: set[str] = set(self.fields)
        self.status = "ok"
        self.reason: str | None = None
        self.gated: dict[str, str] = {}
        self.submitted: list[tuple[str, Any]] = []
        self.hold: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.script: dict[str, Callable[[str, Any], dict[str, Any]]] = {}
        self.snapshots = 0
        self.bind_error: str | None = None

    def enabled(self, inverter_id: str) -> frozenset[str]:
        return frozenset(self.released)

    async def snapshot(self, inverter_id: str) -> SettingsSnapshot:
        self.snapshots += 1
        if self.status != "ok":
            return SettingsSnapshot(self.status, self.reason, {})
        out: dict[str, SettingState] = {}
        for key in sorted(self.released & set(self.fields)):
            desc = BY_KEY[key]
            field_ = self.fields[key]
            cap = 190 if desc.current_cap else 8000 if desc.power_cap else None
            out[key] = SettingState(
                key,
                key not in self.gated,
                self.gated.get(key),
                field_,
                field_,
                desc.display(field_),
                cap,
            )
        return SettingsSnapshot("ok", None, out)

    def bind(self, inverter_id: str, key: str) -> Binding:
        if self.bind_error is not None:
            raise Refused(self.bind_error, 409)
        return Binding(inverter_id, "SN1", 0, "gw1", 1, 1, None, None, (0, 0, 0, 0, 0), 1)

    async def submit(
        self, inverter_id: str, key: str, value: Any, *, source: str, intent: Intent | None = None
    ) -> dict[str, Any]:
        assert source == "mqtt" and intent is not None
        self.submitted.append((key, value))
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        if key in self.script:
            return self.script[key](key, value)
        from deye_monitor.write_ops import parse_value

        field_ = parse_value(BY_KEY[key], value)
        if field_ == self.fields[key]:
            return {"operation_id": "op-noop", "status": "noop", "reason": "no_change"}
        self.fields[key] = field_
        return {"operation_id": f"op-{len(self.submitted)}", "status": "readback_confirmed"}


@dataclass
class Rig:
    pub: MqttPublisher
    plane: SettingsPlane
    source: FakeSource
    broker: FakeBroker
    store: JsonStore
    now: list[float]
    task: asyncio.Task[None] | None = None

    @property
    def conn(self) -> FakeConnection:
        return self.broker.current

    async def ready(self, n: int = 1) -> FakeConnection:
        conn = await self.broker.wait_connections(n)
        topic = NS.app_status_topic
        await settle(lambda: bool(conn.on(topic)) and conn.last(topic).text == "online")
        await settle(lambda: self.source.snapshots >= 1)
        await self.idle()
        return conn

    async def idle(self) -> None:
        for _ in range(40):
            await asyncio.sleep(0.002)

    def components(self) -> dict[str, Any]:
        doc = json.loads(self.conn.last(DEVICE).payload)
        return {k: c for k, c in doc["components"].items() if set(c) != {"platform"}}

    def command(self, key: str) -> str:
        return str(self.components()[f"set_{key}"]["command_topic"])

    def send(self, key: str, payload: bytes | str, *, retain: bool = False) -> None:
        data = payload.encode() if isinstance(payload, str) else payload
        self.conn.incoming.put_nowait(IncomingMessage(self.command(key), data, retain))

    def events(self, conn: FakeConnection | None = None) -> list[dict[str, Any]]:
        return [json.loads(m.payload) for m in (conn or self.conn).on(RESULT)]

    def last_state(self) -> dict[str, Any]:
        return dict(json.loads(self.conn.last(STATE).payload))

    def availability(self, key: str) -> str:
        return self.conn.last(NS.write_availability_topic("deye1", key)).text


@contextlib.asynccontextmanager
async def make(
    tmp_path: Path,
    source: FakeSource | None = None,
    *,
    broker: FakeBroker | None = None,
    store: JsonStore | None = None,
    nonce: str = "t1",
) -> AsyncIterator[Rig]:
    source = source or FakeSource()
    broker = broker or FakeBroker()
    store = store or JsonStore(tmp_path / "discovery.json", validate=valid_discovery_document)
    now = [1000.0]
    plane = SettingsPlane(
        NS,
        {"deye1": PollConfig()},
        source,
        clock=lambda: now[0],
        wall=lambda: 1_700_000_000.0,
        nonce=nonce,
    )

    async def sleep(delay: float) -> None:
        await asyncio.sleep(0)

    pub = MqttPublisher(
        NS,
        [InverterInfo("deye1", "Deye 1", PROFILE, PollConfig())],
        store,
        broker,
        selections={"deye1": (("battery_soc",), 0)},
        settings=FAST,
        rand=lambda: 0.0,
        sleep=sleep,
        plane=plane,
    )
    rig = Rig(pub, plane, source, broker, store, now)
    rig.task = asyncio.create_task(pub.run())
    pub.on_availability("deye1", InverterState.RUNNING, "identified, data flowing")
    try:
        yield rig
    finally:
        if source.hold is not None:
            source.hold.set()
        pub.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(rig.task, 5)


# -- exposure ---------------------------------------------------------------------------------


async def test_released_settings_become_config_entities_with_read_back_state(
    tmp_path: Path,
) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        comps = rig.components()
        written = {k: c for k, c in comps.items() if k.startswith("set_")}
        assert set(written) == {f"set_{k}" for k in START_FIELDS}
        assert {c["platform"] for c in written.values()} == {"number", "switch", "time"}
        for comp in written.values():
            assert comp["entity_category"] == "config"
            assert comp["retain"] is False and comp["optimistic"] is False
            assert comp["state_topic"] == STATE
        charge = comps["set_battery_charge_current"]
        assert (charge["min"], charge["max"], charge["step"], charge["mode"]) == (0, 190, 1, "box")
        assert charge["device_class"] == "current" and charge["unit_of_measurement"] == "A"
        volts = comps["set_smart_load_on_voltage"]
        assert (volts["min"], volts["max"], volts["step"]) == (38.0, 63.0, 0.01)
        assert volts["device_class"] == "voltage"
        assert comps["set_battery_low_soc"]["device_class"] == "battery"
        assert comps["set_tou_slot1_power"]["max"] == 8000  # the model's rated power
        assert comps["set_grid_charge_enable"]["platform"] == "switch"
        assert comps["set_tou_slot2_start"]["platform"] == "time"
        assert "min" not in comps["set_tou_slot2_start"]
        event = comps["write_result"]
        assert event["platform"] == "event" and event["event_types"] == list(EVENT_TYPES)
        # the availability of every setting is announced, the state is the read-back
        assert all(rig.availability(k) == "online" for k in START_FIELDS)
        assert conn.on(NS.write_availability_topic("deye1", "tou_enable"))[-1].retain is True
        state = rig.last_state()
        assert state["battery_charge_current"] == 50 and state["grid_charge_enable"] is False
        assert state["tou_slot2_start"] == "05:00:00" and state["smart_load_on_voltage"] == 50.0
        assert conn.on(STATE)[-1].retain is False
        # the only subscriptions: the birth topic and the current command topics
        topics = {t for t, _ in conn.subscriptions}
        assert topics == {"homeassistant/status", *rig.plane.topics()}
        assert len(topics) == len(START_FIELDS) + 1


async def test_nothing_is_exposed_without_a_release(tmp_path: Path) -> None:
    source = FakeSource()
    source.released.clear()
    source.status, source.reason = "withdrawn", "not_released"
    async with make(tmp_path, source) as rig:
        conn = await rig.ready()
        assert not [k for k in rig.components() if k.startswith("set_") or k == "write_result"]
        assert {t for t, _ in conn.subscriptions} == {"homeassistant/status"}


async def test_only_the_released_settings_are_exposed(tmp_path: Path) -> None:
    source = FakeSource()
    source.released = {"battery_low_soc", "tou_enable"}
    async with make(tmp_path, source) as rig:
        await rig.ready()
        assert {k for k in rig.components() if k.startswith("set_")} == {
            "set_battery_low_soc",
            "set_tou_enable",
        }


async def test_a_gated_setting_stays_an_entity_but_unavailable(tmp_path: Path) -> None:
    source = FakeSource()
    source.gated = {"battery_low_soc": "soc_mode_required"}
    async with make(tmp_path, source) as rig:
        await rig.ready()
        assert "set_battery_low_soc" in rig.components()
        assert rig.availability("battery_low_soc") == "offline"
        assert rig.availability("battery_charge_current") == "online"
        # it opens again with the next fresh read
        source.gated = {}
        rig.plane.request_refresh("deye1")
        await settle(lambda: rig.availability("battery_low_soc") == "online")


@pytest.mark.parametrize("reason", ["parallel_slave", "firmware_changed", "not_released"])
async def test_a_withdrawn_release_removes_the_entities_and_their_topics(
    tmp_path: Path, reason: str
) -> None:
    source = FakeSource()
    async with make(tmp_path, source) as rig:
        conn = await rig.ready()
        old_topic = rig.command("battery_low_soc")
        source.status, source.reason = "withdrawn", reason
        rig.plane.request_refresh("deye1")
        await settle(lambda: "set_battery_low_soc" not in rig.components())
        assert "write_result" not in rig.components()
        entry = rig.store.data["devices"][DEVICE]
        assert "set_battery_low_soc" in entry["retired"]  # the existing cleanup ledger
        assert entry["retired"]["write_result"]["platform"] == "event"
        await rig.idle()
        assert old_topic not in rig.plane.topics()
        # the dead topic is unsubscribed; a late command on it does nothing
        conn.incoming.put_nowait(IncomingMessage(old_topic, b"40"))
        await rig.idle()
        assert source.submitted == [] and rig.events() == []


async def test_an_unreadable_unit_neither_removes_nor_exposes_commands(tmp_path: Path) -> None:
    source = FakeSource()
    async with make(tmp_path, source) as rig:
        await rig.ready()
    # a new process: the persisted entities are carried over, unavailable, without a command topic
    source2 = FakeSource()
    source2.status, source2.reason = "unavailable", "read_failed"
    store = JsonStore(tmp_path / "discovery.json", validate=valid_discovery_document)
    async with make(tmp_path, source2, store=store, nonce="t2") as rig2:
        conn = await rig2.ready()
        comps = rig2.components()
        assert "set_battery_low_soc" in comps and "write_result" in comps
        assert "t1" in comps["set_battery_low_soc"]["command_topic"]  # the old, dead generation
        assert {t for t, _ in conn.subscriptions} == {"homeassistant/status"}
        assert rig2.availability("battery_low_soc") == "offline"
        assert not store.data["devices"][DEVICE]["retired"]
        # commands to the dead topic are not read at all
        conn.incoming.put_nowait(
            IncomingMessage(comps["set_battery_low_soc"]["command_topic"], b"40")
        )
        await rig2.idle()
        assert source2.submitted == []
        # the entities come alive with the first fresh read, under a new generation
        source2.status, source2.reason = "ok", None
        rig2.plane.request_refresh("deye1")
        await settle(lambda: "t2" in rig2.components()["set_battery_low_soc"]["command_topic"])
        assert rig2.availability("battery_low_soc") == "online"


async def test_the_generation_token_changes_with_every_session(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        first = rig.command("battery_low_soc")
        conn.drop()
        conn2 = await rig.ready(2)
        second = json.loads(conn2.last(DEVICE).payload)["components"]["set_battery_low_soc"]
        assert second["command_topic"] != first
        assert first not in {t for t, _ in conn2.subscriptions}


# -- command parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "payload", "field"),
    [
        ("grid_charge_enable", b"ON", 1),
        ("grid_charge_enable", b"OFF", 0),
        ("battery_charge_current", b"50", 50),
        ("battery_charge_current", b"50.0", 50),
        ("battery_charge_current", b"0", 0),
        ("smart_load_on_voltage", b"52.25", 5225),
        ("smart_load_on_voltage", b"52", 5200),
        ("tou_slot2_start", b"05:05:00", 505),
        ("tou_slot2_start", b"23:55:00", 2355),
    ],
)
def test_valid_payloads_are_parsed_per_kind(key: str, payload: bytes, field: int) -> None:
    assert parse_command(BY_KEY[key], payload)[1] == field


@pytest.mark.parametrize(
    ("key", "payload"),
    [
        ("grid_charge_enable", b"on"),
        ("grid_charge_enable", b"1"),
        ("grid_charge_enable", b"true"),
        ("grid_charge_enable", b"ON "),
        ("grid_charge_enable", b""),
        ("battery_charge_current", b""),
        ("battery_charge_current", b" 50"),
        ("battery_charge_current", b"abc"),
        ("battery_charge_current", b"nan"),
        ("battery_charge_current", b"inf"),
        ("battery_charge_current", b"1e2"),
        ("battery_charge_current", b"+50"),
        ("battery_charge_current", b"0x32"),
        ("battery_charge_current", b"50,5"),
        ("battery_charge_current", b"\xff\xfe"),
        ("battery_charge_current", b"5" * 40),
        ("battery_charge_current", b'{"value": 5}'),
        ("smart_load_on_voltage", b"52.255"),
        ("tou_slot2_start", b"05:05"),
        ("tou_slot2_start", b"5:05:00"),
        ("tou_slot2_start", b"05:05:30"),
        ("tou_slot2_start", b"05:05:00+01:00"),
        ("tou_slot2_start", b"05:05:00Z"),
        ("tou_slot2_start", b"2026-11-01T05:05:00"),
        ("tou_slot2_start", b"25:00:00"),
        ("tou_slot2_start", b"05:07:00"),  # not a five-minute boundary
    ],
)
def test_malformed_payloads_are_refused(key: str, payload: bytes) -> None:
    with pytest.raises(Refused):
        parse_command(BY_KEY[key], payload)


async def test_a_malformed_command_is_an_event_and_never_a_write(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        rig.send("battery_charge_current", "1e2")
        rig.send("grid_charge_enable", "on")
        rig.send("tou_slot2_start", "05:07:00")
        await settle(lambda: len(rig.events()) == 3)
        assert [e["event_type"] for e in rig.events()] == ["write_rejected"] * 3
        assert [e["reason"] for e in rig.events()] == [
            "invalid_payload",
            "invalid_payload",
            "value_not_exact",
        ]
        assert rig.events()[0]["requested_value"] == "1e2" and rig.events()[0]["setting"] == (
            "battery_charge_current"
        )
        assert rig.source.submitted == []


async def test_a_retained_command_is_dropped_without_a_trace_in_the_service(
    tmp_path: Path,
) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        rig.send("battery_charge_current", "40", retain=True)
        await rig.idle()
        assert rig.source.submitted == [] and rig.events() == []
        assert rig.plane.dropped_retained == 1
        rig.send("battery_charge_current", "40")  # the same payload, not retained, is executed
        await settle(lambda: len(rig.source.submitted) == 1)


# -- execution and results ----------------------------------------------------------------------


async def test_a_confirmed_write_publishes_the_fresh_state_and_the_result(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        rig.send("battery_charge_current", "40")
        await settle(lambda: len(rig.events()) == 1)
        event = rig.events()[0]
        assert event["event_type"] == "write_readback_confirmed"
        assert event["setting"] == "battery_charge_current" and event["requested_value"] == 40
        assert event["audit_id"] == "op-1" and event["reason"] is None and event["value"] == 40
        assert rig.last_state()["battery_charge_current"] == 40
        assert rig.conn.on(RESULT)[-1].retain is False
        assert rig.source.submitted == [("battery_charge_current", "40")]


async def test_a_command_equal_to_the_read_back_is_a_noop(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        rig.send("battery_low_soc", "35")
        await settle(lambda: len(rig.events()) == 1)
        assert rig.events()[0]["event_type"] == "write_noop"
        assert rig.events()[0]["reason"] == "no_change"


async def test_a_noop_found_at_the_send_point_is_a_noop_too(tmp_path: Path) -> None:
    source = FakeSource()
    source.script["battery_low_soc"] = lambda k, v: {
        "operation_id": "op-9",
        "status": "rejected",
        "reason": "no_change",
    }
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_low_soc", "40")
        await settle(lambda: len(rig.events()) == 1)
        assert rig.events()[0]["event_type"] == "write_noop"


async def test_a_rejected_command_shows_the_read_back_never_the_request(tmp_path: Path) -> None:
    source = FakeSource()

    def refuse(key: str, value: Any) -> dict[str, Any]:
        raise Refused("operation_locked", 409, reason="soc_mode_required")

    source.script["battery_low_soc"] = refuse
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_low_soc", "44")
        await settle(lambda: len(rig.events()) == 1)
        event = rig.events()[0]
        assert event["event_type"] == "write_rejected" and event["reason"] == "soc_mode_required"
        assert event["requested_value"] == 44 and event["value"] == 35
        assert rig.last_state()["battery_low_soc"] == 35  # what the unit holds, not 44


@pytest.mark.parametrize(
    ("result", "event_type"),
    [
        ({"status": "failed", "reason": "device_exception"}, "write_failed"),
        ({"status": "outcome_unknown", "reason": "readback_missing_or_mismatch"},
         "write_outcome_unknown"),
        ({"status": "rejected", "reason": "binding_changed"}, "write_rejected"),
    ],
)  # fmt: skip
async def test_every_outcome_has_its_event_type(
    tmp_path: Path, result: dict[str, Any], event_type: str
) -> None:
    source = FakeSource()
    source.script["battery_low_soc"] = lambda k, v: {"operation_id": "op-7", **result}
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_low_soc", "40")
        await settle(lambda: len(rig.events()) == 1)
        assert rig.events()[0]["event_type"] == event_type
        assert rig.events()[0]["reason"] == result["reason"]
        assert rig.events()[0]["audit_id"] == "op-7"


async def test_an_internal_error_is_reported_and_the_queue_goes_on(tmp_path: Path) -> None:
    source = FakeSource()

    def boom(key: str, value: Any) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    source.script["battery_low_soc"] = boom
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_low_soc", "40")
        rig.send("battery_charge_current", "40")
        await settle(lambda: len(rig.events()) == 2)
        assert rig.events()[0]["reason"] == "internal_error"
        assert "secret" not in json.dumps(rig.events())
        assert rig.events()[1]["event_type"] == "write_readback_confirmed"


# -- the queue ----------------------------------------------------------------------------------


async def test_commands_run_in_order_without_coalescing(tmp_path: Path) -> None:
    """OFF -> edit -> ON: the ToU switch is not folded into its last value."""
    source = FakeSource()
    source.hold = asyncio.Event()
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("tou_enable", "OFF")
        rig.send("tou_slot2_start", "06:00:00")
        rig.send("tou_enable", "ON")
        rig.send("tou_enable", "ON")  # a duplicate is a command of its own
        await asyncio.wait_for(source.entered.wait(), 2)
        await rig.idle()
        assert source.submitted == [("tou_enable", 0)]  # the first is in flight, the rest wait
        source.hold.set()
        await settle(lambda: len(rig.events()) == 4)
        assert source.submitted == [
            ("tou_enable", 0),
            ("tou_slot2_start", "06:00:00"),
            ("tou_enable", 1),
            ("tou_enable", 1),
        ]
        assert [e["event_type"] for e in rig.events()] == [
            "write_noop",  # OFF equals the stored 0
            "write_readback_confirmed",
            "write_readback_confirmed",
            "write_noop",
        ]


async def test_a_queued_command_expires(tmp_path: Path) -> None:
    source = FakeSource()
    source.hold = asyncio.Event()
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_charge_current", "40")
        await asyncio.wait_for(source.entered.wait(), 2)
        rig.send("battery_discharge_current", "40")
        await rig.idle()
        rig.now[0] += COMMAND_TTL_S + 1
        source.hold.set()
        await settle(lambda: len(rig.events()) == 2)
        assert [e["reason"] for e in rig.events()] == [None, "expired"]
        assert rig.events()[1]["event_type"] == "write_rejected"
        assert [k for k, _ in source.submitted] == ["battery_charge_current"]


async def test_the_queue_is_bounded_and_says_so(tmp_path: Path) -> None:
    source = FakeSource()
    source.hold = asyncio.Event()
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_charge_current", "41")
        await asyncio.wait_for(source.entered.wait(), 2)
        for n in range(QUEUE_LIMIT + 1):
            rig.send("battery_discharge_current", str(n))
        await settle(lambda: any(e["reason"] == "queue_full" for e in rig.events()))
        full = [e for e in rig.events() if e["reason"] == "queue_full"]
        assert len(full) == 1 and full[0]["requested_value"] == QUEUE_LIMIT


async def test_commands_of_a_lost_session_are_discarded(tmp_path: Path) -> None:
    source = FakeSource()
    source.hold = asyncio.Event()
    async with make(tmp_path, source) as rig:
        conn = await rig.ready()
        rig.send("battery_charge_current", "40")
        await asyncio.wait_for(source.entered.wait(), 2)
        rig.send("battery_discharge_current", "40")
        rig.send("tou_enable", "ON")
        await rig.idle()
        conn.drop()
        conn2 = await rig.ready(2)
        source.hold.set()
        await settle(lambda: len(rig.events(conn2)) >= 2)
        later = [e for e in rig.events(conn2) if e["reason"] == "superseded_context"]
        assert {e["setting"] for e in later} == {"battery_discharge_current", "tou_enable"}
        assert [k for k, _ in source.submitted] == ["battery_charge_current"]


async def test_commands_are_void_when_the_exposed_set_changes(tmp_path: Path) -> None:
    source = FakeSource()
    source.hold = asyncio.Event()
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_charge_current", "40")
        await asyncio.wait_for(source.entered.wait(), 2)
        rig.send("battery_discharge_current", "40")
        await rig.idle()
        source.status, source.reason = "withdrawn", "parallel_slave"  # the role changed
        rig.plane.request_refresh("deye1")
        await settle(lambda: "set_battery_low_soc" not in rig.components())
        source.hold.set()
        await settle(lambda: any(e["reason"] == "superseded_context" for e in rig.events()))
        assert [k for k, _ in source.submitted] == ["battery_charge_current"]


async def test_no_command_is_read_from_a_foreign_topic(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        for topic in (
            "deye/ab12cd34/deye1/write/set/zz/battery_charge_current",
            "deye/ab12cd34/deye1/write/set/battery_charge_current",
            "deye/ab12cd34/deye2/write/set/t11/battery_charge_current",
            rig.command("battery_charge_current") + "/x",
        ):
            conn.incoming.put_nowait(IncomingMessage(topic, b"40"))
        await rig.idle()
        assert rig.source.submitted == [] and rig.events() == []


async def test_diagnostics_count_commands(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        rig.send("battery_charge_current", "40", retain=True)
        rig.send("battery_charge_current", "40")
        await settle(lambda: len(rig.events()) == 1)
        info = rig.pub.diagnostics()["write_entities"]
        assert info["commands_received"] == 1 and info["retained_dropped"] == 1
        assert info["exposed"] == {"deye1": sorted(START_FIELDS)}


# -- review 23: receipt, fresh rejection values, bounded floods -------------------------------


async def test_a_rejection_carries_a_fresh_read_never_the_cached_value(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        assert rig.last_state()["battery_charge_current"] == 50
        rig.source.fields["battery_charge_current"] = 30  # an external change, not yet polled
        rig.send("battery_charge_current", "abc")  # malformed
        await settle(lambda: len(rig.events()) == 1)
        event = rig.events()[0]
        assert (event["reason"], event["value"], event["read_error"]) == (
            "invalid_payload",
            30,
            None,
        )
        assert rig.last_state()["battery_charge_current"] == 30  # the state is republished


async def test_without_a_fresh_read_the_rejection_has_no_value(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        rig.source.status, rig.source.reason = "unavailable", "read_failed"
        rig.send("battery_charge_current", "abc")
        await settle(lambda: len(rig.events()) == 1)
        event = rig.events()[0]
        assert event["value"] is None and event["read_error"] == "read_failed"
        assert event["reason"] == "invalid_payload"


async def test_a_queue_full_and_an_expired_rejection_are_read_fresh_too(tmp_path: Path) -> None:
    source = FakeSource()
    source.hold = asyncio.Event()
    async with make(tmp_path, source) as rig:
        await rig.ready()
        for n in range(QUEUE_LIMIT + 3):
            rig.send("battery_charge_current", str(40 + n % 2))
        await settle(lambda: any(e["reason"] == "queue_full" for e in rig.events()))
        source.fields["battery_discharge_current"] = 77
        rig.send("battery_discharge_current", "abc")
        await settle(lambda: any(e["setting"] == "battery_discharge_current" for e in rig.events()))
        event = next(e for e in rig.events() if e["setting"] == "battery_discharge_current")
        assert event["value"] == 77 and event["read_error"] is None


async def test_a_flood_of_malformed_commands_is_bounded(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        before = rig.source.snapshots
        for _ in range(500):
            rig.send("battery_charge_current", "abc")
        await settle(lambda: rig.plane.diagnostics()["rejections_suppressed"] > 0)
        await rig.idle()
        events = rig.events()
        assert len(events) <= 64 + 8 + 1  # the bound plus the reads' own batches and the summary
        assert any(str(e["reason"]).startswith("suppressed:") for e in events)
        assert rig.source.snapshots - before < 20  # one fresh read per batch, not per command


async def test_a_command_for_a_closed_gate_is_refused_at_receipt_with_that_reason(
    tmp_path: Path,
) -> None:
    source = FakeSource()
    source.gated["battery_low_soc"] = "soc_mode_required"
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_low_soc", "40")
        await settle(lambda: len(rig.events()) == 1)
        assert rig.events()[0]["reason"] == "soc_mode_required"
        assert source.submitted == []


async def test_a_command_is_refused_at_receipt_while_the_unit_is_unreadable(
    tmp_path: Path,
) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        rig.source.status, rig.source.reason = "unavailable", "read_failed"
        rig.plane.request_refresh("deye1")
        await settle(lambda: rig.availability("battery_charge_current") == "offline")
        rig.send("battery_charge_current", "40")
        await settle(lambda: len(rig.events()) == 1)
        assert rig.events()[0]["reason"] == "unavailable" and rig.source.submitted == []


async def test_the_service_refusing_to_bind_at_receipt_is_a_rejection(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        await rig.ready()
        rig.source.bind_error = "inverter_not_ready"
        rig.send("battery_charge_current", "40")
        await settle(lambda: len(rig.events()) == 1)
        assert rig.events()[0]["reason"] == "inverter_not_ready" and rig.source.submitted == []


async def test_the_intent_carries_the_receipt_deadline_and_a_revocation_test(
    tmp_path: Path,
) -> None:
    seen: list[Intent] = []
    source = FakeSource()
    original = source.submit

    async def spy(*args: Any, **kw: Any) -> dict[str, Any]:
        seen.append(kw["intent"])
        return await original(*args, **kw)

    source.submit = spy  # type: ignore[method-assign]
    async with make(tmp_path, source) as rig:
        await rig.ready()
        rig.send("battery_charge_current", "40")
        await settle(lambda: len(seen) == 1)
        assert seen[0].deadline == 1000.0 + COMMAND_TTL_S and seen[0].binding.serial == "SN1"
        assert seen[0].revoked() is False
        rig.plane._revoke_all()  # a session end revokes what has been accepted
        assert seen[0].revoked() is True


async def test_a_unit_that_was_not_ready_is_read_again_within_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live-27: the first read right after a start found the unit still identifying; the entities
    must not wait a whole settings period for the next one."""
    monkeypatch.setattr("deye_monitor.mqtt_settings.RETRY_FIRST_S", 5.0)
    source = FakeSource()
    source.status, source.reason = "unavailable", "identifying"
    poll = PollConfig(settings_s=300.0)
    plane = SettingsPlane(NS, {"deye1": poll}, source)
    await plane.refresh("deye1")
    assert plane.view("deye1")["status"] == "unavailable"
    assert plane.view("deye1")["reason"] == "identifying"
    assert plane._retry_in("deye1") == 5.0
    await plane.refresh("deye1")
    assert plane._retry_in("deye1") == 10.0
    source.status, source.reason = "ok", None
    await plane.refresh("deye1")
    assert plane.view("deye1")["status"] == "exposed"
    assert plane._retry_in("deye1") == 300.0


async def test_the_refresh_loop_retries_and_logs_each_change(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("deye_monitor.mqtt_settings.RETRY_FIRST_S", 0.02)
    source = FakeSource()
    source.status, source.reason = "unavailable", "identifying"
    plane = SettingsPlane(NS, {"deye1": PollConfig(settings_s=300.0)}, source)
    caplog.set_level("INFO", logger="deye_monitor.mqtt_settings")
    plane.begin_session()
    plane.start()
    try:
        await settle(lambda: source.snapshots >= 3)
        assert plane.view("deye1")["status"] == "unavailable"
        source.status, source.reason = "ok", None
        await settle(lambda: plane.view("deye1")["status"] == "exposed")
    finally:
        await plane.stop()
    lines = [r.getMessage() for r in caplog.records if "write entities" in r.getMessage()]
    assert lines == [
        "deye1: write entities unavailable (identifying)",
        "deye1: write entities exposed (fresh read)",
    ]


async def test_forced_refreshes_keep_the_recovery_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successive RUNNING transitions (also right after a failed read) are coalesced and serviced
    no sooner than RETRY_FIRST_S after the previous attempt; the first one is prompt."""
    gap = 0.05
    monkeypatch.setattr("deye_monitor.mqtt_settings.RETRY_FIRST_S", gap)
    source = FakeSource()
    source.status, source.reason = "unavailable", "identifying"
    stamps: list[float] = []
    plane = SettingsPlane(NS, {"deye1": PollConfig(settings_s=300.0)}, source)
    original = plane.refresh

    async def stamped(inverter_id: str) -> None:
        stamps.append(time.monotonic())
        await original(inverter_id)

    monkeypatch.setattr(plane, "refresh", stamped)
    plane.begin_session()
    plane.start()
    try:
        await settle(lambda: len(stamps) >= 1)
        assert time.monotonic() - stamps[0] < gap  # the first readiness refresh was prompt
        for _ in range(4):
            plane.request_refresh("deye1")
            plane.request_refresh("deye1")  # coalesced
            await asyncio.sleep(gap / 5)
        await settle(lambda: len(stamps) >= 3)
    finally:
        await plane.stop()
    assert all(b - a >= gap * 0.95 for a, b in itertools.pairwise(stamps))
    assert len(stamps) <= 4  # not one cycle per request


class _SchedulerRig:
    """The refresh loop on a fake clock with the production gap: every sleep it asks for is
    recorded and advances the clock; after ``limit`` sleeps it parks."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
        monkeypatch.setattr("deye_monitor.mqtt_settings.RETRY_FIRST_S", 5.0)
        self.now = 0.0
        self.waits: list[float] = []
        self.limit = limit
        self.parked = asyncio.Event()
        rig = self

        class _Timeout:
            def __init__(self, delay: float) -> None:
                self.delay = delay

            async def __aenter__(self) -> None:
                rig.waits.append(self.delay)

            async def __aexit__(self, *exc: object) -> bool:
                return False

        class _Kick(asyncio.Event):
            async def wait(self) -> Literal[True]:
                await asyncio.sleep(0)
                if len(rig.waits) >= rig.limit:
                    rig.parked.set()
                    await asyncio.Event().wait()
                rig.now += rig.waits[-1]
                raise TimeoutError

        monkeypatch.setattr("deye_monitor.mqtt_settings.asyncio.timeout", _Timeout)
        self.kick_cls = _Kick

    def plane(self, source: FakeSource, polls: dict[str, PollConfig]) -> SettingsPlane:
        plane = SettingsPlane(NS, polls, source, clock=lambda: self.now)
        plane._kick = self.kick_cls()
        return plane


async def test_a_short_settings_period_does_not_spin_inside_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _SchedulerRig(monkeypatch, limit=8)
    source = FakeSource()
    plane = rig.plane(source, {"deye1": PollConfig(settings_s=1.0)})
    reads: list[float] = []
    original = plane.refresh

    async def stamped(inverter_id: str) -> str | None:
        reads.append(rig.now)
        return await original(inverter_id)

    monkeypatch.setattr(plane, "refresh", stamped)
    plane.begin_session()
    plane.start()
    try:
        await asyncio.wait_for(rig.parked.wait(), 5)
    finally:
        await plane.stop()
    assert reads[:4] == [0.0, 5.0, 10.0, 15.0]
    assert rig.waits == [5.0] * 8  # one sleep per gap, never a zero-second spin


async def test_a_force_during_another_inverters_read_is_serviced_when_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _SchedulerRig(monkeypatch, limit=2)
    source = FakeSource()
    plane = rig.plane(
        source, {"deye1": PollConfig(settings_s=300.0), "deye2": PollConfig(settings_s=300.0)}
    )
    reads: list[tuple[str, float]] = []
    original = plane.refresh

    async def stamped(inverter_id: str) -> str | None:
        reads.append((inverter_id, rig.now))
        if inverter_id == "deye2" and len(reads) == 2:
            plane.request_refresh("deye1")  # the already visited one, during deye2's read
            rig.now += 6.0
        return await original(inverter_id)

    monkeypatch.setattr(plane, "refresh", stamped)
    plane.begin_session()
    plane.start()
    try:
        await asyncio.wait_for(rig.parked.wait(), 5)
    finally:
        await plane.stop()
    assert reads[:3] == [("deye1", 0.0), ("deye2", 0.0), ("deye1", 6.0)]
