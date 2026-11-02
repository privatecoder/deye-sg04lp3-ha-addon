"""Plant features (SPEC §9.2): how an inverter is used, as opposed to what the hardware can do."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Feature(StrEnum):
    UTILITY_GRID = "utility_grid"
    GRID_PORT_USE = "grid_port_use"
    BATTERY = "battery"
    BMS = "bms"
    GEN_PORT_MODE = "gen_port_mode"
    MPPT1_USED = "mppt1_used"
    MPPT2_USED = "mppt2_used"
    PARALLEL = "parallel"
    EXPORT_POLICY = "export_policy"
    EXTERNAL_MEASUREMENT = "external_measurement"
    EXTERNAL_SIGNAL_USE = "external_signal_use"
    BATTERY_CONTROL_BASIS = "battery_control_basis"
    TOU_USED = "tou_used"
    EXPORT_LIMIT_W = "export_limit_w"
    GRID_LIMIT_W = "grid_limit_w"
    GENERATOR_CAPACITY_W = "generator_capacity_w"
    EV_CHARGER_LIMIT_W = "ev_charger_limit_w"
    EV_OFFGRID_RESERVE_SOC = "ev_offgrid_reserve_soc"
    EV_CHARGER = "ev_charger"


GridPortUse = Literal["utility", "generator", "unused"]
GenPortMode = Literal["unused", "generator", "smart_load", "ac_coupled"]
ExportPolicy = Literal[
    "export_allowed", "zero_export_load", "zero_export_external", "not_applicable", "unknown"
]
ExternalMeasurement = Literal[
    "none", "ct", "meter", "ct_and_second_meter", "meter_and_second_meter", "unknown"
]
ExternalSignalUse = Literal[
    "none",
    "grid_contactor",
    "generator_start",
    "shared_grid_and_generator_logic",
    "other",
    "unknown",
]
BatteryControlBasis = Literal["voltage", "soc", "none", "unknown"]
EvCharger = Literal["none", "grid_port", "load_port", "unknown"]

# Allowed values per feature; booleans are listed as such.
FEATURE_VALUES: dict[Feature, tuple[bool | str, ...]] = {
    Feature.UTILITY_GRID: (True, False),
    Feature.GRID_PORT_USE: ("utility", "generator", "unused"),
    Feature.BATTERY: (True, False),
    Feature.BMS: (True, False),
    Feature.GEN_PORT_MODE: ("unused", "generator", "smart_load", "ac_coupled"),
    Feature.MPPT1_USED: (True, False),
    Feature.MPPT2_USED: (True, False),
    Feature.PARALLEL: (True, False),
    Feature.EXPORT_POLICY: (
        "export_allowed",
        "zero_export_load",
        "zero_export_external",
        "not_applicable",
        "unknown",
    ),
    Feature.EXTERNAL_MEASUREMENT: (
        "none",
        "ct",
        "meter",
        "ct_and_second_meter",
        "meter_and_second_meter",
        "unknown",
    ),
    Feature.EXTERNAL_SIGNAL_USE: (
        "none",
        "grid_contactor",
        "generator_start",
        "shared_grid_and_generator_logic",
        "other",
        "unknown",
    ),
    Feature.BATTERY_CONTROL_BASIS: ("voltage", "soc", "none", "unknown"),
    Feature.TOU_USED: (True, False),
    Feature.EV_CHARGER: ("none", "grid_port", "load_port", "unknown"),
    Feature.EXPORT_LIMIT_W: (),
    Feature.GRID_LIMIT_W: (),
    Feature.GENERATOR_CAPACITY_W: (),
    Feature.EV_CHARGER_LIMIT_W: (),
    Feature.EV_OFFGRID_RESERVE_SOC: (),
}
# Optional installation numbers: the per-installation limits the write contracts intersect with
# the model caps (approved export allowance, grid connection envelope, generator rating, the lower
# of EV charger rating and circuit limit; watts) and the commissioned EV off-grid reserve (SOC %).
# ``None`` = not entered; a setting that needs one stays unavailable until it is. Whole numbers,
# ``(min, max)``. An approved export allowance and the EV reserve may be an explicit 0 (no export /
# no reserve), which is a different installation state from "not entered"; the ratings of a source
# or charger are positive.
NUMBER_FEATURES: dict[Feature, tuple[int, int]] = {
    Feature.EXPORT_LIMIT_W: (0, 100_000),
    Feature.GRID_LIMIT_W: (100, 100_000),
    Feature.GENERATOR_CAPACITY_W: (100, 100_000),
    Feature.EV_CHARGER_LIMIT_W: (100, 100_000),
    Feature.EV_OFFGRID_RESERVE_SOC: (0, 100),
}
# The questions added with 0.5.0 start as "unknown" / "no": nothing is preselected for them until
# the installation description answers (SPEC 9.2).


class Features(BaseModel):
    """Per-inverter plant features. Defaults describe a grid-tied single inverter with a
    battery without CAN BMS and both MPPT inputs in use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    utility_grid: bool = True
    grid_port_use: GridPortUse = "utility"
    battery: bool = True
    bms: bool = True
    gen_port_mode: GenPortMode = "unused"
    mppt1_used: bool = True
    mppt2_used: bool = True
    parallel: bool = False
    export_policy: ExportPolicy = "unknown"
    external_measurement: ExternalMeasurement = "unknown"
    external_signal_use: ExternalSignalUse = "unknown"
    battery_control_basis: BatteryControlBasis = "unknown"
    tou_used: bool = False
    ev_charger: EvCharger = "unknown"
    export_limit_w: int | None = None
    grid_limit_w: int | None = None
    generator_capacity_w: int | None = None
    ev_charger_limit_w: int | None = None
    ev_offgrid_reserve_soc: int | None = None

    def value(self, feature: Feature) -> bool | str:
        return getattr(self, feature.value)  # type: ignore[no-any-return]
