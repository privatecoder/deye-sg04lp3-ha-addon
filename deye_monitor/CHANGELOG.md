# Changelog

## 0.3.2

Owner collection round.

- **Writes, delayed peer observation**: field fact: R108 written on the master reached all slaves
  within about 18 s (the 1 s observation still showed the old value). After the 1 s observation
  the service now reads the other inverters once more at 30 s (read only, same wording
  same / different / unavailable, recorded in the audit log as a later observation, not as an
  attribution). The result shows “Before / After 1 s / After 30 s” with a countdown; the write
  serialisation is not held while it waits. New button “Read other inverters again” in the result
  and in the log entry: a fresh read-only observation, appended to the audit log. Matching values
  still do not prove synchronization. A pending later observation is cancelled with its
  generation and shown as interrupted after a restart.
- **Writes, slaves are locked**: firmware evidence (master copies the whole settings block
  R65-R209 and R339-R429, all nine settings included, to every slave; a slave's differing value is
  overwritten by a later master snapshot) plus the two field anchors (R108, R130). Owner decision:
  a parallel slave is not written. The role is read fresh at the preview and at the send point;
  slave or unreadable role is rejected as `parallel_slave` (no permit, no FC16), a single inverter
  or parallel mode off is unaffected, and the lock follows the current role. The dialog and the
  plant page say so with a link to the master; the release checkboxes of a slave are disabled. On
  the master the app says that the value is distributed (usually within about 20 s in the owner's
  plant, not guaranteed). Persistence across a restart is still unproven.
- **Inverter page**: “Change settings” sits directly below the page head, above the feature and
  sensor steps.
- **Plant card**: the member list has a gap between its items.
- **Sensor catalogue**: no longer offered (and dropped from stored choices): DC transformer
  temperature R540 (firmware writes a fixed 25.0 °C), BMS off-grid charge/discharge limits
  R218/R219 (firmware writes literal 0) and load frequency R655 (duplicate of R638). New notices
  in the picker: R211 BMS discharge voltage (depends on the BMS protocol) and the selected grid
  power values R622-R625/R690 (possibly only a copy of the internal grid power).
- **R0**: 0x0005 and 0x0500 are two equal documented words of the three-phase LV hybrid family
  (MCU1 C070 writes the family word as the bytes 00 05). The hardware profile field
  `also_accepted_device_types` became `device_types: [0x0500, 0x0005]`.

## 0.3.1

- **Sensor list**: an input of a plant total with alternative sources (battery SOC and temperature:
  BMS value first, inverter value as fallback) is marked "Plant total (fallback)" while a preferred
  source is selected; only the source actually used carries the plain "Plant total" chip.

## 0.3.0

**Write layer v1 (opt-in).** The app stays read-only by default. Per inverter the owner can now
release writing for individual settings; everything is written from the web interface only.

- **Nine settings**: maximum battery charge current (R108) and discharge current (R109), generator
  charge current (R125), GRID charge current (R128), GRID charge enable (R130 bit 0, all other bits
  preserved) and the Smart Load thresholds OFF/ON voltage (R134/R136) and OFF/ON SOC (R135/R137).
  Nothing else can be written; protection thresholds (R115-R117), work mode, time-of-use, the clock
  and everything else are excluded and refused by the send guard.
- **Per inverter and per setting, default off.** New inverter fields `allow_writes` (master switch)
  and `enabled_write_operations`; both are needed. Set on the plant page (card section “Writing”,
  with register, range and risk note per setting); changing connection, address, fallback or the
  expected serial number withdraws the release. The inverter form, the sensor selection, “apply
  to all” and the transfer never grant write authority.
- **Release bound to the firmware.** Releasing writing stores the firmware the inverter shows (R11, R14, R15, R17, R18 and the hardware profile). Preview and the send point require an exact match; after a firmware change writing is refused (“Firmware changed since release”) until you release again. Monitoring is unaffected.
- **Complete audit record before every send.** The audit line is written with a checked loop (short writes are completed, no progress blocks the send); an interrupted earlier line cannot corrupt the next record.
- **Model-specific current cap** from the vendor datasheet (5K 120 A, 6K 150 A, 8K 190 A, 10K
  210 A, 12K 240 A), chosen from the reported rated power (R20/R21); it replaces the generic 185 A
  of the protocol document. An unknown model, or a stored value that could not be restored inside
  the range, locks the setting.
