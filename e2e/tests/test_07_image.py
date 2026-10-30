"""The app as its Docker image: real process, network and persistence boundaries (three network
transports; the PTY serial transport is covered by the process run).

The image runs in a network with the real Supervisor subnet so that the product's ingress peer
check is exercised unchanged; only one run at a time can hold that subnet (a lock; a second run
waits, then fails clearly)."""

from __future__ import annotations

import json
import platform
import re
import subprocess
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from conftest import record_versions
from harness import docker
from harness.devices import registers
from harness.fakes import LOGGER_SERIAL, FakeInverter
from harness.stack import APP_DIR, ImageApp, options
from tests.helpers import inverter_states, ours, quiesce_and_check, until

pytestmark = [pytest.mark.image, pytest.mark.usefixtures("running")]

IMG = "e2eimag1"
SN = {"i_tcp": "2403000001", "i_rtu": "2403000002", "i_v5": "2403000003"}
ARCH_LABEL = {"amd64": "amd64", "arm64": "aarch64", "aarch64": "aarch64"}  # docker -> io.hass.arch


def build_image() -> dict[str, str]:
    """Build the image from the app directory for the platform that is going to run it, passing the
    matching BUILD_ARCH, and return its identity (id, architecture, labels)."""
    tag = f"deye-monitor:e2e-{docker.RUN_ID}"
    platform_arg = __import__("os").environ.get("E2E_IMAGE_PLATFORM")
    wanted = (platform_arg or "").split("/")[-1] or platform.machine().replace("x86_64", "amd64")
    build_arch = ARCH_LABEL.get(wanted, "amd64")
    cmd = [
        "docker",
        "build",
        "-t",
        tag,
        "--label",
        docker.RUN_LABEL,
        "--build-arg",
        f"BUILD_ARCH={build_arch}",
    ]
    if platform_arg:
        cmd += ["--platform", platform_arg]
    proc = subprocess.run([*cmd, str(APP_DIR)], capture_output=True, text=True, timeout=1500)
    assert proc.returncode == 0, proc.stderr[-2000:]
    inspect = json.loads(docker.run("image", "inspect", tag).stdout)[0]
    labels = inspect["Config"]["Labels"]
    arch = inspect["Architecture"]
    assert labels.get("io.hass.arch") == ARCH_LABEL[arch], (
        labels.get("io.hass.arch"),
        arch,
    )  # label = reality
    return {
        "tag": tag,
        "id": inspect["Id"],
        "architecture": arch,
        "os": inspect["Os"],
        "labels": labels,
    }


def fake_addresses() -> tuple[str, str]:
    """(bind address on the host, address of the host as seen from a container)."""
    if platform.system() == "Darwin":  # Docker Desktop: the VM reaches the host's loopback
        return "127.0.0.1", "host.docker.internal"
    return "172.30.32.1", "172.30.32.1"  # Linux: the bridge gateway is a host address


