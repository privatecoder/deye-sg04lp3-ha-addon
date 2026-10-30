"""The packaged frontend (T11): texts, profile descriptions and the CSP-relevant source rules.

These tests need no browser; the end-to-end behaviour is covered by ``test_ui.py``."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import pytest

from deye_monitor.device import CONFLICT_POINTS, InverterState
from deye_monitor.profiles.schema import load_profile
from deye_monitor.webapi import catalog_view

SRC = Path(__file__).resolve().parents[1] / "src" / "deye_monitor"
STATIC = SRC / "web" / "static"
PROFILE = load_profile("sg04lp3_lv_v105_8")
JS_FILES = sorted(STATIC.glob("*.js"))
_MESSAGE = re.compile(r"^  '([a-z0-9_.]+)': (['\"])(.*)\2,$", re.MULTILINE)


def messages() -> dict[str, dict[str, str]]:
    text = (STATIC / "i18n.js").read_text(encoding="utf-8")
    de_part, rest = text.split("const en = {", 1)
    en_part = rest.split("export const MESSAGES", 1)[0]
    return {
        "de": {m[0]: m[2] for m in _MESSAGE.findall(de_part.split("const de = {", 1)[1])},
        "en": {m[0]: m[2] for m in _MESSAGE.findall(en_part)},
    }


# -- profile descriptions -------------------------------------------------------------------------


def test_every_selectable_entry_has_a_plain_description() -> None:
    for entry in PROFILE.entries:
        if not entry.selectable:
            continue
        assert entry.description is not None, entry.key
        for text in (entry.description.de, entry.description.en):
            assert 15 <= len(text) <= 190, (entry.key, text)
            assert text.endswith((".", "”")), (entry.key, text)
            assert not re.search(r"\bR\d{2,3}\b|0x[0-9a-fA-F]+", text), (entry.key, text)
        assert entry.description.de != entry.description.en, entry.key


def test_descriptions_name_the_measurement_point_of_grid_and_load_values() -> None:
    grid = PROFILE.entry("grid_voltage_a").description
    load = PROFILE.entry("load_voltage_a").description
    assert grid is not None and "Netzanschluss" in grid.de and "grid connection" in grid.en
    assert load is not None and "Lastausgang" in load.de and "load output" in load.en


def test_catalog_carries_description_and_hint_keys() -> None:
    entries = {e["key"]: e for e in catalog_view(PROFILE)["entries"]}
    assert entries["battery_soc"]["description"]["de"].startswith("Ladestand")
    assert entries["battery_soc"]["hint"] is None
    assert entries["battery_voltage"]["hint"] == "profile_dependent"
    assert entries["pv_ext_r722"]["hint"] == "unresolved"
    assert all(e["description"] for e in entries.values())


# -- texts --------------------------------------------------------------------------------------


def test_german_and_english_texts_have_the_same_keys_and_placeholders() -> None:
    texts = messages()
    assert set(texts["de"]) == set(texts["en"])
    assert len(texts["de"]) > 300
    for key, de in texts["de"].items():
        assert re.findall(r"\{(\w+)\}", de) == re.findall(r"\{(\w+)\}", texts["en"][key]) or (
            sorted(re.findall(r"\{(\w+)\}", de))
            == sorted(re.findall(r"\{(\w+)\}", texts["en"][key]))
        ), key


def used_literal_keys() -> set[str]:
    keys: set[str] = set()
    for path in JS_FILES:
        if path.name == "i18n.js":
            continue
        source = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r"\bt\('([a-z0-9_.]+)'", source))
        keys |= set(re.findall(r"\btOr\('[a-z0-9_.]+', '([a-z0-9_.]+)'", source))
    return keys


def test_every_text_key_used_by_the_ui_exists() -> None:
    texts = messages()
    missing = sorted(k for k in used_literal_keys() if k not in texts["de"])
    assert missing == []


def test_dynamic_key_families_are_complete() -> None:
    de = messages()["de"]
    expected: set[str] = set()
    expected |= {f"state.{s.value}" for s in InverterState}
    expected |= {f"reason.generic.{s.value}" for s in InverterState}
    expected |= {f"path.{p}" for p in ("primary", "fallback")}
    expected |= {
        f"transport.{t}" for t in ("modbus_tcp", "rtu_over_tcp", "serial_rtu", "solarman_v5_tcp")
    }
    expected |= {f"point.{p}" for p in profile_points()}
    expected |= {
        f"ep.t.{t}" for t in ("modbus_tcp", "rtu_over_tcp", "solarman_v5_tcp", "serial_rtu")
    }
    expected |= {
        f"ep.t.{t}_when" for t in ("modbus_tcp", "rtu_over_tcp", "solarman_v5_tcp", "serial_rtu")
    }
    expected |= {
        f"test.outcome.{o}"
        for o in (
            "ok",
            "unreachable",
            "timeout",
            "device_refused",
            "logger_error",
            "bad_frame",
            "bus_busy",
            "transport_error",
            "invalid_serial",
            "internal_error",
        )
    }
    expected |= {
        f"test.hint.{o}"
        for o in (
            "unreachable",
            "timeout",
            "device_refused",
            "logger_error",
            "bad_frame",
            "bus_busy",
            "transport_error",
            "invalid_serial",
            "internal_error",
        )
    }
    expected |= {
        f"test.hint.timeout.{t}" for t in ("modbus_tcp", "rtu_over_tcp", "solarman_v5_tcp")
    }
    expected |= {"test.hint.unreachable.serial_rtu"}
    expected |= {f"plant.notice.{c}" for c in ("plant_migrated", "plant_options_present")}
    expected |= {"plant.notice.plant_options_differ"}
    expected |= {f"load.{x}" for x in ("low", "mid", "high", "over", "unknown")}
    expected |= {f"hint.conflict.{c}" for c in CONFLICT_POINTS}
    expected |= {
        f"hint.obs.{s}"
        for s in (
            "zero_only_inconclusive",
            "known_placeholder",
            "read_failed",
            "capability_conflict",
        )
    }
    expected |= {f"sensor.hint.{h}" for h in ("profile_dependent", "unresolved")}
    expected |= {
        f"maint.reset_{k}_{x}"
        for k in ("energy", "identity")
        for x in ("title", "warning", "effect")
    }
    expected |= {
        "feat.value.battery.true",
        "feat.value.battery.false",
        "feat.value.parallel.true",
        "feat.value.parallel.false",
    }
    expected |= {
        f"feat.value.gen_port_mode.{v}" for v in ("unused", "generator", "smart_load", "ac_coupled")
    }
    expected |= {f"feat.value.grid_port_use.{v}" for v in ("utility", "generator", "unused")}
    expected |= {
        f"feat.gen.{v}{s}"
        for v in ("unused", "generator", "smart_load", "ac_coupled")
        for s in ("", "_help")
    }
    expected |= {
        f"feat.{n}.{v}{s}"
        for n in ("battery", "bms", "parallel")
        for v in ("yes", "no")
        for s in ("", "_help")
    }
    assert sorted(expected - set(de)) == []


def profile_points() -> set[str]:
    return {e.point for e in PROFILE.entries}


def test_every_api_error_and_store_problem_key_has_a_text() -> None:
    de = messages()["de"]
    error_keys: set[str] = set()
    problem_codes: set[str] = set()
    plant_codes: set[str] = set()
    for path in SRC.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        error_keys |= set(re.findall(r'ChangeError\(\s*"([a-z_]+)"', source))
        error_keys |= set(re.findall(r'else "([a-z_]+)",\s*\n\s*500', source))
        problem_codes |= set(re.findall(r'(?<![A-Za-z])Problem\(\s*"([a-z_]+)"', source))
        plant_codes |= set(re.findall(r'PlantProblem\(\s*(?:f?)"([a-z_]+)"', source))
    plant_codes |= {"duplicate_endpoint", "duplicate_inverter"}  # built from the kind name
    error_keys |= {"forbidden_peer", "internal_error", "network_error", "http_404", "http_405"}
    error_keys |= {"http_413", "http_415", "http_428"}
    assert sorted(k for k in error_keys if f"error.{k}" not in de) == []
    assert sorted(c for c in problem_codes if f"problem.{c}" not in de) == []
    assert sorted(c for c in plant_codes if f"plantproblem.{c}" not in de) == []
    assert "error.plant_recovery_failed" in de


# -- source rules (CSP, no build chain, no CDN) ----------------------------------------------


def test_index_has_no_inline_script_style_or_absolute_urls() -> None:
    html = (SRC / "web" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert " style=" not in html and "<style" not in html
    assert not re.search(r"""(?:src|href)=["'](?:/|https?:)""", html.replace('href="data:,"', ""))
    assert "onclick" not in html.lower()


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_javascript_sources_obey_the_content_security_policy(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "new Function",
        "document.write",
    ):
        assert forbidden not in source, forbidden
    assert not re.search(r"""setAttribute\(\s*['"]style['"]""", source)
    assert "style=" not in source
    assert not re.search(r"""(?:fetch|import)\(\s*['"`]/""", source)  # no absolute paths
    assert not re.search(r"https?://", source.replace("http://www.w3.org", ""))


