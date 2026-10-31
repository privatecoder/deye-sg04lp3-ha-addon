"""Shared helpers of the E2E tests: polling with deadlines, entity/registry lookups."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from harness.devices import EXPECTED  # noqa: F401 - re-exported for the tests

INV = ("deye_tcp", "deye_rtu", "deye_v5", "deye_ser")


async def until(
    check: Callable[[], Awaitable[Any]], what: str, deadline_s: float = 60, step_s: float = 1.0
) -> Any:
    """Poll ``check`` (async) until it returns a truthy value; fails with ``what`` and the last value."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    last: Any = None
    while loop.time() < end:
        try:
            last = await check()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - a probe may fail while things start
            last = repr(exc)
        await asyncio.sleep(step_s)
    raise AssertionError(f"timed out after {deadline_s}s waiting for: {what} (last: {last!r})")


def uid(instance: str, inverter: str, key: str) -> str:
    return f"deye_{instance}_{inverter}_{key}"


async def registry(ha: Any) -> list[dict[str, Any]]:
    return list((await ha.ws({"type": "config/entity_registry/list"}))[0])


async def ours(ha: Any, instance: str) -> dict[str, dict[str, Any]]:
    """Registry entries of one app instance by unique_id."""
    return {
        e["unique_id"]: e
        for e in await registry(ha)
        if e.get("platform") == "mqtt"
        and str(e.get("unique_id", "")).startswith(f"deye_{instance}_")
    }


def entity_id(instance: str, inverter: str, key: str, domain: str = "sensor") -> str:
    return f"{domain}.{uid(instance, inverter, key)}"


async def state_of(ha: Any, eid: str) -> str | None:
    states = await ha.states()
    return states[eid]["state"] if eid in states else None


def inverter_states(
    states: dict[str, dict[str, Any]], instance: str, inverter: str
) -> dict[str, str]:
    prefix = re.compile(rf"^(sensor|binary_sensor)\.deye_{instance}_{inverter}_(.+)$")
    return {
        m.group(2): s["state"]
        for eid, s in states.items()
        if (m := prefix.match(eid)) and not m.group(2).startswith("diag_")  # selected sensors only
    }


async def quiesce_and_check(app, fakes: dict, *, expect_traffic: bool = True) -> None:
    """End the app's traffic, finalise every stream of every fake, then check all of them:
    only valid FC03 frames, no unparsed bytes, exact byte accounting, traffic present."""
    import contextlib

    with contextlib.suppress(Exception):
        await app.terminate()
    for fake in fakes.values():
        await fake.stop()
    for name, fake in fakes.items():
        fake.wire.assert_clean(name=name, expect_traffic=expect_traffic)
