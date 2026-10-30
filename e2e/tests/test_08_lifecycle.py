"""Lifecycle and recovery on a third instance: generation persisted before discovery survives a
crash at that exact point (barrier), a failed R65 confirmation changes nothing (the failing read
is observed), a damaged energy history needs an explicit reset (new opaque namespace), fallback
with the same and with a different serial number."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack

import pytest

from harness.devices import registers
from harness.fakes import FakeInverter
from harness.stack import MessageLog, ProcessApp, options
from tests.helpers import inverter_states, ours, quiesce_and_check, until

pytestmark = pytest.mark.usefixtures("running")

C = "e2ecccc3"
KEY = "battery_energy_charge_total"
SERIES = re.compile(r"^(?:[a-z0-9]{8})?g[0-9]+$")


def energy_file(app: ProcessApp) -> dict:
    return json.loads((app.dir / "energy.json").read_text())["data"]


async def series(ha, inverter="c_gen") -> set[str]:
    prefix = f"deye_{C}_{inverter}_{KEY}_"
    return {
        u[len(prefix) :]
        for u in await ours(ha, C)
        if u.startswith(prefix) and SERIES.match(u[len(prefix) :])
    }


@pytest.fixture(scope="module")
async def instance_c(running):
    async with AsyncExitStack() as ex:
        fakes = {
            "gen": FakeInverter("gen", "mbap", regs=registers("2404000001", soc=41)),
            "fbp": FakeInverter("fbp", "mbap", regs=registers("2404000002", soc=42)),
            "fbq": FakeInverter("fbq", "rtu", regs=registers("2404000002", soc=43)),
            "dxp": FakeInverter("dxp", "mbap", regs=registers("2404000003", soc=44)),
            "dxq": FakeInverter("dxq", "rtu", regs=registers("2404000009", soc=99)),
        }
        for f in fakes.values():
            ex.push_async_callback(f.stop)
            await f.start()

        def endpoint(eid, kind, fake):
            return {
                "id": eid,
                "transport": kind,
                "host": "127.0.0.1",
                "port": fake.port,
                "timeout_s": 1.0,
                "request_gap_ms": 20,
            }

        endpoints = [
            endpoint("e_gen", "modbus_tcp", fakes["gen"]),
            endpoint("e_fbp", "modbus_tcp", fakes["fbp"]),
            endpoint("e_fbq", "rtu_over_tcp", fakes["fbq"]),
            endpoint("e_dxp", "modbus_tcp", fakes["dxp"]),
            endpoint("e_dxq", "rtu_over_tcp", fakes["dxq"]),
        ]
        poll = {"poll_fast_s": 2, "poll_energy_s": 10}
        inverters = [
            {"id": "c_gen", "name": "c_gen", "endpoint": "e_gen", "unit_id": 1, **poll},
            {
                "id": "c_fb",
                "name": "c_fb",
                "endpoint": "e_fbp",
                "fallback_endpoint": "e_fbq",
                "unit_id": 1,
                **poll,
            },
            {
                "id": "c_dx",
                "name": "c_dx",
                "endpoint": "e_dxp",
                "fallback_endpoint": "e_dxq",
                "unit_id": 1,
                **poll,
            },
        ]
        app = ProcessApp(
            "c", options("127.0.0.1", running.broker.port, endpoints, inverters, instance_id=C)
        )
        ex.callback(app.cleanup)
        app.start()
        await app.wait_api(60)
        await app.choose_all(["c_gen", "c_fb", "c_dx"])

        async def data():
            states = await running.ha.states()
            return all(
                inverter_states(states, C, i).get("battery_soc") == str(v)
                and inverter_states(states, C, i).get("pv_energy_total_g0")
                not in (None, "unknown", "unavailable")
                for i, v in (("c_gen", 41), ("c_fb", 42), ("c_dx", 44))
            )

        await until(data, "instance C delivers data", 150, 2)
        yield app, fakes
        # the fallback fakes only carry traffic in some phases: the shape of every stream is checked
        await quiesce_and_check(app, fakes, expect_traffic=False)
        assert fakes["gen"].wire.reads and fakes["fbq"].wire.reads


async def test_failed_r65_confirmation_changes_no_generation(running, instance_c):
    app, fakes = instance_c
    fake = fakes["gen"]
    fake.regs[65] = 0x0105  # a different resolution is reported once ...
    fake.drop_connections()  # (R65 is read again after a reconnect)

    async def pending():
        inv = (await app.inverters())["c_gen"]["status"]
        return (inv.get("energy") or {}).get("pending_change") is not None

    await until(
        pending, "the changed R65 shows as a pending change, not as a new resolution", 150, 0.5
    )
    fake.fail_ranges.append((65, 66))  # ... and the confirming read fails

    async def failing_confirmation_observed():
        return fake.hits.get(65, 0) >= 1  # the fake answered a read of R65 with an exception

    await until(
        failing_confirmation_observed,
        "the confirming read of R65 was attempted and failed",
        150,
        0.5,
    )
    fake.fail_ranges.clear()
    fake.regs[65] = 0x0104  # the device was fine all along
    fake.drop_connections()

    async def settled():
        inv = (await app.inverters())["c_gen"]["status"]
        return inv["state"] == "running" and (inv.get("energy") or {}).get("pending_change") is None

    await until(settled, "the pending change is gone and the inverter runs again", 150, 1)
    assert energy_file(app)["c_gen"]["generation"] == 0
    assert await series(running.ha) == {"g0"}

    async def valid():
        return inverter_states(await running.ha.states(), C, "c_gen").get(
            "battery_energy_charge_total_g0"
        ) not in (None, "unknown", "unavailable")

    await until(valid, "the energy entity is valid again under the unchanged generation", 120, 2)


async def test_a_crash_right_after_the_generation_was_persisted_continues_the_same_generation(
    running, instance_c
):
    app, fakes = instance_c
    await app.terminate()
    app.edit_plant("c_gen", energy_resolution="1")
    await app.arm_barrier("energy_generation_persisted")
    app.start()
    await app.barrier_reached("energy_generation_persisted", 90)
    # the exact point: persisted (proven by the file), the event loop is blocked, nothing published
    assert energy_file(app)["c_gen"]["generation"] == 1
    docs = await running.broker.retained("homeassistant/device/#")
    assert not [t for t, p in docs.items() if C in t and b"energy_charge_total_g1" in p], (
        "g1 was already published"
    )
    app.kill()
    app.disarm_barrier()
    app.start()
    await app.wait_api(60)

    async def switched():
        return await series(running.ha) == {"g1"}

    await until(
        switched,
        "after the crash the same new generation g1 is published (never a fallback to g0)",
        180,
        2,
    )
    assert energy_file(app)["c_gen"]["generation"] == 1
    assert not any(
        u.endswith("c_gen_battery_energy_charge_total_g0") for u in await ours(running.ha, C)
    )


async def test_damaged_energy_history_blocks_energy_until_an_explicit_reset_gives_a_new_namespace(
    running, instance_c
):
    app, _ = instance_c
    await app.terminate()
    (app.dir / "energy-generations.json").write_text("{this is not json")  # noqa: ASYNC240
    app.start()
    await app.wait_api(60)

    async def blocked():
        return ((await app.inverters())["c_gen"]["status"].get("energy") or {}).get("blocked")

    reason = await until(blocked, "energy publication is blocked", 90, 1)
    assert reason == "energy_store_corrupt", reason

    async def others_flow():
        return inverter_states(await running.ha.states(), C, "c_gen").get("battery_soc") == "41"

    await until(
        others_flow, "non-energy values keep flowing while the energy series is blocked", 90, 2
    )
    got = inverter_states(await running.ha.states(), C, "c_gen")
    energy_keys = [k for k in got if "_energy_" in k]
    assert energy_keys and all(got[k] in ("unknown", "unavailable") for k in energy_keys), got

    counter = await app.action_counter()
    status, body = await app.api(
        "POST",
        "api/stores/reset",
        {"kind": "energy", "scope": "inverter", "inverter": "c_gen", "confirm": True},
        revision=counter,
    )
    assert status == 200, body

    async def namespaced():
        found = await series(running.ha)
        if not found:
            return None
        assert all(re.match(r"^[a-z0-9]{8}g0$", s) for s in found), f"series now: {sorted(found)}"
        return found

    found = await until(namespaced, "a new opaque namespace series starting at g0", 150, 2)
    assert not found & {"g0", "g1"}, "no reuse of an already published series"


async def test_fallback_with_the_same_serial_keeps_publishing_under_the_same_ids(
    running, instance_c
):
    app, fakes = instance_c
    before = set(await ours(running.ha, C))
    await fakes["fbp"].set_down(True)

    async def on_fallback():
        status = (await app.inverters())["c_fb"]["status"]
        return (
            status["active_path"] != "primary"
            and inverter_states(await running.ha.states(), C, "c_fb").get("battery_soc") == "43"
        )

    await until(
        on_fallback,
        "the inverter continues through the fallback endpoint (same serial number)",
        200,
        2,
    )
    assert {u for u in before if "c_fb_" in u} <= set(await ours(running.ha, C)), (
        "entity ids do not depend on the path"
    )
    assert len(fakes["fbq"].wire.reads) > 20
    await fakes["fbp"].set_down(False)


async def test_fallback_with_a_different_serial_is_never_published(running, instance_c):
    app, fakes = instance_c
    log = await MessageLog(running.broker, f"deye_monitor/{C}/c_dx/#").start()
    try:
        await fakes["dxp"].set_down(True)

        async def foreign_device_contacted_and_withdrawn():
            state = (await app.inverters())["c_dx"]["status"]["state"]
            return len(fakes["dxq"].wire.reads) > 0 and state in ("quarantined", "offline")

        await until(
            foreign_device_contacted_and_withdrawn,
            "the foreign device behind the fallback was contacted and the inverter withdrawn",
            200,
            2,
        )
        await asyncio.sleep(10)
        assert 99 not in log.json_values("battery_soc"), (
            "a value of the foreign device was published"
        )
        assert (
            inverter_states(await running.ha.states(), C, "c_dx").get("battery_soc")
            == "unavailable"
        )
    finally:
        await log.stop()
    await fakes["dxp"].set_down(False)