def test_module_imports_resolve_inside_the_package_and_the_css_has_no_remote_urls() -> None:
    for path in JS_FILES:
        for target in re.findall(r"from '\./([A-Za-z0-9_.-]+)'", path.read_text(encoding="utf-8")):
            assert (STATIC / target).is_file(), (path.name, target)
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css and not re.search(r"url\(\s*['\"]?(?:https?:|//)", css)


def test_frontend_files_are_package_data() -> None:
    root = resources.files("deye_monitor.web")
    assert (root / "index.html").is_file()
    for path in JS_FILES:
        assert (root / "static" / path.name).is_file()


# -- description honesty (R7-05..07): measurement points, settings vs. facts, raw fields ----


def _desc(key: str) -> tuple[str, str]:
    description = PROFILE.entry(key).description
    assert description is not None, key
    return description.de, description.en


def test_load_values_are_reported_plant_values_separate_from_ups_eps() -> None:
    for key in ("load_power_a", "load_power_b", "load_power_c", "load_power_total"):
        de, en = _desc(key)
        assert "getrennt von der Leistung am UPS/EPS-Ausgang" in de, key
        assert "separate from UPS/EPS output power" in en, key
        assert "Lastausgang" not in de and "load output" not in en, key
    for key in ("load_energy_today", "load_energy_total"):
        de, en = _desc(key)
        assert de.startswith("Vom Wechselrichter gemeldete Lastenergie"), key
        assert en.startswith("Load energy reported by the inverter"), key
        assert "Lastausgang" not in de and "load output" not in en, key


