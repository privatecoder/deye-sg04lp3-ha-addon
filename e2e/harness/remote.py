"""Fake inverters in a container of the run's private network (for the Docker image test).

The image reaches them by container name, so nothing depends on how a host can be reached from a
container (bridge gateway, ``host.docker.internal``, firewall): the same path on Linux runners and
Docker Desktop. The wire record comes back when the container is stopped and is checked by the
same ``Wire.assert_clean`` as for in-process fakes."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from harness import docker
from harness.fakes import Kind, Read, Wire

PORT = {"mbap": 5021, "rtu": 5022, "v5": 5023}


class RemoteFake:
    def __init__(self, name: str, kind: Kind, regs: dict[int, int], host: "RemoteFakes") -> None:
        self.name, self.kind, self.regs, self.host = name, kind, regs, host
        self.port = PORT[kind]
        self.wire = Wire(kind)

    async def stop(self) -> None:
        self.host.stop()
        self.host.load_wires()


class RemoteFakes:
    def __init__(self, net: str, image: str, fakes: dict[str, tuple[Kind, dict[int, int]]]) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="deye-e2e-fakes-"))
        self.dir.chmod(0o777)
        self.fakes = {n: RemoteFake(n, k, r, self) for n, (k, r) in fakes.items()}
        self.name = f"deye-e2e-fakes-{uuid.uuid4().hex[:6]}"
        harness = Path(__file__).resolve().parent
        self.container = docker.Container(
            self.name,
            image,
            net,
            args=[
                "--entrypoint",
                "python3",
                "-v",
                f"{harness}:/fakes:ro",
                "-v",
                f"{self.dir}:/out",
            ],
            command=["/fakes/remote_fakes_main.py"],
        )
        self._stopped = False

    @property
    def host(self) -> str:
        return self.name

    def start(self, wait_s: float = 60) -> None:
        (self.dir / "config.json").write_text(
            json.dumps(
                [
                    {"name": f.name, "kind": f.kind, "port": f.port, "regs": f.regs}
                    for f in self.fakes.values()
                ]
            )
        )
        self.container.start()
        end = time.monotonic() + wait_s
        while not (self.dir / "ready").exists():
            if time.monotonic() > end or not self.container.running():
                raise RuntimeError(f"fake inverters did not start\n{self.container.logs()[-1500:]}")
            time.sleep(0.2)

    def stop(self) -> None:
        if not self._stopped and self.container.started:
            self.container.stop(timeout=30)
            self._stopped = True

    def load_wires(self) -> None:
        path = self.dir / "wire.json"
        if not path.exists():
            raise RuntimeError(
                f"no wire record of the fake inverters\n{self.container.logs()[-1500:]}"
            )
        for name, rec in json.loads(path.read_text()).items():
            wire = self.fakes[name].wire
            wire.reads = [Read(**r) for r in rec["reads"]]
            wire.raw = bytearray(rec["raw_len"])  # only its length is accounted for
            wire.violations = rec["violations"]
            wire.frames_bytes = rec["frames_bytes"]
            wire.pending_bytes = rec["pending_bytes"]
            wire.streams_closed = rec["streams_closed"]
            wire.streams_opened = rec["streams_opened"]

    def cleanup(self) -> None:
        self.container.remove()
        shutil.rmtree(self.dir, ignore_errors=True)
