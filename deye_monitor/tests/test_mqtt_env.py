"""Bash -> Python chain for the Supervisor MQTT service (PKG-02, PKG-03).

The shell helpers (``mqtt-env.sh``) are sourced by the real run script; they are exercised here
with simulated service answers and then handed to the app's own ``with_supervisor_mqtt``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.config import ConfigError, MqttConfig, parse_config, with_supervisor_mqtt

APP = Path(__file__).parents[1]
LIB = APP / "rootfs" / "usr" / "local" / "lib" / "deye-monitor" / "mqtt-env.sh"
RUN = APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "deye-monitor" / "run"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

PYTHON_SIDE = """
import json, os, sys
from deye_monitor.config import ConfigError, MqttConfig, with_supervisor_mqtt
try:
    m = with_supervisor_mqtt(MqttConfig(), os.environ)
except ConfigError as exc:
    print(json.dumps({"error": str(exc)})); sys.exit(0)
print(json.dumps({
    "host": m.host, "port": m.port, "username": m.username, "tls": m.tls,
    "password": m.password.get_secret_value() if m.password else None,
}))
"""


def chain(service: str) -> tuple[dict[str, Any], str]:
    """Run the shell export on ``service`` and read the result through the app model."""
    script = (
        f'source "{LIB}"; deye_export_supervisor_mqtt "$SERVICE_JSON"; {sys.executable} -c "$PY"'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEYE_MONITOR_")}
    env.update(SERVICE_JSON=service, PY=PYTHON_SIDE)
    done = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout.strip().splitlines()[-1]), done.stderr


def test_complete_service_answer() -> None:
    result, err = chain(
        json.dumps(
            {
                "host": "core-mosquitto",
                "port": 1884,
                "username": "addons",
                "password": "pw-1",
                "ssl": True,
            }
        )
    )
    assert result == {
        "host": "core-mosquitto",
        "port": 1884,
        "username": "addons",
        "tls": True,
        "password": "pw-1",
    }
    assert err == ""


@pytest.mark.parametrize(
    "service",
    [
        {"host": "core-mosquitto", "port": 1883, "ssl": False},  # no auth fields at all
        {"host": "core-mosquitto", "port": 1883, "username": None, "password": None, "ssl": False},
        {"host": "core-mosquitto", "port": 1883, "username": "", "password": ""},
    ],
)
def test_missing_null_or_empty_auth_means_anonymous(service: dict[str, Any]) -> None:
    result, _ = chain(json.dumps(service))
    assert result["host"] == "core-mosquitto"
    assert result["username"] is None  # never the text "null"
    assert result["password"] is None
    assert result["tls"] is False


def test_the_text_null_is_a_legitimate_credential() -> None:
    result, _ = chain(
        json.dumps({"host": "b", "port": 1883, "username": "null", "password": "null"})
    )
    assert (result["username"], result["password"]) == ("null", "null")


def test_missing_port_keeps_the_default() -> None:
    result, _ = chain(json.dumps({"host": "broker.local"}))
    assert (result["host"], result["port"]) == ("broker.local", 1883)


def test_empty_or_unusable_service_answers_are_ignored_or_rejected() -> None:
    for service in ("{}", "", "not json", '{"host": null}', '{"host": ""}'):
        result, _ = chain(service)
        assert result["host"] is None, service
    result, _ = chain(json.dumps({"host": "bad host", "port": 1883}))
    assert "error" in result  # a broken service host is an error, not a silent other broker


def test_service_secrets_are_never_printed() -> None:
    script = f'source "{LIB}"; deye_export_supervisor_mqtt "$SERVICE_JSON"'
    env = {
        **os.environ,
        "SERVICE_JSON": json.dumps({"host": "h", "username": "u-secret", "password": "p-secret"}),
    }
    done = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert done.stdout == "" and done.stderr == ""


def test_stale_environment_is_cleared_for_missing_fields() -> None:
    count = 'env | grep -c "^DEYE_MONITOR_SUPERVISOR_MQTT_USERNAME=" || true'
    script = f'source "{LIB}"; deye_export_supervisor_mqtt "$SERVICE_JSON"; {count}'
    env = {
        **os.environ,
        "SERVICE_JSON": '{"host": "h"}',
        "DEYE_MONITOR_SUPERVISOR_MQTT_USERNAME": "old",
    }
    done = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "0"


# -- mqtt.host in the options file: the same blank rule in shell and Python -----------------


def host_in_shell(tmp_path: Path, options: object) -> str:
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options))
    done = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; deye_mqtt_option_host "{path}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


MINIMAL: dict[str, Any] = {
    "endpoints": [{"id": "gw1", "transport": "modbus_tcp", "host": "192.0.2.10"}],
    "inverters": [{"id": "deye1", "name": "D", "endpoint": "gw1", "unit_id": 1}],
}


@pytest.mark.parametrize(
    ("mqtt", "service_used", "host"),
    [
        (None, True, None),  # block missing
        ({}, True, None),  # block empty
        ({"host": None}, True, None),
        ({"host": ""}, True, None),
        ({"host": "   "}, True, None),
        ({"host": "broker.local"}, False, "broker.local"),
        ({"host": " Broker.Local. "}, False, "broker.local"),
    ],
)
def test_blank_host_rule_is_identical_in_shell_and_python(
    tmp_path: Path, mqtt: dict[str, Any] | None, service_used: bool, host: str | None
) -> None:
    options = {**MINIMAL, **({} if mqtt is None else {"mqtt": mqtt})}
    assert (host_in_shell(tmp_path, options) == "") is service_used
    cfg = parse_config(options)
    assert cfg.mqtt.host == host
    # the service fills the host only when nothing explicit was configured
    env = {"DEYE_MONITOR_SUPERVISOR_MQTT_HOST": "core-mosquitto"}
    assert (with_supervisor_mqtt(cfg.mqtt, env).host == "core-mosquitto") is service_used


@pytest.mark.parametrize("bad", ["bad host", "http://h", "[::1", "h/x", "u@h"])
def test_invalid_mqtt_host_is_an_error_not_a_service_fallback(bad: str) -> None:
    with pytest.raises(ConfigError, match=r"mqtt\.host"):
        parse_config({**MINIMAL, "mqtt": {"host": bad}})


def test_blank_host_is_still_an_error_for_endpoints() -> None:
    options: dict[str, Any] = json.loads(json.dumps(MINIMAL))
    for blank in ("", "  "):
        options["endpoints"][0]["host"] = blank
        with pytest.raises(ConfigError, match=r"endpoints\.0\.host"):
            parse_config(options)


def test_model_accepts_blank_mqtt_host_directly() -> None:
    assert MqttConfig(host="").host is None
    assert MqttConfig(host="  ").host is None
    assert MqttConfig(host="b").host == "b"


def test_run_script_uses_the_tested_helpers_and_never_dumps() -> None:
    text = RUN.read_text()
    assert "deye_mqtt_option_host" in text and "deye_export_supervisor_mqtt" in text
    assert "bashio::services 'mqtt')" in text  # the service object is read once, as a whole
    for forbidden in ("env\n", "printenv", "set -x", 'echo "$(bashio::services'):
        assert forbidden not in text
    assert os.access(RUN, os.X_OK)
