# Deye SG04LP3 Monitor

## Current implementation status

> **Status: device loop with MQTT publisher and an opt-in write layer (0.4.0).**
> The app validates its configuration, then connects to the configured endpoints (read-only
> by default: Modbus function code 03 only; the 38 settings under “Writing settings” can be
> released per inverter and are written from the web interface or from Home Assistant entities), identifies each inverter, binds and checks its serial number, polls the selected
> registers, fails over to the fallback path and keeps its identity, capability, energy and
> discovery state in `/data`. The values are published through MQTT Device Discovery, so
> **entities appear in Home Assistant** (one device per inverter plus a diagnostic device for
> the app). The Ingress web interface (sidebar: Deye Monitor) sets up the connections and
> inverters (an assistant guides the first start) and chooses the sensors per inverter; an
> inverter reads only identity and status until a selection is saved. The log line `deye_monitor 0.4.0 ready: ...`
> means that the options were accepted and the device loops were started; it is **not** a successful contact with an
> inverter or a broker.

The sensor-selection web interface (Ingress) is described under “Web interface”, the write layer
under “Writing settings”. For tests and CI the environment variable `DEYE_MONITOR_NO_DEVICES=1` starts the app in a validation-only mode without any
device loop and without the publisher.

## MQTT and Home Assistant

* **Devices.** `deye_<instance>_<inverter>` per inverter (only if sensors are selected) and
  `deye_<instance>_app` with diagnostic entities (version, MQTT reconnects, inverters online and,
  per inverter, its connection state and reason). Measurements use the platforms `sensor` and
  `binary_sensor` and have no command topic. Settings you have released for writing are
  `number`, `switch` and `time` entities of the inverter's device plus one `event` entity (see
  “Home Assistant entities” under “Writing settings”). Without a release the app subscribes to
  nothing but the Home Assistant birth topic; with one it also subscribes to the command topics of
  exactly the entities that exist right now.
* **Identities.** Unique id `deye_<instance>_<inverter>_<key>`, suggested entity id
  `sensor.deye_<instance>_<inverter>_<key>` (`default_entity_id`). Energy counters carry a
  statistics series in all three, e.g. `..._battery_energy_charge_total_g0`. A change of the
  effective energy resolution (R65 or override) or of the physical inverter starts a new series
  (`_g1`, ...): the old counter is removed and its statistics stay in Home Assistant; the app never
  links the series and never touches the recorder. If you named another entity like a suggested
  id, Home Assistant appends a number: the app does not rename registry entries.
* **Availability.** Every inverter entity needs the app *and* the inverter to be online
  (`availability_mode: all`). An inverter is `online` only after its identity is verified and a
  fresh measurement was published; the app's status topic is `<topic_prefix>/<instance>/status`
  (will `offline`, retained). A group that falls silent expires after three intervals
  (`expire_after`) and shows `unavailable`; a missing or invalid single value is `unknown`
  (JSON `null`), never 0.
* **States.** One JSON object per inverter and group
  (`<topic_prefix>/<instance>/<inverter>/<group>`), QoS 1, not retained, newest cycle only. After
  a reconnect or the Home Assistant birth message the app discards older measurements, republishes
  the discovery data and requests fresh reads. MQTT 3.1.1; a new client per connection attempt;
  reconnect with exponential backoff (2 to 60 s) and jitter.
* **Removal of entities.** A deselected sensor is published as a platform-only tombstone. A
  freshly started Home Assistant cannot map a tombstone to its registry entry, so at every
  (re)connect and on every birth message the app replays each retired entity under its old unique
  id (always unavailable) followed by its tombstone. The inventory (the cleanup ledger) is kept in
  `/data/discovery.json`. Nothing proves that Home Assistant has processed a replay (a PUBACK only
  means that the broker accepted the message, a birth message only that Home Assistant started), so
  **the ledger is never emptied automatically**. After you checked in Home Assistant that the old
  entities are gone, forget them explicitly (Ingress API `POST api/cleanup/purge`, scope
  `inverter` or `all`, confirmation text `purge-cleanup-ledger`). A removed inverter stays an
  all-tombstone document until then. The ledger holds at most 2000 entries; beyond that the change
  is refused with the diagnostic `discovery_ledger_full` (nothing is dropped silently) until you
  purge. If you uninstall the app while Home Assistant is unreachable, remove leftover entities in
  Home Assistant by hand.
* **Availability on errors.** Whatever ends a session (shutdown, broker error, an inventory that
  cannot be written), the app publishes `offline` for every inverter and for itself, each with its
  own time budget. If the app's `offline` cannot be delivered, the connection is cut without a
  DISCONNECT so that the broker publishes the will.
