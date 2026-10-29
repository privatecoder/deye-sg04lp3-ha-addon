"""Persistent discovery inventory (``/data/discovery.json``) and the cleanup state machine.

Contract §5 (reviewer-codex-6.1-t8-contract.md). The inventory stores per device topic the last
complete component configuration, the retired components (platform plus the archived complete
configuration) and whether the whole device is being removed. It is updated atomically BEFORE
anything is published; every publish step derives from the inventory only, so a crash in the middle
of a sequence repeats the same step idempotently after the restart.

Removal of one component: the same device topic is published retained with the component reduced to
its platform (tombstone). A tombstone alone cannot find an existing registry entity of a freshly
started Home Assistant (no unique id), so at every (re)connect and on the Home Assistant birth
message each retired key is replayed as ONE old component under its original unique id (template
``none``, extra always-``offline`` availability topic) followed at once by the tombstone of the same
key, and the complete retained document is published again. Nothing proves that Home Assistant has
processed this (a PUBACK only means the broker accepted it, a Home Assistant birth message only
means that Home Assistant (re)started): a retired key is therefore NEVER forgotten automatically.
The birth message is a replay trigger only. Every retired key stays in the ledger and is replayed
(idempotently) at every session start and every birth. The ledger is only emptied by the explicit
user action ``DiscoveryEngine.purge`` (publisher: ``purge_cleanup_ledger``), and its size is limited
by ``MAX_RETIRED`` with a visible error (``LedgerFullError``) instead of silently dropping entries.
The E2E check with a real Home Assistant is the T12 acceptance case.

A removed device is never deleted in one step: it stays as an all-tombstone document until the
user purges the ledger; only then the retained topic is cleared with an empty payload. Only
topics that appear in the inventory are ever touched (never foreign prefixes or instances).
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from deye_monitor.ha_discovery import (
    DeviceMeta,
    DiscoveryError,
    Namespace,
    device_document,
    encode,
    replay_document,
    tombstone_document,
    validate_component,
)
from deye_monitor.store import JsonStore

log = logging.getLogger(__name__)

MAX_RETIRED = 2000  # application limit of the cleanup ledger (all devices together)

# (topic, payload, retain) - always QoS 1; returns after the broker acknowledged it
Publish = Callable[[str, bytes, bool], Awaitable[None]]


class LedgerFullError(DiscoveryError):
    """The cleanup ledger would exceed ``MAX_RETIRED``: nothing was changed or published. The
    user must purge the ledger (after Home Assistant has removed the old entities)."""


class DiscoveryStoreError(OSError):
    """``/data/discovery.json`` could not be written: nothing was published for this change."""


@dataclass(frozen=True, slots=True)
class DeviceTarget:
    """What should exist for one device now (the active components only)."""

    meta: DeviceMeta
    components: Mapping[str, Mapping[str, Any]]


@dataclass(slots=True)
class SyncReport:
    documents: int = 0
    replays: int = 0
    cleared_devices: int = 0
    skipped: list[str] = field(default_factory=list)


def _valid_meta(meta: object) -> bool:
    return (
        isinstance(meta, dict)
        and isinstance(meta.get("device_id"), str)
        and isinstance(meta.get("name"), str)
        and meta.get("kind") in ("app", "inverter")
        and (meta.get("inverter_id") is None or isinstance(meta.get("inverter_id"), str))
        and (meta.get("serial_number") is None or isinstance(meta.get("serial_number"), str))
    )


def _valid_components(comps: object) -> bool:
    if not isinstance(comps, dict):
        return False
    try:
        for key, comp in comps.items():
            if not isinstance(key, str) or not isinstance(comp, dict) or set(comp) == {"platform"}:
                return False
            validate_component(key, comp)
    except DiscoveryError:
        return False
    return True


def valid_discovery_document(doc: dict[str, Any]) -> bool:
    """Structure and content check of ``discovery.json`` (archived configs go through the same
    read-only allowlist as new ones, so nothing outside it can ever be replayed)."""
    devices = doc.get("devices")
    if set(doc) != {"devices"} or not isinstance(devices, dict):
        return False
    for topic, entry in devices.items():
        if not isinstance(topic, str) or not isinstance(entry, dict):
            return False
        if set(entry) != {"meta", "components", "retired", "removing"}:
            return False
        retired = entry["retired"]
        if (
            not _valid_meta(entry["meta"])
            or not _valid_components(entry["components"])
            or not isinstance(entry["removing"], bool)
            or not isinstance(retired, dict)
        ):
            return False
        for key, item in retired.items():
            if (
                not isinstance(key, str)
                or not isinstance(item, dict)
                or set(item) != {"platform", "component", "birth_replays"}
                or item["platform"] not in ("sensor", "binary_sensor")
                or not isinstance(item["birth_replays"], int)
                or isinstance(item["birth_replays"], bool)
                or item["birth_replays"] < 0
                or not _valid_components({key: item["component"]})
            ):
                return False
    return True


def _meta(entry: Mapping[str, Any]) -> DeviceMeta:
    m = entry["meta"]
    return DeviceMeta(
        device_id=m["device_id"],
        name=m["name"],
        kind=m["kind"],
        inverter_id=m.get("inverter_id"),
        serial_number=m.get("serial_number"),
    )


class DiscoveryEngine:
    """Serialised discovery publisher. Not re-entrant: the MQTT session calls it from one task."""

    def __init__(self, ns: Namespace, store: JsonStore, publish: Publish) -> None:
        self.ns = ns
        self.store = store
        self._publish = publish

    # -- inventory ---------------------------------------------------------------------------

    def topics(self) -> list[str]:
        return sorted(self.store.data.get("devices", {}))

    def _devices(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.store.data.get("devices", {}))

    def _commit(self, devices: dict[str, dict[str, Any]]) -> None:
        if devices == self.store.data.get("devices", {}):
            return
        try:
            self.store.commit({"devices": devices})
        except OSError as exc:
            raise DiscoveryStoreError(str(exc)) from None

    def purge(self, inverter_id: str | None = None) -> int:
        """Explicit user action: forget the cleanup ledger of one inverter (``None``: of every
        device). Returns the number of entries dropped. The next ``sync`` publishes the documents
        without the tombstones and clears devices that have nothing left."""
        devices = self._devices()
        dropped = 0
        for entry in devices.values():
            if inverter_id is None or entry["meta"].get("inverter_id") == inverter_id:
                dropped += len(entry["retired"])
                entry["retired"] = {}
        self._commit(devices)
        return dropped

    def retired_count(self, inverter_id: str | None = None) -> int:
        return sum(
            len(e["retired"])
            for e in self.store.data.get("devices", {}).values()
            if inverter_id is None or e["meta"].get("inverter_id") == inverter_id
        )

    # -- planning ----------------------------------------------------------------------------

    def _plan(self, targets: Mapping[str, DeviceTarget]) -> dict[str, dict[str, Any]]:
        devices = self._devices()
        for topic, entry in devices.items():
            if topic in targets or entry["removing"]:
                continue
            entry["removing"] = True  # configuration no longer wants this device
            for key, comp in entry["components"].items():
                entry["retired"][key] = {
                    "platform": comp["platform"],
                    "component": comp,
                    "birth_replays": 0,
                }
            entry["components"] = {}
        for topic, target in targets.items():
            found = devices.get(topic)
            if found is None:
                if not target.components:
                    continue  # a device without entities does not exist in Home Assistant
                found = {
                    "meta": target.meta.as_dict(),
                    "components": {},
                    "retired": {},
                    "removing": False,
                }
                devices[topic] = found
            entry = found
            entry["removing"] = False
            old: dict[str, Any] = entry["components"]
            for key, comp in old.items():
                if key not in target.components:
                    entry["retired"][key] = {
                        "platform": comp["platform"],
                        "component": comp,
                        "birth_replays": 0,
                    }
            for key in target.components:
                entry["retired"].pop(key, None)  # a re-selected key replaces its tombstone
            entry["components"] = {k: dict(v) for k, v in target.components.items()}
            entry["meta"] = target.meta.as_dict()
        total = sum(len(e["retired"]) for e in devices.values())
        if total > MAX_RETIRED:
            raise LedgerFullError(
                f"cleanup ledger would hold {total} entries (limit {MAX_RETIRED})"
            )
        return devices

    # -- publishing --------------------------------------------------------------------------

    def _retained_document(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        return device_document(
            self.ns,
            _meta(entry),
            entry["components"],
            {k: v["platform"] for k, v in sorted(entry["retired"].items())},
        )

    async def _publish_device(
        self, topic: str, entry: Mapping[str, Any], report: SyncReport
    ) -> None:
        if not entry["components"] and not entry["retired"]:
            return
        await self._publish(topic, encode(self._retained_document(entry)), True)
        report.documents += 1

    async def _replay(self, topic: str, entry: Mapping[str, Any], report: SyncReport) -> list[str]:
        """Cold-start cleanup of every retired key of one device (steps 2 and 3)."""
        meta = _meta(entry)
        done: list[str] = []
        for key, item in sorted(entry["retired"].items()):
            try:
                replay = replay_document(self.ns, meta, key, item["component"])
                tomb = tombstone_document(self.ns, meta, key, item["platform"])
            except DiscoveryError as exc:
                log.error("cleanup of %s on %s skipped: %s", key, topic, exc)
                report.skipped.append(f"{topic}:{key}")
                continue
            await self._publish(topic, encode(replay), False)
            await self._publish(topic, encode(tomb), False)
            report.replays += 1
            done.append(key)
        return done

    async def sync(
        self,
        targets: Mapping[str, DeviceTarget],
        *,
        replay: bool = False,
        birth: bool = False,
        republish: bool = False,
    ) -> SyncReport:
        """Bring the broker in line with ``targets``.

        ``replay`` (every connect and Home Assistant birth) offers the complete inventory again
        and runs the cold-start cleanup; ``birth`` only marks the Home Assistant birth message (a
        replay trigger, never a confirmation). ``republish`` sends every retained document again
        (after a ledger purge)."""
        report = SyncReport()
        before = self._devices()
        devices = self._plan(targets)
        self._commit(devices)  # atomically BEFORE the first publish
        changed = {t for t, e in devices.items() if replay or republish or e != before.get(t)}
        # the app device first: ``via_device`` needs it registered before its children
        order = sorted(devices, key=lambda t: (devices[t]["meta"]["kind"] != "app", t))
        if replay and any(e["retired"] for e in devices.values()):
            await self._publish(self.ns.cleanup_topic, b"offline", True)
        for topic in order:
            entry = devices[topic]
            if topic not in changed:
                continue
            await self._publish_device(topic, entry, report)
            if replay and entry["retired"]:
                done = await self._replay(topic, entry, report)
                if birth and done:  # informational only: a birth never confirms anything
                    for key in done:
                        entry["retired"][key]["birth_replays"] += 1
                    self._commit(devices)
                await self._publish_device(topic, entry, report)
        # a device is cleared only after all its cleanups are confirmed
        for topic, entry in list(devices.items()):
            if entry["components"] or entry["retired"]:
                continue
            # nothing is left in Home Assistant: drop the (tombstone-only) retained document
            await self._publish(topic, b"", True)
            report.cleared_devices += 1
            del devices[topic]
        self._commit(devices)
        return report
