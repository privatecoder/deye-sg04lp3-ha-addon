"""The app's discovery goldens against the real Home Assistant 2026.10.4 MQTT schemas.

Prüfablauf contract §8.1: strict own allowlist (done by the app suite, which writes the goldens) ->
DEVICE_DISCOVERY_SCHEMA -> per active component: platform removed, shared options and
device/origin injected -> platform schema -> nothing silently stripped -> the templates rendered
by the real renderer and the real MQTT entity handlers inside a HA event loop.
Tombstones (platform-only) are routed to cleanup and not validated as active sensors.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import json
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.mqtt.binary_sensor import (
    DISCOVERY_SCHEMA as BINARY_SCHEMA,
)
from homeassistant.components.mqtt.binary_sensor import MqttBinarySensor
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.mqtt.schemas import DEVICE_DISCOVERY_SCHEMA, SHARED_OPTIONS
from homeassistant.components.mqtt.sensor import DISCOVERY_SCHEMA as SENSOR_SCHEMA
from homeassistant.components.mqtt.sensor import MqttSensor
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

GOLDEN = Path(__file__).resolve().parents[2] / "deye_monitor" / "tests" / "golden"
DOCS = ("discovery_inverter.json", "discovery_app.json", "discovery_app_plant.json")


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((GOLDEN / name).read_text(encoding="utf-8"))
    return data


def run_in_ha(test: Callable[[HomeAssistant], Awaitable[None]]) -> None:
    """HA validates templates only inside an event loop with a HomeAssistant instance."""

    async def main() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hass = HomeAssistant(tmp)
            try:
                await test(hass)
            finally:
                await hass.async_stop()

    asyncio.run(main())


def effective(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Root schema, then the platform schema of every active component (as HA does)."""
    DEVICE_DISCOVERY_SCHEMA(copy.deepcopy(payload))
    out: dict[str, dict[str, Any]] = {}
    for key, original in payload["components"].items():
        config = copy.deepcopy(original)
        platform = config.pop("platform")
        if not config:
            continue  # platform-only tombstone: a removal, not an entity
        config["device"] = payload["device"]
        config["origin"] = payload["origin"]
        for field in SHARED_OPTIONS:
            if field in payload:
                config.setdefault(field, payload[field])
        schema = SENSOR_SCHEMA if platform == "sensor" else BINARY_SCHEMA
        result = dict(schema(config))
        stripped = set(config) - set(result)
        assert not stripped, f"{key}: schema silently removed {sorted(stripped)}"
        out[key] = result
    return out


def test_pinned_versions() -> None:
    assert importlib.metadata.version("homeassistant") == "2026.10.4"
    assert importlib.metadata.version("paho-mqtt") == "2.1.0"


@pytest.mark.parametrize("name", DOCS)
def test_goldens_pass_root_and_platform_schemas(name: str) -> None:
    payload = load(name)

    async def check(_hass: HomeAssistant) -> None:
        comps = effective(payload)
        assert comps and set(comps) == set(payload["components"])
        for key, comp in comps.items():
            assert comp["unique_id"] and comp["state_topic"], key

    run_in_ha(check)


def test_tombstone_and_cleanup_documents_pass_the_root_schema() -> None:
    cleanup = load("discovery_cleanup.json")

    async def check(_hass: HomeAssistant) -> None:
        for name, doc in cleanup.items():
            effective(doc)  # tombstones are skipped, the replayed component is validated
            assert doc["availability_mode"] == "all", name
        retained = cleanup["retained_with_tombstone"]["components"]
        assert any(c == {"platform": "sensor"} for c in retained.values())

    run_in_ha(check)


def test_unknown_or_command_fields_would_be_stripped_or_rejected() -> None:
    """Why the app's own allowlist exists: HA does not enforce read-only on its own."""
    payload = load("discovery_inverter.json")
    payload["components"]["battery_soc"]["command_topic"] = "evil/set"

    async def check(_hass: HomeAssistant) -> None:
        with pytest.raises(AssertionError, match="silently removed"):
            effective(payload)

    run_in_ha(check)


def render(hass: HomeAssistant, template: str, data: Any) -> str:
    return str(Template(template, hass).async_render_with_possible_json_value(json.dumps(data)))


