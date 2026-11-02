# Deye SG04LP3 Monitor – Home Assistant app

Home Assistant app (add-on) for Deye SUN-5/6/8/10/12K-SG04LP3 three-phase low-voltage hybrid inverters, single or in a parallel system. It reads 1..n inverters over a Modbus TCP gateway, an RTU-over-TCP gateway, a local USB-RS485 adapter or a Solarman V5 WiFi logger and publishes them to Home Assistant through MQTT discovery. On request it also changes inverter settings, guarded and one at a time.

## Features

- **Setup in the web interface.** A setup assistant in the sidebar (Ingress) creates connections and inverters and checks each connection read-only. Only the devices you set up are contacted; there is no scanning.
- **Describe your plant, get matching sensors.** You describe how each inverter is used: grid, island or generator on the GRID port; battery and BMS; what is on the GEN port; PV inputs; parallel system; export policy; external meter or CT; external signal use. The app preselects matching sensors and settings from these answers. Values reported by the device are shown as hints but never change your answers.
- **Sensors in plain language.** PV, grid, load, UPS, GEN port, battery, BMS, temperatures, energy counters, alarms and status. States read as plain text, such as "Anti-Island Mode: On", not raw register words. An (i) explains every setting and the sensors whose meaning is not obvious (not every sensor has one). The state text is English and stable for automations; the German text is an attribute. The Web UI does not show live sensor values.
- **Read-only by default.** Out of the box the app only sends Modbus read requests (function code 03).
- **Opt-in settings control.** Writing is released per inverter and per setting; nothing is released by default. It covers battery charge and discharge limits, battery protection thresholds, grid and generator charging, Smart Load and AC coupling, the time-of-use schedule, export and peak shaving, external signal outputs, EV charging and further functions found on the inverter display. Each setting is checked against the firmware's own limits and the current device state before it is sent. It is then read back, and the outcome is reported.
- **Two ways to write.** In the web interface (preview and confirmation), or through Home Assistant `number`, `switch`, `select`, `time` and `button` entities, with the result as an `event`.
- **Parallel systems.** Settings the master distributes to its slaves are written on the master only. Settings each unit keeps for itself can be written per unit, and the app shows when units differ.

Limits and rules come from a static analysis of the inverter firmware (MCU1 and MCU2) and the vendor's Modbus documentation, plus your own entries in the plant description (such as export or connection limits) and deliberate application policy (such as the strict time-of-use ordering). Support for every SG04LP3 configuration is the goal of the app, not a claim that each electrical configuration has been field-tested; electrical effects are not tested by the app. Check the (i) help and the risk text of a setting before you release it.

## Installation

1. In Home Assistant open **Settings → Apps** (formerly Add-ons) → **App store** → menu **⋮ → Repositories**.
2. Add `https://github.com/privatecoder/deye-sg04lp3-ha-addon`.
3. Install **Deye SG04LP3 Monitor** and start it. The app options only hold MQTT, log level and instance id; see the app's *Documentation* tab.
4. Open the app's web UI from the sidebar. The setup assistant creates the connection and the inverter, checks the connection read-only and leads you to the plant description and sensor selection.

An MQTT broker is required. The Mosquitto app is used automatically if it is installed.

## Documentation

- App documentation: [`deye_monitor/DOCS.md`](deye_monitor/DOCS.md)
- Changelog: [`deye_monitor/CHANGELOG.md`](deye_monitor/CHANGELOG.md) and the [releases](https://github.com/privatecoder/deye-sg04lp3-ha-addon/releases)
- Specification and plan: [`docs/SPEC.md`](docs/SPEC.md), [`docs/PLAN.md`](docs/PLAN.md)
- Live read and first-write runbooks: [`docs/LIVE-READ-RUNBOOK.md`](docs/LIVE-READ-RUNBOOK.md), [`docs/FIRST-WRITE-RUNBOOK.md`](docs/FIRST-WRITE-RUNBOOK.md)

## Development

App sources live in `deye_monitor/` (Python 3.14, uv). Tests: `cd deye_monitor && uv run pytest`; broker and UI suites with `-m broker` / `-m ui`. Home Assistant schema checks live in `ha-schema-tests/`, and end-to-end acceptance with real Home Assistant Core in `e2e/` (Docker).

License: MIT.
