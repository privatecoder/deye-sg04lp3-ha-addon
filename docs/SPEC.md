# Deye SG04LP3 Monitor — Spezifikation v1

Home-Assistant-App (früher „Add-on“), die 1..n Deye SUN-5/6/8/10/12K-SG04LP3 (LV, dreiphasig) abfragt, die Werte per MQTT Device Discovery in Home Assistant bereitstellt und – später, ausdrücklich freigeschaltet – ausgewählte Einstellungen steuern kann. **Die Testphase ist rein lesend** (Nutzer nicht vor Ort); die Schreibschicht wird erst nach Abnahme des Lese-Teils implementiert (§10), die Architektur sieht sie aber von Anfang an vor.

Wissensgrundlage: `KB = <lokale Wissensbasis>`, insbesondere
`KB/.orch/reviewer-codex-6.1-addon-grundlagen.md` (im Folgenden **GRUNDLAGEN**, mit Abschnittsnummern zitiert),
`KB/Modbus/V105.8/registers.json` (Registerkatalog) und `KB/Firmware/Analyse/*.md`.

## 1. Harte Grenzen (nicht verhandelbar)

1. **Lesen = nur FC03.** Ohne Schreibfreigabe ist jeder Byte-Strom an ein Gerät (Inverter, Logger, Gateway) ein FC03-Leseframe (Modbus TCP MBAP, RTU-ADU oder Solarman-V5-Request mit innerem FC03-RTU-Frame). Keine Masken-, Broadcast-, Zeit-, Heartbeat-Antwort-, Logger-HTTP- oder Restart-Pfade – auch nicht mit Schreibfreigabe. Kein generisches `execute(function_code)`.
2. **Guard am untersten Sendepfad:** eine einzige Funktion, durch die jede Gerätesendung läuft, dekodiert den Frame. Erlaubt: FC03 (Unit 1..247, Anzahl 1..125). FC16 (0x10) **nur** wenn die Schreibfreigabe des Ziel-Inverters aktiv ist **und** jede Zieladresse in der Schreib-Whitelist des Profils steht. Alles andere → Exception. In der Testphase (bis §10 umgesetzt ist) existiert der FC16-Pfad nicht. Tests prüfen Wire-Bytes, nicht Methodennamen.
3. **Keine Fremdbibliothek für Geräteprotokolle** (kein pysolarmanv5 — sendet automatisch Zeitantworten, GRUNDLAGEN §2.2; kein pymodbus/ESPHome-Code). Framing wird selbst implementiert (klein, getestet).
4. **Ohne Schreibfreigabe keine MQTT-Command-Topics**, keine schreibenden Entity-Plattformen und keine Abonnements auf Command-Topics.
5. Keine automatischen Netzwerkscans. Nur konfigurierte Ziele.

## 2. Architektur

Python 3.14, asyncio, ein Prozess. Paket `deye_monitor` (src-Layout), vollständig typisiert.

| Modul | Verantwortung |
| --- | --- |
| `config` | Lädt `/data/options.json` (bzw. Pfad aus Env), pydantic-v2-Modelle, fachliche Validierung (§4). Keine I/O zu Geräten. |
| `protocol/modbus` | FC03-Request-Bau, RTU-CRC16, RTU-Antwortparser, MBAP-Bau/-Parser, Exception-Antworten (0x83). |
| `protocol/solarman_v5` | V5-Request-Bau (Header 0xA5, Control 0x4510, Sequenz, Logger-SN LE, Checksumme, 0x15) und Stream-Parser (fragmentierte/zusammengefasste Frames, Heartbeat/Fremdframes verwerfen, Sequenz-/SN-Prüfung, Double-CRC-Quirk mit Re-Validierung). **Nie antworten** auf eingehende Management-/Zeitframes. GRUNDLAGEN §2. |
| `protocol/guard` | Read-only-Guard (§1.2). |
| `transport` | `modbus_tcp`, `rtu_over_tcp`, `serial_rtu` (pyserial-asyncio-fast), `solarman_v5_tcp`. Persistente Verbindung, Deadline je Request (Default 5 s), Reconnect mit exponentiellem Backoff + Jitter (2..60 s), byte-stream-sicheres Lesen. |
| `scheduler` | Pro `bus_id` FIFO + Lock, max. 1 Request in-flight je Endpoint, kurze Pause zwischen Requests (Default 50 ms), begrenzte Queue. |
| `profiles` | Kuratierte, versionierte Registerprofile als Datendatei (YAML) im Paket + Loader + Decoder. Default-Profil `sg04lp3_lv_v105_8`. |
| `device` | Pro Inverter: Identität lesen (R0–R28, R65, R336), erwartete Seriennummer prüfen, Gruppen nach Intervall pollen, Snapshot mit Zyklus-ID/Zeitstempel, Frische/Verfügbarkeit, Primary/Fallback-Endpoint. |
| `mqtt` | aiomqtt. Device Discovery (`homeassistant/device/<id>/config`, retained), `origin`, Availability je Gerät + LWT der App, HA-Birth (`homeassistant/status`) → Discovery erneut senden, State-Topics nicht retained, gezielte Bereinigung entfernter Komponenten. |
| `diagnostics` | Strukturierte Logs, Zähler (Timeouts, CRC-Fehler, Quirks, Reconnects) als Diagnose-Sensoren, Secrets-Redaktion. |
| `app` | Einstiegspunkt, Lebenszyklus, Signalbehandlung, geordneter Shutdown. |

## 3. Transporte und Endpoints

Transport-Enum fest: `modbus_tcp | rtu_over_tcp | serial_rtu | solarman_v5_tcp` (GRUNDLAGEN §3.1).

