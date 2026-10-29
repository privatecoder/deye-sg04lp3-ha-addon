"""Energy resolution (SPEC §5): R65 selects 0.1 or 1 kWh per count for the §5.1 whitelist."""

from __future__ import annotations

from deye_monitor.config import EnergyResolution

R65_TENTH_KWH_MAX = 0x0104


def resolution_from_r65(raw: int) -> float:
    """V105.8 R65 (DSP protocol version): ``<= 0x0104`` means 0.1 kWh, otherwise 1 kWh."""
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"not a 16-bit word: {raw}")
    return 0.1 if raw <= R65_TENTH_KWH_MAX else 1.0


def effective_resolution(override: EnergyResolution, r65: int | None) -> float | None:
    """kWh per count, or ``None`` when it is not known yet (no energy values then).

    A missing R65 (timeout, not read yet) is never treated as 0; an explicit override wins.
    """
    if override is EnergyResolution.TENTH:
        return 0.1
    if override is EnergyResolution.ONE:
        return 1.0
    return None if r65 is None else resolution_from_r65(r65)
