"""TLS options of an external broker, the app wiring helpers and the new device hooks."""

from __future__ import annotations

import ssl
import subprocess
from pathlib import Path
from typing import Any

import pytest

from deye_monitor.app import build_publisher, open_discovery_store
from deye_monitor.config import ConfigError, MqttConfig, parse_config
from deye_monitor.device import InverterState, RelaySink
from deye_monitor.mqtt import build_tls_context
from deye_monitor.runtime import Runtime
from tests.device_helpers import config, ep, inverter


def mqtt(**kw: Any) -> MqttConfig:
    return MqttConfig.model_validate({"host": "broker.example", **kw})


def minimal(**mqtt_opts: Any) -> dict[str, Any]:
    return {
        "endpoints": [{"id": "gw1", "transport": "modbus_tcp", "host": "10.0.0.5"}],
        "inverters": [{"id": "deye1", "name": "D", "endpoint": "gw1", "unit_id": 1}],
        "mqtt": mqtt_opts,
    }


def test_tls_files_must_be_below_ssl_and_need_tls_true() -> None:
    ok = mqtt(
        tls=True, tls_ca_file="/ssl/ca.pem", tls_certfile="/ssl/c.pem", tls_keyfile="/ssl/k.pem"
    )
    assert ok.tls_ca_file == "/ssl/ca.pem"
    for bad in ("/etc/passwd", "ca.pem", "/ssl", "/ssl/", "/ssl/../etc/x", "/ssl//x", "/sslx/ca"):
        with pytest.raises(ValueError, match="/ssl"):
            mqtt(tls=True, tls_ca_file=bad)
    with pytest.raises(ValueError, match="require tls"):
        mqtt(tls_ca_file="/ssl/ca.pem")
    with pytest.raises(ValueError, match="together"):
        mqtt(tls=True, tls_certfile="/ssl/c.pem")
    with pytest.raises(ValueError, match="together"):
        mqtt(tls=True, tls_keyfile="/ssl/k.pem")
    assert mqtt(tls=True, tls_ca_file="  ").tls_ca_file is None  # blank = not set


def test_there_is_no_insecure_option() -> None:
    assert not any("insecure" in name for name in MqttConfig.model_fields)
    with pytest.raises(ConfigError):
        parse_config(minimal(tls=True, tls_insecure=True))


def test_birth_topic_is_a_plain_topic() -> None:
    assert mqtt().birth_topic == "homeassistant/status"
    assert mqtt(birth_topic="ha/custom/status").birth_topic == "ha/custom/status"
    for bad in ("ha/#", "ha/+/x", "", "a b"):
        with pytest.raises(ValueError, match="topic"):
            mqtt(birth_topic=bad)


def test_inverter_id_app_is_reserved() -> None:
    data = minimal()
    data["inverters"][0]["id"] = "app"
    with pytest.raises(ConfigError, match="reserved"):
        parse_config(data)


def test_tls_context_verifies_certificate_and_host(tmp_path: Path) -> None:
    assert build_tls_context(mqtt()) is None
    default = build_tls_context(mqtt(tls=True))
    assert default is not None
    assert default.verify_mode is ssl.CERT_REQUIRED and default.check_hostname is True


def test_tls_context_loads_ca_and_client_certificate(tmp_path: Path) -> None:
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    made = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(crt), "-subj", "/CN=test", "-days", "1"],
        capture_output=True,
    )  # fmt: skip
    if made.returncode != 0:
        pytest.skip("openssl is not available")
    cfg = mqtt(tls=True).model_copy(
        update={"tls_ca_file": str(crt), "tls_certfile": str(crt), "tls_keyfile": str(key)}
    )
    ctx = build_tls_context(cfg)
    assert ctx is not None
    assert ctx.verify_mode is ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_relay_sink_forwards_after_attachment() -> None:
    relay = RelaySink()
    relay.on_availability("x", InverterState.OFFLINE, "r")  # null target: no error

    class Rec:
        calls: list[Any] = []  # noqa: RUF012

        def on_state(self, *a: Any) -> None:
            self.calls.append(("state", a))

        def on_availability(self, *a: Any) -> None:
            self.calls.append(("avail", a))

        def on_diagnostics(self, *a: Any) -> None:
            self.calls.append(("diag", a))

    rec = Rec()
    relay.target = rec
    relay.on_availability("x", InverterState.RUNNING, "ok")
    relay.on_diagnostics("x", {"a": 1})
    assert [c[0] for c in rec.calls] == ["avail", "diag"]


def test_discovery_store_damaged_file_is_kept_and_replaced(tmp_path: Path) -> None:
    path = tmp_path / "discovery.json"
    path.write_text("{not json")
    store = open_discovery_store(path)
    assert not store.corrupt and store.data == {"devices": {}}
    assert (tmp_path / "discovery.json.corrupt").read_text() == "{not json"


async def test_build_publisher_wires_runtime_hooks(tmp_path: Path) -> None:
    cfg = config([ep("gw1")], [inverter("deye1", "gw1")])
    runtime = Runtime(cfg, directory=tmp_path)
    pub = build_publisher(
        cfg,
        runtime,
        cfg.mqtt.model_copy(update={"host": "127.0.0.1"}),
        "ab12cd34",
        directory=tmp_path,
    )
    inv = runtime.inverters["deye1"]
    assert pub.birth_topic == "homeassistant/status"
    assert pub._inv["deye1"].keys == inv.selection  # the published selection is the runtime's
    assert pub._inv["deye1"].revision == inv.selection_revision
    assert pub.ns.app_device_id == "deye_ab12cd34_app"
    await runtime.stop()


async def test_request_refresh_makes_running_groups_due(tmp_path: Path) -> None:
    cfg = config([ep("gw1")], [inverter("deye1", "gw1")])
    runtime = Runtime(cfg, directory=tmp_path)
    inv = runtime.inverters["deye1"]
    inv.request_refresh()  # not running: nothing to do, nothing breaks
    assert inv._due == {}
    inv.state = InverterState.RUNNING
    inv.needs_identity = False
    inv.request_refresh()
    assert inv._due and all(t <= inv.clock() for t in inv._due.values())
    await runtime.stop()
