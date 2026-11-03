"""Which released settings the installation description suggests (SPEC §9.2, 0.5.0).

A pure function of the saved plant features and their answer provenance, like the sensor template.
It only proposes the selection of the release dialog: nothing is released, written or changed by
it, and no register value of the device takes part (live words only produce consistency hints
elsewhere).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from deye_monitor.profiles.features import Features
from deye_monitor.write_ops import BY_KEY, OPERATION_KEYS, TOU_SLOTS, WEEKDAYS

_SOC_PROTECTION = ("battery_shutdown_soc", "battery_low_soc", "battery_restart_soc")
_CURRENTS = ("battery_charge_current", "battery_discharge_current")
_VOLTAGE_PROTECTION = (
    "battery_shutdown_voltage",
    "battery_low_voltage",
    "battery_restart_voltage",
)
# internal control (no communicating BMS): the charge curve and the estimator parameters; the
# equalization trio needs a lead-acid answer the description does not have, so it stays unselected
_INTERNAL_BATTERY = (
    "battery_float_voltage",
    "battery_absorption_voltage",
    "battery_temperature_compensation",
    "battery_capacity",
    "battery_empty_voltage",
    "battery_internal_resistance",
    "battery_charge_efficiency",
)
_TOU = (
    "tou_enable",
    *(f"tou_weekday_{tag}" for tag, *_ in WEEKDAYS),
    *(
        f"tou_slot{n}_{x}"
        for n in range(1, TOU_SLOTS + 1)
        for x in ("start", "power", "soc", "grid_charge")
    ),
)


def suggested_operations(
    features: Features, confirmed: Collection[str] | None = None
) -> tuple[str, ...]:
    """Operation keys (canonical order) the description of one inverter points at.

    ``confirmed`` are the questions the owner answered explicitly (or accepted with the template,
    ``Record.confirmed``): only those count. An unanswered question holds a raw default of the
    model, which is no answer, so a predicate that depends on it is not satisfied. ``None`` treats
    every field as answered (a pure function of a complete description)."""

    def answer(name: str) -> Any:
        return getattr(features, name) if confirmed is None or name in confirmed else None

    out: set[str] = set()
    utility = answer("utility_grid") is True
    island = answer("utility_grid") is False
    port_use = answer("grid_port_use")
    battery = answer("battery") is True
    bms = answer("bms")
    basis = answer("battery_control_basis")
    gen_mode = answer("gen_port_mode")
    mppt = answer("mppt1_used") is True or answer("mppt2_used") is True
    # the source LOCATION decides: a charging source on the GRID terminal is a utility or a
    # generator there; a generator on the GEN terminal is a different setting family (R125)
    grid_terminal_source = (utility and port_use == "utility") or port_use == "generator"
    smart_load = gen_mode == "smart_load"
    if battery and basis in ("soc", "voltage"):
        out.update(_CURRENTS)
        if grid_terminal_source:
            out.update(("grid_charge_enable", "grid_charge_current"))
    if battery and basis == "soc":
        out.update(_SOC_PROTECTION)
        if grid_terminal_source:
            out.add("grid_charge_start_soc")
        if smart_load:
            out.update(("smart_load_off_soc", "smart_load_on_soc"))
    if battery and bms is False:
        out.update(_INTERNAL_BATTERY)
    if battery and basis == "voltage":
        out.update(_VOLTAGE_PROTECTION)
        if grid_terminal_source:
            out.add("grid_charge_start_voltage")
    if battery and basis == "voltage" and smart_load:
        out.update(("smart_load_off_voltage", "smart_load_on_voltage"))
    if gen_mode == "generator":
        out.update(("generator_max_run_time", "generator_cooling_time"))
    # R129 / R123 / R124 are GEN-terminal controls (R133 = 0): only a generator on the GEN port
    # suggests them; a generator on the GRID terminal uses the R130 / R128 / R126 / R127 family
    if battery and gen_mode == "generator":
        out.add("generator_charge_enable")
        if basis == "voltage":
            out.add("generator_charge_start_voltage")
        elif basis == "soc":
            out.add("generator_charge_soc")
    if gen_mode == "smart_load":
        out.add("smart_load_force")
    if gen_mode == "ac_coupled":
        out.add("ac_coupling_upper_frequency")
    if gen_mode in ("generator", "smart_load", "ac_coupled"):
        out.add("gen_port_usage")
    if battery and gen_mode == "generator":
        out.add("generator_charge_current")
    if battery and answer("tou_used") is True:
        out.update(_TOU)
        if basis == "voltage":
            out.update(f"tou_slot{n}_voltage" for n in range(1, TOU_SLOTS + 1))
        if gen_mode == "generator":  # the generator-charge flag belongs to a generator on GEN
            out.update(f"tou_slot{n}_gen_charge" for n in range(1, TOU_SLOTS + 1))
    policy = answer("export_policy")
    # the allowance is a typed number without a model default (empty = None): a confirmed 0 W
    # allows no sales, so no sale-power row is suggested; Solar Sell needs a confirmed positive one
    limit = features.export_limit_w
    zero_allowance = limit == 0
    export_permitted = limit is not None and limit > 0
    if utility:
        out.add("grid_reconnection_time")
        if policy in ("export_allowed", "zero_export_load", "zero_export_external"):
            out.add("export_mode")
        if policy == "export_allowed" and not zero_allowance:
            out.add("export_max_power")
        if policy in ("zero_export_load", "zero_export_external"):
            out.add("zero_export_bias")
        if mppt and policy == "export_allowed" and not zero_allowance:
            out.add("pv_sale_max_power")
        # Solar Sell (R145) is the PV-sale switch of the zero-export modes (R142 = 1 / 2)
        if mppt and export_permitted and policy in ("zero_export_load", "zero_export_external"):
            out.add("pv_sale_enable")
    if battery and bms is True:
        out.add("bms_loss_fault")
    if utility and answer("external_measurement") not in (None, "none", "unknown"):
        out.add("external_ct_ratio")
    # the peak-shaving powers (R190 / R191) have no preselection predicate in the contract
    if battery or island:
        out.update(("force_off_grid", "signal_island_mode"))
    if answer("external_signal_use") in (
        "grid_contactor",
        "generator_start",
        "shared_grid_and_generator_logic",
    ):
        out.add("external_signal")
    if mppt:
        out.add("mppt_scan")
    return tuple(k for k in OPERATION_KEYS if k in out and k in BY_KEY)
