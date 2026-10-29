"""Docker plumbing of the E2E run: private networks, loopback-only published ports and a run id on
everything that is created, so that a run removes exactly its own resources (and aborted runs of
this machine, proven dead by their released lock file) and never those of a live run."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Every run owns its resources through a unique run id: labels carry it, a lock file held with
# flock for the life of the process proves that the run is alive. A start-up cleanup removes only
# resources of runs whose lock is no longer held (aborted runs of this very machine), never those of
# a live run.
RUN_ID = uuid.uuid4().hex[:8]
RUN_LABEL = f"deye-e2e.run={RUN_ID}"
LABEL = RUN_LABEL  # kept as the name used by the builders of containers, networks and images
LOCK_DIR = Path(tempfile.gettempdir()) / "deye-e2e-locks"
# The Supervisor address of the ingress peer check is fixed (SUPERVISOR_PEER): only one run at a time
# can own that network; every other run uses networks chosen by Docker.
SUBNET = "172.30.32.0/23"
SUPERVISOR_PEER = "172.30.32.2"
_held: list[object] = []

MOSQUITTO = "eclipse-mosquitto:2.0.22"
MOSQUITTO_DIGEST = "sha256:09b62ff32c2e7b64d4b431b9a89236073289b68db273de2b089edeb9b0d92b5c"
HOME_ASSISTANT = "ghcr.io/home-assistant/home-assistant:2026.10.4"
HOME_ASSISTANT_DIGEST = "sha256:23f4dfbb69ce76315dbc4de7915504f1c2dcecd7ca2648113a493a385972e133"  # as pulled (arm64 host)


class DockerMissing(RuntimeError):
    pass


def run(*args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc


def require_docker() -> None:
    if shutil.which("docker") is None or run("info", check=False, timeout=30).returncode != 0:
        raise DockerMissing("docker is not available")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def image_digest(image: str) -> str:
    out = run("image", "inspect", image, "--format", "{{json .RepoDigests}}").stdout
    digests = json.loads(out)
    return str(digests[0].split("@")[-1]) if digests else "none"


def image_arch(image: str) -> str:
    return run("image", "inspect", image, "--format", "{{.Architecture}}").stdout.strip()


def _flock(path: Path, *, wait_s: float = 0.0) -> object | None:
    """An exclusive flock on ``path`` (kept open); ``None`` if somebody else holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    end = time.monotonic() + wait_s
    while True:
        fh = open(path, "a+")  # noqa: SIM115 - kept open on purpose
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            fh.close()
            if time.monotonic() >= end:
                return None
            time.sleep(2)


def acquire_run_lock() -> None:
    """Mark this run alive for as long as the process lives."""
    fh = _flock(LOCK_DIR / f"{RUN_ID}.lock")
    assert fh is not None
    fh.truncate(0)  # type: ignore[attr-defined]
    fh.write(str(os.getpid()))  # type: ignore[attr-defined]
    fh.flush()  # type: ignore[attr-defined]
    _held.append(fh)


def remove_resources(run_id: str) -> None:
    label = f"deye-e2e.run={run_id}"
    ids = run("ps", "-aq", "--filter", f"label={label}", check=False).stdout.split()
    if ids:
        run("rm", "-f", "-v", *ids, check=False)
    for net in run("network", "ls", "-q", "--filter", f"label={label}", check=False).stdout.split():
        run("network", "rm", net, check=False)
    for image in run("images", "-q", "--filter", f"label={label}", check=False).stdout.split():
        run("rmi", "-f", image, check=False)


def remove_orphans() -> list[str]:
    """Resources of aborted runs: their lock file is not held any more. A live run (lock held) and
    anything without a run label is never touched. Returns the run ids that were cleaned up."""
    cleaned: list[str] = []
    for lock in sorted(LOCK_DIR.glob("*.lock")) if LOCK_DIR.exists() else []:
        run_id = lock.stem
        if run_id in (RUN_ID, "supervisor-net"):
            continue
        fh = _flock(lock)
        if fh is None:
            continue  # alive: not ours to touch
        try:
            remove_resources(run_id)
            lock.unlink(missing_ok=True)
            cleaned.append(run_id)
        finally:
            fh.close()  # type: ignore[attr-defined]
    return cleaned


def remove_own_resources() -> None:
    remove_resources(RUN_ID)
    (LOCK_DIR / f"{RUN_ID}.lock").unlink(missing_ok=True)


def own_resources_left() -> list[str]:
    left = run("ps", "-aq", "--filter", f"label={RUN_LABEL}", check=False).stdout.split()
    left += run("network", "ls", "-q", "--filter", f"label={RUN_LABEL}", check=False).stdout.split()
    return left


class SupervisorNetworkBusy(RuntimeError):
    pass


def hold_supervisor_network(wait_s: float = 1200) -> None:
    """Exclusive use of the fixed Supervisor subnet (only the image test needs it)."""
    fh = _flock(LOCK_DIR / "supervisor-net.lock", wait_s=wait_s)
    if fh is None:
        raise SupervisorNetworkBusy(
            f"another E2E run holds the Supervisor network {SUBNET} for more than {wait_s:.0f}s"
        )
    _held.append(fh)