def test_templates_render_null_zero_value_and_guards() -> None:
    payload = load("discovery_inverter.json")
    comps = payload["components"]
    energy_key = next(k for k in comps if k.startswith("battery_energy_charge_total_"))
    series = energy_key.rsplit("_", 1)[1]

    async def check(hass: HomeAssistant) -> None:
        for key, comp in comps.items():
            tpl = comp["value_template"]
            base = key if not key.endswith(f"_{series}") else key[: -len(series) - 1]
            base = base.removeprefix("diag_")  # the inverter's own diagnostics read the bare name
            if comp["platform"] == "binary_sensor":
                for value, expected in [(True, "ON"), (False, "OFF"), (None, "None"),
                                        ("false", "None"), (0, "None")]:  # fmt: skip
                    assert render(hass, tpl, {base: value}) == expected, (key, value)
                continue
            guard = {"energy_series": series} if key.endswith(f"_{series}") else {}
            assert render(hass, tpl, {**guard, base: None}) == "None", key  # null -> unknown
            assert render(hass, tpl, guard) == "None", key  # missing key -> unknown
            assert render(hass, tpl, {**guard, base: 0}) == "0", key  # a real zero stays zero
        tpl = comps[energy_key]["value_template"]
        assert render(hass, tpl, {"energy_series": series, "battery_energy_charge_total": 5}) == "5"
        for other in ("g99", "x7k2m9q4g0", "", None):  # another series never reaches this counter
            data = {"energy_series": other, "battery_energy_charge_total": 5}
            assert render(hass, tpl, data) == "None", other
        assert render(hass, tpl, {"battery_energy_charge_total": 5}) == "None"
        assert render(hass, tpl, "not a mapping") == "None"

    run_in_ha(check)


def message(data: Any) -> ReceiveMessage:
    return ReceiveMessage(
        topic="t",
        subscribed_topic="t",
        timestamp=datetime.now(UTC),
        payload=json.dumps(data),
        qos=1,
        retain=False,
    )


def make_sensor(hass: HomeAssistant, comp: dict[str, Any]) -> MqttSensor:
    config = copy.deepcopy(comp)
    config.pop("platform")
    obj = object.__new__(MqttSensor)
    obj.hass = hass
    obj.entity_id = comp["default_entity_id"]
    obj._attr_name = comp["name"]
    obj._config = SENSOR_SCHEMA(config)
    for value in obj._config.values():
        if isinstance(value, Template):
            value.hass = hass
    obj._setup_from_config(obj._config)
    return obj


def test_real_sensor_handlers_for_numbers_enums_and_energy() -> None:
    comps = load("discovery_inverter.json")["components"]

    async def check(hass: HomeAssistant) -> None:
        volt = make_sensor(hass, comps["battery_voltage"])
        volt._update_state(message({"battery_voltage": 52.1}))
        assert volt.native_value == "52.1"
        volt._update_state(message({"battery_voltage": None}))
        assert volt.native_value is None  # null -> unknown, not the old value
        volt._update_state(message({"battery_voltage": 0}))
        assert volt.native_value == "0"
        enum = make_sensor(hass, comps["operating_state"])
        enum._update_state(message({"operating_state": "Normal"}))
        assert enum.native_value == "Normal"
        enum._update_state(message({"operating_state": None}))  # the publisher maps unknown_99
        assert enum.native_value is None  # to null, so the old state cannot stay
        energy_key = next(k for k in comps if k.startswith("battery_energy_charge_total_"))
        series = energy_key.rsplit("_", 1)[1]
        energy = make_sensor(hass, comps[energy_key])
        energy._update_state(message({"energy_series": series, "battery_energy_charge_total": 7}))
        assert energy.native_value == "7"
        energy._update_state(message({"energy_series": "g42", "battery_energy_charge_total": 700}))
        assert energy.native_value is None  # an old counter sees a new series only as unknown

    run_in_ha(check)