- `solarman_v5_tcp`: `host`, `port` (Default 8899), `logger_serial` (uint32 ≠ 0). Unit-ID ist die des Inverters.
- `modbus_tcp` / `rtu_over_tcp`: `host`, `port` (Default 502). Kein Auto-Detect des Framings.
- `serial_rtu`: `serial_device` (`/dev/serial/by-id/...`), `baudrate` (Default 9600), 8N1 fest.
- Gemeinsam: `bus_id` (Default = kanonischer Endpunkt, siehe §8.1), `timeout_s`, `max_read_registers` (Default 60, Max 100, harte Obergrenze 125), `request_gap_ms`.

Jeder Inverter hat genau einen `endpoint` (primär) und optional `fallback_endpoint`. Fallback nur nach N aufeinanderfolgenden Fehlschlägen (Default 3), nur wenn dort dieselbe Inverter-Seriennummer (R3–R7) gelesen wird; Rückkehr zum Primärweg periodisch (Default 300 s). Nie beide gleichzeitig dauerhaft abfragen. Entity-IDs hängen nur an der Inverter-ID, nicht am Endpoint.

## 4. Konfiguration (`config.yaml` options/schema)

```yaml
endpoints:
  - id: gw1
    transport: modbus_tcp        # modbus_tcp|rtu_over_tcp|serial_rtu|solarman_v5_tcp
    host: 192.0.2.54
    port: 502
    # logger_serial: 1234567890   # nur solarman_v5_tcp
    # serial_device: /dev/serial/by-id/...  # nur serial_rtu
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
mqtt:                             # leer → Supervisor-MQTT-Dienst (mqtt:want)
  # host, port, username, password, tls
  discovery_prefix: homeassistant
  topic_prefix: deye_monitor
log_level: info
```

Fachliche Validierung: eindeutige IDs; Endpoint-Referenzen existieren; `unit_id` 1..247; Ports 1..65535; `logger_serial` nur und zwingend bei V5; `serial_device` nur und zwingend bei `serial_rtu`; auf demselben `bus_id` eindeutige `unit_id`; Fallback ≠ Primär.

### 4.1 Nachtrag T14: Anlage in der Weboberfläche

Verbindungen (`endpoints`) und Inverter (`inverters`) werden **ausschließlich in der Ingress-Weboberfläche** angelegt und bearbeitet (Bereich „Anlage einrichten“, Assistent beim ersten Start). Das HA-Optionsformular kann keine Auswahllisten bereits angelegter Verbindungen; die Optionen behalten nur `mqtt` (inkl. TLS), `log_level`, `instance_id`. Die Modelle und Validierungen aus §4 gelten unverändert (`EndpointConfig`, `InverterConfig`, Bus-Eindeutigkeit, Fallback, Grenzen); eine **leere Anlage ist gültig** (die Ein-Inverter-Pflicht aus §4 entfällt, der Assistent führt in die Einrichtung), `AppConfig` = Optionen + Anlage.

* **Store.** `/data/plant.a.json` / `plant.b.json`: wie die Auswahl (§9.6) zwei vollständige Slots mit `seq` und SHA-256, atomar, neuester gültiger Slot gilt, beschädigter Slot wird ignoriert/gemeldet (Kopie `*.corrupt` beim Überschreiben); ist kein Slot gültig, ist die Anlage leer und es läuft nichts, bis der Nutzer neu einrichtet (fail closed, keine Migration aus den Optionen). Eigene Revision (`If-Match` jeder Anlagenänderung).
* **Migration.** Enthalten die Optionen `endpoints`/`inverters` und existiert **keine** Anlagendatei, werden sie einmalig übernommen (Log + Hinweis in der Oberfläche: bitte aus den Optionen entfernen). Danach ist der Store maßgeblich; abweichende Options-Einträge erzeugen nur einen Hinweis, nie eine stille Überschreibung. Eine vom Nutzer geleerte Anlage wird nicht erneut migriert. Das Manifest behält `endpoints`/`inverters` als optionale Listen (Default leer).
* **Oberfläche.** Verbindungen: Transport als erklärte Auswahl (`modbus_tcp` Port 502 = „Gateway (Modbus TCP → RTU)“; `rtu_over_tcp` = „Gateway transparent“; `solarman_v5_tcp` Port 8899 mit Logger-Seriennummer *des Sticks*; `serial_rtu` mit Dropdown aus `/dev/serial/by-id/*`, nur gelistet, Baudrate 9600), Felder je Transport, Erweitert-Bereich. Inverter: Verbindung und Ersatzverbindung als Dropdowns, Unit-ID (Default 1), erwartete SN, Energieauflösung, Intervalle; die ID ist nach dem Anlegen nicht änderbar. `allow_writes` ist in der Oberfläche und in der API nicht setzbar (Anzeige „Schreiben: nicht verfügbar“). Löschen einer Verbindung nur ohne nutzende Inverter; beim Löschen eines Inverters wird die Identitätsbindung nur nach ausdrücklicher Bestätigung entfernt.
* **Verbindungstest (nur lesend).** Führt über den normalen geguardeten Transport genau die Identitätsblöcke des Profils aus (nur FC03, Deadline), über den Bus-Scheduler der laufenden Verbindung (gleiches Ziel in der laufenden Anlage) bzw. einen temporären, danach geschlossenen Transport. Ergebnis: erreichbar, Seriennummer (gegen erwartete und gebundene), Modell-/MPPT-Plausibilität, Konflikte, bei Fehlern typische Hinweise je Transport. Keine Änderung der Anlage, ein Test gleichzeitig; dasselbe Ziel mit anderer Anbindungsart wird abgewiesen (`endpoint_in_use_by_other_transport`).
* **Live-Übernahme.** validieren → übernehmen (laufende Generation aus Inverter-Runtimes, Transports, Schedulern und MQTT-Publisher geordnet stoppen, neue aufbauen; Discovery-Bereinigung entfernter Inverter über den bestehenden Ledger; Auswahl-Records entfernter Inverter werden verworfen, neue Inverter beginnen mit `awaiting_selection`) → dauerhaft ablegen. Scheitert das Übernehmen oder Ablegen, wird die vorherige Generation wiederhergestellt (`adoption_failed` / `persist_failed`; nur wenn auch das misslingt `plant_recovery_failed`). Auswahländerungen laufen nie gleichzeitig (gemeinsame Sperre). Sicherheitsregeln wie §9.5: Supervisor-Peer, Benutzer, CSRF, `If-Match`, strikte Schemas, keine freien Register/Frames.

