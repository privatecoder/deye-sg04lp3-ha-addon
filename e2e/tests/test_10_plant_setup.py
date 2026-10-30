"""Setting up the plant through the web interface's API against a real Home Assistant (T14): an app
that starts with an EMPTY plant gets its connection and inverter, identifies it, publishes entities
after the first choice, applies an edit live and removes a deleted inverter from Home Assistant.
The calls are the ones the Ingress UI makes; the browser side is covered by the UI tests of the app
(``pytest -m ui``)."""

from __future__ import annotations

import json

import pytest

from harness.devices import registers
from harness.fakes import FakeInverter
from harness.stack import ProcessApp, options
from tests.helpers import inverter_states, ours, quiesce_and_check, until
from tests.test_00_first_start import BASIC, allowed_blocks

P = "e2epppp3"
SERIAL_P = "2403000001"


@pytest.fixture(scope="module")
async def instance_p(running):
    fake = FakeInverter("p_tcp", "mbap", regs=registers(SERIAL_P, soc=61))
    # nothing is configured in the options: the plant is empty
    app = ProcessApp("p", options("127.0.0.1", running.broker.port, [], [], instance_id=P))
    try:
        await fake.start()
        app.start()
        await app.wait_api(60)
        yield app, fake
        # the same final wire acceptance as for the main fakes: the app's traffic ends, the stream
        # is finalised, then every byte must belong to a valid FC03 frame (no tail, exact balance)
        await quiesce_and_check(app, {"p_tcp": fake})
    finally:
        try:
            await fake.stop()
        finally:
            app.cleanup()


async def test_an_empty_plant_starts_and_asks_for_the_setup(running, instance_p):
    app, fake = instance_p
    status, session = await app.api("GET", "api/session")
    assert status == 200 and session["setup_needed"] is True and session["plant_revision"] == 0
    status, plant = await app.api("GET", "api/plant")
    assert (
        plant["endpoints"] == [] and plant["inverters"] == [] and plant["writes"] == "unavailable"
    )
    assert not fake.wire.reads  # nothing is contacted without a configured connection
    entries = await ours(running.ha, P)
    assert all(u.startswith(f"deye_{P}_app_") for u in entries), sorted(entries)


async def test_the_connection_is_checked_created_and_the_inverter_identified(running, instance_p):
    app, fake = instance_p
    endpoint = {
        "id": "gw_p",
        "transport": "modbus_tcp",
        "host": "127.0.0.1",
        "port": fake.port,
        "timeout_s": 2.0,
        "request_gap_ms": 20,
    }
    # the read-only check works before anything is saved
    status, result = await app.api("POST", "api/plant/test", {"endpoint": endpoint, "unit_id": 1})
    assert status == 200 and result["ok"] and result["serial"] == SERIAL_P, result
    assert result["path"] == "temporary" and result["model"]["mppt"] == 2
    status, plant = await app.api("GET", "api/plant")
    assert plant["endpoints"] == []  # a check changes nothing
    assert fake.wire.reads, "the check reached the device"
    assert {(r.start, r.count) for r in fake.wire.reads} <= allowed_blocks()
    # the strict schema applies
    status, body = await app.plant_call(
        "POST",
        "api/plant/inverters",
        {"id": "p_inv", "name": "x", "endpoint": "gw_p", "unit_id": 1, "allow_writes": True},
    )
    assert status == 400 and body["error"]["key"] == "unknown_field", body

    status, body = await app.plant_call("POST", "api/plant/endpoints", endpoint)
    assert status == 200, body
    status, body = await app.plant_call(
        "POST",
        "api/plant/inverters",
        {
            "id": "p_inv",
            "name": "Deye P",
            "endpoint": "gw_p",
            "unit_id": 1,
            "poll_fast_s": 2,
            "poll_energy_s": 10,
        },
    )
    assert status == 200, body

    async def identified():
        invs = await app.inverters()
        return invs.get("p_inv", {}).get("status", {}).get("serial") == SERIAL_P

    await until(identified, "the new inverter is identified without a restart of the app", 90, 1)
    invs = await app.inverters()
    assert (
        invs["p_inv"]["status"]["awaiting_selection"] is True
    )  # a new inverter waits for its choice
    assert (await app.api("GET", "api/session"))[1]["setup_needed"] is False
    # up to its first choice only the mandatory and bootstrap reads (plus the check) reach the device
    assert {(r.start, r.count) for r in fake.wire.reads} <= allowed_blocks()
    entries = await ours(running.ha, P)
    assert all(u.startswith(f"deye_{P}_app_") for u in entries), sorted(
        entries
    )  # no inverter entity yet


