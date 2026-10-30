# Erster Live-Lesetest: Deye SG04LP3-EU in Home Assistant

Runbook für vier SUN-10K-SG04LP3-EU im Insel-Parallelbetrieb, je ein RS485-LAN-Gateway **am ModeBUS** und ein WiFi-Logger. Gemeldete Firmware: HMI `1001-C070`, Main `2005-1175-1807`. Dieses Dokument bereitet den Test aus der Ferne vor; beim Erstellen wurden keine Anlagenverbindungen hergestellt. Die Fassung beruht auf dem Runbook des Reviewers (Codex, 29.10.2026) und ist auf den **aktuellen App-Stand** gebracht (Weboberfläche, Zwei-Slot-Auswahl, Diagnose-Entities, Optionsschema). Verbindungen und Wechselrichter richtest du seit T14 **in der Weboberfläche** ein (Assistent „Anlage einrichten“), nicht mehr in den App-Optionen; die YAML-Beispiele stehen nur noch als Migrationsweg im Anhang A und werden von einem Test der App gegen das Supervisor-Optionsschema und den App-Parser geprüft.

Die App ist **rein lesend**: Sie sendet an Inverter ausschließlich FC03-Lesezugriffe (siehe Abschnitt 9).

## 1. Startvoraussetzungen – zuerst abhaken

- [ ] Eine **konkrete Version** (Commit bzw. Image-Digest) ist für den Lesetest freigegeben: FC03-Sendeguard, Transporttests, MQTT-/HA-Veröffentlichung, Fehlerbehandlung und die E2E-Abnahme mit echtem Home Assistant (`e2e/`). Keinen ungeprüften Arbeitsbaum testen. Die Versionsnummer `0.1.0` allein identifiziert keinen Stand: Commit/Digest im Ergebnisblatt (Abschnitt 10) notieren.
- [ ] Die früheren Nachprüfungs-Findings N2-01 bis N2-04 (Energienamensraum, isolierter Identitätsreset, Wiederanlauf nach Blockierung, Reconnect-/Bestätigungszustände) sind in den Fix-Runden T7-Fix 2/3 behoben; dass die freigegebene Version diese Runden enthält, bestätigt die Freigabe aus dem ersten Punkt.
- [x] **Die App liest vor deiner ersten Auswahl nur Identität und Status.** Ein Inverter ohne gespeicherte Auswahl zeigt „Bitte Sensoren auswählen“ (Abschnitt 3.1); erst nach dem Speichern über die Weboberfläche beginnt das normale Lesen. Es gibt keine Optionen wie `selected_keys` und es werden keine Dateien in `/data` von Hand verändert.
- [x] Identität, aktive Auswahl/Leseplan und Verbindungsfehler sind einsehbar: Weboberfläche (Übersicht, Abfragelast je Auswahl) und die Diagnose-Entities des App-Geräts (Abschnitt 7).
- [ ] Du kannst die App über HA zuverlässig stoppen und hast ein unabhängiges, bereits funktionierendes Anlagenmonitoring. Kritische Verbraucher sind für diesen Test nicht auf neue Sensoren angewiesen. Bei einer Störung ist ein Ansprechpartner vor Ort erreichbar; ein manueller Wiederanlauf aus der Ferne wird nicht vorausgesetzt.
- [ ] HA-MQTT-Integration und Broker sind betriebsbereit. Konfiguration und vorhandene App-Daten sind gesichert (HA-Backup einschließlich Add-on/App-Daten). Beim ersten Versuch **keine** Energie-Entitäten ins Energy Dashboard und keine neuen Entitäten in Steuerautomationen aufnehmen.