## 5. Registerprofil v1 (`sg04lp3_lv_v105_8`)

Quelle aller Definitionen: `KB/Modbus/V105.8/registers.json` + GRUNDLAGEN §1 + REVIEW2 (`KB/.orch/reviewer-codex-6.1-spec-review-2.md`, §4 Registerzuordnung, §5 PV-/Hardwarebelege). **Einziges verbindliches Eintragsschema** (§8.1/§12 haben Vorrang vor älteren Formulierungen):

| Feld | Pflicht | Bedeutung |
| --- | --- | --- |
| `key`, `name` (de/en) | ja | stabiler Schlüssel, Anzeigename |
| `addr` bzw. `low`/`high` | ja | dezimal, zero-based; 32 bit immer explizit `low` + `high` (nie `high=low+1` annehmen) |
| `type` | ja | `u16, s16, u32, s32, ascii, bitfield, enum, hhmm, raw` |
| `mask`/`shift`/`enum`/`components` | bei Bedarf | gepackte Felder |
| `scale`, `raw_offset`, `scale_source` | bei Zahlen | `value = (decoded_raw + raw_offset) * scale`; `scale_source: energy_resolution` für §5.1 |
| `unit`, `device_class`, `state_class`, `entity_category` | bei physischen Werten | HA-Metadaten |
| `category`, `tags` | ja | Anzeige/Suche (keine Logik) |
| `requires` | ja (ggf. `{}`) | Nutzungsprädikat über Anlagenmerkmale, `all_of`/`any_of` (§9) |
| `supported_on` + `support_status` | ja | Hardware-/Producerachse: `supported, placeholder, unsupported_for_profile, unverified` |
| `release` | ja | Dekodierachse: `confirmed, profile_dependent, unresolved` |
| `source`, `confidence` | ja | Katalog-Datensatz/TXT-Zeilen, Begründung |
| `producer_evidence` | wenn vorhanden | Build, Image-SHA-256, Store-Offset (REVIEW2 §5.3/§5.4) |
| `default_enabled` | ja | Teil der Vorlage, wenn `requires` erfüllt |
| `internal` | optional | für interne Pflichtreads (§9.3), erzeugt keine Entity |

Nur `support_status: supported` **und** `release ∈ {confirmed, profile_dependent}` ergeben physische Sensoren. Alles andere höchstens als ausdrücklich gekennzeichnete, standardmäßig deaktivierte Rohdiagnose (Rohwert, keine Einheit, keine Statistik); `placeholder` wird gar nicht angeboten.

Gruppen und Default-Intervalle (GRUNDLAGEN §1.2):

| Gruppe | Intervall | Inhalt |
| --- | --- | --- |
| `identity` | Start + nach Reconnect, dann 3600 s | R0, R1, R2, R3–R7 (ASCII-SN), R14–R18, R20/R21, R22 (MPPT high byte / Phasen low byte), R23, R24 (Batterieeingangs-Code), R25–R28, **R65**, R336 |
| `status` | 10 s | R500, R551–R558 (alle vier Fehlerwörter roh + bekannte Bits) |
| `battery` | 10 s | R586–R588, R590 (nur Rohdiagnose, Faktor ungeklärt), R591, R592. **R589 und R593–R596 sind Batterie 2** und auf SG04LP3 `placeholder` (1151/1175 zero-filled) |
| `ac` | 10 s | R598–R600, R603–R619, R621–R638, R640–R646, R650–R655, **R656–R659** (Load-High-Words zu R650–R653) + benötigte High-Words aus 687..708 laut Katalog; **R620/R709 nie als Paar**; R654/R660 (Scheinleistung) nicht freigegeben |
| `pv` | 10 s | MPPT1: R672 Leistung, R676 Spannung, R677 Strom; MPPT2: R673, R678, R679. R674/R675/R680–R683 (PV3/PV4), R530–R533, R718–R721 = `placeholder` (zero-filled in 1151/1175); PV5–PV10 und AI-Arc R722–R748 = `unverified`/`unresolved`, nicht angeboten |
| `settings` | 300 s | R80, R98, R111, R133, R146, R148–R177 (ToU) — nur Diagnose-Sensoren; R166–R171 nur Rohdiagnose |
| `energy` | 60 s | Nur die Whitelist aktiver kWh-Zähler (§5.1); Lesegruppe ≠ semantischer Typ |

Die zu lesenden Adressen werden **aus den Abhängigkeiten der aktivierten Sensoren abgeleitet** (inkl. nichtadjazenter Partner), nicht aus festen Bereichen. Der Profilvalidator verlangt für jeden u32/s32-Sensor genau ein Low- und ein High-Word; ausgeschlossene Sensoren schleusen keine Partner ein.

### 5.1 Energie-Whitelist (v1)

Aktive kWh-Zähler mit `scale_source: energy_resolution` (REVIEW2 DR2-01):

