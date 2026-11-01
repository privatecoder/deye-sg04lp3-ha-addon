# Deye SG04LP3 Monitor

> **Status: device loop with MQTT publisher and opt-in write layer (0.4.0).** The app
> validates its configuration, polls the inverters read-only by default (Modbus function code 03) and publishes the
> chosen sensors through MQTT Device Discovery: entities appear in Home Assistant. The Ingress web
> interface (sidebar: Deye Monitor) sets up your connections and inverters (a setup assistant guides
> the first start), describes your plant and chooses the sensors per inverter (see DOCS.md,
> “Web interface”).

Home Assistant app that reads one or more Deye SUN-5/6/8/10/12K-SG04LP3 (low-voltage,
three-phase) hybrid inverters and publishes the values through MQTT discovery.

- **Read-only by default.** It sends Modbus function code 03 (read holding registers) unless you
  release writing for an inverter and a setting (both off by default). There are no command
  topics and no writing entities; writing exists only in the web interface, one setting at a time
  with preview and confirmation: maximum battery charge/discharge current, generator/GRID charge
  current, GRID charge enable and start SOC, the battery protection SOCs, the four Smart Load
  thresholds and the ToU schedule (38 settings in total, see `DOCS.md`, “Writing settings”).
- **No scanning.** Only the gateways, loggers and serial devices you set up are contacted.
- **Set up in the web interface.** Connections and inverters are created there (with a read-only
  connection check), not in the app options.
- **Several connection types:** Modbus TCP, RTU over TCP, serial RTU and Solarman V5
  (logger), with an optional fallback path per inverter.

See the *Documentation* tab (`DOCS.md`) for the current status, the options and the web interface.

Reading against real devices: see `docs/LIVE-READ-RUNBOOK.md`; the procedure of the first field write is kept in `docs/FIRST-WRITE-RUNBOOK.md` (both in the repository).