async def test_the_first_choice_publishes_entities_to_home_assistant(running, instance_p):
    app, fake = instance_p
    ha = running.ha
    effective = (await app.inverters())["p_inv"]["selection"]["effective"]
    status, body = await app.sensors("p_inv", remove=[k for k in effective if k not in BASIC])
    assert status == 200, body

    async def basics():
        got = inverter_states(await ha.states(), P, "p_inv")
        return got.get("serial_number") == SERIAL_P and set(got) == set(BASIC)

    await until(basics, "the six basic sensors of the new inverter appear in Home Assistant", 90, 1)
    status, body = await app.sensors("p_inv", add=["battery_soc"])
    assert status == 200, body

    async def soc():
        return inverter_states(await ha.states(), P, "p_inv").get("battery_soc") == "61"

    await until(soc, "an added sensor delivers its value", 60, 1)


async def test_an_edit_applies_live_and_keeps_the_entities(running, instance_p):
    app, fake = instance_p
    ha = running.ha
    before = {u for u in await ours(ha, P) if "_p_inv_" in u}
    assert before
    status, plant = await app.api("GET", "api/plant")
    inverter = {k: v for k, v in plant["inverters"][0].items()}
    status, body = await app.plant_call(
        "PUT", "api/plant/inverters/p_inv", {**inverter, "name": "Deye P (Keller)"}
    )
    assert status == 200 and body["changed"] is True, body

    async def renamed():
        docs = await running.broker.retained(f"homeassistant/device/deye_{P}_p_inv/config")
        return any(b"Deye P (Keller)" in payload for payload in docs.values())

    await until(renamed, "the new name reaches the retained discovery", 60, 1)

    async def running_again():
        invs = await app.inverters()
        got = inverter_states(await ha.states(), P, "p_inv")
        return invs["p_inv"]["status"]["state"] == "running" and got.get("battery_soc") == "61"

    await until(
        running_again, "the inverter runs again after the live restart, values are back", 90, 1
    )
    after = {u for u in await ours(ha, P) if "_p_inv_" in u}
    assert after == before, sorted(before ^ after)  # the same entities, not new ones
    # a change that cannot be valid is refused and changes nothing
    status, body = await app.plant_call(
        "PUT", "api/plant/inverters/p_inv", {**inverter, "endpoint": "ghost"}
    )
    assert status == 422 and body["error"]["key"] == "invalid_plant", body
    assert (await app.inverters())["p_inv"]["status"]["state"] == "running"


async def test_deleting_the_inverter_removes_its_entities_and_keeps_the_binding(
    running, instance_p
):
    app, fake = instance_p
    ha = running.ha
    status, body = await app.plant_call(
        "POST", "api/plant/endpoints/gw_p/delete", {"confirm": True}
    )
    assert status == 409 and body["error"]["key"] == "endpoint_in_use", body
    status, body = await app.plant_call(
        "POST", "api/plant/inverters/p_inv/delete", {"confirm": True}
    )
    assert status == 200 and body["removed_inverters"] == ["p_inv"], body

    async def gone():
        return not any("_p_inv_" in u for u in await ours(ha, P))

    await until(gone, "the deleted inverter's entities disappear from Home Assistant", 90, 1)
    assert (await app.api("GET", "api/inverters"))[1]["inverters"] == []
    bindings = json.loads((app.dir / "identity.json").read_text())["data"]["bindings"]
    assert bindings["p_inv"]["serial"] == SERIAL_P  # kept unless the user confirms its removal
    status, body = await app.plant_call(
        "POST", "api/plant/endpoints/gw_p/delete", {"confirm": True}
    )
    assert status == 200, body
    plant = (
        json.loads((app.dir / "plant.a.json").read_text()),
        json.loads((app.dir / "plant.b.json").read_text()),
    )
    assert (
        max(p["data"]["body"]["revision"] for p in plant) == 5
    )  # endpoint, inverter, edit, 2 deletes
