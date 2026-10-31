# Deye SG04LP3 Monitor – Home Assistant app

Home Assistant app (add-on) for Deye SUN-5/6/8/10/12K-SG04LP3 three-phase LV hybrid inverters. It reads 1..n inverters over a Modbus TCP gateway, an RTU-over-TCP gateway, a local USB-RS485 adapter or a Solarman V5 WiFi logger and publishes them to Home Assistant via MQTT discovery.

> **Read-only.** The app only sends Modbus FC03 read requests. There is no write path in this version; `allow_writes` exists in the options but has no effect.

## Installation

1. In Home Assistant open **Settings → Apps** (formerly Add-ons) → **App store** → menu **⋮ → Repositories**.
2. Add `https://github.com/privatecoder/deye-sg04lp3-ha-addon`.
3. Install **Deye SG04LP3 Monitor** and start it (the app options only hold MQTT, log level and instance id; see the app's *Documentation* tab).
4. Open the app's web UI from the sidebar: the setup assistant creates the connection and the inverter, checks the connection read-only and leads to the sensor selection. Until you save a selection, an inverter is only identified (serial number, status) — no measurement groups are read.

An MQTT broker is required (the Mosquitto app is used automatically if installed).

## Documentation

- App documentation: [`deye_monitor/DOCS.md`](deye_monitor/DOCS.md)
- First live read test (English): [`docs/LIVE-READ-RUNBOOK.md`](docs/LIVE-READ-RUNBOOK.md)
- Specification and plan (English): [`docs/SPEC.md`](docs/SPEC.md), [`docs/PLAN.md`](docs/PLAN.md)

## Development

App sources live in `deye_monitor/` (Python 3.14, uv). Tests: `cd deye_monitor && uv run pytest`; broker/UI suites with `-m broker` / `-m ui`; Home Assistant schema checks in `ha-schema-tests/`; end-to-end acceptance with real Home Assistant Core in `e2e/` (Docker).

License: MIT.
