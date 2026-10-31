"""Sensor selection per inverter (SPEC §9.2, §9.6): persistence, migration and atomic change.

Effective selection = template(features) + added - removed. ``added``/``removed`` are the user's
explicit deviations from the template; they survive feature changes. Every change runs
validate -> persist -> adopt: a failure at any step leaves the old selection intact (in memory,
on disk and in the running inverter). The revision is monotone and global.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from deye_monitor.config import AppConfig, PollConfig
from deye_monitor.device import BOOTSTRAP_KEYS
from deye_monitor.profiles.features import FEATURE_VALUES, Feature, Features
from deye_monitor.profiles.hardware import HardwareProfile, load_hardware
from deye_monitor.profiles.plan import (
    ReadPlan,
    SelectionError,
    build_read_plan,
    selection_problems,
    template_selection,
)
from deye_monitor.profiles.schema import Profile, load_profile
from deye_monitor.store import JsonStore, LoadStatus

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FILE_NAME = "selection.json"


FEATURE_NAMES = frozenset(f.value for f in Feature)


class ChangeError(Exception):
    """A change was rejected; nothing was changed. ``key`` is a stable text key for the UI."""

    def __init__(self, key: str, status: int = 400, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.status = status
        self.params = params


@dataclass(frozen=True, slots=True)
class Problem:
    """A finding while loading/migrating the stored document (never dropped silently)."""

    code: str
    subject: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "subject": self.subject, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class InverterSpec:
    """What the selection needs to know about one inverter (no I/O)."""

    id: str
    profile: Profile
    hardware: HardwareProfile
    max_read_registers: int
    energy_auto: bool
    poll: PollConfig
    response_time_s: float = 0.25
    gap_s: float = 0.05


@dataclass(frozen=True, slots=True)
class Record:
    """The persisted user choice of one inverter."""

    features: Features = field(default_factory=Features)
    added: frozenset[str] = frozenset()
    removed: frozenset[str] = frozenset()
    # Features the user has answered explicitly (also with the default value): only unconfirmed
    # features may be pre-filled from device settings. Not evidence about the hardware.
    confirmed: frozenset[str] = frozenset()
    # False only for an inverter whose selection was never saved (new installation or a newly
    # added inverter, and only where the store requires a choice): it reads the mandatory
    # identity/status and bootstrap registers and nothing else until the user chose.
    chosen: bool = True


SpecsUndo = tuple[dict[str, InverterSpec], dict[str, Record], bool]


@dataclass(frozen=True, slots=True)
class InverterSelection:
    features: Features
    added: frozenset[str]
    removed: frozenset[str]
    template: frozenset[str]
    effective: frozenset[str]
    confirmed: frozenset[str] = frozenset()
    chosen: bool = True

    @property
    def record(self) -> Record:
        return Record(self.features, self.added, self.removed, self.confirmed, self.chosen)

    @property
    def active(self) -> frozenset[str]:
        """What the running application publishes and reads: nothing before the first choice."""
        return self.effective if self.chosen else frozenset()

    @property
    def deviations(self) -> dict[str, list[str]]:
        """Visible deviations from the template; intents the template already satisfies are
        ``redundant`` (kept, they apply again when the features change)."""
        return {
            "added": sorted(self.added - self.template),
            "removed": sorted(self.removed & self.template),
            "redundant": sorted((self.added & self.template) | (self.removed - self.template)),
        }


@dataclass(frozen=True, slots=True)
class Edit:
    """One change request for one inverter. Fields are applied in this order: replace, features,
    restore, reset, add, remove."""

    replace: Record | None = None
    features: Mapping[str, bool | str] | None = None
    restore_template: bool = False
    reset: frozenset[str] = frozenset()
    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()
    confirm: frozenset[str] = frozenset()
    choose: bool = False  # accept the template as it is (the edit then changes nothing else)


class Applier(Protocol):
    """Adopts a persisted selection in the running application (inverter loop and publisher)."""

    def prepare(
        self, inverter_id: str, selection: InverterSelection, *, selection_changed: bool
    ) -> None:
        """Validate everything the swap needs, BEFORE anything is persisted (raises, no effect)."""
        ...

    def apply(
        self,
        inverter_id: str,
        selection: InverterSelection,
        revision: int,
        *,
        selection_changed: bool,
    ) -> None:
        """The swap itself: validated state exchange; must not fail."""
        ...

    def revoke(self, inverter_id: str, reason: str) -> None:
        """Withdraw every release of the inverter (offline/blocked with ``reason``): used when a
        swap could be neither completed nor taken back, so it must not run on either state."""
        ...


@dataclass(frozen=True, slots=True)
class ChangeResult:
    revision: int
    changed: tuple[str, ...]  # inverters whose record changed
    selection_changed: tuple[str, ...]  # inverters whose effective selection changed


# -- pure logic -------------------------------------------------------------------------------


def make_features(values: Mapping[str, Any], base: Features | None = None) -> Features:
    """Strict feature parsing: only known features, only listed values, exact types."""
    merged: dict[str, Any] = (base or Features()).model_dump()
    for name, value in values.items():
        try:
            feature = Feature(name)
        except ValueError:
            raise ChangeError("unknown_feature", feature=str(name)[:64]) from None
        allowed = FEATURE_VALUES[feature]
        if not any(value == a and type(value) is type(a) for a in allowed):
            raise ChangeError("invalid_feature_value", feature=name)
        merged[name] = value
    return Features(**merged)


def resolve(spec: InverterSpec, record: Record) -> InverterSelection:
    """Effective selection of ``record``. Placeholders, unsupported and internal entries are
    rejected here, server-side, whatever a client sent; unresolved entries pass only as the raw
    diagnostics ``validate_selection`` allows."""
    template = template_selection(spec.profile, spec.hardware, record.features)
    effective = (template | record.added) - record.removed
    rejected = selection_problems(spec.profile, effective | record.added)
    if rejected:
        raise ChangeError("entry_not_selectable", rejected=dict(sorted(rejected.items())))
    return InverterSelection(
        record.features,
        record.added,
        record.removed,
        template,
        effective,
        record.confirmed,
        record.chosen,
    )


def plan_for(spec: InverterSpec, selection: InverterSelection) -> ReadPlan:
    """The read plan (including the mandatory and bootstrap reads) and its budget."""
    try:
        return build_read_plan(
            spec.profile,
            selection.active | BOOTSTRAP_KEYS,
            selection.features,
            max_read_registers=spec.max_read_registers,
            energy_auto=spec.energy_auto,
            poll=spec.poll,
            response_time_s=spec.response_time_s,
            gap_s=spec.gap_s,
        )
    except SelectionError as exc:
        raise ChangeError("entry_not_selectable", rejected=exc.rejected) from None


def apply_edit(spec: InverterSpec, current: Record, edit: Edit) -> Record:
    features, added, removed = current.features, set(current.added), set(current.removed)
    # a transfer never carries the source's answers: the target keeps its own confirmations
    confirmed = set(current.confirmed)
    unknown_features = sorted(n for n in edit.confirm if n not in FEATURE_NAMES)
    if unknown_features:
        raise ChangeError("unknown_feature", feature=unknown_features[0][:64])
    if edit.replace is not None:
        features, added, removed = (
            edit.replace.features,
            set(edit.replace.added),
            set(edit.replace.removed),
        )
    if edit.features:
        features = make_features(edit.features, features)
        confirmed |= set(edit.features)
    confirmed |= edit.confirm
    if edit.restore_template:
        added.clear()
        removed.clear()
    known = {e.key for e in spec.profile.entries if e.selectable}
    unknown = sorted((edit.reset | edit.add | edit.remove) - known)
    if unknown:
        raise ChangeError("unknown_entry", keys=unknown[:20])
    added -= edit.reset
    removed -= edit.reset
    added |= edit.add
    removed -= edit.add
    removed |= edit.remove
    added -= edit.remove
    # any edit is a decision of the user
    return Record(features, frozenset(added), frozenset(removed), frozenset(confirmed), True)


# -- persistence ------------------------------------------------------------------------------


RECOVERY_REQUIRED = "selection_recovery_required"


def _valid_body(body: dict[str, Any]) -> bool:
    revision = body.get("revision")
    inverters = body.get("inverters")
    return (
        isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision >= 0
        and isinstance(inverters, dict)
        and all(isinstance(k, str) and isinstance(v, dict) for k, v in inverters.items())
    )


def record_to_json(record: Record) -> dict[str, Any]:
    return {
        "features": record.features.model_dump(),
        "added": sorted(record.added),
        "removed": sorted(record.removed),
        "confirmed": sorted(record.confirmed),
        "chosen": record.chosen,
    }


def _migrate_record(spec: InverterSpec, raw: Mapping[str, Any], problems: list[Problem]) -> Record:
    """Stable keys: unknown/obsolete keys and bad values are reported and discarded."""
    feats: dict[str, Any] = {}
    raw_features = raw.get("features")
    if isinstance(raw_features, dict):
        for name, value in raw_features.items():
            try:
                feats.update(make_features({name: value}).model_dump(include={name}))
            except ChangeError as exc:
                problems.append(Problem(exc.key, f"{spec.id}.{str(name)[:64]}"))
    elif raw_features is not None:
        problems.append(Problem("invalid_features", spec.id))
    confirmed: set[str] = set()
    if "confirmed" in raw:
        raw_confirmed = raw["confirmed"]
        if isinstance(raw_confirmed, list):
            for name in raw_confirmed:
                if isinstance(name, str) and name in FEATURE_NAMES:
                    confirmed.add(name)
                else:
                    problems.append(Problem("unknown_feature", f"{spec.id}.{str(name)[:64]}"))
        else:
            problems.append(Problem("invalid_features", spec.id))
    selectable = {e.key for e in spec.profile.entries if e.selectable}
    sets: dict[str, set[str]] = {}
    for name in ("added", "removed"):
        out: set[str] = set()
        values = raw.get(name, [])
        if not isinstance(values, list):
            problems.append(Problem(f"invalid_{name}", spec.id))
            values = []
        for key in values:
            if isinstance(key, str) and key in selectable:
                out.add(key)
            else:
                problems.append(Problem("unknown_entry", f"{spec.id}.{str(key)[:64]}", name))
        sets[name] = out
    both = sets["added"] & sets["removed"]
    if both:
        problems.append(Problem("conflicting_entries", spec.id, ",".join(sorted(both))))
        sets["added"] -= both
    for extra in sorted(set(raw) - {"features", "added", "removed", "confirmed", "chosen"}):
        problems.append(Problem("unknown_field", f"{spec.id}.{str(extra)[:64]}"))
    return Record(
        Features(**feats),
        frozenset(sets["added"]),
        frozenset(sets["removed"]),
        frozenset(confirmed),
        raw.get("chosen") is True,
    )


SLOT_NAMES = ("selection.a.json", "selection.b.json")


SLOT_FORMAT = 2


def _digest(content: Mapping[str, Any]) -> str:
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _checksum(seq: int, body: dict[str, Any]) -> str:
    """Over everything that decides validity AND order: format, sequence number and body."""
    return _digest({"format": SLOT_FORMAT, "seq": seq, "body": body})


def _valid_slot(doc: dict[str, Any]) -> bool:
    body, seq = doc.get("body"), doc.get("seq")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1):
        return False
    if not (isinstance(body, dict) and _valid_body(body)):
        return False
    return doc.get("format") == SLOT_FORMAT and bool(doc.get("checksum") == _checksum(seq, body))


class SelectionStore:
    """Two slots, ``selection.a.json`` and ``selection.b.json``, each a complete, checksummed
    document with a monotone ``seq``. A change is accepted exactly when its slot is durable (atomic
    rename + fsync); the newest slot that is complete and valid is the accepted state, always. A
    write goes to the slot that does NOT hold it, so the accepted state is never touched by a
    failing write. Damaged or incomplete slots are ignored and reported, never used.
    """

    def __init__(
        self,
        directory: Path,
        specs: Mapping[str, InverterSpec],
        *,
        require_choice: bool = False,
    ) -> None:
        self.specs = dict(specs)
        self.require_choice = require_choice
        self.directory = directory
        self.path = directory / SLOT_NAMES[0]
        self._slots = [
            JsonStore(directory / name, schema_version=SCHEMA_VERSION, validate=_valid_slot)
            for name in SLOT_NAMES
        ]
        self.problems: list[Problem] = []
        self.revision = 0
        self.pruned = False  # records of removed inverters were dropped but not yet persisted
        # with ``require_choice`` an inverter without a saved record has not been chosen yet
        self.records: dict[str, Record] = {i: Record(chosen=not require_choice) for i in self.specs}
        self.source = "template"
        self._status = LoadStatus.MISSING
        self._seq = 0
        self._remove_stale_temp_files(directory)
        self._load()

    @property
    def status(self) -> LoadStatus:
        return self._status

    def set_specs(self, specs: Mapping[str, InverterSpec]) -> SpecsUndo:
        """The plant changed: records of removed inverters are dropped (in memory; ``pruned``
        asks the manager to persist that), new inverters start without a choice. Returns what is
        needed to take it back."""
        undo = (self.specs, dict(self.records), self.pruned)
        self.specs = dict(specs)
        for inverter_id in [i for i in self.records if i not in self.specs]:
            del self.records[inverter_id]
            self.pruned = True
        for inverter_id in self.specs:
            self.records.setdefault(inverter_id, Record(chosen=not self.require_choice))
        return undo

    def restore_specs(self, undo: SpecsUndo) -> None:
        self.specs, self.records, self.pruned = undo[0], dict(undo[1]), undo[2]

    def _remove_stale_temp_files(self, directory: Path) -> None:
        with contextlib.suppress(OSError):
            for tmp in directory.glob(".selection.*.json.*"):
                tmp.unlink(missing_ok=True)
            for tmp in directory.glob(f".{FILE_NAME}.*"):
                if not tmp.name.endswith((".prev", ".corrupt")):
                    tmp.unlink(missing_ok=True)

    # -- load ------------------------------------------------------------------------------

    def _newest_valid(self) -> int | None:
        valid = [
            (s.data["body"]["revision"], s.data["seq"], i)
            for i, s in enumerate(self._slots)
            if s.status is LoadStatus.VALID
        ]
        # revision and sequence rise together with every accepted change: order by both
        return max(valid)[2] if valid else None

    def _load(self) -> None:
        body: dict[str, Any] | None = None
        corrupt = [s for s in self._slots if s.status is LoadStatus.CORRUPT]
        for slot in corrupt:
            self.problems.append(
                Problem("selection_store_corrupt", slot.path.name, slot.problem or "")
            )
        newest = self._newest_valid()
        if newest is not None:
            self._seq = int(self._slots[newest].data["seq"])
            body, self.source = self._slots[newest].data["body"], "file"
            self._status = LoadStatus.VALID
            if corrupt:
                self.source = "recovered_previous"
                self.problems.append(Problem("selection_store_recovered_previous", FILE_NAME))
        elif corrupt:
            self._status = LoadStatus.CORRUPT
            self.problems.append(Problem("selection_store_template_used", FILE_NAME))
        if body is None:
            return
        self.revision = int(body["revision"])
        for inverter_id in sorted(body["inverters"]):
            spec = self.specs.get(inverter_id)
            if spec is None:
                self.problems.append(Problem("unknown_inverter", inverter_id[:64]))
                self.pruned = (
                    True  # the stale record is still on disk: removed before the next change
                )
                continue
            self.records[inverter_id] = _migrate_record(
                spec, body["inverters"][inverter_id], self.problems
            )
        for inverter_id, record in list(self.records.items()):
            try:  # a stored record that no longer resolves (profile change) falls back, reported
                resolve(self.specs[inverter_id], record)
            except ChangeError as exc:
                self.problems.append(Problem(exc.key, inverter_id, str(exc.params)[:200]))
                self.records[inverter_id] = Record(
                    record.features, confirmed=record.confirmed, chosen=record.chosen
                )

    # -- write -----------------------------------------------------------------------------

    def document(self, records: Mapping[str, Record], revision: int) -> dict[str, Any]:
        return {
            "revision": revision,
            "inverters": {i: record_to_json(r) for i, r in sorted(records.items())},
        }

    def _write_slot(self, index: int, body: dict[str, Any]) -> None:
        slot = self._slots[index]
        if slot.status is LoadStatus.CORRUPT and slot.path.exists():
            # a COPY first: if it cannot be kept, nothing is overwritten
            shutil.copyfile(slot.path, slot.path.with_name(slot.path.name + ".corrupt"))
        seq = self._seq + 1
        slot.commit(
            {"format": SLOT_FORMAT, "seq": seq, "checksum": _checksum(seq, body), "body": body}
        )
        self._seq = seq

    def _target_slot(self) -> int:
        newest = self._newest_valid()
        others = [i for i in (0, 1) if i != newest]
        # a damaged slot first (repaired, a copy is kept), then a missing one, then the older one
        order = {LoadStatus.CORRUPT: 0, LoadStatus.MISSING: 1, LoadStatus.VALID: 2}
        return min(others, key=lambda i: (order[self._slots[i].status], i))

    def commit(self, records: Mapping[str, Record], revision: int) -> None:
        """Make ``records`` the accepted state: durable or ``OSError`` with everything unchanged
        (the previous accepted slot is never touched by a failing write)."""
        self._write_slot(self._target_slot(), self.document(records, revision))
        self.records = dict(records)
        self.revision = revision
        self.source = "file"
        self._status = LoadStatus.VALID
        self.pruned = False
        self.problems = []  # reported once; the new document is clean


# -- manager ----------------------------------------------------------------------------------


class SelectionManager:
    """Single writer of the selection. One lock serialises changes; the revision check happens
    inside the lock, so two concurrent requests with the same revision cannot both win."""

    def __init__(self, store: SelectionStore) -> None:
        self.store = store
        self._lock = asyncio.Lock()
        self._applier: Applier | None = None

    @property
    def revision(self) -> int:
        return self.store.revision

    @property
    def specs(self) -> Mapping[str, InverterSpec]:
        return self.store.specs

    recovery_required: frozenset[str] = frozenset()
    frozen = False  # the plant could not be restored: nothing may change until the restart

    def freeze(self) -> None:
        """Write barrier of a failed plant recovery. Set by the plant service while it holds the
        lock; every change that was already waiting for the lock sees it after acquiring it."""
        self.frozen = True

    @property
    def lock(self) -> asyncio.Lock:
        """Held by plant changes: a selection edit cannot interleave with a rebuild."""
        return self._lock

    def set_specs(self, specs: Mapping[str, InverterSpec]) -> SpecsUndo:
        return self.store.set_specs(specs)

    def restore_specs(self, undo: SpecsUndo) -> None:
        self.store.restore_specs(undo)

    def prune_locked(self) -> bool:
        """Make the removal of inverter records durable (caller holds ``lock``). ``False`` when the
        store cannot be written: a stale record of a removed id must never meet a new inverter of
        the same id, so the plant is not changed while it is still on disk."""
        if not self.store.pruned:
            return True
        try:
            self.store.commit(dict(self.store.records), self.store.revision + 1)
        except OSError:
            log.error("selection records of removed inverters could not be persisted yet")
            return False
        return True

    async def persist_specs_locked(self) -> bool:
        """Best-effort prune after an accepted plant change (see ``prune_locked``)."""
        return self.prune_locked()

    def attach(self, applier: Applier) -> None:
        self._applier = applier

    def selection(self, inverter_id: str) -> InverterSelection:
        return resolve(self.store.specs[inverter_id], self.store.records[inverter_id])

    def initial(self) -> dict[str, tuple[Features, frozenset[str], int]]:
        """Start values for the runtime: features, effective selection, revision."""
        return {i: (s.features, s.active, self.revision) for i, s in self._all().items()}

    def _all(self) -> dict[str, InverterSelection]:
        return {i: self.selection(i) for i in self.store.specs}

    def plan(self, inverter_id: str, selection: InverterSelection | None = None) -> ReadPlan:
        return plan_for(self.store.specs[inverter_id], selection or self.selection(inverter_id))

    def dry_run(
        self, edits: Mapping[str, Edit]
    ) -> dict[str, tuple[Record, InverterSelection, ReadPlan]]:
        """Validate ``edits`` completely (records, selection, read plan) without any side effect."""
        out: dict[str, tuple[Record, InverterSelection, ReadPlan]] = {}
        for inverter_id, edit in edits.items():
            spec = self.store.specs.get(inverter_id)
            if spec is None:
                raise ChangeError("unknown_inverter", 404, inverter=inverter_id[:64])
            record = apply_edit(spec, self.store.records[inverter_id], edit)
            selection = resolve(spec, record)
            out[inverter_id] = (record, selection, plan_for(spec, selection))
        return out

    async def change(self, expected_revision: int, edits: Mapping[str, Edit]) -> ChangeResult:
        async with self._lock:
            if self.frozen:
                raise ChangeError("plant_recovery_failed", 409)
            if self.recovery_required:
                # an unfinished, not fully revocable swap: only a restart (which evaluates the
                # recovery marker) may continue
                raise ChangeError(RECOVERY_REQUIRED, 409)
            if expected_revision != self.store.revision:
                raise ChangeError("stale_revision", 409, current=self.store.revision)
            before = self._all()
            planned = self.dry_run(edits)
            records = dict(self.store.records)
            for inverter_id, (record, _, _) in planned.items():
                records[inverter_id] = record
            changed = tuple(i for i in sorted(planned) if records[i] != self.store.records[i])
            if not changed:
                return ChangeResult(self.store.revision, (), ())
            revision = self.store.revision + 1
            after = {i: planned[i][1] for i in changed}
            sel_changed = tuple(i for i in changed if after[i].active != before[i].active)
            self._prepare(after, sel_changed)  # validated before anything is written
            old_revision = self.store.revision
            try:
                self._adopt(after, revision, sel_changed)
            except Exception:
                log.exception("selection revision %d could not be adopted; reverting", revision)
                self._rollback(before, old_revision, changed, sel_changed)
                raise ChangeError("adoption_failed", 500, current=old_revision) from None
            try:
                # success is reported only when the new state is durable (accepted on disk)
                self.store.commit(records, revision)
            except OSError:
                log.error("selection could not be written; the old selection stays active")
                self._rollback(before, old_revision, changed, sel_changed)
                raise ChangeError("persist_failed", 500) from None
            return ChangeResult(revision, changed, sel_changed)

    def _prepare(
        self, after: Mapping[str, InverterSelection], sel_changed: Collection[str]
    ) -> None:
        if self._applier is None:
            return
        for inverter_id, selection in after.items():
            try:
                self._applier.prepare(
                    inverter_id, selection, selection_changed=inverter_id in sel_changed
                )
            except Exception:
                log.exception("%s: selection cannot be adopted", inverter_id)
                raise ChangeError("adoption_failed", 500, current=self.store.revision) from None

    def _adopt(
        self, after: Mapping[str, InverterSelection], revision: int, sel_changed: Collection[str]
    ) -> None:
        if self._applier is None:
            return
        for inverter_id, selection in after.items():
            self._applier.apply(
                inverter_id, selection, revision, selection_changed=inverter_id in sel_changed
            )

    def _rollback(
        self,
        before: Mapping[str, InverterSelection],
        old_revision: int,
        changed: Collection[str],
        sel_changed: Collection[str],
    ) -> bool:
        """The new selection was adopted by the running application but is not (or cannot be made)
        durable: runtime and publisher return to the accepted state, which is still the newest
        valid slot on disk. An inverter that cannot be set back is revoked (blocked:
        ``selection_recovery_required``) and further changes are refused until a restart."""
        complete = True
        if self._applier is None:
            return complete
        for inverter_id in changed:
            try:
                self._applier.apply(
                    inverter_id,
                    before[inverter_id],
                    old_revision,
                    selection_changed=inverter_id in sel_changed,
                )
            except Exception:
                log.exception("%s: previous selection could not be restored", inverter_id)
                complete = False
                self.recovery_required |= {inverter_id}
                try:
                    self._applier.revoke(inverter_id, RECOVERY_REQUIRED)
                except Exception:
                    log.exception("%s: could not be revoked", inverter_id)
        if not complete:
            self.recovery_required |= set(changed)
        return complete


def specs_from_config(cfg: AppConfig) -> dict[str, InverterSpec]:
    out: dict[str, InverterSpec] = {}
    for inv in cfg.inverters:
        profile = load_profile(inv.profile)
        ep = cfg.endpoint(inv.endpoint)
        out[inv.id] = InverterSpec(
            id=inv.id,
            profile=profile,
            hardware=load_hardware(profile.hardware),
            max_read_registers=ep.read_block_limit,
            energy_auto=inv.energy_resolution.value == "auto",
            poll=inv.poll,
            response_time_s=min(0.25, ep.timeout_s),
            # the same effective bus gap the runtime scheduler uses: the largest of the endpoints
            # that share the canonical bus
            gap_s=max(e.request_gap_ms for e in cfg.endpoints if e.bus == ep.bus) / 1000,
        )
    return out
