"""A throw-away local Mosquitto (Docker, 127.0.0.1, random port) for the ``broker`` tests.

Never a plant broker: the container is started here, listens on loopback only and is removed
afterwards. Tests are skipped when Docker or the image is not available."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import aiomqtt

# Pinned tag and the digest it had when the tests were written (2.0.22, 2026-10-28).
IMAGE = "eclipse-mosquitto:2.0.22"
IMAGE_DIGEST = "sha256:09b62ff32c2e7b64d4b431b9a89236073289b68db273de2b089edeb9b0d92b5c"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except OSError, subprocess.TimeoutExpired:
        return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


class MosquittoContainer:
    def __init__(self, *, persistence: bool) -> None:
        self.persistence = persistence
        self.name = f"deye-test-mosquitto-{uuid.uuid4().hex[:8]}"
        self.port = free_port()
        self._conf = Path(tempfile.mkdtemp(prefix="deye-mosq-")) / "mosquitto.conf"
        lines = ["listener 1883", "allow_anonymous true", "log_type error"]
        if persistence:
            lines += ["persistence true", "persistence_location /mosquitto/data/"]
        else:
            lines += ["persistence false"]
        self._conf.write_text("\n".join(lines) + "\n")

    def start(self) -> None:
        proc = _docker(
            "run", "-d", "--name", self.name, "-p", f"127.0.0.1:{self.port}:1883",
            "-v", f"{self._conf}:/mosquitto/config/mosquitto.conf:ro", IMAGE,
            timeout=300,
        )  # fmt: skip
        if proc.returncode != 0:
            raise RuntimeError(f"cannot start mosquitto: {proc.stderr.strip()[:200]}")
        self._wait()

    def _wait(self) -> None:
        """Wait until the broker ANSWERS an MQTT CONNECT. A bare TCP connect is no readiness proof:
        Docker's port forwarding accepts connections before Mosquitto listens inside the
        container (the cause of a once-seen failure of the first test of a module)."""
        connect = b"\x10\x10\x00\x04MQTT\x04\x02\x00\x0a\x00\x04wait"  # MQTT 3.1.1, clean
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with (
                contextlib.suppress(OSError),
                socket.create_connection(("127.0.0.1", self.port), timeout=1.0) as sock,
            ):
                sock.settimeout(1.0)
                sock.sendall(connect)
                if sock.recv(4)[:2] == b"\x20\x02":  # CONNACK
                    return
            time.sleep(0.1)
        raise RuntimeError("mosquitto did not come up")

    def stop_broker(self) -> None:
        """Graceful stop (writes the persistence file when enabled)."""
        _docker("stop", "-t", "5", self.name)

    def start_broker(self) -> None:
        _docker("start", self.name)
        self._wait()

    def restart(self) -> None:
        self.stop_broker()
        self.start_broker()

    def remove(self) -> None:
        _docker("rm", "-f", self.name)
        shutil.rmtree(self._conf.parent, ignore_errors=True)


@contextlib.contextmanager
def mosquitto(*, persistence: bool = False) -> Iterator[MosquittoContainer]:
    box = MosquittoContainer(persistence=persistence)
    try:
        box.start()
        yield box
    finally:
        box.remove()


@dataclass
class Seen:
    topic: str
    payload: bytes
    retain: bool
    at: float


@dataclass
class Observer:
    """Subscribes to ``#`` and records everything (with the retain flag as the broker sent it)."""

    port: int
    seen: list[Seen] = field(default_factory=list)
    _task: asyncio.Task[None] | None = None
    _ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def _run(self) -> None:
        async with aiomqtt.Client(
            "127.0.0.1", self.port, identifier=f"obs-{uuid.uuid4().hex[:6]}"
        ) as c:
            await c.subscribe("#", qos=1)
            self._ready.set()
            async for m in c.messages:
                raw = m.payload
                data = bytes(raw) if isinstance(raw, bytes | bytearray) else str(raw or "").encode()
                self.seen.append(Seen(str(m.topic), data, m.retain, time.monotonic()))

    async def start(self) -> Observer:
        self._task = asyncio.create_task(self._run())
        async with asyncio.timeout(10):
            await self._ready.wait()
        return self

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def on(self, topic: str) -> list[Seen]:
        return [s for s in self.seen if s.topic == topic]

    async def until(self, predicate: object, timeout: float = 15.0) -> None:
        assert callable(predicate)
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def observing(port: int) -> AsyncIterator[Observer]:
    obs = Observer(port)
    await obs.start()
    try:
        yield obs
    finally:
        await obs.stop()


async def retained_snapshot(port: int, pattern: str = "#", wait: float = 0.7) -> dict[str, bytes]:
    """What a NEW subscriber receives as retained messages (the broker's retained store)."""
    out: dict[str, bytes] = {}
    async with aiomqtt.Client("127.0.0.1", port, identifier=f"snap-{uuid.uuid4().hex[:6]}") as c:
        await c.subscribe(pattern, qos=1)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait):
                async for m in c.messages:
                    if m.retain:
                        raw = m.payload
                        out[str(m.topic)] = (
                            bytes(raw) if isinstance(raw, bytes | bytearray) else b""
                        )
    return out