| Layout | Adressen und Bedeutung |
| --- | --- |
| je **ein** 16-Bit-Feld (Tageswerte) | R514 Batterie-Tagesladung; R515 Batterie-Tagesentladung; R520 Tagesnetzbezug; R521 Tageseinspeisung; R526 Tageslastenergie; R529 PV-Tagesenergie gesamt |
| Low/High, **eine** 32-Bit-Entity je Paar (Gesamtwerte) | R516/R517 Batterie-Ladung; R518/R519 Batterie-Entladung; R522/R523 Netzbezug; R524/R525 Einspeisung; R527/R528 Lastenergie; R534/R535 PV-Energie gesamt |

Keine Paarung 514/515 oder 520/521; R526 ist kein Teil von R527/R528. Low-/High-Rollen aus den Kataloggruppen (`total_516_517` …) übernehmen. Signedness und Release je Entity. GEN R536, R537/R538 sind eine gesonderte Katalogentscheidung (GEN-Port-Prädikat), nicht automatisch R65-skaliert. R501 wird separat entschieden (S16, nicht automatisch monoton). **Nicht** als R65-skalierte kWh: R502/R506/R507 (Blindenergie), R503 (Netzdauer s), R508/R509 (Write-CRC), R510/R511 (reserviert), R512/R513 und R539 (GEN-Laufzeit), R536–R538 (GEN-Energie, GEN ist v1-Katalog-Option, nicht Standard). `state_class` je tatsächlicher Größe, nicht je Gruppe.

### 5.2 Freigabeklassen

Jeder Katalogeintrag hat verpflichtend `release: confirmed | profile_dependent | unresolved`, `source` und `confidence`. `unresolved` erscheint höchstens als klar benannte Rohdiagnose (Rohwert, keine physische Einheit, keine Statistik). Masken, Shifts, Enums und Komponentenlayouts werden im Profil ausgedrückt. Offsetkonvention: `value = (decoded_raw + raw_offset) * scale`. Konfliktfälle ausdrücklich: R590 (Faktor 1 W vs. 10 W) und R166–R171 (ToU-SOC-Aufteilung) nur als Rohdiagnose, bis belegt.

Dekodierregeln (GRUNDLAGEN §1.4–1.6): 32 bit = `(high<<16)|low`, danach Vorzeichen, danach Skalierung. Temperatur R586 = `s16/10`, R217/R541 = `(raw-1000)/10`. R336: Parallel-Enable bit0, Master bit1, Phase bits8–9, SN `(raw>>10)&0x3f`. Felder mit `scale: null` im Katalog werden nicht still auf 1 gesetzt: entweder im Profil explizit entschieden (mit Begründung in `notes`) oder nicht veröffentlicht. Bekannte Platzhalter (R647–R649, Apparent-Power-Aliase) werden nicht veröffentlicht.

**Energieauflösung:** Laut V105.8 R65 („DSP protocol version“): `R65 <= 0x0104` → 0,1 kWh, sonst 1 kWh. Gilt nur für die Whitelist §5.1. Der gelesene R65-Wert, die gewählte Auflösung und die Generation werden als App-/Inverter-Diagnose veröffentlicht (R65 ist kein Default-Sensor mehr, nur Energieabhängigkeit); Override pro Inverter über `energy_resolution: auto|0.1|1` (Default `auto`, im Supervisor-Schema). In `auto` werden Energiewerte erst nach gültigem R65 des **identifizierten** Geräts freigegeben; kein Default-Faktor bei fehlender Antwort, Timeout ≠ R65=0.

**Statistikgeneration (DR2-02):** Pro Inverter wird `{effective_resolution, source: r65|override, generation}` in `/data/energy.json` persistiert. Ein normaler Reconnect mit unverändertem Faktor ändert nichts. Ändert sich der **effektive** Faktor (durch R65 oder durch Override-Wechsel, unabhängig voneinander erkannt), wird `generation` erhöht: Energie-Entitäten tragen **von Anfang an** das Suffix `_g<generation>` (ab `_g0`) in Komponenten-Key, `unique_id` **und** `default_entity_id` (HA-Recorder nutzt die entity_id als statistic_id); der State-Payload enthält `energy_generation`, und jedes Energie-Template liefert nur bei passender Generation einen Wert (sonst `none`). Bei Wechsel erhalten die Energie-Entitäten neue IDs (neue HA-Statistikreihe), die alten Komponenten werden aus der Discovery entfernt (Historie bleibt in HA erhalten), Diagnosemeldung + Hinweis in der Statusansicht. Keine Neuinterpretation alter Punkte, keine künstlichen Deltas, kein Statistikpunkt unter alter Generation nach dem Wechsel. Ein Neustart mitten im Wechsel setzt ihn idempotent fort. **Gerätewechsel:** Wird nach ausdrücklicher Neubindung eine andere Inverter-SN gebunden, beginnt ebenfalls eine neue Generation (auch bei gleichem Faktor); Reconnect, Wegwechsel und Neuprüfung derselben SN ändern nichts; gleichzeitiger SN- und Faktorwechsel migriert einmal. Die persistierte Energie-Epoche trägt die SN. **R65-Bestätigung:** zwei *aufeinanderfolgende* gültige gleiche Lesungen; eine fehlende/ungültige Lesung setzt die Bestätigung zurück; die Bestätigungslesung erfolgt zeitnah (z. B. nach 60 s), nicht erst im Identitätsintervall. **Persistenz fail-closed:** fehlt `identity.json`/`energy.json`, ist das Erstinstallation; ist eine vorhandene Datei beschädigt, unlesbar oder hat ein unbekanntes Schema, werden Bindung bzw. Energiepublikation gesperrt (Diagnose), bis der Nutzer ausdrücklich zurücksetzt; nie stillschweigend neu binden oder `_g0` wiederverwenden. Der Capability-Cache darf nach Diagnose neu aufgebaut werden. `energy.json` und `energy-generations.json` werden gemeinsam bewertet; nur „beide fehlen“ ist Erstinstallation; beschädigte oder widersprüchliche Historie sperrt Energiewerte (`energy_store_corrupt`/`energy_history_inconsistent`). Nächste Generation = `max(High-Water, Record)+1`; High-Water und reservierte Transition werden vor dem Record geschrieben. Kein zeitbasierter Ersatz: nach Historienverlust startet der Reset einen zufälligen, persistierten 8-Zeichen-Namespace bei `g0`. **Energie-Serien-Suffix:** opakes `energy_series` (`g<n>` bzw. `<token>g<n>`, Regex `^([a-z0-9]{8})?g[0-9]+$`) für Komponenten-Key, `unique_id`, `default_entity_id` und Template-Guard; eine veröffentlichte Serie wird nie wieder Messquelle. **Identity-Reset:** pro Inverter isoliert; bei insgesamt beschädigter Datei global (alle Inverter offline, Epoche +1, Neuidentifizierung). Energiezähler: `device_class: energy`, `state_class: total_increasing`; ungültige/fehlende Werte → `unknown` (nie 0 erfinden).

