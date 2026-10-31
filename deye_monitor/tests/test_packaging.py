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
    assert MANIFEST["version"] == deye_monitor.__version__ == "0.2.9"
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

# The options hold only MQTT, the log level and the instance id: connections and inverters are
# set up in the web interface and stored in /data/plant.json.
OPTIONS_ONLY = {"mqtt", "log_level", "instance_id"}
MINIMAL: dict[str, Any] = {"mqtt": {}}


def _walk(schema: dict[str, Any], model: Any, where: str) -> None:
    for key, spec in schema.items():
        field = model.model_fields[key]
        if isinstance(spec, dict):
            continue
        assert isinstance(spec, str), f"{where}.{key}"
        optional = spec.endswith("?")
        assert optional == (not field.is_required()), f"{where}.{key}: required/optional mismatch"


def test_schema_has_only_the_app_options() -> None:
    assert set(SCHEMA) == OPTIONS_ONLY
    assert set(MANIFEST["options"]) == {"mqtt", "log_level"}
    assert set(SCHEMA["mqtt"]) == set(MqttConfig.model_fields)
    assert "endpoints" not in SCHEMA and "inverters" not in SCHEMA


def test_schema_required_flags_match_models() -> None:
    _walk(SCHEMA["mqtt"], MqttConfig, "mqtt")
    top = {k: v for k, v in SCHEMA.items() if isinstance(v, str)}
    assert set(top) == {"log_level", "instance_id"}
    _walk(top, AppConfig, "top")


def through_supervisor(options: dict[str, Any]) -> dict[str, Any]:
    """The options as the app receives them: validated and coerced by the Supervisor rules."""
    return SupervisorOptions(SCHEMA, MANIFEST["name"], MANIFEST["slug"]).validate(options)


def accepted(options: dict[str, Any]) -> Any:
    """Supervisor validation first, then the app model (same chain as in Home Assistant)."""
    return parse_config(through_supervisor(options))


def test_ci_fixture_passes_supervisor_and_parser() -> None:
    fixture = json.loads((ROOT / ".github" / "fixtures" / "options.json").read_text())
    assert set(fixture) <= OPTIONS_ONLY
    cfg = accepted(fixture)
    assert cfg.mqtt.host == "192.0.2.30"


def test_minimal_configuration_passes_supervisor_and_parser() -> None:
    cfg = accepted(MINIMAL)
    assert cfg.mqtt.host is None and cfg.endpoints == () and cfg.inverters == ()


def test_default_options_pass_supervisor_and_describe_an_empty_plant() -> None:
    cfg = parse_config(through_supervisor(MANIFEST["options"]))
    assert cfg.endpoints == () and cfg.inverters == ()


def test_nested_mapping_is_required_by_the_supervisor() -> None:
    """Only "?" strings are optional: a missing mapping is rejected. ``mqtt`` stays a required
    mapping (``mqtt: {}`` is enough)."""
    with pytest.raises(vol.Invalid, match="Missing option 'mqtt'"):
        through_supervisor({"log_level": "info"})
    parse_config({"log_level": "info"})  # the model alone is more lenient: documented difference
    accepted(MINIMAL)


def test_connections_and_inverters_are_not_options_any_more() -> None:
    sup = SupervisorOptions(SCHEMA)
    validated = sup.validate({"mqtt": {}, "endpoints": [], "inverters": []})
    assert "endpoints" not in validated and "inverters" not in validated


@pytest.mark.parametrize(
    "bad",
    [
        {"mqtt": {"port": 0}},
        {"mqtt": {"port": 65536}},
        {"log_level": "loud"},
        {"instance_id": "ABC"},
    ],
)
def test_schema_and_model_reject_the_same_values(bad: dict[str, Any]) -> None:
    with pytest.raises(vol.Invalid):
        through_supervisor({"mqtt": {}, **bad} | ({"mqtt": bad["mqtt"]} if "mqtt" in bad else {}))
    with pytest.raises(ConfigError):
        parse_config({"mqtt": {}, **bad} | ({"mqtt": bad["mqtt"]} if "mqtt" in bad else {}))


def test_transport_values_are_documented() -> None:
    from deye_monitor.config import Transport

    docs = (APP / "DOCS.md").read_text()
    for transport in Transport:
        assert transport.value in docs


# -- translations ------------------------------------------------------------------------------


DERIVED_FIELDS = {"endpoints": {"host_display"}}  # derived by the model, not a user option


def _schema_keys(node: Any) -> dict[str, Any]:
    return cast(dict[str, Any], node[0] if isinstance(node, list) else node)


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
    names = set(EndpointConfig.model_fields) | set(InverterConfig.model_fields)  # web interface
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
    proc = _spawn(tmp_path, MINIMAL)
    assert proc.stdout is not None
    deadline = time.monotonic() + 20
    seen = ""
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        seen += line
        if "ready:" in line or not line:
            break
    assert "ready: 0 endpoint(s), 0 inverter(s)" in seen
    assert (tmp_path / "instance_id").is_file()
    proc.send_signal(signal.SIGTERM)
    rest, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert "shutting down" in rest


def test_process_exits_2_on_invalid_config_without_leaking_secrets(tmp_path: Path) -> None:
    proc = _spawn(tmp_path, {"mqtt": {"password": "topsecret", "port": "x"}})
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 2
    assert "invalid configuration" in out
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
