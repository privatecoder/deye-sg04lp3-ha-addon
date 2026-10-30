"""The wire record of every fake inverter, after the app's traffic was ended and every stream was
finalised: only valid FC03 reads, no unparsed byte, exact byte accounting, real traffic."""

from __future__ import annotations

import json

import pytest

from conftest import ARTIFACTS
from tests.helpers import quiesce_and_check

pytestmark = pytest.mark.usefixtures("running")

KIND = {"deye_tcp": "mbap", "deye_rtu": "rtu", "deye_v5": "v5", "deye_ser": "serial"}


async def test_every_byte_sent_to_any_fake_is_a_valid_fc03_read(running):
    await quiesce_and_check(running.app, running.fakes)  # app stopped, PTY and sockets finalised
    summary = {}
    for name, fake in running.fakes.items():
        wire = fake.wire
        assert len(wire.reads) > 50, (
            name,
            len(wire.reads),
            "a read-only run must show real traffic",
        )
        assert {r.unit for r in wire.reads} == {1}
        assert all(1 <= r.count <= 60 for r in wire.reads), name  # configured max_read_registers
        assert all(r.start + r.count <= 1200 for r in wire.reads), name
        assert {r.kind for r in wire.reads} == {KIND[name]}
        summary[name] = {
            "reads": len(wire.reads),
            "bytes": len(wire.raw),
            "frame_bytes": wire.frames_bytes,
            "streams": wire.streams_closed,
            "distinct_blocks": len({(r.start, r.count) for r in wire.reads}),
        }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "wire.json").write_text(json.dumps(summary, indent=1))
