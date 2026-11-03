"""State rules, part A: mapping rows 0-99 (identity, topology, status, relays, alarms, reported
placeholders). Labels name the state of a setting or report, never a health verdict."""

from __future__ import annotations

from typing import Any

from deye_monitor.profiles.alarms import active_items, describe, format_active
from deye_monitor.profiles.schema import Entry
from deye_monitor.profiles.states import (
    NOT_REPORTED,
    UNCONFIRMED,
    UNKNOWN,
    Label,
    Rule,
    by_field,
    constant,
    on_off,
    options_of,
    owned,
    register,
)

Words = tuple[int, ...]
Result = Label | tuple[Label, dict[str, Any]]

NOT_PROVIDED = Label("Nicht bereitgestellt", "Not provided")
UNCONFIRMED_VALUE = Label("Unbestätigter Wert", "Unconfirmed value")
NOT_ASSIGNED = Label("Nicht zugeordnet", "Unmapped")
THREE_PHASE_LV = Label("Dreiphasiger LV-Hybrid", "Three-phase LV hybrid")
THREE_PHASE_HV = Label("Dreiphasiger HV-Hybrid", "Three-phase HV hybrid")
NONE_REPORTED = Label("Keine gemeldet", "None reported")
UNKNOWN_REPORT = Label("Unbekannte Meldung", "Unknown report")


def _hex(words: Words) -> str:
    return "-".join(f"{w:04X}" for w in words)


# -- identity and configuration reports ------------------------------------------------------

_DEVICE_TYPE = {
    512: Label("String-Wechselrichter", "String inverter"),
    768: Label("Einphasiger Hybrid", "Single-phase hybrid"),
    1024: Label("Mikro-Wechselrichter", "Microinverter"),
    5: THREE_PHASE_LV,
    1280: THREE_PHASE_LV,
    6: THREE_PHASE_HV,
    1536: THREE_PHASE_HV,
    1537: Label("Dreiphasiger HV-Hybrid (20–50 kW)", "Three-phase HV hybrid (20–50 kW)"),  # noqa: RUF001
    2048: Label("PCS100k", "PCS100k"),
    2304: Label("Balkonspeicher", "Balcony storage"),
}
register("device_type", by_field(_DEVICE_TYPE), options=options_of(_DEVICE_TYPE, UNKNOWN))

_OTHER_FAMILY = Label("Andere Gerätefamilie / unbekannt", "Other device family / unknown")


def _id_device_type(entry: Entry, words: Words) -> Result:
    return THREE_PHASE_LV if words[0] in (0x0005, 0x0500) else _OTHER_FAMILY


register("id_device_type", _id_device_type, options=(THREE_PHASE_LV, _OTHER_FAMILY))


def _identifier(entry: Entry, words: Words) -> Result:
    """Address/node number: a valid identifier is 1..247, anything else is unresolved."""
    n = owned(entry, words)
    return Label(f"Adresse {n}", f"Address {n}") if 1 <= n <= 247 else UNKNOWN


register("modbus_address", _identifier)


def _node(entry: Entry, words: Words) -> Result:
    n = owned(entry, words)
    return Label(f"Knoten {n}", f"Node {n}") if 1 <= n <= 63 else UNKNOWN


register("parallel_sn", _node)


def _version(entry: Entry, words: Words) -> Result:
    """Official version/ID spelling: four hex digits per word, in source register order."""
    text = _hex(words)
    return Label(f"Version/Kennung: {text}", f"Version/ID: {text}")


for _key in (
    "protocol_version",
    "hw_platform",
    "id_hw_platform",
    "id_fw_control_r11",
    "fw_main_version",
    "id_fw_main_version",
    "fw_comm_version",
):
    register(_key, _version)

_MODULE_REPORTED = Label("Modulversion gemeldet", "Module version reported")
_MODULE_UNKNOWN = Label("Modulstatus unbekannt", "Module status unknown")


def _afci_module(entry: Entry, words: Words) -> Result:
    return _MODULE_REPORTED if 0 < words[0] < 0xFFFF else _MODULE_UNKNOWN


register("id_gap_r12", _afci_module, options=(_MODULE_REPORTED, _MODULE_UNKNOWN))

# present, but no interpretation is closed
for _key in (
    "id_gap_r13",
    "id_gap_r16",
    "id_r22",
    "model_selector",
    "fan_config",
    "inverter_generation",
    "battery_power_raw",
    "battery2_current",
    "battery2_negative_current",
    "grid_power_factor",
    "ev_charge_power_r709",
    *(f"pv_ext_r{n}" for n in range(722, 733)),
):
    register(_key, constant(UNCONFIRMED), options=(UNCONFIRMED,))


def _count(many: str, many_en: str) -> Rule:
    def rule(entry: Entry, words: Words) -> Result:
        n = owned(entry, words)
        if n == 0xFF:
            return NOT_REPORTED
        return Label(f"{n} {many}", f"{n} {many_en}")

    return rule


register("mppt_count", _count("MPPT", "MPPT"))
register("phase_count", _count("Phasen", "phases"))

