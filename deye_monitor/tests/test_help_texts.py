"""The (i) help texts: every write setting has German and English help, sensors carry help only
for existing profile entries, and the texts stay generic (no owner plant data)."""

from __future__ import annotations

import re

from deye_monitor.help_texts import SENSOR_HELP, WRITE_HELP, sensor_help, write_help
from deye_monitor.profiles.schema import load_profile
from deye_monitor.webapi import entry_view
from deye_monitor.write_ops import DESCRIPTORS

# installation specifics that never belong in generic help: vendor/product names of one plant
PLANT_NAMES = ("seplos", "photovoltaikforum", "waveshare", "solarman", "gw1")
# serial numbers (10+ digits), calendar dates and battery sizes are installation data
PLANT_PATTERNS = (
    re.compile(r"\b\d{10,}\b"),
    re.compile(r"\b\d{1,2}\.\d{1,2}\.20\d\d\b"),
    re.compile(r"\b\d{2,4}\s?ah\b"),
)
SENTENCE = re.compile(r"[.!?](?:\s|$)")


def test_every_write_descriptor_has_german_and_english_help() -> None:
    assert {d.key for d in DESCRIPTORS} == set(WRITE_HELP)
    for d in DESCRIPTORS:
        view = d.view()
        assert set(view["help"]) == {"de", "en"}, d.key
        assert view["help"]["de"].strip() and view["help"]["en"].strip(), d.key


def test_help_is_short_plain_text_of_two_to_five_sentences() -> None:
    for table in (WRITE_HELP, SENSOR_HELP):
        for key, texts in table.items():
            for text in texts:
                assert 2 <= len(SENTENCE.findall(text)) <= 5, (key, text)
                assert not re.search(r"<[A-Za-z/]", text), key  # text only, never markup
                assert len(text) <= 700, key


def test_help_texts_hold_no_owner_plant_data() -> None:
    for table in (WRITE_HELP, SENSOR_HELP):
        for key, texts in table.items():
            for text in texts:
                lowered = text.lower()
                assert not [w for w in PLANT_NAMES if w in lowered], (key, text)
                assert not [p.pattern for p in PLANT_PATTERNS if p.search(lowered)], (key, text)
                assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text), key


def test_german_and_english_differ_and_neither_is_a_placeholder() -> None:
    for table in (WRITE_HELP, SENSOR_HELP):
        for key, (de, en) in table.items():
            assert de != en, key
            assert not de.lstrip().startswith(("TODO", "TBD")) and not en.startswith("TODO"), key


def test_sensor_help_belongs_to_real_profile_entries() -> None:
    profile = load_profile("sg04lp3_lv_v105_8")
    entries = {e.key: e for e in profile.entries}
    assert not sorted(set(SENSOR_HELP) - set(entries))
    assert entry_view(entries["relay_grid"])["help"] == sensor_help("relay_grid")
    assert entry_view(entries["battery_voltage"])["help"] is None


def test_unknown_keys_have_no_help() -> None:
    assert write_help("no_such_setting") is None
    assert sensor_help("no_such_sensor") is None


def test_the_bms_err_stop_text_names_the_trade_off() -> None:
    help_ = write_help("bms_loss_fault")
    assert help_ is not None
    assert "trade-off" in help_["en"] and "BMS" in help_["en"]
    assert "Abwägung" in help_["de"]


def _both(table: dict[str, tuple[str, str]], key: str) -> tuple[str, str]:
    de, en = table[key]
    return de, en


def test_corrected_helps_state_the_qualified_semantics() -> None:
    """Review33: key phrases of every corrected help, German and English."""
    phrases: dict[str, tuple[tuple[str, str], ...]] = {
        "pv_sale_enable": (
            ("Solar Sell", "Solar Sell"),
            ("Zero Export to CT", "Zero Export to CT"),
            ("Selling First", "Selling First"),
        ),
        "bms_loss_fault": (
            ("BMS Err_Stop", "BMS Err_Stop"),
            ("löscht keinen", "does not clear"),
            ("andere Grenzen", "other limits"),
        ),
        "battery_charge_current": (("kommunizierendem", "communicating"),),
        "bms_charge_current_limit": (("0 A", "0 A"), ("nicht aktualisiert", "not refreshed")),
        "bms_charge_voltage": (("R210", "R210"), ("Hundertstelvolt", "centivolts")),
        "bms_discharge_voltage": (("R211", "R211"), ("Protokoll", "protocol")),
        "bms_offgrid_charge_limit": (("nie ein echtes", "never a real"),),
        "bms_temperature": (("82 °C", "82 °C"), ("ungeklärt", "unresolved")),
        "battery_temperature": (("beobachteten", "observed"),),
        "bms_power_derived": (
            ("negiertes Produkt", "negated product"),
            ("500 W", "500 W"),
            ("positiv", "positive"),
        ),
        "smart_load_off_voltage": (("AC-Kopplung", "AC coupling"), ("Anforderung", "request")),
        "smart_load_on_soc": (("AC-Kopplung", "AC coupling"), ("Anforderung", "request")),
        "smart_load_force": (("fordert", "requests"),),
        "battery_activation": (("nicht zugesagt", "not guaranteed"),),
        "afci_enable": (("Hardware", "hardware"), ("Lichtbogenfehler löschen", "Clear arc fault")),
        "remote_run_enable": (("F19", "F19"), ("schreibt den Wert nicht", "does not write")),
        "battery_operating_basis": (
            ("0 = Spannung", "0 = voltage"),
            ("2 = keine Batterie", "2 = no battery"),
        ),
        "parallel_balancing_stop_soc_raw": (("ungeklärt", "unresolved"),),
    }
    for key, wanted in phrases.items():
        table = WRITE_HELP if key in WRITE_HELP else SENSOR_HELP
        de, en = _both(table, key)
        for p_de, p_en in wanted:
            assert p_de in de, (key, p_de)
            assert p_en in en, (key, p_en)


def test_placeholder_wording_stays_on_the_offgrid_reports_only() -> None:
    for key in ("bms_charge_voltage", "bms_discharge_voltage"):
        joined = " ".join(SENSOR_HELP[key])
        assert not re.search(r"R218|R219|placeholder|Platzhalter", joined)


def test_overclaims_of_review33_are_gone() -> None:
    for key in ("remote_run_enable",):
        assert not re.search(r"Never write|Nie auf 0", " ".join(SENSOR_HELP[key]))
    for key in ("bms_charge_current_limit", "bms_discharge_current_limit", "bms_charge_voltage"):
        assert not re.search(
            r"0 means data not provided|bedeutet nicht gelieferte", " ".join(SENSOR_HELP[key])
        )
    for key in ("bms_temperature", "battery_temperature"):
        assert "only on the parallel master" not in SENSOR_HELP[key][1]
    assert "R142 = 1 oder 2" in WRITE_HELP["pv_sale_enable"][0]
    assert "R142 = 0" not in " ".join(WRITE_HELP["pv_sale_enable"])
    assert "synchron" not in " ".join(SENSOR_HELP["it_system_raw"]).lower()


def test_sensor_view_carries_registers_for_the_popover_line() -> None:
    view = entry_view(load_profile("sg04lp3_lv_v105_8").entry("remote_run_enable"))
    assert view["registers"] == [80]
