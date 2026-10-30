"""Manifest, schema, translations and docs must agree with config.py and the package."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
import voluptuous as vol
import yaml
from pydantic import BaseModel

import deye_monitor
from deye_monitor.config import (
    AppConfig,
    ConfigError,
    EndpointConfig,
    InverterConfig,
    MqttConfig,
    parse_config,
    with_supervisor_mqtt,
)
from tests.supervisor_options import SupervisorOptions

APP = Path(__file__).parents[1]
ROOT = APP.parent
MANIFEST = cast(dict[str, Any], yaml.safe_load((APP / "config.yaml").read_text()))
SCHEMA = cast(dict[str, Any], MANIFEST["schema"])

_FORBIDDEN = (
    "host_network",
    "full_access",
    "privileged",
    "docker_api",
    "hassio_api",
    "homeassistant_api",
    "host_pid",
    "host_ipc",
    "host_dbus",
    "devices",
    "kernel_modules",
    "ports",
)


def test_manifest_basics() -> None:
    assert MANIFEST["slug"] == "deye_monitor"
    assert MANIFEST["version"] == deye_monitor.__version__ == "0.2.1"
    assert MANIFEST["arch"] == ["aarch64", "amd64"]
    assert MANIFEST["init"] is False
    assert MANIFEST["stage"] == "experimental"
    assert "startup" not in MANIFEST  # default "application" (linter: no default keys)
    assert MANIFEST.get("boot", "auto") == "auto"
    assert MANIFEST["services"] == ["mqtt:want"]
    assert MANIFEST["uart"] is True
    assert MANIFEST["ingress"] is True
    assert MANIFEST.get("ingress_port", 8099) == 8099
    assert MANIFEST.get("panel_admin", True) is True  # default true, admin-only panel
    assert not [k for k in _FORBIDDEN if k in MANIFEST]
    # /ssl read-only is the only mapped directory: it is needed for the optional TLS files of an
    # external broker (mqtt.tls_ca_file / tls_certfile / tls_keyfile). No config, share or data map.
    assert MANIFEST["map"] == ["ssl:ro"]
    pyproject = (APP / "pyproject.toml").read_text()
    assert f'version = "{MANIFEST["version"]}"' in pyproject
    assert not (APP / "build.yaml").exists()
    repo = yaml.safe_load((ROOT / "repository.yaml").read_text())
    assert repo["name"] == "Deye SG04LP3 Monitor"
    assert {"url", "maintainer"} <= set(repo)


def test_required_files_exist() -> None:
    for name in ("README.md", "DOCS.md", "CHANGELOG.md", "LICENSE", "icon.png", "logo.png"):
        assert (APP / name).is_file(), name
    for name in ("en.yaml", "de.yaml"):
        assert (APP / "translations" / name).is_file()
    png = (APP / "icon.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(png[16:20], "big") == int.from_bytes(png[20:24], "big") == 128


def test_s6_service_layout() -> None:
    svc = APP / "rootfs/etc/s6-overlay/s6-rc.d/deye-monitor"
    assert (svc / "type").read_text() == "longrun\n"
    assert (svc / "dependencies.d/base").is_file()
    assert (APP / "rootfs/etc/s6-overlay/user-bundles.d/user/contents.d/deye-monitor").is_file()
    for script in ("run", "finish"):
        path = svc / script
        text = path.read_text()
        assert text.startswith("#!/command/with-contenv bashio\n")
        assert "\r" not in text
        assert os.access(path, os.X_OK), script
    run = (svc / "run").read_text()
    assert "exec python -m deye_monitor" in run
    assert "bashio::services" in run
    assert "/run/s6/basedir/bin/halt" in (svc / "finish").read_text()


def test_dockerfile_context_and_pins() -> None:
    text = (APP / "Dockerfile").read_text()
    assert re.search(
        r"^ARG BUILD_FROM=ghcr\.io/home-assistant/base-python:3\.14-alpine3\.\d+$", text, re.M
    )
    assert re.search(r"^FROM ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+ AS uv$", text, re.M)
    assert "uv sync --frozen --no-dev" in text
    assert "COPY .." not in text
    assert not re.search(r"^ENTRYPOINT", text, re.M)
    assert "ghcr.io/home-assistant/base-python:latest" not in text


# -- schema <-> config.py ---------------------------------------------------------------------


def _schema_keys(node: Any) -> dict[str, Any]:
    return cast(dict[str, Any], node[0] if isinstance(node, list) else node)


MODEL_FIELDS: dict[str, type[BaseModel]] = {
    "endpoints": EndpointConfig,
    "inverters": InverterConfig,
    "mqtt": MqttConfig,
}


# Fields derived by the models themselves; they are not user options.
DERIVED_FIELDS = {"endpoints": {"host_display"}}


def test_schema_keys_match_models() -> None:
    assert set(SCHEMA) == set(AppConfig.model_fields)
    for name in ("endpoints", "inverters"):
        fields = set(MODEL_FIELDS[name].model_fields) - DERIVED_FIELDS.get(name, set())
        assert set(_schema_keys(SCHEMA[name])) == fields, name
    assert set(SCHEMA["mqtt"]) == set(MqttConfig.model_fields)


def _walk(schema: dict[str, Any], model: Any, where: str) -> None:
    for key, spec in schema.items():
        field = model.model_fields[key]
        if isinstance(spec, dict):
            continue
        assert isinstance(spec, str), f"{where}.{key}"
        optional = spec.endswith("?")
        assert optional == (not field.is_required()), f"{where}.{key}: required/optional mismatch"


def test_schema_required_flags_match_models() -> None:
    _walk(_schema_keys(SCHEMA["endpoints"]), EndpointConfig, "endpoints")
    _walk(_schema_keys(SCHEMA["inverters"]), InverterConfig, "inverters")
    _walk(SCHEMA["mqtt"], MqttConfig, "mqtt")
    top = {k: v for k, v in SCHEMA.items() if isinstance(v, str)}
    assert set(top) == {"log_level", "instance_id"}
    _walk(top, AppConfig, "top")


def _doc_examples() -> list[dict[str, Any]]:
    docs = (APP / "DOCS.md").read_text()
    blocks = re.findall(r"```yaml\n(.*?)```", docs, re.S)
    return [cast(dict[str, Any], yaml.safe_load(b)) for b in blocks]


def _example() -> dict[str, Any]:
    """The documented four-inverter plant (first YAML block in DOCS.md)."""
    examples = _doc_examples()
    assert len(examples) == 2  # four-inverter plant + minimal configuration
    return examples[0]


def through_supervisor(options: dict[str, Any]) -> dict[str, Any]:
    """The options as the app receives them: validated and coerced by the Supervisor rules."""
    return SupervisorOptions(SCHEMA, MANIFEST["name"], MANIFEST["slug"]).validate(options)


def accepted(options: dict[str, Any]) -> Any:
    """Supervisor validation first, then the app model (same chain as in Home Assistant)."""
    return parse_config(through_supervisor(options))


def test_documented_example_passes_supervisor_and_parser() -> None:
    example = _example()
    cfg = accepted(example)
    assert len(cfg.endpoints) == 8
    assert len(cfg.inverters) == 4
    assert all(i.fallback_endpoint for i in cfg.inverters)
    assert {i.unit_id for i in cfg.inverters} == {1}
    assert parse_config(example) == cfg  # nothing is changed by the Supervisor coercion here


def test_ci_fixture_passes_supervisor_and_parser() -> None:
    fixture = json.loads((ROOT / ".github" / "fixtures" / "options.json").read_text())
    cfg = accepted(fixture)
    assert len(cfg.inverters) == 1


def test_minimal_configuration_passes_supervisor_and_parser() -> None:
    minimal = {
        "endpoints": [{"id": "gw1", "transport": "modbus_tcp", "host": "192.0.2.10"}],
        "inverters": [{"id": "deye1", "name": "Deye 1", "endpoint": "gw1", "unit_id": 1}],
        "mqtt": {},
    }
    assert _doc_examples()[1]["mqtt"] == {}  # the documented minimal example keeps the block
    accepted(_doc_examples()[1])
    cfg = accepted(minimal)
    assert cfg.inverters[0].poll_fast_s == 10
    assert cfg.mqtt.host is None


def test_default_options_pass_supervisor_and_describe_an_empty_plant() -> None:
    options = MANIFEST["options"]
    cfg = parse_config(through_supervisor(options))
    assert cfg.endpoints == () and cfg.inverters == ()


def test_nested_mapping_is_required_by_the_supervisor() -> None:
    """Only "?" strings are optional: a missing mapping is rejected, a leaf-optional one is not
    created automatically. The decision (documented in DOCS): ``mqtt`` stays a required mapping
    (``mqtt: {}`` is enough); ``poll`` was flattened into four optional options."""
    example = _example()
    without_mqtt = {k: v for k, v in example.items() if k != "mqtt"}
    with pytest.raises(vol.Invalid, match="Missing option 'mqtt'"):
        through_supervisor(without_mqtt)
    parse_config(without_mqtt)  # the model alone is more lenient: documented difference
    example["mqtt"] = {}
    accepted(example)
    # no remaining nested mapping inside list items (they would be required in every item)
    for item in (SCHEMA["endpoints"][0], SCHEMA["inverters"][0]):
        assert not [k for k, v in item.items() if isinstance(v, dict | list)]


def test_poll_options_are_flat_and_optional() -> None:
    example = _example()
    base = accepted(example).inverters[0]
    assert (base.poll_fast_s, base.poll_energy_s, base.poll_settings_s, base.poll_identity_s) == (
        10,
        60,
        300,
        3600,
    )
    for partial in (
        {"poll_fast_s": 5},
        {"poll_energy_s": 30, "poll_identity_s": 600},
        {"poll_settings_s": 120},
        {"poll_fast_s": 1, "poll_energy_s": 1, "poll_settings_s": 1, "poll_identity_s": 1},
    ):
        data = _example()
        data["inverters"][0].update(partial)
        inv = accepted(data).inverters[0]
        for key, value in partial.items():
            assert getattr(inv, key) == value
        assert inv.poll.fast_s == inv.poll_fast_s
    for bad in (
        {"poll_fast_s": 0.5},
        {"poll_energy_s": 0},
        {"poll_settings_s": -1},
        {"poll_identity_s": "x"},
    ):
        data = _example()
        data["inverters"][0].update(bad)
        with pytest.raises(vol.Invalid):
            through_supervisor(data)
        with pytest.raises(ConfigError):
            parse_config(data)


def test_old_nested_poll_is_dropped_by_the_supervisor() -> None:
    data = _example()
    data["inverters"][0]["poll"] = {"fast_s": 5}
    sup = SupervisorOptions(SCHEMA)
    validated = sup.validate(data)
    assert "poll" not in validated["inverters"][0]
    assert "inverters.poll" in sup.ignored or "inverters" in " ".join(sup.ignored)
    with pytest.raises(ConfigError):  # the model itself refuses the retired key
        parse_config(data)


def test_known_schema_differences_are_intentional() -> None:
    """The schema is not a type-equality proof. Known differences (model vs Supervisor):
    ``timeout_s`` minimum 0.1 vs > 0, ``mqtt`` required vs defaulted, coercion of strings."""
    data = _example()
    data["endpoints"][0]["timeout_s"] = 0.05
    parse_config(data)
    with pytest.raises(vol.Invalid):
        through_supervisor(data)
    data = _example()
    data["endpoints"][0]["port"] = "502"  # coerced by the Supervisor, strict in the model
    assert accepted(data).endpoints[0].port == 502
    with pytest.raises(ConfigError):
        parse_config(data)


@pytest.mark.parametrize(
    ("path", "mutate"),
    [
        ("endpoints.port", lambda e, i: e[0].update(port=65535)),
        ("endpoints.max_read_registers", lambda e, i: e[0].update(max_read_registers=100)),
        ("endpoints.max_read_registers", lambda e, i: e[0].update(max_read_registers=6)),
        ("endpoints.queue_size", lambda e, i: e[0].update(queue_size=1000)),
        (
            "endpoints.baudrate",
            lambda e, i: e.append(
                {
                    "id": "s",
                    "transport": "serial_rtu",
                    "serial_device": "/dev/x",
                    "baudrate": 4000000,
                }
            ),
        ),
        ("endpoints.logger_serial", lambda e, i: e[4].update(logger_serial=4294967295)),
        ("endpoints.logger_serial", lambda e, i: e[4].update(logger_serial=1)),
        ("inverters.unit_id", lambda e, i: i[0].update(unit_id=247)),
        ("inverters.fallback_unit_id", lambda e, i: i[0].update(fallback_unit_id=247)),
        (
            "inverters.poll",
            lambda e, i: i[0].update(
                poll_fast_s=1, poll_energy_s=1, poll_settings_s=1, poll_identity_s=1
            ),
        ),
        ("inverters.expected_serial", lambda e, i: i[0].update(expected_serial="2401234567")),
        ("inverters.expected_serial", lambda e, i: i[0].update(expected_serial="A        Z")),
        ("endpoints.rtu_late_window_s", lambda e, i: e[0].update(rtu_late_window_s=0.1)),
    ],
)
def test_schema_boundary_values_accepted_by_supervisor_and_model(path: str, mutate: Any) -> None:
    example = _example()
    mutate(example["endpoints"], example["inverters"])
    accepted(example)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e, i: e[0].update(port=0),
        lambda e, i: e[0].update(port=65536),
        lambda e, i: e[0].update(max_read_registers=101),
        lambda e, i: e[0].update(max_read_registers=0),
        lambda e, i: e[0].update(max_read_registers=5),
        lambda e, i: e[4].update(logger_serial=0),
        lambda e, i: e[4].update(logger_serial=4294967296),
        lambda e, i: i[0].update(unit_id=0),
        lambda e, i: i[0].update(unit_id=248),
        lambda e, i: i[0].update(poll_fast_s=0.5),
        lambda e, i: i[0].update(energy_resolution="2"),
        lambda e, i: i[0].update(expected_serial="123"),
    ],
)
def test_schema_and_model_reject_the_same_out_of_range_values(mutate: Any) -> None:
    example = _example()
    mutate(example["endpoints"], example["inverters"])
    with pytest.raises(vol.Invalid):
        through_supervisor(example)
    with pytest.raises(ConfigError):
        parse_config(example)


def test_energy_resolution_enum_matches_model() -> None:
    values = re.fullmatch(r"list\((.*)\)\?", _schema_keys(SCHEMA["inverters"])["energy_resolution"])
    assert values
    assert values[1].split("|") == ["auto", "0.1", "1"]
    for v in values[1].split("|"):
        example = _example()
        example["inverters"][0]["energy_resolution"] = v
        parse_config(example)


def test_transport_enum_matches_model() -> None:
    values = re.fullmatch(r"list\((.*)\)", _schema_keys(SCHEMA["endpoints"])["transport"])
    assert values
    from deye_monitor.config import Transport

    assert values[1].split("|") == [t.value for t in Transport]


# -- translations ------------------------------------------------------------------------------


def _translation_keys(schema: Any, tr: dict[str, Any], where: str) -> None:
    for key, spec in _schema_keys(schema).items():
        assert key in tr, f"{where}.{key} missing"
        assert tr[key]["name"].strip() and tr[key]["description"].strip(), f"{where}.{key}"
        if isinstance(spec, dict):
            _translation_keys(spec, tr[key]["fields"], f"{where}.{key}")
    if isinstance(schema, dict | list):
        extra = set(tr) - set(_schema_keys(schema))
        assert not extra, f"{where}: unknown translation keys {extra}"


@pytest.mark.parametrize("lang", ["en", "de"])
def test_translations_cover_every_option(lang: str) -> None:
    data = yaml.safe_load((APP / "translations" / f"{lang}.yaml").read_text())
    assert set(data) == {"configuration"}
    cfg = data["configuration"]
    for key, spec in SCHEMA.items():
        assert key in cfg, key
        assert cfg[key]["name"].strip() and cfg[key]["description"].strip()
        if isinstance(spec, (dict, list)):
            _translation_keys(spec, cfg[key]["fields"], key)
    assert set(cfg) == set(SCHEMA)


def test_docs_mention_every_option() -> None:
    docs = (APP / "DOCS.md").read_text()
    names = set(EndpointConfig.model_fields) | set(InverterConfig.model_fields)
    names |= set(MqttConfig.model_fields) | {"log_level", "instance_id"}
    names -= DERIVED_FIELDS["endpoints"]
    for name in sorted(names):
        assert f"`{name}`" in docs or name in docs, name
    assert "DNS" in docs and "`bus_id`" in docs  # alias limit is documented
    assert "RTU" in docs and "no transaction id" in docs  # residual RTU limit is documented
    assert "read-only" in docs.lower() or "only reads" in docs.lower()
    assert "never scans" in docs


# -- supervisor MQTT + process lifecycle ---------------------------------------------------------


def test_supervisor_mqtt_fills_empty_host_only() -> None:
    env = {
        "DEYE_MONITOR_SUPERVISOR_MQTT_HOST": "core-mosquitto",
        "DEYE_MONITOR_SUPERVISOR_MQTT_PORT": "1884",
        "DEYE_MONITOR_SUPERVISOR_MQTT_USERNAME": "addons",
        "DEYE_MONITOR_SUPERVISOR_MQTT_PASSWORD": "supervisor-pw",
        "DEYE_MONITOR_SUPERVISOR_MQTT_SSL": "true",
    }
    filled = with_supervisor_mqtt(MqttConfig(), env)
    assert (filled.host, filled.port, filled.username, filled.tls) == (
        "core-mosquitto",
        1884,
        "addons",
        True,
    )
    assert filled.password is not None
    assert filled.password.get_secret_value() == "supervisor-pw"
    assert "supervisor-pw" not in repr(filled)
    explicit = MqttConfig(host="my-broker", port=1900)
    assert with_supervisor_mqtt(explicit, env) is explicit
    assert with_supervisor_mqtt(MqttConfig(), {}) == MqttConfig()
    with pytest.raises(ConfigError):
        with_supervisor_mqtt(MqttConfig(), {**env, "DEYE_MONITOR_SUPERVISOR_MQTT_PORT": "abc"})
    keep = with_supervisor_mqtt(MqttConfig(username="me"), env)
    assert keep.username == "me"


def _spawn(tmp_path: Path, options: dict[str, Any]) -> subprocess.Popen[str]:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options))
    env = {
        **os.environ,
        "DEYE_MONITOR_OPTIONS": str(path),
        "DEYE_MONITOR_DATA": str(tmp_path),
        "DEYE_MONITOR_NO_DEVICES": "1",  # tests never open device connections
    }
    return subprocess.Popen(
        [sys.executable, "-m", "deye_monitor"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_process_starts_and_stops_on_sigterm(tmp_path: Path) -> None:
    proc = _spawn(tmp_path, _example())
    assert proc.stdout is not None
    deadline = time.monotonic() + 20
    seen = ""
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        seen += line
        if "ready:" in line or not line:
            break
    assert "ready: 8 endpoint(s), 4 inverter(s)" in seen
    assert (tmp_path / "instance_id").is_file()
    proc.send_signal(signal.SIGTERM)
    rest, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert "shutting down" in rest


def test_process_exits_2_on_invalid_config_without_leaking_secrets(tmp_path: Path) -> None:
    proc = _spawn(
        tmp_path,
        {
            "inverters": [{"id": "a", "name": "A", "endpoint": "nope", "unit_id": 1}],
            "endpoints": [],
            "mqtt": {"password": "topsecret"},
        },
    )
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert "invalid configuration" in out
    assert "is not defined" in out
    assert "topsecret" not in out


def test_docs_and_readme_state_the_implementation_status_first() -> None:
    for name in ("DOCS.md", "README.md"):
        text = " ".join((APP / name).read_text().split("\n\n")[0:3]).replace(">", " ")
        head = " ".join(text.split()).lower()  # unwrap the quoted, line-wrapped status block
        assert "device loop with mqtt publisher" in head, name
        assert "web interface" in head and "does not exist yet" not in head, name
        assert "entities appear" in head, name
    docs = (APP / "DOCS.md").read_text()
    assert "planned" in docs.lower()  # the write layer is not there yet
    assert "### Using the web interface" in docs
    assert "not** a successful contact" in docs  # what the `ready` log line does and does not mean
    assert "entities appear in home assistant" in docs.lower()
    assert "running plus waiting" in docs  # queue_size semantics
    assert "DEYE_MONITOR_NO_DEVICES" in docs
    assert "active in the device loop" in docs.lower()  # RTU timing section
    assert "mqtt: {}" in docs  # the required block is documented
    assert "internal_error" in docs and "internal_errors" in docs  # task supervision is documented
    assert "identity_store_corrupt" in docs and "energy_store_corrupt" in docs  # fail closed