_ONE_INPUT = Label("1 Batterieeingang", "1 battery input")
_TWO_INPUTS = Label("2 Batterieeingänge (dokumentiert)", "2 battery inputs (documented)")


def _battery_inputs(entry: Entry, words: Words) -> Result:
    if words[0] in (0, 1):
        return _ONE_INPUT
    return _TWO_INPUTS if words[0] == 2 else NOT_ASSIGNED


for _key in ("battery_input_code", "id_battery_input_code"):
    register(_key, _battery_inputs, options=(_ONE_INPUT, _TWO_INPUTS, NOT_ASSIGNED))

_PHASES = {
    0: Label("Dreiphasig", "Three-phase"),
    1: Label("Split Phase", "Split phase"),
    2: Label("Zweiphasig 60° (dokumentiert)", "Two-phase 60° (documented)"),
}
register("output_phase_arrangement", by_field(_PHASES), options=options_of(_PHASES, UNKNOWN))
_REGION = {0: Label("EU", "EU"), 1: Label("UL", "UL"), 2: Label("JP", "JP")}
register("grid_standard_region", by_field(_REGION), options=options_of(_REGION, UNKNOWN))

_DSP_UNCONFIRMED = Label("Unbestätigt", "Unconfirmed")


def _dsp_protocol(entry: Entry, words: Words) -> Result:
    word = words[0]
    if word == 0xFFFF:
        return NOT_REPORTED
    if word == 0:
        return _DSP_UNCONFIRMED
    return Label(f"DSP-Protokollkennung {word:04X}", f"DSP protocol ID {word:04X}")


register("dsp_protocol_version", _dsp_protocol)

# -- topology ---------------------------------------------------------------------------------

_STANDALONE = Label("Einzelbetrieb", "Standalone")
_PARALLEL = Label("Parallelbetrieb", "Parallel")
_MASTER = Label("Master", "Master")
_SLAVE = Label("Slave", "Slave")


def _parallel_enable(entry: Entry, words: Words) -> Result:
    return _PARALLEL if words[0] & 1 else _STANDALONE


def _parallel_master(entry: Entry, words: Words) -> Result:
    if not words[0] & 1:
        return _STANDALONE
    return _MASTER if words[0] & 2 else _SLAVE


register("parallel_enable", _parallel_enable, mode="attributes")
register("parallel_master", _parallel_master, mode="attributes")
_THREE_PHASE = Label("Dreiphasig", "Three-phase")
register("parallel_phase", constant(_THREE_PHASE), options=(_THREE_PHASE,))

# -- status -----------------------------------------------------------------------------------

_STATE = {
    0: Label("Standby", "Standby"),
    1: Label("Selbstprüfung", "Self-check"),
    2: Label("Normalbetrieb (gemeldet)", "Normal (reported)"),
    3: Label("Alarm (gemeldet)", "Alarm (reported)"),
    4: Label("Fehler (gemeldet)", "Fault (reported)"),
    5: Label("Aktivierung (buildabhängig)", "Activating (build-dependent)"),
}
register("operating_state", by_field(_STATE), options=options_of(_STATE, UNKNOWN))
_SWITCH = {0: Label("Aus", "Off"), 1: Label("An", "On")}
register("power_switch", by_field(_SWITCH), options=options_of(_SWITCH, UNKNOWN))

# -- relays -----------------------------------------------------------------------------------

OFF = Label("Aus", "Off")
ON = Label("An", "On")
for _key in (
    "relay_inverter",
    "relay_grid",
    "relay_generator",
    "relay_grid_supply",
    "dry_contact_1",
    "dry_contact_2",
):
    register(_key, on_off(OFF, ON), mode="attributes")

_RELAY_BITS = (
    (0, "Wechselrichter", "inverter"),
    (2, "Netz", "grid"),
    (3, "GEN", "GEN"),
    (4, "Versorgung", "supply"),
    (7, "Kontakt 1", "contact 1"),
    (8, "Kontakt 2", "contact 2"),
)
RELAY_KNOWN_MASK = 0x019D
_ALL_RELAYS_OFF = Label("Alle gemeldeten Relais aus", "All reported relays off")
_EXTRA_BITS = Label("Zusätzliche Rückmeldebits", "Additional reported bits")


def _relay_state(entry: Entry, words: Words) -> Result:
    word = words[0]
    active = [(de, en) for n, de, en in _RELAY_BITS if word >> n & 1]
    unknown = word & ~RELAY_KNOWN_MASK & 0xFFFF
    attrs: dict[str, Any] = {
        "known_mask": f"0x{RELAY_KNOWN_MASK:04X}",
        "active": [en for _, en in active],
        "unknown_bits": f"0x{unknown:04X}",
    }
    if not word:
        return _ALL_RELAYS_OFF, attrs
    if not active:
        return _EXTRA_BITS, attrs
    de = "An: " + "/".join(d for d, _ in active)
    en = "On: " + "/".join(e for _, e in active)
    if unknown:
        de, en = f"{de} + {_EXTRA_BITS.de}", f"{en} + {_EXTRA_BITS.en}"
    return Label(de, en), attrs


