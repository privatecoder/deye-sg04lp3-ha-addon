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
    "r98": 0,  # battery type word: the lead / internal curve is active
    "r99": 5500,  # equalization 55.00 V >= absorption >= float
    "r100": 5400,
    "r101": 5300,
    "r102": 200,  # capacity (Ah)
    "r103": 4500,
    "r105": 30,
    "r106": 6,
    "r107": 5,
    "r108": 100,
    "r109": 60,
    "r111": 1,  # battery control mode: SOC
    "r112": 0,
    "r113": 20,
    "r114": 983,
    "r118": 4500,  # voltage shutdown < low < restart
    "r119": 5200,
    "r120": 4800,
    "r115": 25,  # shutdown < low < restart
    "r116": 50,
    "r117": 35,
    "r121": 60,  # generator maximum run time 6.0 h
    "r122": 10,
    "r123": 4800,
    "r124": 40,
    "r125": 30,
    "r129": 0,
    "r131": 5100,
    "r132": 0,
    "r183": 0,  # 50 Hz
    "r126": 4800,
    "r127": 30,
    "r128": 20,
    "r130": 0,
    "r133": 1,  # GEN port = Smart Load
    "r189": 0,  # generator input selector: GEN port
    "r553": 0,  # warning word 1: none active (bit 3 = W4 meter communication)
    "r134": 4500,
    "r135": 20,
    "r136": 5000,
    "r137": 60,
    "r237": 0,  # active unbalanced load off
    "r259": 0x32DD,  # EV: off-grid SOC 50 %, GRID port, free work off (signature DD)
    "r260": 3000,  # EV maximum power (W)
    "r35": 0,  # Low_Noise: 15 kHz
    "r180": 600,  # grid reconnection time (s)
    "r181": 1,  # AFCI configured enabled
    "r347": 2000,  # external CT ratio
    "r104": 100,  # zero-export minimum limit (raw)
    "r142": 0,  # export mode: power sales enabled
    "r143": 3000,
    "r145": 0,
    "r190": 5000,
    "r191": 5000,
    "r340": 5000,
    "r140": 0x0000,  # external signal off
    "r141": 0,  # energy pattern: Batt First
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
    **{f"r{160 + i}": 5000 for i in range(6)},  # slot target voltages (50.00 V)
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
    for (
        inverter
    ) in inverters:  # the installation limits and the EV charger the contracts intersect with
        inv = rig.app.runtime.inverters[inverter] if rig.app.runtime else None
        if inv is not None:
            inv.features = inv.features.model_copy(update=INSTALLATION)


INSTALLATION: dict[str, Any] = {
    "export_limit_w": 12000,
    "grid_limit_w": 16000,
    "generator_capacity_w": 16000,
    "ev_charger_limit_w": 22000,
    "ev_offgrid_reserve_soc": 0,
    "ev_charger": "grid_port",
}


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
