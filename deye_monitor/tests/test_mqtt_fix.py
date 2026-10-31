"""T8-Fix (code review 5): opaque energy series, offline on every session end, curated
diagnostics, reconnect jitter, ledger purge."""

from __future__ import annotations

import asyncio
import json
import math
import random
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.device import InverterState
from deye_monitor.ha_discovery import (
    CONFLICT_LIST_KEY,
    INVERTER_DIAGNOSTICS,
    R65_DIAGNOSTICS,
    app_components,
    firmware_board,
    inverter_diagnostic_components,
    inverter_diagnostic_values,
)
from deye_monitor.mqtt import MqttPublisher, PublisherSettings
from tests.fakes.mqtt import FakeBroker, mqtt_error, settle
from tests.selection_helpers import A, plant
from tests.test_mqtt_publisher import ENERGY, NS, make, running, snap, state_topic

TOPIC = NS.discovery_topic(NS.inverter_device_id("deye1"))
APP = NS.app_status_topic


def live_keys(doc: dict[str, Any]) -> set[str]:
    """Live sensor components (the inverter's own ``diag_*`` entities are no selected sensors)."""
    return {
        k
        for k, v in doc["components"].items()
        if set(v) != {"platform"} and not k.startswith("diag_")
    }


# -- T8-02: the series is an opaque identity, never ordered across namespaces -------------------


async def test_new_namespace_can_restart_its_local_generation_at_zero(tmp_path: Path) -> None:
    async with make(tmp_path, keys=(ENERGY,)) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_state("deye1", snap("energy", series="g7"), {ENERGY: 1.0})
        await settle(lambda: conn.on(state_topic("energy")))
        # authorised recovery: new namespace, generation 0, much newer lifecycle epoch
        rig.pub.on_state(
            "deye1", snap("energy", cycle=2, epoch=99, series="x7k2m9q4g0"), {ENERGY: 5.0}
        )
        await settle(lambda: len(conn.on(state_topic("energy"))) == 2)
        assert conn.last(state_topic("energy")).json()["energy_series"] == "x7k2m9q4g0"
        doc = conn.on(TOPIC)[-1].json()
        assert f"{ENERGY}_x7k2m9q4g0" in live_keys(doc)
        assert doc["components"][f"{ENERGY}_g7"] == {"platform": "sensor"}
        # the retired series is never revived, whatever its number
        rig.pub.on_state("deye1", snap("energy", cycle=3, epoch=99, series="g7"), {ENERGY: 9.0})
        rig.pub.on_state("deye1", snap("energy", cycle=4, epoch=99, series="g0"), {ENERGY: 9.0})
        await rig.idle()
        assert len(conn.on(state_topic("energy"))) == 2


async def test_new_provider_namespace_is_not_overridden_by_old_ledger(tmp_path: Path) -> None:
    async with make(tmp_path, keys=(ENERGY,), series_of=lambda _i: "g7") as rig:
        conn = await rig.ready()
        assert f"{ENERGY}_g7" in live_keys(conn.on(TOPIC)[-1].json())
    async with make(tmp_path, keys=(ENERGY,), series_of=lambda _i: "x7k2m9q4g0") as rig2:
        conn2 = await rig2.ready()
        doc = conn2.on(TOPIC)[0].json()
        assert live_keys(doc) >= {f"{ENERGY}_x7k2m9q4g0"}
        assert f"{ENERGY}_g7" not in live_keys(doc)
        assert doc["components"][f"{ENERGY}_g7"] == {"platform": "sensor"}
        running(rig2)
        rig2.pub.on_state("deye1", snap("energy", series="x7k2m9q4g0"), {ENERGY: 2.0})
        await settle(lambda: conn2.on(state_topic("energy")))
    # a later start must not fall back to the retired g7 either
    async with make(tmp_path, keys=(ENERGY,), series_of=lambda _i: "g7") as rig3:
        conn3 = await rig3.ready()
        assert f"{ENERGY}_x7k2m9q4g0" in live_keys(conn3.on(TOPIC)[0].json())


