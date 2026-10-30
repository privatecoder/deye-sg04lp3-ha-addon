"""Runs inside a container on the run's private network (see ``remote.py``): the fake inverters
of ``fakes.py`` listen there, so the app reaches them by container name like any device on its
network, on every platform. SIGTERM ends the fakes orderly and writes their wire records."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path

sys.path.insert(0, "/fakes")
from fakes import FakeInverter  # noqa: E402

OUT = Path("/out")


async def main() -> None:
    config = json.loads((OUT / "config.json").read_text())
    fakes = {
        c["name"]: FakeInverter(
            c["name"], c["kind"], regs={int(a): v for a, v in c["regs"].items()}
        )
        for c in config
    }
    for c in config:
        await fakes[c["name"]].start("0.0.0.0", c["port"])  # noqa: S104 - the private network
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    (OUT / "ready").write_text("1")
    await stop.wait()
    record = {}
    for name, fake in fakes.items():
        await fake.stop()
        w = fake.wire
        record[name] = {
            "kind": w.kind,
            "reads": [vars(r) for r in w.reads],
            "raw_len": len(w.raw),
            "violations": w.violations,
            "frames_bytes": w.frames_bytes,
            "pending_bytes": w.pending_bytes,
            "streams_closed": w.streams_closed,
            "streams_opened": w.streams_opened,
        }
    (OUT / "wire.json").write_text(json.dumps(record))


asyncio.run(main())
