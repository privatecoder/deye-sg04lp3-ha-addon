"""The live-read runbook (docs/LIVE-READ-RUNBOOK.md) must agree with the code: every YAML example
passes the Supervisor option rules and the app parser, the documented entity names exist and the
documented read plan sizes are the planner's."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from deye_monitor.config import parse_config
from deye_monitor.device import InverterState
from deye_monitor.ha_discovery import Namespace, app_components, inverter_diagnostic_components
from deye_monitor.profiles.schema import load_profile
from deye_monitor.selection import Edit, SelectionManager, SelectionStore, specs_from_config

ROOT = Path(__file__).parents[2]
APP = ROOT / "deye_monitor"
RUNBOOK = (ROOT / "docs" / "LIVE-READ-RUNBOOK.md").read_text(encoding="utf-8")
PROFILE = load_profile("sg04lp3_lv_v105_8")


def plant_config() -> Any:
    """The documented stage 1: one inverter behind a Modbus TCP gateway (set up in the web UI)."""
    return parse_config(
        {
            "endpoints": [
                {
                    "id": "gw1",
                    "transport": "modbus_tcp",
                    "host": "192.0.2.11",
                    "max_read_registers": 20,
                    "request_gap_ms": 250,
                    "queue_size": 8,
                }
            ],
            "inverters": [{"id": "deye1", "name": "Deye 1", "endpoint": "gw1", "unit_id": 1}],
            "instance_id": "deyetest1",
        }
    )


def test_the_runbook_has_no_option_examples_any_more() -> None:
    assert "```yaml" not in RUNBOOK
    assert "Appendix A" not in RUNBOOK


def _plan_blocks(keys: list[str], tmp_path: Path) -> tuple[int, int, bool, set[int]]:
    cfg = plant_config()
    manager = SelectionManager(SelectionStore(tmp_path, specs_from_config(cfg)))
    template = manager.selection("deye1").template
    edit = Edit(remove=frozenset(template - set(keys)), add=frozenset(set(keys) - template))
    plan = manager.dry_run({"deye1": edit})["deye1"][2]
    return len(plan.blocks), len(template), plan.reads_r65, set(plan.addresses)


MINIMAL = [
    "serial_number",
    "operating_state",
    "active_warnings",
    "active_faults",
]
MORE = ["battery_voltage", "battery_soc", "inverter_frequency", "pv1_power", "pv2_power"]


def test_the_documented_read_plan_sizes_are_the_planners(tmp_path: Path) -> None:
    blocks, template_size, r65, addresses = _plan_blocks(MINIMAL, tmp_path)
    assert blocks == 12 and not r65
    assert "**4 sensors, 12 polling blocks**" in RUNBOOK and "**12 read blocks**" in RUNBOOK
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
        11,
        12,
        13,
        16,
        17,
        18,
        20,
        21,
        22,
        24,
        98,
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
    cfg = plant_config()
    manager = SelectionManager(SelectionStore(tmp_path, specs_from_config(cfg)))
    plan = manager.plan("deye1")
    assert len(plan.blocks) == 32 and plan.reads_r65
    assert "32 read blocks" in RUNBOOK


def test_the_documented_diagnostic_entities_exist() -> None:
    ns = Namespace("deyetest1", "homeassistant", "deye")
    states = tuple(x.value for x in InverterState)
    app = {c["default_entity_id"] for c in app_components(ns).values()}
    per_inverter = {
        c["default_entity_id"]
        for c in inverter_diagnostic_components(ns, "deye1", states, show_r65=True).values()
    }
    ids = app | per_inverter
    documented = set(re.findall(r"`(sensor\.deye_deyetest1_[a-z0-9_]+)`", RUNBOOK))
    assert documented and documented <= ids, sorted(documented - ids)
    # every per-inverter diagnostic is documented
    suffixed = set(re.findall(r"`…(_[a-z0-9_]+)`", RUNBOOK))
    covered = {i for i in per_inverter if any(i.endswith(x) for x in suffixed) or i in documented}
    assert covered == per_inverter, sorted(per_inverter - covered)


def test_the_documented_sensor_keys_and_register_claims_match_the_profile() -> None:
    keys = set(
        re.findall(
            r"`(?:…_)?(serial_number|operating_state|active_warnings|active_faults|battery_voltage|battery_soc|inverter_frequency|pv[12]_power)`",
            RUNBOOK,
        )
    )
    assert keys >= {"serial_number", "operating_state", "active_warnings", "active_faults", *MORE}
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
    """Connections and inverters are created in the web interface."""
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
    assert "Writing: off" in RUNBOOK  # label "Writing" + the chip of the card
    assert "'plant.inv.writes': 'Writing'" in texts
    assert "'writes.summary_off': 'off'" in texts
    assert "bei gestoppter App den Inverter in den Optionen" not in RUNBOOK
    assert "plant.a.json" in RUNBOOK