## 6. MQTT / Home Assistant

**Normativer Umsetzungsvertrag:** `KB/.orch/reviewer-codex-6.1-t8-contract.md` (HA Core 2026.10.4, aiomqtt 2.5.1, MQTT 3.1.1) — bei Widersprüchen hat er Vorrang vor diesem Abschnitt.


- Ein HA-Gerät pro Inverter: `identifiers: [deye_<instance_id>_<inverter_id>]`, `serial_number` aus R3–R7, `sw_version` aus R15/R17–R18 dekodiert, `model` „SUN-…-SG04LP3“ (Nennleistung aus R20/R21), `via_device` = App-Gerät.
- `unique_id` und Topics mit `instance_id` (§8.1); `default_entity_id`/`object_id` kompatibel zu HA aktuell prüfen.
- Availability: App-LWT `<topic_prefix>/<instance_id>/status` + je Inverter `<topic_prefix>/<instance_id>/<id>/availability`, `availability_mode: all` (§8.1). `expire_after` je Gruppe (3× Intervall).
- State: ein JSON-Payload je Inverter und Gruppe (`<topic_prefix>/<instance_id>/<inverter_id>/<group>`), Sensoren via `value_template` ohne Default.
- Keine Bank-Summen in v1 (keine SOC-Addition o. Ä.).

## 7. Packaging

Repo-Root = HA-App-Repository: `repository.yaml`, App-Verzeichnis `deye_monitor/` mit `config.yaml`, `Dockerfile`, `rootfs/` (s6-overlay v3, `init: false`, Dienst ruft `exec python -m deye_monitor`), `translations/en.yaml` + `de.yaml`, `README.md`, `DOCS.md`, `CHANGELOG.md`, `icon.png`/`logo.png` (Platzhalter ok), `LICENSE` (MIT). Archs: `aarch64`, `amd64`. `stage: experimental`. `services: [mqtt:want]`, `uart: true` statisch (Manifestrecht, keine Laufzeitbedingung; geöffnet werden ausschließlich konfigurierte `serial_device`-Pfade, keine Gerätesuche), kein `host_network`, kein `full_access`/`privileged`. Ingress für die Sensorauswahl (§9).

**Build-Layout (verbindlich):** Alle Build-Inputs liegen im App-Ordner `deye_monitor/` (Supervisor-Buildkontext): `pyproject.toml`, `uv.lock`, `src/deye_monitor/`, Profil-YAMLs als **Paketdaten** unter `src/deye_monitor/profiles/`, Frontend-Assets als Paketdaten. Kein `COPY ..`, keine Laufzeitabhängigkeit zur Knowledge Base. Root-CI arbeitet mit `working-directory: deye_monitor`. Abnahme: frischer Build nur aus dem App-Kontext, danach im Image Import + Profilvalidierung + Start mit Fake-Konfiguration. Versionen von Base-Image und Python-Dependencies gepinnt; vor dem Pinnen aktuellen Stand unter https://contributors.home-assistant.io/apps/ prüfen.

## 8. Qualität

`uv` für Abhängigkeiten, `ruff` (lint + format), `mypy --strict`, `pytest` + `pytest-asyncio`. GitHub-Actions-Workflow: lint, typecheck, tests, Docker-Build amd64/aarch64, Manifest-Validierung. Testanforderungen siehe GRUNDLAGEN §7.2 — insbesondere Wire-Level-Guard-Tests, Stream-Fragmentierung an jeder Byteposition, Golden-Decoder-Werte, Fake-Server (Modbus TCP, RTU-over-TCP, V5) für Integrationstests, Discovery ohne `command_topic`.

## 8.1 Normative Verträge aus Spec-Review (SR-03 … SR-13)

**Guard-Vertrag (SR-03):** Ein Guard-Aufruf prüft **genau einen vollständigen** Frame; alle gesendeten Bytes gehören dazu (keine führenden/nachfolgenden Bytes, keine zwei ADUs). FC03-PDU exakt 5 Bytes; RTU-ADU exakt 8 Bytes mit korrektem CRC; MBAP: Protocol-ID 0, korrektes Length-Feld, exaktes PDU-Ende; V5: korrekte Länge/Checksumme, Request-Control 0x4510, festes Metadatenlayout, innerer RTU-CRC, Logger-SN = konfigurierte SN. `0 ≤ start`, `1 ≤ count ≤ min(125, max_read_registers)`, `start+count ≤ 65536`; Unit = konfigurierte Ziel-Unit. Gesendet werden genau die geprüften unveränderlichen Bytes (`bytes`, kein nachträglich veränderter Puffer); Replay/Reconnect und UART-Schreibstelle laufen durch denselben Guard. Negativtests: gültiger Read + FC06/0x10-Suffix, Padding, zwei FC03-Frames, gekürzte Frames, gefälschte MBAP/V5-Längen, falscher CRC/Checksumme, fremde Logger-SN, Adressüberlauf; plus positive Randfälle.

