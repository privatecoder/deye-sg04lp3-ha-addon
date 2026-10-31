"""The live-read runbook (docs/LIVE-READ-RUNBOOK.md) must agree with the code: every YAML example
passes the Supervisor option rules and the app parser, the documented entity names exist and the
documented read plan sizes are the planner's."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

from deye_monitor.device import InverterState
from deye_monitor.ha_discovery import Namespace, app_components
from deye_monitor.profiles.schema import load_profile
from deye_monitor.selection import Edit, SelectionManager, SelectionStore, specs_from_config
from tests.test_packaging import accepted

ROOT = Path(__file__).parents[2]
APP = ROOT / "deye_monitor"
RUNBOOK = (ROOT / "docs" / "LIVE-READ-RUNBOOK.md").read_text(encoding="utf-8")
PROFILE = load_profile("sg04lp3_lv_v105_8")


def examples() -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], yaml.safe_load(b))
        for b in re.findall(r"```yaml\n(.*?)```", RUNBOOK, re.S)
    ]


def test_every_yaml_example_passes_the_supervisor_rules_and_the_parser() -> None:
    blocks = examples()
    assert len(blocks) == 3  # gateway, logger, four gateways
    for block in blocks:
        cfg = accepted(block)
        assert cfg.instance_id == "deyetest1"
        assert all(not i.allow_writes for i in cfg.inverters)
        assert all(i.expected_serial and len(i.expected_serial) == 10 for i in cfg.inverters)
        assert all(e.max_read_registers >= 6 for e in cfg.endpoints)
        assert block["mqtt"] == {}
        assert not [k for k in block if k.startswith("selected")]  # the schema has no such option


def test_the_four_gateway_example_uses_four_separate_gateways_with_unit_1() -> None:
    cfg = accepted(examples()[2])
    assert len(cfg.endpoints) == 4 and len(cfg.inverters) == 4
    assert {i.unit_id for i in cfg.inverters} == {1}
    assert len({i.expected_serial for i in cfg.inverters}) == 4


def _plan_blocks(keys: list[str], tmp_path: Path) -> tuple[int, int, bool, set[int]]:
    cfg = accepted(examples()[0])
    manager = SelectionManager(SelectionStore(tmp_path, specs_from_config(cfg)))
    template = manager.selection("deye1").template
    edit = Edit(remove=frozenset(template - set(keys)), add=frozenset(set(keys) - template))
    plan = manager.dry_run({"deye1": edit})["deye1"][2]
    return len(plan.blocks), len(template), plan.reads_r65, set(plan.addresses)


MINIMAL = [
    "serial_number",
    "operating_state",
    "fault_word_1",
    "fault_word_2",
    "fault_word_3",
    "fault_word_4",
]
MORE = ["battery_voltage", "battery_soc", "inverter_frequency", "pv1_power", "pv2_power"]


def test_the_documented_read_plan_sizes_are_the_planners(tmp_path: Path) -> None:
    blocks, template_size, r65, addresses = _plan_blocks(MINIMAL, tmp_path)
    assert blocks == 12 and not r65
    assert "**6 sensors, 12 polling blocks**" in RUNBOOK and "**12 read blocks**" in RUNBOOK
    # no energy, battery, AC or PV registers; the internal identity/status/settings reads only
    assert addresses == {
        0,
        3,
        4,
        5,
        6,
        7,
        14,
        15,
        17,
        18,
        20,
        21,
        22,
        24,
        111,
        133,
        189,
        336,
        500,
    }.union(range(551, 559))
    for chosen in (MINIMAL, MINIMAL + MORE):
        assert all(PROFILE.entry(k).selectable for k in chosen)
    (tmp_path / "more").mkdir()
    more_blocks, _, r65_more, _ = _plan_blocks(MINIMAL + MORE, tmp_path / "more")
    assert more_blocks == 15 and not r65_more
    assert "**11 sensors, 15 polling blocks**" in RUNBOOK
    assert f"{template_size} sensors" in RUNBOOK


def test_the_default_template_figures_in_the_runbook(tmp_path: Path) -> None:
    cfg = accepted(examples()[0])
    manager = SelectionManager(SelectionStore(tmp_path, specs_from_config(cfg)))
    plan = manager.plan("deye1")
    assert len(plan.blocks) == 30 and plan.reads_r65
    assert "30 read blocks" in RUNBOOK


def test_the_documented_diagnostic_entities_exist() -> None:
    ns = Namespace("deyetest1", "homeassistant", "deye_monitor")
    comps = app_components(ns, {"deye1": "Deye 1"}, tuple(s.value for s in InverterState))
    ids = {c["default_entity_id"] for c in comps.values()}
    documented = set(re.findall(r"`(sensor\.deye_deyetest1_app_[a-z0-9_]+)`", RUNBOOK))
    assert documented and documented <= ids, sorted(documented - ids)
    # every per-inverter diagnostic of the app is documented
    per_inverter = {i for i in ids if "_app_inverter_deye1_" in i}
    suffixed = set(re.findall(r"`…(_[a-z0-9_]+)`", RUNBOOK))
    covered = {i for i in per_inverter if any(i.endswith(s) for s in suffixed) or i in documented}
    assert covered == per_inverter, sorted(per_inverter - covered)


def test_the_documented_sensor_keys_and_register_claims_match_the_profile() -> None:
    keys = set(
        re.findall(
            r"`(?:…_)?(serial_number|operating_state|fault_word_[1-4]|battery_voltage|battery_soc|inverter_frequency|pv[12]_power)`",
            RUNBOOK,
        )
    )
    assert keys >= {"serial_number", "operating_state", "fault_word_1", "fault_word_4", *MORE}
    assert (PROFILE.entry("battery_soc").addr, PROFILE.entry("battery_soc").scale) == (588, 1.0)
    assert (PROFILE.entry("battery_voltage").addr, PROFILE.entry("battery_voltage").scale) == (
        587,
        0.01,
    )
    assert (
        PROFILE.entry("inverter_frequency").addr,
        PROFILE.entry("inverter_frequency").scale,
    ) == (638, 0.01)
    assert (PROFILE.entry("pv1_power").addr, PROFILE.entry("pv2_power").addr) == (672, 673)
    assert PROFILE.entry("load_frequency").default_enabled is False  # the R655 duplicate stays out
    assert "max(1 s, 2" in RUNBOOK


def test_the_runbook_keeps_the_read_only_boundary() -> None:
    assert "FC03" in RUNBOOK and "allow_writes: false" in RUNBOOK
    assert "allow_writes: true" not in RUNBOOK
    for forbidden in ("FC06", "FC16", "R80", "R336/R337"):
        assert forbidden in RUNBOOK  # named as excluded actions
    assert "Maintenance area" in RUNBOOK and "/data" in RUNBOOK


def test_docs_and_readme_link_to_the_runbook() -> None:
    assert "docs/LIVE-READ-RUNBOOK.md" in (ROOT / "deye_monitor" / "DOCS.md").read_text()
    assert "docs/LIVE-READ-RUNBOOK.md" in (ROOT / "deye_monitor" / "README.md").read_text()


def test_the_first_start_instructions_use_the_texts_of_the_interface() -> None:
    texts = (APP / "src" / "deye_monitor" / "web" / "static" / "i18n.js").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "Please choose sensors",
        "Choose now",
        "Basics only",
        "Deselect all",
        "Accept the template unchanged",
    ):
        assert phrase in RUNBOOK, phrase
        assert phrase in texts, phrase
    assert "placeholder" not in RUNBOOK  # the workaround of the first version is gone
    assert "127.0.0.1" not in RUNBOOK


def test_the_runbook_sets_the_plant_up_in_the_web_interface() -> None:
    """T14: connections and inverters are created in the web interface; the YAML examples are only
    the migration path (appendix)."""
    texts = (APP / "src" / "deye_monitor" / "web" / "static" / "i18n.js").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "Set up plant",
        "Gateway (Modbus TCP → RTU)",
        "Gateway transparent (RTU over TCP)",
        "WiFi logger (Solarman V5)",
        "Check connection",
        "Start setup",
    ):
        assert phrase in RUNBOOK, phrase
        assert phrase in texts, phrase
    assert "Writing: not available" in RUNBOOK  # shown as label "Writing" + "not available"
    assert "'plant.inv.writes': 'Writing'" in texts
    assert "'plant.writes_unavailable': 'not available'" in texts
    appendix = RUNBOOK.index("## Appendix A")
    assert all(RUNBOOK.index(block) > appendix for block in re.findall(r"```yaml\n", RUNBOOK))
    assert "Migration path" in RUNBOOK[appendix:]
    assert "bei gestoppter App den Inverter in den Optionen" not in RUNBOOK
    assert "plant.a.json" in RUNBOOK