def test_load_frequency_is_a_duplicate_of_the_inverter_frequency() -> None:
    de, en = _desc("load_frequency")
    assert "Duplizierter Wert der Inverterfrequenz" in de and "keine unabhängige Messung" in de
    assert "Duplicate inverter frequency" in en and "not an independent" in en


def test_reported_settings_are_not_described_as_physical_facts() -> None:
    de, en = _desc("modbus_address")
    assert "gemeldetes Modbus-Adressfeld" in de and "je Verbindungsweg" in de
    assert "reported by the device" in en and "each connection path" in en
    assert "hört" not in de and "answers to" not in en
    de, en = _desc("gen_on_grid_input")
    assert "Einstellung für Generatorbetrieb" in de and "weder einen angeschlossenen noch" in de
    assert "Setting for generator use" in en and "does not confirm a connected or running" in en


def test_arc_extension_raw_values_are_not_extra_pv_channels() -> None:
    keys = [f"pv_ext_r{n}" for n in range(737, 749)]
    for key in keys:
        de, en = _desc(key)
        assert "Erweiterungs-/Lichtbogen-Diagnoserohwert" in de, key
        assert "kein Nachweis eines zusätzlichen PV-Eingangs" in de, key
        assert "extension/arc-diagnostic raw value" in en, key
        assert "PV-Kanals" not in de and "PV channel" not in en, key


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_javascript_sources_parse_as_modules(path: Path) -> None:
    """A syntax error in one module blanks the whole page: catch it without a browser."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [node, "--check", "--input-type=module"],
        input=path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
