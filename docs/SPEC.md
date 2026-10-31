# Deye SG04LP3 Monitor — Specification v1

Home Assistant app (formerly "add-on") that polls 1..n Deye SUN-5/6/8/10/12K-SG04LP3 (LV, three-phase) inverters, exposes the values to Home Assistant via MQTT Device Discovery and, later and only when explicitly enabled, can control selected settings. **The test phase is strictly read-only** (user not on site); the write layer is implemented only after the read part has been accepted (§10), but the architecture provides for it from the start.

Knowledge base: `KB = <local knowledge base>`, in particular
`KB/.orch/reviewer-codex-6.1-addon-grundlagen.md` (referred to below as **GRUNDLAGEN**, cited with section numbers),
`KB/Modbus/V105.8/registers.json` (register catalog) and `KB/Firmware/Analyse/*.md`.

## 1. Hard limits (non-negotiable)

1. **Read = FC03 only.** Without write enablement, every byte stream to a device (inverter, logger, gateway) is an FC03 read frame (Modbus TCP MBAP, RTU ADU or Solarman V5 request with inner FC03 RTU frame). No mask, broadcast, time, heartbeat-reply, logger-HTTP or restart paths, even with write enablement. No generic `execute(function_code)`.
2. **Guard at the lowest send path:** a single function through which every device transmission runs decodes the frame. Allowed: FC03 (unit 1..247, count 1..125). FC16 (0x10) **only** if write enablement of the target inverter is active **and** every target address is in the write whitelist of the profile. Everything else → exception. In the test phase (until §10 is implemented) the FC16 path does not exist. Tests check wire bytes, not method names.
3. **No third-party library for device protocols** (no pysolarmanv5, which automatically sends time replies, GRUNDLAGEN §2.2; no pymodbus/ESPHome code). Framing is implemented in-house (small, tested).
4. **Without write enablement no MQTT command topics**, no writing entity platforms and no subscriptions to command topics.
5. No automatic network scans. Configured targets only.

## 2. Architecture

Python 3.14, asyncio, a single process. Package `deye_monitor` (src layout), fully typed.

| Module | Responsibility |
| --- | --- |
| `config` | Loads `/data/options.json` (or path from env), pydantic v2 models, domain validation (§4). No I/O to devices. |
| `protocol/modbus` | FC03 request construction, RTU CRC16, RTU response parser, MBAP construction/parser, exception responses (0x83). |
| `protocol/solarman_v5` | V5 request construction (header 0xA5, control 0x4510, sequence, logger SN LE, checksum, 0x15) and stream parser (fragmented/coalesced frames, discard heartbeat/foreign frames, sequence/SN check, double-CRC quirk with re-validation). **Never reply** to incoming management/time frames. GRUNDLAGEN §2. |
| `protocol/guard` | Read-only guard (§1.2). |
| `transport` | `modbus_tcp`, `rtu_over_tcp`, `serial_rtu` (pyserial-asyncio-fast), `solarman_v5_tcp`. Persistent connection, deadline per request (default 5 s), reconnect with exponential backoff + jitter (2..60 s), byte-stream-safe reading. |
| `scheduler` | Per `bus_id` FIFO + lock, max. 1 request in flight per endpoint, short pause between requests (default 50 ms), bounded queue. |
| `profiles` | Curated, versioned register profiles as a data file (YAML) in the package + loader + decoder. Default profile `sg04lp3_lv_v105_8`. |
| `device` | Per inverter: read identity (R0–R28, R65, R336), check expected serial number, poll groups by interval, snapshot with cycle ID/timestamp, freshness/availability, primary/fallback endpoint. |
| `mqtt` | aiomqtt. Device Discovery (`homeassistant/device/<id>/config`, retained), `origin`, availability per device + LWT of the app, HA birth (`homeassistant/status`) → resend discovery, state topics not retained, targeted cleanup of removed components. |
| `diagnostics` | Structured logs, counters (timeouts, CRC errors, quirks, reconnects) as diagnostic sensors, secrets redaction. |
| `app` | Entry point, lifecycle, signal handling, ordered shutdown. |

## 3. Transports and endpoints

Transport enum fixed: `modbus_tcp | rtu_over_tcp | serial_rtu | solarman_v5_tcp` (GRUNDLAGEN §3.1).

