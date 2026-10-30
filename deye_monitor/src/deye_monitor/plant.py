"""The plant: connections and inverters, edited in the web interface (SPEC §4 Nachtrag).

``/data/plant.json`` is a two-slot document (``plant.a.json`` / ``plant.b.json``) like the sensor
selection: every slot is complete, carries a monotone ``seq`` and a SHA-256 checksum, and is
written atomically (temporary file, fsync, rename). The newest valid slot is the accepted state; a
write goes to the slot that does not hold it, so a failing write never touches the accepted state.
The plant has its own revision (``If-Match`` of every plant change).

A change runs validate -> adopt (stop the running generation, build the new one) -> persist. A
failure at any step restores the previous generation; success is reported only when the new plant
is durable.

Migration: options of older installations may still carry ``endpoints``/``inverters``. They are
taken over ONCE, and only while no plant file exists. Afterwards the store is authoritative; options
that are still present only produce a notice, they never override the store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import ValidationError

from deye_monitor.config import AppConfig, PlantConfig
from deye_monitor.selection import (
    ChangeError,
    InverterSpec,
    Problem,
    SelectionManager,
    SpecsUndo,
    specs_from_config,
)
from deye_monitor.store import JsonStore, LoadStatus

log = logging.getLogger(__name__)

T = TypeVar("T")

SLOT_NAMES = ("plant.a.json", "plant.b.json")
SCHEMA_VERSION = 1
SLOT_FORMAT = 1
MAX_ENDPOINTS = 32
MAX_INVERTERS = 32


def plant_to_body(plant: PlantConfig, revision: int) -> dict[str, Any]:
    return {
        "revision": revision,
        "endpoints": [e.model_dump(mode="json", exclude_none=True) for e in plant.endpoints],
        "inverters": [i.model_dump(mode="json", exclude_none=True) for i in plant.inverters],
    }


def plant_from_body(body: Mapping[str, Any]) -> PlantConfig | None:
    """The plant of a stored body, or ``None`` when it does not validate."""
    try:
        return PlantConfig.model_validate(
            {"endpoints": body.get("endpoints"), "inverters": body.get("inverters")}
        )
    except ValidationError:
        return None


def _checksum(seq: int, body: Mapping[str, Any]) -> str:
    text = json.dumps(
        {"format": SLOT_FORMAT, "seq": seq, "body": body}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode()).hexdigest()


def _valid_slot(doc: dict[str, Any]) -> bool:
    seq, body = doc.get("seq"), doc.get("body")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1):
        return False
    if doc.get("format") != SLOT_FORMAT or not isinstance(body, dict):
        return False
    revision = body.get("revision")
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0):
        return False
    if set(body) != {"revision", "endpoints", "inverters"}:
        return False
    return doc.get("checksum") == _checksum(seq, body) and plant_from_body(body) is not None


class PlantStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._slots = [
            JsonStore(directory / name, schema_version=SCHEMA_VERSION, validate=_valid_slot)
            for name in SLOT_NAMES
        ]
        self.problems: list[Problem] = []
        self.plant = PlantConfig()
        self.revision = 0
        self._seq = 0
        self.status = LoadStatus.MISSING
        self._load()

    # -- load -------------------------------------------------------------------------------

    def _newest_valid(self) -> int | None:
        valid = [
            (s.data["body"]["revision"], s.data["seq"], i)
            for i, s in enumerate(self._slots)
            if s.status is LoadStatus.VALID
        ]
        return max(valid)[2] if valid else None

    def _load(self) -> None:
        corrupt = [s for s in self._slots if s.status is LoadStatus.CORRUPT]
        for slot in corrupt:
            self.problems.append(Problem("plant_store_corrupt", slot.path.name, slot.problem or ""))
        newest = self._newest_valid()
        if newest is None:
            if corrupt:
                # fail closed: nothing runs from a damaged plant; the user sets it up again (the
                # damaged file is kept as ``*.corrupt`` when it is overwritten)
                self.status = LoadStatus.CORRUPT
                self.problems.append(Problem("plant_store_empty_used", "plant"))
            return
        doc = self._slots[newest].data
        plant = plant_from_body(doc["body"])
        assert plant is not None  # checked by the slot validator
        self.plant = plant
        self.revision = int(doc["body"]["revision"])
        self._seq = int(doc["seq"])
        self.status = LoadStatus.VALID
        if corrupt:
            self.problems.append(Problem("plant_store_recovered_previous", "plant"))

    @property
    def exists(self) -> bool:
        """A plant file (valid or damaged) is present: the options are no migration source."""
        return self.status is not LoadStatus.MISSING

    # -- write ------------------------------------------------------------------------------

    def _target_slot(self) -> int:
        newest = self._newest_valid()
        others = [i for i in (0, 1) if i != newest]
        order = {LoadStatus.CORRUPT: 0, LoadStatus.MISSING: 1, LoadStatus.VALID: 2}
        return min(others, key=lambda i: (order[self._slots[i].status], i))

    def commit(self, plant: PlantConfig, revision: int) -> None:
        """Make ``plant`` the accepted state: durable, or ``OSError`` with everything unchanged."""
        slot = self._slots[self._target_slot()]
        if slot.status is LoadStatus.CORRUPT and slot.path.exists():
            shutil.copyfile(slot.path, slot.path.with_name(slot.path.name + ".corrupt"))
        body = plant_to_body(plant, revision)
        seq = self._seq + 1
        slot.commit(
            {"format": SLOT_FORMAT, "seq": seq, "checksum": _checksum(seq, body), "body": body}
        )
        self._seq = seq
        self.plant, self.revision = plant, revision
        self.status = LoadStatus.VALID
        self.problems = []

    def migrate_from_options(self, options: AppConfig) -> bool:
        """Take ``endpoints``/``inverters`` of the options over, once. Only while no plant file
        exists and the options carry something. Returns whether this happened."""
        if self.exists or not (options.endpoints or options.inverters):
            return False
        self.commit(options.plant, 1)
        return True


def options_notice(
    store: PlantStore, options: AppConfig, *, migrated: bool
) -> list[dict[str, Any]]:
    """Notices for the web interface about entries that are still present in the app options."""
    if not (options.endpoints or options.inverters):
        return []
    if migrated:
        return [{"code": "plant_migrated", "differs": False}]
    return [
        {
            "code": "plant_options_present",
            "differs": plant_to_body(options.plant, 0)["endpoints"]
            != plant_to_body(store.plant, 0)["endpoints"]
            or plant_to_body(options.plant, 0)["inverters"]
            != plant_to_body(store.plant, 0)["inverters"],
        }
    ]


# -- live change --------------------------------------------------------------------------------


class SwitchError(Exception):
    """The new generation could not be started. ``restored`` tells whether the previous one runs
    again."""

    def __init__(self, *, restored: bool) -> None:
        super().__init__("restored" if restored else "not restored")
        self.restored = restored


class Generations(Protocol):
    """What the plant service needs from the running application."""

    async def switch(
        self,
        cfg: AppConfig,
        specs: Mapping[str, InverterSpec],
        *,
        restore: SpecsUndo | None = None,
        fallback: bool = True,
    ) -> SpecsUndo:
        """Stop the running generation and start a PROPOSED one for ``cfg`` (no shared file is
        touched, nothing is published until ``accept_generation``). Returns the token that puts
        the selection state back exactly. Raises ``SwitchError`` after rebuilding the previous
        generation where that is allowed (``fallback``). With ``restore`` the call is the roll-back
        of a proposal: the selection state is restored and the accepted generation started."""
        ...

    def accept_generation(self) -> None:
        """The proposed generation is durable: persist its held-back state and let it publish."""
        ...

    def forget_binding(self, inverter_id: str) -> bool:
        """Remove the persisted serial binding of a deleted inverter."""
        ...


@dataclass(slots=True)
class PlantChange:
    revision: int
    changed: bool
    removed_inverters: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)


class PlantService:
    """Single writer of the plant. One lock serialises changes; the revision check happens inside
    it. The selection manager's lock is held across the switch, so a selection edit can never
    interleave with the rebuild."""

    def __init__(
        self,
        store: PlantStore,
        options: AppConfig,
        manager: SelectionManager,
        generations: Generations,
        *,
        notices: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.store = store
        self.options = options
        self.manager = manager
        self._generations = generations
        self._lock = asyncio.Lock()
        self.notices = [dict(n) for n in notices]
        self.recovery_failed = False
        self._probes: set[asyncio.Future[Any]] = set()

    @property
    def revision(self) -> int:
        return self.store.revision

    @property
    def plant(self) -> PlantConfig:
        return self.store.plant

    def config(self, plant: PlantConfig | None = None) -> AppConfig:
        return self.options.with_plant(plant or self.store.plant)

    def _lock_after_failed_recovery(self) -> None:
        """The accepted plant could not be started again: nothing runs and nothing may change until
        the app is restarted (it starts from the accepted plant)."""
        self.recovery_failed = True
        self.manager.freeze()  # also for selection edits that already wait for the lock
        if not any(n.get("code") == "plant_recovery_failed" for n in self.notices):
            self.notices.append({"code": "plant_recovery_failed", "differs": False})

    async def probe(self, start: Callable[[], Awaitable[T]]) -> T:
        """Run a connection test inside the current generation. ``start`` is called while no plant
        change is running and builds the test from that generation; a plant change cancels the test
        and waits for it before the generation is stopped, so a test never outlives its generation
        (a request waiting at the scheduler can not reopen a retired transport)."""
        async with self._lock:
            if self.recovery_failed:
                raise ChangeError("plant_recovery_failed", 409)
            task = asyncio.ensure_future(start())
            self._probes.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() == 0 and task.cancelled():
                raise ChangeError("plant_changed_during_test", 409) from None
            raise
        finally:
            self._probes.discard(task)

    async def _end_probes(self) -> None:
        probes = list(self._probes)
        for task in probes:
            task.cancel()
        if probes:
            await asyncio.gather(*probes, return_exceptions=True)

    async def change(
        self,
        expected_revision: int,
        build: Callable[[PlantConfig], PlantConfig],
        *,
        forget_bindings: frozenset[str] = frozenset(),
    ) -> PlantChange:
        async with self._lock, self.manager.lock:
            if self.recovery_failed:
                raise ChangeError("plant_recovery_failed", 409)
            if self.manager.recovery_required:
                raise ChangeError("selection_recovery_required", 409)
            if expected_revision != self.store.revision:
                raise ChangeError("stale_revision", 409, current=self.store.revision)
            old = self.store.plant
            new = build(old)  # raises ChangeError for anything invalid
            if new == old:
                return PlantChange(self.store.revision, False)
            if not self.manager.prune_locked():
                # a stale selection record of a removed id is still on disk: a new inverter with
                # that id could take it over after a restart
                raise ChangeError("persist_failed", 500)
            removed = tuple(
                i.id for i in old.inverters if i.id not in {n.id for n in new.inverters}
            )
            new_cfg = self.config(new)
            try:
                specs = specs_from_config(new_cfg)
            except Exception:
                log.exception("plant: specs could not be built")
                raise ChangeError("invalid_plant", 422) from None
            old_cfg = self.config(old)
            old_specs = dict(self.manager.specs)
            await self._end_probes()
            try:
                undo = await self._generations.switch(new_cfg, specs)
            except SwitchError as exc:
                log.error("plant change %d could not be adopted", self.store.revision + 1)
                if not exc.restored:
                    self._lock_after_failed_recovery()
                raise ChangeError(
                    "adoption_failed" if exc.restored else "plant_recovery_failed",
                    500,
                    current=self.store.revision,
                ) from None
            revision = self.store.revision + 1
            try:
                self.store.commit(new, revision)
            except OSError:
                log.error("plant could not be written; going back to the accepted plant")
                try:
                    await self._generations.switch(old_cfg, old_specs, restore=undo, fallback=False)
                except SwitchError:
                    log.error("the accepted plant could not be started again; nothing is running")
                    self._lock_after_failed_recovery()
                    raise ChangeError("plant_recovery_failed", 500) from None
                raise ChangeError("persist_failed", 500) from None
            self._generations.accept_generation()
            notes: list[str] = []
            for inverter_id in sorted(forget_bindings & set(removed)):
                try:
                    if not self._generations.forget_binding(inverter_id):
                        notes.append("binding_not_removed")
                except OSError, ValueError:
                    notes.append("binding_not_removed")
            if not await self.manager.persist_specs_locked():
                notes.append("prune_pending")
            return PlantChange(revision, True, removed, notes)
