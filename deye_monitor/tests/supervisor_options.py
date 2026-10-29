"""Test-only port of the Home Assistant Supervisor's app option validation.

Source: https://github.com/home-assistant/supervisor, ``supervisor/apps/options.py``
(``AppOptions``) and ``supervisor/validate.py`` (``network_port``), as shipped in Supervisor
2026.10.5 (image ``ghcr.io/home-assistant/aarch64-hassio-supervisor``, digest
``sha256:0f606324063cd5e7e591fb71efb5214ff56a05514898e33e461757950abc2914``).
Licence: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0).

Modifications (the rules of the supported types are kept as they are): the CoreSys plumbing,
``!secret`` resolution, device types and the pwned-password bookkeeping are removed; unknown
options are collected in ``ignored`` instead of being logged. The Supervisor drops unknown
options silently and requires every schema key that is not a ``?`` string to be present: a nested
mapping is therefore *required* even if all its leaves are optional.

Used to prove that a configuration passes the real validation rules before ``parse_config``.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

_RE_SCHEMA_ELEMENT = re.compile(
    r"^(?:"
    r"|bool"
    r"|email"
    r"|url"
    r"|port"
    r"|str(?:\((?P<s_min>\d+)?,(?P<s_max>\d+)?\))?"
    r"|password(?:\((?P<p_min>\d+)?,(?P<p_max>\d+)?\))?"
    r"|int(?:\((?P<i_min>-?\d+)?,(?P<i_max>-?\d+)?\))?"
    r"|float(?:\((?P<f_min>-?\d*\.?\d+)?,(?P<f_max>-?\d*\.?\d+)?\))?"
    r"|match\((?P<match>.*)\)"
    r"|list\((?P<list>.+)\)"
    r")\??$"
)
_LENGTH_PARTS = ("i_min", "i_max", "f_min", "f_max", "s_min", "s_max", "p_min", "p_max")
_network_port = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


class SupervisorOptions:
    def __init__(self, raw_schema: dict[str, Any], name: str = "app", slug: str = "app") -> None:
        self.raw_schema = raw_schema
        self._name = name
        self._slug = slug
        self.ignored: list[str] = []

    def validate(self, struct: dict[str, Any]) -> dict[str, Any]:
        """Return the validated (coerced) options or raise ``vol.Invalid``."""
        options: dict[str, Any] = {}
        for key, value in struct.items():
            if key not in self.raw_schema:
                self.ignored.append(key)
                continue
            try:
                options[key] = self._element(self.raw_schema[key], value, key)
            except IndexError, KeyError:
                raise vol.Invalid(
                    f"Type error for option '{key}' in {self._name} ({self._slug})"
                ) from None
        self._check_missing(self.raw_schema, options, "root")
        return options

    def _element(self, typ: Any, value: Any, key: str) -> Any:
        if isinstance(typ, list):
            return self._nested_list(typ[0], value, key)
        if isinstance(typ, dict):
            return self._nested_dict(typ, value, key)
        return self._single(typ, value, key)

    def _single(self, typ: str, value: Any, key: str) -> Any:
        if value is None:
            raise vol.Invalid(f"Missing required option '{key}' in {self._name} ({self._slug})")
        return self._typed(typ, value)

    def _typed(self, typ: str, value: Any) -> Any:
        match = _RE_SCHEMA_ELEMENT.match(typ)
        if not match:
            raise vol.Invalid(f"Unknown type '{typ}' in {self._name} ({self._slug})")
        range_args: dict[str, Any] = {}
        for group in _LENGTH_PARTS:
            found = match.group(group)
            if found:
                range_args[group[2:]] = float(found)
        if typ.startswith(("str", "password")):
            return vol.All(str, vol.Length(**range_args))(value)
        if typ.startswith("int"):
            return vol.All(vol.Coerce(int), vol.Range(**range_args))(value)
        if typ.startswith("float"):
            return vol.All(vol.Coerce(float), vol.Range(**range_args))(value)
        if typ.startswith("bool"):
            return vol.Boolean()(value)
        if typ.startswith("email"):
            return vol.Email()(value)
        if typ.startswith("url"):
            return vol.Url()(value)
        if typ.startswith("port"):
            return _network_port(value)
        if typ.startswith("match"):
            return vol.Match(match.group("match"))(str(value))
        if typ.startswith("list"):
            return vol.In(match.group("list").split("|"))(str(value))
        raise vol.Invalid(f"Fatal error with type '{typ}' in {self._name} ({self._slug})")

    def _nested_list(self, typ: Any, data: Any, key: str) -> list[Any]:
        if not isinstance(data, list):
            raise vol.Invalid(f"Invalid list for option '{key}' in {self._name} ({self._slug})")
        return [self._element(typ, element, key) for element in data]

    def _nested_dict(self, typ: dict[Any, Any], data: Any, key: str) -> dict[Any, Any]:
        if not isinstance(data, dict):
            raise vol.Invalid(f"Invalid dict for option '{key}' in {self._name} ({self._slug})")
        options: dict[Any, Any] = {}
        for c_key, c_value in data.items():
            if c_key not in typ:
                self.ignored.append(f"{key}.{c_key}")
                continue
            options[c_key] = self._element(typ[c_key], c_value, c_key)
        self._check_missing(typ, options, key)
        return options

    def _check_missing(self, origin: dict[Any, Any], exists: dict[Any, Any], root: str) -> None:
        for miss in set(origin) - set(exists):
            schema = origin[miss]
            if isinstance(schema, list) and len(schema) > 0:
                schema = schema[0]  # a list is optional if its item type is ("str?")
            if isinstance(schema, str) and schema.endswith("?"):
                continue
            raise vol.Invalid(f"Missing option '{miss}' in {root} in {self._name} ({self._slug})")