**Snapshots und Frische (SR-04):** Ein Sensorwert wird nur aus Rohwörtern **desselben Zyklus** gebildet (Zyklus-ID). Fehlt ein Wort/Partner, ist **diese** Entity `unknown` (JSON `null` im State-Payload, Template ohne `default(0)`), keine Übernahme alter Werte, kein Statistikpunkt; andere Werte der Gruppe bleiben gültig. Gruppen-Payload enthält Zyklus-ID und Zeitstempel.

**Identitätsbindung (SR-05, SR-09):** Vor Betriebsdaten wird nur Identität gelesen und normalisiert (10 druckbare ASCII-Zeichen, sonst ungültig). `expected_serial` hat Vorrang; sonst wird die erste gültige SN an die logische Inverter-ID gebunden und in `/data/identity.json` persistiert. Jeder Reconnect, Fallback und Failback prüft gegen die Bindung; Abweichung → Inverter `quarantined` (offline + Diagnose), keine Betriebsdaten. Neubindung nur per Nutzeraktion (Ingress-Button „Identität neu binden“ oder Optionsänderung). Caches/Snapshots werden bei Transportwechsel verworfen; nie Wörter aus Primär- und Fallback-Weg mischen. Unit-ID darf je Verbindung überschrieben werden (`unit_id` am Inverter, optional `fallback_unit_id`). Kanonisch gleiche Endpunkte (gleicher Host:Port bzw. gleicher aufgelöster Serial-Pfad) werden auf **denselben** Bus-Lock gelegt bzw. als Duplikat abgelehnt; derselbe Inverter über zwei Wege ist kein Doppeladressfehler, zwei Geräte mit gleicher Unit auf einem Bus schon. Der Bus-Lock umfasst Identität, Probes und Daten.

**Availability (SR-06):** Jede Komponente hat `availability: [App-LWT, Inverter-Availability]` mit `availability_mode: all`. App-LWT retained `offline`, beim Start retained `online`. Inverter-Availability retained, beim App-Start zuerst `offline`, `online` erst nach Identitäts- und Datenfreigabe. Nach HA-Birth: Discovery, dann frische States.

**Instanz-ID (SR-10):** Persistente `instance_id` (Default: beim ersten Start generiert, 8 Zeichen, in `/data`, als Option überschreibbar). Discovery-Topics, `identifiers`, `unique_id` und State-Topics enthalten sie: `unique_id = deye_<instance_id>_<inverter_id>_<key>`. Das App-Gerät wird selbst als Gerät registriert (Diagnose-Sensoren), sonst kein `via_device`. Der verwaltete Discovery-Bestand wird in `/data/discovery.json` gespeichert, damit Cleanup auch nach Neustart nur eigene Topics löscht.

**Grenzen (SR-11):** Zeitwerte endlich und > 0, `request_gap_ms ≥ 0`, Poll-Intervalle ≥ 1 s, Queue-Längen begrenzt (Default 32 je Bus), unbekannte Optionen → Fehler. Request-Deadline gilt für den **gesamten** Request (nicht pro Byte). Puffer/Frames an Transport und angeforderte Länge gebunden (V5 max. 512 Bytes, RTU max. 5+2·125 Bytes). Fehlerhafte Quellen → Backoff. Alles als Anwendungsgrenzen dokumentiert, nicht als Deye-Limits.

**RTU-Resynchronisierung (SR-12):** Nach Timeout oder Fehler werden Eingangsdaten verworfen (Empfangspuffer leeren, Stillezeit ≥ 3,5 Zeichen bzw. 50 ms abwarten, bei TCP ggf. Verbindung neu aufbauen), bevor die nächste Anfrage gesendet wird; keine fiktiven Transaction-IDs bei RTU. `serial_rtu` wird über ein PTY-Paar getestet (Teilreads, CRC, Timeout, EOF/Hotunplug, Reopen, Guard am UART-Sendepfad).

**MQTT-Abnahme (SR-13):** Unit-Tests dürfen mocken; T12 nutzt verbindlich einen lokalen Mosquitto-Testbroker (Docker oder Binary, nie einen Anlagenbroker) und prüft Retain, LWT bei hartem Ende, Brokerneustart, HA-Birth, Cleanup nach Neustart. Discovery-Payloads werden zusätzlich gegen das HA-MQTT-Schema geprüft (gepinnte `homeassistant`-Version im Test oder vergleichbare Schemaprüfung).

## 9. Hardwareprofil, Anlagenmerkmale und Sensorauswahl (Ingress)

Ziel: maximal nutzerfreundlich; gepollt wird nur, was gebraucht wird. Vier getrennte Ebenen (REVIEW2 DR2-04/05): **Hardwarefähigkeit** (Profil) → **Nutzung** (Anlagenmerkmale) → **Vorlage** (`default_enabled` + `requires`) → **Nutzerauswahl** (Abweichungen). Vorlagen ändern nur die Defaultauswahl, nie Zugriffsrechte.

### 9.1 Hardwareprofil `sg04lp3_eu`

- Zwei MPPT (Handbuch §3.6.1): 5/6/8 kW 1+1 Strings, 10/12 kW 2+1 Strings; Kanal = MPPT, zwei Strings an einem MPPT bleiben ein Kanal. Ein Batterieeingang. GEN-Port vorhanden.
- Modell (Nennleistung 5/6/8/10/12 kW) wählt der Nutzer oder es wird aus R20/R21 plausibilisiert; R22 (MPPT/Phasen), R24 (Batterieeingangs-Code), R0/R14 werden gelesen und gegen das Profil geprüft. Widerspruch → `capability_conflict` in Statusansicht, **nie** automatische Freischaltung zusätzlicher Kanäle. R28 wird nicht als „4 = SG04“ interpretiert.
- Capability-Cache je `(SN, R15/R17/R18-Version, Profilrevision)`; Firmware-/Identitätswechsel invalidiert ihn.
- `placeholder`/`unsupported_for_profile` werden serverseitig durchgesetzt: auch eine manipulierte API-Anfrage kann sie nicht als physischen Sensor aktivieren.

