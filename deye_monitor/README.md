# Deye SG04LP3 Monitor

Home Assistant app for Deye SUN-5/6/8/10/12K-SG04LP3 (low-voltage, three-phase) hybrid inverters, single or in a parallel system. It reads the inverters, publishes the values through MQTT discovery and, if you release it, changes inverter settings.

- **Set up in the web interface.** The sidebar entry *Deye Monitor* opens a setup assistant. It creates connections and inverters with a read-only connection check, then asks how the plant is used and chooses the sensors per inverter.
- **Several connection types:** Modbus TCP, RTU over TCP, serial RTU and Solarman V5 (logger), with an optional fallback path per inverter. Only the devices you set up are contacted.
- **Read-only by default.** The app sends Modbus read requests (function code 03) until you release writing for an inverter and a setting; both are off by default.
- **Guarded settings control.** Released settings are written one at a time, either in the web interface (preview and confirmation) or through Home Assistant `number`, `switch`, `select`, `time` and `button` entities. Each write is checked against firmware limits and the current device state, then read back. The result is shown, and in Home Assistant it is also sent as an `event`.
- **Plain-language sensors.** Coded values read as text, with the raw word in the attributes. An (i) help explains the settings (all 128) and the 53 sensors whose meaning is not obvious; not every sensor has one.

See the *Documentation* tab (`DOCS.md`) for the options, the web interface, the list of settings and their rules.

Runbooks in the repository: `docs/LIVE-READ-RUNBOOK.md` (first live read) and `docs/FIRST-WRITE-RUNBOOK.md` (first field write).