def test_real_binary_sensor_handler() -> None:
    comps = load("discovery_inverter.json")["components"]

    async def check(hass: HomeAssistant) -> None:
        config = copy.deepcopy(comps["alarm_fan"])
        config.pop("platform")
        obj = object.__new__(MqttBinarySensor)
        obj.hass = hass
        obj.entity_id = config["default_entity_id"]
        obj._attr_name = config["name"]
        obj._config = BINARY_SCHEMA(config)
        obj._config["value_template"].hass = hass
        obj._setup_from_config(obj._config)
        for value, expected in [(True, True), (False, False), (None, None)]:
            obj._state_message_received(message({"alarm_fan": value}))
            assert obj.is_on is expected, value
        obj._state_message_received(message({"alarm_fan": True}))
        obj._state_message_received(message({"alarm_fan": "false"}))  # never guessed as on/off
        assert obj.is_on is None  # the template renders None -> unknown

    run_in_ha(check)


def test_inverter_diagnostics_render_numbers_text_timestamps_and_conflict_attributes() -> None:
    comps = load("discovery_inverter.json")["components"]
    payload = {
        "r65_raw": 260,
        "energy_resolution": 0.1,
        "energy_series": "x7k2m9q4g0",
        "capability_conflicts": 1,
        "transport_timeouts": 3,
        "last_success": "2023-11-14T22:13:20Z",
        "capability_conflict_list": [{"code": "mppt_count", "detail": "R22 = 1"}],
        "serial": None,
        "state": "running",
        "firmware_mcu1": "1001-C070",
        "firmware_mcu2": "2005-1175-1807",
    }

    async def check(hass: HomeAssistant) -> None:
        for key, value in payload.items():
            if key.endswith("conflict_list"):
                continue
            comp = comps[f"diag_{key}"]
            assert comp["availability"] == [{"topic": load("discovery_inverter.json")["availability"][0]["topic"]}]
            obj = make_sensor(hass, comp)
            obj._update_state(message(payload))
            expected = None if value is None else str(value)
            if comp.get("device_class") == "timestamp":
                assert obj.native_value is not None, key  # HA parses the ISO timestamp
            else:
                assert obj.native_value == expected, key
        conflicts = comps["diag_capability_conflicts"]
        tpl = Template(conflicts["json_attributes_template"], hass)
        rendered = json.loads(str(tpl.async_render_with_possible_json_value(json.dumps(payload))))
        assert rendered == {"conflicts": [{"code": "mppt_count", "detail": "R22 = 1"}]}
        empty = tpl.async_render_with_possible_json_value(json.dumps({}))
        assert json.loads(str(empty)) == {"conflicts": None}

    run_in_ha(check)


def test_the_app_device_has_only_the_app_wide_entities() -> None:
    comps = load("discovery_app.json")["components"]
    assert set(comps) == {"app_version", "mqtt_reconnects", "inverters_online", "inverters_configured"}


def test_plant_sensors_render_values_and_guard_the_energy_series() -> None:
    effective(load("discovery_app_plant.json"))  # the root and platform schemas accept all
    comps = load("discovery_app_plant.json")["components"]
    body = {
        "energy_series": "g0",
        "plant_pv_power": 5200.0,
        "plant_load_energy_total": 1234.5,
        "plant_battery_soc": 61,
        "plant_members": 4,
        "plant_members_list": ["a", "b", "c", "d"],
        "plant_master": "a",
    }

    async def check(hass: HomeAssistant) -> None:
        power = make_sensor(hass, comps["plant_pv_power"])
        power._update_state(message(body))
        assert power.native_value == "5200.0"
        energy = make_sensor(hass, comps["plant_load_energy_total_g0"])
        energy._update_state(message(body))
        assert energy.native_value == "1234.5"
        energy._update_state(message({**body, "energy_series": "g1"}))  # another series: no value
        assert energy.native_value is None
        soc = make_sensor(hass, comps["plant_battery_soc"])
        soc._update_state(message({**body, "plant_battery_soc": None}))
        assert soc.native_value is None  # an unknown total stays unknown
        members = make_sensor(hass, comps["plant_members"])
        members._update_state(message(body))
        tpl = Template(comps["plant_members"]["json_attributes_template"], hass)
        rendered = json.loads(str(tpl.async_render_with_possible_json_value(json.dumps(body))))
        assert rendered == {"members": ["a", "b", "c", "d"]}

    run_in_ha(check)
