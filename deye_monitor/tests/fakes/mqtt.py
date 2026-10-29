"""In-memory MQTT connection for publisher tests: records everything, can fail on demand."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import aiomqtt

from deye_monitor.mqtt import Connection, IncomingMessage


@dataclass(frozen=True, slots=True)
class Sent:
    topic: str
    payload: bytes
    qos: int
    retain: bool

    def json(self) -> Any:
        return json.loads(self.payload)

    @property
    def text(self) -> str:
        return self.payload.decode()


@dataclass
class FakeConnection:
    number: int
    sent: list[Sent] = field(default_factory=list)
    subscriptions: list[tuple[str, int]] = field(default_factory=list)
    incoming: asyncio.Queue[IncomingMessage | None] = field(default_factory=asyncio.Queue)
    fail_publish: Callable[[Sent], Exception | None] | None = None
    closed: bool = False
    aborted: bool = False  # cut without DISCONNECT (the broker would fire the will)

    async def abort(self) -> None:
        self.aborted = True

    async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
        item = Sent(topic, bytes(payload), qos, retain)
        if self.fail_publish is not None and (exc := self.fail_publish(item)) is not None:
            raise exc
        self.sent.append(item)

    async def subscribe(self, topic: str, *, qos: int) -> None:
        self.subscriptions.append((topic, qos))

    async def messages(self) -> AsyncIterator[IncomingMessage]:
        while True:
            message = await self.incoming.get()
            if message is None:
                return
            yield message

    # -- test helpers ----------------------------------------------------------------------

    def birth(self, topic: str = "homeassistant/status") -> None:
        self.incoming.put_nowait(IncomingMessage(topic, b"online"))

    def drop(self) -> None:
        """The broker closed the connection."""
        self.incoming.put_nowait(None)

    def topics(self) -> list[str]:
        return [s.topic for s in self.sent]

    def on(self, topic: str) -> list[Sent]:
        return [s for s in self.sent if s.topic == topic]

    def last(self, topic: str) -> Sent:
        return self.on(topic)[-1]


class FakeBroker:
    """Factory: every call is a NEW connection (as with the real client)."""

    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.connect_errors: list[Exception] = []
        self.fail_publish: Callable[[Sent], Exception | None] | None = None
        self.opened = asyncio.Event()

    def __call__(self) -> contextlib.AbstractAsyncContextManager[Connection]:
        return self._connect()

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[Connection]:
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        conn = FakeConnection(len(self.connections) + 1, fail_publish=self.fail_publish)
        self.connections.append(conn)
        self.opened.set()
        try:
            yield conn
        finally:
            conn.closed = True

    @property
    def current(self) -> FakeConnection:
        return self.connections[-1]

    async def wait_connections(self, n: int, timeout: float = 5.0) -> FakeConnection:
        async with asyncio.timeout(timeout):
            while len(self.connections) < n:
                await asyncio.sleep(0.005)
        return self.connections[n - 1]


def mqtt_error(text: str = "broken") -> Exception:
    return aiomqtt.MqttError(text)


async def settle(predicate: Callable[[], object], timeout: float = 5.0) -> None:
    """Wait (real time, tiny polls) until ``predicate`` holds."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)
