# Deye SG04LP3 Monitor

> **Status: device loop with MQTT publisher (0.1.0, experimental).** The app validates its
> configuration, polls the inverters read-only (Modbus function code 03) and publishes the
> default sensor selection through MQTT Device Discovery: entities appear in Home Assistant. The
> Ingress web interface (sidebar: Deye Monitor) lets you describe your plant and choose sensors per
> inverter (see DOCS.md, “Web interface”).

Home Assistant app that reads one or more Deye SUN-5/6/8/10/12K-SG04LP3 (low-voltage,
three-phase) hybrid inverters and publishes the values through MQTT discovery.

- **Read-only.** It only sends Modbus function code 03 (read holding
  registers). There are no write paths, no command topics and no writing entities.
- **No scanning.** Only the gateways, loggers and serial devices you configure are contacted.
- **Several connection types:** Modbus TCP, RTU over TCP, serial RTU and Solarman V5
  (logger), with an optional fallback path per inverter.

See the *Documentation* tab (`DOCS.md`) for the current status, all options and an example for a
four-inverter plant.

First test against real devices: see `docs/LIVE-READ-RUNBOOK.md` in the repository (German).
