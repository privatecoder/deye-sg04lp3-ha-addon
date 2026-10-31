"""R14-01: the plant series is never reused, even after an older file or a failed save."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.totals import SERIES_FILE, PlantSeries

TWO = [("master", "g0"), ("slave1", "g0")]
THREE = [*TWO, ("slave2", "g0")]


@pytest.mark.parametrize("restore_older_file", [True, False])
def test_valid_file_never_revives_a_retired_generation(
    tmp_path: Path, restore_older_file: bool
) -> None:
    s = PlantSeries(tmp_path)
    s.follow(TWO)
    backup = (tmp_path / SERIES_FILE).read_bytes()
    s.follow(THREE)
    assert s.series == "g1"
    if restore_older_file:
        (tmp_path / SERIES_FILE).write_bytes(backup)
    current = PlantSeries(tmp_path, known={"g0", "g1"})
    current.follow(TWO if restore_older_file else THREE)
    if restore_older_file:
        assert current.generation > 1
        assert PlantSeries(tmp_path, known={"g0", "g1"}).generation == current.generation
    else:
        assert current.series == "g1"  # normal restart keeps the series


def test_failed_save_does_not_revive_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = PlantSeries(tmp_path)
    s.follow(TWO)
    real = s._store.commit

    def cannot_save(document: dict[str, object]) -> None:
        raise OSError("injected")

    monkeypatch.setattr(s._store, "commit", cannot_save)
    s.follow(THREE)
    assert s.series == "g1" and s.persist_failed
    assert s.follow(THREE) is False and s.persist_failed  # still failing, nothing started
    monkeypatch.setattr(s._store, "commit", real)
    assert s.follow(THREE) is True and not s.persist_failed  # recovered: safe to publish
    assert PlantSeries(tmp_path, known={"g0", "g1"}).series == "g1"
    # the failed generation was never saved: a restart that knows g1 goes above it
    monkeypatch.setattr(s._store, "commit", cannot_save)
    t = PlantSeries(tmp_path, known={"g0", "g1"})
    t.follow(TWO)
    assert t.generation > 1
