from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import (
    ConfigError,
    EnergyResolution,
    PlantConfig,
    Transport,
    load_config,
    parse_config,
    resolve_instance_id,
)


def gw(i: int, **kw: Any) -> dict[str, Any]:
    return {
        "id": f"gw{i}",
        "transport": "modbus_tcp",
        "host": f"192.0.2.{50 + i}",
        **kw,
    }


def logger(i: int, **kw: Any) -> dict[str, Any]:
    return {
        "id": f"logger{i}",
        "transport": "solarman_v5_tcp",
        "host": f"192.0.2.{70 + i}",
        "logger_serial": 1700000000 + i,
        **kw,
    }


def inv(i: int, endpoint: str | None = None, **kw: Any) -> dict[str, Any]:
    return {
        "id": f"deye{i}",
        "name": f"Deye {i}",
        "endpoint": endpoint or f"gw{i}",
        "unit_id": 1,
        **kw,
    }


def minimal() -> dict[str, Any]:
    return {"endpoints": [gw(1)], "inverters": [inv(1)]}


def four_inverter_plant() -> dict[str, Any]:
    return {
        "endpoints": [gw(i) for i in range(1, 5)] + [logger(i) for i in range(1, 5)],
        "inverters": [inv(i, fallback_endpoint=f"logger{i}") for i in range(1, 5)],
        "mqtt": {"host": "core-mosquitto", "username": "u", "password": "s3cret-pw"},
    }


def bad(data: dict[str, Any]) -> str:
    with pytest.raises(ConfigError) as exc:
        parse_config(data)
    return str(exc.value)


def test_minimal_defaults() -> None:
    cfg = parse_config(minimal())
    e = cfg.endpoints[0]
    assert (e.port, e.timeout_s, e.max_read_registers, e.request_gap_ms, e.queue_size) == (
        502,
        5.0,
        60,
        50.0,
        32,
    )
    i = cfg.inverters[0]
    assert i.profile == "sg04lp3_lv_v105_8"
    assert i.energy_resolution is EnergyResolution.AUTO
    assert (i.poll_fast_s, i.poll_energy_s, i.poll_settings_s, i.poll_identity_s) == (
        10,
        60,
        300,
        3600,
    )
    assert i.allow_writes is False
    assert cfg.mqtt.host is None
    assert (cfg.mqtt.port, cfg.mqtt.discovery_prefix, cfg.mqtt.topic_prefix) == (
        1883,
        "homeassistant",
        "deye_monitor",
    )
    assert cfg.log_level == "info"


def test_four_inverters_gateway_with_v5_fallback() -> None:
    cfg = parse_config(four_inverter_plant())
    assert len(cfg.inverters) == 4
    assert {i.unit_id for i in cfg.inverters} == {1}
    assert all(i.fallback_unit_id == 1 for i in cfg.inverters)
    assert cfg.endpoint("logger1").port == 8899
    assert cfg.endpoint("logger1").transport is Transport.SOLARMAN_V5_TCP


@pytest.mark.parametrize("n", [1, 7, 20])
def test_n_inverters(n: int) -> None:
    data = {
        "endpoints": [gw(i) for i in range(1, n + 1)],
        "inverters": [inv(i) for i in range(1, n + 1)],
    }
    assert len(parse_config(data).inverters) == n


def test_options_may_be_empty_the_plant_is_set_up_in_the_web_interface() -> None:
    assert parse_config({"endpoints": [gw(1)], "inverters": []}).inverters == ()
    assert parse_config({}).inverters == ()


def test_shared_bus_different_units_ok_same_unit_error() -> None:
    data = {
        "endpoints": [gw(1)],
        "inverters": [inv(1, "gw1", unit_id=1), inv(2, "gw1", unit_id=2)],
    }
    assert len(parse_config(data).inverters) == 2
    data["inverters"][1]["unit_id"] = 1
    msg = bad(data)
    assert "deye1" in msg
    assert "deye2" in msg
    assert "unit 1" in msg


def test_separate_gateways_same_unit_ok() -> None:
    data = {"endpoints": [gw(1), gw(2)], "inverters": [inv(1), inv(2)]}
    assert len(parse_config(data).inverters) == 2


