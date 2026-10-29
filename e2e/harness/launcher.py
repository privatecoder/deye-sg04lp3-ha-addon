"""Starts the app exactly like ``python -m deye_monitor`` but lets the ingress API accept the
loopback peer. Test harness only: the product code and its Supervisor-peer check are untouched
(the image run uses the real peer address through a proxy container instead)."""

from __future__ import annotations

import os
import sys

import deye_monitor.app as app
import deye_monitor.webapi as webapi


class LoopbackWebApi(webapi.WebApi):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs["settings"] = webapi.WebSettings(
            "127.0.0.1", int(os.environ["E2E_WEB_PORT"]), frozenset({"127.0.0.1"})
        )
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]


app.WebApi = LoopbackWebApi  # type: ignore[misc]


# -- synchronisation points (test harness only) --------------------------------------------------
# A test arms one named barrier by writing its name into ``$E2E_BARRIER_FILE``. When the app reaches
# that point (right AFTER the named store commit) it writes ``<file>.reached`` and blocks the whole
# event loop, so the test knows the exact persistence phase and can kill the process there.
#
#   selection_durable        a selection slot was committed
#   ledger_written           discovery.json (the cleanup inventory) was committed
#   energy_generation_persisted   energy.json received a record of generation >= 1
import deye_monitor.store as _store  # noqa: E402

_real_commit = _store.JsonStore.commit


def _barrier_name() -> str | None:
    path = os.environ.get("E2E_BARRIER_FILE")
    if not path or not os.path.exists(path):
        return None
    return open(path).read().strip() or None


def _hit(name: str, path: str) -> None:
    flag = os.environ["E2E_BARRIER_FILE"] + ".reached"
    open(flag, "w").write(name)
    import time

    while True:  # the test kills this process
        time.sleep(3600)


def _commit(self: _store.JsonStore, new: dict) -> None:  # type: ignore[type-arg]
    _real_commit(self, new)
    armed = _barrier_name()
    if armed is None:
        return
    fname = self.path.name
    if armed == "selection_durable" and fname.startswith("selection.") and fname.endswith(".json"):
        _hit(armed, fname)
    elif armed == "ledger_written" and fname == "discovery.json":
        _hit(armed, fname)
    elif armed == "energy_generation_persisted" and fname == "energy.json":
        if any(isinstance(r, dict) and r.get("generation", 0) >= 1 for r in new.values()):
            _hit(armed, fname)


_store.JsonStore.commit = _commit  # type: ignore[method-assign]


def _dump_tasks(*_: object) -> None:
    """SIGUSR1: print every asyncio task with its stack (diagnosing a hung shutdown)."""
    import asyncio

    try:
        tasks = asyncio.all_tasks(asyncio.get_event_loop_policy().get_event_loop())
    except RuntimeError:
        return
    for task in tasks:
        task.print_stack(file=sys.stdout)
    import faulthandler

    faulthandler.dump_traceback(file=sys.stdout, all_threads=True)
    sys.stdout.flush()


import signal  # noqa: E402

signal.signal(signal.SIGUSR1, _dump_tasks)

if __name__ == "__main__":
    sys.exit(app.main())
