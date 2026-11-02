"""Energy resolution (SPEC §5): the kWh per count of the §5.1 whitelist."""

from __future__ import annotations

from deye_monitor.config import EnergyResolution

PROFILE_DEFAULT_KWH = 0.1  # SG04LP3: every checked MCU2 build (1151, 1175, 1185)


def effective_resolution(override: EnergyResolution) -> float:
    """kWh per count. An explicit override wins; automatic is the SG04LP3 profile default.

    Owner decision (0.5.1, Codex r65-1): MCU2 1151, 1175 and 1185 all count 0.1 kWh and none has an
    R65 resolution selector, so R65 is a diagnostic only and never selects the factor.
    """
    if override is EnergyResolution.TENTH:
        return 0.1
    if override is EnergyResolution.ONE:
        return 1.0
    return PROFILE_DEFAULT_KWH
