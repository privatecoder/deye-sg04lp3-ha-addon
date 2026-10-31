# Deye SG04LP3 Monitor

## Current implementation status

> **Status: device loop with MQTT publisher (0.2.6, experimental).** The app validates its
> configuration, then connects to the configured endpoints (read-only, Modbus function code 03
> only), identifies each inverter, binds and checks its serial number, polls the selected
> registers, fails over to the fallback path and keeps its identity, capability, energy and
> discovery state in `/data`. The values are published through MQTT Device Discovery, so
> **entities appear in Home Assistant** (one device per inverter plus a diagnostic device for
> the app). The Ingress web interface (sidebar: Deye Monitor) sets up the connections and
> inverters (an assistant guides the first start) and chooses the sensors per inverter; an
> inverter reads only identity and status until a selection is saved. The log line `deye_monitor 0.2.6 ready: ...`
> means that the options were accepted and the device loops were started; it is **not** a successful contact with an
> inverter or a broker.

The sensor-selection web interface (Ingress) is described under “Web interface”. Planned (not
implemented yet): the write layer. For tests and CI the environment variable `DEYE_MONITOR_NO_DEVICES=1` starts the app in a validation-only mode without any
device loop and without the publisher.

## MQTT and Home Assistant

* **Devices.** `deye_<instance>_<inverter>` per inverter (only if sensors are selected) and
  `deye_<instance>_app` with diagnostic entities (version, MQTT reconnects, inverters online and,
  per inverter, its connection state and reason). Only the platforms `sensor` and `binary_sensor`
  are used. There are no command topics and the app subscribes to nothing but the Home Assistant
  birth topic.
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
* **Diagnostics.** The app device carries, per inverter, diagnostic sensors: state and reason,
  connection path and endpoint, bound serial number, firmware words (R15/R17/R18, hex digits such as
  `r15=1175 r17=1001 r18=C070`), parallel role (R336), R65 raw value and whether the last reading
  worked (only while the device reports R65), effective energy resolution and its source (`user
  choice` or `R65`), confirmation/blocking state, current energy series, number of capability conflicts (the
  list is an attribute), bus utilisation, transport timeouts/frame errors/resyncs, cooldown and the
  time of the last successful read. Unknown values are `unknown` (`null`); updates are throttled to
  one message per 0.5 s.
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

## Read-only design

The app is read-only. The only thing it sends to a device is
Modbus function code 03 (read holding registers), wrapped for the chosen transport; a guard at the
sending sink rejects every other frame. There are no write paths, no command topics and no entities
that write. The option `allow_writes` exists only for a later version; setting it to `true` logs a
warning and changes nothing. The app never scans the network or the serial ports; it connects
only to the endpoints you list.

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

`endpoints` and `inverters` remain **optional lists in the options for older installations** (and the
examples in the runbook). If the options still contain them at the start and no plant has been
saved in the web interface yet, the app takes them over **once** into the web interface and says so in
the log and in the web interface (“please remove them from the app options”). From then on the web
interface is authoritative: entries that remain in the options are reported as a hint, never
applied silently. The tables below describe the fields (they are the same in the web interface).

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
| `allow_writes` | Reserved, no effect (see above). |

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

## Example (migration path): four inverters, each behind a gateway with a Solarman logger as fallback

This is what the old options look like and what the web interface stores. New installations create
this plant with the assistant instead. The addresses and serial numbers below are placeholders.

