"""Payload builders: goldens, read-only allowlist, null values, enum/binary/energy templates."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import PollConfig
from deye_monitor.ha_discovery import (
    MAX_DOCUMENT_BYTES,
    DeviceMeta,
    DiscoveryError,
    Namespace,
    app_components,
    clean_value,
    component_key,
    device_document,
    encode,
    expire_after,
    inverter_component,
    replay_document,
    state_payload,
    tombstone_document,
    validate_component,
    validate_document,
)
from deye_monitor.profiles.schema import load_profile

GOLDEN = Path(__file__).parent / "golden"
PROFILE = load_profile("sg04lp3_lv_v105_8")
NS = Namespace("ab12cd34", "homeassistant", "deye")
POLL = PollConfig()
SELECTION = (
    "battery_voltage",
    "battery_soc",
    "grid_selected_power_total",
    "operating_state",
    "alarm_fan",
    "serial_number",
    "rated_power",
    "battery_energy_charge_total",
    "pv_energy_today",
)
STATES = ("starting", "identifying", "running", "quarantined", "offline", "blocked")


def comp(key: str, series: str = "g0") -> tuple[str, dict[str, Any]]:
    return inverter_component(
        NS, "deye1", PROFILE.entry(key), series=series, poll=POLL, profile=PROFILE
    )


def inverter_doc(series: str = "g0", serial: str | None = "2401234567") -> dict[str, Any]:
    components = dict(comp(k, series) for k in SELECTION)
    meta = DeviceMeta(NS.inverter_device_id("deye1"), "Deye 1", "inverter", "deye1", serial)
    return device_document(NS, meta, components)


def golden(name: str, doc: Any) -> None:
    path = GOLDEN / name
    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if not path.exists():  # first run creates it; review the file, then commit it
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, f"{name} changed; review and update"


def test_inverter_document_golden() -> None:
    golden("discovery_inverter.json", inverter_doc())


def test_app_document_golden() -> None:
    meta = DeviceMeta(NS.app_device_id, "Deye Monitor", "app")
    golden(
        "discovery_app.json",
        device_document(NS, meta, app_components(NS, {"deye1": "Deye 1"}, STATES)),
    )


def test_tombstone_and_cleanup_replay_golden() -> None:
    key, c = comp("battery_voltage")
    meta = DeviceMeta(NS.inverter_device_id("deye1"), "Deye 1", "inverter", "deye1", None)
    golden(
        "discovery_cleanup.json",
        {
            "retained_with_tombstone": device_document(
                NS, meta, dict([comp("battery_soc")]), {key: "sensor"}
            ),
            "replay_step_2": replay_document(NS, meta, key, c),
            "tombstone_step_3": tombstone_document(NS, meta, key, "sensor"),
        },
    )


def test_namespace_is_disjoint_per_instance() -> None:
    other = Namespace("ff00ee11", "homeassistant", "deye")
    mine = {
        NS.app_status_topic,
        NS.app_state_topic,
        NS.cleanup_topic,
        NS.client_id,
        NS.discovery_topic(NS.app_device_id),
        NS.discovery_topic(NS.inverter_device_id("deye1")),
        NS.inverter_availability_topic("deye1"),
        NS.group_topic("deye1", "fast"),
    }
    theirs = {
        other.app_status_topic,
        other.app_state_topic,
        other.cleanup_topic,
        other.client_id,
        other.discovery_topic(other.app_device_id),
        other.discovery_topic(other.inverter_device_id("deye1")),
        other.inverter_availability_topic("deye1"),
        other.group_topic("deye1", "fast"),
    }
    assert not mine & theirs
    assert NS.discovery_topic("deye_ab12cd34_deye1") == (
        "homeassistant/device/deye_ab12cd34_deye1/config"
    )
    assert NS.group_topic("deye1", "ac") == "deye/ab12cd34/deye1/ac"


def test_identities_use_the_contract_pattern_and_default_entity_id() -> None:
    key, c = comp("battery_voltage")
    assert key == "battery_voltage"
    assert c["unique_id"] == "deye_ab12cd34_deye1_battery_voltage"
    assert c["default_entity_id"] == "sensor.deye_ab12cd34_deye1_battery_voltage"
    assert "object_id" not in c and "obj_id" not in c
    key, c = comp("alarm_fan")
    assert c["default_entity_id"].startswith("binary_sensor.")


def test_energy_series_is_in_key_unique_id_entity_id_and_template() -> None:
    for series in ("g0", "g1", "g7", "x7k2m9q4g0"):
        key, c = comp("battery_energy_charge_total", series)
        assert key == f"battery_energy_charge_total_{series}"
        assert c["unique_id"].endswith(f"_{series}")
        assert c["default_entity_id"].endswith(f"_{series}")
        assert f"value_json.get('energy_series') == '{series}'" in c["value_template"]
        assert c["state_class"] == "total_increasing"
        assert c["unit_of_measurement"] == "kWh"
        assert "suggested_display_precision" not in c  # depends on the effective resolution
    assert component_key(PROFILE.entry("battery_voltage"), "g5") == "battery_voltage"


@pytest.mark.parametrize("bad", ["", "g", "G1", "x7k2m9q4", "short1g0", "x7k2m9q4g0 ", "gx", "g-1"])
def test_invalid_energy_series_is_refused(bad: str) -> None:
    with pytest.raises(DiscoveryError):
        comp("battery_energy_charge_total", bad)


def test_static_values_get_no_statistics_class() -> None:
    _, c = comp("rated_power")
    assert "state_class" not in c
    assert c["entity_category"] == "diagnostic"


def test_expire_after_is_three_group_intervals() -> None:
    poll = PollConfig(fast_s=10, energy_s=61, settings_s=300, identity_s=3600)
    assert expire_after(poll, PROFILE, "ac") == 30
    assert expire_after(poll, PROFILE, "energy") == 183  # ceil(3 x 61)
    assert expire_after(PollConfig(fast_s=1.5), PROFILE, "pv") == 5
    assert comp("battery_soc")[1]["expire_after"] == 30


def test_enum_sensor_has_a_complete_option_list_and_no_unit_or_state_class() -> None:
    _, c = comp("operating_state")
    assert c["device_class"] == "enum"
    assert c["options"] == ["Standby", "Self-check", "Normal", "Alarm", "Fault", "Activating"]
    assert not {"unit_of_measurement", "state_class"} & set(c)


def test_numeric_template_has_no_default_and_no_arithmetic() -> None:
    _, c = comp("battery_voltage")
    template = c["value_template"]
    assert "default" not in template and "float" not in template and "*" not in template
    assert template == "{{ value_json.get('battery_voltage') if value_json is mapping else none }}"


def test_binary_template_is_strict() -> None:
    _, c = comp("alarm_fan")
    assert "sameas true" in c["value_template"] and "sameas false" in c["value_template"]
    assert (c["payload_on"], c["payload_off"]) == ("ON", "OFF")
    assert "state_class" not in c and "unit_of_measurement" not in c


def test_documents_never_contain_command_fields_or_other_platforms() -> None:
    doc = inverter_doc()
    fields = {k for c in doc["components"].values() for k in c} | set(doc) | set(doc["device"])
    for name in fields:
        assert "command" not in name and "cmd" not in name and not name.startswith("set")
        assert "payload_press" not in name and "brightness" not in name
    assert {c["platform"] for c in doc["components"].values()} <= {"sensor", "binary_sensor"}
    # long keys only: none of the short forms
    for short in ("dev", "cmps", "uniq_id", "stat_t", "def_ent_id", "avty", "o"):
        assert short not in doc and short not in doc["components"]["battery_soc"]


def _bad(mutate: Any) -> dict[str, Any]:
    doc = copy.deepcopy(inverter_doc())
    mutate(doc)
    return doc


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["components"]["battery_soc"].update(command_topic="x/set"),
        lambda d: d["components"]["battery_soc"].update(cmd_t="x/set"),
        lambda d: d["components"]["battery_soc"].update(command_template="{{ x }}"),
        lambda d: d["components"]["alarm_fan"].update(
            json_attributes_topic="x"
        ),  # binary sensors: no attributes
        lambda d: d["components"]["battery_soc"].update(object_id="x"),
        lambda d: d["components"]["battery_soc"].update(obj_id="x"),
        lambda d: d["components"]["battery_soc"].update(unique_id=None),
        lambda d: d["components"]["battery_soc"].update(platform="switch"),
        lambda d: d["components"]["battery_soc"].update(platform="number"),
        lambda d: d["components"]["battery_soc"].update(availability=[{"topic": "x"}]),
        lambda d: d["components"]["battery_soc"].update(entity_category="config"),
        lambda d: d["components"]["alarm_fan"].update(unit_of_measurement="V"),
        lambda d: d["components"]["battery_soc"].update(payload_on="ON"),
        lambda d: d["components"]["operating_state"].update(options=[]),
        lambda d: d["components"]["operating_state"].update(state_class="measurement"),
        lambda d: d.update(command_topic="x"),
        lambda d: d.update(cmps={}),
        lambda d: d["device"].update(connections=[["mac", "x"]]),
        lambda d: d["origin"].update(url="x"),
        lambda d: d.update(availability_mode="any"),
        lambda d: d.update(components={}),
        lambda d: d["availability"].append({"topic": "x", "value_template": "y"}),
        lambda d: d["components"].update(bad={"platform": "sensor", "command_topic": "x"}),
    ],
)
def test_allowlist_rejects(mutate: Any) -> None:
    with pytest.raises(DiscoveryError):
        validate_document(_bad(mutate))


def test_tombstone_is_platform_only_and_alias_free() -> None:
    validate_component("gone", {"platform": "sensor"})
    with pytest.raises(DiscoveryError):
        validate_component("gone", {"platform": "switch"})
    with pytest.raises(DiscoveryError):
        validate_component("gone", {"platform": "sensor", "unique_id": "x"})  # incomplete


def test_replay_never_carries_a_measurement_template_or_unknown_fields() -> None:
    key, c = comp("battery_voltage")
    meta = DeviceMeta(NS.inverter_device_id("deye1"), "Deye 1", "inverter", "deye1", None)
    c = {**c, "command_topic": "evil", "value_template": "{{ 42 }}"}
    doc = replay_document(NS, meta, key, c)
    assert list(doc["components"]) == [key]
    only = doc["components"][key]
    assert only["value_template"] == "{{ none }}" and "command_topic" not in only
    assert only["unique_id"] == c["unique_id"]
    assert {"topic": NS.cleanup_topic} in doc["availability"]
    assert doc["availability_mode"] == "all"
    assert {"topic": NS.app_status_topic} in doc["availability"]
    assert {"topic": NS.inverter_availability_topic("deye1")} in doc["availability"]


def test_inverter_document_availability_is_app_and_inverter_with_mode_all() -> None:
    doc = inverter_doc()
    assert doc["availability"] == [
        {"topic": "deye/ab12cd34/status"},
        {"topic": "deye/ab12cd34/deye1/availability"},
    ]
    assert doc["availability_mode"] == "all"
    assert doc["device"]["via_device"] == "deye_ab12cd34_app"
    assert doc["device"]["identifiers"] == ["deye_ab12cd34_deye1"]
    assert doc["device"]["serial_number"] == "2401234567"
    assert doc["origin"]["name"] and doc["origin"]["sw_version"]


def test_app_device_has_only_app_availability_and_no_via_device() -> None:
    meta = DeviceMeta(NS.app_device_id, "Deye Monitor", "app")
    doc = device_document(NS, meta, app_components(NS, {}, STATES))
    assert doc["availability"] == [{"topic": "deye/ab12cd34/status"}]
    assert "via_device" not in doc["device"]
    assert all(c["entity_category"] == "diagnostic" for c in doc["components"].values())


def test_active_and_retired_key_conflict_is_refused() -> None:
    key, c = comp("battery_voltage")
    meta = DeviceMeta(NS.inverter_device_id("deye1"), "Deye 1", "inverter", "deye1", None)
    with pytest.raises(DiscoveryError):
        device_document(NS, meta, {key: c}, {key: "sensor"})


def test_document_size_limit_is_enforced_without_truncation() -> None:
    doc = inverter_doc()
    doc["device"]["name"] = "x" * (MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(DiscoveryError, match="limit"):
        validate_document(doc)


def test_encode_rejects_nan_and_is_deterministic() -> None:
    with pytest.raises(DiscoveryError):
        encode({"a": math.nan})
    assert encode({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert encode(inverter_doc()) == encode(inverter_doc())


# -- state payload ---------------------------------------------------------------------------------


def payload(values: dict[str, Any], series: str = "g0") -> dict[str, Any]:
    entries = {k: PROFILE.entry(k) for k in values}
    raw, _ = state_payload(
        cycle_id=7,
        wall_ts=1_700_000_000.0,
        energy_generation=0,
        energy_series=series,
        values=values,
        entries=entries,
    )
    result: dict[str, Any] = json.loads(raw)
    return result


def test_state_payload_golden() -> None:
    golden(
        "state_status.json",
        payload(
            {
                "operating_state": "normal",
                "alarm_fan": False,
                "alarm_word_1": 0,
                "battery_soc": None,
            }
        ),
    )


def test_missing_values_are_json_null_and_real_zero_stays_zero() -> None:
    body = payload({"battery_soc": None, "battery_voltage": 0, "grid_selected_power_total": 0.0})
    assert body["battery_soc"] is None
    assert body["battery_voltage"] == 0 and body["battery_voltage"] is not None
    assert body["grid_selected_power_total"] == 0.0
    assert body["cycle_id"] == 7
    assert body["timestamp"] == "2023-11-14T22:13:20Z"
    assert body["energy_generation"] == 0


def test_unknown_enum_and_nan_become_null() -> None:
    assert clean_value(PROFILE.entry("operating_state"), "unknown_99") == (None, True)
    assert clean_value(PROFILE.entry("operating_state"), "normal") == ("Normal", False)
    assert clean_value(PROFILE.entry("battery_voltage"), math.nan) == (None, True)
    assert clean_value(PROFILE.entry("battery_voltage"), math.inf) == (None, True)
    assert clean_value(PROFILE.entry("battery_voltage"), True) == (None, True)


def test_binary_values_must_be_real_booleans() -> None:
    entry = PROFILE.entry("alarm_fan")
    assert clean_value(entry, True) == (True, False)
    assert clean_value(entry, False) == (False, False)
    assert clean_value(entry, None) == (None, False)
    assert clean_value(entry, 1) == (None, True)
    assert clean_value(entry, "false") == (None, True)


def test_state_payload_counts_replaced_values_and_ignores_unselected_keys() -> None:
    entries = {"operating_state": PROFILE.entry("operating_state")}
    raw, replaced = state_payload(
        cycle_id=1,
        wall_ts=0.0,
        energy_generation=2,
        energy_series="g2",
        values={"operating_state": "unknown_99", "not_selected": 5},
        entries=entries,
    )
    body = json.loads(raw)
    assert replaced == 1 and body["operating_state"] is None and "not_selected" not in body