register("relay_state_raw", _relay_state)

# -- warnings and faults ----------------------------------------------------------------------

STATE_LIMIT = 255  # Home Assistant refuses longer states


def _codes(letter: str, base: int, word: int) -> list[str]:
    return [f"{letter}{base + b + 1}" for b in range(16) if word >> b & 1]


def _report_label(codes: list[str]) -> tuple[Label, dict[str, Any]]:
    named = [describe(c) for c in codes]
    unknown = [i["code"] for i in named if i["name"] == "Unknown"]
    attrs: dict[str, Any] = {
        "active_codes": codes,
        "named_reports": [i for i in named if i["name"] != "Unknown"],
        "unknown_bits": unknown,
    }
    if not codes:
        return NONE_REPORTED, attrs
    if len(unknown) == len(codes):
        return UNKNOWN_REPORT, attrs
    text = ", ".join(
        f"{i['code']} {i['name']}".rstrip() if i["name"] != "Unknown" else i["code"] for i in named
    )
    if len(text) > STATE_LIMIT:
        text = ", ".join(codes)
    if len(text) > STATE_LIMIT:
        text = text[: STATE_LIMIT - 1].rsplit(", ", 1)[0] + "…"
    return Label(text, text), attrs


def _word_report(letter: str, base: int) -> Rule:
    def rule(entry: Entry, words: Words) -> Result:
        return _report_label(_codes(letter, base, words[0]))

    return rule


register("alarm_word_1", _word_report("W", 0))
register("alarm_word_2", _word_report("W", 16))
for _i in range(4):
    register(f"fault_word_{_i + 1}", _word_report("F", 16 * _i))


def _aggregate(kind: str) -> Rule:
    def rule(entry: Entry, words: Words) -> Result:
        text = format_active(kind, list(words))
        codes = [p.split(" ", 1)[0] for p in text.split(", ")] if text != "none" else []
        label, attrs = _report_label(
            [c for i, w in enumerate(words) for c in _codes(kind[0].upper(), 16 * i, w)]
        )
        attrs["alarms"] = active_items(text)
        attrs["codes_shown"] = codes
        return label, attrs

    return rule


register("active_warnings", _aggregate("warnings"))
register("active_faults", _aggregate("faults"))

_NOT_REPORTED = Label("Nicht gemeldet", "Not reported")
_REPORTED = Label("Gemeldet", "Reported")
for _key in (
    "alarm_fan",
    "alarm_grid_phase",
    "alarm_battery_missing",
    "alarm_parallel_comm",
    "alarm_meter_comm",
    "fault_f13",
    "fault_f29",
    "fault_f41",
):
    register(_key, on_off(_NOT_REPORTED, _REPORTED), mode="attributes")

# -- reported placeholders and measurements ---------------------------------------------------


def _placeholder(placeholder: int = 0) -> Rule:
    """Zero-filled (or fixed-word) producers: the proved word is "not provided", any other word
    is an unconfirmed value."""

    def rule(entry: Entry, words: Words) -> Result:
        return NOT_PROVIDED if all(w == placeholder for w in words) else UNCONFIRMED_VALUE

    return rule


_PLACEHOLDER_OPTIONS = (NOT_PROVIDED, UNCONFIRMED_VALUE)
# measured sensors with a unit keep their number; the placeholder state is an attribute
register("temp_dc_transformer", _placeholder(1250), mode="attributes")
register("bms_offgrid_charge_limit", _placeholder(), mode="attributes")
register("bms_offgrid_discharge_limit_raw", _placeholder(), mode="attributes")
for _key in (
    "battery2_soc",
    "battery2_voltage",
    "battery2_power",
    "battery2_temperature",
    "grid_apparent_internal",
    "grid_apparent_external_r620",
    "inverter_apparent_r637",
    "load_current_invalid_a",
    "load_current_invalid_b",
    "load_current_invalid_c",
    "load_apparent_r654",
    "pv3_power",
    "pv4_power",
    "pv3_voltage",
    "pv3_current",
    "pv4_voltage",
    "pv4_current",
    "pv1_energy_today_r530",
    "pv2_energy_today_r531",
    "pv3_energy_today_r532",
    "pv4_energy_today_r533",
    "pv_total_power_r718",
    "pv5_voltage_r719",
    "pv6_voltage_r720",
    "pv7_voltage_r721",
):
    register(_key, _placeholder(), options=_PLACEHOLDER_OPTIONS)


def _ac_solar(entry: Entry, words: Words) -> Result:
    watts = words[0] * 10  # C070/1175 magnitude encoding; location and direction unresolved
    return Label(f"AC-Solar: Betrag {watts} W", f"AC-Solar: magnitude {watts} W")


for _key in ("ac_solar_power_raw_a", "ac_solar_power_raw_b", "ac_solar_power_raw_c"):
    register(_key, _ac_solar)