### 9.2 Anlagenmerkmale je Inverter

| Merkmal | Werte |
| --- | --- |
| `utility_grid` | ja/nein (Netzbetrieb vs. Insel) |
| `grid_port_use` | `utility`, `generator` (R189), `unused` |
| `battery` | ja/nein; `bms` ja/nein (Unterbedingung) |
| `gen_port_mode` | `unused`, `generator`, `smart_load`, `ac_coupled` (Abgleich mit R133) |
| `mppt1_used`, `mppt2_used` | ja/nein |
| `parallel` | ja/nein |

`requires` verwendet Prädikate (`all_of`/`any_of`) über diese Merkmale, z. B. GEN-Leistung: `gen_port_mode in [generator, smart_load, ac_coupled]`. Register-Zuordnung je Messort laut REVIEW2 §4 (grid_terminal, battery, bms, gen_port, inverter_output, ups_load, pv, parallel, tou, diagnostic). Diagnose (Identität, R65, R80, R98/R111, R133, R336, R500, R551–R558) ist **unabhängig** von Merkmalen immer verfügbar. Vorschlag der Merkmale aus gelesenen Einstellungen (R111, R133, R189, R336) beim ersten Verbinden, Nutzer bestätigt.

### 9.3 Leseplan

`read_set = mandatory_identity_health + selected_sensor_dependencies (+ später active_operation_dependencies)`. Pflicht-Basisreads (Identität inkl. SN-Bindung, R336-Rolle, R500/R551–R558, R65 nur wenn eine Energie-Entity aktiv und `energy_resolution: auto`) laufen auch bei leerer Auswahl, erzeugen aber nicht automatisch Entitäten. Mehrwortsensoren ziehen exakt ihre Partner nach. Blöcke werden nur über bekannte, lesbare Adressen gebildet. **Planbudget:** Blockanzahl × (Antwortzeit + Gap) wird gegen das Intervall geprüft; Überlast wird in der UI angezeigt und fair koalesziert (keine endlose Nachholschleife).

**Erststart (`awaiting_selection`):** Solange für einen Inverter noch nie eine Auswahl gespeichert wurde (neue Installation oder neu hinzugefügter Inverter), ist seine aktive Auswahl leer: es laufen nur die Pflicht-Basisreads und die internen Hinweis-Reads (R111/R133/R189/R336), es entstehen keine Inverter-Entities, nur die App-Diagnosen. Die Vorlage ist dann lediglich der Vorschlag der Oberfläche. Mit dem ersten Speichern (jede Änderung oder ausdrückliches „Vorlage übernehmen“) beginnt das normale Polling. Bereits gespeicherte Auswahlen älterer Fassungen gelten als bestätigt; ohne die Erzwingung (`require_choice`, in der App immer an) verhält sich der Store wie zuvor.

### 9.4 Laufzeitbeobachtung (nur FC03, nur Anzeige)

Je Signal: Anzahl Antworten, Nullanteil, Min/Max, Variabilität, Tageslichtsamples, Firmware/SN/Profilrevision. Zustände `observed_active`, `zero_only_inconclusive`, `known_placeholder`, `read_failed`, `capability_conflict`, `alias_suspected`. Beobachtungen ändern **nie** still `support_status`/`release` und schalten keine Sensoren ab; sie erscheinen als Hinweis in der UI („liefert seit X Tagen tagsüber 0 – abwählen?“). Wochenlang 0 beweist keine fehlende Unterstützung. Keine Adressraumscans, keine Probes außerhalb des kuratierten Katalogs.

### 9.5 Ingress-UI

- Merkmale je Inverter (mit Vorschlag aus 9.2), durchsuchbarer, nach Kategorie/Messort gruppierter Katalog mit Häkchen; Abweichungen von der Vorlage markiert und einzeln zurücksetzbar; Hinweise aus 9.4; Planbudget-Anzeige. „Auf alle Inverter übertragen“ mit Vorschau und Capability-Prüfung je Zielgerät (keine Übertragung von SN-Bindung oder Schreibfreigaben). Statusansicht: Verbindungsweg, Identität (SN, Firmware, Rolle aus R336), Capability-Konflikte, Energie-Generation, letzte erfolgreiche Abfrage, Fehlerzähler. Aktion „Identität neu binden“ mit expliziter Inverterauswahl.
- **Erststart:** Karte und Einrichtung zeigen „Bitte Sensoren auswählen“ (Link zur Einrichtung); Schnellaktionen in Schritt 2: „Nur Basis“ (Seriennummer, Betriebszustand, Fehlerwörter 1–4) und „Alle abwählen“, jeweils als gewöhnliche Abweichungen von der Vorlage mit Vorschau und Speichern; „Vorlage unverändert übernehmen“ beendet den Zustand ohne Änderung.
- Technik: `ingress: true`, interner Port, relative URLs, keine Host-Portfreigabe. Leichtgewichtiges Frontend ohne Build-Kette (Lit/Preact als Vendor-Dateien im Paket, keine CDN-Abrufe), Backend `aiohttp` im selben Prozess, JSON-API, HA-Optik inkl. Dark Mode, DE/EN, Tastaturbedienung.
- **Sicherheit (DR2-08):** Verbindungen nur vom Supervisor-Peer `172.30.32.2`, sonst 403; `X-Remote-User-*`/Forwarded-Header nur nach dieser Peerprüfung auswerten; fehlende Benutzerangaben gelten nicht als Admin. `panel_admin: true`. Zustandsändernde Aufrufe nur per POST/PUT mit Revisionsnummer (`If-Match`-artig) und CSRF-Token; niemals per GET. Request-Schema nur bekannte Katalogkeys/Merkmale, keine freien Register/Pfade/Frames. Sensor-Häkchen können `allow_writes` nicht setzen. Neubindung invalidiert alte Snapshots, Capabilities und (später) Write-Permits.