In **Einstellungen → Apps** (bei älteren HA-Versionen „Add-ons“) die Test-App öffnen. Für diese App **Start beim Booten, Watchdog und automatische Updates ausschalten**; vorhandene Anlagensteuerungen unverändert lassen. Einen zweiten Browser-Tab mit der App-Infoseite und der Schaltfläche **Stoppen** offen halten. App-Ausgaben erscheinen im Log-Tab; `/data` bleibt bei Neustart/Update erhalten. Die Weboberfläche öffnest du über den Seitenleisteneintrag **Deye Monitor** (nur für HA-Administratoren). [HA: App-Lebenszyklus](https://contributors.home-assistant.io/apps/how-apps-work/)

## 2. Verbindungsdaten selbst nachsehen – nichts scannen oder umstellen

Fülle diese Tabelle anhand deiner bestehenden Konfiguration, erreichbarer Statusseiten und vorhandener Typenschildfotos aus. Historische KB-Adressen sind keine Bestätigung deiner heutigen Einrichtung.

| Gerät / bestehende Rolle | Inverter-SN, exakt 10 Zeichen | Gateway-Modell/Firmware, LAN-IP:Port | Gateway-Protokoll / Unit-ID | Logger-LAN-IP:Port | Numerische Logger-SN / Unit-ID |
| --- | --- | --- | --- | --- | --- |
| deye1 / … | … | … | … | … | … |
| deye2 / … | … | … | … | … | … |
| deye3 / … | … | … | … | … | … |
| deye4 / … | … | … | … | … | … |

**Drei Kennungen auseinanderhalten:** Inverter-SN identifiziert das physische Gerät; numerische Logger-SN adressiert den Solarman-V5-Logger; Modbus-Unit-ID adressiert den Inverter über einen bestimmten Weg. Logger-SN ist weder WLAN-Passwort noch Inverter-SN. Parallelrolle/-nummer aus R336 ist kein ausreichender Nachweis der richtigen Unit-ID auf jedem Transport; das Modbus-Adressfeld R1 ist nur ein gemeldetes Feld, die benötigte Adresse ist je Verbindungsweg zu prüfen. Separate Gateways dürfen jeweils Unit-ID 1 verwenden; nichts auf 1 oder fortlaufend 1–4 umkonfigurieren.

### Gateway: bestehende Einstellungen nur ansehen

Öffne die **bereits bekannte** LAN-Webadresse. Erfasse Modell/Revision, Firmware, TCP-Server-Port und vorhandene Protokollkonvertierung; je nach Waveshare-Modell heißen die Seiten etwa „Quick Configure“, „Application Setting“, „Transfer Protocol“ oder „Mode Selection“.

| Tatsächlich eingerichtete Rahmung | App-Transport | Erkennungsmerkmal |
| --- | --- | --- |
| Modbus TCP → Modbus RTU | `modbus_tcp` | TCP-Seite mit MBAP-Header; RTU-CRC wird vom Gateway behandelt |
| Transparenter serieller TCP-Tunnel | `rtu_over_tcp` | TCP transportiert RTU-Rahmen einschließlich CRC |

**Port 502 beweist die Rahmung nicht.** Bestehende funktionierende Client-Konfiguration und zum exakten Modell passende Anleitung verwenden. Ist der Modus unbekannt, den Test vertagen; nicht durch wechselnde Treiber oder Gateway-Umkonfiguration erraten. Baudrate, Parität, Stopbits und Unit-ID nur dokumentieren. Keine „Save“, „Apply“, „Download“, „Clear“, „Reset“, Firmware-Update- oder Neustartaktion ausführen.

Prüfe außerdem vorhandene JSON-/MQTT-/HTTP-Upload-Funktionen, Registertabellen, Polling-/Cache-/Storage-Modus, TCP-Clients und Wiederholungen. Einige Varianten verwalten Abfragen intern und können selbst weiter pollen. Die exakten Funktionen sind modellabhängig. Bei B-Varianten erläutert die Herstelleranleitung JSON-Konfiguration, Registerabfragen und Uploadzeiten; deren „Serial port polling time“ ist die Pause zum nächsten Eintrag, nicht zwingend dessen Wiederholungsperiode. [Waveshare-B: MQTT/JSON-Handbuch, S. 3–10](https://files.waveshare.com/upload/a/a6/EN-RS485-TO-ETH-B-MQTT-and-json-user-manual2.pdf)

Ein früherer Master-Export der Wissensbasis (47 Einträge, unter anderem `Query Period=1000`) beweist keine heute aktive Konfiguration und keine bestimmte Anfragenzahl pro Sekunde. Sein `last_update` stammt aus „Current Time“, nicht aus einem Inverter-Frischezähler; sein `online` liest R1. Ein frischer JSON-Zeitstempel allein belegt daher keine frischen Messwerte. JSON-Downloadtabellen können Befehlswege enthalten: **keine Tabelle importieren oder testweise auslösen**.

### WiFi-Logger: IP und Seriennummer bestätigen

Nutze die bekannte LAN-IP, Router-Geräteliste, vorhandene Solarman-Gerätedetails oder ein bestehendes Foto des Logger-Aufklebers. In der erreichbaren Logger-Weboberfläche ist „Status“ die passende Informationsseite. Die LSW-3-Handbücher beschreiben daneben AP-Einrichtung; `10.10.100.254` gehört zu diesem AP-Zugang, nicht automatisch zum aus der Ferne erreichbaren LAN. Keinen WLAN-Wechsel, Einrichtungsassistenten, Reset oder Reboot ausführen.

Geplant ist **WiFi-Logger per IP, Solarman V5**, üblicherweise Port 8899, mit numerischer Logger-SN. Kein physisch serieller Loggerweg. Der Port allein bestätigt keine erfolgreiche V5-Verbindung. Nicht von nativem Modbus TCP im Logger ausgehen.

### Andere Abfrager und bestehende Steuerungen erfassen

| Vorhandener Dienst | Welches Gerät / welcher Weg? | Intervall / Clientzahl / letzte Datenlücke | Für Steuerung erforderlich? |
| --- | --- | --- | --- |
| Waveshare-interne JSON-/MQTT-/HTTP-Abfrage | … | … | … |
| HA Modbus, ha-solarman, sunsynk, SolarAssistant, evcc, eigene Skripte | … | … | … |
| Solarman-Cloud und Logger-Upload | … | … | … |
| EMS/Automationen, die aus diesen Daten Aktionen ableiten | … | … | … |

Keine zusätzliche Debug-App oder Modbus-Konsole parallel starten. Nicht benötigte lokale Abfrager kannst **nur du selbst**, nach Prüfung ihrer Steuerungsabhängigkeiten, für ein begrenztes Testfenster pausieren. Keine kritische Automation blind abschalten. Cloud aktiv lassen und deren Aktualisierung beobachten; eine unbekannte maximale Logger-Clientzahl nicht durch Lasttests ermitteln. Gateway am ModeBUS und Logger sind verschiedene Wege; ein gemeinsam genutzter elektrischer RS485-Bus ist dadurch **nicht** bewiesen. Gleichwohl können Inverterressourcen begrenzt sein. App-interne `bus_id`-Queues koordinieren keine fremden Clients, Cloud-Abfragen oder Gateway-Autopoller.

## 3. Ausgangszustand und minimale Auswahl vorbereiten

1. Mindestens 15 Minuten, bei langsamem Cloud-Upload länger, das vorhandene Monitoring beobachten. Uhrzeit/Zeitzone, Fehler/Alarme, Cloud-Aktualisierungsabstand, vorhandene Timeouts und Logger-Verbindungsabbrüche notieren. Eine bereits instabile Anlage ist kein geeigneter Ersttest.
2. Einen Inverter auswählen, dessen beide Kommunikationswege und Inverter-SN bekannt sind. Die Bezeichnung „Master“ legt keine bevorzugte Testreihenfolge fest; keine Parallelrolle ändern.
3. App stoppen, freigegebene Version und vollständige Optionen notieren. `instance_id` sowie Inverter-`id` für alle folgenden Stufen stabil halten. Nicht mehrere App-Instanzen mit denselben IDs gleichzeitig betreiben.

### 3.1 Erststart: „Bitte Sensoren auswählen“

Solange für einen Inverter **noch nie** eine Auswahl gespeichert wurde, liest die App nur die zwingenden Identitäts- und Statusregister und die internen Hinweis-Register (siehe unten) – **keine Sensoren der Standardvorlage** (sonst wären es 59 Sensoren und 30 Lese-Blöcke inklusive Energie und R65) und es entstehen keine Inverter-Entities, nur die Diagnose-Entities des App-Geräts. Bereits gespeicherte Auswahlen einer früheren Version gelten als bestätigt.

1. Die App starten (die App-Optionen bleiben bis auf `instance_id` leer) und Verbindung und Inverter im Einrichtungsassistenten der Weboberfläche anlegen (Stufe 1, Abschnitt 4).
2. In der Weboberfläche (Seitenleiste **Deye Monitor**) zeigt die Karte des Inverters nach der Identifikation **„Bitte Sensoren auswählen“**. Dort **„Auswahl jetzt treffen“** öffnen (oder **Sensoren einrichten**).
3. Schritt 1 „Anlage beschreiben“ **wahrheitsgemäß** beantworten (Insel/Netz, Akku ja/nein, BMS, GEN-Anschluss, PV-Eingänge, Parallelbetrieb). Ein Hinweis „Die Geräteeinstellung meldet …“ ist **kein Nachweis der Installation** und ändert nichts von selbst; deine Antworten bleiben gespeichert.
4. Schritt 2 „Sensoren wählen“: **„Nur Basis“** wählt `serial_number` (Seriennummer), `operating_state` (Betriebszustand) und `fault_word_1` bis `fault_word_4` (Fehlerwörter 1–4) und wählt alles andere ab; **„Alle abwählen“** wählt nichts aus. Beides sind gewöhnliche Abweichungen von der Vorlage (einzeln rücksetzbar, bleiben bei späteren Änderungen der Anlagenbeschreibung erhalten).
5. Die Leiste unten zeigt die **Abfragelast**, „Das ändert sich beim Speichern“ die wegfallenden Sensoren. Erwartet für „Nur Basis“: **6 Sensoren, 12 Abfrageblöcke**, Last „niedrig“, keine Energie, kein R65, keine Batterie-/AC-/PV-Gruppe.
6. **Speichern.** Die Auswahl gilt erst als gespeichert, wenn sie dauerhaft abgelegt ist; danach verschwindet der Hinweis „Bitte Sensoren auswählen“ und das normale Lesen beginnt – mit der kleinen Auswahl.

Wer die Vorlage unverändert will, nutzt **„Vorlage unverändert übernehmen“**; für diesen Test nicht empfohlen.

„Nur Identität und Status“ umfasst zusätzlich notwendige **interne lesende** Abfragen: R0, R3–7, R14–15, R17–18, R20–22, R24, R336 sowie R500 und R551–558. Für die Merkmalshinweise liest die App außerdem R111, R133 und R189 (Gruppe „Einstellungen“, nur Diagnose). Das sind keine Schreibzugriffe. Bei `max_read_registers=20` und ohne Lückenüberbrückung ergibt das **12 Lese-Blöcke** pro vollständigem Plan (Identität 7, Status 2, Einstellungen 3), nicht 12 Anfragen alle 30 Sekunden: jede Gruppe hat ihr eigenes Intervall; Identifikation und Recovery können zusätzliche Probes auslösen. R65 wird in dieser Auswahl nicht benötigt. Im Zustand „Bitte Sensoren auswählen“ sind es dieselben Pflicht- und Hinweis-Register.

## 4. Stufe 1 – ein Inverter, nur das Gateway

Die Einrichtung geschieht in der Weboberfläche (Seitenleiste **Deye Monitor**): Bei leerer Anlage startet der **Einrichtungsassistent** von selbst, sonst öffnest du **Anlage einrichten** (oder „Einrichtung starten“ auf der leeren Übersicht). Der Assistent führt durch **Verbindung anlegen → Wechselrichter anlegen → Verbindung prüfen → Sensoren wählen**. Änderungen gelten sofort; die App muss dafür nicht neu gestartet werden.

1. **Verbindung** (Schritt 1): Art **„Gateway (Modbus TCP → RTU)“** – nur für einen bestätigten Modbus-TCP→RTU-Modus –, Adresse (z. B. `192.0.2.11`) und Port `502`, Kennung `gw1`. Unter **Erweitert** vorsichtige Startwerte setzen: Zeitlimit `5`, Blockgröße `20`, Pause `250`, Warteschlange `8`. Bei einem **nachweislich transparenten** Gateway stattdessen **„Gateway transparent (RTU über TCP)“** und dessen tatsächlich eingerichteten Port. Dort ist optional ein **Nachlauffenster** von `10` s einstellbar: bei Zeitlimit 5 entspricht das dem heutigen Standard (`max(1 s, 2 × Zeitlimit)`). Diese zehn Sekunden sind eine **Annahme über verspätete Antworten**, keine Deye-Garantie. Bei unbekannten Verzögerungen/Cache-Effekten nicht weiter testen; keine unbestätigte Antwort wird als frisch ausgegeben.
2. **Wechselrichter** (Schritt 2): Name `Deye 1`, Kennung `deye1` (nach dem Anlegen nicht mehr änderbar), Verbindung `gw1` aus der Auswahlliste, Modbus-Adresse `1`, **erwartete Seriennummer** `2401230001` (die bestätigte Inverter-SN), Energie-Auflösung „Automatisch“. Unter **Abfrageintervalle (erweitert)**: `30` / `300` / `600` / `3600` s. Das Schreiben ist in dieser Version nicht verfügbar (die Oberfläche zeigt „Schreiben: nicht verfügbar“, `allow_writes` lässt sich dort nicht setzen).
3. **Prüfen** (Schritt 3): „Verbindung prüfen“ liest ausschließlich die Kennung des Wechselrichters (FC03, durch den Sendeguard, über den Bus-Scheduler der laufenden Verbindung) und zeigt Seriennummer, MPPT-/Phasenzahl und Auffälligkeiten. Bei einem Fehler nennt die Anzeige typische Ursachen (z. B. „Falscher Transport? Gateway-Modus prüfen“). Dieselbe Prüfung gibt es später je Verbindung und je Inverter unter **Anlage einrichten**.
4. **Sensoren** (Schritt 4): weiter zu Abschnitt 3.1 mit **„Nur Basis“**.

`max_read_registers` (Blockgröße) `20`, 250 ms Abstand und diese Intervalle sind vorsichtige Startwerte, keine vom Hersteller bestätigten Belastungsgrenzen. Normale Abfragen dürfen kürzer sein. Eine Warteschlange von `8` erlaubt keine acht gleichzeitigen Transaktionen auf demselben App-Bus.

5. Nach dem Speichern des Inverters beginnt die Abfrage sofort. Ab jetzt Logs, Abbruchkriterien und unabhängiges Monitoring gleichzeitig im Blick halten. Der Inverter liest zunächst nur Identität und Status (Abschnitt 3.1); danach die minimale Auswahl speichern.
6. Innerhalb der ersten Minuten Identifikation, gebundene SN und frische Statusdaten prüfen (Abschnitt 7: Zustand „Online“, Entity `…_serial` = `expected_serial`). `ready` im Log bestätigt lediglich gestartete Schleifen, keinen erfolgreichen Gerätekontakt. Bei falscher SN/Quarantäne („Gesperrt“) sofort stoppen, Zuordnung außerhalb der App prüfen; **kein Identity-Reset**, um einen Fehler zu übergehen.
7. 15 Minuten beobachten. Erwartet: wiederkehrende erfolgreiche FC03-Zyklen ohne wachsende Queue, wiederholte Cooldowns oder neue Störungen. Minutenlang unveränderte Leistungswerte sind mit dieser Auswahl kein Testfehler: es werden noch keine Leistungswerte gelesen.
8. Bei stabilem Verlauf in der Weboberfläche zusätzlich `battery_voltage`, `battery_soc`, `inverter_frequency`, `pv1_power`, `pv2_power` aktivieren (Suche, Häkchen setzen) und die Vorschau prüfen. Erwartet: **11 Sensoren, 15 Abfrageblöcke** (zusätzlich Batterie 1, AC 1, PV 1), weiterhin ohne Energie und R65. Speichern und 15 Minuten beobachten, möglichst bei Tageslicht. Keine Diagnosefreigabe für PV3–PV10 oder Batterie 2 (die Experten-Rohwerte bleiben abgewählt).
9. Energie erst danach separat auswählen (Energiezähler über die Suche „Energie“ der Weboberfläche), R65 und dessen Auflösung prüfen und mindestens einen Energiezyklus abwarten. Den Weg nicht zusammen mit der Sensorauswahl wechseln: pro Änderung nur eine Einflussgröße. Danach App stoppen und den letzten freigegebenen Zustand protokollieren.

`mqtt: {}` nutzt den bereits eingerichteten Supervisor-MQTT-Dienst; es startet keinen Broker. Fehlende HA-Entitäten bei funktionierender Verbindung sind separat zu untersuchen, ohne schnelleres Geräte-Polling zu erzwingen. Anfangs `info`; nur bei Bedarf ein begrenztes Debug-Fenster und vor Weitergabe IPs/SNs/Zugangsdaten aus Logs entfernen.

## 5. Stufe 2 – denselben Inverter nacheinander über den Logger lesen

Erst nach erfolgreicher Gateway-Stufe. In **Anlage einrichten** eine zweite Verbindung **„WiFi-Logger (Solarman V5)“** anlegen (Kennung `log1`, Adresse z. B. `192.0.2.21`, Port `8899`, **Logger-Seriennummer** `1700000001` – die Seriennummer des Sticks, nicht die des Wechselrichters; Erweitert wie in Stufe 1) und mit „Verbindung prüfen“ ausprobieren. Dann den Inverter `deye1` bearbeiten und als Verbindung `log1` wählen; Kennung, `instance_id`, erwartete Inverter-SN und Auswahl bleiben gleich (die Auswahl hängt an der Inverter-Kennung, nicht am Weg). Die bisherige Verbindung `gw1` bleibt zunächst angelegt, wird aber nicht mehr abgefragt, sobald kein Inverter sie nutzt; die **Gateway-Abfrage dieser App** ist damit beendet, bevor der Logger abgefragt wird (der Umbau stoppt die alte Abfrage und startet die neue). Zuerst wieder die minimale Auswahl, danach dieselbe kleine Messauswahl wie in Stufe 1. Nicht beide Wege parallel abfragen, zunächst keine Ersatzverbindung festlegen.

Je Auswahl mindestens 15 Minuten und mehrere normale Cloud-Uploadzyklen beobachten. Ein bestehender Gateway-Autopoller kann weiterhin arbeiten, obwohl diese App nur den Logger benutzt: diesen Zustand im Vergleich vermerken. Es gibt keine belegte universelle Client-/Polling-Grenze für deine Logger-/Firmwarekombination.

Vergleiche die **Inverter-SN exakt**, außerdem statische Rohdaten/Firmwarekennung, R22/R24 und – nur wenn gelesen – R65. Ein Transportwechsel darf weder ein anderes physisches Gerät unterschieben noch die Energiestatistik als neue physische Quelle behandeln (bei gleicher SN und gleicher Auflösung bleibt die Energieserie). Bei bekannten Recovery-/Persistenzproblemen anhalten, nicht Dateien löschen.

Dynamische Werte in Zeitfenstern mit Zeitstempel, Minimum/Maximum und Betriebszustand vergleichen. Nacheinander erfasste PV-/Leistungswerte müssen nicht exakt gleich sein; wechselnde Sonne und Last sind normal. Cloudwerte können erheblich älter sein. Beide Wege lesen denselben Inverter und sind keine zwei unabhängigen Messgeräte. Ein unveränderter Zähler oder Status beweist keine eingefrorenen Daten; natürliche Änderungen eines zusätzlichen Messwertes liefern bessere Hinweise. Keine Last-/Schaltversuche zur „Anregung“.

## 6. Stufe 3 – auf vier Inverter erweitern

Für jeden weiteren Inverter zuerst dessen eigene Gateway-/Logger-Zuordnung prüfen. Anschließend dieselbe kleine Auswahl schrittweise für **zwei, drei, vier** Inverter aktivieren: jeweils unter **Anlage einrichten** die Verbindung und den Inverter ergänzen (Änderungen gelten sofort, die übrigen Inverter laufen nach einem kurzen Neustart der Abfrage weiter): der neue Inverter zeigt „Bitte Sensoren auswählen“ und liest nur Identität und Status, bis du seine Auswahl gespeichert hast (mit „Nur Basis“ oder mit „Auf andere übertragen …“ von einem bereits eingerichteten Inverter; Seriennummer-Bindung, Identität, Energieverlauf werden dabei nie übertragen). Nach dem Speichern mindestens 15 Minuten beobachten. Nicht allein durch Hinzufügen eines Geräts dessen volle Standardauswahl ungeprüft aktivieren. Das Zielbild sind vier separate, bestätigte Modbus-TCP-Gateways (`gw1` … `gw4`, je Port 502, Zeitlimit 5, Blockgröße 20, Pause 250, Warteschlange 8) mit je einem Inverter `deye1` … `deye4` (Modbus-Adresse 1, erwartete Seriennummern `2401230001` … `2401230004`, Intervalle 30/300/600/3600 s); reale Werte einsetzen. Als Migrationsweg steht es zusätzlich als YAML im Anhang A.

Mit vier Geräten mindestens eine Stunde einschließlich langsamer Gruppen beobachten; die Weboberfläche zeigt die **Gesamtlast am Bus** je Auswahl. Danach eine längere Beobachtung über Tages-/Nachtwechsel vereinbaren, bevor Intervalle verkürzt oder weitere Sensoren aktiviert werden. Keine Summen aus SOC, gemeinsamem Batterie-SOC oder möglicherweise bereits aggregierten Parallelwerten bilden. Gleiche Werte mehrerer Geräte sind nicht automatisch unabhängige Messungen. Die Auswahl und Messstelle jedes Sensors festhalten.

Eine optionale **Ersatzverbindung** (im Inverter-Formular, mit eigener Modbus-Adresse) erst **nach separater Prüfung beider Wege jedes Inverters** festlegen: sie verweist auf denselben Inverter mit identischer Inverter-SN; die App wechselt erst nach mehreren fehlgeschlagenen Zyklen und nur, wenn dort dieselbe SN antwortet. Aus der Ferne keinen Ausfall provozieren: kein Gateway-/Logger-Neustart, kein Steckerziehen, keine Firewall-Unterbrechung und kein Inverter-Abschalten. Der nacheinander geprüfte Quellenwechsel reicht für diesen Ersttest; er beweist keinen erfolgreichen automatischen Failover am echten Gerät.

## 7. Diagnose und Plausibilität prüfen

In HA: App-Log, die Weboberfläche und unter **Entwicklerwerkzeuge → Zustände** Zeitstempel/Einheiten der Entities. Keine Aktionen oder MQTT-Nachrichten an Befehlsthemen senden (es gibt keine).

**Diagnose-Entities des App-Geräts** (`deye_<instance_id>_app`, hier mit `instance_id: deyetest1` und Inverter `deye1`; alle `entity_category: diagnostic`, jeweils auch für `deye2` … sobald konfiguriert):

| Entity | Bedeutung / Prüfpunkt |
| --- | --- |
| `sensor.deye_deyetest1_app_app_version` | Version der App; mit dem freigegebenen Stand vergleichen. |
| `sensor.deye_deyetest1_app_inverters_online` / `…_inverters_configured` | Anzahl online / konfiguriert. |
| `sensor.deye_deyetest1_app_mqtt_reconnects` | Neuverbindungen zum Broker; wachsend = Broker-/Netzproblem. |
| `sensor.deye_deyetest1_app_inverter_deye1_state` | `starting`, `identifying`, `running`, `quarantined`, `offline`, `blocked` (Weboberfläche: Startet, Wird erkannt, Online, Gesperrt, Offline, Blockiert). Erwartet: `running`. |
| `sensor.deye_deyetest1_app_inverter_deye1_reason` | Grund in Textform (z. B. „no usable identity answer“, „serial number differs“); bei jedem Zustand außer `running` zuerst lesen. |
| `sensor.deye_deyetest1_app_inverter_deye1_path` | Aktiver Weg (`primary`/`fallback`) und Endpoint. |
| `sensor.deye_deyetest1_app_inverter_deye1_serial` | Gebundene Inverter-SN = `expected_serial`, je Gerät eindeutig. |
| `sensor.deye_deyetest1_app_inverter_deye1_firmware` | Firmwarewörter R15/R17/R18. |
| `sensor.deye_deyetest1_app_inverter_deye1_role` | Parallelrolle aus R336 (nur Anzeige). |
| `sensor.deye_deyetest1_app_inverter_deye1_r65_raw` / `…_r65_status` | R65 Rohwert und ob die letzte Lesung funktionierte. In der Minimalstufe ist fehlendes R65 normal. |
| `sensor.deye_deyetest1_app_inverter_deye1_energy_resolution` / `…_energy_source` / `…_energy_status` | Effektive Auflösung (kWh/Schritt), Quelle (`r65`/Override), Bestätigung/Sperre. |
| `sensor.deye_deyetest1_app_inverter_deye1_energy_series` | Aktuelle Energieserie (`g0`, `g1`, … bzw. `<Token>g0`). |
| `sensor.deye_deyetest1_app_inverter_deye1_capability_conflicts` | Anzahl der Konflikte mit dem Hardwareprofil; die Liste steht im Attribut `conflicts`. |
| `sensor.deye_deyetest1_app_inverter_deye1_bus_utilisation` | Auslastung des App-Busses (Schätzung, 1,0 = voll). |
| `sensor.deye_deyetest1_app_inverter_deye1_transport_timeouts` / `…_transport_frame_errors` / `…_transport_resyncs` | Zähler; Zuwachs ist ein Abbruchkriterium (Abschnitt 8). |
| `sensor.deye_deyetest1_app_inverter_deye1_transport_cooldown` | Wartezeit nach Fehlern (Backoff). |
| `sensor.deye_deyetest1_app_inverter_deye1_last_success` | Zeitpunkt der letzten erfolgreichen Abfrage; sollte höchstens wenige Intervalle alt sein. |

**Inverter-Entities** (nur ausgewählte Sensoren): `sensor.deye_deyetest1_deye1_<schlüssel>`, z. B. `…_serial_number`, `…_operating_state`, `…_fault_word_1` … `…_fault_word_4`, später `…_battery_voltage`, `…_battery_soc`, `…_inverter_frequency`, `…_pv1_power`, `…_pv2_power`. Energiezähler tragen die Serie im Namen, z. B. `…_battery_energy_charge_total_g0`.

| Prüfung | Erwartung / Vorgehen |
| --- | --- |
| Identität | `expected_serial` = gebundene Inverter-SN (`…_serial`). Logger-SN separat. Aktiver Endpoint (`…_path`), letzter erfolgreicher Zyklus (`…_last_success`) und Zustand sichtbar; kein `quarantined`/`blocked`. |
| R22 / R24 | R22: Highbyte MPPT-Zahl, Lowbyte Phasenzahl; Profil erwartet 2/3, also `0x0203`. Das ist eine Erwartung, kein bereits gemessener Wert deiner Geräte. R24: 0 oder 1 bedeutet laut Dokument einen Batterieeingang, 2 zwei. Kein Pack-/BMS-Zähler. `…_capability_conflicts` dokumentieren und betroffene Messungen nicht freigeben. |
| R65 / Energie | Wenn Energie ausgewählt ist: Rohwert, effektiven Faktor und Quelle (`auto`/Override) festhalten. `R65 <= 0x0104`: 0,1 kWh/Schritt; sonst 1 kWh/Schritt, nur für die dafür kuratierten Zähler. Fehlend ist nicht 0. Kein Override zum bloßen „Passendmachen“. |
| Energieserie | Bei unveränderter SN und Auflösung bleibt `…_energy_series` über Neustart und Transportwechsel stabil. Eine bestätigte Änderung der Auflösung, ein anderes Gerät oder ein bewusst bestätigter Historienverlust beginnt eine neue Serie (alte Entity entfällt, ihre Statistik bleibt in HA, ohne Verbindung zur neuen). Bei Verlust der Energiehistorie wird die Energie gesperrt (`energy_history_missing`/`…_inconsistent`/`energy_store_corrupt`) und beim ausdrücklichen Reset beginnt ein zufälliges 8-Zeichen-Token. Nicht selbst `/data` löschen. |
| Last / Fehler | Anzahl Lese-Blöcke je Gruppe und Gesamtlast in der Weboberfläche, `…_bus_utilisation`, Timeouts, Reconnects, Cooldown. Planbudget ist eine Schätzung; fremde Poller/Cloud/Cache sind nicht enthalten. „Überlastet“, wachsende Queue oder wiederholte Recovery sind kein Anlass für schnellere Wiederholungen. |
| Qualität in HA | Ausbleibende/ungültige Daten erscheinen als `unknown` (einzelner Wert) bzw. `unavailable` (Gruppe verstummt nach drei Intervallen oder App/Inverter offline), nie als erfundene Nullwerte. Nach Reconnect keine alten Momentanwerte als frische Messung. `Online` allein beweist nicht die Frische jeder Gruppe. |
| SOC R588 | u16, 1 %, plausibel 0–100. Bei gemeinsamer Batterie können die vier Werte gleich sein; nicht addieren. |
| Batteriespannung R587 | u16 × 0,01 V; z. B. Rohwert 5200 → 52,00 V. Mit vorhandener BMS-Anzeige und deren tatsächlichen Grenzwerten vergleichen. Das ist keine neue Ladespannungsempfehlung. |
| Inverterfrequenz R638 | u16 × 0,01 Hz; im laufenden 50-Hz-Inselnetz ungefähr 50 Hz. 0,50 oder 5000 Hz deutet auf Dekodierung/Zuordnung. Zustand und Messstelle beachten; keine Schutzgrenzen daraus ableiten. (Die ähnliche „Lastfrequenz“ R655 ist nur ein Duplikat derselben Quelle und nicht Teil dieser Auswahl.) |
| PV1/PV2 R672/R673 | Je u16, 1 W. Tagsüber mit vorhandener Anzeige plausibilisieren; Schatten/Abregelung beachten. Nachts 0 ist erwartbar und kein Nachweis fehlender Unterstützung. Drei Stringanschlüsse bedeuten bei deinem 10-kW-Modell zwei MPPT-Messkanäle (2+1 Strings), nicht PV3. PV3–PV10 bleiben gesperrt. |
| Mehrwortwerte | Beide Wörter aus demselben Lesezyklus und Weg; 32-Bit-Paarung/Wortreihenfolge laut Profil. Das garantiert keinen exakt gleichzeitigen internen DSP-Snapshot. |

## 8. Abbruch und Rückkehr zum Ausgangszustand

**Sofort die App stoppen**, sobald neue Inverter-/Parallel-/BMS-Fehler, Inselnetzunterbrechung oder Flackern auftreten. Ebenso bei falscher Identität, unbekannter Serienbindung, kritischem Persistenzfehler (`blocked`) oder neuer Logger-Abmeldung. Keine Wiederanlaufexperimente.

Für reine Kommunikationsfehler gelten zusätzlich vorsichtige **Test-Abbruchregeln**, keine Deye-Grenzwerte: drei aufeinanderfolgende Timeouts/CRC-/V5-Sequenzfehler (Zuwachs bei `…_transport_timeouts`/`…_transport_frame_errors`), wiederkehrende Cooldowns/Reconnects oder wachsende Queue → stoppen. Neue Cloud-Datenlücken von mehr als zwei vorher üblichen Uploadperioden → stoppen und mit der Ausgangslage vergleichen; bei gleichzeitigem Anlagenfehler nicht auf diese Frist warten.

1. Im bereitgehaltenen HA-Tab **Einstellungen → Apps → Deye SG04LP3 Monitor → Info → Stoppen**. Stoppen der Monitor-App ist **kein OFF-Befehl an den Inverter**. Auch bei nicht erreichbarer Ingress-Seite die Supervisor-App-Seite verwenden. Nicht HA/Supervisor oder den MQTT-Broker als Ersatz neu starten. Die App meldet beim geordneten Stopp alle Inverter und sich selbst offline (bei hartem Ende über den MQTT-„Last Will“).
2. Gestoppten Zustand bestätigen (alle Entities `unavailable`). Wenn der Status unklar bleibt, keine weitere Testinstanz starten. Unabhängiges Monitoring und Cloud über mehrere normale Zyklen beobachten.
3. Logs, Zeitpunkt, aktiven Weg und letzte Konfiguration sichern. Nicht als erste Reaktion Identitäts-/Energie-/Discovery-Dateien löschen, Geräte neu binden oder Fehlermeldungen quittieren. Insbesondere `blocked`/`quarantined` nicht durch Reset umgehen.
4. Nur falls du zuvor einen unabhängigen lokalen Poller bewusst pausiert hast: dessen ursprünglichen Zustand wiederherstellen. Ein Gateway mit autonomem Polling/Cache kann trotz gestoppter App weiter abfragen; App-Stopp garantiert keinen elektrisch stillen Bus. Bei anhaltenden Problemen Test beenden und die vorhandene Betreuung nutzen.

### Wo die Daten der App liegen

Alles liegt im Datenverzeichnis `/data` der App (bleibt bei Neustart und Update erhalten, ist im HA-Backup der App enthalten; nur per SSH-Zugriff auf das Host-System sichtbar, **nicht zum Bearbeiten gedacht**):

| Datei | Inhalt |
| --- | --- |
| `instance_id` | Instanzkennung (Teil aller Entity-IDs; mit der Option `instance_id` fest vorgeben) |
| `plant.a.json`, `plant.b.json` | Verbindungen und Wechselrichter, wie in der Weboberfläche eingerichtet (zwei Slots; der neueste vollständige gilt) |
| `selection.a.json`, `selection.b.json` | gespeicherte Sensorauswahl und Anlagenmerkmale (zwei Slots; der neueste vollständige gilt) |
| `identity.json` | Bindungen Inverter-`id` ↔ Seriennummer |
| `energy.json`, `energy-generations.json` | Energieauflösung/-serie je Inverter und deren Historie |
| `discovery.json` | Liste der veröffentlichten HA-Entities (für das Aufräumen) |
| `capabilities.json` | Cache der Hardwareprüfung (darf neu aufgebaut werden) |

Löschen einer dieser Dateien ist ein Eingriff mit Folgen (neue Bindung, neue Energieserie, verwaiste HA-Entities). Wenn du eine Rückkehr zum Ausgangszustand brauchst, genügt: App stoppen und deinstallieren/belassen; Home Assistant behält die Statistik bereits angelegter Entities, entfernte Entities musst du bei nicht erreichbarer App von Hand in HA löschen.

### Wartungsbereich der Weboberfläche (Reset)

Nur nach Ursachenklärung und nur bewusst; jede Aktion erklärt ihre Wirkung und verlangt das Eintippen eines Bestätigungstextes:

- **Identität neu binden** (je Inverter): vergisst die gespeicherte Seriennummer; der Inverter wird neu erkannt und neu gebunden. Gesperrt, solange am Inverter eine erwartete Seriennummer eingetragen ist (dann diese unter **Anlage einrichten** ändern).
- **Zurücksetzen → Seriennummer-Bindung** (je Inverter): wie oben für diesen einen Inverter; die anderen laufen weiter.
- **Zurücksetzen → Energiezähler-Verlauf** (je Inverter): beginnt eine **neue Zählerreihe** (neue Energie-Entities mit neuer Serie; die alte Statistik bleibt in HA, ungekoppelt).
- **Alle Bindungen zurücksetzen** (**global**): löscht die Bindungen **aller** Inverter, jeder wird neu erkannt (vorgesehen für eine beschädigte `identity.json`).
- **Cleanup-Liste leeren**: danach räumt die App entfernte Entities nicht mehr automatisch auf. Während des Tests nicht verwenden.

Ein Reset ist **kein** Mittel, um `quarantined`/`blocked` zu überspielen.

## 9. Harte Grenze dieses Tests

Die App sendet an Inverter **ausschließlich FC03-Lesezugriffe** in der zum Transport passenden Rahmung; ein Sendeguard am untersten Pfad erzwingt das unabhängig von jeder Option. `allow_writes: false` bleibt für jedes Gerät gesetzt (die Option hat in dieser Version ohnehin keine Wirkung). Kein FC06/FC16, keine Schreibprobe, keine Befehls-/Steuerentitäten (die App veröffentlicht nur `sensor`/`binary_sensor`, abonniert nur die HA-Birth-Nachricht), keine Gateway- oder Logger-Adminaktion durch die App.

Auch „nur kurz“ ausgeschlossen: R80 OFF/ON, „No Battery“ R111, Parallelrollen/-anzahl R336/R337, ToU, Remote-Mode oder Sollwerte ändern; Master-/Slaveausfall simulieren; AC/Batterie/CAN trennen; Buchsenketten zwischen ModeBUS, Meter-485 und BMS ausprobieren. Alle Konfiguration von Gateway und Logger (Protokollmodus, IP, Polling, Upload, Neustart) ändert **nur du selbst**; die App ändert nichts daran. Eine getrennt begründete Änderung an deiner Anlage ist **nicht Teil dieses Lesetests**.

## 10. Ergebnisblatt und Belege

Pro Stufe notieren: Datum/Uhrzeit, freigegebener Commit/Image-Digest, HA-/Supervisor-Version, reale Endpoints/Unit-IDs/SNs, Auswahlrevision und Leseplan (Anzahl Sensoren/Blöcke laut Weboberfläche), andere aktive Poller, Dauer, Zahl erfolgreicher/fehlgeschlagener Zyklen, maximale Antwortzeit/Queue, R22/R24, gegebenenfalls R65/Faktor/Energieserie, Cloud-Lücken, neue Fehler sowie Zeitpunkt des App-Stopps. Unbekannte Werte als **nicht geprüft** markieren. Zugangsdaten nicht ins Ergebnisblatt aufnehmen.

**Bestanden** heißt nur: Diese konkrete Version/Lesemenge funktionierte über die beobachtete Zeit auf den geprüften Wegen ohne neue Auffälligkeiten. Es beweist weder Schreibbarkeit noch unterbrechungsfreien Slave-/Masterwechsel, maximale Pollrate oder die Gleichwertigkeit aller CON-Buchsen.

Grundlagen im Repository: `docs/SPEC.md`, `deye_monitor/DOCS.md` (Optionen, Weboberfläche, Zustandsdateien), `deye_monitor/config.yaml` (Optionsschema), `e2e/` (End-to-End-Abnahme mit echtem Home Assistant und Fake-Invertern; kein echtes Gerät).

## Anhang A – Migrationsweg über die App-Optionen (YAML)

Ältere Installationen und diese Beispiele tragen Verbindungen und Wechselrichter noch in den **App-Optionen** (Einstellungen → Apps → Deye Monitor → Konfiguration). Beim Start übernimmt die App sie **einmalig** in die Weboberfläche, **nur wenn dort noch keine Anlage gespeichert ist**; die Oberfläche weist darauf hin („Verbindungen und Wechselrichter wurden … übernommen. Bitte entferne sie aus den App-Optionen“). Danach gilt ausschließlich, was in der Weboberfläche steht; abweichende Einträge in den Optionen werden nur gemeldet und nie übernommen. Neue Anlagen richtest du im Assistenten ein.

Die folgenden YAML-Beispiele sind vollständige **Supervisor-Optionen**, keine `config.yaml`-App-Manifeste. Alle IPs aus `192.0.2.0/24`, Seriennummern und Namen sind erfundene Beispiele: durch die oben bestätigten Werte ersetzen. Die Sensorliste steht **nicht** im YAML (das Optionsschema kennt keine); sie liegt in der Weboberfläche. Die Beispiele allein begrenzen also keine Auswahl.

Hinweise zu den Optionen: Das Schema verlangt für `max_read_registers` mindestens 6 (die RTU-Identitätsprobe liest bis zu sechs Register), `mqtt: {}` muss als Block stehen bleiben, die Intervalle sind flache Optionen `poll_*_s` je Inverter. `allow_writes` ist eine Reserve ohne Wirkung (auf `false` lassen; `true` wird nur geloggt, es gibt keine Schreibschicht). TLS-Optionen (`tls`, `tls_ca_file`, …) betreffen nur einen externen Broker und sind für den Supervisor-Dienst `mqtt: {}` nicht nötig.

**Stufe 1 – ein Inverter am Gateway** (nur für einen bestätigten **Modbus-TCP→RTU**-Gateway-Modus; bei einem transparenten Gateway `transport: rtu_over_tcp`):

```yaml
instance_id: deyetest1
endpoints:
  - id: gw1
    transport: modbus_tcp
    host: 192.0.2.11
    port: 502
    timeout_s: 5
    max_read_registers: 20
    request_gap_ms: 250
    queue_size: 8
inverters:
  - id: deye1
    name: Deye 1
    endpoint: gw1
    unit_id: 1
    expected_serial: "2401230001"
    profile: sg04lp3_lv_v105_8
    energy_resolution: "auto"
    poll_fast_s: 30
    poll_energy_s: 300
    poll_settings_s: 600
    poll_identity_s: 3600
    allow_writes: false
mqtt: {}
log_level: info
```

**Stufe 2 – derselbe Inverter über den Logger:**

```yaml
instance_id: deyetest1
endpoints:
  - id: log1
    transport: solarman_v5_tcp
    host: 192.0.2.21
    port: 8899
    logger_serial: 1700000001
    timeout_s: 5
    max_read_registers: 20
    request_gap_ms: 250
    queue_size: 8
inverters:
  - id: deye1
    name: Deye 1
    endpoint: log1
    unit_id: 1
    expected_serial: "2401230001"
    profile: sg04lp3_lv_v105_8
    energy_resolution: "auto"
    poll_fast_s: 30
    poll_energy_s: 300
    poll_settings_s: 600
    poll_identity_s: 3600
    allow_writes: false
mqtt: {}
log_level: info
```

**Stufe 3 – vier separate Gateways mit je Unit-ID 1:**

```yaml
instance_id: deyetest1
endpoints:
  - id: gw1
    transport: modbus_tcp
    host: 192.0.2.11
    port: 502
    timeout_s: 5
    max_read_registers: 20
    request_gap_ms: 250
    queue_size: 8
  - id: gw2
    transport: modbus_tcp
    host: 192.0.2.12
    port: 502
    timeout_s: 5
    max_read_registers: 20
    request_gap_ms: 250
    queue_size: 8
  - id: gw3
    transport: modbus_tcp
    host: 192.0.2.13
    port: 502
    timeout_s: 5
    max_read_registers: 20
    request_gap_ms: 250
    queue_size: 8
  - id: gw4
    transport: modbus_tcp
    host: 192.0.2.14
    port: 502
    timeout_s: 5
    max_read_registers: 20
    request_gap_ms: 250
    queue_size: 8
inverters:
  - id: deye1
    name: Deye 1
    endpoint: gw1
    unit_id: 1
    expected_serial: "2401230001"
    profile: sg04lp3_lv_v105_8
    energy_resolution: "auto"
    poll_fast_s: 30
    poll_energy_s: 300
    poll_settings_s: 600
    poll_identity_s: 3600
    allow_writes: false
  - id: deye2
    name: Deye 2
    endpoint: gw2
    unit_id: 1
    expected_serial: "2401230002"
    profile: sg04lp3_lv_v105_8
    energy_resolution: "auto"
    poll_fast_s: 30
    poll_energy_s: 300
    poll_settings_s: 600
    poll_identity_s: 3600
    allow_writes: false
  - id: deye3
    name: Deye 3
    endpoint: gw3
    unit_id: 1
    expected_serial: "2401230003"
    profile: sg04lp3_lv_v105_8
    energy_resolution: "auto"
    poll_fast_s: 30
    poll_energy_s: 300
    poll_settings_s: 600
    poll_identity_s: 3600
    allow_writes: false
  - id: deye4
    name: Deye 4
    endpoint: gw4
    unit_id: 1
    expected_serial: "2401230004"
    profile: sg04lp3_lv_v105_8
    energy_resolution: "auto"
    poll_fast_s: 30
    poll_energy_s: 300
    poll_settings_s: 600
    poll_identity_s: 3600
    allow_writes: false
mqtt: {}
log_level: info
```
