"""Plausibility of the energy resolution (live-43): daily PV counter vs integrated PV power."""

from __future__ import annotations

from typing import Any

from deye_monitor.energy_check import (
    PLAUSIBLE,
    TOO_HIGH,
    TOO_LOW,
    UNKNOWN,
    EnergyPlausibility,
)


def run(
    counter_per_hour_kwh: float, watts: float = 5000.0, hours: int = 2, generation: int = 0
) -> dict[str, Any]:
    check = EnergyPlausibility()
    check.feed({"pv_energy_today": 10.0}, 0.0, generation)
    t = 0.0
    for step in range(hours * 60):
        t += 60.0
        check.feed({"pv1_power": watts}, t, generation)
        if step % 10 == 9:  # the slow energy group
            check.feed({"pv_energy_today": 10.0 + counter_per_hour_kwh * t / 3600.0}, t, generation)
    return check.view()


def test_matching_counter_is_plausible() -> None:
    assert run(5.0)["state"] == PLAUSIBLE


def test_counter_ten_times_too_high() -> None:
    view = run(50.0)
    assert view["state"] == TOO_HIGH and 9 < view["ratio"] < 11


def test_counter_ten_times_too_low() -> None:
    assert run(0.5)["state"] == TOO_LOW


def test_too_little_data_is_unknown() -> None:
    assert run(50.0, watts=200.0, hours=1)["state"] == UNKNOWN
    assert EnergyPlausibility().view()["state"] == UNKNOWN


def test_gaps_are_not_bridged() -> None:
    check = EnergyPlausibility()
    check.feed({"pv_energy_today": 1.0}, 0.0, 0)
    check.feed({"pv1_power": 9000.0}, 10.0, 0)
    check.feed({"pv1_power": 9000.0}, 100000.0, 0)  # a day later: nothing integrated for the gap
    assert check.view()["integrated_kwh"] == 0.0


def sun(check: EnergyPlausibility, start: float, minutes: int) -> float:
    t = start
    for _ in range(minutes + 1):
        check.feed({"pv1_power": 6000.0}, t, 0)
        t += 60.0
    return t


def test_counter_reset_and_new_series_start_over() -> None:
    check = EnergyPlausibility()
    check.feed({"pv_energy_today": 20.0}, 0.0, 0)
    t = sun(check, 1.0, 60)
    assert check.view()["integrated_kwh"] > 5
    check.feed({"pv_energy_today": 0.0}, t, 0)  # midnight: the daily counter restarts
    assert check.view()["integrated_kwh"] == 0.0
    t = sun(check, t + 1, 60)
    assert check.view()["integrated_kwh"] > 5
    check.feed({"pv_energy_today": 1.0}, t, 1)  # a new energy series
    assert check.view()["integrated_kwh"] == 0.0


def test_an_unknown_power_value_pauses_the_integral() -> None:
    check = EnergyPlausibility()
    check.feed({"pv_energy_today": 0.0}, 0.0, 0)
    check.feed({"pv1_power": 1000.0}, 1.0, 0)
    check.feed({"pv1_power": None}, 61.0, 0)
    check.feed({"pv1_power": 1000.0}, 121.0, 0)
    assert check.view()["integrated_kwh"] == 0.0