@pytest.fixture(scope="module")
async def image_app(running):
    async with AsyncExitStack() as ex:
        docker.hold_supervisor_network()  # raises a clear error if another run keeps it
        ex.callback(docker.release_supervisor_network)
        net = docker.Net(subnet=docker.SUBNET, ip_range="172.30.33.0/24")
        net.create(wait_s=1200)  # waits for a party that holds the Supervisor subnet
        ex.callback(net.remove)
        docker.run("network", "connect", net.name, running.broker.name)
        ex.callback(
            lambda: docker.run(
                "network", "disconnect", "-f", net.name, running.broker.name, check=False
            )
        )
        image = build_image()
        ex.callback(lambda: docker.run("rmi", "-f", image["tag"], check=False))
        record_versions(
            {
                "app_image": {k: image[k] for k in ("id", "architecture", "os")}
                | {"labels": image["labels"]}
            }
        )
        bind, seen = fake_addresses()
        fakes = {
            "i_tcp": FakeInverter("i_tcp", "mbap", regs=registers(SN["i_tcp"], soc=61)),
            "i_rtu": FakeInverter("i_rtu", "rtu", regs=registers(SN["i_rtu"], soc=62)),
            "i_v5": FakeInverter("i_v5", "v5", regs=registers(SN["i_v5"], soc=63)),
        }
        for f in fakes.values():
            ex.push_async_callback(f.stop)
            await f.start(bind)
        endpoints = [
            {
                "id": "g_tcp",
                "transport": "modbus_tcp",
                "host": seen,
                "port": fakes["i_tcp"].port,
                "timeout_s": 2.0,
                "request_gap_ms": 20,
            },
            {
                "id": "g_rtu",
                "transport": "rtu_over_tcp",
                "host": seen,
                "port": fakes["i_rtu"].port,
                "timeout_s": 2.0,
                "request_gap_ms": 20,
            },
            {
                "id": "g_v5",
                "transport": "solarman_v5_tcp",
                "host": seen,
                "port": fakes["i_v5"].port,
                "logger_serial": LOGGER_SERIAL,
                "timeout_s": 2.0,
                "request_gap_ms": 20,
            },
        ]
        inverters = [
            {"id": i, "name": i, "endpoint": e, "unit_id": 1, "poll_fast_s": 2, "poll_energy_s": 10}
            for i, e in (("i_tcp", "g_tcp"), ("i_rtu", "g_rtu"), ("i_v5", "g_v5"))
        ]
        # the broker is reached by its name on the private network, like the Supervisor's service
        app = ImageApp(
            "img",
            options(running.broker.name, 1883, endpoints, inverters, instance_id=IMG),
            net.name,
            image["tag"],
        )
        ex.callback(app.cleanup)
        app.image_arch = image["architecture"]  # type: ignore[attr-defined]
        app.image_tag = image["tag"]  # type: ignore[attr-defined]
        app.start()
        await app.wait_api(120)
        await app.choose_all(list(SN))
        yield app, fakes
        await quiesce_and_check(app, fakes)


async def test_image_runs_and_publishes_to_home_assistant(running, image_app):
    app, _ = image_app

    async def data():
        states = await running.ha.states()
        return all(
            inverter_states(states, IMG, i).get("battery_soc") == str(v)
            for i, v in (("i_tcp", 61), ("i_rtu", 62), ("i_v5", 63))
        )

    await until(
        data, "the image delivers data of all three network transports to Home Assistant", 150, 2
    )
    entries = await ours(running.ha, IMG)
    assert entries and all(
        e["entity_id"].startswith(("sensor.", "binary_sensor.")) for e in entries.values()
    )
    labels = json.loads(
        docker.run("image", "inspect", app.image_tag, "--format", "{{json .Config.Labels}}").stdout
    )
    assert "io.hass.type" in labels and "org.opencontainers.image.title" in labels
    assert labels["io.hass.arch"] == ARCH_LABEL[app.image_arch]


async def test_ingress_api_only_accepts_the_supervisor_peer(running, image_app):
    app, _ = image_app
    foreign = app.probe_from_foreign_peer()
    assert re.search(r"HTTP/1\.\d 403", foreign), foreign  # any other address is refused
    status, _ = await app.api("GET", "api/session")
    assert status == 200  # through the proxy that owns 172.30.32.2


async def test_selection_change_and_persistence_across_a_container_kill(running, image_app):
    app, _ = image_app
    ha = running.ha
    status, body = await app.sensors("i_tcp", remove=["pv2_power"])
    assert status == 200, body

    async def gone():
        return not any(u.endswith("i_tcp_pv2_power") for u in await ours(ha, IMG))

    await until(gone, "the deselected sensor leaves the registry", 90, 1)
    app.kill()

    async def unavailable():
        mine = [s["state"] for e, s in (await ha.states()).items() if f"deye_{IMG}_" in e]
        return mine and all(v == "unavailable" for v in mine)

    await until(
        unavailable, "SIGKILL of the container: the will makes everything unavailable", 60, 1
    )
    app.start()
    await app.wait_api(90)

    async def back():
        got = inverter_states(await ha.states(), IMG, "i_tcp")
        return got.get("battery_soc") == "61" and "pv2_power" not in got

    await until(
        back, "after the restart the data returns and the persisted deselection holds", 150, 2
    )
    assert any((Path(app.dir) / n).exists() for n in ("selection.a.json", "selection.b.json"))  # noqa: ASYNC240
    assert (Path(app.dir) / "identity.json").exists()  # noqa: ASYNC240
