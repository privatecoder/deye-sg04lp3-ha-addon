"""After a failed recovery (nothing runs, the app has to be restarted) - nachprüfung 8: every
changing call is refused before any side effect, reading stays available, and a probe resolves its
saved connection inside its generation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.device import InverterState
from tests.plant_helpers import PlantRig, plant_rig
from tests.test_plant import create_gw, create_inv, err

MUTATIONS = [
    ("PUT", "/api/inverters/deye1/features"),
    ("POST", "/api/inverters/deye1/sensors"),
    ("POST", "/api/inverters/deye1/sensors/preview"),
    ("POST", "/api/inverters/deye1/template/restore"),
    ("POST", "/api/inverters/deye1/rebind"),
    ("POST", "/api/transfer/preview"),
    ("POST", "/api/transfer"),
    ("POST", "/api/stores/reset"),
    ("POST", "/api/cleanup/purge"),
    ("POST", "/api/plant/endpoints"),
    ("PUT", "/api/plant/endpoints/gw1"),
    ("POST", "/api/plant/endpoints/gw1/delete"),
    ("POST", "/api/plant/inverters"),
    ("PUT", "/api/plant/inverters/deye1"),
    ("POST", "/api/plant/inverters/deye1/delete"),
    ("POST", "/api/plant/test"),
]


def refuse(*args: Any) -> None:
    raise OSError("review plant commit failure")


def files(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


async def break_recovery(
    rig: PlantRig, monkeypatch: pytest.MonkeyPatch, *, request: tuple[str, str, Any]
) -> None:
    """Reject the plant commit AND make the accepted generation impossible to rebuild."""
    real = rig.app.install

    def fail_old(cfg: Any) -> Any:
        if cfg.inverters and cfg.inverters[0].name == "deye1":
            raise RuntimeError("review: accepted generation cannot be rebuilt")
        return real(cfg)

    monkeypatch.setattr(rig.app, "install", fail_old)
    monkeypatch.setattr(rig.store, "commit", refuse)
    method, path, body = request
    status, reply = await rig.call(method, path, body)
    assert status == 500 and err(reply) == "plant_recovery_failed", reply
    assert rig.service.recovery_failed and not rig.app.running
    monkeypatch.undo()


RENAME = (
    "PUT",
    "/api/plant/inverters/deye1",
    {"id": "deye1", "name": "new", "endpoint": "gw1", "unit_id": 1},
)


@pytest.mark.parametrize(("method", "path"), MUTATIONS)
async def test_every_changing_route_is_refused_in_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        await break_recovery(rig, monkeypatch, request=RENAME)
        before = files(tmp_path)
        counters = (rig.manager.revision, rig.service.revision, rig.api.action_counter)
        binding = rig.app.runtime.book.bound("deye1")  # type: ignore[union-attr]
        status, reply = await rig.call(method, path, {"confirm": True})
        assert status == 409 and err(reply) == "plant_recovery_failed", (path, reply)
        assert files(tmp_path) == before
        assert (rig.manager.revision, rig.service.revision, rig.api.action_counter) == counters
        assert rig.app.runtime.book.bound("deye1") == binding  # type: ignore[union-attr]


async def test_reading_survives_a_failed_recovery_after_a_rejected_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        real = rig.app.install

        def fail_old(cfg: Any) -> Any:
            if cfg.inverters:
                raise RuntimeError("review: old nonempty generation cannot restart")
            return real(cfg)

        monkeypatch.setattr(rig.app, "install", fail_old)
        monkeypatch.setattr(rig.store, "commit", refuse)
        status, reply = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True}
        )
        assert status == 500 and err(reply) == "plant_recovery_failed"
        monkeypatch.undo()
        status, session = await rig.call("GET", "/api/session")
        assert status == 200 and session["recovery_failed"] is True
        assert {"code": "plant_recovery_failed", "differs": False} in session["plant_notices"]
        status, view = await rig.call("GET", "/api/inverters")
        assert status == 200, view
        [inv] = view["inverters"]
        assert inv["id"] == "deye1" and inv["status"]["state"] == "blocked"
        assert inv["status"]["reason"] == "plant_recovery_failed"
        status, one = await rig.call("GET", "/api/inverters/deye1")
        assert status == 200 and one["id"] == "deye1"


async def test_reading_survives_a_failed_recovery_after_a_rejected_addition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        real = rig.app.install

        def fail_old(cfg: Any) -> Any:
            if len(cfg.inverters) == 1:
                raise RuntimeError("review: old generation cannot restart")
            return real(cfg)

        monkeypatch.setattr(rig.app, "install", fail_old)
        monkeypatch.setattr(rig.store, "commit", refuse)
        status, reply = await rig.call(
            "POST",
            "/api/plant/inverters",
            {"id": "deye2", "name": "two", "endpoint": "gw1", "unit_id": 2},
        )
        assert err(reply) == "plant_recovery_failed", reply
        monkeypatch.undo()
        status, view = await rig.call("GET", "/api/inverters")
        assert status == 200 and [i["id"] for i in view["inverters"]] == ["deye1"], view


async def test_saved_endpoint_is_resolved_inside_the_probe_generation_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        new_server = await rig.add_server("new_target", "modbus_tcp", "2409876543")
        entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_switch = rig.app.switch

        async def held_switch(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            return await real_switch(*args, **kwargs)

        real_probe = rig.service.probe

        async def observed_probe(start: Any) -> Any:
            waiting.set()
            return await real_probe(start)

        monkeypatch.setattr(rig.app, "switch", held_switch)
        monkeypatch.setattr(rig.service, "probe", observed_probe)
        old_frames = len(rig.servers["gw1"].frames)
        change = asyncio.create_task(
            rig.call(
                "PUT", "/api/plant/endpoints/gw1", rig.endpoint_body("gw1", port=new_server.port)
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        probe_task = asyncio.create_task(
            rig.call(
                "POST",
                "/api/plant/test",
                {"endpoint_id": "gw1", "unit_id": 1, "inverter": "deye1"},
            )
        )
        await asyncio.wait_for(waiting.wait(), 3)
        release.set()
        status, reply = await asyncio.wait_for(change, 5)
        assert status == 200, reply
        status, result = await asyncio.wait_for(probe_task, 5)
        assert status == 200 and result["path"] == "live" and result["serial"] == "2409876543"
        assert len(rig.servers["gw1"].frames) >= old_frames  # no new reads of the old target
        # after a deletion the saved id is unknown: no silent test of the old address
        status, reply = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True}
        )
        assert status == 200, reply
        status, reply = await rig.call("POST", "/api/plant/endpoints/gw1/delete", {"confirm": True})
        assert status == 200, reply
        status, reply = await rig.call(
            "POST", "/api/plant/test", {"endpoint_id": "gw1", "unit_id": 1}
        )
        assert status == 404 and err(reply) == "unknown_endpoint", reply


async def test_a_pending_selection_cleanup_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.manager.change(0, {})
        monkeypatch.setattr(rig.manager.store, "commit", refuse)
        status, reply = await rig.call(
            "POST", "/api/plant/inverters/deye1/delete", {"confirm": True}
        )
        assert status == 200 and "prune_pending" in reply["notes"], reply
        _, session = await rig.call("GET", "/api/session")
        assert {"code": "selection_prune_pending", "differs": False} in session["plant_notices"]