def test_explicit_bus_id_groups_endpoints() -> None:
    data = {
        "endpoints": [gw(1, bus_id="rs485a"), logger(1, bus_id="rs485a")],
        "inverters": [inv(1, "gw1"), inv(2, "logger1")],
    }
    assert "unit 1" in bad(data)
    data["inverters"][1]["unit_id"] = 2
    assert len(parse_config(data).inverters) == 2


def test_primary_and_fallback_on_same_bus_ok() -> None:
    data = {
        "endpoints": [gw(1, bus_id="b"), logger(1, bus_id="b")],
        "inverters": [inv(1, "gw1", fallback_endpoint="logger1")],
    }
    cfg = parse_config(data)
    assert cfg.endpoint("gw1").bus == cfg.endpoint("logger1").bus == "b"


def test_fallback_unit_override() -> None:
    data = {
        "endpoints": [gw(1), logger(1)],
        "inverters": [inv(1, fallback_endpoint="logger1", fallback_unit_id=5)],
    }
    assert parse_config(data).inverters[0].fallback_unit_id == 5
    data["inverters"][0].pop("fallback_endpoint")
    assert "fallback_unit_id" in bad(data)


def test_fallback_unit_defaults_to_one_for_a_v5_fallback_and_keeps_explicit_values() -> None:
    data = {
        "endpoints": [gw(1), logger(1)],
        "inverters": [inv(1, "gw1", unit_id=3, fallback_endpoint="logger1")],
    }
    assert parse_config(data).inverters[0].fallback_unit_id == 1
    assert PlantConfig.model_validate(data).inverters[0].fallback_unit_id == 1
    data["inverters"][0]["fallback_unit_id"] = 3
    assert parse_config(data).inverters[0].fallback_unit_id == 3
    # a gateway fallback keeps following the primary's unit
    swapped = {
        "endpoints": [gw(1), logger(1)],
        "inverters": [inv(1, "logger1", unit_id=1, fallback_endpoint="gw1")],
    }
    assert parse_config(swapped).inverters[0].fallback_unit_id == 1
    swapped["inverters"][0]["unit_id"] = 2
    assert parse_config(swapped).inverters[0].fallback_unit_id == 2


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("gateway.example", "Gateway.Example."),
        ("192.168.1.1", "192.168.1.1"),
        ("::1", "[0:0:0:0:0:0:0:1]"),
        ("2001:db8::1", "2001:0DB8:0:0:0:0:0:1"),
        ("127.0.0.1", "::ffff:127.0.0.1"),  # IPv4-mapped IPv6 is the same target
        ("192.0.2.7", "[::FFFF:c000:207]"),
    ],
)
def test_endpoint_aliases_rejected(a: str, b: str) -> None:
    data = {
        "endpoints": [gw(1, host=a), gw(2, host=b)],
        "inverters": [inv(1), inv(2)],
    }
    msg = bad(data)
    assert "same target" in msg


def test_same_host_different_port_is_distinct() -> None:
    data = {
        "endpoints": [gw(1, host="x"), gw(2, host="x", port=503)],
        "inverters": [inv(1), inv(2)],
    }
    assert len(parse_config(data).endpoints) == 2


def test_serial_symlink_alias(tmp_path: Path) -> None:
    dev = tmp_path / "ttyUSB0"
    dev.touch()
    link = tmp_path / "by-id-x"
    os.symlink(dev, link)

    def ser(i: int, p: Path) -> dict[str, Any]:
        return {"id": f"s{i}", "transport": "serial_rtu", "serial_device": str(p)}

    data = {"endpoints": [ser(1, dev), ser(2, link)], "inverters": [inv(1, "s1"), inv(2, "s2")]}
    assert "same target" in bad(data)
    assert (
        parse_config({"endpoints": [ser(1, dev)], "inverters": [inv(1, "s1")]})
        .endpoints[0]
        .baudrate
        == 9600
    )


def test_duplicate_ids_and_references() -> None:
    assert "duplicate endpoint id 'gw1'" in bad(
        {"endpoints": [gw(1), gw(1, host="other")], "inverters": [inv(1)]}
    )
    assert "duplicate inverter id" in bad(
        {"endpoints": [gw(1), gw(2)], "inverters": [inv(1), inv(1, "gw2")]}
    )
    assert "endpoint 'nope' is not defined" in bad(
        {"endpoints": [gw(1)], "inverters": [inv(1, "nope")]}
    )
    assert "fallback_endpoint 'nope'" in bad(
        {"endpoints": [gw(1)], "inverters": [inv(1, fallback_endpoint="nope")]}
    )
    assert "must differ" in bad(
        {"endpoints": [gw(1)], "inverters": [inv(1, fallback_endpoint="gw1")]}
    )


