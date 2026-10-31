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
        assert "diag_r65_raw" not in c.last(topic).json()["components"]  # R65 is unreported
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
        r.pub.on_diagnostics(
            "deye1", {"unreported": {}, "r65": {"raw": 0x0104, "last_read_ok": True}}
        )
        await settle(lambda: "unique_id" in c.last(topic).json()["components"]["device_type"])
        restored = c.last(topic).json()["components"]
        assert {k: restored[k]["unique_id"] for k in keys} == ids
        assert "diag_r65_raw" in c.last(topic).json()["components"]
        r.pub.on_state("deye1", snap("status", cycle=2), {"operating_state": "normal"})
        await settle(lambda: c.on(state_topic("status")))
        assert c.last(state_topic("status")).json()["operating_state"] == "Normal"
        assert "Normal" in restored["operating_state"]["options"]


async def test_r65_diagnostics_exist_only_while_r65_is_read_and_reported(tmp_path: Path) -> None:
    async with make(tmp_path, keys=("battery_voltage",)) as r:
        c = await r.ready()
        running(r)
        topic = NS.discovery_topic(NS.inverter_device_id("deye1"))

        def has_r65() -> bool:
            comps = c.last(topic).json()["components"]
            return any(k.startswith("diag_r65") and "unique_id" in v for k, v in comps.items())

        # the energy resolution is the user's choice: R65 is not read, nothing to show
        r.pub.on_diagnostics(
            "deye1", {"unreported": {}, "r65": {"raw": None, "last_read_ok": None}}
        )
        await r.idle()
        assert not has_r65()
        r.pub.on_diagnostics("deye1", {"unreported": {}, "r65": {"raw": 260, "last_read_ok": True}})
        await settle(has_r65)
        r.pub.on_diagnostics(
            "deye1", {"unreported": {}, "r65": {"raw": None, "last_read_ok": None}}
        )
        await settle(lambda: not has_r65())
