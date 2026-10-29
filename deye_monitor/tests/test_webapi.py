"""Ingress API (SPEC §9.5, DR2-06/08): peer check, identity, CSRF, revisions, strict schema."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from deye_monitor.profiles.schema import load_profile
from deye_monitor.webapi import PURGE_CONFIRMATION, RESET_ALL_CONFIRMATION, WebApi, WebSettings
from tests.selection_helpers import (
    Plant,
    newest_body,
    no_selection_files,
    plant,
    two_inverters,
)

PROFILE = load_profile("sg04lp3_lv_v105_8")
USER = {"X-Remote-User-Id": "u1", "X-Remote-User-Name": "alice"}
SECRET_PASSWORD = "s3cret-broker-pw"


@dataclass
class Client:
    plant: Plant
    api: WebApi
    http: aiohttp.ClientSession
    base: str

    async def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        headers: Mapping[str, str] | None = None,
        user: Mapping[str, str] | None = USER,
        csrf: str | None = "auto",
        revision: int | str | None = "auto",
        raw: bytes | None = None,
        content_type: str | None = "application/json",
    ) -> tuple[int, Any]:
        h: dict[str, str] = dict(user or {})
        if csrf == "auto" and user:
            _, session = await self.call("GET", "/api/session", user=user, csrf=None)
            csrf = session["csrf"]
        if csrf and csrf != "auto":
            h["X-CSRF-Token"] = csrf
        if revision == "auto":
            actions = path.endswith(("/rebind", "/stores/reset", "/cleanup/purge"))
            revision = self.api.action_counter if actions else self.plant.manager.revision
        if revision is not None and method in ("POST", "PUT"):
            h["If-Match"] = f'"{revision}"'
        if content_type:
            h["Content-Type"] = content_type
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        async with self.http.request(method, self.base + path, data=data, headers=h) as resp:
            text = await resp.text()
            try:
                return resp.status, json.loads(text)
            except ValueError:
                return resp.status, text

    async def get(self, path: str, **kw: Any) -> tuple[int, Any]:
        return await self.call("GET", path, **kw)

    async def post(self, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        return await self.call("POST", path, {} if body is None else body, **kw)

    async def put(self, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        return await self.call("PUT", path, body, **kw)


@contextlib.asynccontextmanager
async def client(
    tmp_path: Path,
    *,
    peers: frozenset[str] = frozenset({"127.0.0.1"}),
    publisher: Any = None,
    **plant_kw: Any,
) -> AsyncIterator[Client]:
    async with plant(tmp_path, **plant_kw) as p:
        api = WebApi(
            p.rig.runtime,
            p.manager,
            publisher=publisher,
            settings=WebSettings("127.0.0.1", 0, peers),
        )
        port = await api.start()
        try:
            async with aiohttp.ClientSession() as http:
                yield Client(p, api, http, f"http://127.0.0.1:{port}")
        finally:
            await api.stop()


def err(body: Any) -> str:
    return str(body["error"]["key"])


MUTATIONS: list[tuple[str, str, Any]] = [
    ("PUT", "/api/inverters/deye1/features", {"features": {"battery": False}}),
    ("POST", "/api/inverters/deye1/sensors", {"remove": ["battery_soc"]}),
    ("POST", "/api/inverters/deye1/sensors/preview", {"remove": ["battery_soc"]}),
    ("POST", "/api/inverters/deye1/template/restore", {}),
    ("POST", "/api/inverters/deye1/rebind", {"confirm": True}),
    ("POST", "/api/transfer/preview", {"source": "deye1"}),
    ("POST", "/api/transfer", {"source": "deye1"}),
    ("POST", "/api/cleanup/purge", {"scope": "all", "confirm": PURGE_CONFIRMATION}),
    (
        "POST",
        "/api/stores/reset",
        {"kind": "energy", "scope": "inverter", "inverter": "deye1", "confirm": True},
    ),
]


# -- peer / identity / CSRF (DR2-08) ----------------------------------------------------------


async def test_forged_proxy_headers_from_another_peer_are_forbidden(tmp_path: Path) -> None:
    # default settings: only the Supervisor peer 172.30.32.2; the test connects from 127.0.0.1
    async with client(tmp_path, peers=frozenset({"172.30.32.2"})) as c:
        forged = {
            "X-Remote-User-Id": "admin",
            "X-Remote-User-Name": "admin",
            "X-Ingress-Path": "/api/hassio_ingress/x",
            "X-Forwarded-For": "172.30.32.2",
            "Forwarded": "for=172.30.32.2",
            "X-Real-IP": "172.30.32.2",
        }
        for method, path in [
            ("GET", "/"),
            ("GET", "/static/app.js"),
            ("GET", "/api/session"),
            ("GET", "/api/inverters"),
            ("GET", "/api/catalog"),
        ]:
            status, body = await c.call(method, path, headers=forged, csrf=None)
            assert status == 403 and err(body) == "forbidden_peer", path
        for method, path, payload in MUTATIONS:
            status, body = await c.call(
                method, path, payload, headers=forged, csrf=None, revision=0
            )
            assert status == 403 and err(body) == "forbidden_peer", path
        assert c.plant.manager.revision == 0


def test_the_supervisor_peer_is_fixed_by_default() -> None:
    assert WebSettings().allowed_peers == frozenset({"172.30.32.2"})


async def test_ipv4_mapped_peer_address_is_normalised() -> None:
    from deye_monitor.webapi import _normalise_ip

    assert _normalise_ip("::ffff:172.30.32.2") == "172.30.32.2"
    assert _normalise_ip("172.30.32.2") == "172.30.32.2"
    assert _normalise_ip("fe80::1%eth0") == "fe80::1"


async def test_missing_user_is_not_an_administrator(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        status, body = await c.get("/api/session", user=None, csrf=None)
        assert status == 200 and body["user"] is None and body["csrf"] is None
        for method, path, payload in MUTATIONS:
            status, body = await c.call(method, path, payload, user=None, csrf=None)
            assert status == 403 and err(body) == "identity_required", path
        # an empty or oversized header is no identity either
        for value in ("", " ", "x" * 200):
            status, body = await c.post(
                "/api/inverters/deye1/template/restore",
                headers={"X-Remote-User-Id": value, "X-CSRF-Token": "x"},
                user=None,
                csrf=None,
            )
            assert status == 403 and err(body) == "identity_required"
        assert c.plant.manager.revision == 0


async def test_csrf_missing_wrong_or_of_another_user(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        path, payload = "/api/inverters/deye1/sensors", {"remove": ["battery_soc"]}
        for token in (None, "", "wrong", "0" * 64):
            status, body = await c.post(path, payload, csrf=token)
            assert status == 403 and err(body) == "csrf_invalid"
        _, other = await c.get("/api/session", user={"X-Remote-User-Id": "u2"}, csrf=None)
        status, body = await c.post(path, payload, csrf=other["csrf"])  # u2's token for u1
        assert status == 403 and err(body) == "csrf_invalid"
        assert c.plant.manager.revision == 0
        status, _ = await c.post(path, payload)
        assert status == 200


@pytest.mark.parametrize(("method", "path"), [(m, p) for m, p, _ in MUTATIONS])
async def test_changing_routes_do_not_answer_get(tmp_path: Path, method: str, path: str) -> None:
    del method
    async with client(tmp_path) as c:
        status, body = await c.get(path, csrf=None)
        assert status == 405 and err(body) == "http_405"
        assert c.plant.manager.revision == 0


async def test_other_methods_on_read_routes_are_refused(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            for path in ("/api/session", "/api/catalog", "/api/inverters"):
                status, _ = await c.call(method, path, {})
                assert status == 405
        status, _ = await c.call("DELETE", "/api/inverters/deye1/sensors")
        assert status == 405


async def test_body_must_be_json_and_small(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        path = "/api/inverters/deye1/sensors"
        status, body = await c.post(path, content_type="text/plain", raw=b"{}")
        assert status == 415 and err(body) == "json_required"
        status, body = await c.post(
            path, content_type="application/x-www-form-urlencoded", raw=b"remove=battery_soc"
        )
        assert status == 415
        status, body = await c.post(path, raw=b"{not json")
        assert status == 400 and err(body) == "invalid_json"
        status, body = await c.post(path, raw=b"[" + b"1," * 200_000 + b"1]")
        assert status == 413
        status, body = await c.post(path, raw=b'"remove"')
        assert status == 400


# -- revision / If-Match -----------------------------------------------------------------------


async def test_revision_header_is_required_and_checked(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        path, payload = "/api/inverters/deye1/sensors", {"remove": ["battery_soc"]}
        status, body = await c.post(path, payload, revision=None)
        assert status == 428 and err(body) == "revision_required"
        for bad in ("abc", "-1", "1.5", "99999999999999999"):
            status, body = await c.post(path, payload, revision=bad)
            assert status == 400, bad
        status, body = await c.post(path, payload, revision=5)
        assert (
            status == 409
            and err(body) == "stale_revision"
            and body["error"]["params"] == {"current": 0}
        )
        status, body = await c.post(path, payload, revision=0)
        assert status == 200 and body["revision"] == 1
        status, body = await c.post(path, {"remove": ["pv1_power"]}, revision=0)  # stale now
        assert status == 409
        assert "pv1_power" in c.plant.manager.selection("deye1").effective


async def test_concurrent_requests_with_the_same_revision_only_one_wins(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        _, s = await c.get("/api/session", csrf=None)
        results = await asyncio.gather(
            *(
                c.post(
                    "/api/inverters/deye1/sensors",
                    {"remove": [k]},
                    csrf=s["csrf"],
                    revision=0,
                )
                for k in ("battery_soc", "pv1_power", "pv2_power", "battery_voltage")
            )
        )
        assert sorted(st for st, _ in results) == [200, 409, 409, 409]
        assert c.plant.manager.revision == 1


# -- strict schema -----------------------------------------------------------------------------


async def test_unknown_and_malformed_content_is_rejected(tmp_path: Path) -> None:
    placeholder = next(e.key for e in PROFILE.entries if e.support_status == "placeholder")
    async with client(tmp_path) as c:
        path = "/api/inverters/deye1/sensors"
        cases: list[tuple[Any, str]] = [
            ({"add": ["no_such_sensor"]}, "unknown_entry"),
            ({"add": [placeholder]}, "unknown_entry"),  # placeholders are not even offered
            ({"add": ["../../etc/passwd"]}, "invalid_request"),
            ({"add": ["R586"]}, "invalid_request"),
            ({"add": [586]}, "invalid_request"),
            ({"add": "battery_soc"}, "invalid_request"),
            ({"add": ["battery_soc"] * 700}, "invalid_request"),
            ({"remove": [], "add": []}, "invalid_request"),
            ({}, "invalid_request"),
            ({"remove": ["battery_soc"], "allow_writes": True}, "unknown_field"),
            ({"remove": ["battery_soc"], "register": 586}, "unknown_field"),
            ({"remove": ["battery_soc"], "frame": "0103"}, "unknown_field"),
            ({"remove": ["battery_soc"], "path": "/data"}, "unknown_field"),
            ([], "invalid_request"),
        ]
        for payload, key in cases:
            status, body = await c.post(path, payload)
            assert status == 400 and err(body) == key, payload
        assert c.plant.manager.revision == 0


async def test_features_are_strict_and_allow_writes_can_not_be_set(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        path = "/api/inverters/deye1/features"
        for payload, key in [
            ({"features": {"allow_writes": True}}, "unknown_feature"),
            ({"features": {"battery": "yes"}}, "invalid_feature_value"),
            ({"features": {"mppt1_used": 1}}, "invalid_feature_value"),
            ({"features": {"gen_port_mode": "x"}}, "invalid_feature_value"),
            ({"features": {}}, "invalid_request"),
            ({"features": []}, "invalid_request"),
            ({"features": {"battery": False}, "allow_writes": True}, "unknown_field"),
            ({"allow_writes": True}, "unknown_field"),
            ({"battery": False}, "unknown_field"),
        ]:
            status, body = await c.put(path, payload)
            assert status == 400 and err(body) == key, payload
        assert c.plant.manager.revision == 0
        assert all(not i.cfg.allow_writes for i in c.plant.rig.runtime.inverters.values())
        assert "allow_writes" not in json.dumps((await c.get("/api/inverters"))[1])


async def test_unknown_inverter_and_ids_are_rejected(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        for inverter in ("ghost", "Deye1", "a" * 40, "..", "deye1%2f.."):
            status, body = await c.post(f"/api/inverters/{inverter}/template/restore")
            assert status in (404, 400), inverter
        status, body = await c.get("/api/inverters/ghost")
        assert status == 404 and err(body) == "unknown_inverter"


# -- reading ------------------------------------------------------------------------------------


async def test_catalog_offers_no_placeholder_internal_or_unsupported(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        status, body = await c.get("/api/catalog")
        assert status == 200
        cat = body["profiles"]["sg04lp3_lv_v105_8"]
        keys = {e["key"] for e in cat["entries"]}
        by_key = {e.key: e for e in PROFILE.entries}
        assert keys == {k for k, e in by_key.items() if e.selectable}
        assert not [
            k
            for k in keys
            if by_key[k].support_status in ("placeholder", "unsupported_for_profile")
        ]
        assert not [k for k in keys if by_key[k].internal]
        raw = [e for e in cat["entries"] if e["raw_diagnostic"]]
        assert raw and all(e["release"] == "unresolved" and e["unit"] is None for e in raw)
        one = next(e for e in cat["entries"] if e["key"] == "battery_soc")
        assert set(one) >= {
            "name",
            "category",
            "point",
            "tags",
            "support_status",
            "release",
            "requires",
            "group",
            "default_enabled",
        }
        assert one["name"]["de"] and one["name"]["en"]
        assert one["requires"] == {"feature": "battery", "equals": True}


async def test_hardware_status_and_selection_views(tmp_path: Path) -> None:
    async with client(tmp_path, mqtt={"password": SECRET_PASSWORD, "host": "broker.local"}) as c:
        _, hw = await c.get("/api/hardware")
        assert hw["hardware"]["sg04lp3_eu"]["mppt_channels"] == 2
        status, body = await c.get("/api/inverters")
        assert status == 200 and [i["id"] for i in body["inverters"]] == ["deye1", "deye2"]
        one = body["inverters"][0]
        st, sel = one["status"], one["selection"]
        assert st["state"] == "starting" and st["serial"] is None
        for key in (
            "active_path",
            "bound",
            "capability_conflicts",
            "energy",
            "hints",
            "error_counters",
            "bus_utilisation",
            "suggested_features",
            "last_success_wall",
        ):
            assert key in st
        assert "_basis" not in st["suggested_features"]
        assert sel["effective"] == sel["template"] and sel["deviations"]["added"] == []
        assert set(sel["features"]) == {
            "utility_grid",
            "grid_port_use",
            "battery",
            "bms",
            "gen_port_mode",
            "mppt1_used",
            "mppt2_used",
            "parallel",
        }
        assert sel["budget"]["blocks"] > 0 and "groups" in sel["budget"]
        # no secrets, no raw frames anywhere
        everything = json.dumps(
            [body, hw, (await c.get("/api/catalog"))[1], (await c.get("/api/session"))[1]]
        )
        assert SECRET_PASSWORD not in everything and "broker.local" not in everything


async def test_status_view_after_a_running_cycle(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        inv = c.plant.rig.inv("deye1")
        await inv.step()
        for _ in range(2):
            c.plant.rig.clock.offset += 11
            await inv.step()
        _, body = await c.get("/api/inverters/deye1")
        st = body["status"]
        assert st["state"] == "running" and st["serial"] == "2401234567" and st["bound"] is True
        assert st["identity"]["firmware_words"] == {"15": 0x1001, "17": 0x1000, "18": 0x0070}
        assert st["last_success_wall"] == 1_700_000_000.0
        assert st["error_counters"]["transport"]["primary"]  # counters only


async def test_session_reports_problems_and_store_state(tmp_path: Path) -> None:
    (tmp_path / "selection.json").write_text("garbage")
    async with client(tmp_path) as c:
        _, s = await c.get("/api/session", csrf=None)
        assert s["store"] == {"status": "corrupt", "source": "template"}
        assert {p["code"] for p in s["problems"]} >= {"selection_store_corrupt"}
        assert s["writes_enabled"] is False and s["languages"] == ["de", "en"]
        assert s["user"] == {"id": "u1", "name": "alice", "display_name": None}


# -- selection changes --------------------------------------------------------------------------


async def test_sensor_changes_and_dry_run_preview(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        status, prev = await c.post(
            "/api/inverters/deye1/sensors/preview",
            {"remove": ["battery_soc"], "add": ["gen_voltage_a"]},
        )
        assert status == 200 and prev["preview"]["drops"] == ["battery_soc"]
        assert prev["preview"]["adds"] == ["gen_voltage_a"] and prev["revision"] == 0
        assert c.plant.manager.revision == 0 and no_selection_files(tmp_path)
        status, body = await c.post(
            "/api/inverters/deye1/sensors", {"remove": ["battery_soc"], "add": ["gen_voltage_a"]}
        )
        assert status == 200 and body["revision"] == 1 and body["changed"] == ["deye1"]
        sel = body["inverters"]["deye1"]
        assert sel["deviations"] == {
            "added": ["gen_voltage_a"],
            "removed": ["battery_soc"],
            "redundant": [],
        }
        inv = c.plant.rig.inv("deye1")
        assert "battery_soc" not in inv.selection and "gen_voltage_a" in inv.selection
        status, body = await c.post("/api/inverters/deye1/sensors", {"reset": ["battery_soc"]})
        assert status == 200 and "battery_soc" in inv.selection
        status, body = await c.post("/api/inverters/deye1/template/restore")
        assert status == 200 and body["inverters"]["deye1"]["deviations"]["added"] == []
        assert "gen_voltage_a" not in inv.selection and inv.selection_revision == 3


async def test_features_change_template_but_not_access(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        status, body = await c.put(
            "/api/inverters/deye1/features", {"features": {"battery": False, "mppt2_used": False}}
        )
        assert status == 200
        sel = body["inverters"]["deye1"]
        assert "battery_soc" not in sel["template"] and "pv2_power" not in sel["template"]
        assert sel["features"]["battery"] is False
        status, _ = await c.post("/api/inverters/deye1/sensors", {"add": ["battery_soc"]})
        assert status == 200  # a deviation is allowed: the template is only the default
        assert "battery_soc" in c.plant.rig.inv("deye1").selection


async def test_empty_selection_has_no_entities_but_status_stays(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        template = c.plant.manager.selection("deye1").template
        status, body = await c.post("/api/inverters/deye1/sensors", {"remove": sorted(template)})
        assert status == 200 and body["inverters"]["deye1"]["effective"] == []
        assert c.plant.rig.inv("deye1").selection == frozenset()
        status, one = await c.get("/api/inverters/deye1")
        assert status == 200 and one["status"]["state"] and one["selection"]["effective"] == []
        assert one["selection"]["budget"]["blocks"] > 0  # the mandatory identity/health reads stay
        status, _ = await c.get("/api/inverters")
        assert status == 200


async def test_restart_keeps_the_changes_made_through_the_api(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        await c.post("/api/inverters/deye2/sensors", {"remove": ["battery_soc"]})
    async with client(tmp_path) as c:
        _, body = await c.get("/api/session")
        assert body["revision"] == 1
        assert "battery_soc" not in c.plant.rig.inv("deye2").selection
        _, one = await c.get("/api/inverters/deye2")
        assert one["selection"]["removed"] == ["battery_soc"]


async def test_failed_persist_through_the_api_leaves_everything_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with client(tmp_path) as c:

        def refuse(*_: Any, **__: Any) -> None:
            raise OSError("read-only")

        monkeypatch.setattr("deye_monitor.store.os.replace", refuse)
        status, body = await c.post("/api/inverters/deye1/sensors", {"remove": ["battery_soc"]})
        assert status == 500 and err(body) == "persist_failed"
        monkeypatch.undo()
        assert c.plant.manager.revision == 0
        assert "battery_soc" in c.plant.rig.inv("deye1").selection


# -- transfer -----------------------------------------------------------------------------------


async def test_transfer_preview_with_heterogeneous_inverters(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        await c.put(
            "/api/inverters/deye1/features",
            {"features": {"gen_port_mode": "generator", "mppt2_used": False}},
        )
        await c.post(
            "/api/inverters/deye1/sensors", {"add": ["gen_voltage_a"], "remove": ["battery_soc"]}
        )
        target = c.plant.rig.inv("deye2")
        target.capability_conflicts = [{"code": "mppt_count", "detail": "R22 reports 1 MPPT"}]
        before = (c.plant.manager.revision, json.dumps(newest_body(tmp_path), sort_keys=True))
        status, body = await c.post("/api/transfer/preview", {"source": "deye1"})
        assert status == 200 and list(body["targets"]) == ["deye2"]
        t = body["targets"]["deye2"]
        assert t["ok"] is True
        src = c.plant.manager.selection("deye1")
        assert set(t["selection"]["effective"]) == src.effective
        assert t["selection"]["features"]["gen_port_mode"] == "generator"
        assert "battery_soc" in t["drops"]  # the target had it, the source removed it
        assert t["capability_conflicts"][0]["code"] == "mppt_count"
        assert t["capability_warnings"] and all(
            PROFILE.entry(k).point == "pv" for k in t["capability_warnings"]
        )
        assert (
            c.plant.manager.revision,
            json.dumps(newest_body(tmp_path), sort_keys=True),
        ) == before
        assert c.plant.manager.selection("deye2").effective != src.effective  # nothing applied


async def test_transfer_applies_to_valid_targets_only_and_copies_no_identity(
    tmp_path: Path,
) -> None:
    eps, invs = two_inverters()
    invs.append({**invs[1], "id": "deye3", "unit_id": 3})
    async with client(tmp_path, endpoints=eps, inverters=invs) as c:
        mgr = c.plant.manager
        # deye3 runs a profile without one of the entries: not a valid target
        broken = replace(
            mgr.specs["deye3"],
            profile=mgr.specs["deye3"].profile.model_copy(
                update={"entries": tuple(e for e in PROFILE.entries if e.key != "gen_voltage_a")}
            ),
        )
        mgr.store.specs["deye3"] = broken
        await c.post("/api/inverters/deye1/sensors", {"add": ["gen_voltage_a"]})
        serial_before = {i: x.book.bound(i) for i, x in c.plant.rig.runtime.inverters.items()}
        status, prev = await c.post("/api/transfer/preview", {"source": "deye1"})
        assert prev["targets"]["deye2"]["ok"] is True
        assert prev["targets"]["deye3"]["ok"] is False
        assert prev["targets"]["deye3"]["error"]["key"] in ("unknown_entry", "entry_not_selectable")
        status, body = await c.post("/api/transfer", {"source": "deye1"})
        assert status == 200 and body["applied"] == ["deye2"] and set(body["skipped"]) == {"deye3"}
        assert body["revision"] == 2  # one revision for the whole transfer
        assert "gen_voltage_a" in c.plant.rig.inv("deye2").selection
        assert "gen_voltage_a" not in c.plant.rig.inv("deye3").selection
        assert {
            i: x.book.bound(i) for i, x in c.plant.rig.runtime.inverters.items()
        } == serial_before
        assert all(not i.cfg.allow_writes for i in c.plant.rig.runtime.inverters.values())
        assert "serial" not in json.dumps(newest_body(tmp_path))


async def test_transfer_requests_are_validated(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        for payload, key in [
            ({}, "missing_field"),
            ({"source": "ghost"}, "unknown_inverter"),
            ({"source": "deye1", "targets": ["deye1"]}, "invalid_request"),
            ({"source": "deye1", "targets": ["deye2", "deye2"]}, "invalid_request"),
            ({"source": "deye1", "targets": "deye2"}, "invalid_request"),
            ({"source": "deye1", "include_features": "yes"}, "invalid_request"),
            ({"source": "deye1", "allow_writes": True}, "unknown_field"),
            ({"source": "deye1", "serial": "x"}, "unknown_field"),
        ]:
            status, body = await c.post("/api/transfer/preview", payload)
            assert status in (400, 404) and err(body) == key, payload
        status, body = await c.post("/api/transfer", {"source": "deye1"}, revision=3)
        assert status == 409
        status, body = await c.post("/api/transfer", {"source": "deye1"})  # nothing differs
        assert status == 200 and body["changed"] == []


# -- identity / store actions -------------------------------------------------------------------


async def test_rebind_needs_confirmation_and_respects_expected_serial(tmp_path: Path) -> None:
    eps, invs = two_inverters(expected_serial="2409876543")
    async with client(tmp_path, endpoints=eps, inverters=invs) as c:
        for payload in ({}, {"confirm": False}, {"confirm": "yes"}, {"confirm": True, "x": 1}):
            status, _ = await c.post("/api/inverters/deye1/rebind", payload)
            assert status == 400
        await c.plant.rig.inv("deye1").step()
        assert c.plant.rig.runtime.book.bound("deye1") == "2401234567"
        status, body = await c.post("/api/inverters/deye1/rebind", {"confirm": True})
        assert status == 200 and c.plant.rig.runtime.book.bound("deye1") is None
        status, body = await c.post("/api/inverters/deye2/rebind", {"confirm": True})
        assert status == 409 and err(body) == "rebind_not_possible"  # bound by the option


async def test_store_resets_need_explicit_scope_and_confirmation(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        run = c.plant.rig.runtime
        await run.inverters["deye1"].step()
        epoch2 = run.inverters["deye2"].lifecycle_epoch
        assert run.book.bound("deye1")
        path = "/api/stores/reset"
        bad: list[Any] = [
            {},
            {"kind": "identity", "scope": "inverter", "inverter": "deye1"},  # no confirm
            {"kind": "identity", "scope": "inverter", "inverter": "deye1", "confirm": "true"},
            {"kind": "identity", "scope": "global", "confirm": True},
            {"kind": "identity", "scope": "global", "confirm": "yes"},
            {
                "kind": "identity",
                "scope": "global",
                "inverter": "deye1",
                "confirm": RESET_ALL_CONFIRMATION,
            },
            {"kind": "energy", "scope": "global", "confirm": RESET_ALL_CONFIRMATION},
            {"kind": "energy", "scope": "inverter", "confirm": True},  # no inverter
            {"kind": "files", "scope": "inverter", "inverter": "deye1", "confirm": True},
            {"kind": "identity", "scope": "inverter", "inverter": "../x", "confirm": True},
            {
                "kind": "identity",
                "scope": "inverter",
                "inverter": "deye1",
                "confirm": True,
                "path": "/data",
            },
        ]
        for payload in bad:
            status, _ = await c.post(path, payload)
            assert status == 400, payload
        assert run.book.bound("deye1")  # nothing happened
        status, body = await c.post(
            path, {"kind": "identity", "scope": "inverter", "inverter": "ghost", "confirm": True}
        )
        assert status == 404
        status, body = await c.post(
            path, {"kind": "identity", "scope": "inverter", "inverter": "deye1", "confirm": True}
        )
        assert status == 200 and run.book.bound("deye1") is None
        assert run.inverters["deye2"].lifecycle_epoch == epoch2  # the neighbour is not disturbed
        status, body = await c.post(
            path, {"kind": "energy", "scope": "inverter", "inverter": "deye2", "confirm": True}
        )
        assert status == 200 and body["inverter"] == "deye2"


async def test_global_identity_reset_revokes_everything(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        run = c.plant.rig.runtime
        for inv in run.inverters.values():
            await inv.step()
        epochs = {i: x.lifecycle_epoch for i, x in run.inverters.items()}
        status, body = await c.post(
            "/api/stores/reset",
            {"kind": "identity", "scope": "global", "confirm": RESET_ALL_CONFIRMATION},
        )
        assert status == 200 and body["inverter"] is None
        assert all(run.book.bound(i) is None for i in run.inverters)
        assert all(x.lifecycle_epoch > epochs[i] for i, x in run.inverters.items())


async def test_damaged_identity_file_refuses_a_single_inverter_reset(tmp_path: Path) -> None:
    (tmp_path / "identity.json").write_text("garbage")
    async with client(tmp_path) as c:
        status, body = await c.post(
            "/api/stores/reset",
            {"kind": "identity", "scope": "inverter", "inverter": "deye1", "confirm": True},
        )
        assert status == 409 and err(body) == "reset_requires_global"
        status, _ = await c.post(
            "/api/stores/reset",
            {"kind": "identity", "scope": "global", "confirm": RESET_ALL_CONFIRMATION},
        )
        assert status == 200


# -- static files -------------------------------------------------------------------------------


async def test_static_files_come_from_the_package_with_relative_urls(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        status, html = await c.get("/", csrf=None)
        assert status == 200 and "static/app.js" in html and 'src="/' not in html
        assert 'href="/' not in html
        for path in ("/static/app.js", "/static/app.css"):
            status, _ = await c.get(path, csrf=None)
            assert status == 200
        for bad in (
            "/static/nope.js",
            "/static/..%2fidentity.json",
            "/static/%2e%2e/selection.json",
            "/static/.hidden",
            "/static/a/../../x",
            "/static//etc/passwd",
            "/static/app.js/",
            "/static/a/b/c/d/e/f.js",
        ):
            status, _ = await c.get(bad, csrf=None)
            assert status in (404, 400), bad
        async with c.http.get(
            c.base + "/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"}
        ) as r:
            assert r.status == 200
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert "default-src 'self'" in r.headers["Content-Security-Policy"]


async def test_error_bodies_carry_keys_only(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        status, body = await c.get("/api/nothing", csrf=None)
        assert status == 404 and body == {"error": {"key": "http_404", "params": {}}}
        status, body = await c.post("/api/inverters/deye1/sensors", {"add": ["zz"]})
        assert set(body) == {"error"} and set(body["error"]) == {"key", "params"}
        async with c.http.get(c.base + "/api/session") as r:
            assert r.headers["Cache-Control"] == "no-store"


# -- cleanup ledger purge (T8-01) ----------------------------------------------------------------


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def purge_cleanup_ledger(self, inverter_id: str | None = None) -> int:
        self.calls.append(inverter_id)
        return 3


async def test_purge_needs_confirmation_scope_revision_and_csrf(tmp_path: Path) -> None:
    pub = FakePublisher()
    async with client(tmp_path, publisher=pub) as c:
        path = "/api/cleanup/purge"
        ok = {"scope": "inverter", "inverter": "deye1", "confirm": PURGE_CONFIRMATION}
        for payload, status_code, key in [
            ({}, 400, "missing_field"),
            ({"scope": "all"}, 400, "missing_field"),
            ({"scope": "all", "confirm": True}, 400, "confirmation_required"),
            ({"scope": "all", "confirm": "yes"}, 400, "confirmation_required"),
            ({"scope": "everything", "confirm": PURGE_CONFIRMATION}, 400, "confirmation_required"),
            ({"scope": "inverter", "confirm": PURGE_CONFIRMATION}, 400, "invalid_request"),
            (
                {"scope": "inverter", "inverter": "ghost", "confirm": PURGE_CONFIRMATION},
                404,
                "unknown_inverter",
            ),
            (
                {"scope": "all", "inverter": "deye1", "confirm": PURGE_CONFIRMATION},
                400,
                "invalid_request",
            ),
            ({**ok, "force": True}, 400, "unknown_field"),
        ]:
            status, body = await c.post(path, payload)
            assert status == status_code and err(body) == key, payload
        status, body = await c.post(path, ok, revision=None)
        assert status == 428
        status, body = await c.post(path, ok, revision=9)
        assert status == 409 and err(body) == "stale_revision"
        status, body = await c.post(path, ok, csrf="wrong")
        assert status == 403 and err(body) == "csrf_invalid"
        status, body = await c.post(path, ok, user=None, csrf=None)
        assert status == 403 and err(body) == "identity_required"
        status, body = await c.get(path, csrf=None)
        assert status == 405
        assert pub.calls == []  # none of the above reached the ledger
        status, body = await c.post(path, ok)
        assert status == 200 and body == {
            "ok": True, "scope": "inverter", "inverter": "deye1", "dropped": 3,
            "action_counter": 1,
        }  # fmt: skip
        status, body = await c.post(path, {"scope": "all", "confirm": PURGE_CONFIRMATION})
        assert status == 200 and body["inverter"] is None
        assert pub.calls == ["deye1", None]


async def test_purge_without_a_publisher_is_refused(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        status, body = await c.post(
            "/api/cleanup/purge", {"scope": "all", "confirm": PURGE_CONFIRMATION}
        )
        assert status == 409 and err(body) == "publisher_unavailable"


# -- T11 additions: whole-draft edits, connection info ------------------------------------------


async def test_sensor_edit_can_carry_features_and_restore_for_one_atomic_save(
    tmp_path: Path,
) -> None:
    async with client(tmp_path) as c:
        path = "/api/inverters/deye1/sensors"
        draft = {"features": {"battery": False}, "remove": ["grid_voltage_a"]}
        _, preview = await c.post(path + "/preview", draft)
        sel = preview["preview"]["selection"]
        assert "battery_soc" in preview["preview"]["drops"]
        assert "battery_soc" not in sel["effective"] and "grid_voltage_a" not in sel["effective"]
        assert c.plant.manager.revision == 0  # a preview stores nothing
        status, body = await c.post(path, draft)
        assert status == 200 and body["revision"] == 1
        record = c.plant.manager.selection("deye1")
        assert record.features.battery is False and "grid_voltage_a" in record.removed
        status, body = await c.post(path, {"restore_template": True})
        assert status == 200 and c.plant.manager.selection("deye1").removed == frozenset()
        bad_edits: list[dict[str, Any]] = [
            {"features": {}},
            {"features": []},
            {"restore_template": "yes"},
        ]
        for bad in bad_edits:
            status, body = await c.post(path, bad)
            assert status == 400 and err(body) == "invalid_request", bad
        status, body = await c.post(path, {"features": {"allow_writes": True}})
        assert status == 400 and err(body) == "unknown_feature"


async def test_inverter_view_names_the_connection_without_addresses(tmp_path: Path) -> None:
    async with client(tmp_path) as c:
        _, body = await c.get("/api/inverters")
        first = body["inverters"][0]
        assert first["connection"] == {
            "endpoint": "gw1",
            "transport": "modbus_tcp",
            "serial_pinned": False,
        }
        assert isinstance(body["now"], float)
        assert "127.0.0.1" not in json.dumps(first)
