# E2E acceptance (read-only phase)

A real Home Assistant Core (`ghcr.io/home-assistant/home-assistant:2026.10.4`, digest pinned in
`harness/docker.py`), Mosquitto 2.0.22 and the app against fake inverters, all on `127.0.0.1` /
a private Docker network (`172.30.32.0/23`, so the ingress proxy owns the real Supervisor peer
address). No real device or plant broker is ever contacted; nothing is pushed.

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
