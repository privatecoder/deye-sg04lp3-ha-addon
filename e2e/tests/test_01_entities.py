"""Entities, identities, metadata, values and the absence of anything writable."""

from __future__ import annotations

import json
import re

import pytest

from conftest import INSTANCE, SERIALS
from harness.devices import EXPECTED
from tests.helpers import INV, inverter_states, ours, uid

pytestmark = pytest.mark.usefixtures("running")

COMMAND_FIELDS = re.compile(
    r"(command|^cmd_|_cmd_|^set_|_set_topic|payload_(press|open|close|stop|lock|unlock|install|reset|turn_on|turn_off))"
)


async def test_registry_ids_domains_and_metadata(running):
    entries = await ours(running.ha, INSTANCE)
    assert entries, "no MQTT entities of the app in the registry"
    for unique_id, e in entries.items():
        assert e["entity_id"] == f"{e['entity_id'].split('.')[0]}.{unique_id}", (
            unique_id,
            e["entity_id"],
        )
        assert e["entity_id"].split(".")[0] in ("sensor", "binary_sensor"), e["entity_id"]
    for inverter in INV:
        assert uid(INSTANCE, inverter, "battery_soc") in entries
        # energy entities carry the generation from the first publish on
        for key in ("battery_energy_charge_total", "pv_energy_today", "grid_energy_import_total"):
            assert uid(INSTANCE, inverter, f"{key}_g0") in entries, (inverter, key)
            assert uid(INSTANCE, inverter, key) not in entries
    # no automatic "_2": every entity_id is exactly domain + unique_id (asserted above)
    # diagnostics are categorised, measurements are not
    assert entries[uid(INSTANCE, "deye_tcp", "serial_number")]["entity_category"] == "diagnostic"
    assert entries[uid(INSTANCE, "deye_tcp", "fault_word_1")]["entity_category"] == "diagnostic"
    assert entries[uid(INSTANCE, "deye_tcp", "battery_soc")]["entity_category"] is None


async def test_units_device_and_state_classes(running):
    states = await running.ha.states()
    expect = {
        "battery_soc": ("%", "battery", "measurement"),
        "battery_voltage": ("V", "voltage", "measurement"),
        "battery_current": ("A", "current", "measurement"),
        "battery_temperature": ("°C", "temperature", "measurement"),
        "grid_frequency": ("Hz", "frequency", "measurement"),
        "pv1_power": ("W", "power", "measurement"),
        "battery_energy_charge_total_g0": ("kWh", "energy", "total_increasing"),
        "pv_energy_today_g0": ("kWh", "energy", "total_increasing"),
    }
    for inverter in INV:
        for key, (unit, dclass, sclass) in expect.items():
            attrs = states[f"sensor.{uid(INSTANCE, inverter, key)}"]["attributes"]
            assert (
                attrs.get("unit_of_measurement"),
                attrs.get("device_class"),
                attrs.get("state_class"),
            ) == (unit, dclass, sclass), (inverter, key, attrs)


async def test_values_match_the_fake_registers(running):
    states = await running.ha.states()
    for inverter in INV:
        got = inverter_states(states, INSTANCE, inverter)
        for key, want in EXPECTED.items():
            assert float(got[key]) == pytest.approx(want), (inverter, key, got[key])
        assert got["serial_number"] == SERIALS[inverter]
        # u32 pairs with the resolution of R65=0x0104 (0.1 kWh): 34567 -> 3456.7
        assert float(got["battery_energy_charge_total_g0"]) == pytest.approx(3456.7)
        assert float(got["pv_energy_total_g0"]) == pytest.approx(12345.6)
        assert float(got["battery_energy_charge_today_g0"]) == pytest.approx(12.3)
        assert got["fault_word_1"] == "1"  # raw fault word, bit 0 set
        assert got["operating_state"] == "Normal"
        assert got["parallel_phase"] == "A"


async def test_only_sensor_and_binary_sensor_platforms_and_no_command_fields(running):
    """Everything the app retains at the broker, checked recursively: no command field or alias."""
    docs = await running.broker.retained("homeassistant/#")
    ours_docs = {t: json.loads(p) for t, p in docs.items() if f"_{INSTANCE}" in t or INSTANCE in t}
    assert ours_docs, "no retained discovery documents of the app"
    offenders: list[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if COMMAND_FIELDS.search(str(k)):
                    offenders.append(f"{path}/{k}")
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and re.search(r"/(set|command|cmd)(/|$)", node):
            offenders.append(f"{path}={node}")

    for topic, doc in ours_docs.items():
        assert topic.startswith(
            ("homeassistant/device/", "homeassistant/sensor/", "homeassistant/binary_sensor/")
        ), topic
        for key, comp in doc.get("components", {}).items():
            assert comp.get("platform") in ("sensor", "binary_sensor"), (
                topic,
                key,
                comp.get("platform"),
            )
        walk(doc, topic)
    assert offenders == []


async def test_the_app_subscribes_to_nothing_but_the_birth_topic(running):
    """Broker side: every SUBSCRIBE seen in the broker log of the app's client is the HA birth topic."""
    logs = running.broker.container.logs(5000)
    clients = set(re.findall(r"as (deye[\w\-]*)", logs))
    assert clients, "the app's MQTT client was not seen by the broker"
    subs = re.findall(r"^\d+: (deye[\w\-]*) \d (\S+)", logs, flags=re.M)
    topics = {t for c, t in subs}
    assert subs, "no SUBSCRIBE of the app seen in the broker log"
    assert topics <= {"homeassistant/status"}, topics