- `solarman_v5_tcp`: `host`, `port` (default 8899), `logger_serial` (uint32 ≠ 0). The unit ID is that of the inverter.
- `modbus_tcp` / `rtu_over_tcp`: `host`, `port` (default 502). No auto-detection of the framing.
- `serial_rtu`: `serial_device` (`/dev/serial/by-id/...`), `baudrate` (default 9600), 8N1 fixed.
- Common: `bus_id` (default = canonical endpoint, see §8.1), `timeout_s`, `max_read_registers` (default 60, configurable up to 100 but limited to 80 at runtime, hard upper limit 125), `request_gap_ms`.

Each inverter has exactly one `endpoint` (primary) and optionally a `fallback_endpoint`. Fallback only after N consecutive failures (default 3), and only if the same inverter serial number (R3–R7) is read there; return to the primary path periodically (default 300 s). Never poll both permanently at the same time. Entity IDs depend only on the inverter ID, not on the endpoint.

## 4. Configuration (`config.yaml` options/schema)

```yaml
endpoints:
  - id: gw1
    transport: modbus_tcp        # modbus_tcp|rtu_over_tcp|serial_rtu|solarman_v5_tcp
    host: 192.0.2.54
    port: 502
    # logger_serial: 1234567890   # solarman_v5_tcp only
    # serial_device: /dev/serial/by-id/...  # serial_rtu only
    # baudrate: 9600
    # bus_id, timeout_s, max_read_registers, request_gap_ms optional
inverters:
  - id: deye1                     # [a-z0-9_]+, stabil, Basis der unique_ids
    name: Deye 1 (Master)
    endpoint: gw1
    unit_id: 1
    # fallback_endpoint: logger1
    # expected_serial: "2401234567"
    # profile: sg04lp3_lv_v105_8
    # poll: { fast_s: 10, energy_s: 60, settings_s: 300 }
mqtt:                             # empty → Supervisor MQTT service (mqtt:want)
  # host, port, username, password, tls
  discovery_prefix: homeassistant
  topic_prefix: deye_monitor
log_level: info
```

Domain validation: unique IDs; endpoint references exist; `unit_id` 1..247; ports 1..65535; `logger_serial` only and mandatory for V5; `serial_device` only and mandatory for `serial_rtu`; unique `unit_id` on the same `bus_id`; fallback ≠ primary.

### 4.1 Addendum T14: plant setup in the web interface

Connections (`endpoints`) and inverters (`inverters`) are created and edited **exclusively in the Ingress web interface** (section "Set up plant", wizard on first start). The HA options form cannot offer selection lists of already created connections; the options keep only `mqtt` (incl. TLS), `log_level`, `instance_id`. The models and validations from §4 apply unchanged (`EndpointConfig`, `InverterConfig`, bus uniqueness, fallback, limits); an **empty plant is valid** (the one-inverter requirement from §4 no longer applies, the wizard leads into the setup), `AppConfig` = options + plant.

* **Store.** `/data/plant.a.json` / `plant.b.json`: like the selection (§9.6), two complete slots with `seq` and SHA-256, atomic, the newest valid slot applies, a damaged slot is ignored/reported (copy `*.corrupt` when overwriting); if no slot is valid, the plant is empty and nothing runs until the user sets it up again (fail closed, no migration from the options). Own revision (`If-Match` of every plant change).
* **Migration.** If the options contain `endpoints`/`inverters` and **no** plant file exists, they are taken over once (log + notice in the interface: please remove them from the options). Afterwards the store is authoritative; differing option entries only produce a notice, never a silent overwrite. A plant emptied by the user is not migrated again. The manifest keeps `endpoints`/`inverters` as optional lists (default empty).
* **Interface.** Connections: transport as an explained choice (`modbus_tcp` port 502 = "Gateway (Modbus TCP → RTU)"; `rtu_over_tcp` = "Gateway transparent"; `solarman_v5_tcp` port 8899 with logger serial number *of the stick*; `serial_rtu` with dropdown from `/dev/serial/by-id/*`, listed only, baud rate 9600), fields per transport, Advanced section. Inverter: connection and fallback connection as dropdowns, unit ID (default 1), expected SN, energy resolution, intervals; the ID cannot be changed after creation. `allow_writes` cannot be set in the interface or in the API (display "Writing: not available"). A connection can be deleted only if no inverter uses it; when deleting an inverter, the identity binding is removed only after explicit confirmation.
* **Connection test (read-only).** Runs exactly the identity blocks of the profile via the normal guarded transport (FC03 only, deadline), via the bus scheduler of the running connection (same target in the running plant) or a temporary transport that is closed afterwards. Result: reachable, serial number (against expected and bound), model/MPPT plausibility, conflicts, typical hints per transport on errors. No change to the plant, one test at a time; the same target with a different connection type is rejected (`endpoint_in_use_by_other_transport`).
* **Live adoption.** validate → adopt (stop the running generation of inverter runtimes, transports, schedulers and MQTT publisher in order, build new ones; discovery cleanup of removed inverters via the existing ledger; selection records of removed inverters are discarded, new inverters start with `awaiting_selection`) → persist durably. If adoption or persisting fails, the previous generation is restored (`adoption_failed` / `persist_failed`; only if that also fails `plant_recovery_failed`). Selection changes never run concurrently (shared lock). Security rules as in §9.5: Supervisor peer, user, CSRF, `If-Match`, strict schemas, no free registers/frames.