@pytest.mark.parametrize("bad_id", ["Deye1", "a-b", "", "x" * 33, "ä", "a b"])
def test_invalid_ids(bad_id: str) -> None:
    data = minimal()
    data["inverters"][0]["id"] = bad_id
    assert "inverters.0.id" in bad(data)


def test_transport_specific_fields() -> None:
    v5_missing = {"id": "l", "transport": "solarman_v5_tcp", "host": "h"}
    assert "logger_serial is required" in bad(
        {"endpoints": [v5_missing], "inverters": [inv(1, "l")]}
    )
    tcp_serial = gw(1, serial_device="/dev/ttyUSB0")
    assert "only allowed for serial_rtu" in bad({"endpoints": [tcp_serial], "inverters": [inv(1)]})
    no_path = {"id": "s", "transport": "serial_rtu"}
    assert "serial_device is required" in bad({"endpoints": [no_path], "inverters": [inv(1, "s")]})
    ser_host = {"id": "s", "transport": "serial_rtu", "serial_device": "/dev/x", "host": "h"}
    assert "host is not allowed" in bad({"endpoints": [ser_host], "inverters": [inv(1, "s")]})
    no_host = {"id": "g", "transport": "modbus_tcp"}
    assert "host is required" in bad({"endpoints": [no_host], "inverters": [inv(1, "g")]})
    tcp_ls = gw(1, logger_serial=5)
    assert "logger_serial is only allowed" in bad({"endpoints": [tcp_ls], "inverters": [inv(1)]})
    rel = {"id": "s", "transport": "serial_rtu", "serial_device": "ttyUSB0"}
    assert "absolute device path" in bad({"endpoints": [rel], "inverters": [inv(1, "s")]})
    assert "transport" in bad({"endpoints": [gw(1, transport="telnet")], "inverters": [inv(1)]})


@pytest.mark.parametrize("host", ["", " ", "a b", "http://h", "h/x", "u@h"])
def test_bad_hosts(host: str) -> None:
    assert "host" in bad({"endpoints": [gw(1, host=host)], "inverters": [inv(1)]})


NAN = float("nan")
INF = float("inf")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_s", 0),
        ("timeout_s", -1),
        ("timeout_s", NAN),
        ("timeout_s", INF),
        ("timeout_s", "5"),
        ("timeout_s", True),
        ("rtu_late_window_s", 0),
        ("rtu_late_window_s", -1),
        ("rtu_late_window_s", NAN),
        ("rtu_late_window_s", INF),
        ("rtu_late_window_s", "1"),
        ("max_read_registers", 0),
        ("max_read_registers", 1),
        ("max_read_registers", 5),
        ("max_read_registers", 101),
        ("max_read_registers", 125),
        ("max_read_registers", 60.5),
        ("request_gap_ms", -1),
        ("request_gap_ms", NAN),
        ("request_gap_ms", INF),
        ("queue_size", 0),
        ("queue_size", -3),
        ("queue_size", 10**9),
        ("port", 0),
        ("port", 65536),
        ("port", -1),
        ("port", "502"),
    ],
)
def test_endpoint_numeric_bounds(field: str, value: Any) -> None:
    assert f"endpoints.0.{field}" in bad(
        {"endpoints": [gw(1, **{field: value})], "inverters": [inv(1)]}
    )


@pytest.mark.parametrize("value", [0, 0.5, -1, NAN, INF, "10", None, True])
def test_poll_intervals(value: Any) -> None:
    for name in ("poll_fast_s", "poll_energy_s", "poll_settings_s", "poll_identity_s"):
        data = minimal()
        data["inverters"][0][name] = value
        assert f"inverters.0.{name}" in bad(data)