```yaml
endpoints:
  - id: gw1
    transport: modbus_tcp
    host: 192.0.2.11
  - id: gw2
    transport: modbus_tcp
    host: 192.0.2.12
  - id: gw3
    transport: modbus_tcp
    host: 192.0.2.13
  - id: gw4
    transport: modbus_tcp
    host: 192.0.2.14
  - id: logger1
    transport: solarman_v5_tcp
    host: 192.0.2.21
    logger_serial: 1700000001
  - id: logger2
    transport: solarman_v5_tcp
    host: 192.0.2.22
    logger_serial: 1700000002
  - id: logger3
    transport: solarman_v5_tcp
    host: 192.0.2.23
    logger_serial: 1700000003
  - id: logger4
    transport: solarman_v5_tcp
    host: 192.0.2.24
    logger_serial: 1700000004
inverters:
  - id: deye1
    name: Deye 1
    endpoint: gw1
    unit_id: 1
    fallback_endpoint: logger1
  - id: deye2
    name: Deye 2
    endpoint: gw2
    unit_id: 1
    fallback_endpoint: logger2
  - id: deye3
    name: Deye 3
    endpoint: gw3
    unit_id: 1
    fallback_endpoint: logger3
  - id: deye4
    name: Deye 4
    endpoint: gw4
    unit_id: 1
    fallback_endpoint: logger4
mqtt:
  discovery_prefix: homeassistant
  topic_prefix: deye_monitor
log_level: info
```

## Minimal configuration (migration path)

```yaml
endpoints:
  - id: gw1
    transport: modbus_tcp
    host: 192.0.2.11
inverters:
  - id: deye1
    name: Deye 1
    endpoint: gw1
    unit_id: 1
mqtt: {}
```

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
  number, energy resolution, polling intervals (advanced). Writing is not available: the page shows
  “Writing: not available” and `allow_writes` cannot be set there. Deleting an inverter asks for
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
   please confirm”. The choice decides the default selection: “island” hides all grid values,
   “no battery” all battery values. *Step 2 – choose sensors:* grouped by measuring point with a
   short description and unit; search, filter (all / selected / deviations); quick actions
   “Basics only” (serial number, operating state, fault words) and “Deselect all”, which are
   ordinary deviations you can preview and reset; “Accept the template unchanged” saves the
   proposal as it is. A sensor marked
   “from template” follows your plant description; “added by you” and “deselected” are deviations
   that stay when you change the plant description and can be reset one by one (“reset”) or all at
   once. Raw register values are in the collapsed **Experts** group and have no unit. The bar at
   the bottom shows the polling load of the draft (low / medium / high / overloaded) before you
   save; **Save** applies plant features and sensors in one step. If someone else saved in the
   meantime you get a hint and can reload without losing your changes.
3. **Transfer to other inverters** (button on card or setup page). Choose the targets, optionally
   leave out the plant features, look at the preview per target (which sensors are added or
   dropped, load, hardware warnings; targets that cannot take it are skipped) and confirm. Serial
   binding, identity, energy history and write permissions are never transferred.
4. **Maintenance** (collapsed at the bottom of the overview): rebind the identity after replacing
   a device, reset the energy history or the serial binding of one inverter, reset all bindings
   (marked GLOBAL) and empty the cleanup list. Each action explains its effect and only runs after
   you typed the confirmation text.

Without a Home Assistant user (not through Ingress) everything is read-only.

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
  inverters are blocked (`selection_recovery_required`) until the app is restarted. Files of the
  earlier schema (`selection.json`, `.prev`, `.recovery.json`) are migrated at the first start and
  renamed `*.migrated`.
  Unknown or obsolete keys are reported and discarded.
- The JSON API accepts connections from the Supervisor (`172.30.32.2`) only. Changes need a user
  (`X-Remote-User-Id`), the CSRF token from `GET api/session`, a JSON body with known keys only and
  the current revision in `If-Match`; a stale revision is answered with 409. Placeholder and
  unsupported entries cannot be enabled, `allow_writes` cannot be set, nothing can write to a
  device. Identity re-binding and store resets need explicit confirmation fields; resetting all
  identity bindings is a separate, explicitly global action.
- `POST api/inverters/<id>/sensors` (and `…/sensors/preview`) accept, besides `add` / `remove` /
  `reset`, the plant `features` and `restore_template`, so the interface previews and saves a whole
  draft as one atomic change. The catalog carries a plain-language `description` (de/en) and a
  `hint` key per sensor.