## 5. Register profile v1 (`sg04lp3_lv_v105_8`)

Source of all definitions: `KB/Modbus/V105.8/registers.json` + GRUNDLAGEN §1 + REVIEW2 (`KB/.orch/reviewer-codex-6.1-spec-review-2.md`, §4 register mapping, §5 PV/hardware evidence). **Only binding entry schema** (§8.1/§12 take precedence over older wording):

| Field | Required | Meaning |
| --- | --- | --- |
| `key`, `name` (de/en) | yes | stable key, display name |
| `addr` or `low`/`high` | yes | decimal, zero-based; 32 bit always explicitly `low` + `high` (never assume `high=low+1`) |
| `type` | yes | `u16, s16, u32, s32, ascii, bitfield, enum, hhmm, raw` |
| `mask`/`shift`/`enum`/`components` | as needed | packed fields |
| `scale`, `raw_offset`, `scale_source` | for numbers | `value = (decoded_raw + raw_offset) * scale`; `scale_source: energy_resolution` for §5.1 |
| `unit`, `device_class`, `state_class`, `entity_category` | for physical values | HA metadata |
| `category`, `tags` | yes | display/search (no logic) |
| `requires` | yes (possibly `{}`) | usage predicate over plant features, `all_of`/`any_of` (§9) |
| `supported_on` + `support_status` | yes | hardware/producer axis: `supported, placeholder, unsupported_for_profile, unverified` |
| `release` | yes | decoding axis: `confirmed, profile_dependent, unresolved` |
| `source`, `confidence` | yes | catalog record/TXT lines, rationale |
| `producer_evidence` | if available | build, image SHA-256, store offset (REVIEW2 §5.3/§5.4) |
| `default_enabled` | yes | part of the template if `requires` is satisfied |
| `internal` | optional | for internal mandatory reads (§9.3), creates no entity |

Only `support_status: supported` **and** `release ∈ {confirmed, profile_dependent}` yield physical sensors. Everything else at most as an explicitly labeled, disabled-by-default raw diagnostic (raw value, no unit, no statistics); `placeholder` is not offered at all.

Groups and default intervals (GRUNDLAGEN §1.2):

| Group | Interval | Content |
| --- | --- | --- |
| `identity` | start + after reconnect, then 3600 s | R0, R1, R2, R3–R7 (ASCII SN), R14–R18, R20/R21, R22 (MPPT high byte / phases low byte), R23, R24 (battery input code), R25–R28, **R65**, R336 |
| `status` | 10 s | R500, R551–R558 (all four fault words raw + known bits) |
| `battery` | 10 s | R586–R588, R590 (raw diagnostic only, factor unresolved), R591, R592. **R589 and R593–R596 are battery 2** and `placeholder` on SG04LP3 (1151/1175 zero-filled) |
| `ac` | 10 s | R598–R600, R603–R619, R621–R638, R640–R646, R650–R655, **R656–R659** (load high words for R650–R653) + required high words from 687..708 per catalog; **never R620/R709 as a pair**; R654/R660 (apparent power) not released |
| `pv` | 10 s | MPPT1: R672 power, R676 voltage, R677 current; MPPT2: R673, R678, R679. R674/R675/R680–R683 (PV3/PV4), R530–R533, R718–R721 = `placeholder` (zero-filled in 1151/1175); PV5–PV10 and AI-Arc R722–R748 = `unverified`/`unresolved`, not offered |
| `settings` | 300 s | R80, R98, R111, R133, R146, R148–R177 (ToU) — diagnostic sensors only; R166–R171 raw diagnostic only |
| `energy` | 60 s | Only the whitelist of active kWh counters (§5.1); read group ≠ semantic type |

The addresses to be read are **derived from the dependencies of the enabled sensors** (incl. non-adjacent partners), not from fixed ranges. The profile validator requires exactly one low word and one high word for every u32/s32 sensor; excluded sensors do not smuggle in partners.

