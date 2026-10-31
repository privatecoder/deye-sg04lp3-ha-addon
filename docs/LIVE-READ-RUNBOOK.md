# First live read test: Deye SG04LP3-EU in Home Assistant

Runbook for four SUN-10K-SG04LP3-EU in island parallel operation, each with one RS485 LAN gateway **on the ModeBUS** and one WiFi logger. Reported firmware: HMI `1001-C070`, Main `2005-1175-1807`. This document prepares the test remotely; no plant connections were made while writing it. This version is based on the reviewer's runbook (Codex, 29.10.2026) and has been brought up to the **current app state** (web interface, two-slot selection, diagnostic entities). You set up connections and inverters **in the web interface** (wizard "Set up plant"); the app options hold only MQTT and the log level.

The app is **read-only**: it sends only FC03 read accesses to inverters (see section 9).

## 1. Start prerequisites – tick these off first

- [ ] A **specific version** (commit or image digest) is approved for the read test: FC03 send guard, transport tests, MQTT/HA publishing, error handling and the E2E acceptance with a real Home Assistant (`e2e/`). Do not test an unchecked working tree. The version number `0.2.7` alone does not identify a state: note the commit/digest in the result sheet (section 10).
- [ ] The earlier re-verification findings N2-01 to N2-04 (energy namespace, isolated identity reset, restart after blocking, reconnect/confirmation states) were fixed in the fix rounds T7-Fix 2/3; the approval from the first item confirms that the approved version contains these rounds.
- [x] **Before your first selection the app reads only identity and status.** An inverter without a saved selection shows "Please choose sensors" (section 3.1); normal reading begins only after saving via the web interface. There are no options such as `selected_keys` and no files in `/data` are changed by hand.
- [x] Identity, active selection/read plan and connection errors can be inspected: web interface (overview, polling load per selection) and the diagnostic entities of the inverter devices (section 7).
- [ ] You can reliably stop the app via HA and have an independent, already working plant monitoring. Critical consumers do not depend on new sensors for this test. In case of a fault a contact person on site is reachable; a manual restart from remote is not assumed.
- [ ] The HA MQTT integration and broker are operational. Configuration and existing app data are backed up (HA backup including add-on/app data). On the first attempt do **not** add energy entities to the Energy Dashboard and no new entities to control automations.

