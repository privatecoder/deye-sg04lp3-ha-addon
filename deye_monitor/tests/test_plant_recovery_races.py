"""Recovery is also a barrier for requests that were already in flight (nachprüfung 9): a selection
edit that waits for the lock and a body that is still being read when the plant change fails."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from deye_monitor.device import InverterState
from tests.plant_helpers import USER, PlantRig, plant_rig
from tests.test_plant import create_gw, create_inv, err

RENAME = {"id": "deye1", "name": "new", "endpoint": "gw1", "unit_id": 1}


def hashes(path: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir() if p.is_file()
    }


def break_old(rig: PlantRig, monkeypatch: pytest.MonkeyPatch) -> None:
    install = rig.app.install

    def fail_old(cfg: Any) -> Any:
        if cfg.inverters and cfg.inverters[0].name == "deye1":
            raise RuntimeError("review: accepted generation cannot rebuild")
        return install(cfg)

    def refuse(*args: Any) -> None:
        raise OSError("review: plant commit fails")

    monkeypatch.setattr(rig.app, "install", fail_old)
    monkeypatch.setattr(rig.store, "commit", refuse)


async def _cancel(*tasks: asyncio.Task[Any] | None) -> None:
    live = [t for t in tasks if t is not None]
    for t in live:
        if not t.done():
            t.cancel()
    await asyncio.gather(*live, return_exceptions=True)


async def _queued_selection(
    rig: PlantRig, monkeypatch: pytest.MonkeyPatch
) -> tuple[tuple[int, Any], tuple[int, Any], int]:
    """A plant change holds the locks; a selection edit passes the HTTP gate and waits at the
    selection lock; then the plant change ends. Returns both answers and the revision before."""
    entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    switch, change = rig.app.switch, rig.manager.change

    async def held_switch(*a: Any, **kw: Any) -> Any:
        entered.set()
        await release.wait()
        return await switch(*a, **kw)

    async def observed_change(*a: Any, **kw: Any) -> Any:
        waiting.set()
        return await change(*a, **kw)

    monkeypatch.setattr(rig.app, "switch", held_switch)
    monkeypatch.setattr(rig.manager, "change", observed_change)
    revision = rig.manager.revision
    plant = asyncio.create_task(rig.call("PUT", "/api/plant/inverters/deye1", RENAME))
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        queued = asyncio.create_task(
            rig.call(
                "POST", "/api/inverters/deye1/sensors", {"add": ["battery_soc"]}, revision=revision
            )
        )
        await asyncio.wait_for(waiting.wait(), 3)
        assert rig.manager.lock.locked() and not rig.service.recovery_failed
        release.set()
        return await asyncio.wait_for(plant, 5), await asyncio.wait_for(queued, 5), revision
    finally:
        release.set()
        await _cancel(plant, queued)


async def test_selection_waiting_for_a_failed_plant_change_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        break_old(rig, monkeypatch)
        before = hashes(tmp_path)
        (p_status, p_body), (status, body), revision = await _queued_selection(rig, monkeypatch)
        assert p_status == 500 and err(p_body) == "plant_recovery_failed"
        assert status == 409 and err(body) == "plant_recovery_failed", body
        assert rig.service.recovery_failed and not rig.app.running
        assert rig.manager.revision == revision and hashes(tmp_path) == before
        assert rig.manager.selection("deye1").chosen is False


async def test_body_already_being_read_is_rechecked_after_recovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)
        break_old(rig, monkeypatch)
        _, session = await rig.call("GET", "/api/session")
        counter = rig.api.action_counter
        runtime = rig.app.runtime
        assert runtime is not None
        binding = runtime.book.bound("deye1")
        reading, send_rest = asyncio.Event(), asyncio.Event()
        request_json = web.Request.json

        async def observed_json(self: web.Request, *a: Any, **kw: Any) -> Any:
            if self.path == "/api/stores/reset":
                reading.set()  # the gate was passed, now the HTTP body is read
            return await request_json(self, *a, **kw)

        monkeypatch.setattr(web.Request, "json", observed_json)
        raw = json.dumps(
            {"kind": "identity", "scope": "inverter", "inverter": "deye1", "confirm": True}
        ).encode()

        async def chunks() -> AsyncIterator[bytes]:
            yield raw[:1]
            await send_rest.wait()
            yield raw[1:]

        headers = {
            **USER,
            "X-CSRF-Token": session["csrf"],
            "If-Match": f'"{counter}"',
            "Content-Type": "application/json",
        }

        async def send() -> tuple[int, Any]:
            async with rig.http.post(
                rig.base + "/api/stores/reset", data=chunks(), headers=headers
            ) as response:
                return response.status, await response.json()

        pending = asyncio.create_task(send())
        try:
            await asyncio.wait_for(reading.wait(), 3)
            assert not rig.service.recovery_failed
            status, body = await rig.call("PUT", "/api/plant/inverters/deye1", RENAME)
            assert status == 500 and err(body) == "plant_recovery_failed"
            stopped = (rig.api.action_counter, runtime.book.bound("deye1"))
            send_rest.set()
            status, body = await asyncio.wait_for(pending, 5)
            assert status == 409 and err(body) == "plant_recovery_failed", body
            assert (rig.api.action_counter, runtime.book.bound("deye1")) == stopped
            assert stopped[0] == counter and stopped[1] is not None and binding is not None
        finally:
            send_rest.set()
            await _cancel(pending)


async def test_selection_waiting_for_a_successful_rollback_remains_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a recovered accepted runtime accepts the queued selection."""
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await create_gw(rig)
        await create_inv(rig)
        await rig.wait_state("deye1", InverterState.RUNNING)

        def refuse(*args: Any) -> None:
            raise OSError("review: plant commit fails, rollback succeeds")

        monkeypatch.setattr(rig.store, "commit", refuse)
        (p_status, p_body), (status, body), _ = await _queued_selection(rig, monkeypatch)
        assert p_status == 500 and err(p_body) == "persist_failed"
        assert status == 200, body
        assert rig.app.running and not rig.service.recovery_failed