### 5.1 Energy whitelist (v1)

Active kWh counters with `scale_source: energy_resolution` (REVIEW2 DR2-01):

| Layout | Addresses and meaning |
| --- | --- |
| **one** 16-bit field each (daily values) | R514 battery daily charge; R515 battery daily discharge; R520 daily grid import; R521 daily feed-in; R526 daily load energy; R529 PV daily energy total |
| Low/High, **one** 32-bit entity per pair (total values) | R516/R517 battery charge; R518/R519 battery discharge; R522/R523 grid import; R524/R525 feed-in; R527/R528 load energy; R534/R535 PV energy total |

No pairing of 514/515 or 520/521; R526 is not part of R527/R528. Take low/high roles from the catalog groups (`total_516_517` …). Signedness and release per entity. GEN R536, R537/R538 are a separate catalog decision (GEN port predicate), not automatically R65-scaled. R501 is decided separately (S16, not automatically monotonic). **Not** as R65-scaled kWh: R502/R506/R507 (reactive energy), R503 (grid duration s), R508/R509 (write CRC), R510/R511 (reserved), R512/R513 and R539 (GEN runtime), R536–R538 (GEN energy, GEN is a v1 catalog option, not the default). `state_class` per actual quantity, not per group.

### 5.2 Release classes

Every catalog entry has the mandatory fields `release: confirmed | profile_dependent | unresolved`, `source` and `confidence`. `unresolved` appears at most as a clearly named raw diagnostic (raw value, no physical unit, no statistics). Masks, shifts, enums and component layouts are expressed in the profile. Offset convention: `value = (decoded_raw + raw_offset) * scale`. Explicit conflict cases: R590 (factor 1 W vs. 10 W) and R166–R171 (ToU SOC split) as raw diagnostic only until proven.

Decoding rules (GRUNDLAGEN §1.4–1.6): 32 bit = `(high<<16)|low`, then sign, then scaling. Temperature R586 = `s16/10`, R217/R541 = `(raw-1000)/10`. R336: parallel enable bit0, master bit1, phase bits8–9, SN `(raw>>10)&0x3f`. Fields with `scale: null` in the catalog are not silently set to 1: either explicitly decided in the profile (with rationale in `notes`) or not published. Known placeholders (R647–R649, apparent-power aliases) are not published.

**Energy resolution:** According to V105.8 R65 ("DSP protocol version"): `R65 <= 0x0104` → 0.1 kWh, otherwise 1 kWh. Applies only to the whitelist §5.1. The read R65 value, the chosen resolution and the generation are published as app/inverter diagnostics (R65 is no longer a default sensor, only an energy dependency); override per inverter via `energy_resolution: auto|0.1|1` (default `auto`, in the Supervisor schema). In `auto`, energy values are released only after a valid R65 of the **identified** device; no default factor on a missing response, timeout ≠ R65=0.

**Statistics generation (DR2-02):** Per inverter, `{effective_resolution, source: r65|override, generation}` is persisted in `/data/energy.json`. A normal reconnect with an unchanged factor changes nothing. If the **effective** factor changes (through R65 or through an override change, detected independently of each other), `generation` is incremented: energy entities carry the suffix `_g<generation>` (starting at `_g0`) **from the very start** in the component key, `unique_id` **and** `default_entity_id` (the HA recorder uses the entity_id as statistic_id); the state payload contains `energy_generation`, and every energy template returns a value only for a matching generation (otherwise `none`). On a change, the energy entities receive new IDs (new HA statistics series), the old components are removed from discovery (history is retained in HA), diagnostic message + notice in the status view. No reinterpretation of old points, no artificial deltas, no statistics point under the old generation after the change. A restart in the middle of a change resumes it idempotently. **Device replacement:** If a different inverter SN is bound after explicit rebinding, a new generation also begins (even with the same factor); reconnect, switching away and re-checking the same SN change nothing; a simultaneous SN and factor change migrates once. The persisted energy epoch carries the SN. **R65 confirmation:** two *consecutive* valid identical readings; a missing/invalid reading resets the confirmation; the confirmation reading takes place promptly (e.g. after 60 s), not only in the identity interval. **Persistence fail-closed:** if `identity.json`/`energy.json` is missing, this is a first installation; if an existing file is damaged, unreadable or has an unknown schema, binding or energy publication is blocked (diagnostic) until the user explicitly resets; never silently rebind or reuse `_g0`. The capability cache may be rebuilt after diagnosis. `energy.json` and `energy-generations.json` are evaluated together; only "both missing" is a first installation; damaged or contradictory history blocks energy values (`energy_store_corrupt`/`energy_history_inconsistent`). Next generation = `max(high-water, record)+1`; high-water and reserved transition are written before the record. No time-based substitute: after history loss, the reset starts a random, persisted 8-character namespace at `g0`. **Energy series suffix:** opaque `energy_series` (`g<n>` or `<token>g<n>`, regex `^([a-z0-9]{8})?g[0-9]+$`) for component key, `unique_id`, `default_entity_id` and template guard; a published series never becomes a measurement source again. **Identity reset:** isolated per inverter; if the file as a whole is damaged, global (all inverters offline, epoch +1, re-identification). Energy counters: `device_class: energy`, `state_class: total_increasing`; invalid/missing values → `unknown` (never invent 0).