- **“Change settings” dialog** (inverter page): fresh read of the enabled settings, preview (old →
  new, raw word and mask, risk note, the other inverters' values before the change), confirmation
  with a single-use operation id (30 s), result with the read-back, the other inverters' values
  after the change (same / different / unavailable), your own display check, and the audit list.
  “Writes this inverter only. Other inverters are observed; matching values do not prove
  synchronization.” There is no preselected write target in a parallel plant.
- **Safety**: exactly one FC16 with one register per operation through the lowest byte guard (FC06
  never, no retry, no fallback endpoint, no fan-out, no rollback); the whole register is read again
  inside one bus critical section and any change since the preview is a conflict; permits are
  bound to user, plant revision, enablement, inverter, serial number, firmware words, role, route,
  unit and connection and die with a restart, a plant change or a release change; writes are
  serialised with at least 5 s between them; a durable audit log
  (`/data/write-audit.jsonl`) is written before the frame is sent, and a log that cannot be
  written blocks the send. Statuses: `rejected`, `sent`, `failed`, `readback_confirmed`,
  `outcome_unknown`. A read-back proves the value in MCU1's settings memory only.
- No MQTT command topics, no writing Home Assistant entities, no scheduled, start-up or reconnect
  writes.
- Documentation: DOCS.md “Writing settings”, SPEC §10 as the accepted v1 contract with the owner
  decisions, and the owner-only `docs/FIRST-WRITE-RUNBOOK.md`.
- Tests use fake devices only; the test fakes (and the E2E fakes) store FC16 words, and the E2E
  acceptance fails on any unexpected write.

## 0.2.13

- **One source per battery value.** With BMS communication the template selects the BMS values
  (SOC, temperature, voltage, current, charge/discharge current limits, charge voltage) and no
  longer the inverter's battery SOC and temperature, which read the same source. Inverter battery
  voltage, current, power and energies stay (own measurement per unit). Without BMS nothing
  changes. BMS discharge voltage and the two BMS off-grid limits stay selectable, not preselected.
- **Presets for all wizard combinations** (from the preset design): utility grid values only for a
  used GRID port (utility or generator), the selected grid power and utility energy counters only
  for the public grid, the internal GRID-terminal power for a generator on the GRID port, inverter
  frequency for an unused or generator GRID port, load voltages L1-L3 always, UPS power total not
  preselected (equals the load power off-grid), parallel role and serial number only with
  parallel operation. The raw alarm and fault words 1-4 are off by default (the decoded warnings
  and faults stay); the named alarm bits stay on. "Basics only" now selects serial number,
  operating state, active warnings and active faults.
- **Plant totals**: one "Plant battery SOC" and one "Plant battery temperature" (the master's BMS
  value when selected, else its inverter value). "Plant BMS SOC" and "Plant BMS temperature" are
  gone (the entities are retired from discovery).
- **Plant-total inputs are marked** in the sensor list ("Plant total" chip, tooltip, inline
  warning when deselected). The list comes from the totals definition. The "only 0" hint no longer
  fires for them on a plant member (a master that supplies no load reads 0 legitimately).
- "Relais Netzeinspeisung" is now "Relais Netzversorgung" (English unchanged: "Grid supply
  relay"); the description says the exact function of the V105.8 bit "grid give power to relay" is
  not proven.

## 0.2.12

- **Energy entity names without the series**: "Battery charge total (g0)" is now "Battery charge
  total" (inverter and plant energy entities). The series stays in `unique_id`, the entity id and
  the template guard (statistics identity unchanged) and is exposed as the attribute
  `energy_series` of each energy entity. After a series change the retired entity is removed, so
  two entities with the same name do not coexist.
- **BMS names**: "Plant battery power (BMS bank view)" is "Plant battery power (BMS)". "BMS
  current (bank)" and "Plant BMS current (bank)" keep "(bank)" to tell them from "Battery
  current"; their description says it is the whole bank's current, positive = charging.
- Includes the 0.2.11 CI fix (server default `bms` true).

## 0.2.11

- The server-side feature default `bms` is now `true` too (the wizard shows the server's draft, so the client default alone had no effect). A plant without a saved answer gets the BMS sensors in its template (65 instead of 59 for the default plant).

- **Setup wizard, BMS question**: "Does the battery talk to the inverter (BMS cable)?" now
  defaults to "Yes, with communication" when a battery is present (it was "No", which deselected
  all BMS sensors). The device setting R98 (battery type) is read as a suggestion like the other
  plant answers: lithium suggests "yes", lead suggests "no" ("read from a device setting – please
  confirm"). An unknown value suggests nothing. On parallel slaves the BMS sensors stay inactive
  through the R336 rule, a "yes" does not bring them back.

## 0.2.10

- **Fix**: with no inverters configured the plant page showed the text `null` below "Add
  inverter". The page handed a missing totals section to the native DOM `append`, which prints
  it; it now goes through the `append` helper that skips empty values. No other view had the
  pattern. New browser tests check that no page shows `null` or `undefined`.

## 0.2.9

Small fixes from the 0.2.8 field check.

- **Firmware MCU2 corrected**: the third word is R11 (control board bootloader / assistant program
  version), not R16. The control board now reads `2005-1175-1807` like the Deye cloud
  (R14-R15-R11). MCU1 (R17-R18) is unchanged. R11-R18 are read as one identity block (R12, R13 and R16
  are internal gap entries), so the number of read blocks is unchanged.
- **Plant page, "Anlage (Summen)"**: one checkbox per line, labelled with the inverter name only.
- **Plant master** (diagnostic on the app device) shows the inverter name; the id is the attribute
  `inverter_id`.

## 0.2.8

Named warnings and faults from the Deye alarm list (5-12 kW).

- **Active warnings / Active faults**: two text sensors per inverter (default on) with the active
  codes and names, e.g. `F29 Parallel CANBus fault, F41 Parallel system stop`, `none` when nothing
  is set. The attribute `alarms` carries `code`, `name` and a short `remedy`. Names and remedies are
  in `profiles/alarms.yaml` (W2, W3, W4, W31, W32, F1-F64); an unnamed bit shows its code only.
- **New binary sensor** "Meter communication warning (W4)" (R553 bit 3), off by default.
- **W31** is now named from the source (Battery_comm_warn); the "inferred" note is gone.
- The existing per-bit entities (F13, F29, F41, ...) are unchanged.

## 0.2.7

Totals over all inverters of the installation (live context: four SG04LP3 in off-grid parallel with
one shared battery bank).

- **Plant totals on the app device** "Deye Monitor": PV, battery, load and inverter output power;
  PV, battery charge/discharge and load energy (`total_increasing`, usable in the Home Assistant
  Energy dashboard); optional "today" sums (measurement only). A member with a missing or stale
  value makes the total unknown, never a partial sum.
- **Plant energy series** (`g<n>`, never reused, persisted in `/data/plant-totals.json`): a new
  series starts when the membership changes or any member starts a new energy series, so the plant
  total never drops or jumps. Discovery cleanup retires old series like inverter series.
- **Battery state from the master only**: plant battery SOC and BMS SOC, voltage, current (bank) and
  temperature come from the member that reports itself as parallel master (R336); unknown without
  one. Battery power is also shown from the master's BMS bank view next to the sum of the members'.
- **Membership**: the section "Plant (totals)" on the plant page (checkbox per inverter, default
  all; stored in the plant file). Fewer than two members: no plant entities unless enabled.
  Overview: new plant card with totals and member status. Diagnostics: members, stale members,
  master, series.
- **Profile (cloud export findings)**: R216 is now `bms_current`, "BMS current (bank)": signed, 1 A,
  positive = charging (opposite to R591), default sensor. The former raw entry `bms_current_raw`
  is gone (a saved selection that contained it is reported and cleaned like any obsolete key). The
  warning bit R554 bit 14 is named "Battery communication warning (W31)" (the key stays
  `alarm_battery_missing`); the W number is inferred from the bit position.
- **Firmware as two diagnostics**: "Firmware MCU1" (`1001-C070`, R17-R18) and "Firmware MCU2"
  (`2005-1175-1807`, R14-R16) replace "Firmware". "Main firmware version" (R15) is part of MCU2 and
  no longer a default sensor (still selectable).
- **Fix (review 13, R13-01)**: a pass in which R336 cannot be read no longer clears a confirmed
  parallel-slave verdict (and no longer emits the slave's invalid battery temperature). The role is
  tri-state (slave / master or standalone / unknown); unknown keeps the verdict and infers no slave
  at the first start; a valid master/standalone clears it, a valid slave checks the BMS block again.
- The BMS all-zero check reads in chunks when the endpoint's read limit is smaller than the block.

## 0.2.6

The Modbus address depends on the connection type (field finding: four SG04LP3 in parallel).

- Through an RS485 gateway the address is the inverter's parallel “Modbus SN” (master 1, slaves 2,
  3, …); through a Solarman V5 logger it is always 1, also for slaves.
- Inverter form: per route the address help text follows the connection type; an unedited address
  is prefilled with 1 again when the connection is switched; an unedited fallback address is 1 for
  a logger fallback. A logger route with an address other than 1 shows a non-blocking warning, also
  in the connection check after a failed probe.
- Config and API: an unset `fallback_unit_id` defaults to 1 if the fallback endpoint is a Solarman
  V5 logger (otherwise still the primary's `unit_id`). Explicit values are untouched.
- "Check connection" of an inverter tests every configured route (primary and fallback, each with
  its own address) and shows one result block per route, so a failing fallback is visible even
  when the primary works.
- Intermittent check failures: a check no longer gives up on the first exchange. A transient
  failure (unreachable, timeout, bad frame, logger error) is retried once after a short pause on a
  fresh test connection; a running connection in failure cooldown is waited for (up to 10 s)
  instead of failing immediately. The result shows the number of attempts.
- **Overview hints** (live finding, master 1): "… and N more hints" is now a button that opens and
  closes the rest (keyboard operable, state kept while the page is open). Order: warnings, then
  information, then observations. "Hardware data not reported" and "unknown battery input code"
  are one hint, with the raw words (`R22=0xFFFF, R24=5`).
- **No false "only 0" hints.** The zero heuristic is now opt-in per profile entry
  (`observe_zero`, default by category): only physical measurements that should not be 0 in
  operation are watched (voltages, frequency, SOC, temperatures, PV by daylight only, and the
  explicit `load_power_total`). Alarm/fault/status words, bit fields, enums, raw words, binary
  sensors, "today" counters, gen-port values, settings and placeholders are never reported.
- **Transfer dialog:** the preview loads when the dialog opens and whenever targets or options
  change; "Transfer" is usable at once (the separate "Preview" button is gone). The stale-revision
  protection is unchanged. The preview names keys that stay in the target's selection but are
  inactive there (parallel slave).
- **Parallel slaves** (identified by the device-reported R336 role only: parallel on and not
  master): the battery temperature (R586, valid on the master only) and, when the BMS block
  R210-R219 reads entirely 0, the BMS entries are inactive: not polled, no entity, greyed in the
  selection with the reason. Re-checked at every identity pass, so a role change takes effect.
  Independent inverters, cascades and masters keep everything. The saved selection is untouched.
- **Role display:** "Master" / "Slave" only (the phase stays in the diagnostics).
- **One place for the Modbus address.** A Solarman V5 route has no address field ("Address: 1,
  fixed for Solarman loggers"; an override sits under Advanced), the fallback prefill on a logger
  is 1. The connection check does not look like a stored setting: with inverters on the connection
  each is tested with its own address, otherwise the field is "Address to test (not saved)"; a
  logger needs none.
- **No migration paths** (development only): removed the `endpoints`/`inverters` app options (and
  their translations, import code, notices, docs and the YAML runbook appendix), the import of the
  first selection-file format and of `selection.json`/`.prev`/`.recovery.json`, and the default
  for selection records without the `chosen` flag or without `confirmed`. Options hold MQTT, log
  level and instance id. Stored state of older versions may need to be reset.
- **Per-inverter diagnostics moved to the inverter's own device** (no name prefix: "Bound serial
  number", "Connection path", "State", …; own availability, so they stay visible while the
  inverter is offline). The app device keeps app version, inverters configured/online and MQTT
  reconnects. The old app-device entities are removed by the normal discovery cleanup.
- Removed the diagnostic "parallel role (R336)" (duplicated the inverter's own parallel entities).
- R65 diagnostics ("Energy resolution raw word", "last reading") exist only while R65 is read and
  reported (not with a user-chosen energy resolution).
- **Firmware like the Deye cloud:** `MCU1: 1001-C070 / MCU2: 2005-1175-1807` (MCU1 = R17-R18,
  MCU2 = R14-R15-R16; R16 is read with the identity block). The single-word entities are unchanged.
- The HA device model is "Deye SG04LP3 three-phase low-voltage hybrid inverter"; no register
  numbers in entity names ("(R133)" etc. removed from the profile names, also the R65 diagnostics).

## 0.2.5

Small UI and test hardening.

- The identifier of an existing connection or inverter is shown as locked in the edit dialog
  (greyed, read-only, `aria-readonly`, lock icon and "cannot be changed" text). The value is still
  submitted as before; the server keeps rejecting changes.
- Tests: fixed a race in the UI test for a saved connection whose refresh fails; regression tests
  for the reset of unreported fields on identity changes, the same-connection refresh after a
  firmware change, and discovery removal and restoration of unreported entities.

## 0.2.4

Display polish from the live test (master 1 through the gateway, firmware 1175: R22 = 0xFFFF,
R24 = 5, R65 = 0xFFFF).

- **Breaking for automations:** enum states are now readable labels instead of snake_case, for every
  enum sensor (for example `smart_load` is now `Smart Load`, `three_phase_lv_hybrid` is
  `Three-phase LV hybrid`, `lithium` is `Lithium`, `normal` is `Normal`, `off`/`on` are `Off`/`On`).
  The discovery `options` list the labels. Update templates and automations that compare the old
  values.
- Fields the device does not report are checked once at the identity read (and again after a change
  of serial number, R15/R17/R18 or profile revision) and then no longer polled or published. This is
  per field and only for profile entries that declare a not-reported value (R0, R22, R24, R65), never
  a global 0xFFFF rule. The result is kept per inverter (diagnostics `unreported`: key, register, raw
  value); existing selections stay valid, the field is just inactive and greyed out in the sensor
  selection ("not reported by the device (R22=0xFFFF)"). Discovery of such an entity is removed like
  a deselected one.
- MPPT count and Phase count show the profile value (SUN-10K: 2 MPPT, 3 phases) while R22 is not
  reported; the entity attribute `source` is `profile` (otherwise `device`).
- R24 is now "Battery inputs" (a number): a defined code gives the device value, an undefined code
  (anything except 0/1/2) or 0xFFFF gives the profile value 1 with `source: profile`. The raw code
  is only in the diagnostics (`identity.capability_words`), no longer an own entity. The key
  `battery_input_code` is unchanged.
- Firmware words are shown as hex digits without prefix, upper case, four digits: `1175`, `1001`,
  `C070` (entities and the diagnostics `r15=1175 r17=1001 r18=C070`).
- `alarm_parallel_comm` (R554 bit 15) is named "Parallel communication warning" (key unchanged).
- The entities "R65 raw value" and "R65 last reading" are only created while R65 is reported. The
  derived "Energy resolution" stays, its source reads `user choice` or `R65`.

## 0.2.3

Findings from comparing the sources (community projects, document history V100.0 to V105.8, firmware
analysis) and a third live finding: through the Waveshare gateway (Modbus TCP to RTU) master 1 returns
the same R0 = 0x0005, R22 = 0xFFFF and R24 = 5 as through the logger, so these values come from the
inverter, not from the logger.

- R586 battery temperature: with an applied offset (-1000) a raw 0 is "not available" (parallel
  slaves report 0, which would read as -100 C), no value is published; the diagnostics note
  `identity.firmware_offsets.battery_temperature.unavailable`.
- R664 to R671 (generator port power, raw sensors, off by default): decoded as signed 32 bit instead
  of unsigned (unsigned showed jumps of 65 kW).
- R219 BMS off-grid discharge current is now a current in A (like R218; a third-party dump shows
  420/420 A) instead of a raw number.
- `max_read_registers`: configurations with 81 to 100 still start; values above 80 are limited to 80
  at runtime (87 and 89 registers in one read work, 131 fails). A warning is logged once at start and
  the diagnostics show `max_read_registers` configured and effective. The default 60 and the 50 ms
  pause between requests are unchanged.
- All documents under `docs/` are now in English.
- Hint for an unusable R65: 0.1 kWh is marked as recommended (a third-party capture of a SUN-10K-SG04LP3
  also shows R65 = 0xFFFF with counters in 0.1 kWh). Still no automatic choice.
- Profile notes: R0 0x0005 is documented in V104.2 (22.12.2022); R24 history corrected (V100.0: R24
  empty, R25 to R29 reserve SN byte, R23 grid class; reassigned with V104.2); R586 offset evidence;
  R596, R601, R602 are constant 0 from firmware 1151/1175; R212/R213 are 1 A; R80 = 0 is software OFF
  and triggers F19 (never write it).

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
