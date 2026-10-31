"""Named warnings and faults of the SG04LP3 (vendor alarm list, ``alarms.yaml``).

Wn is bit n-1 over R553/R554, Fn is bit n-1 over R555-R558. A bit the list does not name is shown
by its code alone, never dropped."""

from __future__ import annotations

import functools
import re
from importlib import resources
from typing import Any, Final

import yaml

NONE: Final = "none"
UNKNOWN: Final = "Unknown"
STATE_LIMIT: Final = 255  # Home Assistant refuses longer states
_CODE_RE: Final = re.compile(r"^([WF])(\d{1,2})\b")
_LETTER: Final = {"warnings": "W", "faults": "F"}


@functools.cache
def _table() -> dict[str, dict[int, dict[str, str]]]:
    raw = (resources.files("deye_monitor.profiles") / "alarms.yaml").read_bytes()
    data = yaml.safe_load(raw)
    return {kind: {int(n): dict(v) for n, v in data[kind].items()} for kind in _LETTER}


def active_codes(kind: str, words: list[int]) -> list[str]:
    """Codes of the set bits over ``words`` (low word first), e.g. ``["F29", "F41"]``."""
    letter = _LETTER[kind]
    return [
        f"{letter}{16 * i + bit + 1}"
        for i, word in enumerate(words)
        for bit in range(16)
        if word >> bit & 1
    ]


def describe(code: str) -> dict[str, str]:
    """{code, name, remedy} of one code; unnamed bits say so."""
    m = _CODE_RE.match(code)
    kind = "warnings" if m and m.group(1) == "W" else "faults"
    info = _table()[kind].get(int(m.group(2))) if m else None
    if info is None:
        return {"code": code, "name": UNKNOWN, "remedy": ""}
    return {"code": code, "name": info["name"], "remedy": info["remedy"]}


def format_active(kind: str, words: list[int]) -> str:
    """'F29 Parallel CANBus fault, F41 Parallel system stop', ``none`` without a set bit. Past the
    state limit only the codes are listed."""
    codes = active_codes(kind, words)
    if not codes:
        return NONE
    text = ", ".join(f"{c} {_known_name(c)}".rstrip() for c in codes)
    if len(text) > STATE_LIMIT:
        text = ", ".join(codes)
    if len(text) > STATE_LIMIT:  # cut at a whole code, never in the middle of one
        text = text[: STATE_LIMIT - 1].rsplit(", ", 1)[0] + "…"
    return text


def _known_name(code: str) -> str:
    name = describe(code)["name"]
    return "" if name == UNKNOWN else name


def active_items(text: object) -> list[dict[str, Any]]:
    """The attribute list for a published state text (``[]`` for ``none`` or anything else)."""
    if not isinstance(text, str) or text == NONE:
        return []
    return [describe(part.split(" ", 1)[0]) for part in text.split(", ") if _CODE_RE.match(part)]
