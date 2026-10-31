"""Plant totals (0.2.7): sums, staleness, series transitions, master selection, config."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.config import InverterConfig, PlantConfig, PollConfig, TotalsConfig
from deye_monitor.ha_discovery import Namespace, plant_components, validate_component
from deye_monitor.plant import plant_from_body, plant_to_body
from deye_monitor.profiles.schema import load_profile
from deye_monitor.totals import (
    METRIC_BY_KEY,
    METRICS,
    PlantSeries,
    PlantTotals,
    TotalsMember,
    known_series,
)

PROFILE = load_profile("sg04lp3_lv_v105_8")
POLL = PollConfig()
FAST = {
    "pv1_power",
    "pv2_power",
    "battery_voltage",
    "battery_current",
    "load_power_total",
    "inverter_power_total",
}
ENERGY = {
    "pv_energy_total",
    "battery_energy_charge_total",
    "battery_energy_discharge_total",
    "load_energy_total",
    "battery_energy_charge_today",
    "battery_energy_discharge_today",
    "load_energy_today",
    "pv_energy_today",
}
BMS = {"battery_soc", "bms_soc", "bms_voltage", "bms_current", "bms_temperature"}
SELECTED = FAST | ENERGY | BMS


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        ids: tuple[str, ...] = ("m", "s1", "s2"),
        roles: dict[str, str | None] | None = None,
        series: dict[str, str] | None = None,
    ) -> None:
        self.now = 1000.0
        self.roles = roles or {"m": "master", "s1": "slave", "s2": "slave"}
        self.series = series or {i: "g0" for i in ids}
        self.keys = {i: set(SELECTED) for i in ids}
        self.totals = PlantTotals(
            [TotalsMember(i, i.upper(), PROFILE, POLL) for i in ids],
            PlantSeries(tmp_path),
            keys_of=lambda i: self.keys[i],
            role_of=lambda i: self.roles.get(i),
            member_series_of=lambda i: self.series[i],
        )
        for i in ids:
            self.totals.set_online(i, True)

    def feed(self, member: str, **values: float | None) -> None:
        self.totals.update(member, values, self.now)

    def fast(self, member: str, load: float, pv: float = 0.0) -> None:
        self.feed(
            member,
            pv1_power=pv,
            pv2_power=0,
            battery_voltage=50.0,
            battery_current=-2.0,
            load_power_total=load,
            inverter_power_total=load,
        )

    def value(self, key: str) -> float | None:
        return self.totals.value(METRIC_BY_KEY[key], self.now)


def test_power_totals_add_the_members_and_battery_power_is_voltage_times_current(
    tmp_path: Path,
) -> None:
    r = Rig(tmp_path)
    r.fast("m", 0, pv=100)
    r.fast("s1", 120, pv=200)
    r.fast("s2", 120)
    assert r.value("plant_load_power") == 240
    assert r.value("plant_pv_power") == 300
    assert r.value("plant_inverter_power") == 240
    assert r.value("plant_battery_power") == -300  # three members charging with 2 A at 50 V


def test_a_missing_member_makes_the_total_unknown_never_a_partial_sum(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.fast("m", 0)
    r.fast("s1", 120)
    assert r.value("plant_load_power") is None
    r.fast("s2", 120)
    assert r.value("plant_load_power") == 240


def test_a_stale_member_value_makes_the_total_unknown(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    for member in ("m", "s1", "s2"):
        r.fast(member, 100)
        r.feed(member, **dict.fromkeys(ENERGY, 1.0))
    assert r.value("plant_load_power") == 300
    r.now += 3 * POLL.fast_s + 1  # one window of three poll intervals is over
    assert r.value("plant_load_power") is None
    r.fast("m", 100)
    r.fast("s1", 100)
    assert r.value("plant_load_power") is None  # s2 is still stale
    assert r.totals.stale_members(r.now) == ["s2"]


def test_a_none_value_and_an_offline_member_make_the_total_unknown(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    for member in ("m", "s1", "s2"):
        r.fast(member, 100)
    r.feed("s1", load_power_total=None)
    assert r.value("plant_load_power") is None
    r.fast("s1", 100)
    r.totals.set_online("s2", False)
    r.totals.drop("s2")
    assert r.value("plant_load_power") is None
    assert "s2" in r.totals.stale_members(r.now)


def test_a_member_without_pv_inputs_adds_zero_to_pv_but_not_to_load(tmp_path: Path) -> None:
    r = Rig(tmp_path, ids=("a", "b"))
    r.keys["b"] -= {"pv1_power", "pv2_power", "pv_energy_total", "pv_energy_today"}
    r.fast("a", 10, pv=500)
    r.feed(
        "b", battery_voltage=50.0, battery_current=0.0, load_power_total=5, inverter_power_total=5
    )
    assert r.value("plant_pv_power") == 500
    assert r.value("plant_load_power") == 15


def test_energy_totals_sum_the_counters(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    for member, kwh in (("m", 100.0), ("s1", 50.5), ("s2", 20.25)):
        r.feed(
            member,
            load_energy_total=kwh,
            pv_energy_total=kwh,
            battery_energy_charge_total=kwh,
            battery_energy_discharge_total=kwh,
            load_energy_today=1.0,
            pv_energy_today=1.5,
            battery_energy_charge_today=0.5,
            battery_energy_discharge_today=0.25,
        )
    assert r.value("plant_load_energy_total") == 170.75
    assert r.value("plant_pv_energy_total") == 170.75
    assert r.value("plant_load_energy_today") == 3.0
    assert r.value("plant_pv_energy_today") == 4.5


def test_battery_state_comes_from_the_master_only(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.feed("m", battery_soc=80, bms_soc=81, bms_voltage=52.0, bms_current=-10, bms_temperature=17.5)
    r.feed("s1", battery_soc=0, bms_soc=0, bms_voltage=0, bms_current=0, bms_temperature=-100)
    r.feed("s2", battery_soc=0, bms_soc=0, bms_voltage=0, bms_current=0, bms_temperature=-100)
    assert r.value("plant_battery_soc") == 80  # not averaged
    assert r.value("plant_bms_soc") == 81
    assert r.value("plant_bms_current") == -10
    assert r.value("plant_bms_power") == 520  # bank discharging: positive
    assert r.value("plant_bms_temperature") == 17.5
    assert r.totals.master() == "m"


@pytest.mark.parametrize(
    "roles",
    [
        {"m": "slave", "s1": "slave", "s2": "slave"},
        {"m": None, "s1": None, "s2": None},
        {"m": "master", "s1": "master", "s2": "slave"},
    ],
)
def test_without_exactly_one_master_the_battery_state_is_unknown(
    tmp_path: Path, roles: dict[str, str | None]
) -> None:
    r = Rig(tmp_path, roles=roles)
    for member in ("m", "s1", "s2"):
        r.feed(member, battery_soc=80, bms_soc=81)
    assert r.totals.master() is None
    assert r.value("plant_battery_soc") is None and r.value("plant_bms_soc") is None


# -- series --------------------------------------------------------------------------------------


def test_a_member_series_change_starts_a_new_plant_series_and_voids_old_counters(
    tmp_path: Path,
) -> None:
    r = Rig(tmp_path)
    assert r.totals.series.series == "g0"
    for member in ("m", "s1", "s2"):
        r.feed(member, load_energy_total=10.0)
    assert r.value("plant_load_energy_total") == 30.0
    r.series["s1"] = "g1"  # resolution change or counter reset of one member
    r.totals.drop_energy("s1")
    assert r.totals.sync_series() is True
    assert r.totals.series.series == "g1"
    assert r.value("plant_load_energy_total") is None  # no drop, no jump: unknown until s1 reports
    r.feed("s1", load_energy_total=0.5)
    assert r.value("plant_load_energy_total") == 20.5
    assert r.totals.sync_series() is False


def test_the_series_survives_a_restart_and_a_membership_change_starts_the_next(
    tmp_path: Path,
) -> None:
    first = Rig(tmp_path)
    assert first.totals.series.series == "g0"
    same = Rig(tmp_path)
    assert same.totals.series.series == "g0"
    fewer = Rig(tmp_path, ids=("m", "s1"))
    assert fewer.totals.series.series == "g1"  # membership changed
    back = Rig(tmp_path)
    assert back.totals.series.series == "g2"  # never reused


def test_a_lost_series_file_continues_after_the_discovery_inventory(tmp_path: Path) -> None:
    keys = ["plant_load_energy_total_g4", "plant_pv_energy_total_g7", "plant_pv_power"]
    assert known_series(keys) == {"g4", "g7"}
    series = PlantSeries(tmp_path, known_series(keys))
    assert series.follow([("m", "g0"), ("s1", "g0")]) is True
    assert series.series == "g8"


def test_a_damaged_series_file_is_not_reused(tmp_path: Path) -> None:
    (tmp_path / "plant-totals.json").write_text("{not json")
    series = PlantSeries(tmp_path)
    series.follow([("m", "g0")])
    assert series.series == "g0"
    assert PlantSeries(tmp_path).series == "g0"


# -- config and discovery ------------------------------------------------------------------------


def _inverters(*ids: str) -> tuple[InverterConfig, ...]:
    return tuple(
        InverterConfig(id=i, name=i.upper(), endpoint="gw", unit_id=n + 1)
        for n, i in enumerate(ids)
    )


def test_membership_defaults_to_all_and_needs_two_unless_enabled() -> None:
    invs = _inverters("a", "b", "c")
    assert TotalsConfig().members(invs) == ("a", "b", "c")
    assert TotalsConfig().active(invs)
    assert not TotalsConfig().active(invs[:1])
    assert TotalsConfig(allow_single=True).active(invs[:1])
    assert not TotalsConfig(exclude=("a", "b")).active(invs)
    assert not TotalsConfig().active(())


def test_exclusions_of_deleted_inverters_are_pruned_and_the_body_round_trips() -> None:
    from deye_monitor.config import EndpointConfig, Transport

    endpoint = EndpointConfig(id="gw", transport=Transport.MODBUS_TCP, host="10.0.0.5")
    plant = PlantConfig(
        endpoints=(endpoint,),
        inverters=_inverters("a", "b"),
        totals=TotalsConfig(exclude=("b", "gone")),
    )
    assert plant.totals.exclude == ("b",)
    body = plant_to_body(plant, 3)
    assert plant_from_body(body) == plant
    default = plant_to_body(PlantConfig(endpoints=(endpoint,), inverters=_inverters("a")), 1)
    assert "totals" not in default  # the default is not stored


def test_plant_components_are_valid_stable_and_series_guarded() -> None:
    ns = Namespace("ab12cd34", "homeassistant", "deye")
    comps = plant_components(ns, "g3", {m.key: 30 for m in METRICS})
    for key, comp in comps.items():
        validate_component(key, comp)
        assert comp["unique_id"].startswith(f"{ns.app_device_id}_")
        assert comp["state_topic"] == ns.plant_state_topic
    energy = comps["plant_load_energy_total_g3"]
    assert energy["state_class"] == "total_increasing" and energy["device_class"] == "energy"
    assert "energy_series') == 'g3'" in energy["value_template"]
    assert comps["plant_load_power"]["unique_id"] == f"{ns.app_device_id}_plant_load_power"
    assert "state_class" in comps["plant_pv_energy_today"]
    assert comps["plant_pv_energy_today"]["state_class"] == "measurement"
    assert comps["plant_members"]["state_class"] == "measurement"