@pytest.mark.parametrize("unit", [0, -1, 248, 1.5, "1", NAN])
def test_unit_id_bounds(unit: Any) -> None:
    data = minimal()
    data["inverters"][0]["unit_id"] = unit
    assert "inverters.0.unit_id" in bad(data)
    data = {"endpoints": [gw(1), logger(1)], "inverters": [inv(1, fallback_endpoint="logger1")]}
    data["inverters"][0]["fallback_unit_id"] = unit
    assert "fallback_unit_id" in bad(data)


@pytest.mark.parametrize("serial", [0, 2**32, -5, 1.5, True, "12"])
def test_logger_serial_bounds(serial: Any) -> None:
    data = {"endpoints": [logger(1, logger_serial=serial)], "inverters": [inv(1, "logger1")]}
    assert "logger_serial" in bad(data)


def test_logger_serial_edges() -> None:
    for s in (1, 2**32 - 1):
        data = {"endpoints": [logger(1, logger_serial=s)], "inverters": [inv(1, "logger1")]}
        assert parse_config(data).endpoints[0].logger_serial == s


def test_unknown_options_rejected_everywhere() -> None:
    def top(d: dict[str, Any]) -> None:
        d["bogus"] = 1

    def endpoint(d: dict[str, Any]) -> None:
        d["endpoints"][0]["bogus"] = 1

    def inverter(d: dict[str, Any]) -> None:
        d["inverters"][0]["bogus"] = 1

    def poll(d: dict[str, Any]) -> None:
        d["inverters"][0]["poll"] = {"fast_s": 5}  # retired nested form

    def mqtt(d: dict[str, Any]) -> None:
        d["mqtt"] = {"bogus": 1}

    for mutate, where in [
        (top, "bogus"),
        (endpoint, "endpoints.0.bogus"),
        (inverter, "inverters.0.bogus"),
        (poll, "inverters.0.poll"),
        (mqtt, "mqtt.bogus"),
    ]:
        data = minimal()
        mutate(data)
        assert where in bad(data)


def test_expected_serial_and_energy_resolution() -> None:
    data = minimal()
    data["inverters"][0]["expected_serial"] = "2401234567"
    assert parse_config(data).inverters[0].expected_serial == "2401234567"
    for s in ("123", "24012345678", " 401234567", "240123456é", "240123456 "):
        data["inverters"][0]["expected_serial"] = s
        assert "expected_serial" in bad(data)
    data = minimal()
    for raw, member in [("auto", "auto"), (0.1, "0.1"), (1, "1"), ("0.1", "0.1"), ("1", "1")]:
        data["inverters"][0]["energy_resolution"] = raw
        res = parse_config(data).inverters[0].energy_resolution
        assert res.value == member
    for raw in (0.5, 2, "fast", True):
        data["inverters"][0]["energy_resolution"] = raw
        assert "energy_resolution" in bad(data)
    assert EnergyResolution.TENTH.kwh_step == 0.1
    assert EnergyResolution.AUTO.kwh_step is None


@pytest.mark.parametrize("prefix", ["", "a//b", "/a", "a/", "a/+/b", "a#", "a b", "+"])
def test_topic_prefix_validation(prefix: str) -> None:
    for field in ("topic_prefix", "discovery_prefix"):
        data = minimal()
        data["mqtt"] = {field: prefix}
        assert f"mqtt.{field}" in bad(data)


def test_topic_prefix_multi_segment_ok() -> None:
    data = minimal()
    data["mqtt"] = {"topic_prefix": "home/deye", "discovery_prefix": "ha"}
    assert parse_config(data).mqtt.topic_prefix == "home/deye"


def test_log_level_and_instance_id_validation() -> None:
    data = minimal()
    data["log_level"] = "verbose"
    assert "log_level" in bad(data)
    data = minimal()
    for iid in ("abc", "ABCD1234", "a-b-c-d", "x" * 17):
        data["instance_id"] = iid
        assert "instance_id" in bad(data)
    data["instance_id"] = "abcd1234"
    assert parse_config(data).instance_id == "abcd1234"


def test_config_is_frozen() -> None:
    cfg = parse_config(minimal())
    with pytest.raises(Exception, match="frozen"):
        cfg.log_level = "debug"


