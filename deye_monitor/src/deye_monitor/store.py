"""Small atomic JSON stores for persisted runtime state (identity, capabilities, energy).

A missing file is a first installation (``MISSING``); a present file that cannot be read, has an
unknown schema or an invalid body is ``CORRUPT`` and is never silently replaced. Writes are
atomic for the process (temporary file, fsync, rename, fsync of the directory) and the in-memory
document only changes after the write succeeded. No power-loss guarantee is claimed.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Validator = Callable[[dict[str, Any]], bool]


class LoadStatus(StrEnum):
    MISSING = "missing"  # first installation
    VALID = "valid"
    CORRUPT = "corrupt"  # unreadable, invalid JSON, unknown schema or invalid content


class JsonStore:
    """One JSON object per file, versioned. ``data`` is read-only for callers: change it with
    ``commit`` (which persists first)."""

    def __init__(
        self, path: Path, *, schema_version: int = 1, validate: Validator | None = None
    ) -> None:
        self.path = path
        self.schema_version = schema_version
        self._validate = validate
        self.data: dict[str, Any] = {}
        self.status = LoadStatus.MISSING
        self.problem: str | None = None
        self._load()

    @property
    def corrupt(self) -> bool:
        return self.status is LoadStatus.CORRUPT

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError, UnicodeDecodeError:
            self._fail("unreadable")
            return
        try:
            raw = json.loads(text)
        except ValueError:
            self._fail("invalid JSON")
            return
        if not isinstance(raw, dict) or raw.get("schema_version") != self.schema_version:
            self._fail("unknown schema")
            return
        body = raw.get("data")
        if not isinstance(body, dict) or (self._validate is not None and not self._validate(body)):
            self._fail("invalid content")
            return
        self.data = body
        self.status = LoadStatus.VALID

    def _fail(self, problem: str) -> None:
        self.status = LoadStatus.CORRUPT
        self.problem = problem
        self.data = {}
        log.error("state file %s is not usable (%s)", self.path.name, problem)

    def commit(self, new: dict[str, Any]) -> None:
        """Persist ``new`` atomically; on success it becomes ``data``. Raises ``OSError``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"schema_version": self.schema_version, "data": new}, indent=1)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        _fsync_directory(self.path.parent)
        self.data = copy.deepcopy(new)
        self.status = LoadStatus.VALID
        self.problem = None

    def save(self) -> None:
        """Write the current document again (no change)."""
        self.commit(copy.deepcopy(self.data))

    def reset(self, new: dict[str, Any] | None = None) -> None:
        """Explicit user reset: keep a corrupt file as ``<name>.corrupt`` and start a new one."""
        if self.corrupt and self.path.exists():
            backup = self.path.with_name(self.path.name + ".corrupt")
            try:
                os.replace(self.path, backup)
            except OSError:
                log.warning("could not keep a backup of %s", self.path.name)
        self.commit(new if new is not None else {})


def _fsync_directory(directory: Path) -> None:
    """Make the rename durable where the platform allows it (best effort)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
