"""Plant totals (0.2.7): sums, staleness, series transitions, master selection, config."""

from __future__ import annotations

from pathlib import Path

import pytest

from deye_monitor.config import InverterConfig, PlantConfig, PollConfig, TotalsConfig
from deye_monitor.ha_discovery import Namespace, plant_components, validate_component
from deye_monitor.plant import plant_from_body, plant_to_body
from deye_monitor.profiles.schema import load_profile
from deye_monitor.totals import (
    ENERGY_METRICS,
    METRIC_BY_KEY,
    METRICS,
    PLANT_INPUTS,
    Metric,
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
BMS = {
    "battery_soc",
    "bms_soc",
    "bms_voltage",
    "bms_current",
    "bms_temperature",
    "battery_temperature",
}
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
        self.gen: set[str] = set()  # members whose description has a GEN port use
        self.grid: set[str] = set()  # members whose description has a used GRID port
        self.totals = PlantTotals(
            [TotalsMember(i, i.upper(), PROFILE, POLL) for i in ids],
            PlantSeries(tmp_path),
            keys_of=lambda i: self.keys[i],
            role_of=lambda i: self.roles.get(i),
            member_series_of=lambda i: self.series[i],
            gen_use_of=lambda i: i in self.gen,
            grid_use_of=lambda i: i in self.grid,
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


LIFETIME = [m for m in ENERGY_METRICS if m.state_class == "total_increasing"]


def test_the_lifetime_energy_metrics_are_pv_battery_and_load() -> None:
    assert {m.key for m in LIFETIME} == {
        "plant_pv_energy_total",
        "plant_battery_charge_energy_total",
        "plant_battery_discharge_energy_total",
        "plant_load_energy_total",
    }


@pytest.mark.parametrize("metric", LIFETIME, ids=lambda m: m.key)
def test_an_energy_total_never_loses_a_member_that_has_not_selected_it(
    tmp_path: Path, metric: Metric
) -> None:
    key = metric.keys[0]
    r = Rig(tmp_path, ids=("a", "b"))
    for member, kwh in (("a", 100.0), ("b", 40.0)):
        r.feed(member, **{key: kwh})
    assert r.value(metric.key) == 140.0
    # the selection of one member loses the counter for a cycle: unknown, never the lower 100
    r.keys["b"] -= {key}
    assert r.value(metric.key) is None
    r.keys["b"] |= {key}
    assert r.value(metric.key) == 140.0
    # a null reading is unknown too, and the next real reading recovers the sum
    r.feed("b", **{key: None})
    assert r.value(metric.key) is None
    r.feed("b", **{key: 41.0})
    assert r.value(metric.key) == 141.0


@pytest.mark.parametrize("metric", LIFETIME, ids=lambda m: m.key)
def test_an_energy_total_is_unknown_until_every_member_has_read_it(
    tmp_path: Path, metric: Metric
) -> None:
    key = metric.keys[0]
    r = Rig(tmp_path, ids=("a", "b"))
    r.keys["b"] -= {key}
    r.feed("a", **{key: 100.0})
    assert r.value(metric.key) is None  # b has not selected the counter
    r2 = Rig(tmp_path / "x", ids=("a", "b"))
    r2.feed("a", **{key: 100.0})  # b selected it but has not read it yet
    assert r2.value(metric.key) is None


def test_battery_state_comes_from_the_master_only(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.feed(
        "m",
        battery_soc=80,
        bms_soc=81,
        bms_voltage=52.0,
        bms_current=-10,
        bms_temperature=17.5,
        battery_temperature=18.5,
    )
    r.feed("s1", battery_soc=0, bms_soc=0, bms_voltage=0, bms_current=0, bms_temperature=-100)
    r.feed("s2", battery_soc=0, bms_soc=0, bms_voltage=0, bms_current=0, bms_temperature=-100)
    assert r.value("plant_battery_soc") == 81  # the master's BMS value, not averaged
    assert r.value("plant_battery_temperature") == 17.5
    assert r.value("plant_bms_current") == -10
    assert r.value("plant_bms_power") == 520  # bank discharging: positive
    assert r.totals.master() == "m"
    assert "plant_bms_soc" not in METRIC_BY_KEY and "plant_bms_temperature" not in METRIC_BY_KEY


def test_battery_soc_and_temperature_fall_back_to_the_masters_inverter_values(
    tmp_path: Path,
) -> None:
    r = Rig(tmp_path)
    r.keys["m"] -= {"bms_soc", "bms_temperature"}  # no BMS: the inverter's own values
    r.feed("m", battery_soc=80, bms_soc=81, battery_temperature=18.5, bms_temperature=17.5)
    assert r.value("plant_battery_soc") == 80
    assert r.value("plant_battery_temperature") == 18.5
    r.keys["m"] -= {"battery_soc", "battery_temperature"}  # neither selected: unknown
    assert r.value("plant_battery_soc") is None and r.value("plant_battery_temperature") is None


def test_the_plant_inputs_are_derived_from_the_metrics() -> None:
    assert {i["metric"] for i in PLANT_INPUTS["load_power_total"]} == {"plant_load_power"}
    assert {i["metric"] for i in PLANT_INPUTS["bms_voltage"]} == {
        "plant_bms_voltage",
        "plant_bms_power",
    }
    soc = PLANT_INPUTS["battery_soc"][0]
    assert soc["mode"] == "any" and soc["keys"] == ["bms_soc", "battery_soc"] and soc["master_only"]
    assert PLANT_INPUTS["pv1_power"][0]["mode"] == "optional"
    assert not PLANT_INPUTS["battery_current"][0]["master_only"]
    assert "ups_power_total" not in PLANT_INPUTS


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
    assert r.value("plant_battery_soc") is None and r.value("plant_battery_temperature") is None


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


def test_gen_power_sums_every_members_reported_share(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ids=("m", "s1", "s2", "s3"))
    for i in rig.keys:
        rig.fast(i, load=100)
    assert rig.totals.gen_active() is False
    assert rig.value("plant_gen_power") == 0  # no GEN use anywhere: not published
    rig.gen = {"m"}  # the slaves are described as unused but still report a share
    for i in rig.keys:
        rig.keys[i].add("gen_power_raw_total")
        rig.feed(i, gen_power_raw_total=652)
    assert rig.totals.gen_active() is True
    assert rig.totals.view(rig.now)["gen"] is True
    assert rig.value("plant_gen_power") == 2608
    rig.feed("s1", gen_power_raw_total=-300)  # unequal shares are summed as reported
    assert rig.value("plant_gen_power") == 652 * 3 - 300


def test_parallel_gen_power_is_unknown_while_any_share_is_missing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ids=("m", "s1"))
    for i in rig.keys:
        rig.fast(i, load=100)
        rig.keys[i].add("gen_power_raw_total")
    rig.gen = {"m"}
    rig.feed("m", gen_power_raw_total=650)
    assert rig.value("plant_gen_power") is None  # s1 delivered nothing yet
    rig.feed("s1", gen_power_raw_total=650)
    assert rig.value("plant_gen_power") == 1300
    rig.keys["s1"].discard("gen_power_raw_total")  # not read any more: the cache is not a value
    assert rig.value("plant_gen_power") is None
    rig.keys["s1"].add("gen_power_raw_total")
    rig.totals.set_online("s1", False)
    assert rig.value("plant_gen_power") is None
    rig.totals.set_online("s1", True)
    rig.now += 10_000  # stale
    assert rig.value("plant_gen_power") is None


def test_standalone_gen_power_is_the_members_own_value(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ids=("only",), roles={"only": "standalone"})
    rig.fast("only", load=100)
    rig.gen = {"only"}
    assert rig.value("plant_gen_power") is None  # used GEN port, sensor not selected
    rig.keys["only"].add("gen_power_raw_total")
    assert rig.value("plant_gen_power") is None  # not delivered yet
    rig.feed("only", gen_power_raw_total=-300)
    assert rig.value("plant_gen_power") == -300  # signed, direction not interpreted
    rig.gen = set()
    assert rig.value("plant_gen_power") == 0


def test_the_gen_component_exists_only_when_a_member_describes_a_gen_use() -> None:
    ns = Namespace("ab12cd34", "homeassistant", "deye")
    expire = {m.key: 30 for m in METRICS}
    with_gen = plant_components(ns, "g0", expire)
    without = plant_components(ns, "g0", expire, gates=frozenset({"grid"}))
    assert set(with_gen) - set(without) == {"plant_gen_power"}
    comp = with_gen["plant_gen_power"]
    validate_component("plant_gen_power", comp)
    assert comp["name"] == "Plant GEN port power"
    assert comp["unit_of_measurement"] == "W" and comp["device_class"] == "power"
    assert comp["state_class"] == "measurement"
    assert comp["suggested_display_precision"] == 0
    assert PLANT_INPUTS["gen_power_raw_total"][0]["metric"] == "plant_gen_power"


# -- GRID diagnostics (0.5.3, task 45 stage A) ---------------------------------------------------


def _grid_rig(tmp_path: Path, ids: tuple[str, ...] = ("m", "s1", "s2")) -> Rig:
    rig = Rig(tmp_path, ids=ids)
    rig.grid = set(ids)
    for i in ids:
        rig.keys[i] |= {
            "grid_internal_power_total",
            "grid_voltage_a",
            "grid_voltage_b",
            "grid_voltage_c",
            "grid_frequency",
        }
    return rig


def _grid_feed(rig: Rig, member: str, power: float, volts: float = 230.0, hz: float = 50.0) -> None:
    rig.feed(
        member,
        grid_internal_power_total=power,
        grid_voltage_a=volts,
        grid_voltage_b=volts + 1,
        grid_voltage_c=volts + 2,
        grid_frequency=hz,
    )


def test_grid_terminal_power_sums_the_used_ports_signed(tmp_path: Path) -> None:
    rig = _grid_rig(tmp_path)
    for i, p in (("m", 1500), ("s1", -400), ("s2", 100)):
        _grid_feed(rig, i, p)
    assert rig.totals.grid_active() and rig.totals.gates() == {"grid"}
    assert rig.value("plant_grid_terminal_power") == 1200
    assert METRIC_BY_KEY["plant_inverter_power"] is not METRIC_BY_KEY["plant_grid_terminal_power"]


def test_grid_terminal_power_unused_ports_add_zero_and_unselected_used_ports_are_unknown(
    tmp_path: Path,
) -> None:
    rig = _grid_rig(tmp_path)
    _grid_feed(rig, "m", 800)
    _grid_feed(rig, "s1", 5)
    rig.grid = {"m", "s1"}  # s2 explicitly unused: adds 0 even with stray data
    _grid_feed(rig, "s2", 999)
    assert rig.value("plant_grid_terminal_power") == 805
    rig.grid = {"m", "s1", "s2"}
    rig.keys["s2"].discard("grid_internal_power_total")  # used, not selected: not 0
    assert rig.value("plant_grid_terminal_power") is None
    assert "s2" in rig.totals.stale_members(rig.now)


def test_grid_terminal_power_is_unknown_for_a_stale_missing_or_offline_member(
    tmp_path: Path,
) -> None:
    rig = _grid_rig(tmp_path)
    _grid_feed(rig, "m", 100)
    _grid_feed(rig, "s1", 100)
    assert rig.value("plant_grid_terminal_power") is None  # s2 has not delivered
    _grid_feed(rig, "s2", 100)
    assert rig.value("plant_grid_terminal_power") == 300
    rig.totals.set_online("s2", False)
    assert rig.value("plant_grid_terminal_power") is None
    rig.totals.set_online("s2", True)
    rig.now += 10_000
    assert rig.value("plant_grid_terminal_power") is None


def test_grid_reference_is_the_unique_master_never_the_first_member_or_an_average(
    tmp_path: Path,
) -> None:
    rig = _grid_rig(tmp_path, ids=("a", "m", "s"))
    rig.roles = {"a": "slave", "m": "master", "s": "slave"}
    _grid_feed(rig, "a", 0, volts=100.0, hz=40.0)
    _grid_feed(rig, "m", 0, volts=231.5, hz=50.02)
    _grid_feed(rig, "s", 0, volts=300.0, hz=60.0)
    assert rig.totals.reference() == "m"
    assert rig.value("plant_grid_voltage_a") == 231.5
    assert rig.value("plant_grid_voltage_b") == 232.5
    assert rig.value("plant_grid_voltage_c") == 233.5
    assert rig.value("plant_grid_frequency") == 50.02


@pytest.mark.parametrize(
    "roles",
    [
        {"a": "master", "b": "master"},  # two masters
        {"a": "slave", "b": "slave"},  # no master
        {"a": None, "b": None},  # unknown roles
        {"a": "standalone", "b": "standalone"},  # two standalones are no common bus
        {"a": "master", "b": None},
    ],
)
def test_grid_reference_with_an_ambiguous_or_unknown_role_is_unavailable(
    tmp_path: Path, roles: dict[str, str | None]
) -> None:
    rig = _grid_rig(tmp_path, ids=("a", "b"))
    rig.roles = roles
    for i in ("a", "b"):
        _grid_feed(rig, i, 0)
    if roles == {"a": "master", "b": None}:
        assert rig.totals.reference() == "a"  # a unique master is enough
    else:
        assert rig.totals.reference() is None
        assert rig.value("plant_grid_voltage_a") is None
        assert rig.value("plant_grid_frequency") is None
    assert rig.value("plant_grid_terminal_power") == 0  # the sum does not need a reference


def test_grid_reference_of_a_single_member_plant_is_that_member_unless_it_is_a_slave(
    tmp_path: Path,
) -> None:
    rig = _grid_rig(tmp_path, ids=("only",))
    rig.roles = {"only": "standalone"}
    _grid_feed(rig, "only", 10, volts=229.0)
    assert rig.value("plant_grid_voltage_a") == 229.0
    rig.roles = {"only": "slave"}
    assert rig.value("plant_grid_voltage_a") is None


def test_grid_reference_zero_is_a_reading_but_missing_stale_or_unselected_is_unknown(
    tmp_path: Path,
) -> None:
    rig = _grid_rig(tmp_path, ids=("m", "s"))
    rig.roles = {"m": "master", "s": "slave"}
    assert rig.value("plant_grid_voltage_a") is None  # nothing delivered yet
    _grid_feed(rig, "m", 0, volts=0.0, hz=0.0)  # the grid is absent: a valid reading
    assert rig.value("plant_grid_voltage_a") == 0.0
    assert rig.value("plant_grid_frequency") == 0.0
    rig.keys["m"].discard("grid_frequency")
    assert rig.value("plant_grid_frequency") is None
    rig.keys["m"].add("grid_frequency")
    rig.totals.set_online("m", False)
    assert rig.value("plant_grid_voltage_a") is None
    rig.totals.set_online("m", True)
    rig.now += 10_000
    assert rig.value("plant_grid_voltage_a") is None


def test_a_stale_slave_does_not_invalidate_the_reference_values(tmp_path: Path) -> None:
    rig = _grid_rig(tmp_path, ids=("m", "s"))
    rig.roles = {"m": "master", "s": "slave"}
    _grid_feed(rig, "m", 10)
    assert rig.value("plant_grid_voltage_a") == 230.0
    assert rig.value("plant_grid_terminal_power") is None  # the sum needs the slave


def test_reference_member_with_an_unused_grid_port_is_unavailable(tmp_path: Path) -> None:
    rig = _grid_rig(tmp_path, ids=("m", "s"))
    rig.roles = {"m": "master", "s": "slave"}
    rig.grid = {"s"}
    _grid_feed(rig, "m", 0)
    assert rig.value("plant_grid_voltage_a") is None


def test_grid_components_follow_the_predicate_and_carry_units() -> None:
    ns = Namespace("ab12cd34", "homeassistant", "deye")
    expire = {m.key: 30 for m in METRICS}
    grid_keys = {
        "plant_grid_terminal_power",
        "plant_grid_voltage_a",
        "plant_grid_voltage_b",
        "plant_grid_voltage_c",
        "plant_grid_frequency",
    }
    both = plant_components(ns, "g0", expire)
    only_gen = plant_components(ns, "g0", expire, gates=frozenset({"gen"}))
    assert grid_keys <= set(both) and not grid_keys & set(only_gen)
    for key in grid_keys:
        validate_component(key, both[key])
        assert both[key]["state_class"] == "measurement"
    assert both["plant_grid_terminal_power"]["device_class"] == "power"
    assert both["plant_grid_voltage_c"]["unit_of_measurement"] == "V"
    assert both["plant_grid_frequency"]["device_class"] == "frequency"
    assert both["plant_grid_frequency"]["unit_of_measurement"] == "Hz"


def test_the_32_bit_grid_power_is_decoded_high_word_first_then_signed() -> None:
    from deye_monitor.profiles.decode import decode

    entry = PROFILE.entry("grid_internal_power_total")
    assert (entry.low, entry.high) == (607, 703)
    assert decode(entry, {607: 0xFFFF, 703: 0x0000}) == 65535  # low-word rollover, positive
    assert decode(entry, {607: 0xFC18, 703: 0xFFFF}) == -1000  # negative, high word 0xFFFF
    assert decode(entry, {607: 0x0000, 703: 0x0001}) == 65536
