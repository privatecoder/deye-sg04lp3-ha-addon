"""Pure decoding of profile entries from raw 16-bit words (SPEC §5, GRUNDLAGEN §1.4-1.6).

``None`` means *unknown*: a required word is missing (SR-04), the value is invalid, or the
energy resolution is not known. Nothing is ever defaulted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from deye_monitor.profiles.schema import Entry

Value = int | float | str | bool | None

_PRINTABLE = range(0x20, 0x7F)
_ROUND_DIGITS = 6
# The only kWh-per-count factors R65/override can produce (profiles.energy).
ENERGY_FACTORS = (0.1, 1.0)


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def _masked(entry: Entry, word: int) -> tuple[int, int]:
    """(value, bit width) of the field described by mask/shift."""
    if entry.mask is None:
        return word, 16
    value = (word & entry.mask) >> entry.shift
    return value, (entry.mask >> entry.shift).bit_length()


def decode(
    entry: Entry, words_by_addr: Mapping[int, int], *, energy_resolution: float | None = None
) -> Value:
    """Decode ``entry`` from the words of one cycle."""
    try:
        words = [words_by_addr[a] for a in entry.addresses]
    except KeyError:
        return None
    if any(not 0 <= w <= 0xFFFF for w in words):
        raise ValueError("raw words must be unsigned 16-bit values")
    if energy_resolution is not None and (
        isinstance(energy_resolution, bool)  # True == 1.0 would slip through the membership test
        or not isinstance(energy_resolution, float)
        or energy_resolution not in ENERGY_FACTORS
    ):
        raise ValueError(f"energy_resolution must be one of {ENERGY_FACTORS}")
    t = entry.type
    if t == "ascii":
        return _ascii(words)
    if t in ("u32", "s32"):
        raw = (words[1] << 16) | words[0]  # words are ordered low, high
        if t == "s32":
            raw = _signed(raw, 32)
        return _scale(entry, raw, energy_resolution)
    field, bits = _masked(entry, words[0])
    if t == "u16":
        return _scale(entry, field, energy_resolution)
    if t == "s16":
        return _scale(entry, _signed(field, bits), energy_resolution)
    if t == "bitfield":
        if entry.mask is not None and entry.mask & (entry.mask - 1) == 0:
            return field != 0
        return field
    if t == "enum":
        assert entry.enum is not None
        return entry.enum.get(field, f"unknown_{field}")
    if t == "hhmm":
        hours, minutes = divmod(field, 100)
        return f"{hours:02d}:{minutes:02d}" if hours <= 23 and minutes <= 59 else None
    return field  # raw


def _ascii(words: list[int]) -> str | None:
    data = [b for w in words for b in (w >> 8, w & 0xFF)]
    if any(b not in _PRINTABLE for b in data):
        return None
    return bytes(data).decode("ascii")


def _scale(entry: Entry, raw: int, energy_resolution: float | None) -> int | float | None:
    """``value = (raw + raw_offset) * scale``; no scale means a raw diagnostic value."""
    if entry.scale_source is not None:
        if energy_resolution is None:
            return None
        scale = energy_resolution
    elif entry.scale is None:
        return raw
    else:
        scale = entry.scale
    value = raw + entry.raw_offset
    if scale == 1 and value == int(value):
        return int(value)
    result = round(value * scale, _ROUND_DIGITS)
    # finite inputs can still overflow (e.g. scale 1e308): unknown, never 0
    return result if math.isfinite(result) else None