In **Settings → Apps** (called "Add-ons" in older HA versions) open the test app. For this app **switch off Start on boot, Watchdog and automatic updates**; leave existing plant controls unchanged. Keep a second browser tab open with the app info page and the **Stop** button. App output appears in the Log tab; `/data` is preserved across restart/update. You open the web interface via the sidebar entry **Deye Monitor** (HA administrators only). [HA: app lifecycle](https://contributors.home-assistant.io/apps/how-apps-work/)

## 2. Look up connection data yourself – do not scan or reconfigure anything

Fill in this table from your existing configuration, reachable status pages and existing nameplate photos. Historical KB addresses are no confirmation of your setup today.

| Device / existing role | Inverter SN, exactly 10 characters | Gateway model/firmware, LAN IP:port | Gateway protocol / unit ID | Logger LAN IP:port | Numeric logger SN / unit ID |
| --- | --- | --- | --- | --- | --- |
| deye1 / … | … | … | … | … | … |
| deye2 / … | … | … | … | … | … |
| deye3 / … | … | … | … | … | … |
| deye4 / … | … | … | … | … | … |

**Keep three identifiers apart:** the inverter SN identifies the physical device; the numeric logger SN addresses the Solarman V5 logger; the Modbus unit ID addresses the inverter via a particular path. The logger SN is neither the WiFi password nor the inverter SN. The parallel role/number from R336 is not sufficient proof of the right unit ID on every transport; the Modbus address field R1 is only a reported field, the required address must be checked per connection path. Separate gateways may each use unit ID 1; do not reconfigure anything to 1 or to consecutive 1–4.

### Gateway: only look at existing settings

Open the **already known** LAN web address. Record model/revision, firmware, TCP server port and the existing protocol conversion; depending on the Waveshare model the pages are called e.g. "Quick Configure", "Application Setting", "Transfer Protocol" or "Mode Selection".

| Framing actually configured | App transport | Distinguishing feature |
| --- | --- | --- |
| Modbus TCP → Modbus RTU | `modbus_tcp` | TCP side with MBAP header; RTU CRC is handled by the gateway |
| Transparent serial TCP tunnel | `rtu_over_tcp` | TCP carries RTU frames including CRC |

**Port 502 does not prove the framing.** Use the existing working client configuration and the manual matching the exact model. If the mode is unknown, postpone the test; do not guess by switching drivers or reconfiguring the gateway. Only document baud rate, parity, stop bits and unit ID. Do not perform any "Save", "Apply", "Download", "Clear", "Reset", firmware update or restart action.

Also check existing JSON/MQTT/HTTP upload functions, register tables, polling/cache/storage mode, TCP clients and retries. Some variants manage queries internally and may keep polling on their own. The exact functions depend on the model. For B variants the manufacturer's manual explains JSON configuration, register queries and upload times; its "Serial port polling time" is the pause until the next entry, not necessarily its repetition period. [Waveshare-B: MQTT/JSON manual, pp. 3–10](https://files.waveshare.com/upload/a/a6/EN-RS485-TO-ETH-B-MQTT-and-json-user-manual2.pdf)

An earlier master export of the knowledge base (47 entries, including `Query Period=1000`) proves no configuration active today and no particular number of requests per second. Its `last_update` comes from "Current Time", not from an inverter freshness counter; its `online` reads R1. A fresh JSON timestamp alone therefore does not prove fresh measurements. JSON download tables may contain command paths: **do not import or trigger any table as a test**.

### WiFi logger: confirm IP and serial number

Use the known LAN IP, the router device list, existing Solarman device details or an existing photo of the logger label. In the reachable logger web interface "Status" is the appropriate information page. The LSW-3 manuals additionally describe AP setup; `10.10.100.254` belongs to this AP access, not automatically to the LAN reachable from remote. Do not perform any WiFi change, setup wizard, reset or reboot.

The plan is **WiFi logger by IP, Solarman V5**, usually port 8899, with the numeric logger SN. No physical serial logger path. The port alone does not confirm a successful V5 connection. Do not assume native Modbus TCP in the logger.

### Record other pollers and existing controls

| Existing service | Which device / which path? | Interval / client count / last data gap | Required for control? |
| --- | --- | --- | --- |
| Waveshare-internal JSON/MQTT/HTTP query | … | … | … |
| HA Modbus, ha-solarman, sunsynk, SolarAssistant, evcc, custom scripts | … | … | … |
| Solarman cloud and logger upload | … | … | … |
| EMS/automations that derive actions from this data | … | … | … |

Do not start an additional debug app or Modbus console in parallel. Local pollers that are not needed can be paused **only by you**, after checking their control dependencies, for a limited test window. Do not blindly switch off any critical automation. Leave the cloud active and watch its updates; do not determine an unknown maximum logger client count by load tests. Gateway on the ModeBUS and logger are different paths; a shared electrical RS485 bus is **not** proven by that. Nevertheless inverter resources may be limited. App-internal `bus_id` queues do not coordinate foreign clients, cloud queries or gateway autopollers.

## 3. Prepare baseline and minimal selection

1. Observe the existing monitoring for at least 15 minutes, longer with slow cloud upload. Note time/time zone, faults/alarms, cloud update interval, existing timeouts and logger connection drops. A plant that is already unstable is not suitable for a first test.
2. Pick one inverter whose two communication paths and inverter SN are both known. The designation "master" does not set a preferred test order; do not change any parallel role.
3. Stop the app, note the approved version and the complete options. Keep `instance_id` and the inverter `id` stable for all following stages. Do not run several app instances with the same IDs at the same time.

### 3.1 First start: "Please choose sensors"

As long as a selection has **never** been saved for an inverter, the app reads only the mandatory identity and status registers and the internal hint registers (see below) – **no sensors of the default template** (otherwise it would be 57 sensors and 29 read blocks including energy and R65) – and no inverter entities are created, only the diagnostic entities of the inverter's device. 

1. Start the app (the app options stay empty except `instance_id`) and create the connection and inverter in the setup wizard of the web interface (stage 1, section 4).
2. In the web interface (sidebar **Deye Monitor**) the inverter's card shows **"Please choose sensors"** after identification. Open **"Choose now"** there (or **Set up sensors**).
3. Answer step 1 "Describe your plant" **truthfully** (island/grid, battery yes/no, BMS, GEN port, PV inputs, parallel operation). A note "The device setting reports …" is **no proof of the installation** and changes nothing by itself; your answers stay saved.
4. Step 2 "Choose sensors": **"Basics only"** selects `serial_number` (serial number), `operating_state` (operating state) and `fault_word_1` to `fault_word_4` (fault words 1–4) and deselects everything else; **"Deselect all"** selects nothing. Both are ordinary deviations from the template (individually resettable, preserved across later changes to the plant description).
5. The bar at the bottom shows the **polling load**, "What saving changes" the sensors that drop out. Expected for "Basics only": **6 sensors, 11 polling blocks**, load "low", no energy, no R65, no battery/AC/PV group.
6. **Save.** The selection counts as saved only once it is stored durably; after that the note "Please choose sensors" disappears and normal reading begins – with the small selection.

If you want the template unchanged, use **"Accept the template unchanged"**; not recommended for this test.

"Identity and status only" additionally comprises necessary **internal read** queries: R0, R3–7, R14–18, R20–22, R24, R336 as well as R500 and R551–558. For the feature hints the app also reads R111, R133 and R189 (group "Settings", diagnostics only). These are no write accesses. With `max_read_registers=20` and without gap bridging this gives **11 read blocks** per complete plan (identity 6, status 2, settings 3), not 11 requests every 30 seconds: each group has its own interval; identification and recovery may trigger additional probes. R65 is not needed in this selection. In the state "Please choose sensors" the same mandatory and hint registers are read.

## 4. Stage 1 – one inverter, gateway only

Setup is done in the web interface (sidebar **Deye Monitor**): with an empty plant the **setup wizard** starts by itself, otherwise you open **Set up plant** (or "Start setup" on the empty overview). The wizard leads through **create connection → create inverter → check connection → choose sensors**. Changes apply immediately; the app does not need a restart for this.

1. **Connection** (step 1): type **"Gateway (Modbus TCP → RTU)"** – only for a confirmed Modbus TCP→RTU mode –, address (e.g. `192.0.2.11`) and port `502`, identifier `gw1`. Under **Advanced** set cautious start values: timeout `5`, block size `20`, gap `250`, queue `8`. For a **demonstrably transparent** gateway use **"Gateway transparent (RTU over TCP)"** instead, with its actually configured port. There an optional **late-answer window** of `10` s can be set: with timeout 5 this corresponds to today's default (`max(1 s, 2 × timeout)`). These ten seconds are an **assumption about late answers**, not a Deye guarantee. With unknown delays/cache effects do not test further; no unconfirmed answer is published as fresh.
2. **Inverter** (step 2): name `Deye 1`, identifier `deye1` (cannot be changed after creation), connection `gw1` from the selection list, Modbus address `1`, **expected serial number** `2401230001` (the confirmed inverter SN), energy resolution "Automatic". Under **Polling intervals (advanced)**: `30` / `300` / `600` / `3600` s. Writing is not available in this version (the interface shows "Writing: not available", `allow_writes` cannot be set there).
3. **Check** (step 3): "Check connection" reads only the inverter's identifier (FC03, through the send guard, via the bus scheduler of the running connection) and shows serial number, MPPT/phase count and anomalies. On an error the display names typical causes (e.g. "Wrong transport? Check the gateway mode"). The same check is available later per connection and per inverter under **Set up plant**.
4. **Sensors** (step 4): continue with section 3.1 using **"Basics only"**.

`max_read_registers` (block size) `20`, 250 ms gap and these intervals are cautious start values, not load limits confirmed by the manufacturer. Normal queries may be shorter. A queue of `8` does not allow eight simultaneous transactions on the same app bus.

5. After saving the inverter, polling begins immediately. From now on keep logs, abort criteria and independent monitoring in view at the same time. The inverter first reads only identity and status (section 3.1); then save the minimal selection.
6. Within the first minutes check identification, bound SN and fresh status data (section 7: state "Online", entity `…_serial` = `expected_serial`). `ready` in the log only confirms started loops, not successful device contact. On a wrong SN/quarantine ("Quarantined") stop immediately, check the assignment outside the app; **no identity reset** to bypass an error.
7. Observe for 15 minutes. Expected: recurring successful FC03 cycles without a growing queue, repeated cooldowns or new faults. Power values unchanged for minutes are not a test failure with this selection: no power values are read yet.
8. With a stable course, additionally enable `battery_voltage`, `battery_soc`, `inverter_frequency`, `pv1_power`, `pv2_power` in the web interface (search, tick the checkbox) and check the preview. Expected: **11 sensors, 14 polling blocks** (additionally battery 1, AC 1, PV 1), still without energy and R65. Save and observe for 15 minutes, preferably in daylight. No diagnostic release for PV3–PV10 or battery 2 (the expert raw values stay deselected).
9. Select energy only afterwards and separately (energy counters via the web interface search "Energy"), check R65 and its resolution and wait for at least one energy cycle. Do not change the path together with the sensor selection: only one influencing factor per change. Afterwards stop the app and log the last approved state.

`mqtt: {}` uses the already configured Supervisor MQTT service; it does not start a broker. Missing HA entities with a working connection are to be investigated separately, without forcing faster device polling. Use `info` at first; only if needed a limited debug window, and before passing on remove IPs/SNs/credentials from logs.

## 5. Stage 2 – read the same inverter via the logger, one after the other

Only after a successful gateway stage. Under **Set up plant** create a second connection **"WiFi logger (Solarman V5)"** (identifier `log1`, address e.g. `192.0.2.21`, port `8899`, **serial number of the WiFi stick** `1700000001` – the stick's serial number, not the inverter's; Advanced as in stage 1) and try it with "Check connection". Then edit the inverter `deye1` and choose `log1` as its connection; identifier, `instance_id`, expected inverter SN and selection stay the same (the selection hangs on the inverter identifier, not on the path). The previous connection `gw1` stays created for now, but is no longer polled once no inverter uses it; the **gateway polling of this app** is thus ended before the logger is polled (the rebuild stops the old polling and starts the new one). Again first the minimal selection, then the same small measurement selection as in stage 1. Do not poll both paths in parallel, and do not set a fallback connection for now.

Observe each selection for at least 15 minutes and several normal cloud upload cycles. An existing gateway autopoller may keep working although this app uses only the logger: note this state in the comparison. There is no documented universal client/polling limit for your logger/firmware combination.

Compare the **inverter SN exactly**, also static raw data/firmware identifier, R22/R24 and – only if read – R65. A transport change must neither substitute another physical device nor treat the energy statistics as a new physical source (with the same SN and the same resolution the energy series is kept). With known recovery/persistence problems stop, do not delete files.

Compare dynamic values in time windows with timestamp, minimum/maximum and operating state. PV/power values captured one after the other need not be exactly equal; changing sun and load are normal. Cloud values may be considerably older. Both paths read the same inverter and are not two independent measuring devices. An unchanged counter or status does not prove frozen data; natural changes of an additional measured value give better hints. No load/switching attempts to "stimulate".

## 6. Stage 3 – extend to four inverters

For each further inverter first check its own gateway/logger assignment. Then enable the same small selection step by step for **two, three, four** inverters: each time add the connection and the inverter under **Set up plant** (changes apply immediately, the remaining inverters continue after a short restart of polling): the new inverter shows "Please choose sensors" and reads only identity and status until you have saved its selection (with "Basics only" or with "Transfer to others …" from an already set-up inverter; serial number binding, identity, energy history are never transferred in the process). After saving, observe for at least 15 minutes. Do not enable a device's full default selection unchecked merely by adding it. The target picture is four separate, confirmed Modbus TCP gateways (`gw1` … `gw4`, each port 502, timeout 5, block size 20, gap 250, queue 8) with one inverter each `deye1` … `deye4` (Modbus address 1, expected serial numbers `2401230001` … `2401230004`, intervals 30/300/600/3600 s); insert real values.

With four devices observe for at least one hour including slow groups; the web interface shows the **total bus load** per selection. Afterwards agree on a longer observation across day/night change before shortening intervals or enabling further sensors. Do not form sums from SOC, shared battery SOC or possibly already aggregated parallel values. Equal values from several devices are not automatically independent measurements. Record the selection and measuring point of each sensor.

Set an optional **fallback connection** (in the inverter form, with its own Modbus address) only **after separately checking both paths of each inverter**: it points to the same inverter with identical inverter SN; the app switches only after several failed cycles and only if the same SN answers there. Do not provoke a failure from remote: no gateway/logger restart, no unplugging, no firewall interruption and no inverter shutdown. The sequentially checked source change suffices for this first test; it does not prove a successful automatic failover on the real device.

## 7. Check diagnostics and plausibility

In HA: app log, the web interface and under **Developer tools → States** timestamps/units of the entities. Send no actions or MQTT messages to command topics (there are none).

**Diagnostic entities** (`entity_category: diagnostic`). The app device `deye_<instance_id>_app` carries only what concerns the whole app:

| Entity | Meaning / check point |
| --- | --- |
| `sensor.deye_deyetest1_app_app_version` | Version of the app; compare with the approved state. |
| `sensor.deye_deyetest1_app_inverters_online` / `…_inverters_configured` | Number online / configured. |
| `sensor.deye_deyetest1_app_mqtt_reconnects` | Reconnections to the broker; growing = broker/network problem. |

Everything that belongs to one inverter sits on **that inverter's own device** (here `instance_id: deyetest1`, inverter `deye1`; the entity names carry no inverter prefix, the device does). They stay available while the inverter is offline:

| Entity | Meaning / check point |
| --- | --- |
| `sensor.deye_deyetest1_deye1_diag_state` | `starting`, `identifying`, `running`, `quarantined`, `offline`, `blocked` (web interface: Starting, Identifying, Online, Quarantined, Offline, Blocked). Expected: `running`. |
| `sensor.deye_deyetest1_deye1_diag_reason` | Reason in text form (e.g. "no usable identity answer", "serial number differs"); read first in every state except `running`. |
| `sensor.deye_deyetest1_deye1_diag_path` | Active path (`primary`/`fallback`) and endpoint. |
| `sensor.deye_deyetest1_deye1_diag_serial` | Bound inverter SN = `expected_serial`, unique per device. |
| `sensor.deye_deyetest1_deye1_diag_firmware_mcu1` | Firmware of the communication/HMI board (MCU1, R17-R18), like the Deye cloud: `1001-C070`. |
| `sensor.deye_deyetest1_deye1_diag_firmware_mcu2` | Firmware of the control board (MCU2, R14-R16), like the Deye cloud: `2005-1175-1807`. |
| `sensor.deye_deyetest1_deye1_diag_r65_raw` / `…_r65_status` | Energy resolution raw word and whether the last reading worked. Only present while R65 is read and reported (not with a user-chosen resolution, not on a device that reports `0xFFFF`). |
| `sensor.deye_deyetest1_deye1_diag_energy_resolution` / `…_energy_source` / `…_energy_status` | Effective resolution (kWh/step), source (`R65` or `user choice`), confirmation/lock. |
| `sensor.deye_deyetest1_deye1_diag_energy_series` | Current energy series (`g0`, `g1`, … or `<token>g0`). |
| `sensor.deye_deyetest1_deye1_diag_capability_conflicts` | Number of conflicts with the hardware profile; the list is in the attribute `conflicts`. |
| `sensor.deye_deyetest1_deye1_diag_bus_utilisation` | Utilisation of the app bus (estimate, 1.0 = full). |
| `sensor.deye_deyetest1_deye1_diag_transport_timeouts` / `…_transport_frame_errors` / `…_transport_resyncs` | Counters; an increase is an abort criterion (section 8). |
| `sensor.deye_deyetest1_deye1_diag_transport_cooldown` | Wait time after errors (backoff). |
| `sensor.deye_deyetest1_deye1_diag_last_success` | Time of the last successful query; should be at most a few intervals old. |

**Inverter entities** (selected sensors only): `sensor.deye_deyetest1_deye1_<key>`, e.g. `…_serial_number`, `…_operating_state`, `…_fault_word_1` … `…_fault_word_4`, later `…_battery_voltage`, `…_battery_soc`, `…_inverter_frequency`, `…_pv1_power`, `…_pv2_power`. Energy counters carry the series in the name, e.g. `…_battery_energy_charge_total_g0`.

| Check | Expectation / procedure |
| --- | --- |
| Identity | `expected_serial` = bound inverter SN (`…_serial`). Logger SN separate. Active endpoint (`…_path`), last successful cycle (`…_last_success`) and state visible; no `quarantined`/`blocked`. |
| R22 / R24 | R22: high byte MPPT count, low byte phase count; the profile expects 2/3, i.e. `0x0203`. This is an expectation, not a value already measured on your devices. R24: 0 or 1 means one battery input according to the document, 2 means two. No pack/BMS counter. Document `…_capability_conflicts` and do not release the affected measurements. |
| R65 / energy | If energy is selected: record raw value, effective factor and source (`auto`/override). `R65 <= 0x0104`: 0.1 kWh/step; otherwise 1 kWh/step, only for the counters curated for that. Missing is not 0. R65 = `0xFFFF` or `0` is no version: the app derives nothing and publishes no energy until the resolution is chosen (see "Choosing the energy resolution" below). No override merely to "make it fit". |
| Energy series | With unchanged SN and resolution `…_energy_series` stays stable across restart and transport change. A confirmed change of resolution, a different device or a deliberately confirmed loss of history begins a new series (old entity is dropped, its statistics stay in HA, unconnected to the new one). On loss of the energy history the energy is locked (`energy_history_missing`/`…_inconsistent`/`energy_store_corrupt`) and on the explicit reset a random 8-character token begins. Do not delete `/data` yourself. |
| Load / errors | Number of read blocks per group and total load in the web interface, `…_bus_utilisation`, timeouts, reconnects, cooldown. The plan budget is an estimate; foreign pollers/cloud/cache are not included. "Overloaded", a growing queue or repeated recovery are no reason for faster retries. |
| Quality in HA | Missing/invalid data appears as `unknown` (single value) or `unavailable` (group falls silent after three intervals or app/inverter offline), never as invented zero values. After a reconnect no old instantaneous values as a fresh measurement. `Online` alone does not prove the freshness of every group. |
| SOC R588 | u16, 1 %, plausible 0–100. With a shared battery the four values may be equal; do not add. |
| Battery voltage R587 | u16 × 0.01 V; e.g. raw value 5200 → 52.00 V. Compare with the existing BMS display and its actual limits. This is no new charge voltage recommendation. |
| Inverter frequency R638 | u16 × 0.01 Hz; in the running 50 Hz island grid approximately 50 Hz. 0.50 or 5000 Hz points to decoding/assignment. Mind state and measuring point; derive no protection limits from it. (The similar "load frequency" R655 is only a duplicate of the same source and not part of this selection.) |
| PV1/PV2 R672/R673 | Each u16, 1 W. Plausibility-check in daytime against an existing display; mind shading/curtailment. 0 at night is expected and no proof of missing support. Three string inputs mean two MPPT measuring channels (2+1 strings) on your 10 kW model, not PV3. PV3–PV10 stay locked. |
| Multi-word values | Both words from the same read cycle and path; 32-bit pairing/word order per profile. This does not guarantee an exactly simultaneous internal DSP snapshot. |

### Choosing the energy resolution

If the device reports R65 = `0xFFFF` (or `0`), the overview and the sensor page show "The inverter reports no valid energy resolution (R65)", the energy values are `unknown`, the diagnostics show source `unknown` and status `r65_invalid`.

1. Record the raw value (the `unreported` entry `dsp_protocol_version` in the inverter diagnostics; there is no `…_r65_raw` entity while R65 is not reported) and the daily energy (PV today) on the inverter display.
2. In the note **Choose 0.1 kWh** (usual for SG04LP3) or **Choose 1 kWh**, or set it under **Set up plant → Inverter → Energy resolution**. This is the only writing change of the app configuration; nothing is written to the device.
3. The choice begins a **new energy series** (`…_energy_series` changes); the old series is never continued with a different factor.
4. Check: daily energy in HA against the display (e.g. 7.0 instead of 70 kWh). If it does not match, choose the other resolution; that again begins a new series.

## 8. Abort and return to the baseline

**Stop the app immediately** as soon as new inverter/parallel/BMS faults, island grid interruption or flicker occur. Likewise on a wrong identity, unknown serial binding, critical persistence error (`blocked`) or a new logger disconnect. No restart experiments.

For pure communication errors cautious **test abort rules** additionally apply, not Deye limits: three consecutive timeouts/CRC/V5 sequence errors (increase in `…_transport_timeouts`/`…_transport_frame_errors`), recurring cooldowns/reconnects or a growing queue → stop. New cloud data gaps of more than two previously usual upload periods → stop and compare with the baseline; with a simultaneous plant fault do not wait for this period.

1. In the prepared HA tab **Settings → Apps → Deye SG04LP3 Monitor → Info → Stop**. Stopping the monitor app is **no OFF command to the inverter**. Use the Supervisor app page even if the Ingress page is unreachable. Do not restart HA/Supervisor or the MQTT broker as a substitute. On an orderly stop the app reports all inverters and itself offline (on a hard end via the MQTT "Last Will").
2. Confirm the stopped state (all entities `unavailable`). If the status stays unclear, do not start any further test instance. Observe independent monitoring and cloud over several normal cycles.
3. Save logs, time, active path and last configuration. As a first reaction do not delete identity/energy/discovery files, rebind devices or acknowledge error messages. In particular do not bypass `blocked`/`quarantined` by a reset.
4. Only if you deliberately paused an independent local poller beforehand: restore its original state. A gateway with autonomous polling/cache may keep polling despite the stopped app; stopping the app guarantees no electrically silent bus. With persistent problems end the test and use the existing support.

### Where the app's data lives

Everything lives in the app's data directory `/data` (preserved across restart and update, included in the app's HA backup; visible only via SSH access to the host system, **not meant for editing**):