async def test_same_number_in_different_namespaces_is_a_different_series(tmp_path: Path) -> None:
    async with make(tmp_path, keys=(ENERGY,)) as rig:
        conn = await rig.ready()
        running(rig)
        for cycle, series in enumerate(["g1", "abcd1234g1", "g1", "abcd1234g1", "wxyz9876g1"], 1):
            rig.pub.on_state("deye1", snap("energy", cycle=cycle, series=series), {ENERGY: 1.0})
            await rig.idle()
        got = [s.json()["energy_series"] for s in conn.on(state_topic("energy"))]
        # the old "g1" (same number, other namespace) is retired and dropped; the
        # current series repeats
        assert got == ["g1", "abcd1234g1", "abcd1234g1", "wxyz9876g1"]


# -- T8-03: every session end withdraws "online" ------------------------------------------------


async def test_discovery_error_with_live_connection_withdraws_online(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        await settle(lambda: rig.pub.diagnostics()["connected"])

        def refuse(_new: Any) -> None:
            raise OSError("disk full")

        rig.store.commit = refuse  # type: ignore[method-assign,assignment]
        rig.pub.set_selection("deye1", ("battery_soc",), 1)  # the inventory cannot be written
        await settle(lambda: conn.last(APP).text == "offline")
        assert conn.last(NS.inverter_availability_topic("deye1")).text == "offline"
        # while the failure persists, no new session ever announces "online"
        conn2 = await rig.broker.wait_connections(2)
        await asyncio.sleep(0.15)
        assert all(s.text != "online" for s in conn2.on(APP))
        assert rig.pub.last_discovery_error is not None
        assert not conn.aborted  # the orderly offline was delivered: no abort needed


async def test_failed_app_offline_aborts_the_connection_so_the_will_fires(tmp_path: Path) -> None:
    broker = FakeBroker()
    broker.fail_publish = lambda s: (
        mqtt_error("lost") if s.topic == APP and s.payload == b"offline" else None
    )
    async with make(tmp_path, broker=broker) as rig:
        conn = await rig.ready()
        conn.drop()  # the session ends; the app offline cannot be sent
        await settle(lambda: conn.aborted)
        assert (await rig.ready(2)).number == 2


async def test_shutdown_budget_of_the_app_is_independent_of_the_inverters(tmp_path: Path) -> None:
    broker = FakeBroker()
    slow = {"on": False}

    def fail(s: Any) -> Exception | None:
        if slow["on"] and s.topic == NS.inverter_availability_topic("deye1"):
            return mqtt_error("inverter publish fails")
        return None

    broker.fail_publish = fail
    async with make(tmp_path, broker=broker) as rig:
        conn = await rig.ready()
        slow["on"] = True
        rig.pub.request_stop()
        assert rig.task is not None
        await asyncio.wait_for(rig.task, 5)
        assert conn.last(APP).text == "offline"  # still delivered
        assert not conn.aborted


# -- T8-04: curated diagnostics -----------------------------------------------------------------


def full_diag(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": "running",
        "reason": "identified",
        "active_path": "primary",
        "endpoint": "gw1",
        "bound_serial": A,
        "identity": {
            "firmware_words": {15: 0x1001, 17: 0x1000, 18: 0x0070},
            "role": {"parallel_enable": True, "master": False, "phase": "B"},
        },
        "r65": {"raw": 0x0104, "last_read_ok": True},
        "energy": {
            "effective_resolution": 0.1,
            "source": "r65",
            "blocked": None,
            "pending_change": None,
            "series": "x7k2m9q4g0",
        },
        "capability_conflicts": [{"code": "mppt_count", "detail": "R22 reports 1 MPPT"}],
        "bus_utilisation": 0.123456,
        "transport": {"primary": {"timeouts": 3, "frame_errors": 1, "resyncs": 2}},
        "cooldown_active": False,
        "last_success_wall": 1_700_000_000.0,
    }
    return {**base, **over}


def test_every_contract_field_is_derived_and_unknown_is_null() -> None:
    v = inverter_diagnostic_values(full_diag())
    assert v["path"] == "primary:gw1" and v["serial"] == A
    assert v["firmware_mcu1"] == "1000-0070" and v["firmware_mcu2"] == "????-1001-????"
    assert "role" not in v
    assert v["r65_raw"] == 0x0104 and v["r65_status"] == "ok"
    assert v["energy_resolution"] == 0.1 and v["energy_source"] == "R65"
    assert v["energy_status"] == "confirmed" and v["energy_series"] == "x7k2m9q4g0"
    assert v["capability_conflicts"] == 1
    assert v[CONFLICT_LIST_KEY] == [{"code": "mppt_count", "detail": "R22 reports 1 MPPT"}]
    assert v["bus_utilisation"] == 0.123
    assert (v["transport_timeouts"], v["transport_frame_errors"], v["transport_resyncs"]) == (
        3,
        1,
        2,
    )
    assert v["transport_cooldown"] == "inactive" and v["last_success"] == "2023-11-14T22:13:20Z"
    assert {d.suffix for d in INVERTER_DIAGNOSTICS} <= set(v)
    empty = inverter_diagnostic_values(None)
    assert all(empty[d.suffix] is None for d in INVERTER_DIAGNOSTICS)
    assert empty[CONFLICT_LIST_KEY] == []
    assert all(
        x is None for k, x in inverter_diagnostic_values({}).items() if k != CONFLICT_LIST_KEY
    )


def test_energy_status_shows_blocking_and_pending_changes() -> None:
    blocked = inverter_diagnostic_values(full_diag(energy={"blocked": "energy_store_corrupt"}))
    assert blocked["energy_status"] == "blocked: energy_store_corrupt"
    assert blocked["energy_resolution"] is None  # unknown stays null, never 0
    pending = inverter_diagnostic_values(
        full_diag(energy={"effective_resolution": 1.0, "pending_change": 0.1})
    )
    assert pending["energy_status"] == "change_pending"
    failed = inverter_diagnostic_values(full_diag(r65={"raw": 0x0104, "last_read_ok": False}))
    assert failed["r65_status"] == "failed" and failed["r65_raw"] == 0x0104  # last VALID reading


def test_diagnostics_are_curated_not_passed_through() -> None:
    hostile = full_diag(
        password="s3cret",
        mqtt={"password": "s3cret"},
        bound_serial="x" * 500,
        bus_utilisation=math.nan,
        energy={"effective_resolution": True, "series": "../../etc", "source": 5},
        capability_conflicts=[{"code": "a" * 500, "detail": {"x": 1}}] * 50,
        transport={"primary": {"timeouts": "many"}},
        last_success_wall=math.inf,
        cooldown_active="yes",
    )
    v = inverter_diagnostic_values(hostile)
    text = json.dumps(v, allow_nan=False)
    assert "s3cret" not in text and set(v) == {
        d.suffix for d in (*INVERTER_DIAGNOSTICS, *R65_DIAGNOSTICS)
    } | {CONFLICT_LIST_KEY}
    assert len(v["serial"]) <= 120 and v["bus_utilisation"] is None
    assert v["energy_resolution"] is None and v["energy_series"] is None
    assert v["energy_source"] is None and v["transport_timeouts"] is None
    assert v["last_success"] is None and v["transport_cooldown"] is None
    assert len(v[CONFLICT_LIST_KEY]) == 20 and v["capability_conflicts"] == 50


def test_inverter_device_has_diagnostic_entities_for_every_contract_field() -> None:
    states = tuple(x.value for x in InverterState)
    comps = inverter_diagnostic_components(NS, "deye1", states, show_r65=True)
    for d in (*INVERTER_DIAGNOSTICS, *R65_DIAGNOSTICS):
        comp = comps[f"diag_{d.suffix}"]
        assert comp["entity_category"] == "diagnostic" and comp["platform"] == "sensor"
        assert comp["state_topic"] == NS.inverter_diagnostics_topic("deye1")
        assert comp["unique_id"] == f"deye_ab12cd34_deye1_diag_{d.suffix}"
        assert "deye1" not in comp["name"].lower()  # no inverter prefix: the device carries it
        assert "command_topic" not in comp
        assert comp["availability"] == [{"topic": NS.app_status_topic}]  # visible while offline
    conflicts = comps["diag_capability_conflicts"]
    assert conflicts["json_attributes_topic"] == NS.inverter_diagnostics_topic("deye1")
    assert comps["diag_last_success"]["device_class"] == "timestamp"
    assert comps["diag_transport_timeouts"]["state_class"] == "total_increasing"
    assert comps["diag_state"]["options"] == list(states)
    assert comps["diag_firmware_mcu1"]["name"] == "Firmware MCU1"
    assert comps["diag_firmware_mcu2"]["name"] == "Firmware MCU2"
    assert "diag_firmware" not in comps
    assert "diag_role" not in comps  # the parallel role duplicates the inverter's own entities
    assert not [
        k
        for k in inverter_diagnostic_components(NS, "deye1", states, show_r65=False)
        if k.startswith("diag_r65")
    ]


def test_the_app_device_keeps_only_app_wide_entities() -> None:
    assert set(app_components(NS)) == {
        "app_version",
        "mqtt_reconnects",
        "inverters_online",
        "inverters_configured",
    }


def test_firmware_is_shown_like_the_deye_cloud() -> None:
    words = {14: 0x2005, 15: 0x1175, 16: 0x1807, 17: 0x1001, 18: 0xC070}
    assert firmware_board(words, "mcu1") == "1001-C070"
    assert firmware_board(words, "mcu2") == "2005-1175-1807"
    partial = {15: 0x1175, 17: 0x1001, 18: 0xC070}
    assert firmware_board(partial, "mcu2") == "????-1175-????"
    assert firmware_board({}, "mcu1") is None and firmware_board(None, "mcu2") is None
    out = inverter_diagnostic_values({"identity": {"firmware_words": words}})
    assert out["firmware_mcu1"] == "1001-C070" and out["firmware_mcu2"] == "2005-1175-1807"


async def test_required_energy_diagnostics_are_not_discarded(tmp_path: Path) -> None:
    settings = PublisherSettings(
        reconnect_min_s=0.01, reconnect_max_s=0.04, diagnostics_min_interval_s=0.0
    )
    live = full_diag()
    async with make(tmp_path, settings=settings, diagnostics_of=lambda _i: live) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.on_diagnostics("deye1", {"event": "energy_changed", **live})
        topic = NS.inverter_diagnostics_topic("deye1")
        await settle(lambda: conn.last(topic).json().get("r65_raw"))
        body = conn.last(topic).json()
        assert body["r65_raw"] == 0x0104
        assert body["energy_resolution"] == 0.1
        assert body["energy_source"] == "R65"
        assert body["energy_series"] == "x7k2m9q4g0"
        assert body["capability_conflicts"] == 1
        assert body["bus_utilisation"] == 0.123
        assert not conn.last(topic).retain


async def test_diagnostics_update_is_throttled(tmp_path: Path) -> None:
    settings = PublisherSettings(
        reconnect_min_s=0.01, reconnect_max_s=0.04, diagnostics_min_interval_s=0.3
    )
    async with make(tmp_path, settings=settings) as rig:
        conn = await rig.ready()
        await rig.idle()
        before = len(conn.on(NS.app_state_topic))
        for i in range(30):
            rig.pub.on_diagnostics("deye1", {"event": "tick", "n": i})
        await asyncio.sleep(0.1)
        assert len(conn.on(NS.app_state_topic)) - before <= 1
        await settle(lambda: len(conn.on(NS.app_state_topic)) - before >= 1)  # but it arrives


async def test_secret_never_reaches_the_published_diagnostics(tmp_path: Path) -> None:
    settings = PublisherSettings(
        reconnect_min_s=0.01, reconnect_max_s=0.04, diagnostics_min_interval_s=0.0
    )
    async with make(tmp_path, settings=settings) as rig:
        conn = await rig.ready()
        rig.pub.on_diagnostics("deye1", {"password": "s3cret", "mqtt": {"password": "s3cret"}})
        await rig.idle()
        assert all(b"s3cret" not in s.payload for s in conn.sent)


async def test_device_layer_tracks_r65_serial_and_cooldown(tmp_path: Path) -> None:
    async with plant(tmp_path) as p:
        inv = p.rig.inv("deye1")
        assert inv.diagnostics()["r65"] == {"raw": None, "last_read_ok": None}
        await inv.step()
        d = inv.diagnostics()
        assert d["r65"] == {"raw": 0x0104, "last_read_ok": True}
        assert d["bound_serial"] == A and d["cooldown_active"] is False
        # a failed later reading keeps the last valid raw value but reports the failure
        inv._note_r65(None)
        d = inv.diagnostics()
        assert d["r65"] == {"raw": 0x0104, "last_read_ok": False}
        values = inverter_diagnostic_values(d)
        assert values["r65_status"] == "failed" and values["serial"] == A
        assert values["firmware_mcu1"] == "1000-0070"
        assert values["firmware_mcu2"] == "002B-1001-0031"


# -- T8-05: jitter ------------------------------------------------------------------------------


def test_default_reconnect_jitter_is_not_constant(tmp_path: Path) -> None:
    pub = MqttPublisher.__new__(MqttPublisher)
    del pub  # constructed below through the public signature
    from deye_monitor.config import PollConfig
    from deye_monitor.mqtt import InverterInfo
    from deye_monitor.mqtt_discovery import valid_discovery_document
    from deye_monitor.store import JsonStore
    from tests.test_mqtt_publisher import PROFILE

    real = MqttPublisher(
        NS,
        [InverterInfo("deye1", "Deye 1", PROFILE, PollConfig())],
        JsonStore(tmp_path / "d.json", validate=valid_discovery_document),
        FakeBroker(),
    )
    values = {real._rand() for _ in range(20)}
    assert len(values) > 10 and all(0.0 <= v < 1.0 for v in values)
    assert real._rand is random.random


async def test_backoff_uses_the_injected_random_values_and_resets_after_a_stable_session(
    tmp_path: Path,
) -> None:
    broker = FakeBroker()
    broker.connect_errors = [mqtt_error("x") for _ in range(4)]
    values = iter([0.0, 1.0, 0.5, 0.25] + [0.0] * 20)
    settings = PublisherSettings(
        reconnect_min_s=0.01, reconnect_max_s=0.05, stable_after_s=0.2, publish_timeout_s=1.0
    )
    async with make(tmp_path, broker=broker, settings=settings, rand=lambda: next(values)) as rig:
        conn = await rig.ready()
        # delay doubles 0.01, 0.02, 0.04, 0.05(cap), each multiplied by 1 + 0.25 * r, capped
        expected = [0.01, 0.02 * 1.25, 0.04 * 1.125, min(0.05 * 1.0625, 0.05)]
        assert rig.sleeps[:4] == pytest.approx(expected)
        await asyncio.sleep(0.25)  # a stable session ...
        conn.drop()
        await rig.ready(2)
        assert rig.sleeps[4] == pytest.approx(0.01)  # ... starts the backoff over


# -- T8-01: purge -------------------------------------------------------------------------------


async def test_publisher_purge_is_explicit_scoped_and_republishes(tmp_path: Path) -> None:
    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        rig.pub.set_selection("deye1", ("battery_soc",), 1)
        await settle(lambda: rig.pub.diagnostics()["retired_components"] == 3)
        conn.birth()
        conn.birth()
        conn.birth()
        await rig.idle()
        assert rig.pub.diagnostics()["retired_components"] == 3  # births never forget
        with pytest.raises(KeyError):
            rig.pub.purge_cleanup_ledger("nobody")
        assert rig.pub.purge_cleanup_ledger("deye1") == 3
        await settle(lambda: rig.pub.diagnostics()["retired_components"] == 0)
        await settle(lambda: live_keys(conn.on(TOPIC)[-1].json()) == {"battery_soc"})
        components = conn.on(TOPIC)[-1].json()["components"]
        assert {k for k in components if not k.startswith("diag_")} == {"battery_soc"}


async def test_ledger_full_is_visible_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import deye_monitor.mqtt_discovery as module

    async with make(tmp_path) as rig:
        conn = await rig.ready()
        running(rig)
        monkeypatch.setattr(module, "MAX_RETIRED", 1)
        rig.pub.set_selection("deye1", ("battery_soc",), 1)  # would retire 3
        await settle(lambda: conn.last(APP).text == "offline")
        assert (rig.pub.last_discovery_error or "").startswith("discovery_ledger_full")
        assert rig.pub.diagnostics()["ledger_limit"] == 2000
        monkeypatch.undo()
        await settle(lambda: conn.last(APP).text == "offline")
