"""Help texts behind the (i) symbol: the longer "what does it do / trade-off / when to change"
explanation of a write setting or a sensor, in German and English.

Every statement comes from the firmware and protocol research of the knowledge base (contracts,
switches, parallel-sync and fixed-value findings); where the evidence is open the text says "not
proved". The texts are generic for every installation. They never replace the short description or
the risk text, which stay visible.
"""

from __future__ import annotations

from typing import Final

# key -> (German, English)
WRITE_HELP: Final[dict[str, tuple[str, str]]] = {
    "battery_charge_current": (
        "Begrenzt den Strom, mit dem der Wechselrichter die Batterie lädt. Ein niedrigerer "
        "Wert lädt schonender; 0 kann das Laden ganz stoppen. Bei gültigem, "
        "kommunizierendem BMS-Pfad gelten dessen Vorgaben zusätzlich, ebenso der "
        "Betriebszustand. Den passenden Wert nennt das Datenblatt des Batterieherstellers.",
        "Limits the current the inverter charges the battery with. A lower value charges "
        "more gently; 0 can stop charging completely. With a valid, communicating BMS path "
        "its limits apply on top, as does the operating state. The battery maker's data "
        "sheet gives the right value.",
    ),
    "battery_discharge_current": (
        "Begrenzt den Strom, den der Wechselrichter der Batterie entnimmt. Ein niedriger "
        "Wert schont die Batterie, kann aber im Inselbetrieb die Versorgung schwächen, "
        "weil weniger Leistung bereitsteht. Den passenden Wert nennt das Datenblatt des "
        "Batterieherstellers.",
        "Limits the current the inverter draws from the battery. A low value is gentler "
        "on the battery but can weaken the supply in island operation because less power "
        "is available. The battery maker's data sheet gives the right value.",
    ),
    "battery_shutdown_soc": (
        "Fällt der Ladezustand unter diesen Wert, schaltet der Wechselrichter die "
        "Batterie ab. Das ist mehr als eine Warnung: Im Inselbetrieb endet dann die "
        "Versorgung. Die Reihenfolge Abschalt- < Warn- < Neustart-SOC muss eingehalten "
        "werden. Wirksam nur, wenn der Batterie-Betriebsmodus auf SOC steht (R111 = 1).",
        "If the state of charge falls below this value, the inverter shuts the battery "
        "down. This is more than a warning: in island operation the supply ends. The "
        "order shutdown < low < restart SOC must hold. Effective only while the battery "
        "control mode is SOC (R111 = 1).",
    ),
    "battery_low_soc": (
        "Ab diesem Ladezustand behandelt der Wechselrichter die Batterie als fast leer. "
        "Er liegt zwischen Abschalt- und Neustart-SOC. Wirksam nur, wenn der "
        "Batterie-Betriebsmodus auf SOC steht (R111 = 1).",
        "From this state of charge the inverter treats the battery as nearly empty. It "
        "lies between the shutdown and the restart SOC. Effective only while the battery "
        "control mode is SOC (R111 = 1).",
    ),
    "battery_restart_soc": (
        "Ab diesem Ladezustand startet der Wechselrichter die Batterie nach einer "
        "Abschaltung wieder. Ein Wert deutlich über dem Abschalt-SOC verhindert ständiges "
        "Ein- und Ausschalten. Wirksam nur, wenn der Batterie-Betriebsmodus auf SOC steht "
        "(R111 = 1).",
        "From this state of charge the inverter starts the battery again after a "
        "shutdown. A value well above the shutdown SOC avoids constant switching on and "
        "off. Effective only while the battery control mode is SOC (R111 = 1).",
    ),
    "battery_shutdown_voltage": (
        "Fällt die Batteriespannung unter diesen Wert, schaltet der Wechselrichter die "
        "Batterie ab. Ein zu niedriger Wert kann die Batterie tiefentladen; einen "
        "allgemein sicheren Wert gibt es nicht, maßgeblich ist der Batteriehersteller. "
        "Wirksam nur im Spannungsmodus (R111 = 0).",
        "If the battery voltage falls below this value, the inverter shuts the battery "
        "down. A value that is too low can deep-discharge the battery; there is no "
        "universally safe value, the battery maker decides. Effective only in voltage "
        "mode (R111 = 0).",
    ),
    "battery_low_voltage": (
        "Ab dieser Spannung behandelt der Wechselrichter die Batterie als fast leer. Sie "
        "liegt zwischen Abschalt- und Neustartspannung. Wirksam nur im Spannungsmodus "
        "(R111 = 0).",
        "From this voltage the inverter treats the battery as nearly empty. It lies "
        "between the shutdown and the restart voltage. Effective only in voltage mode "
        "(R111 = 0).",
    ),
    "battery_restart_voltage": (
        "Ab dieser Spannung startet der Wechselrichter die Batterie nach einer "
        "Abschaltung wieder. Ein Wert deutlich über der Abschaltspannung verhindert "
        "ständiges Ein- und Ausschalten. Wirksam nur im Spannungsmodus (R111 = 0).",
        "From this voltage the inverter starts the battery again after a shutdown. A "
        "value well above the shutdown voltage avoids constant switching on and off. "
        "Effective only in voltage mode (R111 = 0).",
    ),
    "battery_float_voltage": (
        "Float-Spannung (Erhaltungsladung), mit der die Batterie nach dem "
        "Vollladen gehalten wird. Eine zu hohe Spannung kann die Batterie beschädigen. "
        "Üblich ist Float ≤ Absorption ≤ Ausgleich. Gilt nur für die Blei-/interne "
        "Ladekennlinie: Mit Batterietyp R98 = 1 und gültigem BMS-Zustand überspringt die "
        "Firmware diese Kennlinie. Die zulässigen Werte nennt der Batteriehersteller.",
        "Target voltage of the float stage that holds the battery after it is full. A "
        "voltage that is too high can damage the battery. The usual order is float ≤ "
        "absorption ≤ equalization. Applies to the lead / internal charge curve only: "
        "with battery type R98 = 1 and a valid BMS state the firmware skips this curve. "
        "The battery maker gives the permitted values.",
    ),
    "battery_absorption_voltage": (
        "Zielspannung der Absorptionsphase, in der die Batterie nach der Hauptladung bis "
        "voll nachgeladen wird. Eine zu hohe Spannung kann die Batterie beschädigen. "
        "Üblich ist Float ≤ Absorption ≤ Ausgleich. Gilt nur für die Blei-/interne "
        "Ladekennlinie: Mit Batterietyp R98 = 1 und gültigem BMS-Zustand überspringt die "
        "Firmware diese Kennlinie. Die zulässigen Werte nennt der Batteriehersteller.",
        "Target voltage of the absorption stage that tops the battery up after the main "
        "charge. A voltage that is too high can damage the battery. The usual order is "
        "float ≤ absorption ≤ equalization. Applies to the lead / internal charge curve "
        "only: with battery type R98 = 1 and a valid BMS state the firmware skips this "
        "curve. The battery maker gives the permitted values.",
    ),
    "battery_equalization_voltage": (
        "Zielspannung der Ausgleichsladung, die bei geeigneten Blei-Batterien die Zellen "
        "angleicht. Eine zu hohe Spannung kann die Batterie beschädigen. Üblich ist Float "
        "≤ Absorption ≤ Ausgleich. Gilt nur für die Blei-/interne Ladekennlinie: Mit "
        "Batterietyp R98 = 1 und gültigem BMS-Zustand überspringt die Firmware diese "
        "Kennlinie. Die zulässigen Werte nennt der Batteriehersteller.",
        "Target voltage of the equalization charge that evens out the cells of suitable "
        "lead batteries. A voltage that is too high can damage the battery. The usual "
        "order is float ≤ absorption ≤ equalization. Applies to the lead / internal "
        "charge curve only: with battery type R98 = 1 and a valid BMS state the firmware "
        "skips this curve. The battery maker gives the permitted values.",
    ),
    "battery_equalization_interval": (
        "Abstand der Ausgleichsladungen in Tagen; 0 verhindert sie. Sinnvoll nur für "
        "geeignete Blei-Batterien. Gilt nur für die Blei-/interne Ladekennlinie: Mit "
        "Batterietyp R98 = 1 und gültigem BMS-Zustand überspringt die Firmware diese "
        "Kennlinie. Die zulässigen Werte nennt der Batteriehersteller.",
        "Interval between equalization charges in days; 0 inhibits them. Useful only for "
        "suitable lead batteries. Applies to the lead / internal charge curve only: with "
        "battery type R98 = 1 and a valid BMS state the firmware skips this curve. The "
        "battery maker gives the permitted values.",
    ),
    "battery_equalization_duration": (
        "Dauer einer Ausgleichsladung in Stunden (in Schritten von 0,5 h); 0 verhindert "
        "sie. Angezeigt wird der am Gerät eingestellte Wert, keine gemessene Dauer. Gilt "
        "nur für die Blei-/interne Ladekennlinie: Mit Batterietyp R98 = 1 und gültigem "
        "BMS-Zustand überspringt die Firmware diese Kennlinie. Die zulässigen Werte nennt "
        "der Batteriehersteller.",
        "Duration of an equalization charge in hours (in steps of 0.5 h); 0 inhibits it. "
        "The value shown is the one set on the device, not a measured duration. Applies "
        "to the lead / internal charge curve only: with battery type R98 = 1 and a valid "
        "BMS state the firmware skips this curve. The battery maker gives the permitted "
        "values.",
    ),
    "battery_temperature_compensation": (
        "Korrigiert die Ladespannung je Grad Celsius Batterietemperatur und je Zelle. Die "
        "Firmware begrenzt die Gesamtkorrektur auf ±2,00 V. Die Zellenzahl kennt die App "
        "nicht; eine falsche Kompensation verschiebt die Ladespannung. Gilt nur für die "
        "Blei-/interne Ladekennlinie: Mit Batterietyp R98 = 1 und gültigem BMS-Zustand "
        "überspringt die Firmware diese Kennlinie. Die zulässigen Werte nennt der "
        "Batteriehersteller.",
        "Corrects the charge voltage per degree Celsius of battery temperature and per "
        "cell. The firmware limits the total correction to ±2.00 V. The app does not know "
        "the cell count; a wrong compensation shifts the charge voltage. Applies to the "
        "lead / internal charge curve only: with battery type R98 = 1 and a valid BMS "
        "state the firmware skips this curve. The battery maker gives the permitted "
        "values.",
    ),
    "battery_capacity": (
        "Kapazität, die der interne Ladezustandsschätzer des Wechselrichters verwendet. "
        "Ein falscher Wert verfälscht die SOC-Anzeige und damit jede SOC-Schwelle; mit "
        "einem BMS-SOC kann die Wirkung vom Pfad abhängen. In einer Parallelanlage gilt "
        "der Wert für alle Geräte.",
        "Capacity the inverter's internal state-of-charge estimator uses. A wrong value "
        "skews the SOC display and with it every SOC threshold; with a BMS SOC the effect "
        "can depend on the path. In a parallel plant the value applies to all units.",
    ),
    "battery_empty_voltage": (
        "Spannung, die die interne Spannungs-/SOC-Kennlinie als leer ansieht (41,0 bis "
        "52,0 V). Beim Start setzt die Firmware Werte außerhalb dieses Bereichs auf 45,0 "
        "V zurück. Die App kennt weder Zellenzahl noch Chemie; maßgeblich ist der "
        "Batteriehersteller.",
        "Voltage the internal voltage / SOC curve treats as empty (41.0 to 52.0 V). At "
        "start the firmware resets values outside that range to 45.0 V. The app knows "
        "neither cell count nor chemistry; the battery maker decides.",
    ),
    "battery_activation": (
        "EIN aktiviert den Versuch des Wechselrichters, eine schlafende oder getrennte "
        "Batterie über seine Aktivierungsfunktion zu wecken. Ob und wie schnell das "
        "gelingt, ist nicht zugesagt und hängt von Spannung und Betriebszustand ab.",
        "ON enables the inverter's attempt to wake a sleeping or disconnected battery "
        "through its activation function. Success, and how fast, is not guaranteed and "
        "depends on voltage and operating state.",
    ),
    "battery_internal_resistance": (
        "Innenwiderstand für die Spannungskompensation (1 bis 200 mΩ, der Bereich der "
        "Geräteanzeige). Ein falscher Wert verschiebt die Lade- und Entladespannungen. "
        "Der App liegt keine Messung vor.",
        "Internal resistance used for the voltage compensation (1 to 200 mΩ, the range of "
        "the device display). A wrong value shifts the charge and discharge voltages. The "
        "app has no measurement of it.",
    ),
    "battery_charge_efficiency": (
        "Wirkungsgradfaktor des internen Ladezustandsschätzers. Ein falscher Wert "
        "verfälscht die SOC-Schätzung. Gesperrt, solange die Kapazität nicht positiv ist. "
        "Der Bereich ist eine Vorgabe der App, keine Firmwaregrenze.",
        "Efficiency factor of the internal state-of-charge estimator. A wrong value skews "
        "the SOC estimate. Locked while the capacity is not positive. The range is a rule "
        "of this app, not a firmware limit.",
    ),
    "grid_charge_enable": (
        "Schaltet das Laden der Batterie aus dem Netz. Die Firmware vergleicht das ganze "
        "Register mit 1. Ob tatsächlich geladen wird, hängt zusätzlich von den "
        "Startschwellen und vom Betriebszustand ab. Mit EIN kann Netzstrom in die "
        "Batterie fließen.",
        "Switches charging the battery from the grid. The firmware compares the whole "
        "register with 1. Whether it actually charges also depends on the start "
        "thresholds and the operating state. With ON, grid power can flow into the "
        "battery.",
    ),
    "grid_charge_start_soc": (
        "Fällt der Ladezustand unter diesen Wert (10 bis 90 %), beginnt das Laden aus dem "
        "Netz, sofern es freigegeben ist. Wirksam nur im SOC-Modus (R111 = 1).",
        "If the state of charge falls below this value (10 to 90 %), charging from the "
        "grid begins, provided it is enabled. Effective only in SOC mode (R111 = 1).",
    ),
    "grid_charge_start_voltage": (
        "Fällt die Batteriespannung unter diesen Wert, beginnt das Laden aus dem Netz, "
        "sofern es freigegeben ist. Der Wert muss mindestens 2,0 V unter der "
        "Float-Spannung liegen. Wirksam nur im Spannungsmodus (R111 = 0).",
        "If the battery voltage falls below this value, charging from the grid begins, "
        "provided it is enabled. The value must be at least 2.0 V below the float "
        "voltage. Effective only in voltage mode (R111 = 0).",
    ),
    "grid_charge_current": (
        "Ladestrom beim Laden aus dem Netz. Er wirkt nur, solange das Netzladen aktiv "
        "ist; ein eingestellter, aber gerade inaktiver Wert kann später wirksam werden.",
        "Charge current when charging from the grid. It only has an effect while grid "
        "charging is active; a value set while inactive can take effect later.",
    ),
    "generator_charge_current": (
        "Ladestrom beim Laden aus einem Generator; sonst gilt der Netz-Ladestrom (R128). "
        "Es ist nicht belegt, dass ein am Netzanschluss hängender Generator dieses Feld "
        "nutzt.",
        "Charge current when charging from a generator; otherwise the grid charge current "
        "(R128) applies. It is not established that a generator on the GRID port uses "
        "this field.",
    ),
    "generator_charge_enable": (
        "Erlaubt das Laden aus dem Generator und das externe Startverhalten, wenn die "
        "übrigen Bedingungen erfüllt sind. Es ist eine Erlaubnis, keine Zusage, dass der "
        "Generator läuft; Kontakt und Polarität lassen sich aus dem Wort nicht ableiten. "
        "Nur mit dem GEN-Anschluss als Generator (R133 = 0).",
        "Permits charging from the generator and the external start behaviour when the "
        "other conditions hold. It is a permission, not a promise that the generator "
        "runs; contact and polarity cannot be derived from the word. Only with the GEN "
        "port configured as a generator (R133 = 0).",
    ),
    "generator_charge_start_voltage": (
        "Fällt die Batteriespannung unter diesen Wert, fordert der Spannungsmodus (R111 = "
        "0) das Laden aus dem Generator an. Laden setzt zusätzlich die Freigabe R129 = 1 "
        "und eine verfügbare Quelle voraus. Nur mit dem GEN-Anschluss als Generator (R133 "
        "= 0).",
        "If the battery voltage falls below this value, voltage mode (R111 = 0) requests "
        "charging from the generator. Charging also needs the permission R129 = 1 and an "
        "available source. Only with the GEN port configured as a generator (R133 = 0).",
    ),
    "generator_charge_soc": (
        "Fällt der Ladezustand unter diesen Wert (10 bis 90 %), fordert der SOC-Modus "
        "(R111 = 1) das Laden aus dem Generator an. Laden setzt zusätzlich die Freigabe "
        "R129 = 1 voraus. Nur mit dem GEN-Anschluss als Generator (R133 = 0).",
        "If the state of charge falls below this value (10 to 90 %), SOC mode (R111 = 1) "
        "requests charging from the generator. Charging also needs the permission R129 = "
        "1. Only with the GEN port configured as a generator (R133 = 0).",
    ),
    "generator_max_run_time": (
        "Laufzeitgrenze des Generators in der Ladeentscheidung der Firmware (0 bis 24 h): "
        "Ist sie erreicht, schließt die Firmware ihre Generator-Freigabe. Zu kurz kann "
        "wiederholte Starts auslösen oder das Laden beenden, zu lang kann den Generator "
        "überfordern. Eine Uhrkalibrierung gibt es nicht. Nur mit dem GEN-Anschluss als "
        "Generator (R133 = 0).",
        "Run-time limit of the generator in the firmware's charge decision (0 to 24 h): "
        "when it is reached, the firmware closes its generator gate. Too short can cause "
        "repeated starts or end charging, too long can overrun the generator. No clock "
        "calibration is available. Only with the GEN port configured as a generator (R133 "
        "= 0).",
    ),
    "generator_cooling_time": (
        "Abkühlzeit, die der Generator nach einem Lauf ruhen soll, bevor er wieder "
        "starten darf (0 bis 24 h). Zu kurz kann wiederholte Starts auslösen, zu lang "
        "hält den Generator unnötig still. Eine Uhrkalibrierung gibt es nicht. Nur mit "
        "dem GEN-Anschluss als Generator (R133 = 0).",
        "Cooling time the generator should rest after a run before it may start again (0 "
        "to 24 h). Too short can cause repeated starts, too long keeps the generator idle "
        "needlessly. No clock calibration is available. Only with the GEN port configured "
        "as a generator (R133 = 0).",
    ),
    "gen_port_usage": (
        "Legt fest, wofür der GEN-Anschluss dient: Generator, Smart Load (schaltbarer "
        "Verbraucher) oder AC-Kopplung (Fremdquelle wie ein weiterer PV-Wechselrichter). "
        "Falsche Verdrahtung oder ein Wechsel im Betrieb kann die Versorgung unterbrechen "
        "oder unverträgliche Quellen verbinden; die Firmware setzt bei einem Wechsel den "
        "Relaiszustand zurück. Die Folgeschritte des Gerätemenüs führt die App nicht aus.",
        "Sets what the GEN port is used for: generator, Smart Load (a switchable load) or "
        "AC coupling (an external source such as another PV inverter). Wrong wiring or a "
        "change during operation can interrupt the supply or connect incompatible "
        "sources; the firmware resets the relay state on a change. The follow-up steps of "
        "the device menu are not carried out by the app.",
    ),
    "smart_load_force": (
        "EIN fordert den erzwungenen Smart-Load-Betrieb an; die Wirkung hängt von den "
        "aktuellen Bedingungen ab. Die Firmware löscht den Wert beim Start und nach einem "
        "internen Timer selbst, es gibt keinen gehaltenen Zustand und keine zugesagte "
        "Dauer. Nur mit dem GEN-Anschluss als Smart Load (R133 = 1) und einem passenden "
        "angeschlossenen Verbraucher.",
        "ON requests forced Smart Load operation; the effect depends on current conditions."
        " The firmware clears the value itself at start and after an internal timer, so "
        "there is no held state and no promised duration. Only with the GEN port configured"
        " as Smart Load (R133 = 1) and a matching connected load.",
    ),
    "smart_load_on_voltage": (
        "Schwelle EIN für die Batteriespannung am GEN-Anschluss. Smart Load: EIN muss um "
        "mindestens 0,5 V (SOC: 5 Punkte) über AUS liegen, bei Erreichen wird das "
        "Einschalten angefordert. Bei AC-Kopplung umgekehrt (AUS ≥ EIN + Abstand). Nur eine"
        " Anforderung, kein zugesagtes Schalten (weitere Bedingungen, Relais nur "
        "abgeleitet). Nur mit passendem Modus (R111: Spannung = 0, SOC = 1).",
        "ON threshold for battery voltage on the GEN port. Smart Load: ON must be at least "
        "0.5 V (SOC: 5 points) above OFF; reaching it requests switching on. With AC "
        "coupling the other way round (OFF ≥ ON + margin). Only a request, not a guaranteed"
        " switch (other conditions, relay only inferred). Only with the matching mode "
        "(R111: voltage = 0, SOC = 1).",
    ),
    "smart_load_off_voltage": (
        "Schwelle AUS für die Batteriespannung am GEN-Anschluss. Smart Load: Fällt der Wert"
        " darauf, wird das Ausschalten angefordert (EIN ≥ AUS + 0,5 V bzw. 5 Punkte); bei "
        "AC-Kopplung ist es umgekehrt (AUS ≥ EIN + Abstand). Nur eine Anforderung, kein "
        "zugesagtes Schalten (weitere Bedingungen, Relais nur abgeleitet). Nur mit "
        "passendem Modus (R111: Spannung = 0, SOC = 1).",
        "OFF threshold for battery voltage on the GEN port. Smart Load: when the value "
        "falls to it, switching off is requested (ON ≥ OFF + 0.5 V or 5 points); with AC "
        "coupling it is the other way round (OFF ≥ ON + margin). Only a request, not a "
        "guaranteed switch (other conditions, relay only inferred). Only with the matching "
        "mode (R111: voltage = 0, SOC = 1).",
    ),
    "smart_load_on_soc": (
        "Schwelle EIN für den Ladezustand am GEN-Anschluss. Smart Load: EIN muss um "
        "mindestens 0,5 V (SOC: 5 Punkte) über AUS liegen, bei Erreichen wird das "
        "Einschalten angefordert. Bei AC-Kopplung umgekehrt (AUS ≥ EIN + Abstand). Nur eine"
        " Anforderung, kein zugesagtes Schalten (weitere Bedingungen, Relais nur "
        "abgeleitet). Nur mit passendem Modus (R111: Spannung = 0, SOC = 1).",
        "ON threshold for state of charge on the GEN port. Smart Load: ON must be at least "
        "0.5 V (SOC: 5 points) above OFF; reaching it requests switching on. With AC "
        "coupling the other way round (OFF ≥ ON + margin). Only a request, not a guaranteed"
        " switch (other conditions, relay only inferred). Only with the matching mode "
        "(R111: voltage = 0, SOC = 1).",
    ),
    "smart_load_off_soc": (
        "Schwelle AUS für den Ladezustand am GEN-Anschluss. Smart Load: Fällt der Wert "
        "darauf, wird das Ausschalten angefordert (EIN ≥ AUS + 0,5 V bzw. 5 Punkte); bei "
        "AC-Kopplung ist es umgekehrt (AUS ≥ EIN + Abstand). Nur eine Anforderung, kein "
        "zugesagtes Schalten (weitere Bedingungen, Relais nur abgeleitet). Nur mit "
        "passendem Modus (R111: Spannung = 0, SOC = 1).",
        "OFF threshold for state of charge on the GEN port. Smart Load: when the value "
        "falls to it, switching off is requested (ON ≥ OFF + 0.5 V or 5 points); with AC "
        "coupling it is the other way round (OFF ≥ ON + margin). Only a request, not a "
        "guaranteed switch (other conditions, relay only inferred). Only with the matching "
        "mode (R111: voltage = 0, SOC = 1).",
    ),
    "ac_coupling_upper_frequency": (
        "Bei AC-Kopplung am GEN-Anschluss (R133 = 2) hebt der Wechselrichter die "
        "Ausgangsfrequenz bis zu dieser Grenze an, damit gekoppelte Mikro-Wechselrichter "
        "ihre Leistung drosseln. Nicht dafür ausgelegte Geräte können sich abschalten "
        "oder außerhalb ihres Frequenzbereichs laufen. Gesperrt, solange die Netzfrequenz "
        "(50/60 Hz) unbekannt ist.",
        "With AC coupling on the GEN port (R133 = 2) the inverter raises its output "
        "frequency up to this limit so that coupled microinverters curtail their power. "
        "Devices not designed for it can disconnect or run outside their frequency range. "
        "Locked while the nominal frequency (50/60 Hz) is unknown.",
    ),
    "on_grid_smart_load_always_on": (
        "EIN hält den Smart-Load-Ausgang im Netzbetrieb dauerhaft eingeschaltet, "
        "unabhängig von den Schwellen. Der Verbraucher ist dann nicht mehr durch die "
        "Batterieschwellen geschützt. Gilt für Anlagen mit GEN-Anschluss als Smart Load.",
        "ON keeps the Smart Load output switched on permanently while on-grid, regardless "
        "of the thresholds. The load is then no longer protected by the battery "
        "thresholds. Relevant for plants with the GEN port set to Smart Load.",
    ),
    "external_signal": (
        "Wählt, ob der Wechselrichter über den Trockenkontakt ein externes Signal "
        "ausgibt: Grid-Signal (Netzladung anfordern, zum Beispiel über ein Schütz) oder "
        "Gen-Signal (Generator starten). Beide teilen sich denselben Ausgang; ein "
        "Quellenwechsel kann einen angeschlossenen Schütz trotzdem lösen. Das Gen-Signal "
        "wirkt nur, wenn der GEN-Anschluss als Generator konfiguriert ist (R133 = 0).",
        "Chooses whether the inverter outputs an external signal on its dry contact: Grid "
        "Signal (request grid charging, for example through a contactor) or Gen Signal "
        "(start a generator). Both share the same output; switching the source can still "
        "release a connected contactor. Gen Signal works only while the GEN port is "
        "configured as a generator (R133 = 0).",
    ),
    "signal_island_mode": (
        "Beeinflusst die netzabhängige Relaisstrategie der Anlage. Die Geräteoberfläche "
        "nennt dasselbe Bitpaar „Signal Island Mode“, die Deye-Cloud „Neutral to earth "
        "bonding“. Im Inselbetrieb kann das die N-PE-Verbindung schließen; Erdung und "
        "Schutz verlangen eine geprüfte Verdrahtungsplanung. Eine Rückmeldung eines "
        "Kontakts oder der Verbindung gibt es nicht.",
        "Changes the grid-dependent relay policy of the plant. The device menu calls the "
        "same bit pair “Signal Island Mode”, the Deye cloud “Neutral to earth bonding”. "
        "In island operation this can close the N–PE bond; earthing and protection "
        "require a verified wiring design. There is no feedback of a contact or of the "
        "bond.",
    ),
    "force_off_grid": (
        "EIN erzwingt den Inselbetrieb, AUS („Auto“) lässt die normale Netzprüfung wieder "
        "zu. Solange EIN gesetzt ist, setzt die Firmware die Netzqualifizierung ständig "
        "zurück; ein angelegtes Netz wird dann nicht angenommen. Der Wechsel kann "
        "Quellen- und Relaisübergänge und die Inselversorgung beeinflussen.",
        "ON forces island operation, OFF (“Auto”) lets the normal grid qualification "
        "apply again. While ON, the firmware keeps resetting the grid qualification, so "
        "an applied grid is not accepted. Switching can affect source and relay "
        "transitions and the island supply.",
    ),
    "grid_reconnection_time": (
        "Wartezeit, bevor sich das Gerät nach einem Netzfehler wieder zuschaltet (10 bis "
        "900 s, in Schritten von 10 s). Netzgekoppelter Betrieb verlangt die "
        "Wiederzuschaltzeit des Netzcodes (zum Beispiel VDE-AR-N 4105); ein zu kurzer "
        "Wert kann zu früh zuschalten. Ein laufender Zähler wird nicht zurückgesetzt. "
        "„Inselbetrieb erzwingen“ (R179) wird zur Einordnung mitgelesen: bei R179 = 1 "
        "läuft die Wartezeit nicht an.",
        "Wait before the unit reconnects after a grid fault (10 to 900 s, in steps of 10 "
        "s). Grid-tied operation needs the reconnection delay of the grid code (for "
        "example VDE-AR-N 4105); a value that is too short can reconnect too early. A "
        "running counter is not reset. “Force off-grid” (R179) is read alongside for "
        "context: with R179 = 1 the delay does not start.",
    ),
    "external_ct_ratio": (
        "Übersetzungsverhältnis N:1 des externen Stromwandlers (CT). Ein falsches "
        "Verhältnis verfälscht die Messung und kann die Nullspeisung irreführen, wo der "
        "CT-Pfad den Wert nutzt; der endgültige Verbraucher in der Firmware ist nicht "
        "belegt. Mit einem digitalen Zähler wird der Wert nur gespeichert. Der "
        "konfigurierte Messweg (R344) wird mitgelesen und angezeigt.",
        "Turns ratio N:1 of the external current transformer (CT). A wrong ratio "
        "falsifies the measurement and can mislead zero-export control where the CT path "
        "uses the value; the final consumer in the firmware is not established. With a "
        "digital meter the value is only stored. The configured measurement path (R344) "
        "is read alongside and shown.",
    ),
    "energy_pattern": (
        "Wählt das Energiemuster der Regelung: „Batterie zuerst“ (0) lädt mit PV-Überschuss "
        "zuerst die Batterie, „Last zuerst“ (1) versorgt zuerst die Last. Das verändert "
        "Strom-, Spannungs- und SOC-Regelzweige. Die Firmware kann den Wert bei aktiver "
        "Peak Shaving selbst auf 1 setzen; die App kämpft nicht dagegen. Gilt für die "
        "ganze Parallelanlage (vom Master kopiert); der gelesene Wert ist die Einstellung, "
        "kein Nachweis der Wirkung.",
        "Selects the energy pattern of the control: “Batt First” (0) charges the battery "
        "first from PV surplus, “Load First” (1) supplies the load first. This changes the "
        "current, voltage and SOC regulation branches. The firmware can set the value to 1 "
        "itself while peak shaving is active; the app does not fight it. Applies to the "
        "whole parallel plant (copied from the master); the value read is the setting, not "
        "proof of the effect.",
    ),
    "bms_loss_fault": (
        "BMS Err_Stop: Bei qualifiziertem BMS-Verlust kann EIN den Stopp bzw. Reset "
        "auslösen – das schützt die Batterie, ein kurzer Kommunikationsfehler kann aber die"
        " Versorgung unterbrechen. AUS unterdrückt diesen Pfad und kann den Weiterbetrieb "
        "mit der internen Batteriekennlinie ohne aktuelle BMS-Vorgaben erlauben; andere "
        "Grenzen und Stopps bleiben. AUS löscht keinen gespeicherten Fehler und startet "
        "nicht neu. Diese Abwägung kann nur der Betreiber treffen; nur bei Batterietyp-Wort"
        " R98 = 1 (Lithium/BMS).",
        "BMS Err_Stop: with a qualified BMS loss, ON can invoke the stop or reset – this "
        "protects the battery, but a short communication fault can interrupt the supply. "
        "OFF suppresses that path and can allow running on the internal battery curve "
        "without current BMS limits; other limits and stops remain. OFF does not clear a "
        "latched fault or restart. This trade-off only the owner can weigh; only with the "
        "battery type word R98 = 1 (Lithium / BMS).",
    ),
    "drm_enable": (
        "Schaltet die Auswertung des DRM-Anschlusses, über den ein Netzbetreiber die "
        "Einspeisung drosseln kann. AUS ignoriert dort anliegende Befehle; ob das erlaubt "
        "ist, hängt von den örtlichen Vorgaben ab.",
        "Switches the evaluation of the DRM port, through which a grid operator can "
        "curtail feed-in. OFF ignores commands present there; whether that is permitted "
        "depends on local rules.",
    ),
    "afci_enable": (
        "Konfiguriertes AFCI (Lichtbogenerkennung auf der DC-Seite), sofern die Hardware "
        "verbaut ist; EIN oder Rücklesen beweist weder Modulzustand noch Schutzfunktion. "
        "AUS birgt das Risiko, dass Lichtbögen nicht erkannt werden. Der Schalter zeigt die"
        " Einstellung, nicht den Fehlerzustand; AUS löscht keinen gespeicherten Fehler. In "
        "dieser App sendet die Taste „Lichtbogenfehler löschen“ die einmalige "
        "Löschanforderung.",
        "Configured AFCI (DC-side arc-fault detection), where the hardware is fitted; ON or"
        " readback is not evidence of module health or a protection test. OFF risks arcs "
        "going undetected. The switch shows the setting, not the fault state; OFF clears no"
        " stored fault. Within this app the “Clear arc fault” button sends the one-shot "
        "request.",
    ),
    "afci_clear_fault": (
        "Sendet einmal den Löschbefehl an die Lichtbogenerkennung und nur, wenn AFCI "
        "bereits eingeschaltet ist. „Befehl angenommen“ und „Fehler weiter gemeldet“ sind "
        "getrennte Aussagen; ein gelöschter Bericht beweist nicht, dass die physische "
        "Ursache beseitigt ist.",
        "Sends the clear command to the arc-fault detection once, and only if AFCI is "
        "already on. “Command accepted” and “fault still reported” are separate "
        "statements; a cleared report does not prove that the physical cause is gone.",
    ),
    "low_noise": (
        "EIN wählt 20 kHz, AUS 15 kHz als Schaltfrequenz der Brücke – sie hat nichts mit "
        "der Lüfterdrehzahl zu tun, ein Geräuschwert wird nicht gemessen. Der Befehl "
        "setzt die Regelung des Geräts zurück; ob die Frequenz sofort oder erst nach dem "
        "nächsten Neustart wechselt und ob der Wechsel unterbrechungsfrei ist, ist nicht "
        "belegt.",
        "ON selects 20 kHz, OFF 15 kHz as the bridge switching frequency – it is not fan "
        "speed, and no noise level is measured. The command resets the control of the "
        "unit; whether the frequency changes at once or only at the next restart, and "
        "whether the change is uninterrupted, is not proved.",
    ),
    "bypass_fan_minimum_request": (
        "EIN unterdrückt unter bestimmten Betriebsbedingungen nur die "
        "Mindest-Lüfteranforderung; AUS stellt die normale Logik wieder her. Der "
        "Kühlbedarf wird nicht auf null gesetzt und die Lüfter nicht abgeschaltet; andere "
        "Anforderungen können sie weiter laufen lassen. Lüfterdrehzahl und thermische "
        "Reserve werden nicht gemessen.",
        "ON suppresses only the minimum fan request under certain operating conditions; "
        "OFF restores the normal logic. It neither zeroes the cooling demand nor switches "
        "the fans off; other requests can keep them running. Fan speed and thermal margin "
        "are not measured.",
    ),
    "mppt_scan": (
        "Schaltet das planmäßige Abtasten mehrerer Arbeitspunkte der PV-Eingänge "
        "(MPPT-Mehrpunkt-Scan, Bit 2 von R341). Es kann die Arbeitspunkte der Module "
        "ändern; die Aktivierung hängt vom Reglerzustand ab. Welchen Ertrag es bringt, "
        "ist nicht belegt.",
        "Switches the scheduled multi-point scanning of the PV inputs (MPPT scan, bit 2 "
        "of R341). It may change the module operating points; activation depends on the "
        "controller state. What it gains in yield is not proved.",
    ),
    "unbalanced_load": (
        "EIN schaltet die Behandlung ungleicher Phasenlast ein; die Leistungsgrenze je "
        "Phase wird dann anders abgeleitet. Einzelne Phasen können ihre Grenze vor der "
        "Gesamt-Nennleistung erreichen und Versorgung oder Schutz beeinflussen. "
        "Phasenströme und -leistungen müssen innerhalb der Grenzen von Modell und "
        "Parallelsystem bleiben.",
        "ON switches on the handling of unbalanced load; the per-phase power limit is "
        "then derived differently. Single phases can reach their limit before the total "
        "rated power and affect supply or protection. Phase currents and powers must stay "
        "inside the limits of the model and the parallel system.",
    ),
    "export_mode": (
        "Wählt zwischen Selling First, Zero Export to Load (interne Nullspeisung) und "
        "Zero Export to CT (externe Nullspeisung, Messung durch CT oder Zähler). Ein "
        "falscher Modus kann ohne "
        "Erlaubnis einspeisen oder unerwarteten Netzbezug und Batterieentladung "
        "verursachen. Vor dem Wechsel werden die dadurch wirksamen Einstellungen (R143, "
        "R145, eingeschaltete Spitzenkappung) frisch gelesen und geprüft, aber nie "
        "geändert. Zero Export to CT braucht einen beschriebenen, passend konfigurierten "
        "Messweg (R344) und keine aktive Zähler-Warnung; ob die Messung arbeitet, prüft "
        "die App nicht.",
        "Selects between Selling First, Zero Export to Load (internal zero-export) and "
        "Zero Export to CT (external zero-export, measured by a CT or meter). A wrong "
        "mode can export without permission or cause "
        "unexpected import and battery discharge. Before the change the settings that "
        "become active (R143, R145, enabled peak shaving) are read fresh and validated, "
        "but never changed. Zero Export to CT needs a described, matching configured "
        "measurement path (R344) and no active meter warning; whether the measurement "
        "works is not checked by the app.",
    ),
    "export_max_power": (
        "Obergrenze der Netzeinspeisung. Sie ersetzt keine Einspeisegenehmigung des "
        "Netzbetreibers und garantiert keine genau gemessene Leistung. Wirksam nur im "
        "Modus Selling First (R142 = 0). Der gelesene Wert ist die Einstellung, "
        "kein Nachweis der tatsächlich eingespeisten Leistung.",
        "Upper limit of the grid export. It does not replace an export permit of the grid "
        "operator and guarantees no exactly metered power. Effective only in the mode "
        "Selling First (R142 = 0). The value read is the setting, not proof of the "
        "power actually exported.",
    ),
    "pv_sale_enable": (
        "Solar Sell: EIN erlaubt den Verkauf von PV-Leistung, AUS blockiert ihn. Die "
        "Einstellung gilt in den Modi Zero Export to Load und Zero Export to CT (R142 = 1 "
        "oder 2); Selling First ist ein eigener Modus. Sie ist eine Erlaubnis, keine "
        "gemessene Einspeisung. EIN braucht zusätzlich eine gültige Einspeisegrenze R143 "
        "innerhalb der in der Anlagenbeschreibung eingetragenen Grenze.",
        "Solar Sell: ON permits the sale of PV power, OFF blocks it. The setting applies in"
        " the modes Zero Export to Load and Zero Export to CT (R142 = 1 or 2); Selling "
        "First is a separate mode. It is a permission, not measured export. ON additionally"
        " needs a valid export limit R143 within the export limit entered in the "
        "installation description.",
    ),
    "zero_export_bias": (
        "Mindestwert der Nullspeise-Regelung als Rohwert (20 bis 500, Bereich des "
        "Geräteeditors). Die physikalische Einheit und alle Moduswechsel sind nicht "
        "belegt, die App rechnet nichts um. Ein falscher Wert kann unerwarteten Bezug "
        "oder Einspeisung verursachen. Nur in den Nullspeise-Modi (R142 = 1 oder 2) mit "
        "in Betrieb genommener Messung.",
        "Minimum setting of the zero-export control as a raw value (20 to 500, the range "
        "of the device editor). The physical unit and every mode transition are not "
        "proved; the app converts nothing. A wrong value can cause unexpected import or "
        "export. Only in the zero-export modes (R142 = 1 or 2) with a commissioned "
        "measurement.",
    ),
    "pv_sale_max_power": (
        "Obergrenze der verkauften PV-Leistung. Bei kleinen Werten nutzt die Firmware "
        "eine modellabhängige Ersatzformel; die Roheinheit der Geräteanzeige muss nicht "
        "der internen 1-W-Größe entsprechen. Eine falsche Grenze kann weit mehr "
        "einspeisen oder weit mehr Erzeugung abregeln.",
        "Upper limit of the PV power sold. For small values the firmware uses a "
        "model-dependent fallback formula; the raw unit of the device display need not "
        "equal the internal 1 W quantity. A wrong limit can export far more or curtail "
        "far more generation.",
    ),
    "microinverter_export_cutoff": (
        "Schaltet die Abregelung gekoppelter Mikro-Wechselrichter bei Überschuss. Der "
        "gelesene Wert ist die konfigurierte Anforderung, kein Nachweis einer gemessenen "
        "Reaktion oder eines geprüften Schutzes.",
        "Switches the curtailment of coupled microinverters on surplus. The value read is "
        "the configured request, not proof of a measured response or of a verified "
        "protection.",
    ),
    "generator_peak_shaving": (
        "Schaltet die Spitzenkappung der Generatorquelle ein (Leistung R190). Die "
        "Firmware kann dabei selbst Energiemodus und ToU ändern. Einschalten setzt eine "
        "Leistung R190 innerhalb der Generatorleistung der Anlagenbeschreibung und eine "
        "eindeutige Generatorquelle am GEN-Anschluss voraus; Ausschalten geht immer.",
        "Switches peak shaving of the generator source on (power R190). The firmware may "
        "change the energy mode and ToU itself. Switching on needs a power R190 within the "
        "generator capacity of the installation description and an unambiguous generator "
        "source on the GEN port; switching off is always possible.",
    ),
    "grid_peak_shaving": (
        "Schaltet die Spitzenkappung des Netzanschlusses ein (Leistung R191). Die "
        "Firmware kann dabei selbst Energiemodus und ToU ändern. Einschalten setzt eine "
        "Leistung R191 innerhalb der Netzanschlussgrenze der Anlagenbeschreibung voraus; "
        "Ausschalten geht immer.",
        "Switches peak shaving of the grid connection on (power R191). The firmware may "
        "change the energy mode and ToU itself. Switching on needs a power R191 within the "
        "grid connection limit of the installation description; switching off is always "
        "possible.",
    ),
    "generator_peak_shaving_power": (
        "Leistungsgrenze der Generatorquelle für die Spitzenkappung. Ein falscher Wert "
        "kann die Batterie stärker entladen oder einen Generator überlasten. Wirksam nur "
        "bei eingeschalteter Spitzenkappung und einem beschriebenen Generator am "
        "GEN-Anschluss (R133 = 0 und Generator-Eingang R189 = 0); die Obergrenze ist die "
        "Nennleistung des Modells, "
        "höchstens die in der Anlagenbeschreibung eingetragene Generatorleistung (ohne "
        "Eintrag gesperrt).",
        "Source power limit for generator peak shaving. A wrong value can discharge the "
        "battery harder or overload a generator. Effective only with peak shaving "
        "enabled and a generator described on the GEN port (R133 = 0 and generator input "
        "R189 = 0); the upper limit is "
        "the model's rated power, at most the generator capacity entered in the "
        "installation description (locked without that entry).",
    ),
    "grid_peak_shaving_power": (
        "Leistungsgrenze des Netzanschlusses für die Spitzenkappung. Ein falscher Wert "
        "verändert Netzbezug und Batterienutzung. Wirksam nur bei eingeschalteter "
        "Spitzenkappung; die Obergrenze ist die Nennleistung des Modells, höchstens die "
        "in der Anlagenbeschreibung eingetragene Netzanschlussgrenze (ohne Eintrag "
        "gesperrt).",
        "Power limit of the grid connection for peak shaving. A wrong value changes "
        "import and battery use. Effective only with peak shaving enabled; the upper "
        "limit is the model's rated power, at most the grid connection limit entered in "
        "the installation description (locked without that entry).",
    ),
    "ev_free_work": (
        "EIN erlaubt den freien Arbeitsmodus der EV-Ladung, AUS nur Solarüberschuss. Ein "
        "falscher Wert ändert die Quelle der Ladeleistung (PV, Batterie, Netz) und kann "
        "gewollte Reserven umgehen. Gilt nur für das angesprochene Gerät; der Master "
        "kopiert dieses Register nicht.",
        "ON allows the free working mode of EV charging, OFF solar surplus only. A wrong "
        "value changes the source of the charging power (PV, battery, grid) and can "
        "bypass intended reserves. Applies to the addressed unit only; the master does "
        "not copy this register.",
    ),
    "ev_location": (
        "Wählt, ob das Ladegerät des Fahrzeugs am GRID- oder am LOAD-Anschluss hängt. Ein "
        "falscher Anschluss verändert Leistungsflüsse und die Grenze der Ladeleistung. "
        "Gilt nur für das angesprochene Gerät; der Master kopiert dieses Register nicht.",
        "Selects whether the vehicle charger hangs on the GRID or the LOAD port. A wrong "
        "port changes the power flows and the limit of the charging power. Applies to the "
        "addressed unit only; the master does not copy this register.",
    ),
    "ev_offgrid_soc": (
        "Ladezustand der Batterie (0 bis 100 % in 10-%-Schritten), ab dem im Inselbetrieb "
        "Energie für die EV-Ladung entnommen werden darf. Ein zu niedriger Wert kann die "
        "Reserve für die Hausversorgung aufbrauchen. Gilt nur für das angesprochene "
        "Gerät; der Master kopiert dieses Register nicht. Der Wert darf nicht unter der in der "
        "Anlagenbeschreibung eingetragenen EV-Reserve liegen (ohne Eintrag gesperrt) und "
        "zusätzlich nicht unter dem Niedrig-SOC der Batterie (R117).",
        "Battery state of charge (0 to 100 % in steps of 10 %) from which energy may be "
        "taken for EV charging in off-grid operation. A value that is too low can use up "
        "the reserve for the house supply. Applies to the addressed unit only; the master "
        "does not copy this register. The value may not be below the EV reserve entered in "
        "the installation description (locked without that entry) and additionally not "
        "below the battery low SOC (R117).",
    ),
    "ev_max_power": (
        "Obergrenze der EV-Ladeleistung. Ein zu hoher Wert kann Fahrzeugkreis oder "
        "Ladegerät überlasten oder unerwartet Netz- und Batterieleistung ziehen; die "
        "Obergrenze ist die in der Anlagenbeschreibung eingetragene Leistung von Ladegerät "
        "und Zuleitung (ohne Eintrag gesperrt). Frühere "
        "Parameterwerte garantieren keine gelieferte Leistung. Gilt nur für das "
        "angesprochene Gerät; der Master kopiert dieses Register nicht.",
        "Upper limit of the EV charging power. A value that is too high can overload the "
        "vehicle circuit or charger or draw unexpected grid and battery power; the upper "
        "limit is the charger / supply line rating entered in the installation "
        "description (locked without that entry). Historic "
        "parameter values do not guarantee delivered power. Applies to the addressed unit "
        "only; the master does not copy this register.",
    ),
    "tou_enable": (
        "Schaltet die zeitgesteuerte Batterieregelung (Time of Use) ein oder aus. Ist sie "
        "aus, werden die sechs Zeitfenster nicht ausgewertet. Die Firmware kann das Bit "
        "selbst setzen (zum Beispiel bei Spitzenkappung); der Schalter zeigt die gelesene "
        "Einstellung. Nicht im Spanien-Modus.",
        "Switches the time-of-use battery control on or off. When off, the six time slots "
        "are not evaluated. The firmware can set the bit itself (for example with peak "
        "shaving); the switch shows the configuration read. Not in Spain mode.",
    ),
    "tou_weekday_mon": (
        "Wochentagsfilter des Zeitplans: Der Zeitplan gilt nur an den eingeschalteten "
        "Tagen. Sind alle sieben Tage aus, gilt er täglich; die App zeigt dann alle Tage "
        "als EIN, und der letzte Tag kann nicht abgeschaltet werden. Die Zuordnung der "
        "Bits zu den Wochentagen stammt aus den Gerätetexten; eine falsche Geräteuhr "
        "verschiebt den Zeitplan.",
        "Weekday filter of the schedule: it applies only on the days switched on. With "
        "all seven days off it applies every day; the app then shows all days as ON, and "
        "the last day cannot be switched off. The bit-to-weekday mapping comes from the "
        "device labels; a wrong device clock shifts the schedule.",
    ),
    "tou_weekday_tue": (
        "Wochentagsfilter des Zeitplans: Der Zeitplan gilt nur an den eingeschalteten "
        "Tagen. Sind alle sieben Tage aus, gilt er täglich; die App zeigt dann alle Tage "
        "als EIN, und der letzte Tag kann nicht abgeschaltet werden. Die Zuordnung der "
        "Bits zu den Wochentagen stammt aus den Gerätetexten; eine falsche Geräteuhr "
        "verschiebt den Zeitplan.",
        "Weekday filter of the schedule: it applies only on the days switched on. With "
        "all seven days off it applies every day; the app then shows all days as ON, and "
        "the last day cannot be switched off. The bit-to-weekday mapping comes from the "
        "device labels; a wrong device clock shifts the schedule.",
    ),
    "tou_weekday_wed": (
        "Wochentagsfilter des Zeitplans: Der Zeitplan gilt nur an den eingeschalteten "
        "Tagen. Sind alle sieben Tage aus, gilt er täglich; die App zeigt dann alle Tage "
        "als EIN, und der letzte Tag kann nicht abgeschaltet werden. Die Zuordnung der "
        "Bits zu den Wochentagen stammt aus den Gerätetexten; eine falsche Geräteuhr "
        "verschiebt den Zeitplan.",
        "Weekday filter of the schedule: it applies only on the days switched on. With "
        "all seven days off it applies every day; the app then shows all days as ON, and "
        "the last day cannot be switched off. The bit-to-weekday mapping comes from the "
        "device labels; a wrong device clock shifts the schedule.",
    ),
    "tou_weekday_thu": (
        "Wochentagsfilter des Zeitplans: Der Zeitplan gilt nur an den eingeschalteten "
        "Tagen. Sind alle sieben Tage aus, gilt er täglich; die App zeigt dann alle Tage "
        "als EIN, und der letzte Tag kann nicht abgeschaltet werden. Die Zuordnung der "
        "Bits zu den Wochentagen stammt aus den Gerätetexten; eine falsche Geräteuhr "
        "verschiebt den Zeitplan.",
        "Weekday filter of the schedule: it applies only on the days switched on. With "
        "all seven days off it applies every day; the app then shows all days as ON, and "
        "the last day cannot be switched off. The bit-to-weekday mapping comes from the "
        "device labels; a wrong device clock shifts the schedule.",
    ),
    "tou_weekday_fri": (
        "Wochentagsfilter des Zeitplans: Der Zeitplan gilt nur an den eingeschalteten "
        "Tagen. Sind alle sieben Tage aus, gilt er täglich; die App zeigt dann alle Tage "
        "als EIN, und der letzte Tag kann nicht abgeschaltet werden. Die Zuordnung der "
        "Bits zu den Wochentagen stammt aus den Gerätetexten; eine falsche Geräteuhr "
        "verschiebt den Zeitplan.",
        "Weekday filter of the schedule: it applies only on the days switched on. With "
        "all seven days off it applies every day; the app then shows all days as ON, and "
        "the last day cannot be switched off. The bit-to-weekday mapping comes from the "
        "device labels; a wrong device clock shifts the schedule.",
    ),
    "tou_weekday_sat": (
        "Wochentagsfilter des Zeitplans: Der Zeitplan gilt nur an den eingeschalteten "
        "Tagen. Sind alle sieben Tage aus, gilt er täglich; die App zeigt dann alle Tage "
        "als EIN, und der letzte Tag kann nicht abgeschaltet werden. Die Zuordnung der "
        "Bits zu den Wochentagen stammt aus den Gerätetexten; eine falsche Geräteuhr "
        "verschiebt den Zeitplan.",
        "Weekday filter of the schedule: it applies only on the days switched on. With "
        "all seven days off it applies every day; the app then shows all days as ON, and "
        "the last day cannot be switched off. The bit-to-weekday mapping comes from the "
        "device labels; a wrong device clock shifts the schedule.",
    ),
    "tou_weekday_sun": (
        "Wochentagsfilter des Zeitplans: Der Zeitplan gilt nur an den eingeschalteten "
        "Tagen. Sind alle sieben Tage aus, gilt er täglich; die App zeigt dann alle Tage "
        "als EIN, und der letzte Tag kann nicht abgeschaltet werden. Die Zuordnung der "
        "Bits zu den Wochentagen stammt aus den Gerätetexten; eine falsche Geräteuhr "
        "verschiebt den Zeitplan.",
        "Weekday filter of the schedule: it applies only on the days switched on. With "
        "all seven days off it applies every day; the app then shows all days as ON, and "
        "the last day cannot be switched off. The bit-to-weekday mapping comes from the "
        "device labels; a wrong device clock shifts the schedule.",
    ),
    "tou_spain_mode": (
        "Schaltet die Zeitplan-Logik auf einen anderen Firmwarezweig um („Mode4“ im "
        "Gerätemenü): Leistung, Ziel-SOC und Netzladung der Intervalle werden dann aus "
        "den Spanien-Bits der Intervall-Flags abgeleitet. Das ist kein Nachweis der "
        "Konformität mit Vorschriften. Solange der Modus aktiv ist, sind die normalen "
        "Zeitplan-Einstellungen gesperrt.",
        "Switches the schedule logic to a different firmware branch (“Mode4” in the "
        "device menu): power, target SOC and grid charging of the slots are then derived "
        "from the Spain bits of the slot flags. This is no proof of regulatory "
        "compliance. While the mode is on, the normal schedule settings are locked.",
    ),
    "tou_slot1_start": (
        "Beginn von Zeitfenster 1 (HH:MM, in 5-Minuten-Schritten). Die Startzeiten müssen "
        "von Slot 1 bis 6 streng aufsteigen; Slot 6 läuft bis Slot 1 beginnt, auch über "
        "Mitternacht. Eine Änderung wirkt sofort auf die angrenzenden Intervalle; andere "
        "Slots ordnet die App nie um.",
        "Start of time slot 1 (HH:MM, in 5-minute steps). The start times must ascend "
        "strictly from slot 1 to 6; slot 6 runs until slot 1 starts, across midnight. A "
        "change affects the adjacent slots at once; the app never rearranges other slots.",
    ),
    "tou_slot1_power": (
        "Leistungsvorgabe von Zeitfenster 1 in W, bis zur Nennleistung des Modells. Es "
        "ist keine Zusage, dass diese Leistung tatsächlich geliefert wird.",
        "Power setting of time slot 1 in W, up to the model's rated power. It is not a "
        "guarantee that this power is actually delivered.",
    ),
    "tou_slot1_soc": (
        "Ziel-Ladezustand von Zeitfenster 1. Ein Wert nahe dem Abschalt-SOC kann die "
        "Reserve aufbrauchen; die App prüft nur den Bereich. Wirksam nur im SOC-Modus "
        "(R111 = 1).",
        "Target state of charge of time slot 1. A value close to the shutdown SOC can use "
        "up the reserve; the app only checks the range. Effective only in SOC mode (R111 "
        "= 1).",
    ),
    "tou_slot1_voltage": (
        "Ziel-Batteriespannung von Zeitfenster 1. Ein Wert nahe der unteren Grenze kann "
        "die Reserve aufbrauchen; die App legt keinen allgemeinen Mindestabstand fest. "
        "Wirksam nur im Spannungsmodus (R111 = 0).",
        "Target battery voltage of time slot 1. A value close to the lower limit can use "
        "up the reserve; the app invents no general minimum gap. Effective only in "
        "voltage mode (R111 = 0).",
    ),
    "tou_slot1_grid_charge": (
        "Erlaubt in Zeitfenster 1 das Laden aus dem Netz. Es ist kein erzwungenes Laden; "
        "die übrigen Flags des Fensters bleiben unverändert.",
        "Allows charging from the grid in time slot 1. It is not forced charging; the "
        "other flags of the slot stay unchanged.",
    ),
    "tou_slot1_gen_charge": (
        "Erlaubt in Zeitfenster 1 das Laden aus dem Generator. Es ist kein erzwungenes "
        "Laden und kein Generatorstart; sinnvoll nur mit angeschlossenem Generator.",
        "Allows charging from the generator in time slot 1. It is not forced charging and "
        "starts no generator; useful only with a generator connected.",
    ),
    "tou_slot1_spain_gm": (
        "Nur im Spanien-Modus: Untermodus „GM“ von Zeitfenster 1. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “GM” of time slot 1. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot1_spain_bu": (
        "Nur im Spanien-Modus: Untermodus „BU“ von Zeitfenster 1. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “BU” of time slot 1. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot1_spain_ch": (
        "Nur im Spanien-Modus: Untermodus „CH“ von Zeitfenster 1. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “CH” of time slot 1. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot2_start": (
        "Beginn von Zeitfenster 2 (HH:MM, in 5-Minuten-Schritten). Die Startzeiten müssen "
        "von Slot 1 bis 6 streng aufsteigen; Slot 6 läuft bis Slot 1 beginnt, auch über "
        "Mitternacht. Eine Änderung wirkt sofort auf die angrenzenden Intervalle; andere "
        "Slots ordnet die App nie um.",
        "Start of time slot 2 (HH:MM, in 5-minute steps). The start times must ascend "
        "strictly from slot 1 to 6; slot 6 runs until slot 1 starts, across midnight. A "
        "change affects the adjacent slots at once; the app never rearranges other slots.",
    ),
    "tou_slot2_power": (
        "Leistungsvorgabe von Zeitfenster 2 in W, bis zur Nennleistung des Modells. Es "
        "ist keine Zusage, dass diese Leistung tatsächlich geliefert wird.",
        "Power setting of time slot 2 in W, up to the model's rated power. It is not a "
        "guarantee that this power is actually delivered.",
    ),
    "tou_slot2_soc": (
        "Ziel-Ladezustand von Zeitfenster 2. Ein Wert nahe dem Abschalt-SOC kann die "
        "Reserve aufbrauchen; die App prüft nur den Bereich. Wirksam nur im SOC-Modus "
        "(R111 = 1).",
        "Target state of charge of time slot 2. A value close to the shutdown SOC can use "
        "up the reserve; the app only checks the range. Effective only in SOC mode (R111 "
        "= 1).",
    ),
    "tou_slot2_voltage": (
        "Ziel-Batteriespannung von Zeitfenster 2. Ein Wert nahe der unteren Grenze kann "
        "die Reserve aufbrauchen; die App legt keinen allgemeinen Mindestabstand fest. "
        "Wirksam nur im Spannungsmodus (R111 = 0).",
        "Target battery voltage of time slot 2. A value close to the lower limit can use "
        "up the reserve; the app invents no general minimum gap. Effective only in "
        "voltage mode (R111 = 0).",
    ),
    "tou_slot2_grid_charge": (
        "Erlaubt in Zeitfenster 2 das Laden aus dem Netz. Es ist kein erzwungenes Laden; "
        "die übrigen Flags des Fensters bleiben unverändert.",
        "Allows charging from the grid in time slot 2. It is not forced charging; the "
        "other flags of the slot stay unchanged.",
    ),
    "tou_slot2_gen_charge": (
        "Erlaubt in Zeitfenster 2 das Laden aus dem Generator. Es ist kein erzwungenes "
        "Laden und kein Generatorstart; sinnvoll nur mit angeschlossenem Generator.",
        "Allows charging from the generator in time slot 2. It is not forced charging and "
        "starts no generator; useful only with a generator connected.",
    ),
    "tou_slot2_spain_gm": (
        "Nur im Spanien-Modus: Untermodus „GM“ von Zeitfenster 2. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “GM” of time slot 2. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot2_spain_bu": (
        "Nur im Spanien-Modus: Untermodus „BU“ von Zeitfenster 2. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “BU” of time slot 2. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot2_spain_ch": (
        "Nur im Spanien-Modus: Untermodus „CH“ von Zeitfenster 2. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “CH” of time slot 2. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot3_start": (
        "Beginn von Zeitfenster 3 (HH:MM, in 5-Minuten-Schritten). Die Startzeiten müssen "
        "von Slot 1 bis 6 streng aufsteigen; Slot 6 läuft bis Slot 1 beginnt, auch über "
        "Mitternacht. Eine Änderung wirkt sofort auf die angrenzenden Intervalle; andere "
        "Slots ordnet die App nie um.",
        "Start of time slot 3 (HH:MM, in 5-minute steps). The start times must ascend "
        "strictly from slot 1 to 6; slot 6 runs until slot 1 starts, across midnight. A "
        "change affects the adjacent slots at once; the app never rearranges other slots.",
    ),
    "tou_slot3_power": (
        "Leistungsvorgabe von Zeitfenster 3 in W, bis zur Nennleistung des Modells. Es "
        "ist keine Zusage, dass diese Leistung tatsächlich geliefert wird.",
        "Power setting of time slot 3 in W, up to the model's rated power. It is not a "
        "guarantee that this power is actually delivered.",
    ),
    "tou_slot3_soc": (
        "Ziel-Ladezustand von Zeitfenster 3. Ein Wert nahe dem Abschalt-SOC kann die "
        "Reserve aufbrauchen; die App prüft nur den Bereich. Wirksam nur im SOC-Modus "
        "(R111 = 1).",
        "Target state of charge of time slot 3. A value close to the shutdown SOC can use "
        "up the reserve; the app only checks the range. Effective only in SOC mode (R111 "
        "= 1).",
    ),
    "tou_slot3_voltage": (
        "Ziel-Batteriespannung von Zeitfenster 3. Ein Wert nahe der unteren Grenze kann "
        "die Reserve aufbrauchen; die App legt keinen allgemeinen Mindestabstand fest. "
        "Wirksam nur im Spannungsmodus (R111 = 0).",
        "Target battery voltage of time slot 3. A value close to the lower limit can use "
        "up the reserve; the app invents no general minimum gap. Effective only in "
        "voltage mode (R111 = 0).",
    ),
    "tou_slot3_grid_charge": (
        "Erlaubt in Zeitfenster 3 das Laden aus dem Netz. Es ist kein erzwungenes Laden; "
        "die übrigen Flags des Fensters bleiben unverändert.",
        "Allows charging from the grid in time slot 3. It is not forced charging; the "
        "other flags of the slot stay unchanged.",
    ),
    "tou_slot3_gen_charge": (
        "Erlaubt in Zeitfenster 3 das Laden aus dem Generator. Es ist kein erzwungenes "
        "Laden und kein Generatorstart; sinnvoll nur mit angeschlossenem Generator.",
        "Allows charging from the generator in time slot 3. It is not forced charging and "
        "starts no generator; useful only with a generator connected.",
    ),
    "tou_slot3_spain_gm": (
        "Nur im Spanien-Modus: Untermodus „GM“ von Zeitfenster 3. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “GM” of time slot 3. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot3_spain_bu": (
        "Nur im Spanien-Modus: Untermodus „BU“ von Zeitfenster 3. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “BU” of time slot 3. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot3_spain_ch": (
        "Nur im Spanien-Modus: Untermodus „CH“ von Zeitfenster 3. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “CH” of time slot 3. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot4_start": (
        "Beginn von Zeitfenster 4 (HH:MM, in 5-Minuten-Schritten). Die Startzeiten müssen "
        "von Slot 1 bis 6 streng aufsteigen; Slot 6 läuft bis Slot 1 beginnt, auch über "
        "Mitternacht. Eine Änderung wirkt sofort auf die angrenzenden Intervalle; andere "
        "Slots ordnet die App nie um.",
        "Start of time slot 4 (HH:MM, in 5-minute steps). The start times must ascend "
        "strictly from slot 1 to 6; slot 6 runs until slot 1 starts, across midnight. A "
        "change affects the adjacent slots at once; the app never rearranges other slots.",
    ),
    "tou_slot4_power": (
        "Leistungsvorgabe von Zeitfenster 4 in W, bis zur Nennleistung des Modells. Es "
        "ist keine Zusage, dass diese Leistung tatsächlich geliefert wird.",
        "Power setting of time slot 4 in W, up to the model's rated power. It is not a "
        "guarantee that this power is actually delivered.",
    ),
    "tou_slot4_soc": (
        "Ziel-Ladezustand von Zeitfenster 4. Ein Wert nahe dem Abschalt-SOC kann die "
        "Reserve aufbrauchen; die App prüft nur den Bereich. Wirksam nur im SOC-Modus "
        "(R111 = 1).",
        "Target state of charge of time slot 4. A value close to the shutdown SOC can use "
        "up the reserve; the app only checks the range. Effective only in SOC mode (R111 "
        "= 1).",
    ),
    "tou_slot4_voltage": (
        "Ziel-Batteriespannung von Zeitfenster 4. Ein Wert nahe der unteren Grenze kann "
        "die Reserve aufbrauchen; die App legt keinen allgemeinen Mindestabstand fest. "
        "Wirksam nur im Spannungsmodus (R111 = 0).",
        "Target battery voltage of time slot 4. A value close to the lower limit can use "
        "up the reserve; the app invents no general minimum gap. Effective only in "
        "voltage mode (R111 = 0).",
    ),
    "tou_slot4_grid_charge": (
        "Erlaubt in Zeitfenster 4 das Laden aus dem Netz. Es ist kein erzwungenes Laden; "
        "die übrigen Flags des Fensters bleiben unverändert.",
        "Allows charging from the grid in time slot 4. It is not forced charging; the "
        "other flags of the slot stay unchanged.",
    ),
    "tou_slot4_gen_charge": (
        "Erlaubt in Zeitfenster 4 das Laden aus dem Generator. Es ist kein erzwungenes "
        "Laden und kein Generatorstart; sinnvoll nur mit angeschlossenem Generator.",
        "Allows charging from the generator in time slot 4. It is not forced charging and "
        "starts no generator; useful only with a generator connected.",
    ),
    "tou_slot4_spain_gm": (
        "Nur im Spanien-Modus: Untermodus „GM“ von Zeitfenster 4. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “GM” of time slot 4. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot4_spain_bu": (
        "Nur im Spanien-Modus: Untermodus „BU“ von Zeitfenster 4. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “BU” of time slot 4. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot4_spain_ch": (
        "Nur im Spanien-Modus: Untermodus „CH“ von Zeitfenster 4. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “CH” of time slot 4. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot5_start": (
        "Beginn von Zeitfenster 5 (HH:MM, in 5-Minuten-Schritten). Die Startzeiten müssen "
        "von Slot 1 bis 6 streng aufsteigen; Slot 6 läuft bis Slot 1 beginnt, auch über "
        "Mitternacht. Eine Änderung wirkt sofort auf die angrenzenden Intervalle; andere "
        "Slots ordnet die App nie um.",
        "Start of time slot 5 (HH:MM, in 5-minute steps). The start times must ascend "
        "strictly from slot 1 to 6; slot 6 runs until slot 1 starts, across midnight. A "
        "change affects the adjacent slots at once; the app never rearranges other slots.",
    ),
    "tou_slot5_power": (
        "Leistungsvorgabe von Zeitfenster 5 in W, bis zur Nennleistung des Modells. Es "
        "ist keine Zusage, dass diese Leistung tatsächlich geliefert wird.",
        "Power setting of time slot 5 in W, up to the model's rated power. It is not a "
        "guarantee that this power is actually delivered.",
    ),
    "tou_slot5_soc": (
        "Ziel-Ladezustand von Zeitfenster 5. Ein Wert nahe dem Abschalt-SOC kann die "
        "Reserve aufbrauchen; die App prüft nur den Bereich. Wirksam nur im SOC-Modus "
        "(R111 = 1).",
        "Target state of charge of time slot 5. A value close to the shutdown SOC can use "
        "up the reserve; the app only checks the range. Effective only in SOC mode (R111 "
        "= 1).",
    ),
    "tou_slot5_voltage": (
        "Ziel-Batteriespannung von Zeitfenster 5. Ein Wert nahe der unteren Grenze kann "
        "die Reserve aufbrauchen; die App legt keinen allgemeinen Mindestabstand fest. "
        "Wirksam nur im Spannungsmodus (R111 = 0).",
        "Target battery voltage of time slot 5. A value close to the lower limit can use "
        "up the reserve; the app invents no general minimum gap. Effective only in "
        "voltage mode (R111 = 0).",
    ),
    "tou_slot5_grid_charge": (
        "Erlaubt in Zeitfenster 5 das Laden aus dem Netz. Es ist kein erzwungenes Laden; "
        "die übrigen Flags des Fensters bleiben unverändert.",
        "Allows charging from the grid in time slot 5. It is not forced charging; the "
        "other flags of the slot stay unchanged.",
    ),
    "tou_slot5_gen_charge": (
        "Erlaubt in Zeitfenster 5 das Laden aus dem Generator. Es ist kein erzwungenes "
        "Laden und kein Generatorstart; sinnvoll nur mit angeschlossenem Generator.",
        "Allows charging from the generator in time slot 5. It is not forced charging and "
        "starts no generator; useful only with a generator connected.",
    ),
    "tou_slot5_spain_gm": (
        "Nur im Spanien-Modus: Untermodus „GM“ von Zeitfenster 5. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “GM” of time slot 5. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot5_spain_bu": (
        "Nur im Spanien-Modus: Untermodus „BU“ von Zeitfenster 5. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “BU” of time slot 5. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot5_spain_ch": (
        "Nur im Spanien-Modus: Untermodus „CH“ von Zeitfenster 5. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “CH” of time slot 5. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot6_start": (
        "Beginn von Zeitfenster 6 (HH:MM, in 5-Minuten-Schritten). Die Startzeiten müssen "
        "von Slot 1 bis 6 streng aufsteigen; Slot 6 läuft bis Slot 1 beginnt, auch über "
        "Mitternacht. Eine Änderung wirkt sofort auf die angrenzenden Intervalle; andere "
        "Slots ordnet die App nie um.",
        "Start of time slot 6 (HH:MM, in 5-minute steps). The start times must ascend "
        "strictly from slot 1 to 6; slot 6 runs until slot 1 starts, across midnight. A "
        "change affects the adjacent slots at once; the app never rearranges other slots.",
    ),
    "tou_slot6_power": (
        "Leistungsvorgabe von Zeitfenster 6 in W, bis zur Nennleistung des Modells. Es "
        "ist keine Zusage, dass diese Leistung tatsächlich geliefert wird.",
        "Power setting of time slot 6 in W, up to the model's rated power. It is not a "
        "guarantee that this power is actually delivered.",
    ),
    "tou_slot6_soc": (
        "Ziel-Ladezustand von Zeitfenster 6. Ein Wert nahe dem Abschalt-SOC kann die "
        "Reserve aufbrauchen; die App prüft nur den Bereich. Wirksam nur im SOC-Modus "
        "(R111 = 1).",
        "Target state of charge of time slot 6. A value close to the shutdown SOC can use "
        "up the reserve; the app only checks the range. Effective only in SOC mode (R111 "
        "= 1).",
    ),
    "tou_slot6_voltage": (
        "Ziel-Batteriespannung von Zeitfenster 6. Ein Wert nahe der unteren Grenze kann "
        "die Reserve aufbrauchen; die App legt keinen allgemeinen Mindestabstand fest. "
        "Wirksam nur im Spannungsmodus (R111 = 0).",
        "Target battery voltage of time slot 6. A value close to the lower limit can use "
        "up the reserve; the app invents no general minimum gap. Effective only in "
        "voltage mode (R111 = 0).",
    ),
    "tou_slot6_grid_charge": (
        "Erlaubt in Zeitfenster 6 das Laden aus dem Netz. Es ist kein erzwungenes Laden; "
        "die übrigen Flags des Fensters bleiben unverändert.",
        "Allows charging from the grid in time slot 6. It is not forced charging; the "
        "other flags of the slot stay unchanged.",
    ),
    "tou_slot6_gen_charge": (
        "Erlaubt in Zeitfenster 6 das Laden aus dem Generator. Es ist kein erzwungenes "
        "Laden und kein Generatorstart; sinnvoll nur mit angeschlossenem Generator.",
        "Allows charging from the generator in time slot 6. It is not forced charging and "
        "starts no generator; useful only with a generator connected.",
    ),
    "tou_slot6_spain_gm": (
        "Nur im Spanien-Modus: Untermodus „GM“ von Zeitfenster 6. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “GM” of time slot 6. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot6_spain_bu": (
        "Nur im Spanien-Modus: Untermodus „BU“ von Zeitfenster 6. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “BU” of time slot 6. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
    "tou_slot6_spain_ch": (
        "Nur im Spanien-Modus: Untermodus „CH“ von Zeitfenster 6. Die drei Bits sind "
        "keine unabhängigen Schalter: Sind mehrere gesetzt, gewinnt Bit 4 vor Bit 3 vor "
        "Bit 2. Was die Untermodi im Einzelnen bewirken, ist nicht belegt.",
        "Spain mode only: sub-mode “CH” of time slot 6. The three bits are not "
        "independent switches: if several are set, bit 4 wins over bit 3 over bit 2. What "
        "the sub-modes do in detail is not proved.",
    ),
}

SENSOR_HELP: Final[dict[str, tuple[str, str]]] = {
    "self_check_time_setting": (
        "Rohwert des Worts „Selbsttest-Zeit“ (R82). Das Protokoll widerspricht sich hier "
        "(Bereich 0–1, Hinweis 0–360 Sekunden). 0xFFFF ist undokumentiert; eine echte "
        "Verzögerung von 65535 Sekunden ist nicht anzunehmen, die tatsächliche Verwendung ist "
        "ungeklärt. Reine Diagnose.",
        "Raw value of the “self-check time” word (R82). The protocol contradicts itself here "
        "(range 0–1, note 0–360 seconds). 0xFFFF is undocumented; do not read it as a real "
        "65535-second delay, the effective use is unresolved. Diagnostics only.",
    ),
    "anti_islanding_enable_raw": (
        "Rohwert des Freigabeworts der Inselerkennung (R83). Die Firmware wertet nur genau 1 "
        "als aktiv. 0xFFFF ist undokumentiert: der von R83 abhängige zusätzliche Pfad ist dann "
        "nicht aktiviert. Das heißt nicht, dass der gesamte Netz- oder Inselschutz des "
        "Wechselrichters aus ist, und es ist kein Gesundheitswert. Der Wert ist ein "
        "Konfigurationswort und keine Rückmeldung eines Schutzes.",
        "Raw value of the anti-islanding enable word (R83). The firmware only counts exactly 1 "
        "as enabled. 0xFFFF is undocumented: the additional R83-dependent path is then not "
        "enabled. This does not mean the inverter's whole grid or island protection is off, and "
        "it is not a health value. It is a configuration word, not feedback from a protection.",
    ),
    "reactive_pf_option_raw": (
        "Rohwert der Blindleistungs-/PF-Option (R84). Dokumentiert sind 0 = aus, 1 = PF, 2 = Q. "
        "0xFFFF ist keiner dieser Modi; der wirksame Modus ist ungeklärt. Die "
        "Blindleistungsregelung hat weitere Parameterpfade, das Wort beschreibt sie nicht "
        "vollständig.",
        "Raw value of the reactive/PF option (R84). Documented: 0 = off, 1 = PF, 2 = Q. 0xFFFF "
        "is none of these modes; the effective mode is unresolved. Reactive-power regulation "
        "has other parameter paths, so this word does not describe it completely.",
    ),
    "rcd_enable_raw": (
        "Rohwert des RCD-Freigabeworts (R86). Die Firmware unterscheidet nur null und nicht "
        "null; 0xFFFF gilt wie 1, die RCD-Abtastung (Zählpfad) ist aktiv. Das beweist nur diese "
        "Software-Bedingung, keine gesunde oder voll funktionierende Fehlerstromüberwachung.",
        "Raw value of the RCD enable word (R86). The firmware only tells zero from nonzero; "
        "0xFFFF counts like 1, so the RCD sampling (counter path) is active. This proves only "
        "that software condition, not a healthy or fully working residual-current monitor.",
    ),
    "riso_enable_raw": (
        "Rohwert der RISO-Option (R87), dokumentiert als 0/1. Für 0xFFFF ist kein Verbraucher "
        "und keine Korrektur bekannt; „ungenutzt“ und „Isolationsüberwachung aus“ sind beide "
        "stärker als die Belege. Das Verhalten ist ungeklärt.",
        "Raw value of the RISO option (R87), documented as 0/1. For 0xFFFF no consumer and no "
        "repair is known; “unused” and “insulation monitoring off” are both stronger than the "
        "evidence. The behaviour is unresolved.",
    ),
    "track_grid_phase_raw": (
        "Rohwert der automatischen Phasenfolge-Korrektur (R235). 0 ist der dokumentierte "
        "Standard: die Korrektur wird nicht angefordert; die normale Netzsynchronisation "
        "bleibt davon unberührt. Nur genau 1 schaltet sie ein. Der Zustand zeigt „Aus“ "
        "oder „An“; das Rohwort steht in den Attributen.",
        "Raw value of the automatic phase-sequence correction (R235). 0 is the documented "
        "default: the correction is not requested; normal grid synchronization is "
        "unaffected. Only exactly 1 turns it on. The state shows Off or On; the raw word is in "
        "the attributes.",
    ),
    "it_system_raw": (
        "Rohwert der Einstellung „IT-System, Neutralleiter nicht geerdet“ (R236). Das Wort ist "
        "eine Konfiguration und beweist nicht, wie Neutralleiter und Erdung tatsächlich "
        "ausgeführt sind.",
        "Raw value of the “IT system, neutral not grounded” setting (R236). The word is a "
        "configuration and does not prove how neutral and earthing are actually wired.",
    ),
    "relay_inverter": (
        "Zeigt, ob die Firmware das Wechselrichter-Relais als eingeschaltet führt. Es ist "
        "der Softwarezustand, keine mechanische Rückmeldung des Relais.",
        "Shows whether the firmware holds the inverter relay as on. This is the software "
        "state, not mechanical feedback from the relay.",
    ),
    "relay_grid": (
        "Zeigt, ob die Firmware das Netzrelais als eingeschaltet führt. 0 heißt: Das "
        "Display zeigt „Off grid“. Es ist der Softwarezustand, keine mechanische "
        "Rückmeldung des Relais. Es beweist keine physische Trennung.",
        "Shows whether the firmware holds the grid relay as on. 0 means the display shows "
        "“Off grid”. This is the software state, not mechanical feedback from the relay. "
        "It does not prove physical isolation.",
    ),
    "relay_generator": (
        "Zeigt, ob die Firmware das Generator-Relais am GEN-Anschluss als eingeschaltet "
        "führt. Es ist der Softwarezustand, keine mechanische Rückmeldung des Relais.",
        "Shows whether the firmware holds the generator relay on the GEN port as on. This "
        "is the software state, not mechanical feedback from the relay.",
    ),
    "relay_grid_supply": (
        "Entspricht dem Bit „grid give power to relay“ der Herstellerbeschreibung. Ob "
        "damit die Versorgung des LOAD-Anschlusses oder das zweite Netzrelais in Reihe "
        "gemeint ist, ist nicht belegt. Es ist der Softwarezustand, keine mechanische "
        "Rückmeldung des Relais.",
        "Matches the bit “grid give power to relay” of the vendor description. Whether it "
        "means the LOAD-port supply or the second series grid relay is not proved. This "
        "is the software state, not mechanical feedback from the relay.",
    ),
    "dry_contact_1": (
        "Zeigt den Softwarezustand von Trockenkontakt 1. Wofür der Kontakt verdrahtet "
        "ist, kann das Gerät nicht sagen. Es ist der Softwarezustand, keine mechanische "
        "Rückmeldung des Relais.",
        "Shows the software state of dry contact 1. What the contact is wired to, the "
        "device cannot tell. This is the software state, not mechanical feedback from the "
        "relay.",
    ),
    "dry_contact_2": (
        "Zeigt den Softwarezustand von Trockenkontakt 2, der laut Firmware-Analyse "
        "vermutlich das Grid-/Gen-Signal (R140) ausgibt. Eine Rückmeldung eines "
        "angeschlossenen Schützes ist es nicht. Es ist der Softwarezustand, keine "
        "mechanische Rückmeldung des Relais.",
        "Shows the software state of dry contact 2, which firmware analysis suggests "
        "carries the Grid / Gen signal (R140). It is no feedback from a connected "
        "contactor. This is the software state, not mechanical feedback from the relay.",
    ),
    "bms_force_charge": (
        "Das BMS meldet eine Zwangsladung (Bit 1 von R222). Ob und wie der Wechselrichter "
        "darauf reagiert, ist nicht belegt.",
        "The BMS reports force charge (bit 1 of R222). Whether and how the inverter "
        "reacts is not established.",
    ),
    "bms_sleep": (
        "Das BMS meldet den Schlafzustand (Bit 3 von R222). Was der Wechselrichter damit "
        "macht, ist nicht belegt.",
        "The BMS reports its sleep state (bit 3 of R222). What the inverter does with it "
        "is not established.",
    ),
    "bms_flags_raw": (
        "Rohwort der BMS-Statusflags (R222). Einzelne Bits sind nur teilweise belegt; 0 "
        "heißt nur, dass keine bekannten Flags gesetzt sind.",
        "Raw word of the BMS status flags (R222). Individual bits are only partly "
        "established; 0 only means no known flags are set.",
    ),
    "bms_alarm_word": (
        "Rohes Alarmwort, wie es das BMS meldet. 0 beweist keine gesunde Batterie.",
        "Raw alarm word as the BMS reports it. Zero does not prove a healthy battery.",
    ),
    "bms_offgrid_charge_limit": (
        "Die Firmware schreibt hier unabhängig vom BMS den festen Wert 0 ein; 0 ist daher "
        "nie ein echtes 0-A-Limit, sondern „nicht geliefert“. Mit den gewöhnlichen "
        "BMS-Strombegrenzungen hat der Wert nichts zu tun.",
        "The firmware writes the fixed value 0 here whatever the BMS sends; 0 is "
        "therefore never a real 0 A limit but “not provided”. The value has nothing to do "
        "with the ordinary BMS current limits.",
    ),
    "bms_offgrid_discharge_limit_raw": (
        "Die Firmware schreibt hier unabhängig vom BMS den festen Wert 0 ein; 0 ist daher "
        "nie ein echtes 0-A-Limit, sondern „nicht geliefert“. Mit den gewöhnlichen "
        "BMS-Strombegrenzungen hat der Wert nichts zu tun.",
        "The firmware writes the fixed value 0 here whatever the BMS sends; 0 is "
        "therefore never a real 0 A limit but “not provided”. The value has nothing to do "
        "with the ordinary BMS current limits.",
    ),
    "force_off_grid_reported": (
        "Die konfigurierte Einstellung „Force Off-Grid“ (Inselbetrieb erzwingen, R179): "
        "0 = Auto, 1 = erzwungen. Es ist die Anforderung, nicht der Netzzustand. Bei 1 setzt die "
        "Firmware die Netzqualifizierung ständig zurück.",
        "The configured “Force Off-Grid” setting (R179): 0 = Auto, 1 = forced. It is the "
        "request, not the grid state. At 1 the firmware keeps resetting the grid "
        "qualification.",
    ),
    "signal_island_mode_reported": (
        "Die konfigurierte Einstellung „Signal Island Mode“ (R178, Bits 8–9; in der "
        "Deye-Cloud „Neutral to earth bonding“): ein, aus oder unbekannt. Es gibt keine "
        "Rückmeldung eines Kontakts oder der N-PE-Verbindung.",
        "The configured “Signal Island Mode” (R178 bits 8–9; “Neutral to earth bonding” "
        "in the Deye cloud): on, off or unknown. There is no feedback of a contact or of "
        "the N–PE bond.",
    ),
    "mppt_scanning_reported": (
        "Die konfigurierte Einstellung „MPPT Scan“ (Mehrpunkt-Suche des MPPT, R341, Bit "
        "2). Sie sagt nicht, ob gerade gescannt wird.",
        "The configured “MPPT Scan” setting (multi-point search of the MPPT, R341 bit 2). "
        "It does not say whether a scan is running.",
    ),
    "external_signal_reported": (
        "Die konfigurierte Signalanforderung (R140): aus, Grid-Signal, Gen-Signal oder "
        "beide. Beide Signale teilen sich einen Ausgang. Über den Zustand eines "
        "angeschlossenen Schützes sagt der Wert nichts.",
        "The configured signal request (R140): off, Grid Signal, Gen Signal or both. Both "
        "signals share one output. The value says nothing about the state of a connected "
        "contactor.",
    ),
    "export_mode_setting": (
        "Die konfigurierte Betriebsart „System Work Mode“ (R142): Selling First, Zero "
        "Export to Load oder Zero Export to CT (Selling First ist nicht „Solar Sell“, "
        "das R142 = 1/2 voraussetzt). Sie beweist keine Einspeiseerlaubnis; die App nutzt "
        "sie nur als Plausibilitätshinweis zur Anlagenbeschreibung.",
        "The configured “System Work Mode” (R142): Selling First, Zero Export to Load "
        "or Zero Export to CT (Selling First is not “Solar Sell”, which needs R142 = "
        "1/2). It proves no permission to export; the app uses it only as a "
        "consistency hint for the installation description.",
    ),
    "ct_ratio_reported": (
        "Das konfigurierte Übersetzungsverhältnis des externen Stromwandlers (R347, "
        "Rohwert). Ein endgültiger Verbraucher in der Firmware ist nicht belegt.",
        "The configured turns ratio of the external current transformer (R347, raw). A "
        "final consumer in the firmware is not established.",
    ),
    "battery_power_derived": (
        "Batterieleistung aus Spannung × Strom. Die Richtung (Laden oder Entladen) wird "
        "nicht gedeutet, weil der Vorzeichennachweis in der Firmware fehlt.",
        "Battery power from voltage × current. The direction (charging or discharging) is "
        "not interpreted because the sign proof in the firmware is missing.",
    ),
    "bms_power_derived": (
        "BMS-Batterieleistung als negiertes Produkt aus gemeldeter BMS-Spannung und "
        "vorzeichenbehaftetem BMS-Strom. An einer Anlage war der BMS-Strom beim Laden "
        "positiv; die Negation macht die Leistung dann beim Laden negativ (50 V × +10 A "
        "ergibt −500 W) und beim Entladen positiv. Die physikalische Richtung hängt von "
        "Installation, Protokoll und Kalibrierung ab.",
        "BMS battery power as the negated product of the reported BMS voltage and the "
        "signed BMS current. On one installation the BMS current was positive while "
        "charging; the negation then makes the power negative while charging (50 V × +10 A "
        "gives −500 W) and positive while discharging. The physical direction depends on "
        "installation, protocol and calibration.",
    ),
    "dc_input_power_total": (
        "Summe der DC-Leistung der PV-Eingänge dieses Wechselrichters, die laut "
        "Anlagenbeschreibung genutzt werden. Nicht benutzte Eingänge zählen nicht mit.",
        "Sum of the DC power of those PV inputs of this inverter that the installation "
        "description marks as used. Unused inputs do not count.",
    ),
    "gen_power_raw_a": (
        "Leistung am GEN-Anschluss, mit Vorzeichen, in W. Mit dem GEN-Anschluss als Smart "
        "Load (R133 = 1) wendet die Firmware eine Vorzeichenumkehr an (belegt); die "
        "elektrische Bezugs-/Einspeise-Richtung und die Kalibrierung sind nicht belegt, "
        "deshalb wird keine Flussrichtung behauptet.",
        "Power at the GEN port, signed, in W. With the GEN port set to Smart Load (R133 = "
        "1) the firmware applies a sign negation (proved); the electrical import / export "
        "orientation and the calibration are not proved, so no flow direction is claimed.",
    ),
    "gen_power_raw_b": (
        "Leistung am GEN-Anschluss, mit Vorzeichen, in W. Mit dem GEN-Anschluss als Smart "
        "Load (R133 = 1) wendet die Firmware eine Vorzeichenumkehr an (belegt); die "
        "elektrische Bezugs-/Einspeise-Richtung und die Kalibrierung sind nicht belegt, "
        "deshalb wird keine Flussrichtung behauptet.",
        "Power at the GEN port, signed, in W. With the GEN port set to Smart Load (R133 = "
        "1) the firmware applies a sign negation (proved); the electrical import / export "
        "orientation and the calibration are not proved, so no flow direction is claimed.",
    ),
    "gen_power_raw_c": (
        "Leistung am GEN-Anschluss, mit Vorzeichen, in W. Mit dem GEN-Anschluss als Smart "
        "Load (R133 = 1) wendet die Firmware eine Vorzeichenumkehr an (belegt); die "
        "elektrische Bezugs-/Einspeise-Richtung und die Kalibrierung sind nicht belegt, "
        "deshalb wird keine Flussrichtung behauptet.",
        "Power at the GEN port, signed, in W. With the GEN port set to Smart Load (R133 = "
        "1) the firmware applies a sign negation (proved); the electrical import / export "
        "orientation and the calibration are not proved, so no flow direction is claimed.",
    ),
    "gen_power_raw_total": (
        "Leistung am GEN-Anschluss, mit Vorzeichen, in W. Mit dem GEN-Anschluss als Smart "
        "Load (R133 = 1) wendet die Firmware eine Vorzeichenumkehr an (belegt); die "
        "elektrische Bezugs-/Einspeise-Richtung und die Kalibrierung sind nicht belegt, "
        "deshalb wird keine Flussrichtung behauptet.",
        "Power at the GEN port, signed, in W. With the GEN port set to Smart Load (R133 = "
        "1) the firmware applies a sign negation (proved); the electrical import / export "
        "orientation and the calibration are not proved, so no flow direction is claimed.",
    ),
    "ac_solar_power_raw_a": (
        "Gemeldete Leistung einer AC-gekoppelten PV-Anlage, nur als Betrag, 10 W je "
        "Zählschritt. Ob sie am GEN-Anschluss, am Lastanschluss oder an einem "
        "Zusatzzähler gemessen wird, ist nicht geklärt; es ist kein belegter GEN-Eingang.",
        "Reported power of an AC-coupled PV system, magnitude only, 10 W per count. "
        "Whether it is measured at the GEN port, the load port or an extra meter is "
        "unresolved; it is not a verified GEN-port input.",
    ),
    "ac_solar_power_raw_b": (
        "Gemeldete Leistung einer AC-gekoppelten PV-Anlage, nur als Betrag, 10 W je "
        "Zählschritt. Ob sie am GEN-Anschluss, am Lastanschluss oder an einem "
        "Zusatzzähler gemessen wird, ist nicht geklärt; es ist kein belegter GEN-Eingang.",
        "Reported power of an AC-coupled PV system, magnitude only, 10 W per count. "
        "Whether it is measured at the GEN port, the load port or an extra meter is "
        "unresolved; it is not a verified GEN-port input.",
    ),
    "ac_solar_power_raw_c": (
        "Gemeldete Leistung einer AC-gekoppelten PV-Anlage, nur als Betrag, 10 W je "
        "Zählschritt. Ob sie am GEN-Anschluss, am Lastanschluss oder an einem "
        "Zusatzzähler gemessen wird, ist nicht geklärt; es ist kein belegter GEN-Eingang.",
        "Reported power of an AC-coupled PV system, magnitude only, 10 W per count. "
        "Whether it is measured at the GEN port, the load port or an extra meter is "
        "unresolved; it is not a verified GEN-port input.",
    ),
    "load_current_invalid_a": (
        "In der Herstellerbeschreibung als ungültig geführt und bei den untersuchten "
        "Firmwareständen mit Nullen gefüllt. Ein Wert hier ist kein gemessener Laststrom.",
        "Documented as invalid and zero-filled in the analysed firmware builds. A value "
        "here is not a measured load current.",
    ),
    "load_current_invalid_b": (
        "In der Herstellerbeschreibung als ungültig geführt und bei den untersuchten "
        "Firmwareständen mit Nullen gefüllt. Ein Wert hier ist kein gemessener Laststrom.",
        "Documented as invalid and zero-filled in the analysed firmware builds. A value "
        "here is not a measured load current.",
    ),
    "load_current_invalid_c": (
        "In der Herstellerbeschreibung als ungültig geführt und bei den untersuchten "
        "Firmwareständen mit Nullen gefüllt. Ein Wert hier ist kein gemessener Laststrom.",
        "Documented as invalid and zero-filled in the analysed firmware builds. A value "
        "here is not a measured load current.",
    ),
    "temp_dc_transformer": (
        "Die Firmware überschreibt den Wert im normalen Betrieb mit der festen Rohzahl "
        "1250, also 25,0 °C. Er ist ein Platzhalter und keine Messung; ob ein physischer "
        "Fühler fehlt, ist damit nicht bewiesen.",
        "In normal operation the firmware overwrites the value with the fixed raw 1250, "
        "i.e. 25.0 °C. It is a placeholder, not a measurement; this does not prove that "
        "no physical sensor exists.",
    ),
    "battery_power_raw": (
        "Batterieleistung als Rohzahl: Einheit (1 W oder 10 W je Zählschritt) und "
        "Richtung sind nicht bestätigt. Zur Auswertung eignet sich die abgeleitete "
        "Batterieleistung besser.",
        "Battery power as a raw number: the unit (1 W or 10 W per count) and the "
        "direction are not confirmed. The derived battery power is better suited for "
        "evaluation.",
    ),
    "operating_state": (
        "Aktueller Betriebszustand des Geräts. In den untersuchten Ständen ist der "
        "öffentliche Wert eine Abbildung eines internen Ablaufs; für die Zustände 5 und 6 "
        "sollten keine festen Abfolgen angenommen werden.",
        "Current operating state of the device. In the analysed builds the public value "
        "is a projection of an internal stage; no fixed sequences should be assumed for "
        "states 5 and 6.",
    ),
    "remote_run_enable": (
        "Konfigurierte Software-Laufanforderung (0 = AUS, 1 = EIN), getrennt vom "
        "tatsächlichen Betriebs- oder Inselzustand. AUS kann den Ausgang stoppen, eine "
        "parallele Insel unterbrechen und gemeldete Stoppfehler auslösen, in dokumentierten"
        " Tests F19. Es ist keine elektrische Trennung. Diese App schreibt den Wert nicht.",
        "Configured software run request (0 = OFF, 1 = ON), separate from the actual "
        "running or island state. OFF can stop the output, interrupt a parallel island and "
        "cause reported stop faults, F19 in documented tests. It is not electrical "
        "isolation. This app does not write the value.",
    ),
    "battery_operating_basis": (
        "Der konfigurierte Batterie-Betriebsmodus (R111). Für die unterstützte Firmware "
        "gilt 0 = Spannung, 1 = SOC, 2 = keine Batterie; andere Kodierungen sind unbekannt."
        " Er legt fest, ob Schwellen als Spannung oder als Ladezustand gelten. Das Rohwort "
        "steht im Attribut raw_hex.",
        "The configured battery control mode (R111). On the supported firmware 0 = voltage,"
        " 1 = SOC, 2 = no battery; other encodings are unknown. It decides whether "
        "thresholds apply as voltage or as state of charge. The raw word is in the "
        "attribute raw_hex.",
    ),
    "alarm_battery_missing": (
        "Warnung W31 (Bit 14 von R554). Die Herstellerbeschreibung nennt sie „lithium "
        "battery missing“, die Cloud „Batteriekommunikationswarnung“: Die Kommunikation "
        "mit dem BMS fehlt oder ist abgerissen.",
        "Warning W31 (bit 14 of R554). The vendor description calls it “lithium battery "
        "missing”, the cloud “battery communication warning”: communication with the BMS "
        "is missing or has been lost.",
    ),
    "bms_charge_current_limit": (
        "Vom BMS gemeldete Ladestromgrenze. Ein Wert von 0 A kann eine tatsächlich "
        "gemeldete Grenze sein. Ist der BMS-Block nicht verfügbar oder nicht aktualisiert, "
        "gilt der Wert als unbekannt.",
        "Charge current limit reported by the BMS. A value of 0 A can be an actually "
        "reported limit. If the BMS block is unavailable or not refreshed, the value counts"
        " as unknown.",
    ),
    "bms_discharge_current_limit": (
        "Vom BMS gemeldete Entladestromgrenze. Ein Wert von 0 A kann eine tatsächlich "
        "gemeldete Grenze sein. Ist der BMS-Block nicht verfügbar oder nicht aktualisiert, "
        "gilt der Wert als unbekannt.",
        "Discharge current limit reported by the BMS. A value of 0 A can be an actually "
        "reported limit. If the BMS block is unavailable or not refreshed, the value counts"
        " as unknown.",
    ),
    "bms_charge_voltage": (
        "Vom BMS gemeldete Ladespannung (R210, vorzeichenloses Rohwort in "
        "Hundertstelvolt, in Volt umgerechnet). Ob das Feld befüllt wird, hängt von "
        "Protokoll und Decoder ab. Ist der BMS-Block nicht verfügbar, gilt der Wert als "
        "unbekannt. Ein frischer Lesezugriff beweist keinen neuen BMS-Bericht: er kann auch "
        "einen zwischengespeicherten Wert liefern.",
        "Charge voltage reported by the BMS (R210, unsigned centivolts converted to volts). "
        "Whether the field is filled depends on protocol and decoder. If the BMS block is "
        "unavailable, the value counts as unknown. A fresh read does not prove a new BMS "
        "report: it can return a cached value.",
    ),
    "bms_discharge_voltage": (
        "Vom BMS gemeldete Entladespannung (R211). Ob das Feld befüllt wird, hängt von "
        "Protokoll und Decoder ab; manche Berichte lassen es aus. Das Feld hat einen "
        "echten Verbraucher in der Schutzgrenzen-Auswertung, ein Wert ist also nicht "
        "bedeutungslos. Ist der BMS-Block nicht verfügbar, gilt der Wert als unbekannt. "
        "Ein frischer Lesezugriff beweist keinen neuen BMS-Bericht.",
        "Discharge voltage reported by the BMS (R211). Whether the field is filled depends "
        "on protocol and decoder; some reports omit it. The field has a real consumer in "
        "the protective-limit evaluation, so a value is not meaningless. If the BMS block "
        "is unavailable, the value counts as unknown. A fresh read does not prove a new BMS "
        "report.",
    ),
    "bms_temperature": (
        "Batterietemperatur, soweit verfügbar. Die App zeigt sie nur unter bestimmten "
        "Bedingungen; an einer beobachteten Parallelanlage lieferten Slaves ohne BMS Werte "
        "wie −100 °C. Die genaue Ursache von −82 °C ist ungeklärt. Daraus folgt weder, dass"
        " jeder Slave ungültige Werte liefert, noch dass jede Einzelanlage gültige hat.",
        "Battery temperature, where available. The app shows it only under certain "
        "conditions; on one observed parallel installation slaves without a BMS gave values"
        " such as −100 °C. The exact cause of −82 °C is unresolved. It does not follow that"
        " every slave reports invalid values or that every standalone reading is valid.",
    ),
    "battery_temperature": (
        "Batterietemperatur, soweit verfügbar. Die App zeigt sie nur unter bestimmten "
        "Bedingungen; an einer beobachteten Parallelanlage lieferten Slaves ohne BMS Werte "
        "wie −100 °C. Die genaue Ursache von −82 °C ist ungeklärt. Daraus folgt weder, dass"
        " jeder Slave ungültige Werte liefert, noch dass jede Einzelanlage gültige hat.",
        "Battery temperature, where available. The app shows it only under certain "
        "conditions; on one observed parallel installation slaves without a BMS gave values"
        " such as −100 °C. The exact cause of −82 °C is unresolved. It does not follow that"
        " every slave reports invalid values or that every standalone reading is valid.",
    ),
    "parallel_balancing_stop_soc_raw": (
        "Dokumentiert als Ladezustandswort für das Ende des Ausgleichs im Inselbetrieb; "
        "Wirkung, Einheit und Bereich sind ungeklärt. Eine wirksame Verwendung durch die "
        "Firmware ist nicht belegt.",
        "Documented as an off-grid balancing-stop SOC word; effective use, unit and range "
        "are unresolved. Effective use by the firmware is not proved.",
    ),
    "grid_monitoring_source": (
        "Die konfigurierte Netzmessquelle (R344, Rohwort): Bit 0 CT/Zähler, Bits 1–2 "
        "Verwendungen eines Zusatzzählers. Die genaue Aufteilung ist nicht in allen "
        "Teilen belegt.",
        "The configured grid sensing source (R344, raw word): bit 0 CT / meter, bits 1–2 "
        "extra-meter uses. The exact split is not proved in every part.",
    ),
    "ct_selfcheck_result_raw": (
        "Ergebnis der CT-Selbstprüfung als Rohwort. Unbekannt, solange keine Prüfung "
        "abgeschlossen ist – niemals „gesund“.",
        "CT self-check result as a raw word. Unknown unless a check has completed – never "
        "“healthy”.",
    ),
}


def _both(texts: tuple[str, str] | None) -> dict[str, str] | None:
    return None if texts is None else {"de": texts[0], "en": texts[1]}


def write_help(key: str) -> dict[str, str] | None:
    """The (i) help of a write setting in both languages, or None when it has none."""
    return _both(WRITE_HELP.get(key))


def sensor_help(key: str) -> dict[str, str] | None:
    """The (i) help of a sensor in both languages, or None when it has none."""
    return _both(SENSOR_HELP.get(key))
