"""Energy resolution change 0.1 -> 1 against a real Home Assistant with the recorder.

Deterministic counter values per generation (set by the test, nothing random) are followed through
the recorder's 5-minute (short-term) statistics: the states AND the sums are checked, the old
series is untouched by the migration, the new one is separate. The wait for a statistics row is
bounded by a deadline and never a blind sleep. Long-term (hourly) statistics only exist after a
full hour: they are recorded in ``artifacts/recorder.json`` but are not part of the acceptance."""

from __future__ import annotations

import json
import re

import pytest

from conftest import ARTIFACTS, INSTANCE
from tests.helpers import ours, uid, until

pytestmark = [pytest.mark.usefixtures("running"), pytest.mark.slow]

KEY = "battery_energy_charge_total"
SERIES = re.compile(r"^(?:[a-z0-9]{8})?g[0-9]+$")
ROW_DEADLINE = 600  # a 5-minute window plus the compile and commit delay, with reserve


def series_of(unique_id: str, inverter: str) -> str | None:
    prefix = f"deye_{INSTANCE}_{inverter}_{KEY}_"
    return unique_id[len(prefix):] if unique_id.startswith(prefix) and SERIES.match(unique_id[len(prefix):]) else None


async def energy_series(ha, inverter="deye_tcp") -> set[str]:
    return {s for u in await ours(ha, INSTANCE) if (s := series_of(u, inverter))}


async def stats(ha, statistic_id: str, period: str = "5minute") -> list[dict]:
    result = await ha.ws(
        {
            "type": "recorder/statistics_during_period",
            "start_time": "2026-01-01T00:00:00+00:00",
            "statistic_ids": [statistic_id],
            "period": period,
            "types": ["state", "sum", "min", "max", "last_reset"],
        }
    )
    return list(result[0].get(statistic_id, []))


async def statistic_ids(ha) -> set[str]:
    return {i["statistic_id"] for i in (await ha.ws({"type": "recorder/list_statistic_ids"}))[0]}


def recorded_states(ha, entity: str) -> set[float]:
    """Numeric states the recorder actually wrote to its database for ``entity``."""
    rows = ha.db_query(
        "SELECT s.state FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id WHERE m.entity_id = ?",
        (entity,),
    )
    out: set[float] = set()
    for (value,) in rows:
        try:
            out.add(float(value))
        except (TypeError, ValueError):
            pass
    return out


async def state_is(ha, eid: str, expected: float) -> bool:
    st = (await ha.states()).get(eid)
    try:
        return bool(st) and abs(float(st["state"]) - expected) < 0.001
    except (TypeError, ValueError):
        return False


async def row_with_state(ha, eid: str, expected: float) -> dict:
    async def find():
        for row in await stats(ha, eid):
            if row.get("state") is not None and abs(row["state"] - expected) < 0.001:
                return row
        return None

    return await until(find, f"a short-term statistics row of {eid} with state {expected}", ROW_DEADLINE, 5)


def check_sum(row: dict, expected: float) -> None:
    assert row.get("sum") is not None and abs(row["sum"] - expected) < 0.01, (expected, row)