| File | Content |
| --- | --- |
| `instance_id` | Instance identifier (part of all entity IDs; fix it with the option `instance_id`) |
| `plant.a.json`, `plant.b.json` | Connections and inverters as set up in the web interface (two slots; the newest complete one applies) |
| `selection.a.json`, `selection.b.json` | saved sensor selection and plant features (two slots; the newest complete one applies) |
| `identity.json` | Bindings inverter `id` ↔ serial number |
| `energy.json`, `energy-generations.json` | Energy resolution/series per inverter and their history |
| `discovery.json` | List of the published HA entities (for cleanup) |
| `capabilities.json` | Cache of the hardware check (may be rebuilt) |

Deleting one of these files is an intervention with consequences (new binding, new energy series, orphaned HA entities). If you need to return to the baseline, it suffices to stop the app and uninstall/keep it; Home Assistant keeps the statistics of entities already created, removed entities you must delete by hand in HA if the app is unreachable.

### Maintenance area of the web interface (reset)

Only after clarifying the cause and only deliberately; each action explains its effect and requires typing a confirmation text:

- **Rebind identity** (per inverter): forgets the saved serial number; the inverter is recognised and bound anew. Locked as long as an expected serial number is entered on the inverter (then change it under **Set up plant**).
- **Reset → Serial number binding** (per inverter): as above for this one inverter; the others keep running.
- **Reset → Energy counter history** (per inverter): begins a **new counter series** (new energy entities with a new series; the old statistics stay in HA, unconnected).
- **Reset all bindings** (**global**): deletes the bindings of **all** inverters, each is recognised anew (intended for a damaged `identity.json`).
- **Empty cleanup list**: afterwards the app no longer cleans up removed entities automatically. Do not use during the test.

