"""Plausibility of the energy resolution: daily PV counter vs integrated PV power, compared over
equal, fresh windows and one common measuring boundary (R39-01/02)."""

from __future__ import annotations

from typing import Any

from deye_monitor.energy_check import (
    PLAUSIBLE,
    TOO_HIGH,
    TOO_LOW,
    UNKNOWN,
    EnergyPlausibility,
)

ONE = frozenset({"pv1_power"})
BOTH = frozenset({"pv1_power", "pv2_power"})


def counter(
    check: EnergyPlausibility, kwh: float, t: float, keys: frozenset[str] | None = ONE
) -> None:
    check.feed({"pv_energy_today": kwh}, t, 0, "g0", keys)


def power(
    check: EnergyPlausibility, values: dict[str, Any], t: float, keys: frozenset[str] | None = ONE
) -> None:
    check.feed(values, t, 0, "g0", keys)


def run(
    counter_per_hour_kwh: float, watts: float = 5000.0, hours: int = 2
) -> tuple[EnergyPlausibility, float]:
    check = EnergyPlausibility()
    counter(check, 10.0, 0.0)
    t = 0.0
    for step in range(hours * 60):
        t += 60.0
        power(check, {"pv1_power": watts}, t)
        if step % 10 == 9:  # the slow energy group
            counter(check, 10.0 + counter_per_hour_kwh * t / 3600.0, t)
    return check, t


def test_matching_counter_is_plausible() -> None:
    check, t = run(5.0)
    assert check.view(t)["state"] == PLAUSIBLE


def test_counter_ten_times_too_high() -> None:
    check, t = run(50.0)
    view = check.view(t)
    assert view["state"] == TOO_HIGH and 9 < view["ratio"] < 11


def test_counter_ten_times_too_low() -> None:
    check, t = run(0.5)
    assert check.view(t)["state"] == TOO_LOW


def test_too_little_data_is_unknown() -> None:
    check, t = run(50.0, watts=200.0, hours=1)
    assert check.view(t)["state"] == UNKNOWN
    assert EnergyPlausibility().view()["state"] == UNKNOWN


def test_a_gap_in_the_power_does_not_count_the_counter_delta() -> None:
    """R39-01: constant 6 kW, correct counter, two observed segments three hours apart."""
    check = EnergyPlausibility()
    t, kwh = 0.0, 0.0
    counter(check, kwh, t)
    for _ in range(2):
        for _ in range(60):  # ten minutes
            t += 10.0
            kwh += 6.0 * 10.0 / 3600.0
            power(check, {"pv1_power": 6000.0}, t)
            if int(t) % 60 == 0:
                counter(check, kwh, t)
        t += 3 * 3600.0  # the monitor was blind for three hours
        kwh += 18.0
    view = check.view(t - 3 * 3600.0)
    assert view["state"] != TOO_HIGH
    assert abs(view["counter_kwh"] - view["integrated_kwh"]) < 0.3


def test_a_stale_counter_expires_and_is_not_compared_to_later_power() -> None:
    """R39-01: counter last read at 180 s, power goes on to 1800 s (factor 0.1 is right)."""
    check = EnergyPlausibility()
    counter(check, 0.0, 0.0)
    for t in range(10, 1801, 10):
        power(check, {"pv1_power": 6000.0}, float(t))
        if t <= 180 and t % 60 == 0:
            counter(check, 6.0 * t / 3600.0, float(t))
    view = check.view(1800.0)
    assert view["state"] == UNKNOWN and view["ratio"] is None


def test_a_failed_energy_read_lets_the_verdict_expire() -> None:
    check, t = run(50.0)
    assert check.view(t)["state"] == TOO_HIGH
    assert check.view(t + 1000.0)["state"] == UNKNOWN


def test_a_failed_power_read_lets_the_verdict_expire() -> None:
    check, t = run(50.0)
    assert check.view(t + 400.0)["state"] == UNKNOWN


def test_one_of_two_active_mppts_is_not_a_complete_boundary() -> None:
    """R39-02: both MPPTs produce, only PV1 is selected: nothing may be judged."""
    check = EnergyPlausibility()
    counter(check, 0.0, 0.0, None)
    t = 0.0
    for _ in range(120):
        t += 60.0
        power(check, {"pv1_power": 500.0}, t, None)  # PV1 only; the counter counts 10x that
        counter(check, 5.0 * t / 3600.0, t, None)
    assert check.view(t)["state"] == UNKNOWN


def test_a_counted_power_missing_in_a_sample_breaks_the_window() -> None:
    check = EnergyPlausibility()
    counter(check, 0.0, 0.0, BOTH)
    t = 0.0
    for _ in range(120):
        t += 60.0
        power(check, {"pv1_power": 500.0}, t, BOTH)  # PV2 absent from every sample
        counter(check, 5.0 * t / 3600.0, t, BOTH)
    assert check.view(t)["state"] == UNKNOWN


def test_both_mppts_summed_over_the_boundary() -> None:
    check = EnergyPlausibility()
    counter(check, 0.0, 0.0, BOTH)
    t = 0.0
    for _ in range(120):
        t += 60.0
        power(check, {"pv1_power": 2500.0, "pv2_power": 2500.0}, t, BOTH)
        counter(check, 5.0 * t / 3600.0, t, BOTH)
    assert check.view(t)["state"] == PLAUSIBLE


def test_a_changed_boundary_starts_over() -> None:
    check, t = run(50.0)
    assert check.view(t)["integrated_kwh"] > 5
    check.feed({"pv_energy_today": 1.0}, t, 0, "g0", BOTH)  # a different set of counted sources
    assert check.view(t)["integrated_kwh"] == 0.0


def sun(check: EnergyPlausibility, start: float, minutes: int, kwh: float) -> tuple[float, float]:
    t = start
    for _ in range(minutes + 1):
        power(check, {"pv1_power": 6000.0}, t)
        counter(check, kwh, t)
        kwh += 0.1
        t += 60.0
    return t, kwh


def test_counter_reset_and_new_series_start_over() -> None:
    check = EnergyPlausibility()
    counter(check, 20.0, 0.0)
    t, _ = sun(check, 1.0, 60, 20.0)
    assert check.view(t)["integrated_kwh"] > 5
    counter(check, 0.0, t)  # midnight: the daily counter restarts
    assert check.view(t)["integrated_kwh"] == 0.0
    t, _ = sun(check, t + 1, 60, 0.0)
    assert check.view(t)["integrated_kwh"] > 5
    check.feed({"pv_energy_today": 1.0}, t, 1, "g1", ONE)  # a new energy series
    assert check.view(t)["integrated_kwh"] == 0.0
    check.feed({"pv_energy_today": 1.0}, t, 1, "xxxxxxxxg1", ONE)  # same number, another series
    assert check.view(t)["integrated_kwh"] == 0.0


def test_an_unknown_power_value_pauses_the_integral() -> None:
    check = EnergyPlausibility()
    counter(check, 0.0, 0.0)
    power(check, {"pv1_power": 1000.0}, 1.0)
    power(check, {"pv1_power": None}, 61.0)
    power(check, {"pv1_power": 1000.0}, 121.0)
    counter(check, 1.0, 122.0)
    assert check.view(122.0)["integrated_kwh"] == 0.0
