"""Review 9 contracts of the plant change (T14): transaction, rollback, identity, lifecycle, probes.

Real service/API on local fake devices and a fake MQTT broker; nothing leaves the machine."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.device import InverterState
from deye_monitor.selection import Edit
from tests.fakes.servers import Behavior
from tests.plant_helpers import A, B, plant_rig
from tests.test_plant import create_gw, create_inv, err


def live(rig: Any) -> Any:
    runtime = rig.app.runtime
    assert runtime is not None
    return runtime


def refuse(*args: Any, **kwargs: Any) -> None:
    raise OSError("review: plant disk write refused")


async def test_failed_deletion_restores_the_accepted_sensor_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.manager.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
        old = rig.manager.store.records["deye1"]
        monkeypatch.setattr(rig.store, "commit", refuse)
        status, body = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True}
        )
        now = rig.manager.store.records["deye1"]
        assert status == 500 and err(body) == "persist_failed"
        assert now == old, "Failed deletion lost the accepted sensor selection"


async def test_failed_rollback_cannot_leave_the_rejected_generation_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        old = rig.service.plant.inverters[0]
        real = rig.app.install

        def reject_old(cfg: Any) -> Any:
            if cfg.inverters and cfg.inverters[0].name == old.name:
                raise RuntimeError("review: old generation cannot be rebuilt")
            return real(cfg)

        monkeypatch.setattr(rig.app, "install", reject_old)
        monkeypatch.setattr(rig.store, "commit", refuse)
        status, body = await rig.call(
            "PUT",
            "/api/plant/inverters/deye1",
            {"id": "deye1", "name": "Rejected new generation", "endpoint": "gw1", "unit_id": 1},
        )
        observation = {
            "status": status,
            "error": err(body),
            "accepted_name": rig.service.plant.inverters[0].name,
            "runtime_name": rig.app.cfg.inverters[0].name,
            "running": rig.app.running,
            "recovery_required": sorted(rig.manager.recovery_required),
        }
        assert err(body) == "plant_recovery_failed" and not rig.app.running, observation


async def test_rejected_expected_serial_does_not_replace_the_accepted_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        assert live(rig).book.bound("deye1") == A
        monkeypatch.setattr(rig.store, "commit", refuse)
        status, body = await rig.call(
            "PUT",
            "/api/plant/inverters/deye1",
            {"id": "deye1", "name": "deye1", "endpoint": "gw1", "unit_id": 1, "expected_serial": B},
        )
        observation = {
            "status": status,
            "error": err(body),
            "accepted_expected_serial": rig.service.plant.inverters[0].expected_serial,
            "actual_binding": live(rig).book.bound("deye1"),
            "identity_file": json.loads((tmp_path / "identity.json").read_text()),
        }
        assert status == 500 and err(body) == "persist_failed"
        assert live(rig).book.bound("deye1") == A, observation


async def test_deleted_id_does_not_resurrect_an_old_choice_after_prune_write_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.manager.change(0, {"deye1": Edit(choose=True)})
        monkeypatch.setattr(rig.manager.store, "commit", refuse)
        status, body = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True}
        )
        assert status == 200, body
    # Restart on the durably empty plant: the stale selection record is only logged/discarded.
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_inv(rig)
        assert rig.manager.selection("deye1").chosen is False
    # Restart again, before the user has chosen sensors for this newly created inverter.
    async with plant_rig(tmp_path, monkeypatch) as rig:
        selection = rig.manager.selection("deye1")
        assert selection.chosen is False and not selection.active, (
            "Deleted selection silently resurrected"
        )


async def test_unsaved_connection_on_an_explicit_shared_bus_uses_the_same_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        status, body = await rig.call(
            "PUT", "/api/plant/endpoints/gw1", rig.endpoint_body("gw1", bus_id="shared")
        )
        assert status == 200, body
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        server = rig.servers["gw1"]
        server.default = Behavior(delay_s=0.15)
        runtime = live(rig)
        request = asyncio.create_task(
            runtime.schedulers["shared"].run(
                lambda: runtime.transports["gw1"].read_holding(1, 3, 5)
            )
        )
        for _ in range(100):
            if server.active:
                break
            await asyncio.sleep(0.005)
        assert server.active == 1
        try:
            status, body = await rig.call(
                "POST",
                "/api/plant/test",
                {
                    "endpoint": rig.endpoint_body(
                        "gw1", id="draft", host="localhost", bus_id="shared"
                    ),
                    "unit_id": 1,
                },
            )
        finally:
            await request
        observation = {
            "status": status,
            "path": body.get("path"),
            "ok": body.get("ok"),
            "connections": server.connections,
            "max_active": server.max_active,
        }
        assert status == 200 and body["ok"], body
        assert server.max_active == 1, observation


async def test_a_live_probe_cannot_reopen_the_retired_generation_after_a_plant_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        await asyncio.sleep(0.05)
        old = live(rig)
        scheduler = old.schedulers[old.cfg.endpoint("gw1").bus]
        transport = old.transports["gw1"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_gap(delay: float) -> None:
            task = asyncio.current_task()
            if task is None or not task.get_name().startswith("inverter-"):
                entered.set()
                await release.wait()
            else:
                await asyncio.sleep(delay)

        # Hold the probe at the scheduler's normal inter-request gap while the plant is edited.
        monkeypatch.setattr(scheduler, "_gap_s", 0.2)
        monkeypatch.setattr(scheduler, "_sleep", held_gap)
        request = asyncio.create_task(
            rig.call(
                "POST", "/api/plant/test", {"endpoint_id": "gw1", "unit_id": 1, "inverter": "deye1"}
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        try:
            status, body = await rig.call(
                "PUT",
                "/api/plant/inverters/deye1",
                {"id": "deye1", "name": "new generation", "endpoint": "gw1", "unit_id": 1},
            )
            assert status == 200, body
            assert rig.app.runtime is not old and transport._writer is None
            release.set()
            status, result = await asyncio.wait_for(request, 5)
            reopened = transport._writer is not None
            observation = {
                "status": status,
                "probe_ok": result.get("ok"),
                "probe_path": result.get("path"),
                "old_generation_reopened": reopened,
                "connections": rig.servers["gw1"].connections,
            }
            assert not reopened, observation
        finally:
            release.set()
            if not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            await transport.close()  # the reviewer's old transport; the app only owns the new one


async def test_control_nonremoving_change_keeps_the_accepted_choice_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.manager.change(0, {"deye1": Edit(remove=frozenset({"battery_soc"}))})
        old = rig.manager.store.records["deye1"]
        monkeypatch.setattr(rig.store, "commit", refuse)
        status, body = await rig.call(
            "PUT",
            "/api/plant/inverters/deye1",
            {"id": "deye1", "name": "new name", "endpoint": "gw1", "unit_id": 1},
        )
        assert status == 500 and err(body) == "persist_failed"
        assert rig.manager.store.records["deye1"] == old


async def test_control_saved_connection_reuses_the_live_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_plant import test_a_saved_inverter_is_tested_through_its_running_connection

    await test_a_saved_inverter_is_tested_through_its_running_connection(tmp_path, monkeypatch)


async def test_failed_deletion_never_retires_discovery_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import deye_monitor.app as app_module
    from deye_monitor.mqtt import PublisherSettings
    from tests.fakes.mqtt import FakeBroker, settle
    from tests.test_mqtt_fix import live_keys

    broker = FakeBroker()
    real = app_module.build_publisher

    def build(*args: Any, **kwargs: Any) -> Any:
        return real(
            *args,
            **kwargs,
            factory=broker,
            settings=PublisherSettings(
                reconnect_min_s=0.01, reconnect_max_s=0.04, discovery_settle_s=0.01
            ),
        )

    monkeypatch.setattr(app_module, "build_publisher", build)
    async with plant_rig(tmp_path, monkeypatch, options={"mqtt": {"host": "127.0.0.1"}}) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.manager.change(0, {"deye1": Edit(add=frozenset({"battery_soc"}))})
        topic = "homeassistant/device/deye_testinst_deye1/config"

        def docs() -> list[Any]:
            return [m.json() for c in broker.connections for m in c.on(topic)]

        await settle(lambda: any("battery_soc" in live_keys(d) for d in docs()))
        before = len(docs())
        monkeypatch.setattr(rig.store, "commit", refuse)
        status, body = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True}
        )
        assert status == 500 and err(body) == "persist_failed"
        await asyncio.sleep(0.05)
        after = docs()[before:]
        retired = any(d["components"].get("battery_soc") == {"platform": "sensor"} for d in after)
        assert not retired, "Rejected deletion published a persisted discovery tombstone"


async def test_an_accepted_expected_serial_is_bound_after_the_plant_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        status, body = await rig.call(
            "PUT",
            "/api/plant/inverters/deye1",
            {"id": "deye1", "name": "deye1", "endpoint": "gw1", "unit_id": 1, "expected_serial": B},
        )
        assert status == 200, body
        bindings = json.loads((tmp_path / "identity.json").read_text())["data"]["bindings"]
        assert bindings["deye1"] == {"serial": B, "source": "expected"}
        assert live(rig).book.bound("deye1") == B


async def test_a_new_inverter_binds_nothing_while_its_plant_change_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        monkeypatch.setattr(rig.store, "commit", refuse)
        status, body = await rig.call(
            "POST",
            "/api/plant/inverters",
            {"id": "deye1", "name": "deye1", "endpoint": "gw1", "unit_id": 1},
        )
        assert status == 500 and err(body) == "persist_failed", body
        identity = tmp_path / "identity.json"
        assert not identity.exists() or "deye1" not in identity.read_text()


async def test_no_plant_change_after_a_failed_recovery_until_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        real = rig.app.install

        def fail_old(cfg: Any) -> Any:
            if cfg.inverters and cfg.inverters[0].name == "deye1":
                raise RuntimeError("review: accepted generation cannot be rebuilt")
            return real(cfg)

        monkeypatch.setattr(rig.app, "install", fail_old)
        monkeypatch.setattr(rig.store, "commit", refuse)
        body = {"id": "deye1", "name": "other", "endpoint": "gw1", "unit_id": 1}
        status, reply = await rig.call("PUT", "/api/plant/inverters/deye1", body)
        assert err(reply) == "plant_recovery_failed" and not rig.app.running
        monkeypatch.undo()
        status, reply = await rig.call("PUT", "/api/plant/inverters/deye1", {**body, "name": "x"})
        assert status == 409 and err(reply) == "plant_recovery_failed", reply
        _, session = await rig.call("GET", "/api/session")
        assert {"code": "plant_recovery_failed", "differs": False} in session["plant_notices"]


async def test_a_stale_selection_record_must_be_removed_before_the_plant_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.manager.change(0, {"deye1": Edit(choose=True)})
        monkeypatch.setattr(rig.manager.store, "commit", refuse)
        status, body = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True}
        )
        assert status == 200, body
    # restart: the record of the deleted id is still on disk
    async with plant_rig(tmp_path, monkeypatch) as rig:
        assert rig.manager.store.pruned
        monkeypatch.setattr(rig.manager.store, "commit", refuse)
        await rig.add_server("gw1", "modbus_tcp")
        status, body = await rig.call(
            "POST",
            "/api/plant/inverters",
            {"id": "deye1", "name": "deye1", "endpoint": "gw1", "unit_id": 1},
        )
        assert status == 500 and err(body) == "persist_failed", body
        assert rig.service.plant.inverters == ()


async def test_a_waiting_probe_is_ended_by_a_plant_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        rig.servers["gw1"].default = Behavior(delay_s=2.0)
        probe = asyncio.create_task(
            rig.call("POST", "/api/plant/test", {"endpoint_id": "gw1", "unit_id": 1})
        )
        await asyncio.sleep(0.2)
        status, body = await rig.call(
            "PUT",
            "/api/plant/inverters/deye1",
            {"id": "deye1", "name": "renamed", "endpoint": "gw1", "unit_id": 1},
        )
        assert status == 200, body
        status, reply = await asyncio.wait_for(probe, 5)
        assert status == 409 and err(reply) == "plant_changed_during_test", reply