A reset is **not** a means to bypass `quarantined`/`blocked`.

## 9. Hard limit of this test

The app sends **only FC03 read accesses** to inverters, in the framing matching the transport; a send guard at the lowest path enforces this independently of any option. `allow_writes: false` stays set for every device (the option has no effect in this version anyway). No FC06/FC16, no write probe, no command/control entities (the app publishes only `sensor`/`binary_sensor`, subscribes only to the HA birth message), no gateway or logger admin action by the app.

Also excluded "just briefly": R80 OFF/ON, "No Battery" R111, parallel roles/count R336/R337, ToU, remote mode or changing setpoints; simulating master/slave failure; disconnecting AC/battery/CAN; trying socket chains between ModeBUS, Meter-485 and BMS. All configuration of gateway and logger (protocol mode, IP, polling, upload, restart) is changed **only by you**; the app changes nothing there. A separately justified change to your plant is **not part of this read test**.

## 10. Result sheet and evidence

Per stage note: date/time, approved commit/image digest, HA/Supervisor version, real endpoints/unit IDs/SNs, selection revision and read plan (number of sensors/blocks per the web interface), other active pollers, duration, number of successful/failed cycles, maximum response time/queue, R22/R24, where applicable R65/factor/energy series, cloud gaps, new faults and the time of the app stop. Mark unknown values as **not checked**. Do not include credentials in the result sheet.

**Passed** means only: this specific version/read set worked over the observed time on the checked paths without new anomalies. It proves neither writability nor uninterrupted slave/master switchover, maximum poll rate or the equivalence of all CON sockets.

Foundations in the repository: `docs/SPEC.md`, `deye_monitor/DOCS.md` (options, web interface, state files), `deye_monitor/config.yaml` (options: MQTT, log level), `e2e/` (end-to-end acceptance with a real Home Assistant and fake inverters; no real device).
