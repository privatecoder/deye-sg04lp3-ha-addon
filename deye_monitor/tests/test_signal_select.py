"""0.5.0 part B: the R140 select "External signal" (Off / Grid Signal / Gen Signal): one fresh
read-modify-write of mask 0x0003, one FC16 word, the incumbent "Both" shown but never written, the
Gen option gated by the GEN port mode, the description gate, HA select entity and preselection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deye_monitor.ha_discovery import setting_component, validate_component
from deye_monitor.mqtt_settings import parse_command, state_value
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.setting_presets import suggested_operations
from deye_monitor.protocol.guard import (
    WRITABLE_FIELDS,
    GuardViolation,
    WriteGrant,
    check_grant,
)
from deye_monitor.write_ops import BY_KEY, Kind, Refused, parse_value
from deye_monitor.writes import SettingState
from tests.plant_helpers import plant_rig
from tests.test_ha_discovery import NS
from tests.write_helpers import build_plant as _build_plant
from tests.write_helpers import err, preview, svc_of, write


async def build_plant(rig: Any, **kw: Any) -> None:
    """A plant whose description says a signal contact is used (R140 is locked otherwise)."""
    await _build_plant(rig, **kw)
    for inv in svc_of(rig).runtime.inverters.values():
        inv.features = inv.features.model_copy(update={"external_signal_use": "other"})


SIGNAL = BY_KEY["external_signal"]


def grant(old: int, new: int, mask: int = 0x0003) -> WriteGrant:
    return WriteGrant(unit=1, register=140, mask=mask, old_word=old, new_word=new)


# -- descriptor ----------------------------------------------------------------------------------


def test_the_descriptor_is_a_select_over_mask_3_with_a_risk_text() -> None:
    assert (SIGNAL.register, SIGNAL.mask, SIGNAL.kind) == (140, 0x0003, Kind.SELECT)
    assert WRITABLE_FIELDS[140] == {0x0003}
    assert [(o.field, o.slug) for o in SIGNAL.options] == [
        (0, "off"),
        (1, "grid"),
        (2, "gen"),
        (3, "both"),
    ]
    assert "last change to Off" in SIGNAL.risk.en and SIGNAL.risk.de
    assert SIGNAL.group == "signal"


@pytest.mark.parametrize(
    ("old", "field", "new"),
    [
        (0x0000, 1, 0x0001),
        (0x0001, 2, 0x0002),  # grid -> gen is ONE word, never through off
        (0x0002, 1, 0x0001),  # gen -> grid
        (0x0001, 0, 0x0000),
        (0x0003, 1, 0x0001),  # an incumbent "both": the user picks one
        (0x0003, 2, 0x0002),
        (0xABCC, 1, 0xABCD),  # bits 2-15 are preserved
        (0xABCF, 0, 0xABCC),
    ],
)
def test_encoding_replaces_bits_0_and_1_only(old: int, field: int, new: int) -> None:
    assert SIGNAL.encode(old, field) == new
    assert (old ^ new) & 0xFFFC == 0


def test_the_sentinel_word_is_refused_in_and_out() -> None:
    with pytest.raises(Refused) as exc:
        SIGNAL.encode(0xFFFF, 1)
    assert exc.value.key == "unexpected_bits"
    with pytest.raises(Refused):
        SIGNAL.encode(0xFFFC, 3)  # would produce 0xFFFF


def test_values_are_the_option_names_and_both_is_never_a_target() -> None:
    assert [parse_value(SIGNAL, s) for s in ("off", "grid", "gen")] == [0, 1, 2]
    with pytest.raises(Refused) as exc:
        parse_value(SIGNAL, "both")
    assert exc.value.key == "option_not_writable"
    for bad in ("Both", "grid ", 1, True, None, ""):
        with pytest.raises(Refused) as exc2:
            parse_value(SIGNAL, bad)
        assert exc2.value.key == "invalid_value"


def test_an_incumbent_both_is_displayed_as_such() -> None:
    assert [SIGNAL.display(f) for f in range(4)] == ["off", "grid", "gen", "both"]
    assert all(SIGNAL.incumbent_valid(f) for f in range(4))
    assert SIGNAL.view()["options"][3] == {
        "field": 3,
        "slug": "both",
        "label": {"de": "Beide (gemeinsamer Ausgang)", "en": "Both (shared output)"},
        "writable": False,
        "gate_reason": None,
        "feature_reason": None,
    }


# -- guard ---------------------------------------------------------------------------------------


def test_the_guard_admits_exactly_mask_3_without_the_sentinel() -> None:
    for old, new in ((0, 1), (1, 2), (2, 1), (3, 1), (0xAB00, 0xAB01)):
        check_grant(grant(old, new))
    refused = [
        grant(0, 1, mask=0xFFFF),
        grant(0, 1, mask=0x0001),  # the old per-bit masks are not the table's
        grant(0, 1, mask=0x0002),
        grant(0, 4),  # a bit outside the pair
        grant(0x0004, 0x0001),
        grant(0xFFFF, 0xFFFD),
        grant(0xFFFC, 0xFFFF),
    ]
    for g in refused:
        with pytest.raises(GuardViolation):
            check_grant(g)


# -- the guarded write path ----------------------------------------------------------------------


async def test_a_source_change_is_one_word_and_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(
            rig, inverters=("deye1",), regs={"deye1": {"r133": 0, "r140": 0xAB01}}
        )  # generator port, grid signal on
        server = rig.servers["gw1"]
        op = await write(rig, "deye1", "external_signal", "gen")
        assert op["status"] == "readback_confirmed"
        assert (op["old_word"], op["new_word"]) == (0xAB01, 0xAB02)
        assert server.responder.regs[140] == 0xAB02
        assert [w.start for w in server.responder.writes] == [140]  # one write, never Off first
        op = await write(rig, "deye1", "external_signal", "off")
        assert server.responder.regs[140] == 0xAB00
        status, body = await preview(rig, "deye1", "external_signal", "off")
        assert (status, err(body)) == (409, "no_change")
        assert len(server.responder.writes) == 2


async def test_an_incumbent_both_can_only_be_replaced_by_one_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 0, "r140": 3}})
        status, body = await preview(rig, "deye1", "external_signal", "both")
        assert (status, err(body)) == (422, "option_not_writable")
        assert rig.servers["gw1"].responder.writes == []
        op = await write(rig, "deye1", "external_signal", "grid")
        assert rig.servers["gw1"].responder.regs[140] == 1
        assert (op["old_word"], op["new_word"]) == (3, 1)


async def test_gen_signal_needs_the_generator_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 1}})  # Smart Load
        status, body = await preview(rig, "deye1", "external_signal", "gen")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "gen_port_not_generator"
        op = await write(rig, "deye1", "external_signal", "grid")  # the others stay open
        assert op["status"] == "readback_confirmed"
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        row = next(r for r in body["operations"] if r["key"] == "external_signal")
        assert row["state"] == "ready"
        assert row["options"] == {
            "off": None,
            "grid": None,
            "gen": "gen_port_not_generator",
            "both": "option_not_writable",
        }


@pytest.mark.parametrize("word", [0xFFFF])
async def test_the_sentinel_locks_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: int
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r140": word}})
        status, body = await preview(rig, "deye1", "external_signal", "grid")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "unexpected_bits"
        assert rig.servers["gw1"].responder.writes == []


async def test_a_description_without_a_signal_contact_shows_the_setting_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await build_plant(rig, inverters=("deye1",), regs={"deye1": {"r133": 0}})
        svc = svc_of(rig)
        inv = svc.runtime.inverters["deye1"]
        inv.features = inv.features.model_copy(update={"external_signal_use": "none"})
        status, body = await preview(rig, "deye1", "external_signal", "grid")
        assert (status, err(body)) == (409, "operation_locked")
        assert body["error"]["params"]["reason"] == "signal_not_used"
        status, body = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        row = next(r for r in body["operations"] if r["key"] == "external_signal")
        assert (row["state"], row["reason"]) == ("locked", "signal_not_used")  # shown, not hidden
        assert rig.servers["gw1"].responder.writes == []
        inv.features = inv.features.model_copy(update={"external_signal_use": "unknown"})
        status, body = await preview(rig, "deye1", "external_signal", "grid")
        assert (status, body["error"]["params"]["reason"]) == (409, "signal_not_used")
        _, cur = await rig.call("POST", "/api/inverters/deye1/writes/current", {})
        row = next(r for r in cur["operations"] if r["key"] == "external_signal")
        assert (row["state"], row["reason"]) == ("locked", "signal_not_used")
        inv.features = inv.features.model_copy(update={"external_signal_use": "other"})
        assert (await write(rig, "deye1", "external_signal", "grid"))[
            "status"
        ] == "readback_confirmed"
        assert rig.servers["gw1"].responder.writes[-1].start == 140


# -- description, preselection ---------------------------------------------------------------------


def test_the_description_gate_blocks_no_and_unanswered() -> None:
    (gate,) = SIGNAL.feature_gates
    assert gate.feature == "external_signal_use" and gate.blocked == {"none", "unknown"}
    assert gate.reason == "signal_not_used"


@pytest.mark.parametrize(
    ("use", "expected"),
    [
        ("grid_contactor", True),
        ("generator_start", True),
        ("shared_grid_and_generator_logic", True),
        ("none", False),
        ("unknown", False),
        ("other", False),
    ],
)
def test_preselection_follows_the_description(use: str, expected: bool) -> None:
    ops = suggested_operations(Features(external_signal_use=use))  # type: ignore[arg-type]
    assert ("external_signal" in ops) is expected


# -- Home Assistant ------------------------------------------------------------------------------


def test_the_entity_is_a_select_in_the_config_category() -> None:
    key, comp = setting_component(NS, "deye1", SIGNAL, "t1", None)
    validate_component(key, comp)
    assert key == "set_external_signal" and comp["platform"] == "select"
    assert comp["entity_category"] == "config"
    assert comp["options"] == ["Off", "Grid Signal", "Gen Signal", "Both (shared output)"]
    assert comp["command_topic"].endswith("/external_signal")
    assert not {"min", "max", "step", "mode", "unit_of_measurement"} & set(comp)


def test_commands_are_exactly_an_option_text() -> None:
    assert parse_command(SIGNAL, b"Grid Signal") == ("grid", 1)
    assert parse_command(SIGNAL, b"Off") == ("off", 0)
    assert parse_command(SIGNAL, b"Gen Signal") == ("gen", 2)
    for bad in (b"grid", b"ON", b"", b"Grid Signal ", b"1", b"\xff"):
        with pytest.raises(Refused) as exc:
            parse_command(SIGNAL, bad)
        assert exc.value.key == "invalid_payload"
    with pytest.raises(Refused) as exc3:  # the incumbent text is not a command
        parse_command(SIGNAL, b"Both (shared output)")
    assert exc3.value.key == "option_not_writable"


def test_the_state_is_the_option_text() -> None:
    def state(field: int) -> SettingState:
        return SettingState(
            key="external_signal",
            ready=True,
            reason=None,
            word=field,
            field=field,
            value=SIGNAL.display(field),
            cap=None,
        )

    assert [state_value(SIGNAL, state(f)) for f in range(4)] == [
        "Off",
        "Grid Signal",
        "Gen Signal",
        "Both (shared output)",
    ]
