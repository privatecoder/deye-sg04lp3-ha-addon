"""Session-wide stack: private Docker network, broker, Home Assistant, four fake inverters
(modbus_tcp, rtu_over_tcp, solarman_v5_tcp, serial_rtu over a PTY) and the app as a process.

Every resource is registered for removal the moment it exists (``AsyncExitStack``), so a failing
set-up or test still releases all of it; the run owns its resources through a run id (see
``harness.docker``)."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from harness import docker  # noqa: E402
from harness.devices import registers  # noqa: E402
from harness.fakes import LOGGER_SERIAL, FakeInverter  # noqa: E402
from harness.ha import HomeAssistant  # noqa: E402
from harness.stack import Broker, ProcessApp, options  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ARTIFACTS = HERE / "artifacts" / docker.RUN_ID  # per run: parallel runs never overwrite each other
INSTANCE = "e2eaaaa1"
SERIALS = {
    "deye_tcp": "2401000001",
    "deye_rtu": "2401000002",
    "deye_v5": "2401000003",
    "deye_ser": "2401000004",
}


@dataclass
class Stack:
    net: docker.Net
    broker: Broker
    ha: HomeAssistant
    fakes: dict[str, FakeInverter]
    app: ProcessApp
    exit: AsyncExitStack
    extras: list[object] = field(default_factory=list)


def app_options(
    broker_port: int, fakes: dict[str, FakeInverter], instance: str, prefix: str = "deye_monitor"
) -> dict:
    endpoints = [
        {
            "id": "gw_tcp",
            "transport": "modbus_tcp",
            "host": "127.0.0.1",
            "port": fakes["deye_tcp"].port,
            "timeout_s": 2.0,
            "request_gap_ms": 20,
        },
        {
            "id": "gw_rtu",
            "transport": "rtu_over_tcp",
            "host": "127.0.0.1",
            "port": fakes["deye_rtu"].port,
            "timeout_s": 2.0,
            "request_gap_ms": 20,
        },
        {
            "id": "gw_v5",
            "transport": "solarman_v5_tcp",
            "host": "127.0.0.1",
            "port": fakes["deye_v5"].port,
            "logger_serial": LOGGER_SERIAL,
            "timeout_s": 2.0,
            "request_gap_ms": 20,
        },
        {
            "id": "gw_ser",
            "transport": "serial_rtu",
            "serial_device": fakes["deye_ser"].path,
            "baudrate": 9600,
            "timeout_s": 2.0,
            "request_gap_ms": 20,
        },
    ]
    endpoint_of = {
        "deye_tcp": "gw_tcp",
        "deye_rtu": "gw_rtu",
        "deye_v5": "gw_v5",
        "deye_ser": "gw_ser",
    }
    inverters = [
        {
            "id": iid,
            "name": iid,
            "endpoint": endpoint_of[iid],
            "unit_id": 1,
            "expected_serial": SERIALS[iid],
            "poll_fast_s": 2,
            "poll_energy_s": 10,
        }
        for iid in SERIALS
    ]
    return options(
        "127.0.0.1", broker_port, endpoints, inverters, instance_id=instance, topic_prefix=prefix
    )


@pytest.fixture(scope="session")
async def stack() -> AsyncIterator[Stack]:
    docker.require_docker()
    docker.acquire_run_lock()
    cleaned = docker.remove_orphans()  # only resources of dead runs of this machine
    versions = docker.ensure_images()
    async with AsyncExitStack() as ex:
        ex.callback(docker.remove_own_resources)  # runs last: whatever else was missed
        net = docker.Net()
        net.create()
        ex.callback(net.remove)
        broker = Broker(net.name)
        ex.callback(broker.remove)
        broker.start()
        await broker.wait_ready()
        ha = HomeAssistant(net.name)
        ex.callback(ha.remove)
        ex.push_async_callback(ha.close)
        ha.start()
        fakes: dict[str, FakeInverter] = {}
        kinds = {"deye_tcp": "mbap", "deye_rtu": "rtu", "deye_v5": "v5", "deye_ser": "serial"}
        for iid, kind in kinds.items():
            fakes[iid] = FakeInverter(iid, kind, regs=registers(SERIALS[iid]))  # type: ignore[arg-type]
            ex.push_async_callback(fakes[iid].stop)
            await fakes[iid].start()
        await ha.wait_ready()
        await ha.onboard()
        await ha.setup_mqtt(broker.name)
        app = ProcessApp("a", app_options(broker.port, fakes, INSTANCE))
        ex.callback(app.cleanup)
        _write_versions(versions, cleaned)
        yield Stack(net, broker, ha, fakes, app, ex)


@pytest.fixture(scope="session")
async def running(stack: Stack) -> Stack:
    """The app is up, every inverter has a saved selection and delivers data to Home Assistant."""
    from tests.helpers import INV, inverter_states, until

    if not stack.app.running():
        stack.app.start()
    await stack.app.wait_api(60)
    # the first start waits for a choice (SPEC §9.3). The fakes have no BMS cable: the inverter's
    # own battery values (SOC, temperature) are the template; the raw fault word is added by hand
    # (not in the template since 0.2.13).
    await stack.app.choose_all(list(SERIALS), features={"bms": False}, add=["fault_word_1"])

    async def all_data() -> bool:
        from harness.devices import EXPECTED

        states = await stack.ha.states()

        def good(i: str) -> bool:
            got = inverter_states(states, INSTANCE, i)
            keys = [*EXPECTED, "pv_energy_total_g0", "serial_number"]
            return all(got.get(k) not in (None, "unknown", "unavailable") for k in keys)

        return all(good(i) for i in INV)

    await until(
        all_data, "all four inverters deliver data (incl. energy) to Home Assistant", 120, 2
    )
    return stack


# -- provenance ------------------------------------------------------------------------------------


def _git_head() -> str:
    """The checked-out commit read from ``.git`` (no git command is run)."""
    try:
        head = (REPO / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            path = REPO / ".git" / ref
            if path.exists():
                return path.read_text().strip()
            for line in (REPO / ".git" / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0]
        return head
    except OSError:
        return "unknown"


def _tree_hash(paths: list[Path]) -> tuple[str, int]:
    digest, count = hashlib.sha256(), 0
    for base in paths:
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for f in files:
            if (
                "__pycache__" in f.parts
                or f.suffix == ".pyc"
                or "artifacts" in f.parts
                or ".venv" in f.parts
            ):
                continue
            digest.update(str(f.relative_to(REPO)).encode() + b"\0" + f.read_bytes() + b"\0")
            count += 1
    return digest.hexdigest(), count


def record_versions(extra: dict) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / "versions.json"
    info = json.loads(path.read_text()) if path.exists() else {}
    info.update(extra)
    path.write_text(json.dumps(info, indent=1))


def _write_versions(images: dict[str, str], cleaned: list[str]) -> None:
    import deye_monitor

    app_hash, app_files = _tree_hash(
        [
            REPO / "deye_monitor" / "src",
            REPO / "deye_monitor" / "Dockerfile",
            REPO / "deye_monitor" / "pyproject.toml",
            REPO / "deye_monitor" / "uv.lock",
        ]
    )
    e2e_hash, e2e_files = _tree_hash([HERE / "harness", HERE / "tests", HERE / "conftest.py"])
    record_versions(
        {
            "run_id": docker.RUN_ID,
            "orphans_cleaned": cleaned,
            "checked_out_commit": _git_head(),
            "app_source_sha256": app_hash,
            "app_source_files": app_files,
            "e2e_source_sha256": e2e_hash,
            "e2e_source_files": e2e_files,
            "imported_app_module": str(Path(deye_monitor.__file__).resolve()),
            "host": f"{platform.system()} {platform.machine()}",
            "docker_arch": docker.run("info", "--format", "{{.Architecture}}").stdout.strip(),
            "python": sys.version.split()[0],
            "images": images,
            "ha_image_arch": docker.image_arch(docker.HOME_ASSISTANT),
        }
    )
