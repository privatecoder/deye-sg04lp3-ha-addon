"""0.5.0 owner switches: R179 (force off-grid), R178 bits 8-9 (Signal Island Mode), R341 bit 2
(MPPT multi-point scanning): encoding, sentinel and unknown words, guard admission of exactly these
fields, and the guarded write path against fake devices."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.protocol.guard import (
    DENIED_REGISTERS,
    WRITABLE_FIELDS,
    Framing,
    GuardViolation,
    WriteGrant,
    check_grant,
    guard_write_frame,
)
from deye_monitor.write_ops import BY_KEY, Refused, parse_value
from tests.plant_helpers import plant_rig
from tests.write_helpers import build_plant, err, preview, write

OFF_GRID = BY_KEY["force_off_grid"]
ISLAND = BY_KEY["signal_island_mode"]
SCAN = BY_KEY["mppt_scan"]


def grant(register: int, mask: int, old: int, new: int) -> WriteGrant:
    return WriteGrant(unit=1, register=register, mask=mask, old_word=old, new_word=new)


# -- descriptors ---------------------------------------------------------------------------------


def test_the_table_has_exactly_these_masks() -> None:
    assert (WRITABLE_FIELDS[178], WRITABLE_FIELDS[179], WRITABLE_FIELDS[341]) == (
        frozenset({0x0003, 0x000C, 0x0030, 0x00C0, 0x0300, 0x0C00, 0x3000}),
        frozenset({0x0001}),
        frozenset({0x0004, 0x0010}),
    )
    assert 341 not in DENIED_REGISTERS
    for d in (OFF_GRID, ISLAND, SCAN):
        assert d.mask in WRITABLE_FIELDS[d.register]
        assert d.risk.de and d.risk.en


def test_r179_is_a_whole_word_switch_auto_or_forced() -> None:
    assert OFF_GRID.encode(0, 1) == 1 and OFF_GRID.encode(1, 0) == 0
    for word in (2, 3, 0x8000, 0xFFFF):
        with pytest.raises(Refused) as exc:
            OFF_GRID.encode(word, 1)
        assert exc.value.key == "unexpected_bits"
    assert "Auto" in OFF_GRID.risk.en and "on-grid" not in OFF_GRID.risk.en.lower().replace(
        "off-grid", ""
    )


@pytest.mark.parametrize(
    ("old", "field", "new"),
    [
        (0x2AAA, 3, 0x2BAA),  # ON: pair 11, every other pair untouched
        (0x2BAA, 2, 0x2AAA),  # OFF: pair 10
        (0x0000, 3, 0x0300),
        (0x0000, 2, 0x0200),
        (0xFCFF & 0x1234, 3, 0x1234 | 0x0300),
    ],
)
def test_r178_is_a_two_bit_pair_with_the_other_pairs_preserved(
    old: int, field: int, new: int
) -> None:
    assert ISLAND.encode(old, field) == new
    assert (new ^ old) & ~0x0300 & 0xFFFF == 0


def test_r178_value_mapping_and_unknown_pairs() -> None:
    assert parse_value(ISLAND, 1) == 3 and parse_value(ISLAND, 0) == 2
    for bad in (2, -1, True, "on"):
        with pytest.raises(Refused):
            parse_value(ISLAND, bad)
    assert [ISLAND.display(f) for f in (0, 1, 2, 3)] == [None, None, 0, 1]
    assert [ISLAND.incumbent_valid(f) for f in (0, 1, 2, 3)] == [False, False, True, True]


def test_r178_and_r341_refuse_the_sentinel_word_in_and_out() -> None:
    for d, field in ((ISLAND, 3), (SCAN, 1)):
        with pytest.raises(Refused) as exc:
            d.encode(0xFFFF, field)  # incumbent
        assert exc.value.key == "unexpected_bits"
    # the result would be 0xFFFF
    with pytest.raises(Refused):
        ISLAND.encode(0xFEFF, 3)
    with pytest.raises(Refused):
        SCAN.encode(0xFFFB, 1)


def test_r341_changes_bit_2_only() -> None:
    assert SCAN.encode(0x0060, 1) == 0x0064 and SCAN.encode(0x0064, 0) == 0x0060
    assert SCAN.encode(0x1B60, 1) == 0x1B64
    assert SCAN.field_of(0x0064) == 1 and SCAN.field_of(0x0060) == 0


# -- guard ---------------------------------------------------------------------------------------


def test_the_guard_admits_exactly_these_fields() -> None:
    for g in (
        grant(178, 0x0300, 0x2AAA, 0x2BAA),
        grant(179, 1, 0, 1),
        grant(179, 1, 1, 0),
        grant(341, 4, 0x0060, 0x0064),
    ):
        check_grant(g)
    refused = [
        grant(178, 0xFFFF, 0x2AAA, 0x2BAA),  # a wider mask than the field
        grant(178, 0x0100, 0x2AAA, 0x2BAA),  # a narrower one is not the table's either
        grant(178, 0x0300, 0x2AAA, 0x2BAB),  # a bit outside the pair
        grant(178, 0x0300, 0xFFFF, 0xFEFF),  # sentinel in
        grant(178, 0x0300, 0xFEFF, 0xFFFF),  # sentinel out
        grant(179, 1, 2, 3),  # not a 0 / 1 word
        grant(179, 1, 1, 0xFFFF),
        grant(179, 0xFFFF, 0, 1),
        grant(341, 0x0060, 0x0000, 0x0020),  # the limiter bits stay denied
        grant(341, 0xFFFF, 0x0060, 0x0064),
        grant(341, 4, 0x0060, 0x0065),
        grant(341, 4, 0xFFFB, 0xFFFF),
        grant(341, 4, 0xFFFF, 0xFFFB),
    ]
    for g in refused:
        with pytest.raises(GuardViolation):
            check_grant(g)


def test_other_bits_of_r178_and_r341_cannot_be_granted_on_the_wire() -> None:
    from tests.test_write_protocol import rtu_write

    for register, old, new in ((178, 0x2BAA, 0x2BAB), (341, 0x0060, 0x0020), (341, 0, 0x0040)):
        g = grant(register, next(iter(WRITABLE_FIELDS[register])), old, new)
        with pytest.raises(GuardViolation):
            guard_write_frame(rtu_write(1, register, new), Framing.RTU, grant=g)


# -- the guarded write path ----------------------------------------------------------------------


async def test_the_switches_write_one_word_and_read_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r178": 0x2AAA, "r179": 0, "r341": 0x0060}}
        )
        regs = rig.servers["gw1"].responder.regs
        op = await write(rig, "deye1", "signal_island_mode", 1)
        assert op["status"] == "readback_confirmed" and regs[178] == 0x2BAA
        assert (op["old_word"], op["new_word"]) == (0x2AAA, 0x2BAA)
        op = await write(rig, "deye1", "signal_island_mode", 0)
        assert regs[178] == 0x2AAA
        op = await write(rig, "deye1", "force_off_grid", 1)
        assert regs[179] == 1 and op["status"] == "readback_confirmed"
        op = await write(rig, "deye1", "mppt_scan", 1)
        assert regs[341] == 0x0064
        status, body = await preview(rig, "deye1", "mppt_scan", 1)  # already on
        assert (status, err(body)) == (409, "no_change")


@pytest.mark.parametrize(
    ("key", "register", "word", "reason"),
    [
        ("force_off_grid", 179, 2, "unexpected_bits"),
        ("force_off_grid", 179, 0xFFFF, "unexpected_bits"),
        ("signal_island_mode", 178, 0xFFFF, "unexpected_bits"),
        ("signal_island_mode", 178, 0x2900, "existing_value_outside_range"),  # pair 01
        ("signal_island_mode", 178, 0x2800, "existing_value_outside_range"),  # pair 00
        ("mppt_scan", 341, 0xFFFF, "unexpected_bits"),
    ],
)
async def test_an_unknown_incumbent_locks_the_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    register: int,
    word: int,
    reason: str,
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        regs = {"r178": 0x2AAA, "r179": 0, "r341": 0x0060, f"r{register}": word}
        await build_plant(rig, inverters=("deye1",), regs={"deye1": regs})
        status, body = await preview(rig, "deye1", key, 1)
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == reason
        assert rig.servers["gw1"].responder.writes == []


@pytest.mark.parametrize("framing", [Framing.RTU, Framing.MBAP, Framing.SOLARMAN_V5])
@pytest.mark.parametrize(
    ("old", "new"),
    [(0x2AAA, 0x28AA), (0x2AAA, 0x29AA), (0x28AA, 0x2AAA), (0x29AA, 0x2BAA), (0x28AA, 0x29AA)],
)
def test_r178_wire_guard_admits_only_the_pairs_10_and_11(
    framing: Framing, old: int, new: int
) -> None:
    from deye_monitor.protocol import modbus as m
    from deye_monitor.protocol import solarman_v5 as v5

    g = grant(178, 0x0300, old, new)
    rtu = m.build_rtu_write(1, 178, new)
    frame = {
        Framing.RTU: rtu,
        Framing.MBAP: m.build_mbap_write(1, 1, 178, new),
        Framing.SOLARMAN_V5: v5.build_v5_write_request(2401234567, 1, rtu),
    }[framing]
    with pytest.raises(GuardViolation):
        guard_write_frame(frame, framing, grant=g, logger_serial=2401234567)