def test_password_never_in_repr_or_errors(tmp_path: Path) -> None:
    cfg = parse_config(four_inverter_plant())
    for text in (repr(cfg), str(cfg), repr(cfg.mqtt), cfg.mqtt.model_dump_json()):
        assert "s3cret-pw" not in text
    assert cfg.mqtt.password is not None
    assert cfg.mqtt.password.get_secret_value() == "s3cret-pw"

    data = four_inverter_plant()
    data["mqtt"]["password"] = 123456789
    data["mqtt"]["port"] = "s3cret-pw"
    data["inverters"][0]["unit_id"] = 0
    msg = bad(data)
    assert "123456789" not in msg
    assert "s3cret-pw" not in msg
    assert "mqtt.password" in msg

    path = tmp_path / "options.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "s3cret-pw" not in str(exc.value)
    assert "123456789" not in str(exc.value)


def test_errors_are_readable_with_field_paths() -> None:
    data = minimal()
    data["inverters"][0]["unit_id"] = 300
    data["endpoints"][0]["timeout_s"] = -1
    msg = bad(data)
    assert "inverters.0.unit_id:" in msg
    assert "endpoints.0.timeout_s:" in msg
    assert "pydantic" not in msg
    assert "http" not in msg


def test_load_config_paths_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(minimal()))
    assert len(load_config(path).inverters) == 1
    monkeypatch.setenv("DEYE_MONITOR_OPTIONS", str(path))
    assert len(load_config().inverters) == 1
    monkeypatch.setenv("DEYE_MONITOR_OPTIONS", str(tmp_path / "missing.json"))
    with pytest.raises(ConfigError, match="cannot read"):
        load_config()
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(path)
    path.write_text('{"inverters": [], "endpoints": [], "x": NaN}')
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text("[]")
    with pytest.raises(ConfigError):
        load_config(path)


def test_nan_from_json_rejected(tmp_path: Path) -> None:
    path = tmp_path / "o.json"
    raw = json.dumps(minimal()).replace('"unit_id": 1', '"unit_id": 1, "poll_fast_s": NaN')
    path.write_text(raw)
    with pytest.raises(ConfigError, match=r"poll_fast_s"):
        load_config(path)


def test_allow_writes_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    data = minimal()
    with caplog.at_level(logging.WARNING, logger="deye_monitor.config"):
        parse_config(data)
    assert not caplog.records
    data["inverters"][0]["allow_writes"] = True
    with caplog.at_level(logging.WARNING, logger="deye_monitor.config"):
        cfg = parse_config(data)
    assert cfg.inverters[0].allow_writes is True
    assert any("Schreibschicht nicht implementiert" in r.getMessage() for r in caplog.records)
    data["inverters"][0]["allow_writes"] = "yes"
    assert "allow_writes" in bad(data)


def test_instance_id_persists_across_loads(tmp_path: Path) -> None:
    cfg = parse_config(minimal())
    first = resolve_instance_id(cfg, tmp_path)
    assert len(first) == 8
    assert first.isalnum()
    assert first == first.lower()
    assert resolve_instance_id(cfg, tmp_path) == first
    assert (tmp_path / "instance_id").read_text().strip() == first
    assert [p.name for p in tmp_path.iterdir()] == ["instance_id"]  # no tmp files left


def test_instance_id_configured_wins_and_is_not_written(tmp_path: Path) -> None:
    data = minimal()
    data["instance_id"] = "myinst01"
    assert resolve_instance_id(parse_config(data), tmp_path) == "myinst01"
    assert not (tmp_path / "instance_id").exists()


def test_instance_id_regenerates_when_corrupt_and_creates_dir(tmp_path: Path) -> None:
    cfg = parse_config(minimal())
    target = tmp_path / "sub"
    (tmp_path / "sub").mkdir()
    (target / "instance_id").write_text("NOT VALID!!")
    new = resolve_instance_id(cfg, target)
    assert new != "NOT VALID!!"
    assert resolve_instance_id(cfg, target) == new
    fresh = tmp_path / "a" / "b"
    assert len(resolve_instance_id(cfg, fresh)) == 8


def test_instance_id_env_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEYE_MONITOR_DATA", str(tmp_path))
    iid = resolve_instance_id(parse_config(minimal()))
    assert (tmp_path / "instance_id").read_text().strip() == iid


def test_input_dict_not_mutated() -> None:
    data = four_inverter_plant()
    snapshot = copy.deepcopy(data)
    parse_config(data)
    assert data == snapshot


