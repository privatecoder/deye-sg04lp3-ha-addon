# Deye SG04LP3 Monitor – Home Assistant app

Home Assistant app (add-on) for Deye SUN-5/6/8/10/12K-SG04LP3 three-phase LV hybrid inverters. It reads 1..n inverters over a Modbus TCP gateway, an RTU-over-TCP gateway, a local USB-RS485 adapter or a Solarman V5 WiFi logger and publishes them to Home Assistant via MQTT discovery.

> **Read-only by default.** Out of the box the app only sends Modbus FC03 read requests. Since 0.3.0 you can release writing **per inverter and per setting** (default: off, nothing released). Writing happens only in the web interface, one setting at a time, with a preview and a confirmation; there are no command topics and no writing entities in Home Assistant. Nine settings can be released: maximum battery charge and discharge current (R108, R109), generator and GRID charge current (R125, R128), GRID charge enable (R130 bit 0) and the four Smart Load thresholds (R134-R137, OFF/ON voltage and SOC). Everything else cannot be written. See the app's *Documentation* tab (“Writing settings”) and `docs/FIRST-WRITE-RUNBOOK.md` before the first write.

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
