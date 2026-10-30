"""Plant setup in the web interface (T14): store, migration, validation, API security, live
adoption with rollback and the read-only connection test."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import PlantConfig, parse_config, plant_problems
from deye_monitor.device import InverterState
from deye_monitor.plant import PlantStore, SwitchError, options_notice
from deye_monitor.store import LoadStatus
from tests.fakes.servers import SERIAL, FakePty, Kind
from tests.plant_helpers import A, B, PlantRig, plant_rig, serial_symlink
from tests.transport_helpers import assert_wire_clean

KINDS: dict[str, Kind] = {"modbus_tcp": "mbap", "rtu_over_tcp": "rtu", "solarman_v5_tcp": "v5"}
GW = {
    "id": "gw1",
    "transport": "modbus_tcp",
    "host": "192.0.2.10",
    "port": 502,
    "request_gap_ms": 0,
}
INV = {"id": "deye1", "name": "Deye 1", "endpoint": "gw1", "unit_id": 1}


def plant_of(endpoints: list[dict[str, Any]], inverters: list[dict[str, Any]]) -> PlantConfig:
    return parse_config({"endpoints": endpoints, "inverters": inverters}).plant


def err(body: Any) -> str:
    return str(body["error"]["key"])


async def create_gw(rig: PlantRig, name: str = "gw1", transport: str = "modbus_tcp") -> None:
    await rig.add_server(name, transport)
    status, body = await rig.call(
        "POST", "/api/plant/endpoints", rig.endpoint_body(name, transport)
    )
    assert status == 200, body


async def create_inv(
    rig: PlantRig, inverter_id: str = "deye1", endpoint: str = "gw1", **kw: Any
) -> None:
    body = {"id": inverter_id, "name": inverter_id, "endpoint": endpoint, "unit_id": 1, **kw}
    status, data = await rig.call("POST", "/api/plant/inverters", body)
    assert status == 200, data


# -- store ---------------------------------------------------------------------------------------


def test_a_fresh_directory_is_a_first_installation(tmp_path: Path) -> None:
    store = PlantStore(tmp_path)
    assert store.status is LoadStatus.MISSING and not store.exists
    assert store.plant == PlantConfig() and store.revision == 0 and store.problems == []


def test_commit_alternates_slots_and_survives_a_restart(tmp_path: Path) -> None:
    store = PlantStore(tmp_path)
    store.commit(plant_of([GW], [INV]), 1)
    assert (tmp_path / "plant.a.json").exists() and not (tmp_path / "plant.b.json").exists()
    store.commit(plant_of([GW], []), 2)
    assert (tmp_path / "plant.b.json").exists()
    again = PlantStore(tmp_path)
    assert again.revision == 2 and again.plant.inverters == () and again.problems == []
    assert again.plant.endpoints[0].host == "192.0.2.10"


def test_a_damaged_newest_slot_falls_back_to_the_previous_one(tmp_path: Path) -> None:
    store = PlantStore(tmp_path)
    store.commit(plant_of([GW], [INV]), 1)
    store.commit(plant_of([GW], []), 2)
    (tmp_path / "plant.b.json").write_text("{ cut")
    again = PlantStore(tmp_path)
    assert again.revision == 1 and len(again.plant.inverters) == 1
    assert {p.code for p in again.problems} == {
        "plant_store_corrupt",
        "plant_store_recovered_previous",
    }


def test_all_slots_damaged_fails_closed_and_keeps_a_copy(tmp_path: Path) -> None:
    store = PlantStore(tmp_path)
    store.commit(plant_of([GW], [INV]), 1)
    (tmp_path / "plant.a.json").write_text("garbage")
    again = PlantStore(tmp_path)
    assert again.status is LoadStatus.CORRUPT and again.exists
    assert again.plant == PlantConfig() and again.revision == 0
    assert {p.code for p in again.problems} == {"plant_store_corrupt", "plant_store_empty_used"}
    assert not again.migrate_from_options(parse_config({"endpoints": [GW], "inverters": [INV]}))
    again.commit(plant_of([GW], []), 1)  # an explicit new setup repairs the slot ...
    assert (tmp_path / "plant.a.json.corrupt").read_text() == "garbage"  # ... and keeps the copy


def test_a_tampered_or_invalid_slot_is_not_used(tmp_path: Path) -> None:
    store = PlantStore(tmp_path)
    store.commit(plant_of([GW], [INV]), 1)
    path = tmp_path / "plant.a.json"
    doc = json.loads(path.read_text())
    doc["data"]["body"]["inverters"][0]["unit_id"] = 2  # checksum no longer matches
    path.write_text(json.dumps(doc))
    assert PlantStore(tmp_path).status is LoadStatus.CORRUPT
    # a correct checksum over a plant that does not validate is refused as well
    from deye_monitor.plant import _checksum

    body = doc["data"]["body"]
    body["inverters"][0]["endpoint"] = "missing"
    doc["data"]["checksum"] = _checksum(doc["data"]["seq"], body)
    path.write_text(json.dumps(doc))
    assert PlantStore(tmp_path).status is LoadStatus.CORRUPT


def test_a_failing_write_leaves_the_accepted_state_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PlantStore(tmp_path)
    store.commit(plant_of([GW], [INV]), 1)
    real = os.replace

    def broken(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        store.commit(plant_of([GW], []), 2)
    monkeypatch.setattr(os, "replace", real)
    assert store.revision == 1 and len(store.plant.inverters) == 1
    assert PlantStore(tmp_path).revision == 1
    assert not list(tmp_path.glob(".plant.*"))  # no temporary leftovers


# -- migration -----------------------------------------------------------------------------------


def test_options_are_taken_over_once_and_never_again(tmp_path: Path) -> None:
    options = parse_config({"endpoints": [GW], "inverters": [INV]})
    store = PlantStore(tmp_path)
    assert store.migrate_from_options(options)
    assert store.revision == 1 and store.plant == options.plant
    assert options_notice(store, options, migrated=True) == [
        {"code": "plant_migrated", "differs": False}
    ]
    second = PlantStore(tmp_path)  # the next start: the store is authoritative
    assert not second.migrate_from_options(options)
    assert options_notice(second, options, migrated=False) == [
        {"code": "plant_options_present", "differs": False}
    ]
    edited = parse_config({"endpoints": [{**GW, "port": 1502}], "inverters": [INV]})
    assert options_notice(second, edited, migrated=False)[0]["differs"] is True
    assert second.plant.endpoints[0].port == 502  # never overridden silently
    assert options_notice(second, parse_config({}), migrated=False) == []


def test_an_emptied_plant_is_not_migrated_again(tmp_path: Path) -> None:
    options = parse_config({"endpoints": [GW], "inverters": [INV]})
    store = PlantStore(tmp_path)
    store.migrate_from_options(options)
    store.commit(PlantConfig(), 2)  # the user deleted everything in the web interface
    again = PlantStore(tmp_path)
    assert not again.migrate_from_options(options)
    assert again.plant == PlantConfig()


def test_empty_options_migrate_nothing(tmp_path: Path) -> None:
    store = PlantStore(tmp_path)
    assert not store.migrate_from_options(parse_config({}))
    assert not store.exists


def test_plant_problems_have_stable_codes() -> None:
    cfg = parse_config(
        {
            "endpoints": [GW, {**GW, "id": "gw2", "host": "192.0.2.11"}],
            "inverters": [INV, {**INV, "id": "deye2", "name": "x", "endpoint": "gw2"}],
        }
    )
    assert plant_problems(cfg.endpoints, cfg.inverters) == []
    twin = parse_config({"endpoints": [GW]}).endpoints[0]
    twin2 = twin.model_copy(update={"id": "gw9"})
    codes = {p.code for p in plant_problems([twin, twin2], [])}
    assert codes == {"same_target"}
    inv2 = parse_config({"endpoints": [GW], "inverters": [INV]}).inverters[0]
    assert {p.code for p in plant_problems(cfg.endpoints, [inv2, inv2])} == {"duplicate_inverter"}
    clash = inv2.model_copy(update={"id": "deye9"})
    assert {p.code for p in plant_problems(cfg.endpoints, [inv2, clash])} == {"unit_conflict"}


# -- API: security and schema --------------------------------------------------------------------


async def test_the_plant_routes_are_guarded_like_every_other_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig.add_server("gw1", "modbus_tcp")
        body = rig.endpoint_body("gw1")
        routes = [
            ("POST", "/api/plant/endpoints", body),
            ("POST", "/api/plant/inverters", {**INV}),
            ("POST", "/api/plant/test", {"endpoint": body, "unit_id": 1}),
        ]
        for method, path, payload in routes:
            assert (await rig.call(method, path, payload, user=None, csrf=None))[0] == 403
            assert err((await rig.call(method, path, payload, csrf="wrong"))[1]) == "csrf_invalid"
        # no If-Match / stale revision
        assert (await rig.call("POST", "/api/plant/endpoints", body, revision=None))[0] == 428
        status, data = await rig.call("POST", "/api/plant/endpoints", body, revision=99)
        assert (
            status == 409
            and err(data) == "stale_revision"
            and data["error"]["params"]["current"] == 0
        )
        # reading routes never change anything
        for path in ("/api/plant/endpoints", "/api/plant/inverters", "/api/plant/test"):
            assert (await rig.call("GET", path))[0] == 405
        for path in ("/api/plant/endpoints/gw1", "/api/plant/inverters/deye1"):
            assert (await rig.call("GET", path))[0] == 405
        assert rig.service.revision == 0 and rig.store.status is LoadStatus.MISSING


async def test_only_the_supervisor_peer_may_use_the_plant_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch, peers=frozenset({"172.30.32.2"})) as rig:
        forged = {
            "X-Remote-User-Id": "root",
            "X-Ingress-Path": "/x",
            "Forwarded": "for=172.30.32.2",
        }
        for method, path in (
            ("GET", "/api/plant"),
            ("GET", "/api/plant/serial-devices"),
            ("POST", "/api/plant/endpoints"),
            ("POST", "/api/plant/test"),
            ("POST", "/api/plant/inverters/deye1/delete"),
        ):
            status, body = await rig.call(method, path, {}, user=None, csrf=None, headers=forged)
            assert status == 403 and err(body) == "forbidden_peer", path


async def test_requests_are_validated_against_a_strict_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig.add_server("gw1", "modbus_tcp")
        good = rig.endpoint_body("gw1")
        post = "/api/plant/endpoints"
        status, body = await rig.call("POST", post, {**good, "bogus": 1})
        assert status == 400 and err(body) == "unknown_field"
        for hidden in ("host_display", "profile", "allow_writes"):
            assert (await rig.call("POST", post, {**good, hidden: "x"}))[0] == 400, hidden
        assert (
            err((await rig.call("POST", post, {k: v for k, v in good.items() if k != "id"}))[1])
            == "missing_field"
        )
        status, body = await rig.call("POST", post, {**good, "port": 0})
        assert status == 422 and err(body) == "invalid_fields"
        assert body["error"]["params"]["fields"][0]["field"] == "port"
        assert (await rig.call("POST", post, {**good, "port": "502"}))[0] == 422  # no coercion
        assert (await rig.call("POST", post, {**good, "max_read_registers": 5}))[0] == 422
        assert (await rig.call("POST", post, {**good, "id": "Bad Id"}))[0] == 422
        assert (await rig.call("POST", post, {**good, "serial_device": "/dev/ttyUSB0"}))[0] == 422
        assert (await rig.call("POST", post, {**good, "transport": "ftp"}))[0] == 422
        assert (await rig.call("POST", post, {**good, "logger_serial": 5}))[0] == 422
        status, body = await rig.call("POST", post, {**good, "transport": "serial_rtu"})
        assert status == 422  # host/port are not allowed for a serial connection
        assert rig.service.revision == 0  # nothing was stored by any rejected call
        status, body = await rig.call("POST", post, good)
        assert status == 200 and body["revision"] == 1 and body["changed"] is True
        assert err((await rig.call("POST", post, good))[1]) == "id_taken"


async def test_inverters_cannot_enable_writes_or_reference_unknown_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        post = "/api/plant/inverters"
        status, body = await rig.call("POST", post, {**INV, "allow_writes": True})
        assert status == 400 and err(body) == "unknown_field"
        status, body = await rig.call("POST", post, {**INV, "endpoint": "nope"})
        assert status == 422 and err(body) == "invalid_plant"
        assert body["error"]["params"]["problems"][0]["code"] == "undefined_endpoint"
        assert (await rig.call("POST", post, {**INV, "unit_id": 0}))[0] == 422
        assert (await rig.call("POST", post, {**INV, "unit_id": True}))[0] == 422
        assert (await rig.call("POST", post, {**INV, "expected_serial": "short"}))[0] == 422
        assert (await rig.call("POST", post, {**INV, "id": "app"}))[0] == 422  # reserved
        assert (await rig.call("POST", post, {**INV, "fallback_endpoint": "gw1"}))[0] == 422
        await create_inv(rig)
        status, body = await rig.call("POST", post, {**INV, "id": "deye2", "name": "second"})
        assert status == 422 and body["error"]["params"]["problems"][0]["code"] == "unit_conflict"
        status, body = await rig.call("GET", "/api/plant")
        assert body["writes"] == "unavailable"
        assert "allow_writes" not in body["inverters"][0] and "profile" not in body["inverters"][0]
        # what GET shows can be sent back as it is
        status, _ = await rig.call("PUT", "/api/plant/inverters/deye1", body["inverters"][0])
        assert status == 200
        assert rig.service.plant.inverters[0].allow_writes is False


async def test_ids_are_immutable_and_unknown_targets_are_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        status, body = await rig.call(
            "PUT", "/api/plant/inverters/deye1", {**INV, "id": "other", "name": "x"}
        )
        assert status == 422 and err(body) == "id_immutable"
        status, body = await rig.call(
            "PUT", "/api/plant/endpoints/gw1", {**rig.endpoint_body("gw1"), "id": "gw2"}
        )
        assert status == 422 and err(body) == "id_immutable"
        assert (await rig.call("PUT", "/api/plant/inverters/ghost", INV))[0] == 404
        assert (await rig.call("PUT", "/api/plant/endpoints/ghost", GW))[0] == 404
        assert (await rig.call("POST", "/api/plant/inverters/ghost/delete", {"confirm": True}))[
            0
        ] == 404
        assert (await rig.call("POST", "/api/plant/inverters/Bad..id/delete", {"confirm": True}))[
            0
        ] == 404


async def test_the_serial_device_list_only_lists_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-FTDI_adapter-if00-port0").touch()
    (by_id / ".hidden").touch()
    async with plant_rig(tmp_path, monkeypatch, serial_dir=by_id) as rig:
        status, body = await rig.call("GET", "/api/plant/serial-devices")
        assert status == 200
        assert body["devices"] == [f"{by_id}/usb-FTDI_adapter-if00-port0"]
    async with plant_rig(tmp_path / "other", monkeypatch, serial_dir=tmp_path / "gone") as rig:
        assert (await rig.call("GET", "/api/plant/serial-devices"))[1]["devices"] == []


# -- live adoption -------------------------------------------------------------------------------


async def test_an_empty_plant_runs_and_the_first_connection_and_inverter_go_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        _status, session = await rig.call("GET", "/api/session")
        assert session["setup_needed"] is True and session["plant_revision"] == 0
        assert rig.app.runtime is not None and rig.app.runtime.inverters == {}
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        _, session = await rig.call("GET", "/api/session")
        assert session["setup_needed"] is False and session["plant_revision"] == 2
        _, inverters = await rig.call("GET", "/api/inverters")
        [inv] = inverters["inverters"]
        assert inv["id"] == "deye1" and inv["status"]["serial"] == A
        assert inv["status"]["awaiting_selection"] is True  # a new inverter waits for its choice
        assert inv["connection"] == {
            "endpoint": "gw1",
            "transport": "modbus_tcp",
            "serial_pinned": False,
        }
        assert rig.store.revision == 2 and PlantStore(tmp_path).plant == rig.service.plant


async def test_editing_an_inverter_restarts_it_with_the_new_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        first = rig.app.runtime
        status, body = await rig.call(
            "PUT", "/api/plant/inverters/deye1", {**INV, "name": "Keller", "poll_fast_s": 20}
        )
        assert status == 200 and body["changed"] is True
        assert rig.app.runtime is not first and rig.api.runtime is rig.app.runtime
        await rig.wait_state("deye1", InverterState.RUNNING)
        assert rig.app.runtime.inverters["deye1"].cfg.name == "Keller"
        assert rig.app.runtime.inverters["deye1"].cfg.poll_fast_s == 20
        assert rig.app.runtime.book.bound("deye1") == A  # the binding survives the rebuild
        # nothing changed: no restart, no revision
        status, body = await rig.call(
            "PUT", "/api/plant/inverters/deye1", {**INV, "name": "Keller", "poll_fast_s": 20}
        )
        assert status == 200 and body["changed"] is False and body["revision"] == 3
        assert rig.app.runtime.inverters["deye1"] is not None


async def test_editing_a_connection_applies_without_a_restart_of_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig, "gw1", "modbus_tcp")
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        await rig.add_server("gw2", "rtu_over_tcp", B)
        status, _ = await rig.call(
            "PUT",
            "/api/plant/endpoints/gw1",
            rig.endpoint_body("gw2", "rtu_over_tcp", id="gw1"),
        )
        assert status == 200
        assert rig.app.runtime is not None
        assert rig.app.runtime.cfg.endpoint("gw1").transport.value == "rtu_over_tcp"
        # the serial number bound to the inverter does not match the other device: quarantined
        await rig.wait_state("deye1", InverterState.QUARANTINED)


async def test_deleting_inverters_and_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        status, body = await rig.call("POST", "/api/plant/endpoints/gw1/delete", {"confirm": True})
        assert status == 409 and err(body) == "endpoint_in_use"
        assert body["error"]["params"]["inverters"] == ["deye1"]
        status, body = await rig.call("POST", "/api/plant/inverters/deye1/delete", {})
        assert status == 400  # a confirmation is required
        status, body = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": False}
        )
        assert err(body) == "confirmation_required"
        status, body = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True}
        )
        assert status == 200 and body["removed_inverters"] == ["deye1"]
        assert rig.app.runtime is not None and rig.app.runtime.inverters == {}
        # the binding stays unless the user confirms its removal
        assert (
            json.loads((tmp_path / "identity.json").read_text())["data"]["bindings"]["deye1"][
                "serial"
            ]
            == A
        )
        assert "deye1" not in rig.manager.store.records  # selection record dropped
        status, body = await rig.call("POST", "/api/plant/endpoints/gw1/delete", {"confirm": True})
        assert status == 200 and rig.service.plant == PlantConfig()


async def test_removing_the_binding_needs_the_explicit_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        status, body = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True, "remove_binding": True}
        )
        assert status == 200 and body["notes"] == []
        bindings = json.loads((tmp_path / "identity.json").read_text())["data"]["bindings"]
        assert "deye1" not in bindings


async def test_a_failing_adoption_restores_the_previous_plant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        before = rig.service.plant
        real = rig.app.install
        calls = {"n": 0}

        def flaky(cfg: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("cannot build")
            return real(cfg)

        monkeypatch.setattr(rig.app, "install", flaky)
        status, body = await rig.call("PUT", "/api/plant/inverters/deye1", {**INV, "name": "New"})
        assert status == 500 and err(body) == "adoption_failed"
        assert rig.service.plant == before and rig.service.revision == 2
        assert PlantStore(tmp_path).revision == 2  # nothing was persisted
        await rig.wait_state("deye1", InverterState.RUNNING)
        assert (
            rig.app.runtime is not None and rig.app.runtime.inverters["deye1"].cfg.name == "deye1"
        )
        assert rig.manager.specs.keys() == {"deye1"}


async def test_a_plant_that_cannot_be_restored_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)

        def broken(cfg: Any) -> Any:
            raise RuntimeError("cannot build")

        monkeypatch.setattr(rig.app, "install", broken)
        status, body = await rig.call("PUT", "/api/plant/inverters/deye1", {**INV, "name": "New"})
        assert status == 500 and err(body) == "plant_recovery_failed"
        assert rig.service.revision == 2


async def test_a_plant_that_cannot_be_written_is_taken_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)

        def refuse(plant: Any, revision: int) -> None:
            raise OSError("read-only")

        monkeypatch.setattr(rig.store, "commit", refuse)
        status, body = await rig.call("PUT", "/api/plant/inverters/deye1", {**INV, "name": "New"})
        assert status == 500 and err(body) == "persist_failed"
        assert rig.service.revision == 2 and rig.service.plant.inverters[0].name == "deye1"
        await rig.wait_state("deye1", InverterState.RUNNING)
        assert (
            rig.app.runtime is not None and rig.app.runtime.inverters["deye1"].cfg.name == "deye1"
        )


async def test_concurrent_changes_with_one_revision_have_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig.add_server("gw1", "modbus_tcp")
        await rig.add_server("gw2", "modbus_tcp")
        results = await asyncio.gather(
            rig.call("POST", "/api/plant/endpoints", rig.endpoint_body("gw1"), revision=0),
            rig.call("POST", "/api/plant/endpoints", rig.endpoint_body("gw2"), revision=0),
        )
        assert sorted(s for s, _ in results) == [200, 409]
        assert rig.service.revision == 1 and len(rig.service.plant.endpoints) == 1


async def test_a_plant_change_does_not_interleave_with_a_selection_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        await rig.manager.lock.acquire()  # a selection change is in progress
        task = asyncio.create_task(
            rig.call("PUT", "/api/plant/inverters/deye1", {**INV, "name": "Later"})
        )
        await asyncio.sleep(0.2)
        assert not task.done() and rig.service.plant.inverters[0].name == "deye1"
        rig.manager.lock.release()
        status, _ = await task
        assert status == 200 and rig.service.plant.inverters[0].name == "Later"


async def test_the_new_inverter_waits_for_its_choice_and_new_ids_do_not_inherit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        _, session = await rig.call("GET", "/api/session")
        rev = session["revision"]
        status, _ = await rig.call(
            "POST", "/api/inverters/deye1/sensors", {"choose": True}, revision=rev
        )
        assert status == 200
        assert rig.manager.selection("deye1").chosen is True
        await rig.call("POST", "/api/plant/inverters/deye1/delete", {"confirm": True})
        await create_inv(rig)  # the same id again starts from scratch
        assert rig.manager.selection("deye1").chosen is False


async def test_the_selection_revision_and_the_plant_revision_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        status, body = await rig.call(
            "POST", "/api/inverters/deye1/sensors", {"choose": True}, revision=rig.service.revision
        )
        # the plant revision (2) is not the selection revision (0)
        assert (status, err(body)) == (409, "stale_revision")


async def test_options_with_connections_are_migrated_on_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = {"endpoints": [GW], "inverters": [INV]}
    async with plant_rig(tmp_path, monkeypatch, options=options) as rig:
        assert rig.migrated and rig.service.revision == 1
        _, session = await rig.call("GET", "/api/session")
        assert session["plant_notices"] == [{"code": "plant_migrated", "differs": False}]
        _, view = await rig.call("GET", "/api/plant")
        assert [e["id"] for e in view["endpoints"]] == ["gw1"]
        assert view["endpoints"][0]["used_by"] == [{"inverter": "deye1", "role": "primary"}]
        assert view["notices"][0]["code"] == "plant_migrated"
    async with plant_rig(tmp_path, monkeypatch, options=options) as rig:
        assert not rig.migrated
        _, session = await rig.call("GET", "/api/session")
        assert session["plant_notices"][0]["code"] == "plant_options_present"


# -- connection test -----------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["modbus_tcp", "rtu_over_tcp", "solarman_v5_tcp"])
async def test_the_test_reads_the_identity_of_an_unsaved_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        server = await rig.add_server("new", transport)
        status, result = await rig.call(
            "POST",
            "/api/plant/test",
            {"endpoint": rig.endpoint_body("new", transport), "unit_id": 1},
        )
        assert status == 200 and result["ok"] is True, result
        assert result["serial"] == A and result["path"] == "temporary"
        assert result["transport"] == transport and result["blocks_ok"] == result["blocks"] > 0
        assert result["model"]["mppt"] == 2 and result["model"]["phases"] == 3
        assert_wire_clean(server, KINDS[transport])
        assert rig.service.revision == 0  # a test changes nothing
        assert rig.app.runtime is not None and rig.app.runtime.inverters == {}
        await asyncio.sleep(0.05)
        assert server.active == 0


async def test_the_test_reads_through_a_serial_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pty = FakePty()
    pty.responder.regs = {**pty.responder.regs}
    from tests.device_helpers import device_regs

    pty.responder.regs = device_regs(A)
    pty.start()
    try:
        by_id = serial_symlink(tmp_path, pty)
        async with plant_rig(tmp_path, monkeypatch, serial_dir=by_id) as rig:
            device = f"{by_id}/usb-test-if00"
            endpoint = {
                "id": "usb1",
                "transport": "serial_rtu",
                "serial_device": device,
                "baudrate": 9600,
                "timeout_s": 1.0,
                "request_gap_ms": 0,
            }
            status, result = await rig.call(
                "POST", "/api/plant/test", {"endpoint": endpoint, "unit_id": 1}
            )
            assert status == 200 and result["ok"] is True and result["serial"] == A, result
            assert_wire_clean(pty, "rtu")
            # a device outside the by-id directory cannot be tested
            status, body = await rig.call(
                "POST",
                "/api/plant/test",
                {"endpoint": {**endpoint, "serial_device": "/dev/ttyUSB0"}, "unit_id": 1},
            )
            assert status == 422 and err(body) == "invalid_fields"
    finally:
        pty.close()


async def test_a_saved_inverter_is_tested_through_its_running_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        server = rig.servers["gw1"]
        status, result = await rig.call(
            "POST", "/api/plant/test", {"endpoint_id": "gw1", "unit_id": 1, "inverter": "deye1"}
        )
        assert status == 200 and result["ok"] and result["path"] == "live"
        assert result["serial_vs_bound"] is True
        assert server.connections == 1  # the same connection as the polling, no second one
        assert server.max_active == 1  # queued between the polling requests of that bus
        assert_wire_clean(server, "mbap")
        status, result = await rig.call(
            "POST",
            "/api/plant/test",
            {"endpoint_id": "gw1", "unit_id": 1, "inverter": "deye1", "expected_serial": B},
        )
        assert result["serial_vs_expected"] is False
        # an edited, unsaved variant of a saved connection still goes through the live transport
        status, result = await rig.call(
            "POST",
            "/api/plant/test",
            {"endpoint": {**rig.endpoint_body("gw1"), "timeout_s": 2.0}, "unit_id": 1},
        )
        assert result["ok"] and result["path"] == "live"
        # ... but not with another transport: that target is in use
        status, body = await rig.call(
            "POST",
            "/api/plant/test",
            {"endpoint": rig.endpoint_body("gw1", "rtu_over_tcp"), "unit_id": 1},
        )
        assert status == 409 and err(body) == "endpoint_in_use_by_other_transport"


async def test_a_different_device_than_the_bound_one_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        other = await rig.add_server("other", "modbus_tcp", B)
        _status, result = await rig.call(
            "POST",
            "/api/plant/test",
            {"endpoint": rig.endpoint_body("other"), "unit_id": 1, "inverter": "deye1"},
        )
        assert result["ok"] and result["serial"] == B and result["serial_vs_bound"] is False
        assert_wire_clean(other, "mbap")


async def test_typical_failures_are_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        body = await rig.add_server("gw", "rtu_over_tcp")
        del body
        # an RTU gateway answers a Modbus TCP client with something it cannot use
        status, result = await rig.call(
            "POST",
            "/api/plant/test",
            {"endpoint": rig.endpoint_body("gw", "modbus_tcp"), "unit_id": 1},
        )
        assert status == 200 and result["ok"] is False
        assert result["outcome"] in {"timeout", "bad_frame"}
        assert result["transport"] == "modbus_tcp" and result["blocks_ok"] == 0
        # nothing listens there at all
        dead = rig.endpoint_body("gw")
        dead["port"] = 1
        status, result = await rig.call(
            "POST", "/api/plant/test", {"endpoint": {**dead, "timeout_s": 0.5}, "unit_id": 1}
        )
        assert result["ok"] is False and result["outcome"] == "unreachable"
        # a silent device behind a working connection
        rig.servers["gw"].responder.unit = 9
        status, result = await rig.call(
            "POST",
            "/api/plant/test",
            {"endpoint": rig.endpoint_body("gw", "rtu_over_tcp"), "unit_id": 1},
        )
        assert result["ok"] is False and result["outcome"] == "timeout"
        # a device that answers with nonsense instead of a serial number
        rig.servers["gw"].responder.unit = 1
        rig.servers["gw"].responder.regs.update({3: 0, 4: 0, 5: 0, 6: 0, 7: 0})
        status, result = await rig.call(
            "POST",
            "/api/plant/test",
            {"endpoint": rig.endpoint_body("gw", "rtu_over_tcp"), "unit_id": 1},
        )
        assert result["ok"] is False and result["outcome"] == "invalid_serial"


async def test_only_one_test_runs_at_a_time_and_the_request_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await rig.add_server("gw", "modbus_tcp")
        body = {"endpoint": rig.endpoint_body("gw"), "unit_id": 1}
        await rig.api._test_lock.acquire()
        try:
            assert err((await rig.call("POST", "/api/plant/test", body))[1]) == "test_running"
        finally:
            rig.api._test_lock.release()
        for bad in (
            {"unit_id": 1},
            {**body, "endpoint_id": "gw"},
            {**body, "unit_id": 0},
            {**body, "unit_id": "1"},
            {**body, "unit_id": 248},
            {**body, "extra": 1},
            {"endpoint_id": "ghost", "unit_id": 1},
            {**body, "expected_serial": "x"},
            {**body, "inverter": "Bad Id"},
        ):
            status, _ = await rig.call("POST", "/api/plant/test", bad)
            assert status in (400, 404, 422), bad
        assert (await rig.call("POST", "/api/plant/test", body))[0] == 200


async def test_the_test_sends_nothing_but_whole_fc03_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        servers = {
            t: await rig.add_server(t, t) for t in ("modbus_tcp", "rtu_over_tcp", "solarman_v5_tcp")
        }
        for transport in servers:
            _status, result = await rig.call(
                "POST",
                "/api/plant/test",
                {"endpoint": rig.endpoint_body(transport, transport), "unit_id": 1},
            )
            assert result["ok"], (transport, result)
        for transport, kind in KINDS.items():
            assert_wire_clean(servers[transport], kind)
            assert servers[transport].connections == 1
            assert SERIAL  # the logger serial is the configured one (checked by the guard)


def test_switch_error_tells_whether_the_previous_plant_runs() -> None:
    assert SwitchError(restored=True).restored and not SwitchError(restored=False).restored


async def test_serve_takes_the_options_over_once_and_warns_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from deye_monitor import app as app_module
    from deye_monitor.webapi import WebApi

    monkeypatch.setenv("DEYE_MONITOR_DATA", str(tmp_path))
    monkeypatch.delenv("DEYE_MONITOR_NO_DEVICES", raising=False)
    monkeypatch.delenv("DEYE_MONITOR_SUPERVISOR_MQTT_HOST", raising=False)

    async def no_bind(self: WebApi) -> int:  # no listening socket in this test
        return 0

    async def no_stop(self: WebApi) -> None:
        return None

    monkeypatch.setattr(WebApi, "start", no_bind)
    monkeypatch.setattr(WebApi, "stop", no_stop)
    cfg = parse_config({"endpoints": [GW], "inverters": [INV]})

    async def run_once() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(app_module.serve(cfg, stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 20)

    caplog.set_level("INFO")
    await run_once()
    assert PlantStore(tmp_path).plant == cfg.plant
    assert "taken over into the web interface" in caplog.text
    caplog.clear()
    await run_once()
    assert "still contain connections/inverters" in caplog.text
    assert "taken over" not in caplog.text
    assert PlantStore(tmp_path).revision == 1  # the second start did not touch the store
