"""Cycle evaluation (T7-04/05), budget inputs (T7-06) and the R111 hint integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.device import InverterState, RuntimeSettings
from deye_monitor.transport import RequestTimeout
from tests.device_helpers import Rig, device_regs, ep, install_gate, inverter, rig, state_of
from tests.fakes.servers import Behavior

A = "2401234567"
BATTERY_ADDRS = {586, 587, 588, 591}  # what the default selection needs of the battery group


async def ready(r: Rig) -> None:
    await r.inv("deye1").step()
    assert state_of(r.inv("deye1")) is InverterState.RUNNING
    r.clock.offset += 11


def only_battery_is_due(r: Rig) -> None:
    """Make the battery group the only due group (the others would heal the counter in a real
    plant only if they succeeded; here the point is the battery cycle itself)."""
    inv = r.inv("deye1")
    for group in inv._groups():
        inv._due[group] = 0.0 if group == "battery" else 1e12


def battery_snapshots(r: Rig, after: int = 0) -> list[tuple[object, dict[str, object]]]:
    return [(s, dict(v)) for _, s, v in r.sink.states[after:] if s.group == "battery"]


# -- T7-04 ----------------------------------------------------------------------------------------


async def test_all_exception_group_publishes_unknowns(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        await ready(r)
        assert r.sink.values("deye1", "battery_soc")[-1] == 55
        r.servers["gw1"].responder.fail_addresses = set(BATTERY_ADDRS)  # every block is refused
        before = len(r.sink.states)
        await r.inv("deye1").step()
        snaps = battery_snapshots(r, before)
        assert len(snaps) == 1
        snapshot, values = snaps[0]
        assert snapshot.failed is True and snapshot.words == {}  # type: ignore[attr-defined]
        assert set(values) >= {"battery_soc", "battery_voltage", "battery_current"}
        assert set(values.values()) == {None}  # no zero, no carry-forward
        assert snapshot.missing >= BATTERY_ADDRS  # type: ignore[attr-defined]
        assert r.inv("deye1").fails == 0  # address-related refusals are no transport failure
        # the other groups of the same step were fine and published normally
        assert any(s.group == "status" and not s.failed for _, s, _ in r.sink.states[before:])


async def test_timeout_after_a_valid_value_publishes_unknowns(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        await ready(r)
        r.servers["gw1"].default = Behavior(silent=True)
        before = len(r.sink.states)
        await r.inv("deye1").step()
        snaps = battery_snapshots(r, before)
        assert snaps and all(s.failed and set(v.values()) == {None} for s, v in snaps)  # type: ignore[attr-defined]
        assert r.inv("deye1").fails >= 1


async def test_failure_snapshot_covers_only_the_selected_keys(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        inv.set_selection({"battery_soc"}, revision=1)
        await ready(r)
        await inv.step()
        r.servers["gw1"].responder.fail_addresses = set(BATTERY_ADDRS)
        r.clock.offset += 11
        before = len(r.sink.states)
        await inv.step()
        snaps = battery_snapshots(r, before)
        assert snaps and snaps[-1][1] == {"battery_soc": None}
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A)}, tmp_path / "e"
    ) as r2:
        r2.inv("deye1").set_selection(set(), revision=1)
        await ready(r2)
        r2.servers["gw1"].responder.fail_addresses = set(BATTERY_ADDRS)
        await r2.inv("deye1").step()
        assert r2.sink.states == []  # an empty selection never creates entities, failure or not


async def test_quarantine_and_epoch_changes_publish_no_failure_snapshots(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await ready(r)
        r.servers["gw1"].responder.regs.update(device_regs("9999999999"))
        r.clock.offset += 4000
        await inv.step()
        assert state_of(inv) is InverterState.QUARANTINED
        before = len(r.sink.states)
        for _ in range(3):
            r.clock.offset += 100
            await inv.step()
        assert len(r.sink.states) == before  # old operating data stays blocked, no None either


# -- T7-05 ----------------------------------------------------------------------------------------


async def test_transport_error_in_a_partial_cycle_is_counted_and_keeps_the_partial_values(
    tmp_path: Path,
) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await ready(r)
        proxy = install_gate(r, "deye1")
        proxy.fail_before[591] = RequestTimeout("late block")  # first block fine, second fails
        only_battery_is_due(r)
        before = len(r.sink.states)
        await inv.step()
        assert inv.fails == 1  # the good first block does not heal the transport failure
        snap, values = battery_snapshots(r, before)[-1]
        assert values["battery_soc"] == 55  # the partial values stay ...
        assert values["battery_current"] is None  # ... the missing ones are unknown
        assert snap.failed is False and 591 in snap.missing  # type: ignore[attr-defined]
        assert state_of(inv) is InverterState.RUNNING


async def test_repeated_abort_at_the_last_block_triggers_the_failover_on_time(
    tmp_path: Path,
) -> None:
    endpoints = [ep("gw1"), ep("logger1", "solarman_v5_tcp")]
    inverters = [inverter("deye1", "gw1", fallback_endpoint="logger1")]
    banks = {"gw1": device_regs(A, r588=55), "logger1": device_regs(A, r588=66)}
    settings = RuntimeSettings(failover_after=3)
    async with rig(endpoints, inverters, banks, tmp_path, settings=settings) as r:
        inv = r.inv("deye1")
        await ready(r)
        proxy = install_gate(r, "deye1")
        proxy.fail_before[591] = RequestTimeout("always the last block")
        counts: list[int] = []
        released: list[int] = []
        for _ in range(3):
            only_battery_is_due(r)
            before = len(battery_snapshots(r))
            await inv.step()
            counts.append(inv.fails)
            released.append(len(battery_snapshots(r)) - before)
            r.clock.offset += 11
        assert counts[:2] == [1, 2]  # counted every time, not healed by the first block
        assert inv.active == 1  # the third consecutive failure switched the path ...
        assert released[:2] == [1, 1] and released[2] == 0  # ... and its partial cycle was dropped


async def test_address_related_exceptions_are_no_failures(tmp_path: Path) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r588=55)}, tmp_path
    ) as r:
        inv = r.inv("deye1")
        await ready(r)
        r.servers["gw1"].responder.fail_addresses = {591}
        for _ in range(5):
            await inv.step()
            r.clock.offset += 11
        assert inv.fails == 0 and inv.active == 0 and state_of(inv) is InverterState.RUNNING
        _, values = battery_snapshots(r)[-1]
        assert values["battery_soc"] == 55 and values["battery_current"] is None


# -- T7-06 ----------------------------------------------------------------------------------------


async def test_plan_budget_includes_the_configured_bus_gap(tmp_path: Path) -> None:
    endpoints = [ep("gw1", request_gap_ms=3000)]
    async with rig(endpoints, [inverter("deye1", "gw1")], {"gw1": device_regs(A)}, tmp_path) as r:
        inv = r.inv("deye1")
        plan = inv.current_plan()
        status = plan.budget.per_group["status"]
        assert status.cycle_s == pytest.approx(status.blocks * (0.2 + 3.0))  # timeout caps 0.25 s
        assert plan.budget.overloaded and plan.budget.utilisation > 1
        assert r.runtime.bus_utilisation()[r.cfg.endpoint("gw1").bus] > 1
        assert inv.routes[0].bus_gap_s == 3.0 and inv.routes[0].response_time_s == 0.2


async def test_shared_bus_uses_the_largest_gap_for_every_route_after_a_switch(
    tmp_path: Path,
) -> None:
    endpoints = [
        ep("gw1", bus_id="shared", request_gap_ms=50),
        ep("logger1", "solarman_v5_tcp", bus_id="shared", request_gap_ms=3000, host="127.0.0.2"),
    ]
    inverters = [inverter("deye1", "gw1", fallback_endpoint="logger1", fallback_unit_id=2)]
    banks = {"gw1": device_regs(A), "logger1": device_regs(A)}
    async with rig(endpoints, inverters, banks, tmp_path) as r:
        inv = r.inv("deye1")
        assert len(r.runtime.schedulers) == 1 and r.runtime.schedulers["shared"].gap_s == 3.0
        assert [x.bus_gap_s for x in inv.routes] == [3.0, 3.0]
        primary = inv.current_plan().budget.per_group["status"].cycle_s
        inv._switch(1, 0.0, "test")
        assert inv.current_plan().budget.per_group["status"].cycle_s == pytest.approx(primary)
        assert inv.route.name == "fallback"
        assert primary == pytest.approx(inv.current_plan().budget.per_group["status"].blocks * 3.2)


# -- R111 integration (T5-04) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("r111", "battery"), [(0, True), (1, True), (2, False), (3, True), (0xFFFF, True)]
)
async def test_r111_is_read_as_a_protocol_value(tmp_path: Path, r111: int, battery: bool) -> None:
    async with rig(
        [ep("gw1")], [inverter("deye1", "gw1")], {"gw1": device_regs(A, r111=r111)}, tmp_path
    ) as r:
        await r.inv("deye1").step()
        hint = r.inv("deye1").diagnostics()["suggested_features"]
        assert hint["battery"] is battery  # only the numbered 2 means "no battery"
        assert str(r111) in hint["_basis"]["battery"]
        assert "battery_operating_basis" not in {k for _, _, v in r.sink.states for k in v}


# -- VF-02 / VF-03 ---------------------------------------------------------------------------------


def test_finite_scale_overflow_decodes_to_unknown_never_zero() -> None:
    from deye_monitor.profiles.decode import decode
    from deye_monitor.profiles.schema import Entry, load_profile

    raw = load_profile("sg04lp3_lv_v105_8").entry("battery_voltage").model_dump()
    raw["scale"] = 1e308
    assert decode(Entry.model_validate(raw), {587: 5120}) is None


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5])
def test_small_max_read_registers_are_rejected(limit: int) -> None:
    from deye_monitor.config import ConfigError, parse_config
    from deye_monitor.transport.base import TransportSettings

    data = {
        "endpoints": [ep("gw1", max_read_registers=limit)],
        "inverters": [inverter("deye1", "gw1")],
    }
    with pytest.raises(ConfigError, match="greater than or equal to 6"):
        parse_config(data)
    with pytest.raises(ValueError, match=r"6\.\.125"):
        TransportSettings(max_read_registers=limit)


def test_limit_six_is_accepted_and_the_default_check_needs_five_words() -> None:
    from deye_monitor.config import parse_config
    from deye_monitor.transport.base import TransportSettings, default_identity_check

    data = {"endpoints": [ep("gw1", max_read_registers=6)], "inverters": [inverter("deye1", "gw1")]}
    assert parse_config(data).endpoints[0].max_read_registers == 6
    assert TransportSettings(max_read_registers=6).max_read_registers == 6
    word = (ord("2") << 8) | ord("4")
    assert default_identity_check([word] * 5)
    for n in (0, 1, 4, 6):
        assert not default_identity_check([word] * n)
    assert not default_identity_check([word] * 4 + [0x0001])