def release_supervisor_network() -> None:
    for fh in list(_held):
        if getattr(fh, "name", "").endswith("supervisor-net.lock"):
            fh.close()  # type: ignore[attr-defined]
            _held.remove(fh)


def _subnet_holders(subnet: str | None) -> list[str]:
    if not subnet:
        return []
    out = []
    for net in run("network", "ls", "-q", check=False).stdout.split():
        info = run("network", "inspect", net, "--format", "{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}} {{.Labels}}", check=False).stdout.strip()
        if subnet.split("/")[0].rsplit(".", 2)[0] in info:
            out.append(info)
    return out


@dataclass
class Net:
    """A private bridge network of this run. Without ``subnet`` Docker picks a free one."""

    subnet: str | None = None
    ip_range: str | None = None
    name: str = field(default_factory=lambda: f"deye-e2e-{RUN_ID}-{uuid.uuid4().hex[:6]}")

    def create(self, wait_s: float = 0.0) -> None:
        """Create the network. A fixed subnet that another party holds is waited for (up to
        ``wait_s``), never taken away; the holder is named in the error."""
        cmd = ["network", "create", "--driver", "bridge", "--label", RUN_LABEL]
        if self.subnet:
            cmd += ["--subnet", self.subnet]
        if self.ip_range:
            cmd += ["--ip-range", self.ip_range]
        end = time.monotonic() + wait_s
        while True:
            proc = run(*cmd, self.name, check=False)
            if proc.returncode == 0:
                return
            if "overlap" in proc.stderr and time.monotonic() < end:
                time.sleep(15)
                continue
            raise RuntimeError(
                f"cannot create network {self.name} ({self.subnet}): {proc.stderr.strip()[:300]}; "
                f"held by: {_subnet_holders(self.subnet)}"
            )

    def remove(self) -> None:
        run("network", "rm", self.name, check=False)


@dataclass
class Container:
    name: str
    image: str
    net: str
    args: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    ip: str | None = None
    published: dict[int, int] = field(default_factory=dict)  # container port -> loopback port (assigned)
    started: bool = False

    def start(self) -> None:
        cmd = ["run", "-d", "--name", self.name, "--label", LABEL, "--network", self.net]
        if self.ip:
            cmd += ["--ip", self.ip]
        for inner, outer in self.published.items():
            # loopback only; 0 = Docker picks the host port, otherwise it is fixed (the app is
            # configured with it and must find the broker again after a restart)
            cmd += ["-p", f"127.0.0.1:{outer or ''}:{inner}"]
        cmd += self.args + [self.image] + self.command
        run(*cmd, timeout=180)
        self.started = True
        self.read_ports()

    def read_ports(self) -> None:
        """The host ports Docker assigned (re-read after every start)."""
        for inner in list(self.published):
            out = run("port", self.name, f"{inner}/tcp", check=False).stdout.strip()
            if out:
                self.published[inner] = int(out.splitlines()[0].rsplit(":", 1)[1])

    def stop(self, timeout: int = 10) -> None:
        run("stop", "-t", str(timeout), self.name, check=False, timeout=timeout + 30)

    def kill(self) -> None:
        run("kill", self.name, check=False)

    def restart(self) -> None:
        run("start", self.name)
        self.read_ports()

    def remove(self) -> None:
        run("rm", "-f", "-v", self.name, check=False)

    def logs(self, tail: int = 200) -> str:
        proc = run("logs", "--tail", str(tail), self.name, check=False)
        return proc.stdout + proc.stderr

    def running(self) -> bool:
        return run("inspect", "-f", "{{.State.Running}}", self.name, check=False).stdout.strip() == "true"


def wait_for(predicate, what: str, deadline_s: float, step_s: float = 0.5):  # type: ignore[no-untyped-def]
    """Poll ``predicate`` until it returns something truthy; the failure names ``what``."""
    end = time.monotonic() + deadline_s
    last: Exception | None = None
    while time.monotonic() < end:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - readiness probes may fail while starting
            last = exc
        time.sleep(step_s)
    raise TimeoutError(f"timed out after {deadline_s}s waiting for {what} ({last!r})")


PROXY = "caddy:2.11.7-alpine"
PROXY_DIGEST = "sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f"


def ensure_images() -> dict[str, str]:
    """Pull what is missing and verify the pinned digests; returns image -> digest for the report."""
    seen: dict[str, str] = {}
    for image, digest in ((MOSQUITTO, MOSQUITTO_DIGEST), (HOME_ASSISTANT, HOME_ASSISTANT_DIGEST), (PROXY, PROXY_DIGEST)):
        if run("image", "inspect", image, check=False).returncode != 0:
            run("pull", image, timeout=1500)
        actual = image_digest(image)
        if digest is not None and actual != digest:
            raise RuntimeError(f"{image}: digest {actual} differs from the pinned {digest}")
        seen[image] = actual
    return seen