@pytest.mark.parametrize("baud", [0, 49, -9600, 9600.5, "9600", 5_000_000])
def test_serial_baudrate_bounds(baud: Any) -> None:
    ep = {"id": "s", "transport": "serial_rtu", "serial_device": "/dev/x", "baudrate": baud}
    assert "endpoints.0.baudrate" in bad({"endpoints": [ep], "inverters": [inv(1, "s")]})
    ok = {"id": "s", "transport": "serial_rtu", "serial_device": "/dev/x", "baudrate": 19200}
    assert (
        parse_config({"endpoints": [ok], "inverters": [inv(1, "s")]}).endpoints[0].baudrate == 19200
    )


@pytest.mark.parametrize(
    ("entered", "canonical"),
    [
        ("[::1]", "::1"),
        ("[2001:DB8:0:0:0:0:0:1]", "2001:db8::1"),
        ("::ffff:127.0.0.1", "127.0.0.1"),
        ("Gateway.Example.", "gateway.example"),
        ("  192.0.2.7 ", "192.0.2.7"),
    ],
)
def test_host_is_stored_in_canonical_connect_form(entered: str, canonical: str) -> None:
    cfg = parse_config({"endpoints": [gw(1, host=entered)], "inverters": [inv(1)]})
    e = cfg.endpoints[0]
    assert e.host == canonical
    assert e.host_display == entered  # original text kept for display only
    assert e.canonical_key.startswith("tcp:")
    assert canonical in e.canonical_key.replace("[", "").replace("]", "")


def test_mapped_ipv6_alias_with_equal_units_is_one_bus_not_two() -> None:
    data = {
        "endpoints": [gw(1, host="127.0.0.1"), gw(2, host="::ffff:127.0.0.1")],
        "inverters": [inv(1), inv(2)],
    }
    assert "same target" in bad(data)
    # a distinct port or a distinct host stays allowed, each device with its own unit 1
    ok = {
        "endpoints": [gw(1, host="127.0.0.1"), gw(2, host="::ffff:127.0.0.1", port=503)],
        "inverters": [inv(1), inv(2)],
    }
    assert len(parse_config(ok).endpoints) == 2


@pytest.mark.parametrize("host", ["[::1", "::1]", "[]", "a b", "http://h", "u@h", "h/x", ""])
def test_unusable_hosts_are_rejected(host: str) -> None:
    assert "endpoints.0.host" in bad({"endpoints": [gw(1, host=host)], "inverters": [inv(1)]})


def test_mqtt_host_is_normalised_too() -> None:
    cfg = parse_config({**minimal(), "mqtt": {"host": "[::1]"}})
    assert cfg.mqtt.host == "::1"


def test_reparsing_a_dumped_endpoint_is_stable() -> None:
    cfg = parse_config({"endpoints": [gw(1, host="[::1]")], "inverters": [inv(1)]})
    again = parse_config(cfg.model_dump(mode="json"))
    assert again.endpoints[0].host == "::1"
    assert again.endpoints[0].host_display == "[::1]"


def test_rtu_late_window_option() -> None:
    cfg = parse_config({"endpoints": [gw(1, rtu_late_window_s=3)], "inverters": [inv(1)]})
    assert cfg.endpoints[0].rtu_late_window_s == 3
    assert parse_config(minimal()).endpoints[0].rtu_late_window_s is None  # transport default


def test_read_block_above_80_is_accepted_but_limited_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = minimal()
    data["endpoints"][0]["max_read_registers"] = 100
    with caplog.at_level(logging.WARNING, logger="deye_monitor.config"):
        cfg = parse_config(data)
    ep = cfg.endpoints[0]
    assert (ep.max_read_registers, ep.read_block_limit) == (100, 80)
    assert [r.getMessage() for r in caplog.records if "max_read_registers" in r.getMessage()] == [
        "endpoint 'gw1': max_read_registers 100 is above 80 and is limited to 80"
    ]
    data["endpoints"][0]["max_read_registers"] = 80
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="deye_monitor.config"):
        cfg = parse_config(data)
    assert cfg.endpoints[0].read_block_limit == 80
    assert not [r for r in caplog.records if "max_read_registers" in r.getMessage()]
