# Changelog

## 0.1.0

- Plant setup in the web interface (T14): connections and inverters are created, edited and deleted
  in the Ingress interface (setup assistant for the first start, read-only connection check,
  transport chooser with explanations, serial device list, live adoption with rollback) and stored
  in `/data/plant.{a,b}.json`; the app options keep only `mqtt`, `log_level` and `instance_id`.
  `endpoints`/`inverters` in the options are taken over once (migration) and then only produce a
  hint.

- Initial packaging: configuration schema, translations (en, de), s6 service, container build.
- Protocol layer (Modbus FC03, Solarman V5), read-only guard, transports and bus scheduler.
- Device loop: identity binding and quarantine, polling groups, snapshots, fallback path,
  energy resolution state, capability checks.
- MQTT publisher: Device Discovery (sensor/binary_sensor only, no command topics), availability
  with will and inverter state, birth rejoin, persistent discovery inventory with cold-start
  cleanup, energy statistics series in entity ids, TLS options for external brokers (CA file,
  optional client certificate, `/ssl` mapped read-only), `birth_topic` option.
- Per-inverter poll intervals are four flat optional options (`poll_*_s`); `mqtt:` stays a required
  (possibly empty) block, as Home Assistant requires nested blocks.
- Island template (no utility grid) now includes load voltages, inverter frequency and the UPS/EPS
  total power; R65 is no longer selected by default (read as an energy dependency); R111 is a raw
  diagnostic; producer evidence for R621, R638 and R655; failed inverter tasks restart with backoff.
- Device loop hardening: connection epoch checked after every request, lifecycle token for
  in-flight results, fail-closed identity/energy stores (`blocked`), failure snapshots, energy
  epoch with the serial number, consecutive R65 confirmation.
- Sensor selection backend: `/data/selection.json` (revision, per-inverter plant features and
  deviations from the template, atomic change, previous document kept, migration reports unknown
  keys) and the Ingress JSON API (Supervisor peer only, user + CSRF token + `If-Match` revision for
  every change, strict schema, transfer preview, explicit identity/energy resets). The web
  interface itself follows.
- MQTT publisher fixes (code review 5): a Home Assistant birth is only a replay trigger, the
  cleanup ledger is emptied solely by the explicit purge action (API route `api/cleanup/purge`) and
  its size is limited with a visible error; `offline` is published (and the connection cut without
  DISCONNECT if that fails) at the end of every session; curated per-inverter diagnostic sensors
  (R65, energy resolution/series, capability conflicts, bus load, transport counters, ...); random
  reconnect jitter; the opaque energy series is covered by regression tests.
- Ingress web interface (German and English, light and dark, keyboard operable, no build chain and
  no external requests): overview with state, path, identity, hints and bus load per inverter;
  guided setup (plant features pre-filled from the device, grouped and searchable sensor list with
  descriptions, deviations from the template marked and individually resettable, expert raw
  values, live polling-load preview, atomic save with conflict handling); transfer to other
  inverters with a preview per target; maintenance (rebind identity, store resets, cleanup list)
  with typed confirmations. Profile entries carry a plain-language `description` (de/en); the sensor
  API accepts plant features and `restore_template` so that a draft is previewed and saved as one
  change. Browser tests run with Playwright (`pytest -m ui`).
