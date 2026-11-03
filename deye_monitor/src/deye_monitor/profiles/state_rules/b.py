"""State rules, part B: Review33 mapping rows 100..199 (see states.py).

Labels name the state of a setting or report, never a health verdict. Rules scoped to the
C070/1175 build carry ``scope: C070/1175`` (the profile has no build discriminator).
"""

from __future__ import annotations

from typing import Any

from deye_monitor.profiles.schema import Entry
from deye_monitor.profiles.states import (
    UNCONFIRMED,
    UNKNOWN,
    Label,
    bit,
    by_field,
    constant,
    on_off,
    options_of,
    owned,
    register,
    state,
)

SCOPE: dict[str, Any] = {"scope": "C070/1175"}
UNDOCUMENTED: dict[str, Any] = {"encoding_status": "undocumented"}
NOT_SET_ATTRS: dict[str, Any] = {**UNDOCUMENTED, "effect": "unresolved"}

OFF = Label("Aus", "Off")
ON = Label("An", "On")
# Presentation vocabulary for 0xFFFF on R82/R84/R87 only: no storage origin of the word is proved.
NOT_SET = Label("Nicht gesetzt (Wirkung unbekannt)", "Not set (effect unknown)")
UNCONFIRMED_ENCODING = Label("Unbestätigte Codierung", "Unconfirmed encoding")


def _tail(prefix: Label, *, de: str, en: str) -> Label:
    return Label(f"{prefix.de}: {de}", f"{prefix.en}: {en}")


# -- placeholders with an unresolved interpretation ---

UNRESOLVED = (
    *(f"pv_ext_r{n}" for n in range(733, 749)),
    "grid_side_energy_today_r501",
    "grid_connected_time_today",
    "gen_runtime_today_r539",
    "gen_min_power_retention",
    "parallel_balancing_stop_soc_raw",  # R33-15: documented label only, effect unresolved
    "parallel_config_2_raw",
    "bms_soh_raw",
    "bms_software_version",
    "bms_capacity_reported",
    "bms_hardware_version",
    "ct_phase_sequence_result",
    "ct_direction_bit7_raw",
    "ct_direction_bit8_raw",
    "grid_reactive_power_a_raw",
    "grid_reactive_power_b_raw",
    "grid_reactive_power_c_raw",
    "inverter_reactive_power_a_raw",
    "inverter_reactive_power_b_raw",
    "inverter_reactive_power_c_raw",
    "dsp_operating_hours_raw",
    "reported_comm_version_r8",
    "grid_standard_version",
    "safety_type_raw",
    "inverter_series_raw",
    "rtc_source_raw",
    "lcd_mcu_type_raw",
    "control_mcu_type_raw",
)
_BINARY_UNRESOLVED = {"ct_direction_bit7_raw", "ct_direction_bit8_raw"}
for _key in UNRESOLVED:
    register(
        _key,
        constant(UNCONFIRMED),
        mode="attributes" if _key in _BINARY_UNRESOLVED else "text",
        options=None if _key in _BINARY_UNRESOLVED else (UNCONFIRMED,),
    )

# -- configuration enums ---

_ONOFF = {0: OFF, 1: ON}
register("remote_run_enable", by_field(_ONOFF), options=options_of(_ONOFF, UNKNOWN))

_CHEM = {
    0: Label("Blei/interne Kennlinie", "Lead/internal curve"),
    1: Label("Lithium/BMS", "Lithium/BMS"),
}
register("battery_chemistry", by_field(_CHEM), options=options_of(_CHEM, UNKNOWN))

_BASIS = {
    0: Label("Spannung", "Voltage"),
    1: Label("SOC", "SOC"),
    2: Label("Keine Batterie", "No Battery"),
}


