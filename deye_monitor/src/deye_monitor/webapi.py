"""Ingress JSON API for the sensor selection and the status view (SPEC §9.5).

Security (DR2-08): connections are accepted only from the Supervisor peer; ``X-Remote-User-*`` is
read only after that check and a missing user is never an administrator. Changing calls are
POST/PUT only, need a user, a CSRF token bound to that user, a JSON body that matches a strict
schema (known keys only, no free registers/paths/frames) and, for selection changes, the current
revision in ``If-Match``. Nothing here can enable writes to a device. Responses carry text KEYS
(the UI translates); no secrets and no raw frames.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from deye_monitor import probe
from deye_monitor.config import (
    EndpointConfig,
    InverterConfig,
    PlantConfig,
    Transport,
    plant_problems,
)
from deye_monitor.device import CONFLICT_POINTS, InverterRuntime
from deye_monitor.mqtt import MqttPublisher
from deye_monitor.observation import ObservationState
from deye_monitor.plant import MAX_ENDPOINTS, MAX_INVERTERS, PlantService
from deye_monitor.profiles.plan import ReadPlan
from deye_monitor.profiles.schema import Entry, Profile
from deye_monitor.runtime import Runtime
from deye_monitor.selection import (
    ChangeError,
    Edit,
    InverterSelection,
    Record,
    SelectionManager,
)

log = logging.getLogger(__name__)

SUPERVISOR_PEER = "172.30.32.2"
RESET_ALL_CONFIRMATION = "reset-all-identity-bindings"
PURGE_CONFIRMATION = "purge-cleanup-ledger"
MAX_BODY = 64 * 1024
MAX_KEYS = 600

_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_REVISION_RE = re.compile(r'^(?:W/)?"?([0-9]{1,12})"?$')
_SERIAL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_ENDPOINT_FIELDS = (
    "id",
    "host",
    "port",
    "logger_serial",
    "serial_device",
    "baudrate",
    "bus_id",
    "timeout_s",
    "rtu_late_window_s",
    "max_read_registers",
    "request_gap_ms",
    "queue_size",
)
_INVERTER_FIELDS = (
    "id",
    "fallback_endpoint",
    "fallback_unit_id",
    "expected_serial",
    "energy_resolution",
    "poll_fast_s",
    "poll_energy_s",
    "poll_settings_s",
    "poll_identity_s",
)
_STATIC_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'self'"
    ),
}
_HINT_STATES = frozenset(
    {
        ObservationState.ZERO_ONLY_INCONCLUSIVE,
        ObservationState.KNOWN_PLACEHOLDER,
        ObservationState.READ_FAILED,
        ObservationState.CAPABILITY_CONFLICT,
    }
)


@dataclass(frozen=True, slots=True)
class WebSettings:
    host: str = "0.0.0.0"
    port: int = 8099
    # Only for tests: the Supervisor peer is fixed in production.
    allowed_peers: frozenset[str] = field(default_factory=lambda: frozenset({SUPERVISOR_PEER}))
    # The only directory serial devices of connections created in the web interface may come from
    # (listed by name, never scanned or opened by the listing).
    serial_dir: str = "/dev/serial/by-id"


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str | None
    display_name: str | None


def _jsonable(data: Any) -> str:
    return json.dumps(data, allow_nan=False, separators=(",", ":"), sort_keys=False)


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_jsonable)


def error_body(key: str, **params: Any) -> dict[str, Any]:
    return {"error": {"key": key, "params": params}}


# -- schema helpers ---------------------------------------------------------------------------


def _object(
    body: Any, *, required: Collection[str] = (), optional: Collection[str] = ()
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ChangeError("invalid_request", field="body")
    unknown = sorted(str(k)[:64] for k in body if k not in {*required, *optional})
    if unknown:
        raise ChangeError("unknown_field", fields=unknown[:10])
    missing = sorted(k for k in required if k not in body)
    if missing:
        raise ChangeError("missing_field", fields=missing)
    return body


def _key_set(body: Mapping[str, Any], name: str) -> frozenset[str]:
    value = body.get(name, [])
    if (
        not isinstance(value, list)
        or len(value) > MAX_KEYS
        or not all(isinstance(k, str) and _KEY_RE.match(k) for k in value)
    ):
        raise ChangeError("invalid_request", field=name)
    return frozenset(value)


def _flag(body: Mapping[str, Any], name: str, default: bool = False) -> bool:
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise ChangeError("invalid_request", field=name)
    return value


def _field_error(field_name: str, kind: str) -> ChangeError:
    return ChangeError(
        "invalid_fields", 422, fields=[{"field": field_name, "type": kind, "msg": kind}]
    )


def _validation_error(exc: ValidationError) -> ChangeError:
    """Field-level findings of a model: location, type and our own English message (the models
    never put input values into their messages)."""
    fields = [
        {
            "field": ".".join(str(p) for p in err["loc"]),
            "type": err["type"],
            "msg": err["msg"].removeprefix("Value error, ")[:200],
        }
        for err in exc.errors(include_input=False, include_url=False, include_context=False)
    ]
    return ChangeError("invalid_fields", 422, fields=fields[:20])


def _inverter_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ChangeError("invalid_request", field="inverter")
    return value


# -- views ------------------------------------------------------------------------------------


def entry_view(e: Entry) -> dict[str, Any]:
    return {
        "key": e.key,
        "name": {"de": e.name.de, "en": e.name.en},
        "description": (
            {"de": e.description.de, "en": e.description.en} if e.description else None
        ),
        # Text key for the UI: a decoded value whose meaning depends on the build, or a raw word.
        "hint": e.release if e.release in ("profile_dependent", "unresolved") else None,
        "category": e.category,
        "point": e.point,
        "group": e.group,
        "tags": list(e.tags),
        "support_status": e.support_status,
        "release": e.release,
        "requires": e.requires.model_dump(mode="json", by_alias=True, exclude_none=True),
        "unit": e.unit,
        "platform": e.platform,
        "default_enabled": e.default_enabled,
        "raw_diagnostic": e.is_raw_diagnostic,
    }


def catalog_view(profile: Profile) -> dict[str, Any]:
    """What the user may choose: physical sensors and clearly marked raw diagnostics. Placeholders,
    unsupported and internal entries are not offered at all."""
    return {
        "profile": profile.profile,
        "revision": profile.revision,
        "hardware": profile.hardware,
        "entries": [entry_view(e) for e in profile.entries if e.selectable],
    }


def budget_view(plan: ReadPlan, bus_utilisation: float | None = None) -> dict[str, Any]:
    return {
        "blocks": len(plan.blocks),
        "registers": len(plan.addresses),
        "utilisation": plan.budget.utilisation,
        "overloaded": plan.budget.overloaded,
        "bus_utilisation": bus_utilisation,
        "groups": {
            g: {
                "blocks": b.blocks,
                "cycle_s": b.cycle_s,
                "interval_s": b.interval_s,
                "utilisation": b.utilisation,
            }
            for g, b in plan.budget.per_group.items()
        },
    }


def selection_view(
    sel: InverterSelection, plan: ReadPlan, bus_utilisation: float | None = None
) -> dict[str, Any]:
    return {
        "features": sel.features.model_dump(),
        "template": sorted(sel.template),
        "added": sorted(sel.added),
        "removed": sorted(sel.removed),
        "confirmed": sorted(sel.confirmed),
        "chosen": sel.chosen,
        "effective": sorted(sel.effective),
        "deviations": sel.deviations,
        "budget": budget_view(plan, bus_utilisation),
    }


def status_view(inv: InverterRuntime, bus_load: float | None) -> dict[str, Any]:
    """Operational state for the status view: counters only, no frames and no addresses."""
    d = inv.diagnostics()
    hints = []
    for key in sorted(inv.selection):
        obs = inv.observations.get(key)
        if obs is not None and obs.state in _HINT_STATES:
            hints.append(
                {
                    "key": key,
                    "state": obs.state.value,
                    "responses": obs.responses,
                    "zero_share": round(obs.zero_share, 4),
                    "daylight_samples": obs.daylight_samples,
                    "daylight_nonzero": obs.daylight_nonzero,
                }
            )
    suggested = dict(d["suggested_features"])
    basis = suggested.pop("_basis", {})
    return {
        "state": d["state"],
        "reason": d["reason"],
        "active_path": d["active_path"],
        "endpoint": d["endpoint"],
        "serial": inv.seen_serial,
        "bound": d["bound"],
        "binding_source": d["binding_source"],
        "serial_matches": d["serial_matches"],
        "identity": d["identity"],
        "capability_conflicts": d["capability_conflicts"],
        "energy": d["energy"],
        "hints": hints,
        "error_counters": {
            "consecutive_failed_cycles": d["consecutive_failed_cycles"],
            "coalesced_ticks": d["coalesced_ticks"],
            "internal_errors": d["internal_errors"],
            "last_internal_error": d["last_internal_error"],
            "transport": d["transport"],
        },
        "last_success_wall": d["last_success_wall"],
        "bus_utilisation": bus_load,
        "suggested_features": suggested,
        "suggestion_basis": basis,
        "selection_revision": d["selection_revision"],
        "awaiting_selection": d["awaiting_selection"],
    }


# -- application ------------------------------------------------------------------------------


class WebApi:
    def __init__(
        self,
        runtime: Runtime,
        manager: SelectionManager,
        *,
        publisher: MqttPublisher | None = None,
        settings: WebSettings | None = None,
        secret: bytes | None = None,
        plant: PlantService | None = None,
    ) -> None:
        self.runtime = runtime
        self.publisher = publisher
        self.manager = manager
        self.plant = plant
        self._test_lock = asyncio.Lock()
        self.settings = settings or WebSettings()
        self._secret = secret or secrets.token_bytes(32)
        self._peers = frozenset(_normalise_ip(p) for p in self.settings.allowed_peers)
        self._runner: web.AppRunner | None = None
        self.port: int | None = None
        self._actions = 0  # identity / energy / cleanup actions performed by this process

    @property
    def action_counter(self) -> int:
        """Monotone state counter for identity, energy and cleanup actions (``If-Match`` of those
        calls): the selection revision plus every action performed, so a confirmation given for an
        older state, or one that was already used, is rejected (409)."""
        return self.manager.revision + self._actions

    def _action_gate(self, expected: int | None) -> None:
        assert expected is not None
        if expected != self.action_counter:
            raise ChangeError("stale_revision", 409, current=self.action_counter)

    # -- lifecycle --------------------------------------------------------------------------

    def make_app(self) -> web.Application:
        app = web.Application(
            middlewares=[self._peer_check, self._errors], client_max_size=MAX_BODY
        )
        r = app.router
        r.add_get("/", self._index)
        r.add_get("/static/{path:.+}", self._static)
        r.add_get("/api/session", self._session)
        r.add_get("/api/catalog", self._catalog)
        r.add_get("/api/hardware", self._hardware)
        r.add_get("/api/inverters", self._inverters)
        r.add_get("/api/inverters/{inverter}", self._inverter)
        r.add_put("/api/inverters/{inverter}/features", self._set_features)
        r.add_post("/api/inverters/{inverter}/sensors", self._sensors)
        r.add_post("/api/inverters/{inverter}/sensors/preview", self._sensors_preview)
        r.add_post("/api/inverters/{inverter}/template/restore", self._restore)
        r.add_post("/api/inverters/{inverter}/rebind", self._rebind)
        r.add_post("/api/transfer/preview", self._transfer_preview)
        r.add_post("/api/transfer", self._transfer)
        r.add_post("/api/stores/reset", self._reset)
        r.add_post("/api/cleanup/purge", self._purge)
        if self.plant is not None:
            r.add_get("/api/plant", self._plant)
            r.add_get("/api/plant/serial-devices", self._serial_devices)
            r.add_post("/api/plant/endpoints", self._endpoint_create)
            r.add_put("/api/plant/endpoints/{endpoint}", self._endpoint_update)
            r.add_post("/api/plant/endpoints/{endpoint}/delete", self._endpoint_delete)
            r.add_post("/api/plant/inverters", self._inverter_create)
            r.add_put("/api/plant/inverters/{inverter}", self._inverter_update)
            r.add_post("/api/plant/inverters/{inverter}/delete", self._inverter_delete)
            r.add_post("/api/plant/test", self._plant_test)
        return app

    async def start(self) -> int:
        self._runner = web.AppRunner(self.make_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.settings.host, self.settings.port)
        await site.start()
        sockets = self._runner.addresses
        self.port = int(sockets[0][1]) if sockets else self.settings.port
        return self.port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- middleware -------------------------------------------------------------------------

    @web.middleware
    async def _peer_check(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        # The TCP peer decides, never a header: this runs before any X-Remote-User-*,
        # X-Ingress-Path or Forwarded header is looked at.
        peername = request.transport.get_extra_info("peername") if request.transport else None
        host = peername[0] if isinstance(peername, tuple) and peername else None
        if host is None or _normalise_ip(str(host)) not in self._peers:
            return json_response(error_body("forbidden_peer"), 403)
        return await handler(request)

    @web.middleware
    async def _errors(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        try:
            response = await handler(request)
        except ChangeError as exc:
            response = json_response(error_body(exc.key, **exc.params), exc.status)
        except web.HTTPException as exc:
            response = json_response(error_body(f"http_{exc.status}"), exc.status)
            if exc.status == 405:
                response.headers["Allow"] = exc.headers.get("Allow", "")
        except Exception as exc:
            log.error("web request failed (%s)", type(exc).__name__)
            response = json_response(error_body("internal_error"), 500)
        response.headers.update(_SECURITY_HEADERS)
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # -- identity / CSRF --------------------------------------------------------------------

    def _user(self, request: web.Request) -> User | None:
        raw = request.headers.get("X-Remote-User-Id", "").strip()
        if not raw or len(raw) > 128 or not raw.isprintable():
            return None
        return User(
            raw,
            _header(request, "X-Remote-User-Name"),
            _header(request, "X-Remote-User-Display-Name"),
        )

    def _csrf(self, user: User) -> str:
        return hmac.new(self._secret, user.id.encode(), hashlib.sha256).hexdigest()

    async def _mutation(
        self, request: web.Request, *, revision: bool
    ) -> tuple[User, Any, int | None]:
        """Common gate of every changing call: user, CSRF, JSON body and (optionally) revision."""
        user = self._user(request)
        if user is None:
            raise ChangeError("identity_required", 403)
        given = request.headers.get("X-CSRF-Token", "")
        if not given or not hmac.compare_digest(given.encode(), self._csrf(user).encode()):
            raise ChangeError("csrf_invalid", 403)
        if request.content_type != "application/json":
            raise ChangeError("json_required", 415)
        try:
            body = await request.json(loads=json.loads)
        except ValueError:
            raise ChangeError("invalid_json") from None
        expected: int | None = None
        if revision:
            header = request.headers.get("If-Match")
            if header is None:
                raise ChangeError("revision_required", 428)
            match = _REVISION_RE.match(header.strip())
            if match is None:
                raise ChangeError("invalid_request", field="If-Match")
            expected = int(match.group(1))
        return user, body, expected

    def _inverter_of(self, request: web.Request) -> str:
        inverter_id = request.match_info["inverter"]
        if not _ID_RE.match(inverter_id) or inverter_id not in self.manager.specs:
            raise ChangeError("unknown_inverter", 404)
        return inverter_id

    # -- static files -----------------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        return _package_file(["index.html"])

    async def _static(self, request: web.Request) -> web.Response:
        parts = request.match_info["path"].split("/")
        if len(parts) > 4 or not all(_STATIC_PART_RE.match(p) for p in parts):
            raise web.HTTPNotFound
        return _package_file(["static", *parts])

    # -- reading ----------------------------------------------------------------------------

    async def _session(self, request: web.Request) -> web.Response:
        user = self._user(request)
        store = self.manager.store
        return json_response(
            {
                "revision": self.manager.revision,
                "action_counter": self.action_counter,
                "csrf": self._csrf(user) if user else None,
                "user": (
                    {"id": user.id, "name": user.name, "display_name": user.display_name}
                    if user
                    else None
                ),
                "languages": ["de", "en"],
                "inverters": list(self.manager.specs),
                "store": {"status": store.status.value, "source": store.source},
                "problems": [p.as_dict() for p in store.problems]
                + ([p.as_dict() for p in self.plant.store.problems] if self.plant else []),
                "writes_enabled": False,
                "plant_revision": self.plant.revision if self.plant else None,
                "setup_needed": not self.manager.specs,
                "plant_notices": list(self.plant.notices) if self.plant else [],
            }
        )

    async def _catalog(self, request: web.Request) -> web.Response:
        profiles = {s.profile.profile: s.profile for s in self.manager.specs.values()}
        return json_response(
            {"profiles": {n: catalog_view(p) for n, p in sorted(profiles.items())}}
        )

    async def _hardware(self, request: web.Request) -> web.Response:
        hardware = {s.hardware.name: s.hardware for s in self.manager.specs.values()}
        return json_response(
            {"hardware": {n: h.model_dump(mode="json") for n, h in sorted(hardware.items())}}
        )

    def _inverter_view(self, inverter_id: str) -> dict[str, Any]:
        spec = self.manager.specs[inverter_id]
        runtime = self.runtime.inverters[inverter_id]
        bus_load = self.runtime.bus_utilisation().get(self.runtime._bus_of(inverter_id))
        sel = self.manager.selection(inverter_id)
        return {
            "id": inverter_id,
            "name": runtime.cfg.name,
            "profile": spec.profile.profile,
            "hardware": spec.hardware.name,
            "connection": {
                "endpoint": runtime.route.endpoint_id,
                "transport": self.runtime.cfg.endpoint(runtime.route.endpoint_id).transport.value,
                "serial_pinned": runtime.cfg.expected_serial is not None,
            },
            "status": status_view(runtime, bus_load),
            "selection": selection_view(sel, self.manager.plan(inverter_id, sel), bus_load),
        }

    async def _inverters(self, request: web.Request) -> web.Response:
        return json_response(
            {
                "revision": self.manager.revision,
                "plant_revision": self.plant.revision if self.plant else None,
                "now": self.runtime._wall(),  # server clock: the UI shows ages relative to it
                "inverters": [self._inverter_view(i) for i in self.manager.specs],
            }
        )

    async def _inverter(self, request: web.Request) -> web.Response:
        view = self._inverter_view(self._inverter_of(request))
        return json_response({"revision": self.manager.revision, **view})

    # -- selection changes ------------------------------------------------------------------

    async def _apply(self, expected: int, edits: Mapping[str, Edit]) -> web.Response:
        result = await self.manager.change(expected, edits)
        return json_response(
            {
                "revision": result.revision,
                "changed": list(result.changed),
                "selection_changed": list(result.selection_changed),
                "inverters": {i: self._inverter_view(i)["selection"] for i in edits},
            }
        )

    async def _set_features(self, request: web.Request) -> web.Response:
        inverter_id = self._inverter_of(request)
        _, body, expected = await self._mutation(request, revision=True)
        data = _object(body, required=["features"])
        features = data["features"]
        if not isinstance(features, dict) or not features:
            raise ChangeError("invalid_request", field="features")
        assert expected is not None
        return await self._apply(expected, {inverter_id: Edit(features=features)})

    def _sensor_edit(self, body: Any) -> Edit:
        """Sensor edit; ``features`` and ``restore_template`` let the UI preview and save its whole
        draft (plant features plus deviations) in one atomic step."""
        data = _object(
            body,
            optional=[
                "add",
                "remove",
                "reset",
                "features",
                "restore_template",
                "confirm",
                "choose",
            ],
        )
        features = data.get("features")
        if features is not None and (not isinstance(features, dict) or not features):
            raise ChangeError("invalid_request", field="features")
        edit = Edit(
            features=features,
            restore_template=_flag(data, "restore_template"),
            add=_key_set(data, "add"),
            remove=_key_set(data, "remove"),
            reset=_key_set(data, "reset"),
            confirm=_key_set(data, "confirm"),
            choose=_flag(data, "choose"),
        )
        if not (
            edit.add
            or edit.remove
            or edit.reset
            or edit.features
            or edit.restore_template
            or edit.confirm
            or edit.choose
        ):
            raise ChangeError("invalid_request", field="add")
        return edit

    async def _sensors(self, request: web.Request) -> web.Response:
        inverter_id = self._inverter_of(request)
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        return await self._apply(expected, {inverter_id: self._sensor_edit(body)})

    async def _sensors_preview(self, request: web.Request) -> web.Response:
        inverter_id = self._inverter_of(request)
        _, body, _ = await self._mutation(request, revision=False)
        edit = self._sensor_edit(body)
        return json_response(
            {"revision": self.manager.revision, "preview": self._preview(inverter_id, edit)}
        )

    async def _restore(self, request: web.Request) -> web.Response:
        inverter_id = self._inverter_of(request)
        _, body, expected = await self._mutation(request, revision=True)
        _object(body)
        assert expected is not None
        return await self._apply(expected, {inverter_id: Edit(restore_template=True)})

    def _preview(self, inverter_id: str, edit: Edit) -> dict[str, Any]:
        """Dry run of one inverter's edit: the resulting selection, its budget and the capability
        findings of the target. Nothing is stored or applied."""
        record, sel, _ = self.manager.dry_run({inverter_id: edit})[inverter_id]
        del record
        current = self.manager.selection(inverter_id)
        runtime = self.runtime.inverters[inverter_id]
        # the budget comes from the runtime's own route description (active route, effective bus
        # gap), the same one the running plan is built from
        plan = runtime.candidate_plan(sel.effective)
        bus = self.runtime._bus_of(inverter_id)
        load = self.runtime.bus_utilisation({inverter_id: plan})[bus]
        points = [CONFLICT_POINTS.get(c["code"]) for c in runtime.capability_conflicts]
        everything = any(p is None for p in points)  # a conflict that puts every entity in doubt
        flagged = frozenset().union(*(p for p in points if p is not None))
        profile = self.manager.specs[inverter_id].profile
        warned = sorted(
            k
            for k in sel.effective
            if runtime.capability_conflicts and (everything or profile.entry(k).point in flagged)
        )
        return {
            "ok": True,
            "selection": selection_view(sel, plan, load),
            "bus_utilisation_now": self.runtime.bus_utilisation()[bus],
            "adds": sorted(sel.active - current.active),
            "drops": sorted(current.active - sel.active),
            "capability_conflicts": list(runtime.capability_conflicts),
            "capability_warnings": warned,
        }

    # -- transfer ---------------------------------------------------------------------------

    def _transfer_plan(self, body: Any) -> tuple[str, dict[str, Edit]]:
        data = _object(body, required=["source"], optional=["targets", "include_features"])
        source = _inverter_id(data["source"])
        if source not in self.manager.specs:
            raise ChangeError("unknown_inverter", 404)
        raw_targets = data.get("targets")
        if raw_targets is None:
            targets = [i for i in self.manager.specs if i != source]
        else:
            if not isinstance(raw_targets, list) or len(raw_targets) > len(self.manager.specs):
                raise ChangeError("invalid_request", field="targets")
            targets = [_inverter_id(t) for t in raw_targets]
            if source in targets or len(set(targets)) != len(targets):
                raise ChangeError("invalid_request", field="targets")
            for t in targets:
                if t not in self.manager.specs:
                    raise ChangeError("unknown_inverter", 404)
        include_features = _flag(data, "include_features", True)
        src = self.manager.selection(source)
        # Only the plant choice is copied: features and deviations. Never a serial binding, an
        # identity, an energy series or (later) a write permit.
        edits = {
            t: Edit(
                replace=Record(
                    src.features if include_features else self.manager.selection(t).features,
                    src.added,
                    src.removed,
                )
            )
            for t in targets
        }
        return source, edits

    def _transfer_preview_of(self, edits: Mapping[str, Edit]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for target, edit in edits.items():
            try:
                out[target] = self._preview(target, edit)
            except ChangeError as exc:
                out[target] = {"ok": False, "error": {"key": exc.key, "params": exc.params}}
        # Several targets on one bus load it together: the candidate load of a bus is the sum of
        # all proposed plans plus the unchanged inverters.
        plans = {
            t: self.runtime.inverters[t].candidate_plan(edit_sel)
            for t, edit_sel in (
                (t, frozenset(p["selection"]["effective"])) for t, p in out.items() if p["ok"]
            )
        }
        joint = self.runtime.bus_utilisation(plans)
        for target, view in out.items():
            if view["ok"]:
                view["selection"]["budget"]["bus_utilisation"] = joint[self.runtime._bus_of(target)]
        return out

    async def _transfer_preview(self, request: web.Request) -> web.Response:
        _, body, _ = await self._mutation(request, revision=False)
        source, edits = self._transfer_plan(body)
        return json_response(
            {
                "revision": self.manager.revision,
                "source": source,
                "targets": self._transfer_preview_of(edits),
            }
        )

    async def _transfer(self, request: web.Request) -> web.Response:
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        source, edits = self._transfer_plan(body)
        preview = self._transfer_preview_of(edits)
        ok = {t: e for t, e in edits.items() if preview[t]["ok"]}
        skipped = {t: p["error"] for t, p in preview.items() if not p["ok"]}
        if not ok:
            raise ChangeError("nothing_to_transfer", 422, skipped=skipped)
        result = await self.manager.change(expected, ok)
        return json_response(
            {
                "revision": result.revision,
                "source": source,
                "applied": sorted(ok),
                "changed": list(result.changed),
                "skipped": skipped,
            }
        )

    # -- identity / stores ------------------------------------------------------------------

    async def _rebind(self, request: web.Request) -> web.Response:
        inverter_id = self._inverter_of(request)
        _, body, expected = await self._mutation(request, revision=True)
        data = _object(body, required=["confirm"])
        if data["confirm"] is not True:
            raise ChangeError("confirmation_required")
        self._action_gate(expected)
        # A refusal (False) had no effect and keeps the confirmation; an error after the binding was
        # revoked (a failing notification) has: the confirmation is used up either way.
        spent = True
        try:
            if not self.runtime.rebind(inverter_id):
                spent = False
                raise ChangeError("rebind_not_possible", 409)
        finally:
            if spent:
                self._actions += 1
        return json_response(
            {"ok": True, "inverter": inverter_id, "action_counter": self.action_counter}
        )

    async def _reset(self, request: web.Request) -> web.Response:
        _, body, expected = await self._mutation(request, revision=True)
        data = _object(body, required=["kind", "scope", "confirm"], optional=["inverter"])
        kind, scope = data["kind"], data["scope"]
        if kind not in ("identity", "energy") or scope not in ("inverter", "global"):
            raise ChangeError("invalid_request", field="kind")
        target: str | None
        if scope == "global":
            # The explicitly global variant: every binding is gone, every inverter is revoked.
            if kind != "identity" or data.get("inverter") is not None:
                raise ChangeError("invalid_request", field="scope")
            if data["confirm"] != RESET_ALL_CONFIRMATION:
                raise ChangeError("confirmation_required")
            target = None
        else:
            if data["confirm"] is not True:
                raise ChangeError("confirmation_required")
            target = _inverter_id(data.get("inverter"))
            if target not in self.manager.specs:
                raise ChangeError("unknown_inverter", 404)
        self._action_gate(expected)
        try:
            self.runtime.reset_store(target, kind)
        except ValueError:
            raise ChangeError("reset_requires_global", 409) from None
        except OSError:
            raise ChangeError("reset_failed", 500) from None
        finally:
            self._actions += 1  # the stores may have changed even if a notification failed
        return json_response(
            {
                "ok": True,
                "kind": kind,
                "scope": scope,
                "inverter": target,
                "action_counter": self.action_counter,
            }
        )

    async def _purge(self, request: web.Request) -> web.Response:
        """Forget the MQTT cleanup ledger (T8-01): the only way retired Home Assistant entities
        are ever dropped from the retained cleanup. Explicit scope and confirmation text."""
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        data = _object(body, required=["scope", "confirm"], optional=["inverter"])
        scope = data["scope"]
        if scope not in ("inverter", "all") or data["confirm"] != PURGE_CONFIRMATION:
            raise ChangeError("confirmation_required")
        target: str | None = None
        if scope == "inverter":
            target = _inverter_id(data.get("inverter"))
            if target not in self.manager.specs:
                raise ChangeError("unknown_inverter", 404)
        elif data.get("inverter") is not None:
            raise ChangeError("invalid_request", field="scope")
        self._action_gate(expected)
        if self.publisher is None:
            raise ChangeError("publisher_unavailable", 409)
        try:
            dropped = self.publisher.purge_cleanup_ledger(target)
        finally:
            self._actions += 1  # the ledger may have changed even if the call failed
        return json_response(
            {
                "ok": True,
                "scope": scope,
                "inverter": target,
                "dropped": dropped,
                "action_counter": self.action_counter,
            }
        )

    # -- plant (connections and inverters) --------------------------------------------------

    def _plant_service(self) -> PlantService:
        assert self.plant is not None
        return self.plant

    def _plant_view(self) -> dict[str, Any]:
        service = self._plant_service()
        plant = service.plant
        users: dict[str, list[dict[str, str]]] = {e.id: [] for e in plant.endpoints}
        for inv in plant.inverters:
            users.get(inv.endpoint, []).append({"inverter": inv.id, "role": "primary"})
            if inv.fallback_endpoint:
                users.get(inv.fallback_endpoint, []).append(
                    {"inverter": inv.id, "role": "fallback"}
                )
        endpoints = []
        for e in plant.endpoints:
            data = e.model_dump(mode="json", exclude_none=True, exclude={"host_display"})
            if e.host is not None:
                data["host"] = e.host_display or e.host
            endpoints.append({**data, "used_by": users[e.id]})
        inverters = [
            # neither is set in the web interface (the profile is fixed, writing does not exist)
            i.model_dump(mode="json", exclude_none=True, exclude={"allow_writes", "profile"})
            for i in plant.inverters
        ]
        return {
            "revision": service.revision,
            "endpoints": endpoints,
            "inverters": inverters,
            "notices": list(service.notices),
            "limits": {"endpoints": MAX_ENDPOINTS, "inverters": MAX_INVERTERS},
            "writes": "unavailable",
        }

    async def _plant(self, request: web.Request) -> web.Response:
        return json_response(self._plant_view())

    async def _serial_devices(self, request: web.Request) -> web.Response:
        """Names below the serial directory, listed only: nothing is opened or probed."""
        directory = self.settings.serial_dir
        names: list[str] = []
        try:
            with os.scandir(directory) as entries:
                names = sorted(e.name for e in entries if _SERIAL_NAME_RE.match(e.name))
        except OSError:
            names = []
        return json_response(
            {"directory": directory, "devices": [f"{directory}/{n}" for n in names[:64]]}
        )

    def _endpoint_input(
        self, body: Any, *, endpoint_id: str | None, known: str | None
    ) -> EndpointConfig:
        data = _object(body, required=["transport"], optional=[*_ENDPOINT_FIELDS])
        if endpoint_id is not None:
            if data.get("id", endpoint_id) != endpoint_id:
                raise ChangeError("id_immutable", 422)
            data = {**data, "id": endpoint_id}
        elif "id" not in data:
            raise ChangeError("missing_field", fields=["id"])
        if data.get("transport") == Transport.SERIAL_RTU.value:
            device = data.get("serial_device")
            if isinstance(device, str) and device != known:
                prefix = self.settings.serial_dir.rstrip("/") + "/"
                if not device.startswith(prefix) or not _SERIAL_NAME_RE.match(
                    device[len(prefix) :]
                ):
                    raise _field_error("serial_device", "serial_device_not_allowed")
        try:
            return EndpointConfig.model_validate(data)
        except ValidationError as exc:
            raise _validation_error(exc) from None

    def _inverter_input(self, body: Any, *, inverter_id: str | None) -> InverterConfig:
        data = _object(body, required=["name", "endpoint", "unit_id"], optional=[*_INVERTER_FIELDS])
        if inverter_id is not None:
            if data.get("id", inverter_id) != inverter_id:
                raise ChangeError("id_immutable", 422)
            data = {**data, "id": inverter_id}
        elif "id" not in data:
            raise ChangeError("missing_field", fields=["id"])
        try:
            return InverterConfig.model_validate(data)
        except ValidationError as exc:
            raise _validation_error(exc) from None

    def _plant_checked(
        self, endpoints: list[EndpointConfig], inverters: list[InverterConfig]
    ) -> PlantConfig:
        if len(endpoints) > MAX_ENDPOINTS or len(inverters) > MAX_INVERTERS:
            raise ChangeError("too_many", 422)
        problems = plant_problems(endpoints, inverters)
        if problems:
            raise ChangeError(
                "invalid_plant",
                422,
                problems=[{"code": p.code, "params": p.params} for p in problems[:10]],
            )
        return PlantConfig(endpoints=tuple(endpoints), inverters=tuple(inverters))

    async def _plant_change(
        self,
        expected: int,
        build: Callable[[PlantConfig], PlantConfig],
        *,
        forget: frozenset[str] = frozenset(),
    ) -> web.Response:
        service = self._plant_service()
        result = await service.change(expected, build, forget_bindings=forget)
        if result.changed:
            self._actions += 1  # confirmations given for the old plant are void
        return json_response(
            {
                "revision": result.revision,
                "changed": result.changed,
                "removed_inverters": list(result.removed_inverters),
                "notes": result.notes,
                "plant": self._plant_view(),
            }
        )

    def _endpoint_of(self, request: web.Request) -> str:
        endpoint_id = request.match_info["endpoint"]
        if not _ID_RE.match(endpoint_id) or all(
            e.id != endpoint_id for e in self._plant_service().plant.endpoints
        ):
            raise ChangeError("unknown_endpoint", 404)
        return endpoint_id

    async def _endpoint_create(self, request: web.Request) -> web.Response:
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        endpoint = self._endpoint_input(body, endpoint_id=None, known=None)

        def build(plant: PlantConfig) -> PlantConfig:
            if any(e.id == endpoint.id for e in plant.endpoints):
                raise ChangeError("id_taken", 409)
            return self._plant_checked([*plant.endpoints, endpoint], list(plant.inverters))

        return await self._plant_change(expected, build)

    async def _endpoint_update(self, request: web.Request) -> web.Response:
        endpoint_id = self._endpoint_of(request)
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        current = self._plant_service().plant.endpoint(endpoint_id)
        endpoint = self._endpoint_input(body, endpoint_id=endpoint_id, known=current.serial_device)

        def build(plant: PlantConfig) -> PlantConfig:
            return self._plant_checked(
                [endpoint if e.id == endpoint_id else e for e in plant.endpoints],
                list(plant.inverters),
            )

        return await self._plant_change(expected, build)

    async def _endpoint_delete(self, request: web.Request) -> web.Response:
        endpoint_id = self._endpoint_of(request)
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        data = _object(body, required=["confirm"])
        if data["confirm"] is not True:
            raise ChangeError("confirmation_required")

        def build(plant: PlantConfig) -> PlantConfig:
            using = [
                i.id for i in plant.inverters if endpoint_id in (i.endpoint, i.fallback_endpoint)
            ]
            if using:
                raise ChangeError("endpoint_in_use", 409, inverters=using)
            return self._plant_checked(
                [e for e in plant.endpoints if e.id != endpoint_id], list(plant.inverters)
            )

        return await self._plant_change(expected, build)

    async def _inverter_create(self, request: web.Request) -> web.Response:
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        inverter = self._inverter_input(body, inverter_id=None)

        def build(plant: PlantConfig) -> PlantConfig:
            if any(i.id == inverter.id for i in plant.inverters):
                raise ChangeError("id_taken", 409)
            return self._plant_checked(list(plant.endpoints), [*plant.inverters, inverter])

        return await self._plant_change(expected, build)

    async def _inverter_update(self, request: web.Request) -> web.Response:
        inverter_id = self._plant_inverter_of(request)
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        inverter = self._inverter_input(body, inverter_id=inverter_id)

        def build(plant: PlantConfig) -> PlantConfig:
            return self._plant_checked(
                list(plant.endpoints),
                [inverter if i.id == inverter_id else i for i in plant.inverters],
            )

        return await self._plant_change(expected, build)

    def _plant_inverter_of(self, request: web.Request) -> str:
        inverter_id = request.match_info["inverter"]
        if not _ID_RE.match(inverter_id) or all(
            i.id != inverter_id for i in self._plant_service().plant.inverters
        ):
            raise ChangeError("unknown_inverter", 404)
        return inverter_id

    async def _inverter_delete(self, request: web.Request) -> web.Response:
        inverter_id = self._plant_inverter_of(request)
        _, body, expected = await self._mutation(request, revision=True)
        assert expected is not None
        data = _object(body, required=["confirm"], optional=["remove_binding"])
        if data["confirm"] is not True:
            raise ChangeError("confirmation_required")
        forget = _flag(data, "remove_binding")

        def build(plant: PlantConfig) -> PlantConfig:
            return self._plant_checked(
                list(plant.endpoints), [i for i in plant.inverters if i.id != inverter_id]
            )

        return await self._plant_change(
            expected, build, forget=frozenset({inverter_id}) if forget else frozenset()
        )

    async def _plant_test(self, request: web.Request) -> web.Response:
        """Read-only identity read of one connection (``probe``); the form values need not be
        saved. Changes nothing, so it needs no revision, but the same user and CSRF gate."""
        _, body, _ = await self._mutation(request, revision=False)
        data = _object(
            body,
            required=["unit_id"],
            optional=["endpoint", "endpoint_id", "inverter", "expected_serial"],
        )
        if ("endpoint" in data) == ("endpoint_id" in data):
            raise ChangeError("invalid_request", field="endpoint")
        plant = self._plant_service().plant
        if "endpoint_id" in data:
            ep_id = data["endpoint_id"]
            if not isinstance(ep_id, str) or not _ID_RE.match(ep_id):
                raise ChangeError("invalid_request", field="endpoint_id")
            try:
                endpoint = plant.endpoint(ep_id)
            except KeyError:
                raise ChangeError("unknown_endpoint", 404) from None
        else:
            spec = data["endpoint"]
            known = None
            if isinstance(spec, dict) and isinstance(spec.get("id"), str):
                known = next((e.serial_device for e in plant.endpoints if e.id == spec["id"]), None)
            endpoint = self._endpoint_input(
                {"id": "connection_test", **spec} if isinstance(spec, dict) else spec,
                endpoint_id=None,
                known=known,
            )
        unit = data["unit_id"]
        if type(unit) is not int or not 1 <= unit <= 247:
            raise ChangeError("invalid_request", field="unit_id")
        inverter_id = data.get("inverter")
        if inverter_id is not None:
            inverter_id = _inverter_id(inverter_id)
        expected_serial = data.get("expected_serial")
        if expected_serial is not None:
            try:
                InverterConfig.model_validate(
                    {
                        "id": "x",
                        "name": "x",
                        "endpoint": "x",
                        "unit_id": 1,
                        "expected_serial": expected_serial,
                    }
                )
            except ValidationError:
                raise _field_error("expected_serial", "invalid_serial") from None
        if self._test_lock.locked():
            raise ChangeError("test_running", 409)
        async with self._test_lock:
            runtime = self.runtime
            try:
                result = await probe.run_test(
                    runtime,
                    endpoint,
                    unit,
                    bound_serial=lambda: runtime.book.bound(inverter_id) if inverter_id else None,
                    expected_serial=expected_serial,
                )
            except probe.EndpointInUse:
                raise ChangeError("endpoint_in_use_by_other_transport", 409) from None
        return json_response(result)


def _header(request: web.Request, name: str) -> str | None:
    value = request.headers.get(name, "").strip()
    return value[:128] if value and value.isprintable() else None


def _normalise_ip(value: str) -> str:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return value
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def _package_file(parts: list[str]) -> web.Response:
    """A file of the packaged frontend (``deye_monitor/web``). Names are validated by the caller;
    nothing outside the package is ever read."""
    node: Any = resources.files("deye_monitor.web")
    for part in parts:
        node = node / part
    try:
        data = node.read_bytes()
    except FileNotFoundError, IsADirectoryError, NotADirectoryError:
        raise web.HTTPNotFound from None
    ctype = mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
    response = web.Response(body=data, content_type=ctype.split(";")[0])
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        response.charset = "utf-8"
    return response
