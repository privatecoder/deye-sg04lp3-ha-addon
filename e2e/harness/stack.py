"""Broker, app (process or Docker image), ingress API client and the instance directories."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import aiomqtt

from harness import docker
from harness.fakes import FakeInverter

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO / "deye_monitor"
USER_HEADERS = {"X-Remote-User-Id": "u1", "X-Remote-User-Name": "e2e"}


class Broker:
    def __init__(self, net: str, *, persistence: bool = False) -> None:
        self.persistence = persistence
        self.dir = Path(tempfile.mkdtemp(prefix="deye-e2e-mosq-"))
        lines = ["listener 1883", "allow_anonymous true"] + [f"log_type {t}" for t in ("error", "warning", "notice", "subscribe", "unsubscribe")]
        lines += ["persistence true", "persistence_location /mosquitto/data/"] if persistence else ["persistence false"]
        (self.dir / "mosquitto.conf").write_text("\n".join(lines) + "\n")
        (self.dir / "data").mkdir()
        (self.dir / "data").chmod(0o777)
        self.name = f"deye-e2e-broker-{uuid.uuid4().hex[:6]}"
        self.container = docker.Container(
            self.name,
            docker.MOSQUITTO,
            net,
            args=[
                "-v", f"{self.dir / 'mosquitto.conf'}:/mosquitto/config/mosquitto.conf:ro",
                "-v", f"{self.dir / 'data'}:/mosquitto/data",
            ],
            published={1883: docker.free_port()},
        )

    @property
    def port(self) -> int:
        return self.container.published[1883]

    def start(self) -> None:
        self.container.start()

    async def wait_ready(self, deadline_s: float = 30) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline_s
        while loop.time() < end:
            try:
                async with aiomqtt.Client("127.0.0.1", self.port, identifier=f"probe-{uuid.uuid4().hex[:6]}", timeout=3):
                    return  # CONNACK received
            except (aiomqtt.MqttError, OSError):
                await asyncio.sleep(0.5)
        raise TimeoutError("broker did not accept a connection\n" + self.container.logs(20))

    async def restart(self) -> None:
        self.container.stop(5)
        self.container.restart()
        await self.wait_ready()

    def client(self, name: str | None = None) -> aiomqtt.Client:
        return aiomqtt.Client("127.0.0.1", self.port, identifier=name or f"e2e-{uuid.uuid4().hex[:8]}")

    async def retained(self, filt: str, settle_s: float = 1.5) -> dict[str, bytes]:
        """Everything retained under ``filt`` (a second client, so the app is not involved)."""
        out: dict[str, bytes] = {}
        async with self.client() as c:
            await c.subscribe(filt)
            try:
                async with asyncio.timeout(settle_s):
                    async for m in c.messages:
                        if m.retain and m.payload:
                            out[str(m.topic)] = bytes(m.payload)  # type: ignore[arg-type]
            except TimeoutError:
                pass
        return out

    def remove(self) -> None:
        self.container.remove()
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)


def options(
    broker_host: str,
    broker_port: int,
    endpoints: list[dict[str, Any]],
    inverters: list[dict[str, Any]],
    *,
    instance_id: str,
    topic_prefix: str = "deye_monitor",
) -> dict[str, Any]:
    return {
        "endpoints": endpoints,
        "inverters": inverters,
        "mqtt": {"host": broker_host, "port": broker_port, "topic_prefix": topic_prefix},
        "instance_id": instance_id,
        "log_level": "debug",
    }


class App:
    """One app instance with its own data directory (survives restarts of the process/container)."""

    def __init__(self, name: str, opts: dict[str, Any], web_port: int) -> None:
        self.name = name
        self.opts = opts
        self.web_port = web_port
        self.dir = Path(tempfile.mkdtemp(prefix=f"deye-e2e-{name}-"))
        self.log = self.dir / "app.log"
        (self.dir / "options.json").write_text(json.dumps(opts))
        self.csrf: str | None = None

    @property
    def instance_id(self) -> str:
        return str(self.opts["instance_id"])

    def write_options(self) -> None:
        (self.dir / "options.json").write_text(json.dumps(self.opts))

    def edit_plant(self, inverter_id: str, **changes: Any) -> None:
        """What the user would change in the web interface, done on disk while the app is
        stopped (the plant store, not the options, is authoritative since T14)."""
        from deye_monitor.config import InverterConfig, PlantConfig
        from deye_monitor.plant import PlantStore

        store = PlantStore(self.dir)
        inverters = tuple(
            InverterConfig.model_validate({**i.model_dump(mode="json", exclude_none=True), **changes})
            if i.id == inverter_id
            else i
            for i in store.plant.inverters
        )
        store.commit(PlantConfig(endpoints=store.plant.endpoints, inverters=inverters), store.revision + 1)

    # to be provided by the variants
    def start(self) -> None: ...
    def kill(self) -> None: ...
    async def terminate(self) -> None: ...
    def running(self) -> bool: ...
    def cleanup(self) -> None: ...
    def logs(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def keep_log_and_remove_dir(self) -> None:
        """The app log goes to ``e2e/artifacts/logs``; the data directory is removed."""
        import shutil

        logs = REPO / "e2e" / "artifacts" / docker.RUN_ID / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"app-{self.name}-{self.instance_id}.log").write_text(self.logs()[-200000:])
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- ingress API -----------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.api_base}/{path.lstrip('/')}"

    api_base = ""
    api_headers: dict[str, str] = {}

    async def wait_api(self, deadline_s: float = 60) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline_s
        while loop.time() < end:
            try:
                async with aiohttp.ClientSession(headers=self.api_headers) as s, s.get(
                    self._url("api/session"), headers=USER_HEADERS, timeout=aiohttp.ClientTimeout(total=3)
                ) as r:
                    if r.status == 200:
                        return
            except aiohttp.ClientError, TimeoutError:
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError(f"{self.name}: ingress API not ready\n{self.logs()[-1500:]}")

    async def api(self, method: str, path: str, body: Any = None, *, revision: int | None = None) -> tuple[int, Any]:
        async with aiohttp.ClientSession(headers={**self.api_headers, **USER_HEADERS}) as s:
            async with s.get(self._url("api/session")) as r:
                session = await r.json()
            headers = {"X-CSRF-Token": session["csrf"]}
            if revision is not None:
                headers["If-Match"] = f'"{revision}"'
            async with s.request(method, self._url(path), json=body, headers=headers) as r:
                try:
                    data = await r.json()
                except aiohttp.ContentTypeError:
                    data = await r.text()
                return r.status, data

    async def choose_all(self, inverter_ids: list[str]) -> None:
        """Save the template as the selection of every inverter (ends the first-start state)."""
        for iid in inverter_ids:
            status, body = await self.sensors(iid, choose=True)
            assert status == 200, (iid, body)

    async def revision(self) -> int:
        s, d = await self.api("GET", "api/session")
        return int(d["revision"])

    async def plant_call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """A change of the plant (connections, inverters) with the current plant revision."""
        s, d = await self.api("GET", "api/session")
        return await self.api(method, path, body, revision=int(d["plant_revision"]))

    async def action_counter(self) -> int:
        s, d = await self.api("GET", "api/session")
        return int(d["action_counter"])

    async def sensors(self, inverter: str, **edit: Any) -> tuple[int, Any]:
        return await self.api("POST", f"api/inverters/{inverter}/sensors", edit, revision=await self.revision())

    async def inverters(self) -> dict[str, Any]:
        s, d = await self.api("GET", "api/inverters")
        assert s == 200, d
        return {i["id"]: i for i in d["inverters"]}


class ProcessApp(App):
    def __init__(self, name: str, opts: dict[str, Any]) -> None:
        super().__init__(name, opts, docker.free_port())
        self.proc: subprocess.Popen[bytes] | None = None
        self.api_base = f"http://127.0.0.1:{self.web_port}"

    def start(self) -> None:
        env = {
            **os.environ,
            "DEYE_MONITOR_OPTIONS": str(self.dir / "options.json"),
            "DEYE_MONITOR_DATA": str(self.dir),
            "E2E_WEB_PORT": str(self.web_port),
            "E2E_BARRIER_FILE": str(self.dir / "barrier"),
            "PYTHONUNBUFFERED": "1",
        }
        self.write_options()
        log = open(self.log, "ab")  # noqa: SIM115 - closed with the process
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "harness.launcher"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def arm_barrier(self, name: str) -> None:
        """The process blocks right after the named commit (see ``harness.launcher``)."""
        (self.dir / "barrier.reached").unlink(missing_ok=True)
        (self.dir / "barrier").write_text(name)

    async def barrier_reached(self, name: str, deadline_s: float = 60) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline_s
        flag = self.dir / "barrier.reached"
        while loop.time() < end:
            if flag.exists() and flag.read_text() == name:
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"barrier {name} not reached\n{self.logs()[-1500:]}")

    def disarm_barrier(self) -> None:
        (self.dir / "barrier").unlink(missing_ok=True)
        (self.dir / "barrier.reached").unlink(missing_ok=True)

    def kill(self) -> None:
        if self.proc is not None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)

    async def terminate(self, deadline_s: float = 60) -> None:
        """SIGTERM and wait without blocking the event loop (the PTY fake runs on it: a blocked
        loop would stop reading the pseudo terminal and hang the app's flush)."""
        if self.proc is None:
            return
        self.proc.send_signal(signal.SIGTERM)
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline_s
        while self.proc.poll() is None and loop.time() < end:
            await asyncio.sleep(0.2)
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGUSR1)  # task/thread dump into the log for the report
            await asyncio.sleep(1)
            self.proc.kill()
            self.proc.wait(10)
            raise TimeoutError(f"{self.name}: no orderly stop within {deadline_s}s\n{self.logs()[-2500:]}")

    def cleanup(self) -> None:
        if self.running():
            self.kill()
        self.keep_log_and_remove_dir()


