"""Failover and failback between the two paths of an inverter (SPEC §3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deye_monitor.device import InverterState, RuntimeSettings
from tests.device_helpers import device_regs, ep, inverter, rig, serial_words, state_of
from tests.fakes.servers import Behavior

SN = "2401234567"
OTHER = "9999999999"
SETTINGS = RuntimeSettings(failover_after=3, failback_probe_s=300.0)


def two_paths(**inv_kw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    endpoints = [ep("gw1"), ep("logger1", "solarman_v5_tcp")]
    inverters = [inverter("deye1", "gw1", fallback_endpoint="logger1", **inv_kw)]
    return endpoints, inverters


def banks(fallback_serial: str = SN) -> dict[str, dict[int, int]]:
    return {
        "gw1": device_regs(SN, soc=55),
        "logger1": device_regs(fallback_serial, soc=66),
    }


async def test_failover_after_n_failed_cycles_and_failback_after_probe(tmp_path: Path) -> None:
    endpoints, inverters = two_paths()
    async with rig(endpoints, inverters, banks(), tmp_path, settings=SETTINGS) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert state_of(inv) is InverterState.RUNNING and inv.active == 0
        first_keys = set(r.sink.states[-1][2])
        soc_primary = r.sink.values("deye1", "battery_soc")
        assert soc_primary and set(soc_primary) == {55}

        r.servers["gw1"].default = Behavior(silent=True)  # primary path dies
        await r.step_until("deye1", lambda i: i.active == 1 and i.state is InverterState.RUNNING)
        assert inv.route.endpoint_id == "logger1"
        assert any(
            d["event"] == "path_switched" and d["path"] == "fallback" for _, d in r.sink.diagnostics
        )

        await inv.step()
        soc_all = r.sink.values("deye1", "battery_soc")
        # nothing but the two banks and unknowns (failure snapshots), never a mixture
        assert set(soc_all) - {None} == {55, 66}
        fallback_snaps = [s for _, s, _ in r.sink.states if s.endpoint_id == "logger1"]
        assert fallback_snaps
        assert all(s.words[588] == 66 for s in fallback_snaps if 588 in s.words)
        primary_snaps = [s for _, s, _ in r.sink.states if s.endpoint_id == "gw1"]
        assert all(s.words[588] == 55 for s in primary_snaps if 588 in s.words)
        # the entities (keys) are the same on either path: they hang on the inverter id only
        keys_fallback = {k for i, s, v in r.sink.states if s.endpoint_id == "logger1" for k in v}
        keys_primary = {k for i, s, v in r.sink.states if s.endpoint_id == "gw1" for k in v}
        assert keys_fallback == keys_primary
        assert first_keys <= keys_primary

        # primary comes back: it is probed only after failback_probe_s
        r.servers["gw1"].default = Behavior()
        r.clock.offset += 60
        await inv.step()
        assert inv.active == 1
        r.clock.offset += 400
        await r.step_until("deye1", lambda i: i.active == 0 and i.state is InverterState.RUNNING)
        assert any(d["event"] == "failback" for _, d in r.sink.diagnostics)
        await inv.step()
        assert r.sink.values("deye1", "battery_soc")[-1] == 55


async def test_wrong_serial_on_the_fallback_is_never_used(tmp_path: Path) -> None:
    endpoints, inverters = two_paths()
    async with rig(
        endpoints, inverters, banks(fallback_serial=OTHER), tmp_path, settings=SETTINGS
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        assert r.runtime.book.bound("deye1") == SN
        r.servers["gw1"].default = Behavior(silent=True)
        for _ in range(8):
            await inv.step()
            r.clock.pass_cooldown()
        assert inv.routes[1].bad_until > 0 or any(
            d["event"] == "fallback_quarantined" for _, d in r.sink.diagnostics
        )
        assert not [s for _, s, _ in r.sink.states if s.endpoint_id == "logger1"]  # no foreign data
        assert state_of(inv) is not InverterState.RUNNING or inv.active == 0
        assert any(d["event"] == "fallback_quarantined" for _, d in r.sink.diagnostics)
        assert 66 not in r.sink.values("deye1", "battery_soc")


async def test_a_wrong_device_on_the_primary_after_return_is_not_adopted(tmp_path: Path) -> None:
    endpoints, inverters = two_paths()
    async with rig(endpoints, inverters, banks(), tmp_path, settings=SETTINGS) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].default = Behavior(silent=True)
        await r.step_until("deye1", lambda i: i.active == 1 and i.state is InverterState.RUNNING)
        r.servers["gw1"].default = Behavior()
        r.servers["gw1"].responder.regs.update(serial_words(OTHER))  # a different device is back
        r.clock.offset += 400
        for _ in range(3):
            await inv.step()
            r.clock.pass_cooldown()
        assert inv.active == 1  # stays on the fallback; the foreign primary is not used
        assert any(d["event"] == "primary_serial_mismatch" for _, d in r.sink.diagnostics)
        assert not [
            s for _, s, _ in r.sink.states if s.endpoint_id == "gw1" and s.words.get(3) == 0x3939
        ]


async def test_no_fallback_goes_offline_and_recovers_after_identity(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")],
        [inverter("deye1", "gw1")],
        {"gw1": device_regs(SN)},
        tmp_path,
        settings=SETTINGS,
    ) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].default = Behavior(silent=True)
        await r.step_until("deye1", lambda i: i.state is InverterState.OFFLINE)
        before = len(r.sink.states)
        r.servers["gw1"].default = Behavior()
        await r.step_until("deye1", lambda i: i.state is InverterState.RUNNING)
        assert any(s is InverterState.OFFLINE for _, s, _ in r.sink.availability)
        await inv.step()
        assert len(r.sink.states) > before


async def test_after_a_path_switch_the_loop_wakes_up_for_the_identification(
    tmp_path: Path,
) -> None:
    """E2E finding: the delay returned by the step that switched to the fallback path must be the
    identification due time (now), not the 300 s failback probe interval."""
    endpoints, inverters = two_paths()
    async with rig(endpoints, inverters, banks(), tmp_path, settings=SETTINGS) as r:
        inv = r.inv("deye1")
        await inv.step()
        r.servers["gw1"].default = Behavior(silent=True)
        delay = None
        for _ in range(12):
            d = await inv.step()
            if inv.active == 1 and inv.state is not InverterState.RUNNING:
                delay = d
                break
            r.clock.pass_cooldown()
        assert delay is not None, "the inverter never switched to the fallback path"
        assert delay <= 5.0, delay
