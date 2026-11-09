"""The plant GEN total reads every member's GEN share without selecting or publishing it."""

from __future__ import annotations

from pathlib import Path

from deye_monitor.selection import PLANT_GEN_KEY, Edit
from tests.selection_helpers import plant

KEY_ADDRESSES = {667, 671}  # low and high word of the signed 32-bit value


async def test_a_gen_use_makes_every_member_read_its_share_without_selecting_it(
    tmp_path: Path,
) -> None:
    async with plant(tmp_path) as p:
        invs = p.rig.runtime.inverters
        assert all(not inv.plant_keys for inv in invs.values())  # GEN unused everywhere
        await p.manager.change(
            p.manager.revision, {"deye1": Edit(features={"gen_port_mode": "smart_load"})}
        )
        for inv in invs.values():  # deye2 stays "unused" and still reads the share
            assert inv.plant_keys == {PLANT_GEN_KEY}
            assert inv.current_plan().addresses >= KEY_ADDRESSES
        assert PLANT_GEN_KEY not in invs["deye2"].selection  # the user's choice is untouched
        await p.manager.change(
            p.manager.revision, {"deye1": Edit(features={"gen_port_mode": "unused"})}
        )
        for inv in invs.values():
            assert not inv.plant_keys
            assert not KEY_ADDRESSES & inv.current_plan().addresses


async def test_the_internal_plant_read_counts_in_the_budget_preview_and_is_shared(
    tmp_path: Path,
) -> None:
    async with plant(tmp_path) as p:
        before = p.manager.plan("deye2").addresses
        assert not KEY_ADDRESSES & before
        change = {"deye1": Edit(features={"gen_port_mode": "smart_load"})}
        planned = p.manager.dry_run(change)  # the preview of a not yet applied edit
        assert planned["deye1"][2].addresses >= KEY_ADDRESSES
        await p.manager.change(p.manager.revision, change)
        assert p.manager.plan("deye2").addresses >= KEY_ADDRESSES  # unused member, internal read
        # choosing the sensor on the unused member adds the same registers once
        await p.manager.change(p.manager.revision, {"deye2": Edit(add=frozenset({PLANT_GEN_KEY}))})
        inv = p.rig.runtime.inverters["deye2"]
        assert PLANT_GEN_KEY in inv.selection and PLANT_GEN_KEY in inv.plant_keys
        assert p.manager.plan("deye2").addresses == inv.current_plan().addresses
        assert sum(b.needed.count(667) for b in inv.current_plan().blocks) == 1
