# Changelog

## 0.2.2

Second live read test (same SUN-10K, Solarman logger):

- R0 = 0x0005 is accepted like 0x0500: both mark the three-phase low-voltage hybrid family
  (kellerza/sunsynk `identity.py` `DEVICE_TYPES`; V105.8 names only 0x0500). The device type sensor
  shows the same value for both, the raw word stays in the diagnostics. Other codes (e.g. 0x0006,
  0x0600) are still a conflict.
- R24: every value except 0/1/2 and 0xFFFF (live: 5; V105.8 declares [0,4] and defines 0/1/2 only) is
  an unknown code, not a conflict: no "read battery values with care" warning, only a discreet hint
  with the raw value; the profile applies (1 battery input). A defined value that contradicts the
  profile (2 = two inputs) stays a conflict.
- The connection test no longer shows 255 MPPT / 255 phases for R22 = 0xFFFF: it says "MPPT/phases
  not reported" and marks the profile values as such.

## 0.2.1

Corrections from the first live read test (SUN-10K, firmware word R15 = 0x1175, Solarman logger):

- Energy resolution: R65 = 0xFFFF (and 0) is not a protocol version. Nothing is derived from it any
  more (it used to select 1 kWh and showed values ten times too high). Without a usable R65 and
  without an override the energy counters are not published; the overview and the sensor page ask
  for the choice (0.1 kWh, usual for the SG04LP3, or 1 kWh) with one click, which sets the
  inverter's existing `energy_resolution` override. Diagnostics: energy source `unknown`, status
  `r65_invalid`, event `energy_resolution_unknown`. A change of the resolution starts a new
  statistics series as before; a factor that an earlier version had taken from R65 = 0xFFFF counts
  as unknown after the update until the choice is made.
- R0, R22 and R24 reading exactly 0xFFFF mean "not reported by the device": no capability conflict
  (it used to show 255 MPPT / 255 phases), the dependent sensors are unknown, the profile applies
  (2 MPPT, 3 phases, 1 battery input) and the overview says so. Any other deviating value is still
  a conflict. The raw words stay in the diagnostics (`identity.capability_words`).
- R24 codes 3 and 4 (declared range [0,4], but only 0/1/2 are defined in V105.8) are no longer
  reported as a conflict; codes above 4 still are.
- Battery temperature (R586) depends on the main firmware (R15): 0x1151 reads signed16 x 0.1,
  0x1175 reads (raw - 1000) x 0.1 (live: raw 1176 = 17.6 C, as the BMS says). For another firmware
  the offset is applied only if raw x 0.1 is above 80 C and the offset value lies in -40..80 C. The
  decision is in the diagnostics (`identity.firmware_offsets`). The profile entry is now
  `profile_dependent`, not `confirmed`.
- Firmware words (main and communication board) are shown as hex (`0x1175`), not decimal.
- Live-1 points: the "accept the template unchanged" button works on the first start (the answers
  pre-filled from the device are saved with it), capability conflicts show the raw values, the
  Docker image test reaches its fake inverters by container name.
- BMS current limits R212/R213 are unchanged (bank sums are plausible).

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
  - The recovery lock is atomic for requests already in flight: selection edits that wait for the
    lock and bodies that are still being read are refused with `plant_recovery_failed` too.
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
