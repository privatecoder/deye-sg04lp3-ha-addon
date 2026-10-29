"""Rig for the browser tests: the real web app on fake runtimes behind a tiny Ingress-like proxy.

The proxy plays the Supervisor: it serves the app below ``/api/hassio_ingress/<token>/`` and adds
the ``X-Remote-User-*`` headers. The app sees the proxy as peer ``127.0.0.1``, which is allowed
for these tests only (production accepts the Supervisor address alone)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from deye_monitor.webapi import WebApi, WebSettings
from tests.device_helpers import device_regs, ep, inverter
from tests.selection_helpers import A, B, Plant, plant

PREFIX = "/api/hassio_ingress/TESTTOKEN"
USER_HEADERS = {"X-Remote-User-Id": "u1", "X-Remote-User-Name": "tester"}
_FORWARD = ("Content-Type", "X-CSRF-Token", "If-Match", "Accept")


@dataclass
class UiStack:
    plant: Plant
    api: WebApi
    proxy_port: int
    user: bool = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.proxy_port}{PREFIX}/"

    @property
    def revision(self) -> int:
        return self.plant.manager.revision


async def _step_running(p: Plant) -> None:
    for inverter_id in p.rig.runtime.inverters:
        inv = p.rig.inv(inverter_id)
        await inv.step()
        for _ in range(2):
            p.rig.clock.offset += 11
            await inv.step()


@contextlib.asynccontextmanager
async def ui_stack(
    tmp_path: Path, *, user: bool = True, running: bool = True, **plant_kw: Any
) -> AsyncIterator[UiStack]:
    # R111 = 2 is the documented "no battery": the device suggests a plant without a battery.
    if "endpoints" not in plant_kw:
        plant_kw["endpoints"] = [
            ep("gw1", host="192.0.2.11"),
            ep("gw2", "rtu_over_tcp", host="192.0.2.12"),
        ]
        plant_kw["inverters"] = [inverter("deye1", "gw1"), inverter("deye2", "gw2")]
        plant_kw["banks"] = {
            "gw1": device_regs(A, r588=55, r111=2, r336=0),
            "gw2": device_regs(B, r588=80, r336=0),
        }
    async with plant(tmp_path, **plant_kw) as p:
        if running:
            await _step_running(p)
        api = WebApi(
            p.rig.runtime, p.manager, settings=WebSettings("127.0.0.1", 0, frozenset({"127.0.0.1"}))
        )
        app_port = await api.start()
        session = aiohttp.ClientSession(auto_decompress=False)

        async def forward(request: web.Request) -> web.Response:
            tail = request.match_info["tail"]
            headers = {k: v for k in _FORWARD if (v := request.headers.get(k)) is not None}
            if user:
                headers.update(USER_HEADERS)
            headers["X-Ingress-Path"] = PREFIX
            async with session.request(
                request.method,
                f"http://127.0.0.1:{app_port}/{tail}",
                data=await request.read(),
                headers=headers,
            ) as upstream:
                body = await upstream.read()
                out = {
                    k: v
                    for k, v in upstream.headers.items()
                    if k in ("Content-Type", "Content-Security-Policy", "Cache-Control")
                }
                return web.Response(body=body, status=upstream.status, headers=out)

        proxy = web.Application()
        proxy.router.add_route("*", PREFIX + "/{tail:.*}", forward)
        runner = web.AppRunner(proxy, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        proxy_port = int(runner.addresses[0][1])
        try:
            yield UiStack(p, api, proxy_port, user)
        finally:
            await runner.cleanup()
            await session.close()
            await api.stop()