class ImageApp(App):
    """The app as its Docker image on the private network. The ingress API is reached through a
    proxy container that owns the Supervisor address (172.30.32.2), so the product's peer check
    runs unmodified."""

    def __init__(self, name: str, opts: dict[str, Any], net: str, image: str) -> None:
        super().__init__(name, opts, 0)
        self.net = net
        self.image = image
        self.cname = f"deye-e2e-app-{name}-{uuid.uuid4().hex[:6]}"
        self.container = docker.Container(
            self.cname, image, net, args=["-v", f"{self.dir}:/data"]
        )
        self.proxy = docker.Container(
            f"deye-e2e-proxy-{name}-{uuid.uuid4().hex[:6]}",
            docker.PROXY,
            net,
            ip=docker.SUPERVISOR_PEER,
            published={8099: 0},
            command=["caddy", "reverse-proxy", "--from", ":8099", "--to", f"{self.cname}:8099"],
        )

    @property
    def api_base(self) -> str:  # type: ignore[override]
        return f"http://127.0.0.1:{self.proxy.published[8099]}"

    def start(self) -> None:
        self.write_options()
        if not self.container.started:
            self.container.start()
            self.proxy.start()
        else:
            self.container.restart()

    def running(self) -> bool:
        return self.container.running()

    def kill(self) -> None:
        self.container.kill()

    async def terminate(self, deadline_s: float = 60) -> None:
        docker.run("stop", "-t", str(int(deadline_s)), self.cname, timeout=deadline_s + 30)

    def logs(self) -> str:
        return self.container.logs(400)

    def cleanup(self) -> None:
        self.keep_log_and_remove_dir()
        self.container.remove()
        self.proxy.remove()

    def probe_from_foreign_peer(self) -> str:
        """An HTTP request from any other address than the Supervisor's must be refused."""
        out = docker.run(
            "run", "--rm", "--label", docker.LABEL, "--network", self.net, docker.PROXY,
            "wget", "-S", "-q", "-O", "-", f"http://{self.cname}:8099/api/session",
            check=False, timeout=60,
        )
        return out.stderr + out.stdout


