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


GridPortUse = Literal["utility", "generator", "unused"]
GenPortMode = Literal["unused", "generator", "smart_load", "ac_coupled"]

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
}


class Features(BaseModel):
    """Per-inverter plant features. Defaults describe a grid-tied single inverter with a
    battery without CAN BMS and both MPPT inputs in use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    utility_grid: bool = True
    grid_port_use: GridPortUse = "utility"
    battery: bool = True
    bms: bool = False
    gen_port_mode: GenPortMode = "unused"
    mppt1_used: bool = True
    mppt2_used: bool = True
    parallel: bool = False

    def value(self, feature: Feature) -> bool | str:
        return getattr(self, feature.value)  # type: ignore[no-any-return]
