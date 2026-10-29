"""Write a Markdown overview of all profile entries for review.

Usage (from deye_monitor/): ``uv run python ../scripts/profile_report.py [output.md]``
Default output: ``../.orch/profile-report.md``. Not part of the package.
"""

from __future__ import annotations

import sys
from pathlib import Path

from deye_monitor.profiles.schema import Entry, Predicate, load_profile

PROFILE = "sg04lp3_lv_v105_8"


def predicate_text(p: Predicate) -> str:
    if p.is_always:
        return "-"
    if p.all_of is not None:
        return "all(" + "; ".join(predicate_text(c) for c in p.all_of) + ")"
    if p.any_of is not None:
        return "any(" + "; ".join(predicate_text(c) for c in p.any_of) + ")"
    assert p.feature is not None
    rhs = f"= {p.equals}" if p.equals is not None else f"in {list(p.in_ or ())}"
    return f"{p.feature.value} {rhs}"


def addresses(e: Entry) -> str:
    if e.low is not None:
        return f"{e.low}/{e.high}"
    if e.addrs is not None:
        return f"{e.addrs[0]}..{e.addrs[-1]}"
    mask = f" &0x{e.mask:04X}" if e.mask is not None else ""
    return f"{e.addr}{mask}"


def main() -> None:
    profile = load_profile(PROFILE)
    out = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).parents[1] / ".orch" / "profile-report.md"
    )
    lines = [
        f"# Profile report: {profile.profile}",
        "",
        f"- protocol revision: {profile.protocol_revision}",
        f"- hardware: {profile.hardware}",
        f"- profile revision (sha256): `{profile.revision}`",
        f"- entries: {len(profile.entries)}",
        "",
        "| key | addr (low/high) | type | group | support_status | release | default "
        "| internal/mandatory | requires |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in sorted(profile.entries, key=lambda e: (e.addresses[0], e.key)):
        flags = ("internal " if e.internal else "") + ("mandatory" if e.mandatory else "")
        default_mark = "yes" if e.default_enabled else ""
        lines.append(
            f"| `{e.key}` | {addresses(e)} | {e.type} | {e.group} | {e.support_status} "
            f"| {e.release} | {default_mark} | {flags.strip()} | {predicate_text(e.requires)} |"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(profile.entries)} entries)")


if __name__ == "__main__":
    main()
