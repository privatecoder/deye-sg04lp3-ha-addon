"""Helpers of the write-layer tests: a plant of fake inverters, previews, confirmations, audit."""

from __future__ import annotations

import json
from typing import Any

from deye_monitor.device import InverterState
from deye_monitor.write_ops import OPERATION_KEYS
from deye_monitor.writes import AUDIT_FILE, WriteService, WriteSettings
from tests.fakes.servers import FakeServer
from tests.plant_helpers import A, B, PlantRig

REGS: dict[str, int] = {
    "r20": 0x3880,  # R20/R21: 80000 x 0.1 W = 8 kW -> 190 A cap
    "r21": 1,
    "r108": 50,
    "r109": 60,
    "r125": 30,
    "r128": 20,
    "r130": 0x0004,
    "r133": 1,  # GEN port = Smart Load
    "r134": 4500,
    "r135": 20,
    "r136": 5000,
    "r137": 60,
}
SERIALS = {"deye1": A, "deye2": B, "deye3": "2405555555"}
ALL = list(OPERATION_KEYS)
FAST = WriteSettings(observe_delay_s=0.0, pacing_s=0.0, permit_s=30.0)


def err(body: Any) -> str:
    return str(body["error"]["key"])


async def build_plant(
    rig: PlantRig,
    *,
    inverters: tuple[str, ...] = ("deye1", "deye2"),
    enable: tuple[str, ...] = ("deye1",),
    operations: list[str] | None = None,
    regs: dict[str, dict[str, int]] | None = None,
    link_timeout: float = 0.3,
) -> None:
    for n, inverter in enumerate(inverters, start=1):
        gw = f"gw{n}"
        own = {**REGS, **(regs or {}).get(inverter, {})}
        await rig.add_server(gw, "modbus_tcp", SERIALS[inverter], **own)
        status, body = await rig.call(
            "POST", "/api/plant/endpoints", rig.endpoint_body(gw, timeout_s=link_timeout)
        )
        assert status == 200, body
        status, body = await rig.call(
            "POST",
            "/api/plant/inverters",
            {"id": inverter, "name": inverter, "endpoint": gw, "unit_id": 1},
        )
        assert status == 200, body
    for inverter in enable:
        status, body = await rig.call(
            "PUT",
            f"/api/plant/inverters/{inverter}/writes",
            {"allow_writes": True, "operations": ALL if operations is None else operations},
        )
        assert status == 200, body
    await settle(rig, inverters)


async def settle(rig: PlantRig, inverters: tuple[str, ...]) -> None:
    for inverter in inverters:
        await rig.wait_state(inverter, InverterState.RUNNING)
    fast(rig)


def fast(rig: PlantRig, settings: WriteSettings = FAST) -> WriteService:
    svc = svc_of(rig)
    svc.settings = settings
    return svc


def svc_of(rig: PlantRig) -> WriteService:
    assert rig.app.runtime is not None
    return rig.app.runtime.writes


async def preview(
    rig: PlantRig, inverter: str, operation: str, value: Any, **kw: Any
) -> tuple[int, Any]:
    return await rig.call(
        "POST",
        f"/api/inverters/{inverter}/writes/preview",
        {"operation": operation, "value": value},
        **kw,
    )


async def confirm(rig: PlantRig, operation_id: str, **kw: Any) -> tuple[int, Any]:
    return await rig.call("POST", f"/api/writes/operations/{operation_id}/confirm", {}, **kw)


async def write(rig: PlantRig, inverter: str, operation: str, value: Any) -> dict[str, Any]:
    status, body = await preview(rig, inverter, operation, value)
    assert status == 200, body
    status, body = await confirm(rig, body["preview"]["operation_id"])
    assert status == 200, body
    return dict(body["operation"])


def fc16(server: FakeServer) -> list[bytes]:
    return [f for f in server.frames if len(f) == 15 and f[7] == 0x10]


def audit_lines(rig: PlantRig) -> list[dict[str, Any]]:
    path = rig.directory / AUDIT_FILE
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
