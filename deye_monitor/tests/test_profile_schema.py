from __future__ import annotations

import copy
import hashlib
from importlib import resources
from typing import Any

import pytest
from pydantic import ValidationError

from deye_monitor.profiles.features import Feature, Features
from deye_monitor.profiles.hardware import capability_conflicts, load_hardware
from deye_monitor.profiles.schema import (
    ENERGY_DAILY_ADDRS,
    ENERGY_TOTAL_PAIRS,
    Entry,
    Predicate,
    Profile,
    ProfileError,
    load_profile,
)

NAME = "sg04lp3_lv_v105_8"
SHA_1151 = "96338f74efadeb15dcdf8a2bf74e6b1e44bc7e9bb634a0de5747240e51261444"
SHA_1175 = "0cee626529b3bdb850af3c7891066924b4505bb066b072f82f9bbd05c811eb8b"


def entry_dict(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": "x",
        "name": {"de": "x", "en": "x"},
        "addr": 1,
        "type": "u16",
        "scale": 1,
        "category": "power",
        "point": "diagnostic",
        "group": "status",
        "requires": {},
        "supported_on": ["sg04lp3_eu"],
        "support_status": "supported",
        "release": "confirmed",
        "source": "s",
        "confidence": "c",
        "default_enabled": False,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def bad_entry(**over: Any) -> str:
    with pytest.raises(ValidationError) as exc:
        Entry.model_validate(entry_dict(**over))
    return str(exc.value)


def profile_dict(*entries: dict[str, Any]) -> dict[str, Any]:
    r65 = entry_dict(
        key="dsp", addr=65, type="raw", scale=None, group="identity", default_enabled=True
    )
    return {
        "profile": "t",
        "protocol_revision": "x",
        "hardware": "sg04lp3_eu",
        "energy_resolution_key": "dsp",
        "groups": {"identity": {"poll": "identity_s"}, "status": {"poll": "fast_s"}},
        "entries": [r65, *entries],
    }


def bad_profile(*entries: dict[str, Any], **over: Any) -> str:
    data = profile_dict(*entries)
    data.update(over)
    with pytest.raises(ValidationError) as exc:
        Profile.model_validate(data)
    return str(exc.value)


# -- the shipped profile -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def profile() -> Profile:
    return load_profile(NAME)


def test_profile_loads_and_revision_is_content_hash(profile: Profile) -> None:
    raw = (resources.files("deye_monitor.profiles") / f"{NAME}.yaml").read_bytes()
    assert profile.revision == hashlib.sha256(raw).hexdigest()
    assert profile.hardware == "sg04lp3_eu"
    assert profile is load_profile(NAME)  # cached, immutable


def test_loader_rejects_unknown_and_unsafe_names() -> None:
    for name in ("nope", "../x", "a/b", "", "UPPER", "x.yaml"):
        with pytest.raises(ProfileError):
            load_profile(name)
        with pytest.raises(ProfileError):
            load_hardware(name)


def test_every_entry_has_the_mandatory_metadata(profile: Profile) -> None:
    for e in profile.entries:
        assert e.name.de and e.name.en
        assert e.source and e.confidence
        assert e.supported_on == ("sg04lp3_eu",)


def test_unresolved_entries_are_raw_without_units(profile: Profile) -> None:
    for e in profile.entries:
        if e.release == "unresolved":
            assert (e.unit, e.device_class, e.state_class, e.scale, e.scale_source) == (None,) * 5
            assert not e.default_enabled, e.key
        if e.support_status == "placeholder":
            assert not e.default_enabled
            assert {ev.mcu2 for ev in e.producer_evidence} == {"1151", "1175"}, e.key
            assert {ev.image_sha256 for ev in e.producer_evidence} == {SHA_1151, SHA_1175}


def test_defaults_are_only_physical_supported_entries(profile: Profile) -> None:
    defaults = [e for e in profile.entries if e.default_enabled]
    assert defaults
    assert all(e.is_physical and not e.internal for e in defaults)


def test_pv_hardware_decisions(profile: Profile) -> None:
    for key in (
        "pv1_power",
        "pv1_voltage",
        "pv1_current",
        "pv2_power",
        "pv2_voltage",
        "pv2_current",
    ):
        e = profile.entry(key)
        assert e.support_status == "supported"
        assert e.is_physical
        assert {ev.effect for ev in e.producer_evidence} == {"store"}
    assert [
        profile.entry(k).addr
        for k in (
            "pv1_power",
            "pv2_power",
            "pv1_voltage",
            "pv1_current",
            "pv2_voltage",
            "pv2_current",
        )
    ] == [672, 673, 676, 677, 678, 679]
    placeholders = {
        674: "pv3_power",
        675: "pv4_power",
        680: "pv3_voltage",
        681: "pv3_current",
        682: "pv4_voltage",
        683: "pv4_current",
        530: "pv1_energy_today_r530",
        531: "pv2_energy_today_r531",
        532: "pv3_energy_today_r532",
        533: "pv4_energy_today_r533",
        718: "pv_total_power_r718",
        719: "pv5_voltage_r719",
        720: "pv6_voltage_r720",
        721: "pv7_voltage_r721",
    }
    for addr, key in placeholders.items():
        e = profile.entry(key)
        assert (e.addr, e.support_status, e.default_enabled, e.selectable) == (
            addr,
            "placeholder",
            False,
            False,
        )
        assert all(ev.effect == "zero_fill" for ev in e.producer_evidence)
    for addr in range(722, 749):
        e = profile.entry(f"pv_ext_r{addr}")
        assert (e.support_status, e.release, e.default_enabled) == (
            "unverified",
            "unresolved",
            False,
        )
    assert not any(e.key.startswith(("pv3_", "pv4_")) and e.is_physical for e in profile.entries)


def test_battery2_is_not_offered(profile: Profile) -> None:
    expected = {
        589: "placeholder",
        593: "placeholder",
        595: "placeholder",
        596: "placeholder",
        594: "unsupported_for_profile",
        602: "unsupported_for_profile",
    }
    found = {e.addr: e for e in profile.entries if e.key.startswith("battery2_")}
    assert {a: e.support_status for a, e in found.items()} == expected
    assert not any(e.default_enabled or e.selectable for e in found.values())


def test_aliases_and_version_bound_fields(profile: Profile) -> None:
    for key in (
        "grid_apparent_internal",
        "grid_apparent_external_r620",
        "inverter_apparent_r637",
        "load_apparent_r654",
        "load_current_invalid_a",
        "load_current_invalid_b",
        "load_current_invalid_c",
    ):
        e = profile.entry(key)
        assert e.support_status == "placeholder"
        assert not e.selectable
    r709 = profile.entry("ev_charge_power_r709")
    assert (r709.support_status, r709.release, r709.default_enabled) == (
        "unverified",
        "unresolved",
        False,
    )
    effects = {ev.mcu2: ev.effect for ev in r709.producer_evidence}
    assert effects == {"1151": "zero_fill", "1175": "copy"}
    assert profile.entry("grid_apparent_external_r620").addr == 620
    assert not [e for e in profile.entries if {620, 709} <= set(e.addresses)]


def test_conflicting_fields_are_raw_diagnostics_only(profile: Profile) -> None:
    r590 = profile.entry("battery_power_raw")
    assert (r590.addr, r590.release, r590.unit, r590.scale, r590.default_enabled) == (
        590,
        "unresolved",
        None,
        None,
        False,
    )
    for n in range(1, 7):
        soc = profile.entry(f"tou_slot{n}_soc_raw")
        assert (soc.addr, soc.release, soc.unit, soc.default_enabled) == (
            165 + n,
            "unresolved",
            None,
            False,
        )


def test_energy_whitelist_is_exactly_the_typed_list(profile: Profile) -> None:
    scaled = [e for e in profile.entries if e.scale_source]
    assert len(scaled) == len(ENERGY_DAILY_ADDRS) + len(ENERGY_TOTAL_PAIRS) == 12
    daily = {e.addr for e in scaled if e.type == "u16"}
    pairs = {(e.low, e.high) for e in scaled if e.type == "u32"}
    assert daily == ENERGY_DAILY_ADDRS
    assert pairs == ENERGY_TOTAL_PAIRS
    for e in scaled:
        assert (e.unit, e.device_class, e.state_class) == ("kWh", "energy", "total_increasing")
    forbidden = {502, 503, 506, 507, 508, 509, 510, 511, 512, 513, 536, 537, 538, 539, 501}
    assert not [e.key for e in scaled if forbidden & set(e.addresses)]


def test_load_and_grid_power_pairs_and_roles(profile: Profile) -> None:
    for i, key in enumerate(("a", "b", "c", "total")):
        e = profile.entry(f"load_power_{key}")
        assert (e.type, e.low, e.high) == ("s32", 650 + i, 656 + i)
    pairs = {
        "grid_internal_power_a": (604, 700),
        "grid_internal_power_total": (607, 703),
        "grid_external_power_a": (616, 705),
        "grid_external_power_total": (619, 708),
        "grid_selected_power_a": (622, 687),
        "grid_selected_power_total": (625, 690),
        "inverter_power_a": (633, 691),
        "inverter_power_total": (636, 694),
        "ups_power_a": (640, 696),
        "ups_power_total": (643, 699),
    }
    for key, (low, high) in pairs.items():
        e = profile.entry(key)
        assert (e.low, e.high) == (low, high)
    assert profile.entry("ups_power_a").type == "u32"
    assert profile.entry("gen_power_raw_a").release == "unresolved"
    assert not profile.entry("gen_power_raw_total").default_enabled


def test_identity_and_role_entries(profile: Profile) -> None:
    assert profile.entry("serial_number").addrs == (3, 4, 5, 6, 7)
    assert profile.entry("id_serial").internal and profile.entry("id_serial").mandatory
    assert profile.entry("rated_power").addresses == (20, 21)
    assert (profile.entry("mppt_count").mask, profile.entry("phase_count").mask) == (0xFF00, 0x00FF)
    assert profile.entry("battery_input_code").addr == 24
    assert profile.entry("dsp_protocol_version").addr == 65
    assert profile.energy_resolution_key == "dsp_protocol_version"
    roles = {
        k: profile.entry(k)
        for k in ("parallel_enable", "parallel_master", "parallel_phase", "parallel_sn")
    }
    assert {e.addr for e in roles.values()} == {336}
    assert [e.mask for e in roles.values()] == [0x0001, 0x0002, 0x0300, 0xFC00]
    assert all(e.mandatory for e in roles.values())
    assert profile.entry("inverter_generation").release == "unresolved"  # R28 never interpreted


def test_mandatory_set_is_identity_and_health(profile: Profile) -> None:
    mandatory = {e.key for e in profile.entries if e.mandatory}
    assert {
        "id_serial",
        "operating_state",
        "power_switch",
        "fault_word_1",
        "fault_word_4",
        "parallel_sn",
    } <= mandatory
    assert all(profile.entry(k).group in ("identity", "status") for k in mandatory)
    assert "dsp_protocol_version" not in mandatory  # only read when energy is selected (SPEC 9.3)


def test_fault_words_and_known_bits(profile: Profile) -> None:
    assert [profile.entry(f"fault_word_{n}").addr for n in (1, 2, 3, 4)] == [555, 556, 557, 558]
    for key, addr, mask in (
        ("fault_f13", 555, 1 << 12),
        ("fault_f29", 556, 1 << 12),
        ("fault_f41", 557, 1 << 8),
    ):
        e = profile.entry(key)
        assert (e.addr, e.mask, e.platform) == (addr, mask, "binary_sensor")


# -- hardware profile ----------------------------------------------------------------------------


def test_hardware_profile_values() -> None:
    hw = load_hardware("sg04lp3_eu")
    assert (hw.mppt_channels, hw.battery_inputs, hw.gen_port) == (2, 1, True)
    strings = {kw: (s.mppt1, s.mppt2) for kw, s in hw.strings_by_rated_kw.items()}
    assert strings == {5: (1, 1), 6: (1, 1), 8: (1, 1), 10: (2, 1), 12: (2, 1)}
    assert hw.expected_device_type == 0x0500
    assert (hw.expected_mppt_count, hw.expected_phase_count) == (2, 3)
    # three string sockets on a 10 kW unit are still two MPPT measurement channels
    assert hw.mppt_channels == 2 and sum(strings[10]) == 3


def test_capability_conflicts() -> None:
    hw = load_hardware("sg04lp3_eu")
    ok = {0: 0x0500, 22: 0x0203, 24: 1, 28: 4}
    assert capability_conflicts(hw, ok) == []
    assert capability_conflicts(hw, {}) == []  # missing words are not conflicts

    def codes(words: dict[int, int], **kw: Any) -> list[str]:
        return [c.code for c in capability_conflicts(hw, words, **kw)]

    assert codes({0: 0x0600}) == ["device_type"]
    assert codes({22: 0x0503}) == ["mppt_count"]  # the document example is not SG04
    assert codes({22: 0x0204}) == ["phase_count"]
    assert codes({24: 2}) == ["battery_inputs"]
    assert codes({24: 9}) == []  # undefined code: unknown, no conflict
    ten_kw = {20: 100000 & 0xFFFF, 21: 100000 >> 16}
    assert codes(ten_kw, rated_kw=10) == []
    assert codes(ten_kw, rated_kw=8) == ["rated_power"]
    assert codes(ten_kw, rated_kw=7) == ["model"]


# -- validator negatives -------------------------------------------------------------------------


def test_entry_layout_negatives() -> None:
    assert "exactly one" in bad_entry(addr=None)
    assert "exactly one" in bad_entry(low=1, high=2)
    assert "both low and high" in bad_entry(addr=None, type="u32", low=1)
    assert "needs explicit low and high" in bad_entry(type="u32")
    assert "must differ" in bad_entry(addr=None, type="u32", low=5, high=5)
    assert "ascii needs addrs" in bad_entry(type="ascii", scale=None)
    assert "R620/R709" in bad_entry(addr=None, type="u32", low=620, high=709)
    assert "R620/R709" in bad_entry(addr=None, type="u32", low=709, high=620)
    assert "mask requires" in bad_entry(addr=None, type="u32", low=1, high=2, mask=1)
    assert "bits below shift" in bad_entry(mask=0x00FF, shift=4)
    assert "shift requires mask" in bad_entry(shift=2)
    assert "enum mapping" in bad_entry(type="enum", scale=None)
    assert "enum mapping" in bad_entry(enum={0: "a"})
    assert "key must match" in bad_entry(key="Bad Key")
    assert "extra" in bad_entry(bogus=1).lower()


def test_entry_scale_negatives() -> None:
    assert "exactly one of scale and scale_source" in bad_entry(scale=None)
    assert "exactly one of scale and scale_source" in bad_entry(
        scale_source="energy_resolution",
        addr=514,
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    )
    assert "carry no scale" in bad_entry(type="raw", scale=1)
    assert "raw value only" in bad_entry(release="unresolved", scale=1)
    assert "raw value only" in bad_entry(release="unresolved", scale=None, raw_offset=-1000)
    assert "scale must be > 0" in bad_entry(scale=0)
    assert "physical unit" in bad_entry(release="unresolved", scale=None, unit="W")
    assert "physical unit" in bad_entry(release="unresolved", scale=None, device_class="power")


def test_energy_scale_source_only_on_whitelist() -> None:
    common = {
        "scale": None,
        "scale_source": "energy_resolution",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
    }
    assert "whitelist" in bad_entry(addr=530, **common)  # PV1 daily: zero-filled, not whitelisted
    assert "whitelist" in bad_entry(
        addr=None, type="u32", low=516, high=518, **common
    )  # wrong partner
    assert "whitelist" in bad_entry(
        addr=None, type="u32", low=517, high=516, **common
    )  # roles swapped
    assert "whitelist" in bad_entry(addr=None, type="u32", low=512, high=513, **common)  # runtime
    assert "whitelist" in bad_entry(addr=514, type="s16", **common)
    assert "kWh" in bad_entry(addr=514, **{**common, "unit": "W"})
    assert "kWh" in bad_entry(addr=514, **{**common, "state_class": "measurement"})
    Entry.model_validate(entry_dict(addr=514, **common))
    Entry.model_validate(entry_dict(addr=None, type="u32", low=516, high=517, **common))


def test_entry_policy_negatives() -> None:
    assert "placeholder needs producer_evidence" in bad_entry(
        support_status="placeholder", release="unresolved", scale=None
    )
    ev = {"mcu2": "1151", "image_sha256": SHA_1151, "store_byte": 1, "effect": "zero_fill"}
    assert "must not be default_enabled" in bad_entry(
        support_status="placeholder",
        release="unresolved",
        scale=None,
        producer_evidence=[ev],
        default_enabled=True,
    )
    assert "unverified entries must be release unresolved" in bad_entry(support_status="unverified")
    assert "default_enabled needs" in bad_entry(
        release="unresolved", scale=None, default_enabled=True
    )
    assert "default_enabled needs" in bad_entry(
        support_status="unsupported_for_profile",
        release="unresolved",
        scale=None,
        default_enabled=True,
    )
    assert "internal entries" in bad_entry(internal=True, default_enabled=True)
    assert "mandatory entries must be supported" in bad_entry(
        mandatory=True, support_status="unverified", release="unresolved", scale=None
    )
    assert "single-bit mask" in bad_entry(platform="binary_sensor")
    assert "single-bit mask" in bad_entry(
        platform="binary_sensor", type="bitfield", scale=None, mask=0x0003
    )
    for sha in ("a" * 63, "A" * 64, "g" * 64, ""):
        assert "64 lowercase hex" in bad_entry(
            support_status="placeholder",
            release="unresolved",
            scale=None,
            producer_evidence=[{**ev, "image_sha256": sha}],
        )
    assert "String should match" in bad_entry(
        support_status="placeholder",
        release="unresolved",
        scale=None,
        producer_evidence=[{**ev, "mcu2": "11"}],
    )


def test_profile_level_negatives() -> None:
    assert "duplicate key" in bad_profile(entry_dict(key="a", addr=2), entry_dict(key="a", addr=3))
    assert "claimed twice" in bad_profile(entry_dict(key="a", addr=2), entry_dict(key="b", addr=2))
    assert "claimed twice" in bad_profile(
        entry_dict(key="a", addr=None, type="u32", low=2, high=3), entry_dict(key="b", addr=3)
    )
    assert "claimed twice" in bad_profile(
        entry_dict(key="a", addr=2, type="bitfield", scale=None),
        entry_dict(key="b", addr=2, type="bitfield", scale=None),
    )
    assert "claimed twice" in bad_profile(
        entry_dict(key="a", addr=2), entry_dict(key="b", addr=2, type="u16", mask=1)
    )  # numeric word + bit view
    assert "overlapping masks" in bad_profile(
        entry_dict(key="a", addr=2, mask=0x00FF), entry_dict(key="b", addr=2, mask=0x0F00 | 0x0010)
    )
    assert "not configured" in bad_profile(entry_dict(key="a", addr=2, group="pv"))
    assert "supported_on must list" in bad_profile(
        entry_dict(key="a", addr=2, supported_on=["other"])
    )
    assert "R65" in bad_profile(entry_dict(key="a", addr=2), energy_resolution_key="a")
    assert "R65" in bad_profile(entry_dict(key="a", addr=2), energy_resolution_key="missing")


def test_profile_allows_documented_overlaps() -> None:
    raw = entry_dict(key="word", addr=2, type="bitfield", scale=None)
    bits = [
        entry_dict(
            key=f"bit{i}",
            addr=2,
            type="bitfield",
            scale=None,
            mask=1 << i,
            platform="binary_sensor",
        )
        for i in (1, 12)
    ]
    twin = entry_dict(key="twin", addr=2, type="raw", scale=None, internal=True, mandatory=True)
    Profile.model_validate(profile_dict(raw, *bits, twin))


def test_predicate_negatives_and_evaluation() -> None:
    def bad(**kw: Any) -> None:
        with pytest.raises(ValidationError):
            Predicate.model_validate(kw)

    bad(feature="nope", equals=True)
    bad(feature="battery", equals="yes")
    bad(feature="battery", in_=["x"])
    bad(feature="gen_port_mode", equals="ac")
    bad(feature="battery")
    bad(feature="battery", equals=True, **{"in": ["a"]})
    bad(equals=True)
    bad(
        all_of=[],
    )
    bad(any_of=[])
    bad(all_of=[{}], any_of=[{}])
    bad(all_of=[{}], feature="battery", equals=True)
    bad(bogus=1)

    f = Features(battery=True, bms=False, gen_port_mode="ac_coupled")
    assert Predicate.model_validate({}).evaluate(f)
    assert Predicate.model_validate({}).is_always
    assert Predicate.model_validate({"feature": "battery", "equals": True}).evaluate(f)
    assert not Predicate.model_validate({"feature": "bms", "equals": True}).evaluate(f)
    gen = Predicate.model_validate({"feature": "gen_port_mode", "in": ["generator", "ac_coupled"]})
    assert gen.evaluate(f) and not gen.evaluate(Features())
    nested = Predicate.model_validate(
        {
            "all_of": [
                {"feature": "battery", "equals": True},
                {
                    "any_of": [
                        {"feature": "bms", "equals": True},
                        {"feature": "parallel", "equals": False},
                    ]
                },
            ]
        }
    )
    assert nested.evaluate(f)
    assert not nested.evaluate(Features(parallel=True, bms=False))
    assert nested.features_used() == {Feature.BATTERY, Feature.BMS, Feature.PARALLEL}


def test_model_dicts_are_not_mutated_by_validation() -> None:
    data = profile_dict(entry_dict(key="a", addr=2))
    snapshot = copy.deepcopy(data)
    Profile.model_validate(data)
    assert data == snapshot


def test_capability_words_0xffff_are_not_conflicts_but_other_values_are() -> None:
    from deye_monitor.profiles.hardware import unreported_capabilities

    hw = load_hardware("sg04lp3_eu")
    assert capability_conflicts(hw, {0: 0xFFFF, 22: 0xFFFF, 24: 0xFFFF}) == []
    assert unreported_capabilities({0: 0xFFFF, 22: 0xFFFF, 24: 0xFFFF, 15: 1}) == [
        "device_type",
        "mppt_phases",
        "battery_input_code",
    ]
    assert unreported_capabilities({0: 0x0500, 22: 0x0203, 24: 1}) == []
    # field by field: one unreported word does not hide a real deviation in another
    assert {c.code for c in capability_conflicts(hw, {0: 0xFFFF, 22: 0x0503})} == {"mppt_count"}
    assert {c.code for c in capability_conflicts(hw, {0: 0x0600, 22: 0xFFFF})} == {"device_type"}


def test_r24_codes_other_than_0_1_2_are_unknown_but_no_conflict() -> None:
    from deye_monitor.profiles.hardware import unknown_capability_codes

    hw = load_hardware("sg04lp3_eu")
    for code in (3, 4, 5, 7, 0x1234):  # V105.8 inverter:24: only 0/1/2 defined
        assert capability_conflicts(hw, {24: code}) == []
        assert unknown_capability_codes(hw, {24: code}) == {"battery_input_code": code}
    for code in (0, 1, 2, 0xFFFF):
        assert unknown_capability_codes(hw, {24: code}) == {}
    assert capability_conflicts(hw, {24: 1}) == [] and capability_conflicts(hw, {24: 0}) == []
    assert [c.code for c in capability_conflicts(hw, {24: 2})] == ["battery_inputs"]


def test_r0_0x0005_and_0x0500_are_both_the_three_phase_lv_family() -> None:
    hw = load_hardware("sg04lp3_eu")
    for word in (0x0005, 0x0500):
        assert capability_conflicts(hw, {0: word}) == []
    for word in (0x0006, 0x0600, 0x0050):
        assert [c.code for c in capability_conflicts(hw, {0: word})] == ["device_type"]