@state("battery_operating_basis", options=options_of(_BASIS, UNKNOWN))
def _basis(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    return _BASIS.get(words[0], UNKNOWN), SCOPE  # 0/1/2 closed for C070/1175 only (R33-10)


_PORT = {
    0: Label("Generator", "Generator"),
    1: Label("Smart Load", "Smart Load"),
    2: Label("AC Coupling", "AC coupling"),
}
register("gen_port_usage", by_field(_PORT), options=options_of(_PORT, UNKNOWN))


# the legacy decoder masks bit 0: only the whole words 0 and 1 are a boolean, the rest is null
@state("gen_on_grid_input", mode="attributes", valid=lambda w: w[0] in (0, 1))
def _gen_on_grid(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    return {0: OFF, 1: ON}.get(words[0], UNCONFIRMED_ENCODING), SCOPE


_EXPORT = {
    0: Label("Selling First", "Selling First"),
    1: Label("Zero Export to Load", "Zero Export to Load"),
    2: Label("Zero Export to CT", "Zero Export to CT"),
}
register("export_mode_setting", by_field(_EXPORT), options=options_of(_EXPORT, UNKNOWN))

_FORCE = {0: Label("Auto", "Auto"), 1: ON}
register("force_off_grid_reported", by_field(_FORCE), options=options_of(_FORCE, UNKNOWN))

# -- Time of Use ---

register("tou_enable", on_off(OFF, ON), mode="attributes")
register("tou_spain_mode", on_off(OFF, ON), mode="attributes")

_DAYS_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_DAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@state("tou_weekdays")
def _weekdays(entry: Entry, words: tuple[int, ...]) -> Label:
    mask = (words[0] >> 1) & 0x7F
    if mask in (0, 0x7F):
        return Label("Täglich", "Every day")
    picked = [i for i in range(7) if mask >> i & 1]
    return Label(
        "Liste: " + "/".join(_DAYS_DE[i] for i in picked),
        "List: " + "/".join(_DAYS_EN[i] for i in picked),
    )


def _soc_rule(entry: Entry, words: tuple[int, ...]) -> Label | tuple[Label, dict[str, Any]]:
    word = words[0]
    if 0 <= word <= 100:
        return Label(f"SOC-Ziel: {word} %", f"SOC target: {word} %"), SCOPE
    return UNCONFIRMED_ENCODING


_NO_CHARGE = Label("Laden aus GRID/GEN nicht freigegeben", "GRID/GEN charging not allowed")
_TOU_BRANCH = 146  # bit 8 selects the Spain branch for every slot
_SPAIN_BIT = 8


def _spain_listed(word: int) -> list[str]:
    return [n for b, n in ((4, "CH"), (3, "BU"), (2, "GM")) if bit(word, b)]


def _flags_rule(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    # words = (slot flags[, R146]). The Spain sub-modes (GM bit 2, BU bit 3, CH bit 4, priority
    # 4>3>2) apply only while R146 bit 8 selects the Spain branch; the GRID/GEN allow bits (0, 1)
    # only otherwise. Without R146 the stored bits are described and the mode stays unresolved.
    word = words[0]
    grid, gen = bit(word, 0), bit(word, 1)
    spain = _spain_listed(word)
    stored: dict[str, Any] = {
        "stored_spain_bits": spain,
        "stored_grid_gen_bits": [n for n, v in (("GRID", grid), ("GEN", gen)) if v],
    }
    if len(words) < 2:
        parts = [*stored["stored_grid_gen_bits"], *spain]
        listed = " / ".join(parts)
        return (
            Label(
                f"Gespeicherte Bits: {listed or 'keine'} (Modus unaufgelöst)",
                f"Stored bits: {listed or 'none'} (mode unresolved)",
            ),
            {**stored, "branch": "unresolved"},
        )
    if bit(words[1], _SPAIN_BIT):
        if spain:
            listed = " / ".join(spain)
            return (
                Label(
                    f"Spain Mode: {listed} (Priorität 4>3>2)",
                    f"Spain Mode: {listed} (priority 4>3>2)",
                ),
                {**stored, "branch": "spain"},
            )
        return (
            Label("Spain Mode: Untermodus unaufgelöst", "Spain Mode: submode unresolved"),
            {**stored, "branch": "spain"},
        )
    attrs = {**stored, "branch": "normal"}
    if grid and gen:
        return Label("Freigegeben: GRID+GEN", "Allowed: GRID+GEN"), attrs
    if grid:
        return Label("Freigegeben: GRID", "Allowed: GRID"), attrs
    if gen:
        return Label("Freigegeben: GEN", "Allowed: GEN"), attrs
    return _NO_CHARGE, attrs


for _n in range(1, 7):
    register(f"tou_slot{_n}_soc_raw", _soc_rule)
    register(f"tou_slot{_n}_flags_raw", _flags_rule, context=(_TOU_BRANCH,))

# -- external signals and island mode ---

_SIGNAL = {
    0: OFF,
    1: Label("Grid Signal", "Grid Signal"),
    2: Label("Gen Signal", "Gen Signal"),
    3: Label("Beide Anforderungen", "Both requests"),
}


@state("external_signal_reported", options=options_of(_SIGNAL, UNKNOWN))
def _external(entry: Entry, words: tuple[int, ...]) -> Label:
    return UNKNOWN if words[0] == 0xFFFF else _SIGNAL[words[0] & 3]


@state("signal_island_mode_reported", options=options_of({2: OFF, 3: ON}, UNKNOWN))
def _island(entry: Entry, words: tuple[int, ...]) -> Label:
    if words[0] == 0xFFFF:
        return UNKNOWN
    return {2: OFF, 3: ON}.get((words[0] >> 8) & 3, UNKNOWN)


@state("mppt_scanning_reported", mode="attributes")
def _mppt_scan(entry: Entry, words: tuple[int, ...]) -> Label:
    if words[0] == 0xFFFF:
        return UNKNOWN
    return ON if bit(words[0], 2) else OFF


# -- BMS ---

_NO_ALARM = Label("Kein Alarm gemeldet", "No alarm reported")
_ALARM = Label("Alarm gemeldet", "Alarm reported")
_NO_FAULT = Label("Kein Fehler gemeldet", "No fault reported")
_FAULT = Label("Fehler gemeldet", "Fault reported")
register(
    "bms_alarm_word",
    lambda e, w: _ALARM if w[0] else _NO_ALARM,  # a zero is "none reported", not "healthy"
    options=(_NO_ALARM, _ALARM),
)
register("bms_fault_word", lambda e, w: _FAULT if w[0] else _NO_FAULT, options=(_NO_FAULT, _FAULT))
register("bms_force_charge", on_off(OFF, ON), mode="attributes")
register("bms_sleep", on_off(OFF, ON), mode="attributes")


@state("bms_flags_raw")
def _bms_flags(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    # Low byte = status field; bits 8..15 are BMS brand metadata in V105.8 (older documents used
    # the byte differently), never a status flag and no proof of communication or health.
    word = words[0]
    low, brand = word & 0xFF, word >> 8
    force, sleep = bit(low, 1), bit(low, 3)
    unknown_low = low & ~0x0A
    attrs: dict[str, Any] = {
        "high_byte_metadata": brand,
        "unknown_status_bits": f"0x{unknown_low:02X}",
    }
    if force and sleep:
        label = Label("Gemeldet: Zwangsladung und Sleep", "Reported: Force Charge and Sleep")
    elif force:
        label = Label("Gemeldet: Zwangsladung", "Reported: Force Charge")
    elif sleep:
        label = Label("Gemeldet: Sleep", "Reported: Sleep")
    elif unknown_low:
        label = Label("Weitere Statusflags gemeldet", "Additional status flags reported")
    else:
        label = Label("Keine bekannten Statusflags", "No known status flags")
    return label, attrs


_PROTOCOLS = {
    0: "Generic CAN (PYLON/SOLAX)",
    1: "Tianbangda RS485 Modbus",
    2: "KOK",
    3: "Keith",
    4: "Tuopai",
    5: "Pylon RS485",
    6: "Jielisi RS485",
    7: "Xinwangda RS485",
    8: "Xinruineng RS485",
    9: "Tianbangda RS485",
    10: "Shenggao Dianqi CAN",
}


@state("bms_protocol_reported")
def _bms_protocol(entry: Entry, words: tuple[int, ...]) -> Label:
    code = words[0]
    if code == 0xFFFF:
        return UNKNOWN
    if code == 0:
        return Label("Generic CAN (PYLON/SOLAX-Protokoll)", "Generic CAN (PYLON/SOLAX protocol)")
    if code in _PROTOCOLS:
        return Label(f"Dokumentiert: {_PROTOCOLS[code]}", f"Documented: {_PROTOCOLS[code]}")
    return Label(f"Protokoll {code} (Name unbestätigt)", f"Protocol {code} (name unconfirmed)")


# -- DRM and CT self-check ---


_DRM_ENABLE = 178  # bits 12-13: 2 = Off, 3 = On; 0/1 establish no explicit setting


@state("drm_status_raw", context=(_DRM_ENABLE,))
def _drm(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, object]]:
    """The reported DRM flags (R544 bits 0-8), qualified by the DRM setting (R178) when it was
    read with them. No statement about wiring, electrical level or an applied power limit."""
    word = words[0]
    known = [i for i in range(9) if bit(word, i)]
    unknown_bits = word & 0xFE00
    # R178 whole-word 0xFFFF is the invalid sentinel: no enable state can be read from it
    context = words[1] if len(words) > 1 and words[1] != 0xFFFF else None
    pair = (context >> 12) & 3 if context is not None else None
    setting = {2: "off", 3: "on"}.get(pair, "unknown") if pair is not None else "unknown"
    extra: dict[str, object] = {
        "reported_modes": [f"DRM{i}" for i in known],
        "unknown_bits": unknown_bits,
        "enable_setting": setting,
    }
    if setting == "off":  # the reported bits stay in the attributes
        return Label("DRM: Aus (Einstellung)", "DRM: Off (setting)"), extra
    if setting == "unknown":
        return Label("DRM-Freigabe unbekannt", "DRM enable state unknown"), extra
    if known:
        names = "/".join(f"DRM{i}" for i in known)
        if unknown_bits:
            return Label(
                f"Gemeldet: {names} + weitere Flags", f"Reported: {names} + additional flags"
            ), extra
        return Label(f"Gemeldet: {names}", f"Reported: {names}"), extra
    if unknown_bits:
        return Label("Weitere DRM-Flags gemeldet", "Additional DRM flags reported"), extra
    return Label("Keine DRM-Flags gemeldet", "No DRM flags reported"), extra


for _i in range(9):
    register(f"drm{_i}_reported", on_off(OFF, ON), mode="attributes")


@state("ct_selfcheck_result_raw")
def _ct_result(entry: Entry, words: tuple[int, ...]) -> Label:
    ok = [n for i, n in enumerate(("L1", "L2", "L3")) if bit(words[0], i)]
    if not ok:  # bit 0..2 clear: no success reported, not necessarily a failed test
        return Label("Kein Prüferfolg gemeldet", "No successful check reported")
    names = "/".join(ok)
    return Label(f"Prüferfolg gemeldet: {names}", f"Successful check reported: {names}")


_CT_OK = {
    False: Label("Kein Prüferfolg gemeldet", "No successful check reported"),
    True: Label("Prüferfolg gemeldet", "Successful check reported"),
}
for _phase in ("a", "b", "c"):
    register(
        f"ct_{_phase}_selfcheck_success",
        lambda e, w: _CT_OK[bool(owned(e, w))],
        mode="attributes",
    )
register(
    "ct_a_direction_reversed",
    on_off(
        Label("Keine Umkehr gemeldet", "No reversal reported"),
        Label("Umkehr gemeldet", "Reversal reported"),
    ),
    mode="attributes",
)

# -- identifiers and meter ---


@state("communication_address_r74")
def _address(entry: Entry, words: tuple[int, ...]) -> Label:
    word = words[0]
    if 1 <= word <= 247:
        return Label(f"Adresse {word}", f"Address {word}")
    return UNKNOWN


_SOURCE = {0: Label("CT", "CT"), 1: Label("Zähler", "Meter")}


@state("grid_monitoring_source", options=options_of(_SOURCE))
def _grid_source(entry: Entry, words: tuple[int, ...]) -> Label:
    return _SOURCE[bit(words[0], 0)]


_METER = {
    0: Label("Reserviert", "Reserved"),
    1: Label("Eastron", "Eastron"),
    3: Label("Smart.TX", "Smart.TX"),
}
_METER_DOC = Label(
    "Dokumentierte Auswahl: CHNT / Deye-3P / AccuEnergy",
    "Documented selection: CHNT / Deye-3P / AccuEnergy",
)


@state("meter_manufacturer_reported", options=options_of(_METER, _METER_DOC, UNKNOWN))
def _meter(entry: Entry, words: tuple[int, ...]) -> Label:
    if words[0] in (2, 4, 5):
        return _METER_DOC
    return _METER.get(words[0], UNKNOWN)


@state("ct_ratio_reported")
def _ct_ratio(entry: Entry, words: tuple[int, ...]) -> Label:
    word = words[0]
    if word == 0xFFFF:
        return UNKNOWN
    if 100 <= word <= 20000 and word % 100 == 0:
        return Label(f"{word}:1", f"{word}:1")
    return Label(f"{word}:1 (außerhalb Editorbereichs)", f"{word}:1 (outside editor range)")


# -- R82..R87, R235, R236: undocumented words, labelled by the setting they name ---


@state("self_check_time_setting")
def _self_check(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    word = words[0]
    if word == 0xFFFF:
        return NOT_SET, UNDOCUMENTED
    if word <= 360:
        return Label("Zeitbedeutung unbestätigt", "Delay interpretation unconfirmed"), UNDOCUMENTED
    return Label("Unbekannter Wert", "Unknown value"), UNDOCUMENTED


_ANTI_ISLAND = (
    _tail(Label("Anti-Island Mode", "Anti-Island Mode"), de="An", en="On"),
    _tail(Label("Anti-Island Mode", "Anti-Island Mode"), de="Aus", en="Off"),
)


# R83: exactly 1 enables it (C070/1175); every other word counts as off.
register(
    "anti_islanding_enable_raw",
    lambda e, w: (_ANTI_ISLAND[0] if w[0] == 1 else _ANTI_ISLAND[1], SCOPE),
    options=_ANTI_ISLAND,
)

_PF = {0: OFF, 1: Label("PF", "PF"), 2: Label("Q", "Q")}


@state("reactive_pf_option_raw", options=options_of(_PF, NOT_SET, UNKNOWN))
def _pf(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    if words[0] == 0xFFFF:
        return NOT_SET, NOT_SET_ATTRS
    return _PF.get(words[0], UNKNOWN), UNDOCUMENTED


_RCD = (
    _tail(Label("Interner RCD", "Internal RCD"), de="An", en="On"),
    _tail(Label("Interner RCD", "Internal RCD"), de="Aus", en="Off"),
)
# R86: any nonzero word, including 0xFFFF, enables it (C070/1175), unlike R83.
register(
    "rcd_enable_raw",
    lambda e, w: (_RCD[0] if w[0] != 0 else _RCD[1], SCOPE),
    options=_RCD,
)

_RISO = (
    Label("Isolationsüberwachung (Riso): An", "Insulation check (Riso): On"),
    Label("Isolationsüberwachung (Riso): Aus", "Insulation check (Riso): Off"),
)


@state("riso_enable_raw", options=(*_RISO, NOT_SET, UNKNOWN))
def _riso(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    word = words[0]
    if word == 0xFFFF:
        return NOT_SET, UNDOCUMENTED
    if word in (0, 1):
        return _RISO[0 if word else 1], {}
    return UNKNOWN, {}


_TRACK = (
    Label(
        "Netzphasen-Nachführung (Track grid phase): Aus (Standard)",
        "Track grid phase: Off (default)",
    ),
    Label("Netzphasen-Nachführung (Track grid phase): An", "Track grid phase: On"),
    Label("Netzphasen-Nachführung (Track grid phase): Aus", "Track grid phase: Off"),
)


@state("track_grid_phase_raw", options=_TRACK)
def _track(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    return _TRACK[{0: 0, 1: 1}.get(words[0], 2)], SCOPE


_IT = {0: OFF, 1: ON}


@state("it_system_raw", options=options_of(_IT, UNKNOWN))
def _it_system(entry: Entry, words: tuple[int, ...]) -> tuple[Label, dict[str, Any]]:
    # the setting word is a configuration, not proof of the physical earthing
    return _IT.get(words[0], UNKNOWN), (UNDOCUMENTED if words[0] not in _IT else {})


B_KEYS = (
    "pv_ext_r733",
    "pv_ext_r734",
    "pv_ext_r735",
    "pv_ext_r736",
    "pv_ext_r737",
    "pv_ext_r738",
    "pv_ext_r739",
    "pv_ext_r740",
    "pv_ext_r741",
    "pv_ext_r742",
    "pv_ext_r743",
    "pv_ext_r744",
    "pv_ext_r745",
    "pv_ext_r746",
    "pv_ext_r747",
    "pv_ext_r748",
    "grid_side_energy_today_r501",
    "grid_connected_time_today",
    "gen_runtime_today_r539",
    "remote_run_enable",
    "battery_chemistry",
    "battery_operating_basis",
    "gen_port_usage",
    "gen_on_grid_input",
    "gen_min_power_retention",
    "parallel_balancing_stop_soc_raw",
    "parallel_config_2_raw",
    "tou_enable",
    "tou_weekdays",
    "tou_spain_mode",
    "tou_slot1_soc_raw",
    "tou_slot1_flags_raw",
    "tou_slot2_soc_raw",
    "tou_slot2_flags_raw",
    "tou_slot3_soc_raw",
    "tou_slot3_flags_raw",
    "tou_slot4_soc_raw",
    "tou_slot4_flags_raw",
    "tou_slot5_soc_raw",
    "tou_slot5_flags_raw",
    "tou_slot6_soc_raw",
    "tou_slot6_flags_raw",
    "external_signal_reported",
    "bms_alarm_word",
    "bms_fault_word",
    "bms_force_charge",
    "bms_sleep",
    "bms_flags_raw",
    "bms_protocol_reported",
    "bms_soh_raw",
    "bms_software_version",
    "bms_capacity_reported",
    "bms_hardware_version",
    "drm_status_raw",
    "drm0_reported",
    "drm1_reported",
    "drm2_reported",
    "drm3_reported",
    "drm4_reported",
    "drm5_reported",
    "drm6_reported",
    "drm7_reported",
    "drm8_reported",
    "ct_selfcheck_result_raw",
    "ct_a_selfcheck_success",
    "ct_b_selfcheck_success",
    "ct_c_selfcheck_success",
    "ct_phase_sequence_result",
    "ct_a_direction_reversed",
    "ct_direction_bit7_raw",
    "ct_direction_bit8_raw",
    "grid_reactive_power_a_raw",
    "grid_reactive_power_b_raw",
    "grid_reactive_power_c_raw",
    "inverter_reactive_power_a_raw",
    "inverter_reactive_power_b_raw",
    "inverter_reactive_power_c_raw",
    "dsp_operating_hours_raw",
    "reported_comm_version_r8",
    "grid_standard_version",
    "safety_type_raw",
    "inverter_series_raw",
    "rtc_source_raw",
    "lcd_mcu_type_raw",
    "control_mcu_type_raw",
    "communication_address_r74",
    "grid_monitoring_source",
    "meter_manufacturer_reported",
    "ct_ratio_reported",
    "force_off_grid_reported",
    "signal_island_mode_reported",
    "mppt_scanning_reported",
    "export_mode_setting",
    "self_check_time_setting",
    "anti_islanding_enable_raw",
    "reactive_pf_option_raw",
    "rcd_enable_raw",
    "riso_enable_raw",
    "track_grid_phase_raw",
    "it_system_raw",
)
