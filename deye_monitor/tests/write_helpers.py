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
    "r20": 0x3880,  # R20/R21: 80000 x 0.1 W = 8 kW -> 185 A cap, 8000 W rated power
    "r21": 1,
    "r62": (26 << 8) | 11,  # inverter clock 2026-11-01 12:30:15
    "r63": (1 << 8) | 12,
    "r64": (30 << 8) | 15,
    "r108": 50,
    "r109": 60,
    "r111": 1,  # battery control mode: SOC
    "r115": 25,  # shutdown < low < restart
    "r116": 50,
    "r117": 35,
    "r125": 30,
    "r127": 30,
    "r128": 20,
    "r130": 0,
    "r133": 1,  # GEN port = Smart Load
    "r134": 4500,
    "r135": 20,
    "r136": 5000,
    "r137": 60,
    "r146": 0x00FE,  # ToU off, all weekdays, no Spain mode
    "r148": 0,  # slot start times (HHMM), strictly ascending
    "r149": 500,
    "r150": 1000,
    "r151": 1500,
    "r152": 2000,
    "r153": 2300,
    "r154": 1000,  # slot discharge power (W)
    "r155": 2000,
    "r156": 3000,
    "r157": 4000,
    "r158": 5000,
    "r159": 6000,
    "r166": 30,  # slot target SOC (%)
    "r167": 40,
    "r168": 50,
    "r169": 60,
    "r170": 70,
    "r171": 80,
    "r172": 0x0002,  # slot flags: bit 1 (generator charge) set, bit 0 (allow GRID charging) clear
    "r173": 0x0002,
    "r174": 0x0002,
    "r175": 0x0002,
    "r176": 0x0002,
    "r177": 0x0002,
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
    for inverter in enable:  # a release needs a fresh identity read: running first
        await rig.wait_state(inverter, InverterState.RUNNING)
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
