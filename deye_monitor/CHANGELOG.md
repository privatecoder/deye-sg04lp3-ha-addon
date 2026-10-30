# Changelog

## 0.2.0

- Setup in the web interface (T14): connections and inverters are created, edited and deleted in
  the Ingress interface (setup assistant for the first start, read-only connection check,
  transport chooser with explanations, serial device list, live adoption with rollback) and stored
  in `/data/plant.{a,b}.json`; the app options keep only `mqtt`, `log_level` and `instance_id`.
  `endpoints`/`inverters` in the options are taken over once (migration) and then only produce a
  hint. The app stays strictly read-only (`allow_writes` cannot be set in the interface).
- Manifest: default-valued keys flagged by the Home Assistant app linter were removed.
- Review 9 corrections of the plant change:
  - A rejected change (disk write or start failure) restores the accepted state exactly: selection
    records are put back as they were, the proposed generation neither publishes discovery nor
    retires entities, and it does not touch `identity.json`; an `expected_serial` is bound only
    after the plant is durable (also safe against a process kill in between).
  - If the accepted plant cannot be started again after a rejected change, nothing keeps running,
    the interface shows it and blocks further changes until the app is restarted.
  - A plant change cancels running connection checks before the generation is stopped; a check
    can no longer reopen a retired connection. Unsaved checks on an explicitly shared `bus_id` use
    that bus' scheduler.
  - A stale selection record of a deleted inverter is removed before the next plant change and at
    start, so a re-created inverter with the same id never inherits an old selection.
  - Editing a connection keeps its saved port; a saved change whose reload fails is not reported
    as failed and not sent again.
  - After a failed recovery every changing call (selection, transfer, rebind, resets, cleanup,
    plant, checks) is refused with `plant_recovery_failed` before any side effect and the
    interface is locked; reading keeps working and shows the inverters as stopped, restart needed.
  - A connection check resolves its saved connection inside the generation it runs in (a deleted
    one is `unknown_endpoint`, never a silent test of the old address).
  - A deletion whose selection cleanup could not be written shows a visible "cleanup pending" hint.
  - End-to-end: the wire check also covers the plant setup scenario's fake inverter.

## 0.1.0

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