## 6. MQTT / Home Assistant

**Normative implementation contract:** `KB/.orch/reviewer-codex-6.1-t8-contract.md` (HA Core 2026.10.4, aiomqtt 2.5.1, MQTT 3.1.1) — in case of contradictions it takes precedence over this section.


- One HA device per inverter: `identifiers: [deye_<instance_id>_<inverter_id>]`, `serial_number` from R3–R7, `sw_version` decoded from R15/R17–R18, `model` "SUN-…-SG04LP3" (rated power from R20/R21), `via_device` = app device.
- `unique_id` and topics with `instance_id` (§8.1); check `default_entity_id`/`object_id` for compatibility with current HA.
- Availability: app LWT `<topic_prefix>/<instance_id>/status` + per inverter `<topic_prefix>/<instance_id>/<id>/availability`, `availability_mode: all` (§8.1). `expire_after` per group (3× interval).
- State: one JSON payload per inverter and group (`<topic_prefix>/<instance_id>/<inverter_id>/<group>`), sensors via `value_template` without default.
- No bank sums in v1 (no SOC addition or the like).

## 7. Packaging

Repo root = HA app repository: `repository.yaml`, app directory `deye_monitor/` with `config.yaml`, `Dockerfile`, `rootfs/` (s6-overlay v3, `init: false`, service calls `exec python -m deye_monitor`), `translations/en.yaml` + `de.yaml`, `README.md`, `DOCS.md`, `CHANGELOG.md`, `icon.png`/`logo.png` (placeholder ok), `LICENSE` (MIT). Archs: `aarch64`, `amd64`. `stage: experimental`. `services: [mqtt:want]`, `uart: true` static (manifest right, no runtime condition; only configured `serial_device` paths are opened, no device search), no `host_network`, no `full_access`/`privileged`. Ingress for the sensor selection (§9).

**Build layout (binding):** All build inputs live in the app folder `deye_monitor/` (Supervisor build context): `pyproject.toml`, `uv.lock`, `src/deye_monitor/`, profile YAMLs as **package data** under `src/deye_monitor/profiles/`, frontend assets as package data. No `COPY ..`, no runtime dependency on the knowledge base. Root CI works with `working-directory: deye_monitor`. Acceptance: fresh build from the app context only, then in the image import + profile validation + start with a fake configuration. Versions of base image and Python dependencies pinned; before pinning, check the current state at https://contributors.home-assistant.io/apps/.

## 8. Quality

`uv` for dependencies, `ruff` (lint + format), `mypy --strict`, `pytest` + `pytest-asyncio`. GitHub Actions workflow: lint, typecheck, tests, Docker build amd64/aarch64, manifest validation. For test requirements see GRUNDLAGEN §7.2 — in particular wire-level guard tests, stream fragmentation at every byte position, golden decoder values, fake servers (Modbus TCP, RTU-over-TCP, V5) for integration tests, discovery without `command_topic`.

## 8.1 Normative contracts from spec review (SR-03 … SR-13)

**Guard contract (SR-03):** A guard call checks **exactly one complete** frame; all bytes sent belong to it (no leading/trailing bytes, no two ADUs). FC03 PDU exactly 5 bytes; RTU ADU exactly 8 bytes with correct CRC; MBAP: protocol ID 0, correct length field, exact PDU end; V5: correct length/checksum, request control 0x4510, fixed metadata layout, inner RTU CRC, logger SN = configured SN. `0 ≤ start`, `1 ≤ count ≤ min(125, max_read_registers)`, `start+count ≤ 65536`; unit = configured target unit. Exactly the checked immutable bytes are sent (`bytes`, no subsequently modified buffer); replay/reconnect and UART write point run through the same guard. Negative tests: valid read + FC06/0x10 suffix, padding, two FC03 frames, truncated frames, forged MBAP/V5 lengths, wrong CRC/checksum, foreign logger SN, address overflow; plus positive edge cases.

