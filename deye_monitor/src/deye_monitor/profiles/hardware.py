"""Hardware profile (SPEC §9.1): what the hardware can do, independent of how it is used.

The R0/R22/R24 values are *expectations for plausibility checks*. A mismatch yields a
``capability_conflict`` for the status view; it never unlocks additional channels.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from deye_monitor.profiles.schema import ProfileError, _read_yaml, format_error


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StringsPerMppt(_Model):
    """Physical string inputs per MPPT; two strings on one MPPT stay one measurement channel."""

    mppt1: Annotated[int, Field(ge=1)]
    mppt2: Annotated[int, Field(ge=1)]


class HardwareProfile(_Model):
    name: str
    mppt_channels: Annotated[int, Field(ge=1)]
    strings_by_rated_kw: dict[int, StringsPerMppt]
    battery_inputs: Annotated[int, Field(ge=0)]
    gen_port: bool
    expected_device_type: int  # R0
    also_accepted_device_types: list[int] = []  # further R0 codes for the same family
    expected_mppt_count: int  # R22 high byte
    expected_phase_count: int  # R22 low byte
    battery_input_codes: dict[int, int]  # R24 code -> number of battery inputs
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> HardwareProfile:
        if self.expected_mppt_count != self.mppt_channels:
            raise ValueError("expected_mppt_count must equal mppt_channels")
        if not self.strings_by_rated_kw:
            raise ValueError("strings_by_rated_kw must not be empty")
        return self


@dataclass(frozen=True, slots=True)
class CapabilityConflict:
    code: str
    detail: str


@functools.cache
def load_hardware(name: str) -> HardwareProfile:
    _, data = _read_yaml(name, "hardware")
    try:
        profile = HardwareProfile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(format_error(exc)) from None
    if profile.name != name:
        raise ProfileError(f"hardware name {profile.name!r} does not match file {name!r}")
    return profile


NOT_REPORTED = 0xFFFF  # a word the device does not fill in (per field, never a value)


def unreported_capabilities(words: Mapping[int, int]) -> list[str]:
    """R0, R22 and R24 that read exactly 0xFFFF: capability unknown, not a conflict."""
    return [
        name
        for name, addr in (("device_type", 0), ("mppt_phases", 22), ("battery_input_code", 24))
        if words.get(addr) == NOT_REPORTED
    ]


def unknown_capability_codes(hardware: HardwareProfile, words: Mapping[int, int]) -> dict[str, int]:
    """R24 values the profile does not define (V105.8: only 0/1/2): raw value, no statement."""
    r24 = words.get(24)
    if r24 is None or r24 == NOT_REPORTED or r24 in hardware.battery_input_codes:
        return {}
    return {"battery_input_code": r24}


def capability_conflicts(
    hardware: HardwareProfile,
    words: Mapping[int, int],
    *,
    rated_kw: int | None = None,
) -> list[CapabilityConflict]:
    """Compare identity words with the hardware profile. Missing words are not conflicts.

    ``rated_kw`` is the model chosen by the user (5/6/8/10/12); R20/R21 are plausibilised
    against it. R28 is deliberately not interpreted ("4 = SG04" is not documented).
    """
    out: list[CapabilityConflict] = []
    r0 = words.get(0)
    accepted = {hardware.expected_device_type, *hardware.also_accepted_device_types}
    if r0 is not None and r0 != NOT_REPORTED and r0 not in accepted:
        out.append(
            CapabilityConflict(
                "device_type", f"R0=0x{r0:04x}, expected 0x{hardware.expected_device_type:04x}"
            )
        )
    r22 = words.get(22)
    if r22 is not None and r22 != NOT_REPORTED:
        mppt, phases = r22 >> 8, r22 & 0xFF
        if mppt != hardware.expected_mppt_count:
            out.append(
                CapabilityConflict(
                    "mppt_count", f"R22 reports {mppt} MPPT, profile has {hardware.mppt_channels}"
                )
            )
        if phases != hardware.expected_phase_count:
            out.append(
                CapabilityConflict(
                    "phase_count",
                    f"R22 reports {phases} phases, expected {hardware.expected_phase_count}",
                )
            )
    r24 = words.get(24)
    if r24 is not None and r24 != NOT_REPORTED:
        inputs = hardware.battery_input_codes.get(r24)
        # an undefined code (V105.8: 0/1/2 only) is no statement: unknown_capability_codes
        if inputs is not None and inputs > hardware.battery_inputs:
            out.append(
                CapabilityConflict(
                    "battery_inputs",
                    f"R24 reports {inputs} battery inputs, profile has {hardware.battery_inputs}",
                )
            )
    if rated_kw is not None:
        if rated_kw not in hardware.strings_by_rated_kw:
            out.append(CapabilityConflict("model", f"{rated_kw} kW is not a profile model"))
        elif 20 in words and 21 in words:
            watts = ((words[21] << 16) | words[20]) / 10
            if abs(watts - rated_kw * 1000) > rated_kw * 1000 * 0.1:
                out.append(
                    CapabilityConflict(
                        "rated_power", f"R20/R21 report {watts:.0f} W, model is {rated_kw} kW"
                    )
                )
    return out


def string_inputs(hardware: HardwareProfile, rated_kw: int) -> StringsPerMppt:
    return hardware.strings_by_rated_kw[rated_kw]


_ = yaml  # PyYAML is used through schema._read_yaml
