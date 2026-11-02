"""Review33 R33-16: sensor names carry no register numbers (the register is in the (i) note)."""

from __future__ import annotations

import re

from deye_monitor.profiles.schema import load_profile
from deye_monitor.webapi import entry_view

P = load_profile("sg04lp3_lv_v105_8")
REGISTER = re.compile(r"(?<![A-Za-z0-9])R\d{1,3}(?!\d)")


def test_no_register_number_in_any_sensor_name() -> None:
    bad = [
        (e.key, lang, text)
        for e in P.entries
        for lang in ("de", "en")
        if REGISTER.search(text := getattr(e.name, lang))
    ]
    assert not bad


def test_web_sensor_view_names_have_no_register_number() -> None:
    for e in P.entries:
        name = entry_view(e)["name"]
        assert not REGISTER.search(name["de"]) and not REGISTER.search(name["en"]), e.key


def test_sensor_names_have_no_raw_qualifier() -> None:
    for e in P.entries:
        for text in (e.name.de, e.name.en):
            assert not re.search(r"\b(roh|raw|Rohwert)\b|\(hex\)", text, re.I), (e.key, text)


def test_established_terms_are_used() -> None:
    names = {e.key: e.name for e in P.entries}
    assert names["track_grid_phase_raw"].de == "Track Grid-Phase"
    assert "Interner RCD" in names["rcd_enable_raw"].de
