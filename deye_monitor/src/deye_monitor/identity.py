"""Inverter identity (SPEC §8.1 SR-05/SR-09): serial normalisation and the persisted binding.

Binding precedence: ``expected_serial`` (option) > persisted binding > first valid serial seen
(then persisted). A binding is only ever replaced by an explicit ``rebind`` or by a changed
``expected_serial`` option; a different device on a bound inverter is quarantined, never adopted.

The binding file fails closed: a missing file is a first installation, an existing but damaged,
unreadable or incompatible file blocks every binding (``problem``) until an explicit ``reset``.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from deye_monitor.store import JsonStore

SERIAL_WORDS = 5  # R3..R7, two ASCII characters per register, high byte first
SOURCES = ("expected", "first_seen")
PROBLEM_CORRUPT = "identity_store_corrupt"
PROBLEM_UNWRITABLE = "identity_store_unwritable"


def normalize_serial(words: Sequence[int]) -> str | None:
    """Ten printable ASCII characters from R3..R7, else ``None`` (invalid, never guessed)."""
    if len(words) < SERIAL_WORDS:
        return None
    data = [b for w in words[:SERIAL_WORDS] for b in (w >> 8, w & 0xFF)]
    if any(not 0x20 <= b < 0x7F for b in data):
        return None
    text = bytes(data).decode("ascii")
    return text if text.strip() == text and text else None


def valid_serial(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 2 * SERIAL_WORDS
        and value.strip() == value
        and all(0x20 <= ord(c) < 0x7F for c in value)
    )


def valid_identity_document(doc: dict[str, Any]) -> bool:
    bindings = doc.get("bindings", {})
    if set(doc) - {"bindings"} or not isinstance(bindings, dict):
        return False
    for inverter_id, entry in bindings.items():
        if not isinstance(inverter_id, str) or not isinstance(entry, dict):
            return False
        if set(entry) != {"serial", "source"}:
            return False
        if not valid_serial(entry["serial"]) or entry["source"] not in SOURCES:
            return False
    return True


def identity_store(path: Any) -> JsonStore:
    return JsonStore(path, validate=valid_identity_document)


class IdentityBook:
    """Bindings per logical inverter id, persisted in ``/data/identity.json``."""

    def __init__(
        self, store: JsonStore, expected: Mapping[str, str | None], *, provisional: bool = False
    ) -> None:
        """``provisional``: the book belongs to a generation that is not accepted yet. Every change
        (a new ``expected_serial``, a first-seen binding) stays in memory; ``accept()`` writes it
        once the plant is durable. A process that dies before that leaves the file untouched, and
        the next start derives the expected bindings again from the accepted plant."""
        self._store = store
        self._expected = {k: v for k, v in expected.items() if v}
        self._pending: dict[str, Any] | None = None
        self._provisional = provisional
        self.problem: str | None = PROBLEM_CORRUPT if store.corrupt else None
        if self.problem is None:
            self._apply_expected()

    @property
    def _data(self) -> dict[str, Any]:
        return self._pending if self._pending is not None else self._store.data

    def _write(self, doc: dict[str, Any]) -> None:
        if self._provisional:
            self._pending = doc
        else:
            self._store.commit(doc)

    def accept(self) -> None:
        """The generation is accepted: write what was held back (an unwritable file is reported by
        ``problem`` like everywhere else)."""
        if not self._provisional:
            return
        self._provisional = False
        pending, self._pending = self._pending, None
        if pending is not None and self.problem is None:
            try:
                self._store.commit(pending)
            except OSError:
                self._pending = pending  # still what this generation uses
                self.problem = PROBLEM_UNWRITABLE

    def _apply_expected(self) -> None:
        doc = copy.deepcopy(self._data)
        bindings: dict[str, dict[str, str]] = doc.setdefault("bindings", {})
        changed = False
        for inverter_id, serial in self._expected.items():
            current = bindings.get(inverter_id)
            if current != {"serial": serial, "source": "expected"}:
                bindings[inverter_id] = {"serial": serial, "source": "expected"}
                changed = True
        if changed:
            try:
                self._write(doc)
            except OSError:
                self.problem = PROBLEM_UNWRITABLE

    @property
    def _bindings(self) -> dict[str, dict[str, str]]:
        return self._data.get("bindings", {})  # type: ignore[no-any-return]

    def bound(self, inverter_id: str) -> str | None:
        if self.problem == PROBLEM_CORRUPT:
            return None
        entry = self._bindings.get(inverter_id)
        return entry["serial"] if entry else None

    def source(self, inverter_id: str) -> str | None:
        entry = self._bindings.get(inverter_id)
        return entry["source"] if entry else None

    def flush(self) -> None:
        if self.problem is None:
            try:
                self._write(copy.deepcopy(self._data))
            except OSError:
                self.problem = PROBLEM_UNWRITABLE

    def _with(self, mutate: Any) -> None:
        doc = copy.deepcopy(self._data)
        mutate(doc.setdefault("bindings", {}))
        self._write(doc)  # raises OSError: the in-memory binding does not change then

    def bind_first_seen(self, inverter_id: str, serial: str) -> None:
        if self.problem is not None:
            raise ValueError("identity store is blocked")
        if inverter_id in self._bindings:
            raise ValueError("already bound")
        self._with(lambda b: b.__setitem__(inverter_id, {"serial": serial, "source": "first_seen"}))

    def rebind(self, inverter_id: str) -> bool:
        """Drop a first-seen binding so the next valid serial is bound. A binding that comes from
        the ``expected_serial`` option can only be changed through the option: returns False."""
        if self.problem is not None or inverter_id in self._expected:
            return False
        if inverter_id in self._bindings:
            self._with(lambda b: b.pop(inverter_id, None))
        return True

    def reset(self, inverter_id: str | None = None) -> set[str] | None:
        """Explicit user action.

        ``inverter_id`` given: only this inverter's record is removed (transactionally); the file
        and the other bindings are untouched. If the file as a whole is damaged only the global
        reset (``inverter_id=None``) is possible: it starts a new, empty file (the old one is kept
        as ``identity.json.corrupt``) and EVERY inverter is bound again. Returns the inverter
        ids that lost their binding (``None`` = all)."""
        if self.problem == PROBLEM_CORRUPT:
            if inverter_id is not None:
                raise ValueError("the identity file is damaged: only a global reset is possible")
            self._store.reset({"bindings": {}})
            self.problem = None
            self._apply_expected()
            return None
        if inverter_id is None:
            self._with(lambda b: b.clear())
            self.problem = None
            self._apply_expected()
            return None
        if self.problem is not None:
            self._write(copy.deepcopy(self._data))  # raises while still unwritable
            self.problem = None
        if inverter_id in self._bindings:
            self._with(lambda b: b.pop(inverter_id, None))
        self._apply_expected()
        return {inverter_id}