* **Warnings and faults.** Besides the per-bit entities (fan, grid phase, meter communication W4
  (off by default), battery communication W31, parallel communication W32, F13, F29, F41) every
  inverter has two text sensors, "Active warnings" and "Active faults": the active codes with names
  from the Deye alarm list, e.g. `F29 Parallel CANBus fault, F41 Parallel system stop`, or `none`.
  The attribute `alarms` lists `code`, `name` and a short `remedy` per active entry; a bit the list
  does not name shows its code alone. Wn is bit n-1 over R553/R554, Fn is bit n-1 over R555-R558.
  The table is `profiles/alarms.yaml`. Beyond 255 characters (Home Assistant's limit) the state
  lists the codes only.
* **Diagnostics.** The app device carries only app-wide entities: app version, inverters
  configured and online, MQTT reconnects. Everything that belongs to one inverter is a diagnostic
  sensor of that inverter's own device (no name prefix; available while the inverter is offline):
  state and reason, connection path and endpoint, bound serial number, the firmware of both boards
  like the Deye cloud (`Firmware MCU1` = `1001-C070`, communication/HMI board R17-R18, and
  `Firmware MCU2` = `2005-1175-1807`, control board R14-R15-R11; the former "Main firmware
  version" R15 is part of MCU2 and no longer a default sensor), energy resolution raw word and last reading (only while R65 is read
  and reported), effective energy resolution and its source (`user choice` or `R65`),
  confirmation/blocking state, current energy series, number of capability conflicts (the list is
  an attribute), bus utilisation, transport timeouts/frame errors/resyncs, cooldown and the time
  of the last successful read. Unknown values are `unknown` (`null`); updates are throttled to
  one message per 0.5 s.
* **Plant totals.** With two or more inverters the app device also carries the totals over the
  inverters ("Plant (totals)", below). They are computed from the newest value of each member; a
  member whose value is missing, `null` or older than three poll intervals makes the total
  `unknown` (never a partial sum). A member without PV inputs (none selected) adds 0 to the PV
  totals.

  | Sensor | Meaning |
  | --- | --- |
  | Plant PV power / load power / inverter output power | sums of PV1..n power, "Load power total" (equals the LOAD/UPS output on these units) and "Inverter output power total" |
  | Plant battery power | sum of battery voltage x battery current of the members; **positive = discharging** |
  | Plant PV energy, battery charge, battery discharge, load energy | sums of the members' total counters (`total_increasing`); the series is in the entity id (`..._g<n>`) and the attribute `energy_series`, not in the name |
  | Plant ... today (PV, battery charge/discharge, load) | sums of the "today" counters, measurement only |
  | Plant battery SOC, battery temperature | from the parallel **master** only (R336), never averaged; one entity each: the master's BMS value when it is selected, else the master's inverter value (R588 / R586); `unknown` without exactly one master among the members |
  | Plant BMS voltage / current (bank), battery power (BMS) | from the master only, as above. BMS current is positive while charging, so the BMS power is negated to show discharging as positive |
  | Plant members / stale members / master / energy series | diagnostics; the members are attributes |

  **Energy dashboard.** Use the energy sensors of the current series (the entity id ends in `_g<n>`; the attribute `energy_series` and the diagnostic *Plant energy series* show it; after a series change the retired entities are removed, so the names do not clash): *Plant PV energy* as solar
  production, *Plant battery charge* and *Plant battery discharge* as the battery's "energy
  going in" and "out", *Plant load energy* as individual device or house consumption. Do not use
  the "today" sensors there. Every inverter counts its own battery, PV and load energy, so the sum
  is the plant value; do not add the inverters' own sensors on top. A new plant series
  (`g<n>`, never reused) starts when the membership changes or any member starts a new energy
  series (resolution change, counter reset); the old plant energy entities are then retired, like
  the inverter ones. The power sensors are for the live view (or for the Energy dashboard's
  "power" options).
* **Parallel slaves.** On a unit whose R336 says "parallel on, not master" the battery temperature
  (valid on the master only) and, when the BMS block R210-R219 reads entirely 0, the BMS entries
  are inactive: not polled, no entity, greyed in the selection with the reason; they become active
  again by themselves if the unit becomes master or leaves parallel mode. Never applied to
  independent inverters or cascades. A pass in which R336 cannot be read (missing or undecodable)
  keeps the established verdict (no polling, no values, no entity restored); only a valid master or
  standalone role clears it, a valid slave role checks the BMS block again (in chunks if the read
  limit is smaller than the block). No slave is assumed while the role is unknown.
* **"Only 0" hints.** Only physical measurements that should not be 0 in operation are watched
  (`observe_zero` in the profile; default by category). Alarm/fault words, flags, enums, counters
  and similar are healthy at 0 and never reported. No "only 0" hint is shown for an entry that
  feeds a plant total on a plant member: a master that supplies no load legitimately reads 0.
* **Plant-total inputs.** In the sensor list every entry that feeds a plant total carries the chip
  "Plant total" (only on a member of the plant totals; battery state and BMS values only on the
  master). Deselecting one shows what happens to that total (it becomes unknown, or no longer
  includes this share). Where a total has alternative sources (battery SOC and temperature: BMS
  first), an input that is only a fallback while a preferred one is selected shows "Plant total
  (fallback)". The list is derived from the same definition as the totals.
* **Fields the device does not report.** Profile entries that declare a not-reported value (R0,
  R22, R24, R65: `0xFFFF`, R65 also `0`, R24 any undefined code) are checked once at the identity
  read and again after a change of serial number, firmware (R15/R17/R18) or profile revision.
  Such a field is no longer polled and gets no Home Assistant entity (a published one is removed
  like a deselected sensor); in the sensor selection it is greyed out with the note "not reported by
  the device (R22=0xFFFF)", and saved selections stay valid. The result is kept per inverter and
  shown in the inverter diagnostics (`unreported`: key, register, raw value). MPPT count, Phase count
  and Battery inputs are the exception: they show the hardware profile value (SUN-10K: 2 MPPT, 3
  phases, 1 battery input) with the attribute `source: profile`, and `source: device` otherwise.
* **Readable states.** Enum sensors publish readable labels (`Smart Load`, `Three-phase LV hybrid`,
  `Lithium`, `Normal`); the discovery `options` list them.
* **Limits.** One discovery document per device is limited to 256 KiB (the app refuses to send
  more; nothing is truncated). `/data/discovery.json` is written before anything is published; if it
  cannot be written, nothing is published and the app stays `offline`.
* **Inverter id `app`** is reserved for the app device.

## Read-only by default

Out of the box the app is read-only. The only thing it sends to a device is Modbus function code 03
(read holding registers), wrapped for the chosen transport; a guard at the sending sink rejects
every other frame. Without a release there are no command topics and no entities that write.
Writing exists only as an explicit, per-inverter, per-setting opt-in that is used from the web
interface or through the Home Assistant entities of the released settings (see “Writing
settings”); `allow_writes` is `false` and `enabled_write_operations` is empty until you change them
there. The app never scans the network or the serial ports; it
connects only to the endpoints you list.

## Why `uart: true`

The manifest permission `uart` is static (Home Assistant cannot make it conditional). It maps the
serial devices into the container so that a `serial_device` you configure can be opened. No device is
opened unless an endpoint with `transport: serial_rtu` names it.

## RTU timing and the resynchronisation limit

*Active in the device loop; the values it protects are published as `null` while unknown.*

Modbus RTU (serial and RTU over TCP) has no transaction id: an answer does not say which request it
belongs to. After a timeout, a malformed answer, a lost connection or a cancelled request the
app therefore treats the line as *uncertain* and recovers in three steps before it requests data
again:

1. **Late-answer window:** for at least `rtu_late_window_s` after the failed request everything
   that arrives is discarded (default `max(1 s, 2 x timeout_s)`).
2. **Silence:** then the line must be quiet for 200 ms (at most 8192 bytes of noise).
3. **Identity probe:** the serial number registers (R3 to R7) are read and must match the bound
   serial number; only before a serial number is bound, printable ASCII is accepted.

The whole recovery has a time budget (window + silence + probe deadline + 1 s); noise that never
stops, a wrong or missing probe answer, a lost connection or a cancellation leave the line
uncertain and the stream is closed. Values read in that state are never published. Every failure
also starts a cooldown of 2 s that doubles up to 60 s and is reset by the next successful exchange.

**Assumption, not a Deye guarantee:** no device or gateway answers later than
`rtu_late_window_s` after the request it belongs to. A later answer with the same unit and length
and a valid CRC cannot be told apart from a fresh one (RTU has no transaction id, and none is
invented). Widen `rtu_late_window_s` for slow gateways. For RTU over TCP no guarantee is given about
how a gateway maps old RS485 answers to new TCP connections. Modbus TCP and Solarman V5 are not
affected (transaction id and sequence number); a Modbus TCP timeout always drops the connection.

## Configuration

**Connections and inverters are set up in the web interface** (sidebar **Deye Monitor** → *Set up
plant*, see “Setting up the plant”), not in the app options: a drop-down in the options form cannot
offer the connections you created, so the web interface does it. The options form keeps only
`mqtt` (including TLS), `log_level` and `instance_id`. The empty defaults start the app with an empty
plant; the web interface then offers the setup assistant.

The tables below describe the connection and inverter fields as the web interface offers them.

### `endpoints`

| Option | Description |
| --- | --- |
| `id` | Unique name (`a-z`, `0-9`, `_`, up to 32 characters) used by the inverters. |
| `transport` | `modbus_tcp`, `rtu_over_tcp`, `serial_rtu` or `solarman_v5_tcp`. |
| `host` | IP address or host name. Required for all transports except `serial_rtu`. |
| `port` | TCP port. Default 502 (`modbus_tcp`, `rtu_over_tcp`) or 8899 (`solarman_v5_tcp`). |
| `logger_serial` | Solarman logger serial number (1 to 4294967295). Required for `solarman_v5_tcp`, not allowed otherwise. |
| `serial_device` | Absolute device path, preferably `/dev/serial/by-id/...`. Required for `serial_rtu`, not allowed otherwise. |
| `baudrate` | Serial baud rate (8N1), default 9600. `serial_rtu` only. |
| `bus_id` | Optional. Endpoints with the same `bus_id` share one request queue. |
| `timeout_s` | Total deadline per request in seconds, default 5. |
| `rtu_late_window_s` | RTU only: assumed latest arrival of a late answer after its request, in seconds. Default `max(1, 2 x timeout_s)`. See "RTU timing". |
| `max_read_registers` | Registers per read request, 6 to 100, default 60. Values above 80 are limited to 80 (a warning is logged once at start and the diagnostics show configured and effective value): larger blocks fail on gateways, 87 and 89 registers work, 131 does not. At least 6 because the RTU identity probe reads 5 registers (6 after a failed request of 5) and is never shortened. |
| `request_gap_ms` | Pause between requests on a bus in milliseconds, default 50. |
| `queue_size` | Maximum running plus waiting requests per bus, default 32. |

Two endpoints that address the same target (same host and port, or the same serial device after
resolving links) are rejected as aliases.

Hosts are normalised before use: lower case, no trailing dot, IPv6 without brackets and
compressed, IPv4-mapped IPv6 (`::ffff:192.0.2.1`) reduced to IPv4. The normalised form is what the
app connects to and what the bus identity is built from. Host names are **not resolved** (no DNS lookup), so two
different names (or a name and an IP address) for the same device cannot be recognised as aliases.
When several endpoints reach one physical bus under different names, give them the same `bus_id`.

### `inverters`

| Option | Description |
| --- | --- |
| `id` | Stable identifier (`a-z`, `0-9`, `_`). Part of every entity ID; do not change it later. |
| `name` | Display name in Home Assistant. |
| `endpoint` | `id` of the primary endpoint. |
| `unit_id` | Modbus unit address of the inverter, 1 to 247. |
| `fallback_endpoint` | Optional second path to the same inverter. Used only after repeated failures and only if the same serial number answers there. |
| `fallback_unit_id` | Unit address on the fallback path. Default: `unit_id`, but 1 if the fallback endpoint is a Solarman V5 logger. |
| `expected_serial` | Optional 10-character serial number. A device with a different serial number is quarantined (no data from it is used). |
| `profile` | Register profile, default `sg04lp3_lv_v105_8`. |
| `energy_resolution` | `auto` (derived from register 65), `0.1` or `1` (kWh). |
| `poll_fast_s` | Interval for power, battery and status values, seconds (at least 1), default 10. |
| `poll_energy_s` | Interval for energy counters, default 60. |
| `poll_settings_s` | Interval for settings (diagnostic only), default 300. |
| `poll_identity_s` | Interval for identity registers, default 3600. |
| `allow_writes` | Master switch of the write layer for this inverter (default `false`). Set in the web interface (plant page), never in the inverter form. Without the matching entry in `enabled_write_operations` nothing can be written. |
| `write_release_firmware` | Set by the app, not by hand: the hardware profile and firmware words (R11, R14, R15, R17, R18) read when you released writing. If the inverter later reports other words (firmware upgrade), writing stays blocked (“Firmware changed since release”) until you release again. Monitoring is not affected. |
| `enabled_write_operations` | The settings released for writing on this inverter (default: none). A subset of the nine operations listed under “Writing settings”. |

On one bus (same host and port, or the same `bus_id`) every `unit_id` can belong to only one inverter.
Separate gateways may all use unit 1.

#### Modbus addresses: gateway versus logger

The address depends on the **connection type of each route**, so the primary and the fallback of one
inverter can use different addresses:

* **RS485 gateway** (`modbus_tcp`, `rtu_over_tcp`, `serial_rtu`): the address is the inverter's own
  Modbus address, the parallel setting “Modbus SN”. In a parallel system the master is usually 1 and
  the slaves are 2, 3, …
* **Solarman V5 logger** (`solarman_v5_tcp`): the address must **always be 1**, also for parallel
  slaves. Any other address fails.

Example, four inverters in parallel: through the gateway `unit_id` is 1, 2, 3, 4; through each
inverter's own logger the fallback is `fallback_unit_id: 1` (the default for a V5 fallback). The
web interface prefills 1 and shows the matching help text per route. An address other than 1 on a
logger route only shows a warning (in the form and after a failed connection check); saving stays
allowed.

### `mqtt`

The `mqtt:` block must stay in the configuration (Home Assistant requires a nested block, it may be
empty: `mqtt: {}`). Leave `host` empty or omit it to use the Home Assistant MQTT service (the app
requests it optionally; if the service has no credentials, none are used). Otherwise set `host`, `port` (default 1883), `username`, `password` and `tls`. `birth_topic` (default
`homeassistant/status`) is the Home Assistant birth message. `discovery_prefix` (default
`homeassistant`) and `topic_prefix` (default `deye_monitor`) may contain several `/`-separated
segments, without wildcards.

**TLS for an external broker.** `tls: true` enables TLS; the server certificate and host name are
always verified (there is no insecure mode, and the port is never used to guess TLS). `tls_ca_file`
is a PEM file below `/ssl` (the app maps `/ssl` read-only for exactly this reason; nothing else is
read from it); empty means the system trust store. For brokers that require a client certificate set
`tls_certfile` and `tls_keyfile` together (also below `/ssl`). Passwords and keys are never logged.

### Why `poll_*` are flat options

Home Assistant requires every nested block in a configuration, even if all its entries are optional.
The intervals are therefore four flat optional options per inverter instead of a `poll:` block; each
can simply be left out.

### `log_level` and `instance_id`

`log_level` is `debug`, `info` (default), `warning` or `error`. `instance_id` (4 to 16 characters of
`a-z0-9`) is generated and stored in `/data/instance_id` when empty; set it only to keep entity
identities when moving an installation.

## Diagnostics and self-healing

Each inverter has a diagnostic record (state, reason, active path, consecutive failed cycles,
capability conflicts, energy resolution and generation, suggested plant features, bus
utilisation). If an inverter task fails unexpectedly the inverter goes `offline` with the reason
`internal_error`, the counter `internal_errors` and the exception type (never its message) appear in
its diagnostics, and the task is restarted with an increasing delay (5 s, doubling up to 300 s,
with jitter; a long healthy run starts over at 5 s). The other inverters and the application keep
running; a shutdown also interrupts a waiting restart. R65 (the DSP protocol version) is not
selected by default; it is read automatically when an energy entity is selected and
`energy_resolution` is `auto`, and it can be selected explicitly as a diagnostic without changing an
override.

### Choosing the energy resolution

Some firmware/logger paths report R65 = 0xFFFF (or 0), which is not a protocol version. The app then
derives no resolution and does not publish the energy counters; the overview and the sensor page of
the inverter show "The inverter reports no valid energy resolution (R65)". Choose **0.1 kWh** (usual
for the SG04LP3; compare the daily energy with the inverter display) or **1 kWh** with the button
there, or set "Energy resolution" in the inverter's connection settings. The choice starts a new
statistics series (the old one is never continued with another factor). Check afterwards that
the daily PV energy matches the display.

## Writing settings

Writing is **off by default** and **per inverter and per setting**. It is a regular feature for
38 reviewed settings of the SG04LP3 only (register numbers of the V105.8 protocol document; every
write is exactly one register, function code 16 with count 1). Everything not listed (work mode,
clock, battery type, topology, identity, the ToU weekday bits and every other register) cannot be
written.

| Group | Setting | Register | Range / rule |
| --- | --- | --- | --- |
| Battery protection | Maximum battery charge current | R108 | 0 to the model limit |
| | Maximum battery discharge current | R109 | 0 to the model limit |
| | Shutdown SOC | R115 | 0 to 100 %, SOC mode only; shutdown < low < restart |
| | Low SOC | R117 | 0 to 100 %, SOC mode only; shutdown < low < restart |
| | Restart SOC | R116 | 0 to 100 %, SOC mode only; shutdown < low < restart |
| GRID charging | Generator charge current | R125 | 0 to the model limit |
| | GRID charge current | R128 | 0 to the model limit |
| | GRID charge enable | R130 | 0 or 1 (the whole register) |
| | GRID charge start SOC | R127 | 10 to 90 %, SOC mode only |
| Smart Load | OFF voltage / ON voltage | R134 / R136 | 38.00 to 63.00 V (0.01 V), voltage mode only; OFF < ON |
| | OFF SOC / ON SOC | R135 / R137 | 0 to 100 %, SOC mode only; OFF < ON |
| ToU schedule | ToU enable (bit 0 only) | R146 | 0 or 1, all other bits preserved |
| | Slot 1-6 start time | R148-R153 | 00:00 to 23:55, minutes in steps of 5, strictly ascending |
| | Slot 1-6 discharge power | R154-R159 | 0 W to the rated power of the model |
| | Slot 1-6 target SOC | R166-R171 | 0 to 100 %, SOC mode only |
| | Slot 1-6 allow GRID charging (bit 0 only) | R172-R177 | 0 or 1, all other bits preserved |

Values must be exact: a fractional or non-finite number is refused, nothing is rounded or clamped.

- **Model limit.** The limit of the four current settings comes from the vendor datasheet (5K 120 A,
  6K 150 A, 8K 190 A, 10K 210 A, 12K 240 A) and is selected from the rated power the inverter
  reports (registers 20/21, exactly 5, 6, 8, 10 or 12 kW). The slot discharge power is limited by
  the same rated power in W. If the model cannot be determined, or the stored value is outside the
  allowed range (so it could not be restored), the setting stays locked.
- **Battery control mode (R111).** SOC settings (R115-R117, R127, R135, R137 and the slot target
  SOC) are offered only while R111 is 1 (SOC mode); the Smart Load voltages (R134, R136) only while
  R111 is 0 (voltage mode). The mode is read fresh every time; it is never changed by the app.
- **Smart Load.** The four Smart Load settings also need the GEN port to be configured as Smart
  Load (R133). OFF must stay below ON (a rule of this app, not a proven firmware check).
- **Protection thresholds.** Shutdown, low and restart SOC are checked against a fresh read of the
  other two: shutdown < low < restart. The app does not claim a safe minimum; the limits come from
  the commissioning of your battery. The current values are not a specification.
- **GRID charge enable (R130).** The firmware compares the whole word with 1, so the app writes
  exactly 0 or 1 and refuses to write while the register holds anything else (`unexpected_bits`).
- **ToU schedule.** The start times must ascend strictly from slot 1 to 6 (slot 6 runs on until
  slot 1 starts, across midnight). Each time is sent on its own and takes effect immediately, so
  the neighbouring intervals change with it; the app checks all six times freshly for every edit,
  refuses a time that would cross a neighbour (`tou_order`) and never rearranges other slots.
  Several edits are never one atomic schedule change: other units can observe the intermediate
  schedule. The app does not switch the ToU off by itself. The ToU switch shows the configuration
  read; the firmware can set bit 0 itself (for example with peak shaving). All ToU settings are
  closed while the Spain mode (R146 bit 8) is set (`spain_mode`). The slot switch is called “allow
  GRID charging”, not “force charging”; the generator charge bit of the same word stays untouched.
  The settings dialog shows, read-only next to the ToU group, the effective weekday filter
  (R146 bits 1-7 = Monday-Sunday; all zero means every day) and the inverter's own clock
  (R62-R64). Not offered: the slot voltage words R160-R165 and the slot generator charge bit.
- **Parallel plants.** All 38 registers lie in the settings block that the master copies to the
  slaves, so only the master is written (see below).

All gates are decided at the moment of writing from fresh reads (release and firmware
fingerprint, identity, parallel role, R111/R133/R146, the neighbours of an ordered value and the old
word), not only at the preview.

**Enabling.** Plant page → inverter card → “Writing”: tick the master switch **and** the settings
you want to allow, then save. Both are needed; the default is off. Saving needs a fresh identity read of the inverter and stores the firmware it shows
(R11, R14, R15, R17, R18, hardware profile) with the release. If the firmware changes later, the
release is inert (“Firmware changed since release — check and release again”) until you release
again; there is no built-in firmware list, your release is the acceptance. This is a plant change (the
polling generation is rebuilt). There is no way to enable writing through the sensor selection,
the setup assistant or “apply to all”.

**The flow** (inverter page → “Change settings”; admin user, CSRF and the plant revision are
required):

1. The dialog reads the enabled settings freshly from the inverter and shows the current value.
2. You enter a value and open the **preview**: target inverter (serial, role), old → new value,
   the raw register word and mask, the risk note, and the values of every other inverter of the
   plant before the change. Nothing is sent. If another inverter is unreachable or not
   identified, no preview is issued. The text “Writes this inverter only. Other inverters are
   observed; matching values do not prove synchronization.” is always shown; there is no
   preselected write target in a parallel plant.
3. You confirm. The preview issued a **single-use operation id** valid for 30 seconds. At the send
   point the app checks again that writing is still enabled, that user, plant revision, inverter,
   serial number, firmware words, role, route, unit and connection are unchanged, reads the whole
   register again inside one bus critical section (any change since the preview is a conflict
   that needs a new preview), writes the durable audit log entry (if the log cannot be written,
   nothing is sent) and sends exactly one FC16 frame for one register through the write guard.
   There is no retry, no FC06, no fallback endpoint, no fan-out to other inverters and no
   automatic rollback. Writes of the whole plant are serialised with at least 5 seconds between
   them (a conservative field workaround, not a vendor guarantee).
4. After 1 second the app reads the target register back and the same register of every other
   inverter and shows the result. It reads the other inverters once more after 30 seconds (read
   only; a countdown shows when) and the result table then has the columns “Before / After 1 s /
   After 30 s”. The write serialisation is not held while it waits. “Read other inverters again”
   (in the result and in the log entry) reads them once more at any time; each reading is
   appended to the audit log as a later observation - never as an attribution.

**Parallel plants: only the master is written.** In a parallel system the master copies its whole
settings block (R65-R209 and R339-R429, so all of these settings) to every slave, and a slave's own
value is overwritten by a later master snapshot. That follows from the firmware (Codex analysis of
MCU2 1175 and MCU1 C070) and from two field observations: R108 written on the master reached all
slaves within about 18 s, and R130 reached all four units. Persistence across a restart is not
proven, no delay is guaranteed (the transfer is staged), and none of this shows that the other
settings behave the same. Therefore a **slave is locked for writing**: the dialog and the release
show “In parallel operation the master distributes these settings to all units. Writing is only
possible on the master.” with a link to the master, and the release checkboxes of a slave are
disabled. The lock is decided from a fresh role read (R336) at the preview **and** at the send
point (`parallel_slave`, nothing is sent); an R336 read that fails is rejected as `read_failed` and a role word that does not decode locks as `parallel_slave` (nothing is sent), a single inverter or a
plant with parallel mode off is unaffected, and the lock follows the current role. On the master
the app says that the value is distributed to the slaves (in one owner test, R108 reached all
slaves within about 18 s; no latency is guaranteed); the 30 s observation shows whether they follow. Matching values
still do not prove synchronization.

**Statuses** (also in the audit log, `/data/write-audit.jsonl`): `rejected` (nothing was sent),
`sent` (in progress), `failed` (the device answered with an exception code), `readback_confirmed`
(valid acknowledgement and the fresh complete word equals the requested one) and `outcome_unknown`
(the frame may have been transmitted but there is no valid acknowledgement, or the read-back is
missing or different). After a restart an unfinished `sent` entry becomes `outcome_unknown`; an
operation is never replayed. A restart, a plant change or switching the release off voids every
pending permit.

**What the read-back proves and what not.** A valid acknowledgement with a matching read-back
shows the value in the settings memory of the inverter's communication processor (MCU1). It does
not show that MCU2 adopted it, that it survives a restart, that the setting has the intended
effect, or that other inverters follow. Other inverters are only observed and reported as
same / different / unavailable, with their values before and after 1 s and after 30 s - never as
“propagated”.
After `outcome_unknown` a later matching value is recorded but does not show that this command
caused it. External writers (the display, the cloud) are not excluded: Modbus has no
compare-and-swap. Your own check of the display or the effect is recorded separately (“Checked at
the device”). A change made by someone else later is noticed the next time the dialog reads the
values and recorded as drift; the app never reasserts a setting by itself.

**Good practice.** Write on the master (slaves are locked), change one setting at a time, note the
value before, check the display afterwards and restore a value as a new operation. The historical
procedure of the first field write is kept in `docs/FIRST-WRITE-RUNBOOK.md`. Never use these buttons
to “try something” on a plant you depend on.

### Home Assistant entities

Requires Home Assistant 2026.5 or newer for the `time` entities (the app is tested against 2026.10.4).

Every setting you have released for an inverter appears as an entity of that inverter's device in
Home Assistant (category *Configuration*), written the Home Assistant way: there is no
confirmation dialog and no preview in Home Assistant. The release is your decision; use Home
Assistant's own permissions (who may call services, who can reach the MQTT broker) to decide who
may use the entities.

| Setting | Entity |
| --- | --- |
| currents (R108, R109, R125, R128) | `number`, input box, 1 A steps, unit A, 0 up to the model limit |
| SOC values (R115-R117, R127, R135, R137, slot target SOC) | `number`, input box, 1 % steps (R127 10 to 90) |
| slot discharge power (R154-R159) | `number`, input box, 1 W steps, 0 up to the rated power |
| Smart Load voltages (R134, R136) | `number`, input box, 0.01 V steps, 38.00 to 63.00 V |
| GRID charge enable (R130), ToU enable (R146 bit 0), slot “allow GRID charging” (R172-R177 bit 0) | `switch` |
| slot start times (R148-R153) | `time` (seconds are always `00`, minutes in steps of 5) |
| the result of every command | one `event` entity per inverter, “Write result” |

* **When an entity exists.** Only for released settings, only after a fresh read confirmed that
  the release still applies (same firmware as at the release, and the inverter is a master or not
  in a parallel system, from the role read at that moment), and only for the master of a parallel
  system. Revoking the release, a firmware change or losing the master role removes the entity
  (through the usual clean-up of Home Assistant entities, see below); a command to a removed entity
  is not read, and the service refuses stale commands in any case. After a restart the entities
  stay until the first fresh read has decided; until then, and while the inverter cannot be read,
  they are *unavailable* and take no commands.
* **Closed settings are unavailable, not removed.** A setting whose precondition is closed right
  now (battery control mode R111 does not fit, GEN port is not Smart Load, ToU Spain mode, unknown
  model, a stored value outside the accepted range) stays in Home Assistant, marked unavailable,
  and becomes usable again when the next fresh read opens it. A command that arrives in the
  meantime is refused with the reason.
* **State is always a read-back.** The entities show the value read from the inverter - at
  start, periodically with the settings poll interval (`poll_settings_s`, default 300 s), after
  every command and after a Home Assistant restart - never the value you asked for. After a
  rejected command the freshly read value is published; an input field in the Home Assistant
  frontend can still show what you typed until it refreshes. The inverter's own display or cloud can change a value at any time;
  the entity follows at the next read.
* **Commands.** They go through exactly the same guarded write service as the web interface:
  release and firmware fingerprint, identity, role, mode gates, order against the neighbours, the
  old value, the audit intent before the send, one FC16 with one register, at least 5 s between
  writes, no retry, no fallback, no fan-out, and the read-back afterwards. The old value and all
  gates come from fresh reads at the moment of the send, not from the time the command arrived.
  The audit log records `source: mqtt` and no user (the Home Assistant call carries none).
  A value equal to what the inverter holds is an audited no-op without any write.
* **Order and queue.** Commands of a plant are executed one at a time in the order they arrive.
  They are never merged: *OFF, edit, ON* writes three times. The queue holds 32 commands; one more
  is refused (`queue_full`). A command waits at most 60 s; later it is discarded (`expired`). When
  the MQTT connection is lost, the Home Assistant restarts the session, the set of entities
  changes or the plant is changed, everything still waiting is discarded
  (`superseded_context`).
* **Strict payloads.** `number`: a plain decimal, an integer where the step is 1, two decimals for
  volts; `switch`: exactly `ON` or `OFF`; `time`: `HH:MM:SS` with seconds `00` and no time zone.
  Anything else is refused as `invalid_payload` (or `value_not_exact`, `value_out_of_range`, ...)
  and never reaches the inverter. Retained messages on a command topic are ignored.
* **Command topics** contain a generation token that changes with every MQTT session and every
  change of the entity set: `deye/<instance>/<inverter>/write/set/<token>/<setting>`. Only the
  current ones are subscribed, so an old or retained command cannot reach a new session. Do not
  publish to them by hand; do not allow retained publishes on `.../write/set/#` in your broker's
  access rules (MQTT 3.1.1 cannot mark a live retained delivery as retained).
* **Events for automations.** The “Write result” entity fires `write_readback_confirmed`,
  `write_rejected`, `write_failed`, `write_outcome_unknown` or `write_noop`, with the attributes
  `setting`, `requested_value`, `reason` (for example `tou_order`, `soc_mode_required`,
  `invalid_payload`, `expired`), `audit_id` (the operation id of the audit log, empty if nothing
  was recorded) and `value` (the value read back afterwards). Events are not retained: an
  automation that was not running does not see them. `write_readback_confirmed` means the value
  was read back from the inverter, with the limits described above.

Example - switch the ToU schedule off, move slot 2 to 06:00 and on again, then look at the result:

```yaml
actions:
  - action: switch.turn_off
    target: { entity_id: switch.deye_<instance>_<inverter>_set_tou_enable }
  - action: time.set_value
    target: { entity_id: time.deye_<instance>_<inverter>_set_tou_slot2_start }
    data: { time: "06:00:00" }
  - action: switch.turn_on
    target: { entity_id: switch.deye_<instance>_<inverter>_set_tou_enable }
```

```yaml
triggers:
  - trigger: state
    entity_id: event.deye_<instance>_<inverter>_write_result
conditions:
  - "{{ trigger.to_state.attributes.event_type != 'write_readback_confirmed' }}"
actions:
  - action: notify.notify
    data:
      message: >-
        Deye: {{ trigger.to_state.attributes.setting }} -
        {{ trigger.to_state.attributes.event_type }}
        ({{ trigger.to_state.attributes.reason }})
```

Because every step is a separate write at least 5 s apart, other inverters (and the display) can
see the intermediate schedule; the sequence is not an atomic update and the app does not roll it
back if a step fails. Entity ids can differ if you renamed them or if Home Assistant had to
disambiguate; the app only suggests them. English names are used (Home Assistant has no
per-language names in MQTT discovery); the web interface has both languages.

## First live read test

A step-by-step runbook for the first read-only test against real devices (preparation, minimal
sensor selection saved before the first device contact, one inverter via the gateway, then via the
logger, then four inverters, diagnostics, abort and return) is in the repository:
`docs/LIVE-READ-RUNBOOK.md`.

## Damaged state files (fail closed)

The app keeps `identity.json` (serial-number bindings), `energy.json` (the energy record in
use) and `energy-generations.json` (per inverter: statistics namespace, high-water mark, reserved
transition) and `capabilities.json` (a cache) in `/data`. Only "`energy.json` and
`energy-generations.json` both missing" is a first installation; the two energy files are always
judged together. A file that exists but is damaged, unreadable or of an unknown version is
**never** replaced silently: a damaged `identity.json` puts every inverter into the state
`blocked` (`identity_store_corrupt`; no binding, no data); a damaged energy file, or energy files
that contradict each other, block only the energy values (`energy_store_corrupt` /
`energy_history_inconsistent`; an energy record whose history file is missing: `energy_history_missing`); a store that cannot be written blocks in the same way
(`identity_store_unwritable`, `energy_store_unwritable`). A blocked inverter reads nothing and waits
for the reset. The capability cache is rebuilt after a diagnostic.

The explicit reset (offered by the future web interface) keeps a damaged file as `*.corrupt`.
*Identity:* for one inverter only that inverter's binding is removed and the others keep running;
if the whole file is damaged the reset is **global** (all bindings are lost, every inverter goes
offline and identifies again). *Energy:* a statistics series is named `g<n>` (or `<token>g<n>`);
the next generation is always above the high-water mark and above the record in use. If the
history was lost, the reset starts a new random 8-character namespace token (stored before use)
at `g0`; there is no time-based substitute. Until then, removing a file by hand after inspecting
it is the same explicit user action. Writes are atomic for the process (temporary file, fsync,
rename, directory fsync); no protection against power loss is claimed.

## Web interface

The app has an Ingress panel (sidebar entry **Deye Monitor**, admin-only panel (Supervisor default), internal port
8099, no host port). It needs no internet access: all scripts, styles and texts are part of the app.
Language follows the browser (switch with **DE / EN**), appearance follows the system (button
“Theme” switches automatic / light / dark).

### Setting up the plant

**First start.** While the plant is empty the web interface opens the **setup assistant**: *create
connection → create inverter → check connection → choose sensors* (the last step leads to the
sensor selection, where “Basics only” is the careful start). It can be started again at any time
from the empty overview; the page **Set up plant** (top navigation) edits everything later.

* **Connections** (add / edit / delete). The type is chosen from four explained options:
  **Gateway (Modbus TCP → RTU)** (`modbus_tcp`, port 502, the usual choice for an RS485 network
  gateway), **Gateway transparent (RTU over TCP)** (`rtu_over_tcp`, for a gateway in transparent
  mode; choose it when “Modbus TCP” does not answer), **WiFi logger (Solarman V5)**
  (`solarman_v5_tcp`, port 8899, needs the serial number *of the stick, not of the inverter*) and
  **USB-RS485 at the HA computer** (`serial_rtu`, the device is chosen from the entries found under
  `/dev/serial/by-id`; the list is only read, nothing is opened until you save or check; 9600 baud).
  Fields appear per type with sensible defaults. *Advanced*: timeout, late-answer window (RTU),
  block size, pause between requests, queue size, bus identifier. A connection that an inverter
  still uses cannot be deleted. Serial devices can only come from `/dev/serial/by-id`.
* **Inverters** (add / edit / delete): name, identifier (proposed from the name, `a-z0-9_`,
  **cannot be changed after creating** because Home Assistant recognises the entities by it),
  **connection** and optional **fallback connection** as drop-downs of the connections you created,
  Modbus address (default 1; fallback address defaults to the same), optional expected serial
  number, energy resolution, polling intervals (advanced). The inverter form does not set the write
  release; the card has its own “Writing” section for it (default off, see “Writing settings”).
  Changing the connection, the Modbus address, the fallback connection or the expected serial
  number withdraws the release. Deleting an inverter asks for
  confirmation; its stored serial-number binding is only removed if you tick the extra box (its
  entities in Home Assistant are cleaned up through the cleanup ledger, the history stays).
* **Check connection** (button in the connection and inverter dialogs, on the inverter card and in
  the assistant) is **read-only**: it reads exactly the identity registers of the profile (function
  code 03, through the same guarded transport as the normal polling) and shows whether the device
  answers, its serial number (compared with the expected and the bound one), MPPT/phase count and
  rated power for plausibility, and findings against the hardware profile. For a connection that
  is already part of the plant the request is queued on its bus scheduler between the polling
  requests; a connection that is not saved yet gets a temporary transport that is closed
  afterwards. Typical failures are explained (“Wrong transport? Check the gateway mode …”,
  unreachable, no answer, logger refuses, no readable serial number). One check runs at a time.
* **Changes apply immediately, without restarting the app.** The app stops the running polling and
  MQTT session in order and starts them again for the new plant (a short pause; the inverters are
  published `offline` in between and come back after identification). If the new plant cannot be
  started, or cannot be saved, the previous plant is restored exactly (selection records included)
  and the change is reported as failed. Until a change is saved, the proposed generation publishes
  nothing to MQTT and does not touch `identity.json` (an `expected_serial` is bound after saving).
  If the previous plant cannot be started again either, nothing keeps running, the interface says
  so and further changes are blocked until the app is restarted (it then starts from the saved
  plant). In that state every changing call is refused (`plant_recovery_failed`); the views stay
  readable and show the inverters as stopped. A running connection check is cancelled by a plant
  change.
  Removed inverters are taken out of the discovery through the existing cleanup ledger.

### Plant (totals)

On the "Set up plant" page, below "Inverters", the section **Plant (totals)** lists every inverter
with a checkbox (default: all). Unchecked inverters are left out of the totals, so independent
units can be excluded. The totals exist for two or more members; for a single member tick "Create
the totals even for a single inverter". Saving restarts the polling like any plant change and
starts a new plant energy series if the membership changed. The overview shows a **Plant total**
card (totals and the status of every member).

### Using the web interface

1. **Overview.** One card per inverter: state (online, offline, quarantined, blocked) with a
   plain explanation, the way to the device (gateway or logger), serial number, role, time of the
   last poll, the number of selected sensors and a traffic light for the bus load. Hints appear
   on the card, for example “reports only 0 – deselect?”, conflicts between the device data and
   the hardware profile, or a running energy-resolution change.
2. **First start.** An inverter whose selection was never saved shows “Please choose sensors” on its card and reads only identity and status (and the internal hint registers) until you saved a selection; no sensor entities exist before that. Existing saved selections count as confirmed.
   **Set up sensors** (button on the card). *Step 1 – describe your plant:* grid connection
   (public grid, island, generator), battery (and BMS cable), GEN port, used PV inputs, parallel
   operation. Values the device reports itself are filled in and marked “read from the device –
   please confirm”. The BMS question defaults to “Yes, with communication” and is suggested
   from the battery type (R98: lithium = yes, lead = no); parallel slaves keep their BMS values
   inactive regardless. The choice decides the default selection (the template, below):
   “island” hides all grid values, “no battery” all battery values. *Step 2 – choose sensors:* grouped by measuring point with a
   short description and unit; search, filter (all / selected / deviations); quick actions
   “Basics only” (serial number, operating state, active warnings and faults) and “Deselect all”, which are
   ordinary deviations you can preview and reset; “Accept the template unchanged” saves the
   proposal as it is. A sensor marked
   “from template” follows your plant description; “added by you” and “deselected” are deviations
   that stay when you change the plant description and can be reset one by one (“reset”) or all at
   once. Raw register values are in the collapsed **Experts** group and have no unit. The bar at
   the bottom shows the polling load of the draft (low / medium / high / overloaded) before you
   save; **Save** applies plant features and sensors in one step. If someone else saved in the
   meantime you get a hint and can reload without losing your changes.

   **Default selection (template).** Every answer of step 1 changes only what is preselected;
   anything can still be ticked by hand.

   | Answer | Preselected | Not preselected |
   | --- | --- | --- |
   | Battery with BMS communication | BMS SOC, temperature, voltage, current, charge/discharge current limits, charge voltage; inverter battery voltage, current, power and energies | inverter battery SOC and temperature (same source), BMS discharge voltage |
   | Battery without BMS | inverter battery SOC, temperature, voltage, current, energies | all BMS values |
   | Public grid | grid voltage and frequency, selected grid power, grid energies | internal GRID-terminal power |
   | Generator on the GRID port | grid voltage and frequency, internal GRID-terminal power, inverter frequency | utility grid power and energy counters |
   | Island (grid port unused) | inverter frequency; no grid values | grid values |
   | Any | load power and voltage L1-L3, inverter output power total, active warnings and faults, the named alarm bits | UPS/EPS power (equals the load power off-grid), raw alarm and fault words 1-4 |
   | Parallel operation | parallel role and serial number | (slaves: BMS values and temperature stay inactive, the plant device carries SOC and temperature) |

   **Not offered at all** (the firmware writes a fixed value, or the value duplicates another):
   the DC transformer temperature (R540, fixed 25.0 °C), the BMS off-grid charge/discharge limits
   (R218/R219, literal 0) and the load frequency (R655, a copy of the inverter frequency R638,
   which stays). A stored choice of one of them is dropped on loading. **Notices in the list:**
   the BMS discharge-voltage limit (R211) depends on the BMS protocol and not every BMS provides
   it; the “selected grid power” values (R622-R625, R687-R690) are, depending on the configuration,
   only a copy of the internal grid power and must not be added to it.

3. **Transfer to other inverters** (button on card or setup page). Choose the targets, optionally
   leave out the plant features, look at the preview per target (which sensors are added or
   dropped, load, hardware warnings; targets that cannot take it are skipped) and confirm. Serial
   binding, identity, energy history and write permissions are never transferred.
4. **Maintenance** (collapsed at the bottom of the overview): rebind the identity after replacing
   a device, reset the energy history or the serial binding of one inverter, reset all bindings
   (marked GLOBAL) and empty the cleanup list. Each action explains its effect and only runs after
   you typed the confirmation text.

Without a Home Assistant user (not through Ingress) everything is read-only, writing included.

### Backend of the web interface

- The plant is stored in the same two-slot way as the selection: `/data/plant.a.json` and
  `/data/plant.b.json` (complete, checksummed, rising sequence number, atomic writes; the newest
  valid slot wins; a damaged slot is ignored and reported, kept as `*.corrupt` when overwritten; if
  no slot is valid the plant is empty and nothing runs until you set it up again). It has its own
  revision (`If-Match` of every plant change). Routes: `GET api/plant`, `GET api/plant/serial-devices`,
  `POST api/plant/endpoints`, `PUT api/plant/endpoints/<id>`, `POST api/plant/endpoints/<id>/delete`,
  the same for `inverters`, and `POST api/plant/test` (read-only check). The same rules as for the
  selection apply (Supervisor peer, user, CSRF, strict schema, no free registers, paths or frames).
- The selection lives in two slots, `/data/selection.a.json` and `/data/selection.b.json`: each is a
  complete, checksummed document (per inverter the plant features and the deviations from the
  template, `added` / `removed`; effective selection = template(features) + added - removed) with
  a rising sequence number. A change is adopted by the inverter loop and the MQTT publisher and
  reported as saved only when its slot is durable on disk; otherwise the application returns to the
  previous state and the request fails. The newest complete, valid slot is always the accepted
  state; a damaged or truncated slot is ignored and reported (kept as `*.corrupt` when it is
  overwritten), never used. If an adopted change can neither be saved nor taken back, the affected
  inverters are blocked (`selection_recovery_required`) until the app is restarted.
  Unknown or obsolete keys are reported and discarded.
- The JSON API accepts connections from the Supervisor (`172.30.32.2`) only. Changes need a user
  (`X-Remote-User-Id`), the CSRF token from `GET api/session`, a JSON body with known keys only and
  the current revision in `If-Match`; a stale revision is answered with 409. Placeholder and
  unsupported entries cannot be enabled, `allow_writes` cannot be set through the sensor, inverter
  or transfer calls, and there is no generic register, hex or frame call. The write layer has its
  own calls (below). Identity re-binding and store resets need explicit confirmation fields; resetting all
  identity bindings is a separate, explicitly global action.
- Write layer routes (all changing calls need user, CSRF and the plant revision in `If-Match`):
  `PUT api/plant/inverters/<id>/writes` (release: `allow_writes` and `operations`; a plant change),
  `GET api/writes/catalogue`, `POST api/inverters/<id>/writes/current` (fresh values, read only),
  `POST api/inverters/<id>/writes/preview`, `POST api/writes/operations/<operation id>/confirm`,
  `POST api/writes/operations/<operation id>/verification`, `GET api/writes/operations/<id>` and
  `GET api/inverters/<id>/writes/operations` (audit list). These calls are the web path; the MQTT
  entities use the same write service but not these routes.
- `POST api/inverters/<id>/sensors` (and `…/sensors/preview`) accept, besides `add` / `remove` /
  `reset`, the plant `features` and `restore_template`, so the interface previews and saves a whole
  draft as one atomic change. The catalog carries a plain-language `description` (de/en) and a
  `hint` key per sensor.
