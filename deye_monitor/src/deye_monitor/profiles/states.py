"""Plain-language primary states (Review33 R33-14).

The primary value of a coded sensor names the *state of the setting or report* in plain words
("Anti-Island Mode: On", "None reported"), never a health verdict. The raw word stays available
only as attributes (``raw_hex``, ``raw_decimal``, ``registers``) and in the (i) help. A rule
decides from the raw words of one read; ``None`` (a missing word) is unavailable, never a label.

Two modes per key:

* ``text`` (default): the English label is the sensor state, the German one an attribute. With
  ``options`` the Home Assistant sensor is an enum sensor with exactly those labels.
* ``attributes``: the entity keeps its decoded value (a binary sensor, a measured number with a
  unit) and only receives the attributes, including the plain-language state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from deye_monitor.profiles.schema import Entry


@dataclass(frozen=True, slots=True)
class Label:
    de: str
    en: str


@dataclass(frozen=True, slots=True)
class Presented:
    """One rendered state: the label and the attributes published next to it."""

    label: Label
    attributes: dict[str, Any] = field(default_factory=dict)


# A rule gets the entry and the raw words in ``entry.addresses`` order. It returns the label,
# ``(label, extra attributes)``, or ``None`` when the words allow no statement (unavailable).
Rule = Callable[[Entry, tuple[int, ...]], Label | tuple[Label, dict[str, Any]] | None]


@dataclass(frozen=True, slots=True)
class StateSpec:
    key: str
    rule: Rule
    mode: Literal["text", "attributes"] = "text"
    options: tuple[Label, ...] | None = None  # complete label list of an enum sensor
    # words of other registers the rule needs to qualify its statement: they are read with the
    # entry and appended to ``words`` when all of them are present (otherwise the rule sees only
    # its own words and must leave the effective mode unresolved)
    context: tuple[int, ...] = ()
    # attributes mode only: the whole own words must pass for the primary value to be published
    valid: Callable[[tuple[int, ...]], bool] | None = None


STATES: Final[dict[str, StateSpec]] = {}

UNCONFIRMED: Final = Label("Nicht bestätigt", "Unconfirmed")
UNKNOWN: Final = Label("Unbekannt", "Unknown")
NOT_REPORTED: Final = Label("Nicht gemeldet", "Not reported")


def register(
    key: str,
    rule: Rule,
    *,
    mode: Literal["text", "attributes"] = "text",
    options: tuple[Label, ...] | None = None,
    context: tuple[int, ...] = (),
    valid: Callable[[tuple[int, ...]], bool] | None = None,
) -> None:
    if key in STATES:
        raise ValueError(f"duplicate state rule for {key}")
    STATES[key] = StateSpec(key, rule, mode, options, context, valid)


def state(
    key: str,
    *,
    mode: Literal["text", "attributes"] = "text",
    options: tuple[Label, ...] | None = None,
    context: tuple[int, ...] = (),
    valid: Callable[[tuple[int, ...]], bool] | None = None,
) -> Callable[[Rule], Rule]:
    """Decorator registering a rule for the sensor ``key``."""

    def wrap(rule: Rule) -> Rule:
        register(key, rule, mode=mode, options=options, context=context, valid=valid)
        return rule

    return wrap


# -- helpers for the rules -----------------------------------------------------------------------


def owned(entry: Entry, words: tuple[int, ...]) -> int:
    """The field the entry owns: the masked, shifted first word (the whole word without a mask)."""
    word = words[0]
    return word if entry.mask is None else (word & entry.mask) >> entry.shift


def bit(word: int, n: int) -> int:
    return (word >> n) & 1


def by_field(table: Mapping[int, Label], otherwise: Label = UNKNOWN) -> Rule:
    """Rule: the owned field looked up in ``table``."""

    def rule(entry: Entry, words: tuple[int, ...]) -> Label:
        return table.get(owned(entry, words), otherwise)

    return rule


def on_off(off: Label, on: Label) -> Rule:
    """Rule for an owned bit/flag: zero is ``off``, anything else ``on``."""

    def rule(entry: Entry, words: tuple[int, ...]) -> Label:
        return on if owned(entry, words) else off

    return rule


def constant(label: Label) -> Rule:
    def rule(entry: Entry, words: tuple[int, ...]) -> Label:
        return label

    return rule


def options_of(table: Mapping[int, Label], *extra: Label) -> tuple[Label, ...]:
    """The complete, de-duplicated label list of a table plus fallback labels."""
    return tuple(dict.fromkeys([*table.values(), *extra]))


# -- rendering -----------------------------------------------------------------------------------


def context_addresses(key: str) -> tuple[int, ...]:
    """Extra registers the rule of ``key`` reads to qualify its statement."""
    spec = STATES.get(key)
    return spec.context if spec else ()


def present(entry: Entry, words_by_addr: Mapping[int, int]) -> Presented | None:
    """The plain-language state of ``entry`` from one read, or ``None`` (unavailable)."""
    spec = STATES.get(entry.key)
    if spec is None or entry.derive is not None:
        return None
    try:
        words = tuple(words_by_addr[a] for a in entry.addresses)
    except KeyError:
        return None
    extra_words = tuple(words_by_addr[c] for c in spec.context if c in words_by_addr)
    with_context = extra_words if len(extra_words) == len(spec.context) else ()
    result = spec.rule(entry, words + with_context)
    if result is None:
        return None
    label, extra = result if isinstance(result, tuple) else (result, {})
    attributes: dict[str, Any] = {
        "state_de": label.de,
        "raw_hex": [f"0x{w:04X}" for w in words],
        "raw_decimal": list(words),
        "registers": list(entry.addresses),
    }
    attributes.update(extra)
    return Presented(label, attributes)


def primary_valid(entry: Entry, words_by_addr: Mapping[int, int]) -> bool:
    """False when an attributes-mode rule rejects the whole own words: the primary is then null."""
    spec = STATES.get(entry.key)
    if spec is None or spec.valid is None:
        return True
    try:
        entry_words = tuple(words_by_addr[a] for a in entry.addresses)
    except KeyError:
        return True
    return spec.valid(entry_words)


def attributes_key(key: str) -> str:
    """Key of the attribute object in the group payload."""
    return f"{key}_attributes"


# the rule tables register themselves on import
from deye_monitor.profiles.state_rules import a, b  # noqa: E402,F401