### 9.5a Nachtrag T14

Die Ingress-UI enthält zusätzlich den Bereich „Anlage einrichten“ und den Einrichtungsassistenten (§4.1); die Navigation (Übersicht / Anlage einrichten) ist im Kopf der Seite.

Änderungsvertrag (Review 9): Eine vorgeschlagene Generation ist bis zum dauerhaften Plant-Commit *vorläufig*: sie schreibt keine gemeinsamen Dateien (`identity.json`: Bindungen/`expected_serial` nur im Speicher, erst nach dem Commit geschrieben; beim Start werden erwartete Bindungen aus dem akzeptierten Plant abgeleitet) und ihr Publisher läuft nicht (keine Discovery, keine Tombstones). Bei abgelehnter Änderung werden Auswahl-Specs und -Records exakt aus dem Transaktionszustand zurückgesetzt; scheitert der Rückbau, wird nichts weiterbetrieben (`plant_recovery_failed`, Änderungen gesperrt bis Neustart). Laufende Verbindungstests gehören zur Generation und werden vor der Stilllegung abgebrochen und abgewartet. Ein veralteter Auswahl-Record gelöschter IDs wird vor jeder weiteren Plant-Änderung (und beim Start) entfernt; ist das nicht möglich, bleibt die Anlage unverändert (fail closed).

### 9.6 Persistenz und atomarer Wechsel (DR2-07)

`/data/selection.json` mit `schema_version` und monotoner `revision`; Migration mit stabilen Keys, unbekannte/veraltete Keys werden gemeldet und verworfen. Neue Auswahl: validieren → übernehmen (Runtime/Publisher) → als Zwei-Slot-Dokument (`selection.a.json`/`selection.b.json`, je vollständig mit Sequenz und Prüfsumme, atomar tmp + fsync + rename in den Slot, der nicht den neuesten gültigen Stand hält) dauerhaft akzeptieren; Erfolg wird erst danach gemeldet, jeder Fehler (Übernahme oder Persistenz) nimmt die Übernahme zurück und lässt den akzeptierten Stand intakt; ist die Rücknahme nicht möglich, werden die Inverter gesperrt (`selection_recovery_required`). Beim Laden gilt der neueste vollständig gültige Slot. Die Revision ist an Plan, Zyklus und Publish-Auftrag gebunden: Antworten eines alten Plans werden nach Entfernen/Neudefinition einer Entity nicht mehr veröffentlicht. Discovery-Bestand (`/data/discovery.json`) wird mit der Revision abgeglichen; entfernte Komponenten werden gezielt aus der Device-Discovery genommen, ohne andere zu löschen; Neustart in jedem Übergang ist idempotent.

## 10. Schreibschicht (T13+, nach Abnahme des Lese-Teils)

Rahmen: pro Inverter `allow_writes: false` (Default); ohne Freigabe Verhalten exakt wie §1. Vor Implementierung wird ein eigener Write-Vertrag abgenommen, der mindestens REVIEW2 DR2-09 bis DR2-13 erfüllt:

- **Guard (DR2-09):** getrennte, typisierte Read-/Write-Operationen; FC16 = 0x10; Anzahl 1..123, Bytecount `2n`, PDU `6+2n`, RTU-ADU `9+2n`, MBAP-Gesamtlänge `13+2n`; V5-Hülle unverändert streng. Write-Response-Echo (FC/Start/Count) prüfen, Exception 0x90. Jede Operation bindet SN, Identitäts-/Profilrevision, Endpoint, Unit, erlaubte Wertkodierung und Maske. `allow_writes` + Permit **unmittelbar beim Senden** erneut prüfen; Fallback/Neubindung/Profilwechsel widerruft Permits. Kein globales „FC16 erlaubt“.
- **Bitfelder (DR2-10):** pro Operation eigene Bitmaske/Enum; Read-modify-write auf derselben identitätsgeprüften Verbindung im Bus-Lock; fremde Bits bleiben erhalten; Konflikt melden statt überschreiben. ToU-Pläne als fachliche Mehrregister-Operation.
- **Ergebnis (DR2-11):** Status `rejected, sent, readback_confirmed, failed, outcome_unknown`; keine automatische Wiederholung, kein Write-Fallback auf anderen Endpoint, kein Auto-Rollback. HA-Telemetrie zeigt immer den gelesenen Wert; Kommandoresultat separat.
- **Autorisierung/MQTT (DR2-12):** alle Eingänge prüfen aktuelle serverseitige Freigabe; Deaktivierung widerruft Queues/Permits und entfernt Command-Abos; keine nachgeholten Kommandos nach Reconnect/Neustart; Befristung, Deduplikation, Boot-/Freigabegeneration; retained Commands werden ignoriert.
- **Whitelist (DR2-13):** startet leer; jede Funktion einzeln mit Profil-/Firmware-/Precondition-/Verifikationsvertrag und ausdrücklicher Nutzerentscheidung. Topologie-, Schutz-, Batterieart-, Factory-, Reset-, Watchdog-Funktionen (u. a. R80, R111, R336, R341, R1100–R1121) nicht als Beifang; R336 nicht als erste Demo. Live-Writes führt ausschließlich der Nutzer aus.

## 11. Nicht in v1

Bank-Aggregation; HV-Batterie-Packs; LV-Pack-Register 10000+; eigene HA-Integration.
