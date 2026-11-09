"""The plant DC/AC power difference: PV + battery power - inverter output, 5-minute mean."""

from __future__ import annotations

from pathlib import Path

from tests.test_totals import Rig

IDS = ("a", "b", "c", "d")
# SYNTHETIC fixture: the per-unit battery powers are the R590 values of the night snapshot, fed as
# V x I at 50 V (not the field V x I result); discharging is positive as the plant sums it
BATTERY = (603.0, 860.0, 874.0, 905.0)
OUTPUT = (485.0, 779.0, 794.0, 810.0)


def feed_night(rig: Rig, scale: float = 1.0, pv: float = 0.0) -> None:
    for i, bat, out in zip(IDS, BATTERY, OUTPUT, strict=True):
        rig.feed(
            i,
            pv1_power=pv,
            pv2_power=0,
            battery_voltage=50.0,
            battery_current=bat * scale / 50.0,
            inverter_power_total=out,
            load_power_total=out,
        )


def test_four_members_at_night_give_the_snapshot_difference(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ids=IDS)
    feed_night(rig)
    assert rig.value("plant_dc_ac_power_difference") == 3242 - 2868  # 374 W


def test_the_pv_power_is_part_of_the_difference_and_charging_is_negative(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ids=IDS)
    feed_night(rig, pv=100)  # 4 x 100 W more PV input
    assert rig.value("plant_dc_ac_power_difference") == 374 + 400
    feed_night(rig, scale=-1.0)  # charging: battery power below zero
    assert rig.value("plant_dc_ac_power_difference") == -3242 - 2868


def fresh_rig(tmp_path: Path) -> Rig:
    rig = Rig(tmp_path, ids=IDS)
    feed_night(rig)
    assert rig.value("plant_dc_ac_power_difference") == 374  # every case starts from a fresh state
    return rig


def test_an_offline_member_makes_it_unknown(tmp_path: Path) -> None:
    rig = fresh_rig(tmp_path)
    rig.totals.set_online("d", False)
    assert rig.value("plant_dc_ac_power_difference") is None


def test_a_deselected_battery_or_output_input_makes_it_unknown(tmp_path: Path) -> None:
    for key in ("inverter_power_total", "battery_voltage", "battery_current"):
        rig = fresh_rig(tmp_path)
        rig.keys["d"].discard(key)
        assert rig.value("plant_dc_ac_power_difference") is None, key


def test_stale_inputs_make_it_unknown(tmp_path: Path) -> None:
    rig = fresh_rig(tmp_path)
    rig.now += 10_000
    assert rig.value("plant_dc_ac_power_difference") is None


# -- R61-01: the PV channels come from the installation description ----------


def test_only_the_used_mppt_channel_counts_and_an_unused_one_adds_zero(tmp_path: Path) -> None:
    rig = fresh_rig(tmp_path)
    for i in IDS:
        rig.mppt[i] = (True, False)
        rig.keys[i].discard("pv2_power")  # an explicitly unused channel needs no selection
    feed_night(rig, pv=50)
    assert rig.value("plant_dc_ac_power_difference") == 374 + 4 * 50


def test_a_used_channel_that_is_deselected_makes_it_unknown(tmp_path: Path) -> None:
    rig = fresh_rig(tmp_path)
    rig.keys["b"].discard("pv2_power")  # both channels used, MPPT2 not selected
    assert rig.value("plant_dc_ac_power_difference") is None
    rig = fresh_rig(tmp_path)
    for key in ("pv1_power", "pv2_power"):
        rig.keys["b"].discard(key)  # PV used but nothing selected
    assert rig.value("plant_dc_ac_power_difference") is None


def test_no_used_channel_gives_no_pv_contribution_even_with_a_selection(tmp_path: Path) -> None:
    rig = fresh_rig(tmp_path)
    for i in IDS:
        rig.mppt[i] = (False, False)
    feed_night(rig, pv=999)  # selected, but the description says nothing is connected
    assert rig.value("plant_dc_ac_power_difference") == 374
    for key in ("pv1_power", "pv2_power"):
        rig.keys["a"].discard(key)
    assert rig.value("plant_dc_ac_power_difference") == 374


def test_a_used_channel_that_is_missing_or_stale_makes_it_unknown(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ids=("a",))
    rig.feed("a", battery_voltage=50.0, battery_current=1.0, inverter_power_total=10)
    assert rig.value("plant_dc_ac_power_difference") is None  # no PV value delivered yet
    rig.feed("a", pv1_power=5, pv2_power=5)
    assert rig.value("plant_dc_ac_power_difference") == 5 + 5 + 50 - 10
    rig.now += 10_000
    assert rig.value("plant_dc_ac_power_difference") is None


# -- R61-02: a gap in the inputs ends the mean even without a value() call ----------


def test_a_stale_battery_ends_the_mean_when_only_the_battery_updates(tmp_path: Path) -> None:
    """R61-02 (short probe): the battery goes stale while PV and AC keep arriving; the update that
    renews it must start a new window instead of averaging with the old samples."""
    rig = Rig(tmp_path, ids=("a",))  # fast group: fresh for 30 s

    def battery(watts: float) -> None:
        rig.feed("a", battery_voltage=50.0, battery_current=watts / 50.0)

    def pv_ac() -> None:
        rig.feed("a", pv1_power=0, pv2_power=0, inverter_power_total=400)

    battery(600)  # t=0: complete, 600 + 0 - 400 = 200 W
    pv_ac()
    for t in (10, 20, 30):  # PV/AC are renewed, the battery (age 30 s at most) is still fresh
        rig.now = 1000.0 + t
        pv_ac()
    assert rig.value("plant_dc_ac_power_difference") == 200
    rig.now = 1000.0 + 31  # the battery is now 31 s old: stale before this update overwrites it
    battery(1000)
    assert [v for _, v in rig.totals._samples] == [600]  # exactly one new sample, no old 200 W
    assert rig.value("plant_dc_ac_power_difference") == 600


def test_the_mean_covers_five_minutes_and_restarts_after_a_gap(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ids=IDS)
    for _ in range(40):  # 200 s at 374 W
        feed_night(rig)
        rig.now += 5
    feed_night(rig, scale=1.1)  # 698.2 W
    mixed = rig.value("plant_dc_ac_power_difference")
    assert mixed is not None and 374 < mixed < 698.2  # a mean, not the last value
    for _ in range(70):  # 350 s: the old samples have left the window
        rig.now += 5
        feed_night(rig, scale=1.1)
    assert rig.value("plant_dc_ac_power_difference") == 698.2
    rig.totals.set_online("a", False)  # a member drop resets the window
    rig.totals.set_online("a", True)
    assert rig.value("plant_dc_ac_power_difference") is None
    feed_night(rig)
    assert rig.value("plant_dc_ac_power_difference") == 374


def test_the_metric_is_a_diagnostic_component() -> None:
    from deye_monitor.ha_discovery import Namespace, plant_components

    ns = Namespace("ab12cd34", "homeassistant", "deye")
    comps = plant_components(ns, "g0", {})
    comp = comps["plant_dc_ac_power_difference"]
    assert comp["entity_category"] == "diagnostic" and comp["unit_of_measurement"] == "W"
    assert comp["name"] == "Plant DC/AC power difference"
