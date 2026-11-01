"""Review 23: the periodic settings snapshot is part of the bus-load accounting (R23-08) and
records drift without reasserting anything (R23-09)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.app import build_publisher
from deye_monitor.mqtt import PublisherSettings
from deye_monitor.write_ops import OPERATION_KEYS
from deye_monitor.writes import settings_poll_budget, settings_read_requests
from tests.device_helpers import device_regs, ep, inverter, rig
from tests.fakes.mqtt import FakeBroker
from tests.plant_helpers import plant_rig
from tests.write_helpers import audit_lines, build_plant, fast, svc_of, write

ALL = tuple(OPERATION_KEYS)
FAST_PUBLISHER = PublisherSettings(reconnect_min_s=0.01, reconnect_max_s=0.04)


def test_the_request_count_follows_the_released_set_and_the_block_limit() -> None:
    assert settings_read_requests(["battery_charge_current"], 125) == 4
    assert settings_read_requests(ALL, 125) == 13  # identity block, selectors, 11 other runs
    assert settings_read_requests(ALL, 10) > settings_read_requests(ALL, 125)
    assert settings_read_requests(ALL, 1) == 61
    assert settings_read_requests([], 125) == 1  # the identity block is always read
    assert settings_read_requests(ALL, 125) > settings_read_requests(["tou_enable"], 125)


async def test_the_budget_matches_the_requests_a_snapshot_really_makes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig_:
        await build_plant(rig_, inverters=("deye1",))
        svc = svc_of(rig_)
        inv = svc.runtime.inverters["deye1"]
        budget = settings_poll_budget(inv)
        assert budget is not None
        server = rig_.servers["gw1"]
        before = len(server.frames)
        snap = await svc.snapshot("deye1")
        assert snap.status == "ok"
        assert len(server.frames) - before == budget["requests"]
        assert budget["max_block"] == inv.routes[0].max_read_registers
        assert budget["cadence_s"] == float(inv.cfg.poll.settings_s)
        assert budget["utilisation"] == pytest.approx(
            budget["requests"]
            * (inv.routes[0].response_time_s + inv.routes[0].bus_gap_s)
            / budget["cadence_s"]
        )


async def test_the_bus_load_includes_the_snapshot_only_while_the_entities_poll_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig_:
        await build_plant(rig_, inverters=("deye1",))
        runtime = rig_.app.runtime
        assert runtime is not None
        bus = runtime._bus_of("deye1")
        plain = runtime.bus_utilisation()[bus]
        assert runtime.diagnostics()["settings_snapshots"] == {}
        runtime.settings_polling = True
        budget = settings_poll_budget(runtime.inverters["deye1"])
        assert budget is not None
        assert runtime.bus_utilisation()[bus] == pytest.approx(plain + budget["utilisation"])
        assert runtime.diagnostics()["settings_snapshots"] == {"deye1": budget}


async def test_different_released_sets_report_a_different_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads = []
    for n, operations in enumerate((["tou_enable"], list(ALL))):
        async with plant_rig(tmp_path / str(n), monkeypatch) as rig_:
            await build_plant(rig_, inverters=("deye1",), operations=operations)
            runtime = rig_.app.runtime
            assert runtime is not None
            runtime.settings_polling = True
            loads.append(runtime.bus_utilisation()[runtime._bus_of("deye1")])
    assert loads[1] > loads[0]


async def test_a_publisher_marks_the_runtime_as_polling_the_settings(tmp_path: Path) -> None:
    banks = {"gw1": device_regs("2401234567", soc=55)}
    async with rig([ep("gw1")], [inverter("deye1", "gw1")], banks, tmp_path) as r:
        assert r.runtime.settings_polling is False
        build_publisher(
            r.cfg,
            r.runtime,
            r.cfg.mqtt.model_copy(update={"host": "127.0.0.1"}),
            "ab12cd34",
            factory=FakeBroker(),
            directory=tmp_path / "pub",
            settings=FAST_PUBLISHER,
        )
        assert r.runtime.settings_polling is True


async def test_a_periodic_snapshot_records_drift_once_and_never_reasserts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig_:
        await build_plant(rig_, inverters=("deye1",))
        svc = svc_of(rig_)
        fast(rig_)
        op = await write(rig_, "deye1", "battery_charge_current", 40)
        assert op["status"] == "readback_confirmed"
        writes = len(rig_.servers["gw1"].responder.writes)
        await svc.snapshot("deye1")
        current = svc.operation(op["operation_id"])
        assert current is not None and current["drift"] is None  # unchanged: nothing to record
        rig_.servers["gw1"].responder.regs[108] = 77  # changed behind our back (LCD, cloud)
        await svc.snapshot("deye1")
        await svc.snapshot("deye1")
        after = svc.operation(op["operation_id"])
        assert after is not None
        drift = after["drift"]
        assert drift["word"] == 77
        assert [x["event"] for x in audit_lines(rig_)].count("drift") == 1  # recorded once
        assert len(rig_.servers["gw1"].responder.writes) == writes  # never reasserted
        assert rig_.servers["gw1"].responder.regs[108] == 77
