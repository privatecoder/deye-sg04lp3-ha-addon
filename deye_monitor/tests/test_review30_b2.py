"""Review 30 (R30-03) and Addendum 4: asymmetric select guard domains on every framing, and the
per-descriptor role rule derived from parallel synchronization."""

from __future__ import annotations

import pytest

from deye_monitor.protocol import modbus as m
from deye_monitor.protocol import solarman_v5 as v5
from deye_monitor.protocol.guard import (
    Framing,
    GuardViolation,
    WriteGrant,
    guard_write_frame,
)
from deye_monitor.write_ops import DESCRIPTORS
from deye_monitor.writes import WriteService

LOGGER = 1234567890


def frames(register: int, word: int) -> list[tuple[bytes, Framing]]:
    rtu = m.build_rtu_write(1, register, word)
    return [
        (rtu, Framing.RTU),
        (m.build_mbap_write(7, 1, register, word), Framing.MBAP),
        (v5.build_v5_write_request(LOGGER, 5, rtu), Framing.SOLARMAN_V5),
    ]


def send(register: int, mask: int, old: int, new: int) -> None:
    grant = WriteGrant(1, register, mask, old, new)
    for frame, framing in frames(register, new):
        guard_write_frame(frame, framing, grant=grant, logger_serial=LOGGER)


def test_a_both_target_is_refused_on_every_framing() -> None:
    for old in (0xAB00, 0xAB01, 0xAB02, 0xAB03):
        with pytest.raises(GuardViolation):
            send(140, 0x0003, old, 0xAB03)


@pytest.mark.parametrize("target", [0, 1, 2])
def test_an_incumbent_both_can_be_replaced(target: int) -> None:
    send(140, 0x0003, 0xAB03, 0xAB00 | target)


def test_the_new_selects_use_exact_target_domains() -> None:
    for old, new in ((0, 3), (1, 5), (2, 0xFFFF)):
        with pytest.raises(GuardViolation):
            send(133, 0xFFFF, old, new)
    send(133, 0xFFFF, 0, 2)


PER_UNIT = {
    "low_noise", "unbalanced_load", "ev_free_work", "ev_location", "ev_offgrid_soc", "ev_max_power",
}  # fmt: skip


def test_every_descriptor_declares_synchronization_from_the_address_ranges() -> None:
    for d in DESCRIPTORS:
        assert d.synchronized == (65 <= d.register <= 209 or 339 <= d.register <= 429), d.key
        assert d.view()["synchronized"] is d.synchronized
    assert {d.key for d in DESCRIPTORS if not d.synchronized} == PER_UNIT


def test_a_per_unit_register_is_writable_on_a_slave_a_synchronized_one_is_not() -> None:
    slave = {"parallel": True, "master": False}
    unknown = {"parallel": None, "master": None}
    assert WriteService._slave_locked(slave, True) is True
    assert WriteService._slave_locked(slave, False) is False  # addressed unit only
    assert WriteService._slave_locked(unknown, False) is True  # an unreadable role still locks
    assert WriteService._slave_locked({"parallel": True, "master": True}, True) is False