async def test_resolution_change_creates_a_new_series_with_separate_recorder_statistics(running):
    ha, app, fake = running.ha, running.app, running.fakes["deye_tcp"]
    before = await energy_series(ha)
    assert len(before) == 1, before
    (old_series,) = before
    old_eid = f"sensor.{uid(INSTANCE, 'deye_tcp', KEY + '_' + old_series)}"

    # -- generation 0 (0.1 kWh per count): 3460.0, then +10.0 -> 3470.0 (never below the earlier value:
    # a falling total_increasing counter would be a reset) ----------------------------------------
    fake.regs[516] = 34600
    await until(lambda: state_is(ha, old_eid, 3460.0), "the old series shows 3460.0", 120, 2)
    row0 = await row_with_state(ha, old_eid, 3460.0)
    fake.regs[516] = 34700
    await until(lambda: state_is(ha, old_eid, 3470.0), "the old series shows 3470.0", 120, 2)
    row1 = await row_with_state(ha, old_eid, 3470.0)
    assert abs((row1["sum"] - row0["sum"]) - 10.0) < 0.01, (row0, row1)  # +10.0 kWh
    old_rows = await stats(ha, old_eid)
    snapshot_old = json.dumps(old_rows, sort_keys=True)
    old_states = {r["state"] for r in old_rows}
    assert old_states >= {3460.0, 3470.0} and all(v < 6000 for v in old_states), old_states
    base = old_rows[0]["state"] - old_rows[0]["sum"]
    for r in old_rows:  # the sum is the increase since the first row of the series
        assert abs(r["sum"] - (r["state"] - base)) < 0.01, (base, r)
    written_old = recorded_states(ha, old_eid)
    assert {3460.0, 3470.0} <= written_old and all(v in written_old for v in old_states)

    # -- the change: resolution override 1 (a new generation starts) ------------------------------
    await app.terminate()
    app.edit_plant("deye_tcp", energy_resolution="1")
    app.start()
    await app.wait_api(60)

    async def new_series():
        return (await energy_series(ha)) - before

    fresh = await until(new_series, "the entity of a new energy series appears", 120, 2)
    (new_name,) = fresh
    assert new_name != old_series
    new_eid = f"sensor.{uid(INSTANCE, 'deye_tcp', KEY + '_' + new_name)}"

    # -- generation 1 (1 kWh per count): the first value of the new series is 34700 (the raw word as it
    # stands); then 40000 and +100 -> 40100 (rising, never a reset) ------------------------------
    fake.regs[516] = 40000
    await until(lambda: state_is(ha, new_eid, 40000.0), "the new series shows 40000", 120, 2)
    new0 = await row_with_state(ha, new_eid, 40000.0)
    fake.regs[516] = 40100
    await until(lambda: state_is(ha, new_eid, 40100.0), "the new series shows 40100", 120, 2)
    new1 = await row_with_state(ha, new_eid, 40100.0)
    assert abs((new1["sum"] - new0["sum"]) - 100.0) < 0.01, (new0, new1)
    states = await ha.states()
    assert old_eid not in states or states[old_eid]["state"] in ("unknown", "unavailable"), "the old series is no longer updated"
    assert not any(e.endswith("_2") for e in states if KEY in e)

    # -- separation: ids, values, sums ----------------------------------------------------------
    ids = await statistic_ids(ha)
    assert old_eid in ids and new_eid in ids
    assert not [i for i in ids if re.search(rf"{KEY}_({old_series}|{new_name})_\d+$", i)]
    old_after = await stats(ha, old_eid)
    assert json.dumps(old_after[: len(old_rows)], sort_keys=True) == snapshot_old, "history of the old series changed"
    assert all(r["state"] < 6000 and r["sum"] < 100 for r in old_after), "new-scale data entered the old series"
    new_rows = await stats(ha, new_eid)
    assert all(r["state"] >= 30000 for r in new_rows), new_rows  # always the 1 kWh scale
    new_base = new_rows[0]["state"] - new_rows[0]["sum"]
    for r in new_rows:
        assert abs(r["sum"] - (r["state"] - new_base)) < 0.01, (new_base, r)
    written_new = recorded_states(ha, new_eid)
    assert {40000.0, 40100.0} <= written_new and all(r["state"] in written_new for r in new_rows)
    assert not (written_new & written_old)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "recorder.json").write_text(
        json.dumps(
            {
                "old_series": old_series,
                "new_series": new_name,
                "old_rows": [{k: r.get(k) for k in ("state", "sum")} for r in old_after],
                "new_rows": [{k: r.get(k) for k in ("state", "sum")} for r in new_rows],
                "old_hourly_rows": len(await stats(ha, old_eid, "hour")),
                "new_hourly_rows": len(await stats(ha, new_eid, "hour")),
                "note": "short-term (5 minute) statistics are the accepted evidence; hourly rows are informative",
            },
            indent=1,
        )
    )
