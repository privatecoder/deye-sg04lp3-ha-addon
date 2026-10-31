"""Template, selection checks and read plan (SPEC §9.3).

``read_set = mandatory_identity_health + selected_sensor_dependencies``. Multi-word entities pull
in exactly their partners; blocks are built only over addresses known to the profile.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from deye_monitor.config import PollConfig
from deye_monitor.profiles.features import Features
from deye_monitor.profiles.hardware import HardwareProfile
from deye_monitor.profiles.schema import Profile

MAX_REGISTERS_PER_READ = 125


class SelectionError(ValueError):
    """The selection contains entries that must never be enabled."""

    def __init__(self, rejected: Mapping[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in sorted(rejected.items())))
        self.rejected = dict(rejected)


def template_selection(
    profile: Profile, hardware: HardwareProfile, features: Features
) -> frozenset[str]:
    """Default selection: ``default_enabled`` entries on supported hardware whose ``requires``
    holds for the plant features. Templates change the default only, never access rights."""
    return frozenset(
        e.key
        for e in profile.entries
        if e.default_enabled
        and e.is_physical
        and hardware.name in e.supported_on
        and e.requires.evaluate(features)
    )


def unreported_entries(profile: Profile, words: Mapping[int, int]) -> dict[str, int]:
    """Entries that declare a not-reported word and read exactly that word: key -> raw word.
    Per field, never global; an address that was not read says nothing."""
    out: dict[str, int] = {}
    for e in profile.entries:
        if e.internal or not e.declares_not_reported or e.addr is None:
            continue
        word = words.get(e.addr)
        if word is not None and e.is_not_reported(word):
            out[e.key] = word
    return out


def selection_problems(profile: Profile, selected_keys: Collection[str]) -> dict[str, str]:
    by_key = {e.key: e for e in profile.entries}
    rejected: dict[str, str] = {}
    for key in selected_keys:
        entry = by_key.get(key)
        if entry is None:
            rejected[key] = "unknown entry"
        elif entry.internal:
            rejected[key] = "internal entry"
        elif entry.support_status in ("placeholder", "unsupported_for_profile"):
            rejected[key] = entry.support_status
        elif not entry.selectable:
            rejected[key] = "not released for this profile"
    return rejected


def validate_selection(profile: Profile, selected_keys: Collection[str]) -> None:
    """Server-side enforcement: placeholders and unsupported entries can never be enabled,
    unresolved entries only as raw diagnostics (they carry no unit). Raises ``SelectionError``."""
    rejected = selection_problems(profile, selected_keys)
    if rejected:
        raise SelectionError(rejected)


@dataclass(frozen=True, slots=True)
class ReadBlock:
    group: str
    start: int
    count: int
    needed: tuple[int, ...]  # addresses of this block that some entry actually uses


@dataclass(frozen=True, slots=True)
class GroupBudget:
    blocks: int
    cycle_s: float  # blocks x (response time + gap)
    interval_s: float
    utilisation: float  # cycle_s / interval_s


@dataclass(frozen=True, slots=True)
class PlanBudget:
    per_group: Mapping[str, GroupBudget]
    utilisation: float  # sum of the group utilisations; > 1 cannot be sustained
    overloaded: bool


@dataclass(frozen=True, slots=True)
class ReadPlan:
    blocks: tuple[ReadBlock, ...]
    keys: frozenset[str]  # every entry the plan can decode (selected + mandatory)
    mandatory_keys: frozenset[str]
    dependencies: Mapping[str, tuple[int, ...]]
    reads_r65: bool  # address 65 is in the read set (any reason: dependency or explicit choice)
    r65_required_for_energy: bool  # an energy entity needs R65 (automatic resolution only)
    budget: PlanBudget

    @property
    def addresses(self) -> frozenset[int]:
        return frozenset(a for b in self.blocks for a in b.needed)


def build_read_plan(
    profile: Profile,
    selected_keys: Collection[str],
    features: Features,
    *,
    max_read_registers: int,
    energy_auto: bool,
    max_gap: int = 0,
    unreported: Collection[str] = (),
    poll: PollConfig | None = None,
    response_time_s: float = 0.25,
    gap_s: float = 0.05,
) -> ReadPlan:
    """Build the read plan for one inverter.

    ``features`` are accepted for the call contract of the UI backend; entry selection is
    explicit (``selected_keys``), the template is applied by the caller. ``max_gap`` allows
    bridging up to that many addresses between needed ones, but only when every bridged address
    is known to the profile; the default reads exactly the needed words. ``unreported`` are
    entries the device does not report (``unreported_entries``): they are not read any more; a
    mandatory entry or the R65 dependency of the automatic energy resolution stays.
    """
    del features
    if not 1 <= max_read_registers <= MAX_REGISTERS_PER_READ:
        raise ValueError("max_read_registers must be 1..125")
    if max_gap < 0:
        raise ValueError("max_gap must be >= 0")
    if not math.isfinite(response_time_s) or response_time_s <= 0:
        raise ValueError("response_time_s must be finite and > 0")
    if not math.isfinite(gap_s) or gap_s < 0:
        raise ValueError("gap_s must be finite and >= 0")
    validate_selection(profile, selected_keys)

    by_key = {e.key: e for e in profile.entries}
    mandatory = frozenset(e.key for e in profile.entries if e.mandatory)
    keys = (set(selected_keys) - set(unreported)) | mandatory
    r65_required = energy_auto and any(by_key[k].scale_source for k in selected_keys)
    if r65_required:
        keys.add(profile.energy_resolution_key)

    deps = {k: by_key[k].addresses for k in sorted(keys)}
    known = profile.known_addresses
    blocks: list[ReadBlock] = []
    for group in profile.groups:
        needed = sorted({a for k in keys if by_key[k].group == group for a in by_key[k].addresses})
        blocks += _blocks(group, needed, known, max_read_registers, max_gap)

    return ReadPlan(
        blocks=tuple(blocks),
        keys=frozenset(keys),
        mandatory_keys=mandatory,
        dependencies=deps,
        reads_r65=any(65 in by_key[k].addresses for k in keys),
        r65_required_for_energy=r65_required,
        budget=_budget(profile, blocks, poll or PollConfig(), response_time_s, gap_s),
    )


def _blocks(
    group: str, needed: list[int], known: frozenset[int], limit: int, max_gap: int
) -> list[ReadBlock]:
    out: list[ReadBlock] = []
    run: list[int] = []

    def flush() -> None:
        if run:
            out.append(ReadBlock(group, run[0], run[-1] - run[0] + 1, tuple(run)))
            run.clear()

    for addr in needed:
        if run:
            gap = range(run[-1] + 1, addr)
            bridgeable = len(gap) <= max_gap and all(a in known for a in gap)
            if (addr - run[0] + 1 > limit) or (addr != run[-1] + 1 and not bridgeable):
                flush()
        run.append(addr)
    flush()
    return out


def _budget(
    profile: Profile,
    blocks: list[ReadBlock],
    poll: PollConfig,
    response_time_s: float,
    gap_s: float,
) -> PlanBudget:
    per_group: dict[str, GroupBudget] = {}
    for group, cfg in profile.groups.items():
        n = sum(1 for b in blocks if b.group == group)
        if not n:
            continue
        interval = float(getattr(poll, cfg.poll))
        cycle = n * (response_time_s + gap_s)
        per_group[group] = GroupBudget(n, cycle, interval, cycle / interval)
    total = sum(g.utilisation for g in per_group.values())
    return PlanBudget(per_group, total, total > 1.0)