**Snapshots and freshness (SR-04):** A sensor value is formed only from raw words **of the same cycle** (cycle ID). If a word/partner is missing, **that** entity is `unknown` (JSON `null` in the state payload, template without `default(0)`), no carrying over of old values, no statistics point; other values of the group remain valid. Group payload contains cycle ID and timestamp.

**Identity binding (SR-05, SR-09):** Before operating data, only identity is read and normalized (10 printable ASCII characters, otherwise invalid). `expected_serial` takes precedence; otherwise the first valid SN is bound to the logical inverter ID and persisted in `/data/identity.json`. Every reconnect, fallback and failback checks against the binding; deviation → inverter `quarantined` (offline + diagnostic), no operating data. Rebinding only by user action (Ingress button "Rebind identity" or options change). Caches/snapshots are discarded on transport change; never mix words from the primary and fallback path. The unit ID may be overridden per connection (`unit_id` on the inverter, optional `fallback_unit_id`). Canonically identical endpoints (same host:port or same resolved serial path) are put on the **same** bus lock or rejected as a duplicate; the same inverter via two paths is not a double-address error, two devices with the same unit on one bus are. The bus lock covers identity, probes and data.

**Availability (SR-06):** Every component has `availability: [app LWT, inverter availability]` with `availability_mode: all`. App LWT retained `offline`, retained `online` on start. Inverter availability retained, `offline` first on app start, `online` only after identity and data release. After HA birth: discovery, then fresh states.

**Instance ID (SR-10):** Persistent `instance_id` (default: generated on first start, 8 characters, in `/data`, overridable as an option). Discovery topics, `identifiers`, `unique_id` and state topics contain it: `unique_id = deye_<instance_id>_<inverter_id>_<key>`. The app device is itself registered as a device (diagnostic sensors), otherwise no `via_device`. The managed discovery inventory is stored in `/data/discovery.json` so that cleanup deletes only own topics even after a restart.

**Limits (SR-11):** Time values finite and > 0, `request_gap_ms ≥ 0`, poll intervals ≥ 1 s, queue lengths bounded (default 32 per bus), unknown options → error. The request deadline applies to the **entire** request (not per byte). Buffers/frames bound to transport and requested length (V5 max. 512 bytes, RTU max. 5+2·125 bytes). Faulty sources → backoff. All documented as application limits, not as Deye limits.

**RTU resynchronization (SR-12):** After a timeout or error, input data is discarded (clear receive buffer, wait for silence ≥ 3.5 characters or 50 ms, for TCP re-establish the connection if necessary) before the next request is sent; no fictitious transaction IDs for RTU. `serial_rtu` is tested via a PTY pair (partial reads, CRC, timeout, EOF/hot unplug, reopen, guard at the UART send path).

**MQTT acceptance (SR-13):** Unit tests may mock; T12 binding uses a local Mosquitto test broker (Docker or binary, never a plant broker) and checks retain, LWT on hard termination, broker restart, HA birth, cleanup after restart. Discovery payloads are additionally checked against the HA MQTT schema (pinned `homeassistant` version in the test or comparable schema check).

## 9. Hardware profile, plant features and sensor selection (Ingress)

Goal: maximally user-friendly; only what is needed is polled. Four separate layers (REVIEW2 DR2-04/05): **hardware capability** (profile) → **usage** (plant features) → **template** (`default_enabled` + `requires`) → **user selection** (deviations). Templates change only the default selection, never access rights.

### 9.1 Hardware profile `sg04lp3_eu`

- Two MPPTs (manual §3.6.1): 5/6/8 kW 1+1 strings, 10/12 kW 2+1 strings; channel = MPPT, two strings on one MPPT remain one channel. One battery input. GEN port present.
- The user selects the model (rated power 5/6/8/10/12 kW) or it is checked for plausibility from R20/R21; R22 (MPPT/phases), R24 (battery input code), R0/R14 are read and checked against the profile. Contradiction → `capability_conflict` in the status view, **never** automatic enabling of additional channels. R28 is not interpreted as "4 = SG04".
- Capability cache per `(SN, R15/R17/R18 version, profile revision)`; a firmware/identity change invalidates it.
- `placeholder`/`unsupported_for_profile` are enforced server-side: even a manipulated API request cannot enable them as a physical sensor.

