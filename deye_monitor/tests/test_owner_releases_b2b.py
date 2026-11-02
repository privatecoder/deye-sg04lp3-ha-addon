"""0.5.0 B2b domain 7: R35 special command (wire shapes, per-unit on slaves, equal hint), R180,
R347, R178 bits 10-11 and the R181 AFCI switch / one-shot clear button."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deye_monitor.protocol import modbus as m
from deye_monitor.protocol import solarman_v5 as v5
from deye_monitor.protocol.guard import (
    Framing,
    GuardViolation,
    WriteGrant,
    guard_frame,
    guard_write_frame,
)
from deye_monitor.write_ops import BY_KEY
from tests.plant_helpers import plant_rig
from tests.test_write_protocol import rtu_write
from tests.write_helpers import build_plant, err, fast, preview, write

LOGGER = 2_700_000_001
SLAVE = {"r336": 0x0001}
MASTER = {"r336": 0x0003}


def cmd(target: int = 1, second: int = 100) -> WriteGrant:
    return WriteGrant(1, 35, 0xFFFF, 1 - target, target, command_word=second)


def frames(target: int = 1, second: int = 100) -> dict[Framing, bytes]:
    rtu = m.build_rtu_write(1, 35, target, second)
    return {
        Framing.RTU: rtu,
        Framing.MBAP: m.build_mbap_write(7, 1, 35, target, second),
        Framing.SOLARMAN_V5: v5.build_v5_write_request(LOGGER, 5, rtu),
    }


def test_the_command_frames_have_the_exact_shape() -> None:
    f = frames()
    assert f[Framing.RTU][:-2] == bytes.fromhex("011000230002040001") + bytes.fromhex("0064")
    assert len(f[Framing.RTU]) == m.RTU_COMMAND_REQUEST_LEN == 13
    assert len(f[Framing.MBAP]) == m.MBAP_COMMAND_REQUEST_LEN == 17
    assert len(f[Framing.SOLARMAN_V5]) == v5.COMMAND_REQUEST_FRAME_LEN == 41


@pytest.mark.parametrize("framing", list(Framing))
def test_the_guard_admits_exactly_the_command_frame(framing: Framing) -> None:
    ok = guard_write_frame(frames()[framing], framing, grant=cmd(), logger_serial=LOGGER)
    assert ok.grant.command_word == 100


@pytest.mark.parametrize("framing", list(Framing))
@pytest.mark.parametrize(("target", "second"), [(1, 99), (1, 101), (1, 0), (1, 0xFFFF), (0, 99)])
def test_a_command_with_another_second_word_is_refused(
    framing: Framing, target: int, second: int
) -> None:
    f = frames(target, second)[framing]
    with pytest.raises(GuardViolation):
        guard_write_frame(f, framing, grant=cmd(target), logger_serial=LOGGER)
    with pytest.raises(GuardViolation):  # nor can a grant carry it
        guard_write_frame(f, framing, grant=cmd(target, second), logger_serial=LOGGER)


@pytest.mark.parametrize("framing", list(Framing))
def test_no_scalar_or_foreign_count_two_write_passes(framing: Framing) -> None:
    scalar = WriteGrant(1, 35, 0xFFFF, 0, 1)  # an ordinary write of R35 is refused
    one = {
        Framing.RTU: m.build_rtu_write(1, 35, 1),
        Framing.MBAP: m.build_mbap_write(7, 1, 35, 1),
        Framing.SOLARMAN_V5: v5.build_v5_write_request(LOGGER, 5, m.build_rtu_write(1, 35, 1)),
    }[framing]
    with pytest.raises(GuardViolation):
        guard_write_frame(one, framing, grant=scalar, logger_serial=LOGGER)
    # a count-2 frame under a scalar grant, and a count-1 frame under a command grant
    with pytest.raises(GuardViolation):
        guard_write_frame(frames()[framing], framing, grant=scalar, logger_serial=LOGGER)
    with pytest.raises(GuardViolation):
        guard_write_frame(one, framing, grant=cmd(), logger_serial=LOGGER)
    # count 2 for any other register is not a command
    other = WriteGrant(1, 128, 0xFFFF, 20, 50, command_word=100)
    with pytest.raises(GuardViolation):
        guard_write_frame(
            m.build_rtu_write(1, 128, 50, 100), Framing.RTU, grant=other, logger_serial=None
        )


@pytest.mark.parametrize(("old", "new"), [(0, 2), (2, 0), (0xFFFF, 1), (1, 0xFFFF), (1, 1)])
def test_the_command_target_is_exactly_zero_or_one(old: int, new: int) -> None:
    g = WriteGrant(1, 35, 0xFFFF, old, new, command_word=100)
    with pytest.raises(GuardViolation):
        guard_write_frame(m.build_rtu_write(1, 35, new, 100), Framing.RTU, grant=g)


def test_the_read_guard_refuses_the_command_frames() -> None:
    for framing, frame in frames().items():
        with pytest.raises(GuardViolation):
            guard_frame(frame, framing, unit_id=1, max_read_registers=60, logger_serial=LOGGER)


def test_low_noise_descriptor_is_a_per_unit_command() -> None:
    d = BY_KEY["low_noise"]
    assert (d.register, d.command_word, d.synchronized, d.equal_hint) == (35, 100, False, True)
    assert "reset" in d.risk.en.lower() and "Reset" in d.risk.de
    assert "equal" in d.risk.en and "gleich" in d.risk.de


async def test_low_noise_is_written_once_as_one_two_word_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r35": 0}})
        op = await write(rig, "deye1", "low_noise", 1)
        assert op["status"] == "readback_confirmed" and op["new_word"] == 1
        writes = rig.servers["gw1"].responder.writes
        assert len(writes) == 1 and (writes[0].start, writes[0].fcount) == (35, 2)
        assert rig.servers["gw1"].responder.regs[35] == 1
        status, body = await preview(rig, "deye1", "low_noise", 1)
        assert (status, err(body)) == (409, "no_change")  # a no-op needs no send
        assert len(rig.servers["gw1"].responder.writes) == 1


@pytest.mark.parametrize("word", [2, 0x0100, 0xFFFF])
async def test_low_noise_with_a_foreign_word_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r35": word}})
        status, body = await preview(rig, "deye1", "low_noise", 1)
        assert (status, err(body)) == (409, "operation_locked")
        assert rig.servers["gw1"].responder.writes == []


async def test_a_slave_writes_only_its_per_unit_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig,
            inverters=("deye1", "deye2"),
            enable=("deye1", "deye2"),
            regs={"deye1": MASTER, "deye2": SLAVE},
        )
        op = await write(rig, "deye2", "low_noise", 1)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw2"].responder.regs[35] == 1  # the addressed unit only
        assert rig.servers["gw1"].responder.regs[35] == 0
        assert [w.start for w in rig.servers["gw1"].responder.writes] == []
        status, body = await preview(rig, "deye2", "grid_charge_current", 50)
        assert status == 409 and err(body) == "parallel_slave"


def test_an_unreadable_role_locks_every_setting_but_known_roles_split_by_sync() -> None:
    from deye_monitor.writes import WriteService

    lock = WriteService._slave_locked
    assert lock({"parallel": None, "master": None}, False) is True  # unknown system: lock all
    assert lock({"parallel": None, "master": None}, True) is True
    assert lock({"parallel": True, "master": False}, False) is False  # a slave: per unit ok
    assert lock({"parallel": True, "master": False}, True) is True
    assert lock({"parallel": True, "master": True}, True) is False
    assert lock({"parallel": False, "master": False}, True) is False


async def test_the_equal_hint_lists_every_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig,
            inverters=("deye1", "deye2"),
            regs={"deye1": {**MASTER, "r35": 1}, "deye2": {**SLAVE, "r35": 0}},
        )
        fast(rig)
        _status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        hint = body["info"]["equal"]["low_noise"]
        assert hint["values"] == {"deye1": 1, "deye2": 0} and hint["equal"] is False
        rig.servers["gw2"].responder.regs[35] = 1
        _status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert body["info"]["equal"]["low_noise"]["equal"] is True
        rig.servers["gw2"].responder.regs[35] = 7  # a word outside the domain is unknown
        __status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        assert body["info"]["equal"]["low_noise"] == {
            "values": {"deye1": 1, "deye2": None},
            "equal": None,
        }


# -- R180 / R347 / R178 bits 10-11 ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "ok"), [(10, True), (900, True), (300, True), (5, False), (905, False), (905, False),
                      (15, False), (901, False), ("12.5", False)]
)  # fmt: skip
async def test_reconnection_time_targets_are_in_the_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any, ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", "grid_reconnection_time", value)
        assert (status == 200) is ok, (value, body)


@pytest.mark.parametrize("word", [901, 905, 1500])
async def test_an_off_grid_incumbent_is_shown_not_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r180": word}})
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        row = {r["key"]: r for r in body["operations"]}["grid_reconnection_time"]
        assert (row["state"], row["value"]) == ("ready", word)
        status, body = await preview(rig, "deye1", "grid_reconnection_time", 600)
        assert status == 200, body
        status, body = await preview(rig, "deye1", "grid_reconnection_time", 905)
        assert status == 422


async def test_reconnection_time_refuses_the_sentinel_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r180": 0xFFFF}})
        status, _body = await preview(rig, "deye1", "grid_reconnection_time", 600)
        assert status in (409, 422)
        assert rig.servers["gw1"].responder.writes == []


@pytest.mark.parametrize(
    ("value", "ok"), [(100, True), (20000, True), (3000, True), (99, False), (150, False),
                      (20100, False), (0, False)]
)  # fmt: skip
async def test_ct_ratio_range_and_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: int, ok: bool
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",))
        status, body = await preview(rig, "deye1", "external_ct_ratio", value)
        assert (status == 200) is ok, (value, body)


async def test_bms_err_stop_needs_battery_and_bms_and_keeps_other_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r178": 0x2AAA, "r98": 1}})
        inv = rig.servers["gw1"]
        from tests.write_helpers import svc_of

        svc_of(rig).runtime.inverters["deye1"].features = (
            svc_of(rig)
            .runtime.inverters["deye1"]
            .features.model_copy(update={"battery": True, "bms": False})
        )
        status, body = await preview(rig, "deye1", "bms_loss_fault", 1)
        assert (status, err(body)) == (409, "operation_locked")
        svc_of(rig).runtime.inverters["deye1"].features = (
            svc_of(rig).runtime.inverters["deye1"].features.model_copy(update={"bms": True})
        )
        op = await write(rig, "deye1", "bms_loss_fault", 1)
        assert op["status"] == "readback_confirmed"
        assert inv.responder.regs[178] == 0x2EAA


@pytest.mark.parametrize("word", [0x2AA5 & ~0x0C00 | 0x0400, 0x2AAA | 0x0C00 & 0x0400, 0xFFFF])
async def test_bms_err_stop_refuses_a_no_action_pair_and_the_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    word = (word & ~0x0C00) | 0x0400 if word != 0xFFFF else word  # pair 01
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r178": word}})
        status, _body = await preview(rig, "deye1", "bms_loss_fault", 1)
        assert status == 409
        assert rig.servers["gw1"].responder.writes == []


def test_a_final_word_of_ffff_is_never_produced_by_a_bms_pair_write() -> None:
    g = WriteGrant(1, 178, 0x0C00, 0xFBFF, 0xFFFF)
    with pytest.raises(GuardViolation):
        guard_write_frame(rtu_write(1, 178, 0xFFFF), Framing.RTU, grant=g)


# -- R181 AFCI -----------------------------------------------------------------------------------


async def test_afci_switch_and_the_one_shot_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r181": 1, "r558": 0x4000}})
        server = rig.servers["gw1"]
        op = await write(rig, "deye1", "afci_clear_fault", "press")
        assert op["status"] == "readback_confirmed"  # command accepted ...
        assert server.responder.regs[181] == 2
        assert op["fault"]["state"] == "still_reported"  # ... reported apart from the fault
        server.responder.regs[558] = 0
        # a pending command (2) is neither cleared again nor toggled
        status, body = await preview(rig, "deye1", "afci_clear_fault", "press")
        assert (status, err(body)) == (409, "operation_locked")
        status, body = await preview(rig, "deye1", "afci_enable", 0)
        assert (status, err(body)) == (409, "operation_locked")
        assert len(server.responder.writes) == 1


async def test_clear_never_enables_afci_implicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r181": 0}})
        status, body = await preview(rig, "deye1", "afci_clear_fault", "press")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "afci_clear_needs_enabled"
        for value in ("PRESS", 1, True, None, 2):
            status, body = await preview(rig, "deye1", "afci_clear_fault", value)
            assert status in (409, 422), value
        op = await write(rig, "deye1", "afci_enable", 1)
        assert op["status"] == "readback_confirmed"
        assert rig.servers["gw1"].responder.writes[0].value == 1


async def test_afci_info_shows_module_version_and_fault_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r558": 0x4000}})
        _status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        info = body["info"]["afci"]
        assert info["fault"] == "still_reported" and "module_version" in info


@pytest.mark.parametrize(
    ("old", "new", "ok"),
    [(0, 1, True), (1, 0, True), (1, 2, True), (0, 2, False), (2, 1, False), (2, 0, False),
     (0xFFFF, 1, False), (1, 0xFFFF, False), (1, 3, False)],
)  # fmt: skip
def test_guard_allows_only_the_r181_transitions(old: int, new: int, ok: bool) -> None:
    g = WriteGrant(1, 181, 0xFFFF, old, new)
    if ok:
        assert guard_write_frame(rtu_write(1, 181, new), Framing.RTU, grant=g)
    else:
        with pytest.raises(GuardViolation):
            guard_write_frame(rtu_write(1, 181, new), Framing.RTU, grant=g)
