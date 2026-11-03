"""Upgrade behaviour of 0.5.2 (owner decision on review 41): the template is recomputed from the
shipped profile on every release, so an accepted selection follows the new template while the
explicit additions and removals stay. Documents the decision: no freeze, no migration."""

from __future__ import annotations

from pathlib import Path

from deye_monitor.profiles.features import Features
from deye_monitor.selection import Record, SelectionManager, SelectionStore, specs_from_config
from tests.test_runbook import plant_config

# what 0.5.1 preselected for the default description and 0.5.2 no longer does / newly does
DROPPED = {"fault_f13", "fault_f29", "fault_f41", "drm_status_raw", "drm0_reported", "mppt_count"}
ADDED = {"grid_internal_power_total", "inverter_frequency", "relay_grid"}


def test_an_accepted_051_selection_follows_the_new_template_and_keeps_explicit_choices(
    tmp_path: Path,
) -> None:
    specs = specs_from_config(plant_config())
    features = Features()
    record = Record(
        features=features,
        added=frozenset({"ups_power_total"}),
        removed=frozenset({"pv1_power"}),
        confirmed=frozenset(Features.model_fields),
        chosen=True,
    )
    SelectionStore(tmp_path, specs).commit({"deye1": record}, 1)  # as a 0.5.1 store left it
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}

    reopened = SelectionManager(SelectionStore(tmp_path, specs))
    sel = reopened.selection("deye1")
    assert sel.chosen and reopened.revision == 1  # still the accepted selection, no new choice
    assert "ups_power_total" in sel.effective and "pv1_power" not in sel.effective
    assert sel.added == {"ups_power_total"} and sel.removed == {"pv1_power"}
    assert not DROPPED & sel.effective  # formerly default, now opt-in: they leave Home Assistant
    assert sel.effective >= ADDED  # the new defaults appear
    assert {"serial_number", "active_faults", "load_power_total"} <= sel.effective
    # an explicit re-add (sensor selection -> tick) brings a dropped sensor back
    readded = Record(
        features=features,
        added=record.added | {"fault_f13", "drm_status_raw"},
        removed=record.removed,
        confirmed=record.confirmed,
    )
    (tmp_path / "again").mkdir()
    SelectionStore(tmp_path / "again", specs).commit({"deye1": readded}, 2)
    again = SelectionManager(SelectionStore(tmp_path / "again", specs)).selection("deye1")
    assert {"fault_f13", "drm_status_raw"} <= again.effective
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