### 9.2 Plant features per inverter

| Feature | Values |
| --- | --- |
| `utility_grid` | yes/no (grid operation vs. island) |
| `grid_port_use` | `utility`, `generator` (R189), `unused` |
| `battery` | yes/no; `bms` yes/no (sub-condition) |
| `gen_port_mode` | `unused`, `generator`, `smart_load`, `ac_coupled` (reconcile with R133) |
| `mppt1_used`, `mppt2_used` | yes/no |
| `parallel` | yes/no |

`requires` uses predicates (`all_of`/`any_of`) over these features, e.g. GEN power: `gen_port_mode in [generator, smart_load, ac_coupled]`. Register mapping per measurement location according to REVIEW2 §4 (grid_terminal, battery, bms, gen_port, inverter_output, ups_load, pv, parallel, tou, diagnostic). Diagnostics (identity, R65, R80, R98/R111, R133, R336, R500, R551–R558) are **always** available, independent of features. Suggestion of the features from read settings (R111, R133, R189, R336) on first connection, user confirms.

### 9.3 Read plan

`read_set = mandatory_identity_health + selected_sensor_dependencies (+ later active_operation_dependencies)`. Mandatory base reads (identity incl. SN binding, R336 role, R500/R551–R558, R65 only if an energy entity is active and `energy_resolution: auto`) run even with an empty selection, but do not automatically create entities. Multi-word sensors pull in exactly their partners. Blocks are formed only over known, readable addresses. **Plan budget:** block count × (response time + gap) is checked against the interval; overload is shown in the UI and fairly coalesced (no endless catch-up loop).

**First start (`awaiting_selection`):** As long as no selection has ever been saved for an inverter (new installation or newly added inverter), its active selection is empty: only the mandatory base reads and the internal hint reads (R111/R133/R189/R336) run, no inverter entities are created, only the app diagnostics. The template is then merely the interface's suggestion. With the first save (any change or an explicit "Apply template") normal polling begins. Selections already saved by older versions count as confirmed; without the enforcement (`require_choice`, always on in the app) the store behaves as before.

### 9.4 Runtime observation (FC03 only, display only)

Per signal: number of responses, zero share, min/max, variability, daylight samples, firmware/SN/profile revision. States `observed_active`, `zero_only_inconclusive`, `known_placeholder`, `read_failed`, `capability_conflict`, `alias_suspected`. Observations **never** silently change `support_status`/`release` and do not switch off sensors; they appear as a hint in the UI ("has been delivering 0 during the day for X days – deselect?"). Weeks of 0 do not prove missing support. No address-space scans, no probes outside the curated catalog.

### 9.5 Ingress UI

- Features per inverter (with suggestion from 9.2), searchable catalog grouped by category/measurement location with checkboxes; deviations from the template marked and individually resettable; hints from 9.4; plan budget display. "Apply to all inverters" with preview and capability check per target device (no transfer of SN binding or write enablements). Status view: connection path, identity (SN, firmware, role from R336), capability conflicts, energy generation, last successful poll, error counters. Action "Rebind identity" with explicit inverter selection.
- **First start:** Card and setup show "Please select sensors" (link to the setup); quick actions in step 2: "Base only" (serial number, operating state, fault words 1–4) and "Deselect all", each as ordinary deviations from the template with preview and save; "Apply template unchanged" ends the state without change.
- Technology: `ingress: true`, internal port, relative URLs, no host port exposure. Lightweight frontend without a build chain (Lit/Preact as vendor files in the package, no CDN requests), backend `aiohttp` in the same process, JSON API, HA look incl. dark mode, DE/EN, keyboard operation.
- **Security (DR2-08):** Connections only from the Supervisor peer `172.30.32.2`, otherwise 403; evaluate `X-Remote-User-*`/Forwarded headers only after this peer check; missing user information does not count as admin. `panel_admin: true`. State-changing calls only via POST/PUT with revision number (`If-Match`-like) and CSRF token; never via GET. Request schema only known catalog keys/features, no free registers/paths/frames. Sensor checkboxes cannot set `allow_writes`. Rebinding invalidates old snapshots, capabilities and (later) write permits.

### 9.5a Addendum T14

The Ingress UI additionally contains the section "Set up plant" and the setup wizard (§4.1); the navigation (Overview / Set up plant) is in the page header.

