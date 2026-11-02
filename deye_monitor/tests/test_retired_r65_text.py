"""R39-03: shipped operational and profile text must not instruct the retired R65 behaviour."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = [
    ROOT / "deye_monitor/src/deye_monitor/profiles/sg04lp3_lv_v105_8.yaml",
    ROOT / "deye_monitor/src/deye_monitor/energy_state.py",
    ROOT / "deye_monitor/src/deye_monitor/web/static/i18n.js",
    ROOT / "docs/LIVE-READ-RUNBOOK.md",
]
RETIRED = [
    r"Resolution from R65",
    r"decides the resolution",
    r"bestimmt, wie genau",
    r"selects 0\.1 kWh energy resolution",
    r"user chooses it",
    r"r65_invalid",
    r"no valid energy resolution \(R65\)",
    r"two\s+\*?consecutive\*? identical",
    r"source \(`R65`",
]


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_no_retired_r65_instruction(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    found = [p for p in RETIRED if re.search(p, text)]
    assert not found, f"{path.name} still carries retired R65 text: {found}"
