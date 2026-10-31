"""Discovery of fields the device does not report: removal, restoration, profile sources."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.fakes.mqtt import settle
from tests.test_mqtt_publisher import NS, make, running, snap, state_topic


async def test_unreported_entities_are_removed_and_restored_with_the_same_ids(
    tmp_path: Path,
) -> None:
    keys = (
        "device_type",
        "dsp_protocol_version",
        "mppt_count",
        "phase_count",
        "battery_input_code",
        "battery_soc",
        "operating_state",
    )
    topic = NS.discovery_topic(NS.inverter_device_id("deye1"))
    app = NS.discovery_topic(NS.app_device_id)
    async with make(tmp_path, keys=keys) as r:
        c = await r.ready()
        running(r)
        first = c.last(topic).json()["components"]
        ids = {k: v["unique_id"] for k, v in first.items() if k in keys}
        found = {
            k: {"raw": 65535}
            for k in (
                "device_type",
                "dsp_protocol_version",
                "mppt_count",
                "phase_count",
                "battery_input_code",
            )
        }
        r.pub.on_diagnostics("deye1", {"unreported": found})
        await settle(
            lambda: c.last(topic).json()["components"]["device_type"] == {"platform": "sensor"}
        )
        dropped = c.last(topic).json()["components"]
        assert dropped["dsp_protocol_version"] == {"platform": "sensor"}
        assert all(
            "unique_id" in dropped[k]
            for k in keys
            if k not in ("device_type", "dsp_protocol_version")
        )
        assert "unique_id" not in c.last(app).json()["components"]["inverter_deye1_r65_raw"]
        assert r.pub._inv["deye1"].keys == frozenset(keys)  # the saved selection stays intact
        fallback = replace(
            snap("identity"),
            profile_keys=frozenset({"mppt_count", "phase_count", "battery_input_code"}),
        )
        r.pub.on_state(
            "deye1", fallback, {"mppt_count": 2, "phase_count": 3, "battery_input_code": 1}
        )
        await settle(lambda: c.on(state_topic("identity")))
        payload = c.last(state_topic("identity")).json()
        assert payload["sources"] == {
            "mppt_count": "profile",
            "phase_count": "profile",
            "battery_input_code": "profile",
        }
        for k in payload["sources"]:
            template = dropped[k]["json_attributes_template"]
            assert "source" in template
        r.pub.on_diagnostics("deye1", {"unreported": {}})
        await settle(lambda: "unique_id" in c.last(topic).json()["components"]["device_type"])
        restored = c.last(topic).json()["components"]
        assert {k: restored[k]["unique_id"] for k in keys} == ids
        assert "unique_id" in c.last(app).json()["components"]["inverter_deye1_r65_raw"]
        r.pub.on_state("deye1", snap("status", cycle=2), {"operating_state": "normal"})
        await settle(lambda: c.on(state_topic("status")))
        assert c.last(state_topic("status")).json()["operating_state"] == "Normal"
        assert "Normal" in restored["operating_state"]["options"]
