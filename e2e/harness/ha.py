"""A real Home Assistant Core in Docker (loopback API, private network), onboarded through its
own API, with the MQTT integration set up by config flow against the test broker."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from harness import docker

CLIENT_ID = "http://127.0.0.1:8123/"
CONFIGURATION = """\
homeassistant:
  name: Deye E2E
  unit_system: metric
  time_zone: UTC
  latitude: 52.0
  longitude: 13.0
  elevation: 30
  currency: EUR
logger:
  default: info
  logs:
    homeassistant.components.mqtt: info
    homeassistant.components.recorder: info
http:
api:
config:
websocket_api:
person:
onboarding:
history:
recorder:
  db_url: sqlite:////config/home-assistant_v2.db
  commit_interval: 1
  auto_purge: false
sensor:
binary_sensor:
"""


class HomeAssistant:
    def __init__(self, net: str, config_dir: Path | None = None) -> None:
        self.config_dir = config_dir or Path(tempfile.mkdtemp(prefix="deye-e2e-ha-"))
        self.container = docker.Container(
            f"deye-e2e-ha-{uuid.uuid4().hex[:6]}",
            docker.HOME_ASSISTANT,
            net,
            args=["-v", f"{self.config_dir}:/config", "-e", "TZ=UTC"],
            published={8123: 0},
        )
        self.token: str | None = None
        self.last_start_s = 0.0
        self._session: aiohttp.ClientSession | None = None

    @property
    def port(self) -> int:
        return self.container.published[8123]

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        (self.config_dir / "configuration.yaml").write_text(CONFIGURATION)
        self.container.start()

    async def wait_ready(self, deadline_s: float = 300) -> None:
        """Wait until the HTTP stack answers. (``/auth/providers`` is used: the unauthenticated
        ``/api/`` itself never answers in this configuration.)"""
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline_s
        last: object = None
        async with aiohttp.ClientSession() as s:
            while loop.time() < end:
                try:
                    async with s.get(
                        f"{self.base}/auth/providers", timeout=aiohttp.ClientTimeout(total=3)
                    ) as r:
                        last = r.status
                        if r.status < 500:
                            return
                except (aiohttp.ClientError, TimeoutError) as exc:
                    last = repr(exc)
                await asyncio.sleep(1)
        Path(tempfile.gettempdir(), "deye-e2e-ha-fail.log").write_text(self.container.logs(3000))
        raise TimeoutError(
            f"Home Assistant not ready after {deadline_s}s (last probe: {last})\n{self.container.logs(25)}"
        )

    async def stop(self) -> None:
        await self.close()
        self.container.stop(30)

    async def cold_start(self, deadline_s: float = 300) -> None:
        """Start the stopped container again (new process; registry and recorder from disk)."""
        import time

        t0 = time.monotonic()
        self.container.restart()
        await self.wait_ready(deadline_s)
        self.last_start_s = time.monotonic() - t0
        self.token = self._token_file().read_text()
        await self.wait_authenticated(deadline_s)

    async def wait_authenticated(self, deadline_s: float = 120) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + deadline_s
        while loop.time() < end:
            try:
                await self.get("/api/config")
                return
            except aiohttp.ClientError, TimeoutError, RuntimeError:
                await asyncio.sleep(1)
        raise TimeoutError("Home Assistant API does not accept the token")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def remove(self) -> None:
        self.container.remove()
        import shutil

        shutil.rmtree(self.config_dir, ignore_errors=True)

    # -- onboarding ------------------------------------------------------------------------

    def _token_file(self) -> Path:
        return self.config_dir / ".e2e-token"

    async def onboard(self) -> None:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{self.base}/api/onboarding/users",
                json={
                    "client_id": CLIENT_ID,
                    "name": "e2e",
                    "username": "e2e",
                    "password": "e2e-pass-1",
                    "language": "en",
                },
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"onboarding user failed: {r.status} {await r.text()}")
                code = (await r.json())["auth_code"]
            async with s.post(
                f"{self.base}/auth/token",
                data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID},
            ) as r:
                access = (await r.json())["access_token"]
            async with s.ws_connect(f"{self.base}/api/websocket") as ws:
                await ws.receive_json()
                await ws.send_json({"type": "auth", "access_token": access})
                await ws.receive_json()
                await ws.send_json(
                    {"id": 1, "type": "auth/long_lived_access_token", "client_name": "e2e", "lifespan": 3}
                )
                self.token = str((await ws.receive_json())["result"])
            self._token_file().write_text(self.token)
            for step, body in (
                ("core_config", {}),
                ("analytics", {}),
                ("integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID + "?auth_callback=1"}),
            ):
                async with s.post(
                    f"{self.base}/api/onboarding/{step}",
                    json=body,
                    headers={"Authorization": f"Bearer {access}"},
                ) as r:
                    await r.read()

    # -- REST / WebSocket ------------------------------------------------------------------

    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def get(self, path: str) -> Any:
        async with self.session().get(f"{self.base}{path}") as r:
            if r.status >= 400:
                raise RuntimeError(f"GET {path}: {r.status}")
            return await r.json()

    async def post(self, path: str, body: Any = None) -> Any:
        async with self.session().post(f"{self.base}{path}", json=body) as r:
            if r.status >= 400:
                raise RuntimeError(f"POST {path}: {r.status} {await r.text()}")
            return await r.json()

    async def ws(self, *commands: dict[str, Any]) -> list[Any]:
        """Run WebSocket commands on a fresh authenticated connection; returns their results."""
        out: list[Any] = []
        async with self.session().ws_connect(f"{self.base}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": self.token})
            hello = await ws.receive_json()
            if hello.get("type") != "auth_ok":
                raise RuntimeError(f"websocket auth failed: {hello}")
            for i, command in enumerate(commands, 1):
                await ws.send_json({"id": i, **command})
                reply = await ws.receive_json()
                if not reply.get("success"):
                    raise RuntimeError(f"websocket {command['type']} failed: {reply}")
                out.append(reply["result"])
        return out

    async def states(self) -> dict[str, dict[str, Any]]:
        return {s["entity_id"]: s for s in await self.get("/api/states")}

    async def setup_mqtt(self, broker: str, port: int = 1883) -> None:
        step = await self.post(
            "/api/config/config_entries/flow", {"handler": "mqtt", "show_advanced_options": False}
        )
        for _ in range(4):
            if step["type"] == "create_entry":
                return
            if step["type"] != "form":
                raise RuntimeError(f"unexpected mqtt flow step: {step}")
            names = {f["name"] for f in step.get("data_schema", [])}
            data: dict[str, Any] = {}
            if "broker" in names:
                data = {
                    "broker": broker,
                    "port": port,
                    "protocol": "3.1.1",
                    "other_settings": {"set_client_cert": False, "set_ca_cert": "off", "tls_insecure": False},
                }
            step = await self.post(f"/api/config/config_entries/flow/{step['flow_id']}", data)
        raise RuntimeError(f"mqtt config flow did not finish: {step}")

    # -- what HA persisted (read-only) -----------------------------------------------------

    def db_query(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        db = self.config_dir / "home-assistant_v2.db"
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def registry(self) -> list[dict[str, Any]]:
        path = self.config_dir / ".storage" / "core.entity_registry"
        return list(json.loads(path.read_text())["data"]["entities"]) if path.exists() else []
