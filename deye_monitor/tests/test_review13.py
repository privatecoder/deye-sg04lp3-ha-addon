"""Review 13 (0.2.6, R13-01): an unknown role (R336 missing) never changes the slave verdict."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.device_helpers import device_regs, ep, inverter, rig
from tests.test_live9 import BMS_KEYS, MASTER, SLAVE, SN, ZEROS

SLAVE_KEYS = BMS_KEYS | {"battery_temperature"}


def _run(tmp_path: Path, **regs: int) -> Any:
    banks = {"gw1": device_regs(SN, r15=0x1175, **regs)}
    return rig([ep("gw1")], [inverter("deye1", "gw1")], banks, tmp_path)


async def _next_identity_pass(r: Any, inv: Any, *, fail: frozenset[int] = frozenset()) -> None:
    r.servers["gw1"].responder.fail_addresses.clear()
    r.servers["gw1"].responder.fail_addresses.update(fail)
    r.clock.offset += inv.cfg.poll_identity_s + 1
    r.sink.states.clear()
    await inv.step()


@pytest.mark.parametrize("failed_register", [336, 210])
async def test_failed_role_or_bms_read_preserves_the_established_slave_verdict(
    tmp_path: Path, failed_register: int
) -> None:
    async with _run(tmp_path, r336=SLAVE, r586=180, **ZEROS) as r:
        inv = r.inv("deye1")
        await inv.step()
        before, selection = dict(inv.inactive), inv.selection
        assert set(before) == SLAVE_KEYS
        await _next_identity_pass(r, inv, fail=frozenset({failed_register}))
        assert inv.inactive == before
        assert inv.selection == selection
        assert not inv.current_plan().addresses & {586, *range(210, 220)}  # still not polled
        r.clock.offset += 11
        await inv.step()
        emitted = {k for _, _, v in r.sink.states for k in v}
        assert not emitted & SLAVE_KEYS  # no value of an inactive entity is emitted


async def test_unknown_role_does_not_restore_the_discovery(tmp_path: Path) -> None:
    async with _run(tmp_path, r336=SLAVE, r586=180, **ZEROS) as r:
        inv = r.inv("deye1")
        await inv.step()
        events = len(r.sink.diagnostics)
        await _next_identity_pass(r, inv, fail=frozenset({336}))
        # no "inactive_updated" event after the pass: the publisher is told to restore nothing
        assert not any(d.get("event") == "inactive_updated" for _, d in r.sink.diagnostics[events:])
        assert all(set(d["inactive"]) == SLAVE_KEYS for _, d in r.sink.diagnostics[events:])
        assert set(inv.diagnostics()["inactive"]) == SLAVE_KEYS


async def test_no_slave_is_inferred_on_first_start_with_an_unknown_role(tmp_path: Path) -> None:
    async with _run(tmp_path, r586=180, **ZEROS) as r:
        r.servers["gw1"].responder.fail_addresses.add(336)
        inv = r.inv("deye1")
        await inv.step()
        assert inv.inactive == {}


async def test_a_valid_master_response_restores_after_an_unknown_one(tmp_path: Path) -> None:
    async with _run(tmp_path, r336=SLAVE, r586=180, **ZEROS) as r:
        inv = r.inv("deye1")
        await inv.step()
        await _next_identity_pass(r, inv, fail=frozenset({336}))
        assert set(inv.inactive) == SLAVE_KEYS
        r.servers["gw1"].responder.regs[336] = MASTER
        await _next_identity_pass(r, inv)
        assert inv.inactive == {}
        assert inv.current_plan().addresses >= {586}


async def test_a_valid_slave_response_rechecks_the_bms_block(tmp_path: Path) -> None:
    async with _run(tmp_path, r336=SLAVE, r586=180, **ZEROS) as r:
        inv = r.inv("deye1")
        await inv.step()
        await _next_identity_pass(r, inv, fail=frozenset({336}))
        r.servers["gw1"].responder.regs[214] = 77  # the BMS link works now
        await _next_identity_pass(r, inv)
        assert set(inv.inactive) == {"battery_temperature"}


async def test_the_bms_check_reads_in_chunks_when_the_read_limit_is_small(tmp_path: Path) -> None:
    async with _run(tmp_path, r336=SLAVE, r586=180, **ZEROS) as r:
        inv = r.inv("deye1")
        inv.route.max_read_registers = 4  # smaller than the ten-word BMS block
        await inv.step()
        assert set(inv.inactive) == SLAVE_KEYS
        r.servers["gw1"].responder.regs[219] = 5  # a non-zero word in the last chunk
        await _next_identity_pass(r, inv)
        assert set(inv.inactive) == {"battery_temperature"}
