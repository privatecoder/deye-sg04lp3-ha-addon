"""Energy resolution (SPEC §5): the kWh per count of the §5.1 whitelist."""

from __future__ import annotations

from deye_monitor.config import EnergyResolution

PROFILE_DEFAULT_KWH = 0.1  # SG04LP3 profile policy for every firmware


def effective_resolution(override: EnergyResolution) -> float:
    """kWh per count. An explicit override wins; automatic is the SG04LP3 profile default.

    Owner decision (0.5.1): 0.1 kWh on every SG04LP3 firmware. The firmware analysis (Codex r65-1)
    supports it for MCU2 1151 and 1175 and found no R65 selector in a bounded search (1185: exports
    only). R65 is a diagnostic only and never selects the factor.
    """
    if override is EnergyResolution.TENTH:
        return 0.1
    if override is EnergyResolution.ONE:
        return 1.0
    return PROFILE_DEFAULT_KWH
