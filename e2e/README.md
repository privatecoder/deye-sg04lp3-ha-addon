# E2E acceptance (read-only by default; the fakes also take the v1 write)

A real Home Assistant Core (`ghcr.io/home-assistant/home-assistant:2026.10.4`, digest pinned in
`harness/docker.py`), Mosquitto 2.0.22 and the app against fake inverters, all on `127.0.0.1` /
a private Docker network (`172.30.32.0/23`, so the ingress proxy owns the real Supervisor peer
address). No real device or plant broker is ever contacted; nothing is pushed.

Write layer (0.3.0): the fake inverters understand exactly one write, FC16 with one register (the
word is stored and echoed; `reject_writes` / `silent_writes` simulate a device exception and a lost
acknowledgement). `Wire.assert_clean` fails on **any** write unless a test passes `allow_writes=True`,
so every acceptance run also proves that the app writes nothing while writing is not released.

```sh
cd e2e
uv sync --frozen
uv run pytest            # about 30-40 minutes; includes waits for the recorder's 5-minute statistics
uv run pytest -m "not slow and not image"   # without the recorder wait and the image run
```

Parallel runs are possible: every run has a run id (label `deye-e2e.run=<id>`, lock file in the
temp directory held with `flock`); the start-up cleanup removes only resources of runs whose lock is
no longer held, never those of a live run. Networks are chosen by Docker; only the image test needs
the fixed Supervisor subnet (`172.30.32.0/23`, the real ingress peer address) and waits for it.

Files: `harness/` (Docker, Home Assistant onboarding/API, fakes with strict frame decomposition,
app launcher), `tests/test_01..08, 99`. `E2E_IMAGE_PLATFORM=linux/amd64` builds the app image for
that platform. `artifacts/` (ignored by git) receives versions, recorder and wire summaries and
the app logs. 
