"""Review 20 fixes: peer observations bound to the saved serial, the late look due 30 s after the
send. Production WriteService with in-memory register transports (no sockets)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

from deye_monitor.device import InverterState
from deye_monitor.identity import IdentityBook, identity_store
from deye_monitor.profiles.schema import load_profile
from deye_monitor.protocol.modbus import WriteAck
from deye_monitor.scheduler import BusScheduler
from deye_monitor.writes import WriteService, WriteSettings


def _words(serial: str) -> dict[int, int]:
    return {3 + n: int.from_bytes(serial[2 * n : 2 * n + 2].encode(), "big") for n in range(5)}


class Memory:
    connection_epoch = 0
    cooldown_remaining_s = 0

    def __init__(self, serial: str, role: int, on_read: Callable[[], None] | None = None) -> None:
        self.regs = {
            **_words(serial),
            11: 1,
            14: 2005,
            15: 0x1175,
            17: 0x1001,
            18: 0x70,
            20: 0x3880,
            21: 1,
            128: 20,
            336: role,
        }
        self.grants: list[Any] = []
        self.reads = 0
        self.on_read = on_read

    async def read_holding(self, unit: int, start: int, count: int) -> list[int]:
        self.reads += 1
        if self.on_read:
            self.on_read()
        return [self.regs.get(r, 0) for r in range(start, start + count)]

    async def write_register(self, grant: Any) -> WriteAck:
        self.grants.append(grant)
        self.regs[grant.register] = grant.new_word
        return WriteAck(grant.unit, grant.register, 1)


def _unit(unit_id: str, serial: str, role: int, on_read: Callable[[], None] | None = None) -> Any:
    transport = Memory(serial, role, on_read)
    inv = NS(
        id=unit_id,
        hardware=NS(name="sg04lp3_eu"),
        state=InverterState.RUNNING,
        needs_identity=False,
        active=0,
        lifecycle_epoch=0,
        seen_serial=serial,
        book=NS(bound=lambda key: serial),
        features=NS(battery=True),
        profile=load_profile("sg04lp3_lv_v105_8"),
        identity_info={},
        cfg=NS(
            name=unit_id,
            allow_writes=True,
            enabled_write_operations=["grid_charge_current"],
            write_release_firmware=None,
        ),
        routes=[
            NS(
                transport=transport,
                scheduler=BusScheduler(gap_s=0),
                unit=1,
                endpoint_id=unit_id,
                verified_epoch=0,
                max_read_registers=125,
            )
        ],
    )
    inv.cfg.write_release_firmware = WriteService._fingerprint(inv, transport.regs)  # type: ignore[arg-type]
    return inv


def _service(path: Path, units: dict[str, Any], **extra: Any) -> WriteService:
    settings = extra.pop(
        "settings", WriteSettings(observe_delay_s=0, late_observe_delay_s=0, pacing_s=0)
    )
    svc = WriteService(
        NS(inverters=units),  # type: ignore[arg-type]
        directory=path,
        settings=settings,
        **extra,
    )
    svc.plant_revision = lambda: 1
    return svc


async def _write(svc: WriteService) -> dict[str, Any]:
    p = await svc.preview("master", "grid_charge_current", 50, user_id="u", plant_revision=1)
    return await svc.confirm(p["operation_id"], user_id="u", plant_revision=1)


async def test_a_replaced_peer_is_not_reported_under_the_old_identity(tmp_path: Path) -> None:
    units = {"master": _unit("master", "2401234567", 3), "peer": _unit("peer", "2409876543", 1)}
    svc = _service(tmp_path / "a", units)
    op = await _write(svc)
    await asyncio.gather(*list(svc._late))
    await svc.close()
    replacement = _unit("peer", "2411111111", 1)
    replacement.routes[0].transport.regs[128] = 50
    restarted = _service(tmp_path / "a", {"master": units["master"], "peer": replacement})
    view = await restarted.observe_again(op["operation_id"])
    read = view["peer_rereads"][-1]["peers"][0]
    assert read["state"] == "unavailable" and read["reason"] == "identity_changed"
    assert read["word"] is None and read["value"] is None
    assert (read["expected_serial"], read["observed_serial"]) == ("2409876543", "2411111111")
    assert replacement.routes[0].transport.reads == 0 and not replacement.routes[0].transport.grants
    await restarted.close()


async def test_the_late_look_is_due_30_s_after_the_send_not_after_the_first_look(
    tmp_path: Path,
) -> None:
    now = [0.0]
    gate = asyncio.Event()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if seconds == 1:
            now[0] += 1
        else:
            await gate.wait()

    def slow_read() -> None:
        now[0] += 3  # initial reads, cooldown and the peer work take time

    units = {
        "master": _unit("master", "2401234567", 3, slow_read),
        "peer": _unit("peer", "2409876543", 1),
    }
    svc = _service(
        tmp_path / "t",
        units,
        wall=lambda: now[0],
        clock=lambda: now[0],
        sleep=sleep,
        settings=WriteSettings(observe_delay_s=1, late_observe_delay_s=30, pacing_s=0),
    )
    op = await _write(svc)
    sent_at = svc._last_send
    assert sent_at is not None
    late = op["late_observation"]
    assert late["due_at"] == sent_at + 30 and waits[-1] == sent_at + 30 - now[0]
    now[0] = sent_at + 42  # the look is overdue by the time the gate opens
    gate.set()
    await asyncio.gather(*list(svc._late))
    done = (svc.operation(op["operation_id"]) or {})["late_observation"]
    assert done["status"] == "done" and done["observed_after_s"] == 42.0
    await svc.close()


async def test_an_overdue_late_look_is_taken_at_once(tmp_path: Path) -> None:
    now = [0.0]
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    def slow_read() -> None:
        if units["master"].routes[0].transport.grants:  # only the reads after the send are slow
            now[0] += 20

    units = {
        "master": _unit("master", "2401234567", 3, slow_read),
        "peer": _unit("peer", "2409876543", 1),
    }
    svc = _service(
        tmp_path / "o",
        units,
        wall=lambda: now[0],
        clock=lambda: now[0],
        sleep=sleep,
        settings=WriteSettings(observe_delay_s=1, late_observe_delay_s=30, pacing_s=0),
    )
    op = await _write(svc)
    await asyncio.gather(*list(svc._late))
    assert waits[-1] == 0.0
    assert (svc.operation(op["operation_id"]) or {})["late_observation"]["status"] == "done"
    await svc.close()


async def test_the_first_peer_observation_is_bound_to_the_preview_identity(tmp_path: Path) -> None:
    peer = _unit("peer", "2409876543", 1)
    peer.book = IdentityBook(identity_store(tmp_path / "identity.json"), {})
    peer.book.bind_first_seen("peer", "2409876543")
    units = {"master": _unit("master", "2401234567", 3), "peer": peer}
    svc = _service(tmp_path / "r", units)
    p = await svc.preview("master", "grid_charge_current", 50, user_id="u", plant_revision=1)
    assert peer.book.rebind("peer")  # another device behind the same id before the confirm
    peer.book.bind_first_seen("peer", "2411111111")
    peer.routes[0].transport.regs[128] = 50
    reads_before = peer.routes[0].transport.reads
    op = await svc.confirm(p["operation_id"], user_id="u", plant_revision=1)
    seen = op["peers"][0]
    assert seen["state"] == "unavailable" and seen["reason"] == "identity_changed"
    assert (seen["expected_serial"], seen["observed_serial"]) == ("2409876543", "2411111111")
    assert seen["later_word"] is None and seen["changed"] is None
    assert peer.routes[0].transport.reads == reads_before and not peer.routes[0].transport.grants
    await asyncio.gather(*list(svc._late))
    await svc.close()