class MessageLog:
    """Every message under a topic filter from the moment of ``start()`` on, without gaps (a
    dedicated client): proof of what was published, not a sample taken by polling."""

    def __init__(self, broker: Broker, filt: str) -> None:
        self.broker = broker
        self.filt = filt
        self.messages: list[tuple[float, str, bytes, bool]] = []
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def start(self) -> MessageLog:
        self._task = asyncio.get_running_loop().create_task(self._run())
        await asyncio.wait_for(self._ready.wait(), 15)
        return self

    async def _run(self) -> None:
        async with self.broker.client(f"log-{uuid.uuid4().hex[:6]}") as c:
            await c.subscribe(self.filt, qos=1)
            self._ready.set()
            loop = asyncio.get_running_loop()
            async for m in c.messages:
                self.messages.append((loop.time(), str(m.topic), bytes(m.payload), bool(m.retain)))  # type: ignore[arg-type]

    async def stop(self) -> None:
        if self._task is not None:
            await asyncio.sleep(0.3)  # let the last messages arrive
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def json_values(self, key: str, topic_part: str = "") -> list[object]:
        """Every value found under ``key`` in JSON payloads of topics containing ``topic_part``."""
        out: list[object] = []
        for _, topic, payload, _ in self.messages:
            if topic_part not in topic:
                continue
            try:
                doc = json.loads(payload)
            except ValueError:
                continue
            if isinstance(doc, dict) and key in doc:
                out.append(doc[key])
        return out