Change contract (Review 9): A proposed generation is *provisional* until the durable plant commit: it writes no shared files (`identity.json`: bindings/`expected_serial` only in memory, written only after the commit; at start, expected bindings are derived from the accepted plant) and its publisher does not run (no discovery, no tombstones). If a change is rejected, selection specs and records are reset exactly from the transaction state; if the rollback fails, nothing is operated further (`plant_recovery_failed`, changes locked until restart). Running connection tests belong to the generation and are aborted and awaited before decommissioning. A stale selection record of deleted IDs is removed before any further plant change (and at start); if that is not possible, the plant remains unchanged (fail closed).

### 9.6 Persistence and atomic switch (DR2-07)

`/data/selection.json` with `schema_version` and monotonic `revision`; migration with stable keys, unknown/obsolete keys are reported and discarded. New selection: validate → adopt (runtime/publisher) → durably accept as a two-slot document (`selection.a.json`/`selection.b.json`, each complete with sequence and checksum, atomic tmp + fsync + rename into the slot that does not hold the newest valid state); success is reported only afterwards, any error (adoption or persistence) rolls back the adoption and leaves the accepted state intact; if rollback is not possible, the inverters are locked (`selection_recovery_required`). On load, the newest fully valid slot applies. The revision is bound to plan, cycle and publish job: responses of an old plan are no longer published after an entity is removed/redefined. The discovery inventory (`/data/discovery.json`) is reconciled with the revision; removed components are taken out of the device discovery in a targeted way without deleting others; a restart in any transition is idempotent.

### 9.7 Plant totals (0.2.7)

Totals over the member inverters live on the app device (`deye_<instance>_app_plant_*`, state topic
`<prefix>/<instance>/app/plant`). Members: every inverter except `totals.exclude` in the plant file
(default all; `allow_single` keeps the device for one member; fewer than two members: no plant
entities, they are retired by the normal cleanup). Computation (`totals.py`): newest value per
member, stale after 3 poll intervals of its group, any missing/stale/`null` member value makes the
total `null` (never a partial sum); members without PV entries add 0 to PV. Energy totals are sums of
the members' total counters; the plant series `g<n>` (`/data/plant-totals.json`: generation +
signature of members and their own series) advances when the membership or any member series
changes and is never reused (after a lost file it continues after the highest series in the discovery
inventory; a saved generation behind the discovery high-water mark is never reused either: the next
one above both is allocated and saved first). If saving the generation fails, no plant entities or
states are published for it; the save is retried on each plant cycle and the entities appear once it
succeeds. Battery state comes from the single member whose R336 says parallel master. Battery
power per member is battery voltage x battery current (positive = discharging); the BMS bank view is
`-(BMS voltage x BMS current)` because R216 is positive while charging.

## 10. Write layer (T13+, after acceptance of the read part)

Framework: per inverter `allow_writes: false` (default); without enablement, behavior exactly as in §1. Before implementation, a separate write contract is accepted that satisfies at least REVIEW2 DR2-09 through DR2-13:

- **Guard (DR2-09):** separate, typed read/write operations; FC16 = 0x10; count 1..123, byte count `2n`, PDU `6+2n`, RTU ADU `9+2n`, MBAP total length `13+2n`; V5 envelope unchanged strict. Check write response echo (FC/start/count), exception 0x90. Every operation binds SN, identity/profile revision, endpoint, unit, permitted value encoding and mask. Re-check `allow_writes` + permit **immediately when sending**; fallback/rebinding/profile change revokes permits. No global "FC16 allowed".
- **Bit fields (DR2-10):** own bit mask/enum per operation; read-modify-write on the same identity-checked connection within the bus lock; foreign bits are preserved; report a conflict instead of overwriting. ToU plans as a domain-level multi-register operation.
- **Result (DR2-11):** status `rejected, sent, readback_confirmed, failed, outcome_unknown`; no automatic retry, no write fallback to another endpoint, no auto-rollback. HA telemetry always shows the read value; command result separately.
- **Authorization/MQTT (DR2-12):** all inputs check the current server-side enablement; deactivation revokes queues/permits and removes command subscriptions; no commands replayed after reconnect/restart; expiry, deduplication, boot/enablement generation; retained commands are ignored.
- **Whitelist (DR2-13):** starts empty; every function individually with profile/firmware/precondition/verification contract and explicit user decision. Topology, protection, battery type, factory, reset, watchdog functions (among others R80, R111, R336, R341, R1100–R1121) not as a by-catch; R336 not as the first demo. Live writes are performed exclusively by the user.

## 11. Not in v1

Bank aggregation; HV battery packs; LV pack registers 10000+; own HA integration.
