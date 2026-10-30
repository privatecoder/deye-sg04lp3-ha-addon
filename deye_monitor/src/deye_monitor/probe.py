"""Read-only connection test (T14): the identity read of one connection, shown to the user.

The test uses the normal guarded transport (``StreamTransport.read_holding``; the guard sits on its
only sending path), reads only the registers of the profile's identity group (function code 03) and
sends nothing else. A connection that is already part of the running configuration is used through
its own transport and bus scheduler (so the test is queued between the polling requests of the
inverters on that bus); a connection that is not saved yet gets a temporary transport that is closed
afterwards. Only the exception TYPE of a failure is reported, never frames or addresses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from deye_monitor.config import DEFAULT_PROFILE, EndpointConfig
from deye_monitor.identity import normalize_serial
from deye_monitor.profiles.decode import decode
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.hardware import capability_conflicts, load_hardware
from deye_monitor.profiles.plan import build_read_plan
from deye_monitor.profiles.schema import load_profile
from deye_monitor.protocol.modbus import ModbusExceptionResponse, ModbusFrameError
from deye_monitor.protocol.solarman_v5 import V5LoggerError
from deye_monitor.runtime import Runtime, make_transport
from deye_monitor.scheduler import BusScheduler, QueueFull
from deye_monitor.transport.base import (
    ConnectBackoff,
    ConnectionLost,
    RequestTimeout,
    ResyncFailed,
    StreamTransport,
    TransportError,
    default_identity_check,
)

log = logging.getLogger(__name__)

TEST_DEADLINE_S = 45.0  # for the whole test, on top of the per-request deadline of the transport
_CAPABILITY_WORDS = (0, 15, 17, 18, 20, 21, 22, 24)


class EndpointInUse(Exception):
    """The target is polled through a saved connection of another kind: it cannot be tested with
    different settings while that connection is active."""


@dataclass(slots=True)
class Target:
    """Where a test sends its requests."""

    transport: StreamTransport
    scheduler: BusScheduler
    temporary: bool


def classify(exc: BaseException) -> str:
    """Stable outcome key of a failed read (the UI turns it into advice)."""
    if isinstance(exc, ConnectBackoff | ConnectionLost):
        return "unreachable"
    if isinstance(exc, RequestTimeout):
        return "timeout"
    if isinstance(exc, ModbusExceptionResponse):
        return "device_refused"
    if isinstance(exc, V5LoggerError):
        return "logger_error"
    if isinstance(exc, ModbusFrameError | ResyncFailed):
        return "bad_frame"
    if isinstance(exc, QueueFull):
        return "bus_busy"
    if isinstance(exc, TransportError):
        return "transport_error"
    return "internal_error"


def live_target(runtime: Runtime, ep: EndpointConfig) -> Target | None:
    """The running transport of a saved connection that addresses the same target as ``ep``."""
    for saved in runtime.cfg.endpoints:
        if saved.canonical_key != ep.canonical_key:
            continue
        if saved.transport is not ep.transport or saved.logger_serial != ep.logger_serial:
            raise EndpointInUse(saved.id)
        return Target(runtime.transports[saved.id], runtime.schedulers[saved.bus], False)
    return None


def temporary_target(runtime: Runtime, ep: EndpointConfig) -> Target:
    transport = make_transport(
        ep,
        runtime.connector_factory(ep),
        runtime._clock,
        runtime._rand,
        default_identity_check,
    )
    scheduler = BusScheduler(gap_s=ep.request_gap_ms / 1000, max_queue=ep.queue_size)
    return Target(transport, scheduler, True)


def identity_blocks(max_read_registers: int) -> list[tuple[int, int]]:
    """The (start, count) blocks of the profile's identity group: exactly what the running
    application reads to identify an inverter."""
    profile = load_profile(DEFAULT_PROFILE)
    plan = build_read_plan(
        profile,
        frozenset(),
        Features(),
        max_read_registers=max_read_registers,
        energy_auto=False,
    )
    return [(b.start, b.count) for b in plan.blocks if b.group == "identity"]


async def _read_blocks(
    target: Target, unit: int, blocks: list[tuple[int, int]]
) -> tuple[dict[int, int], int, str | None]:
    words: dict[int, int] = {}
    ok = 0
    for start, count in blocks:

        def request(s: int = start, c: int = count) -> Awaitable[list[int]]:
            return target.transport.read_holding(unit, s, c)

        try:
            got = await target.scheduler.run(request)
        except (TransportError, ModbusExceptionResponse, ModbusFrameError, V5LoggerError) as exc:
            return words, ok, classify(exc)
        except QueueFull as exc:
            return words, ok, classify(exc)
        words.update(zip(range(start, start + count), got, strict=True))
        ok += 1
    return words, ok, None


def _model(words: dict[int, int]) -> dict[str, Any]:
    profile = load_profile(DEFAULT_PROFILE)
    hardware = load_hardware(profile.hardware)
    entry = profile.entry("rated_power")
    watts = decode(entry, words)
    conflicts = capability_conflicts(
        hardware, {a: words[a] for a in _CAPABILITY_WORDS if a in words}
    )
    r22 = words.get(22)
    return {
        "device_type": words.get(0),
        "mppt": None if r22 is None else r22 >> 8,
        "phases": None if r22 is None else r22 & 0xFF,
        "rated_kw": (
            round(float(watts) / 1000, 1)
            if isinstance(watts, int | float) and 1000 <= watts <= 20000
            else None
        ),
        "conflicts": [{"code": c.code, "detail": c.detail} for c in conflicts],
    }


async def run_test(
    runtime: Runtime,
    ep: EndpointConfig,
    unit: int,
    *,
    bound_serial: Callable[[], str | None] = lambda: None,
    expected_serial: str | None = None,
) -> dict[str, Any]:
    """Run the identity read; never raises for a device problem (that is the result)."""
    started = time.monotonic()
    target = live_target(runtime, ep)
    if target is None:
        target = temporary_target(runtime, ep)
    blocks = identity_blocks(ep.max_read_registers)
    outcome: str | None = None
    words: dict[int, int] = {}
    ok = 0
    try:
        async with asyncio.timeout(TEST_DEADLINE_S):
            words, ok, outcome = await _read_blocks(target, unit, blocks)
    except TimeoutError:
        outcome = "timeout"
    finally:
        if target.temporary:
            try:
                await target.transport.close()
            except Exception as exc:
                log.warning(
                    "temporary test connection did not close cleanly (%s)", type(exc).__name__
                )
    serial = (
        normalize_serial([words[a] for a in range(3, 8)])
        if all(a in words for a in range(3, 8))
        else None
    )
    if outcome is None and serial is None:
        outcome = "invalid_serial"
    bound = bound_serial()
    result: dict[str, Any] = {
        "ok": outcome is None,
        "outcome": outcome or "ok",
        "transport": ep.transport.value,
        "path": "temporary" if target.temporary else "live",
        "blocks": len(blocks),
        "blocks_ok": ok,
        "duration_s": round(time.monotonic() - started, 2),
        "serial": serial,
        "serial_vs_expected": (
            None if expected_serial is None or serial is None else serial == expected_serial
        ),
        "serial_vs_bound": None if bound is None or serial is None else serial == bound,
        "model": _model(words) if words else None,
    }
    return result
