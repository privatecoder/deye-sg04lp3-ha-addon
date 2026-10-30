"""Real process death after generation adoption, immediately before plant persistence."""

import os
import subprocess
import sys
from pathlib import Path

from deye_monitor.config import parse_config
from deye_monitor.identity import IdentityBook, identity_store
from deye_monitor.plant import PlantStore

HERE = Path(__file__).resolve().parent


def test_process_death_cannot_change_identity_of_the_still_accepted_plant(tmp_path: Path) -> None:
    cfg = parse_config(
        {
            "endpoints": [{"id": "gw", "transport": "modbus_tcp", "host": "127.0.0.1", "port": 9}],
            "inverters": [{"id": "deye1", "name": "deye1", "endpoint": "gw", "unit_id": 1}],
        }
    )
    store = PlantStore(tmp_path)
    store.commit(cfg.plant, 1)
    book = IdentityBook(identity_store(tmp_path / "identity.json"), {"deye1": None})
    book.bind_first_seen("deye1", "2401234567")
    proc = subprocess.run(
        [sys.executable, str(HERE / "crash_before_plant_commit.py"), str(tmp_path)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode == 79, proc.stderr
    again = PlantStore(tmp_path)
    binding = IdentityBook(identity_store(tmp_path / "identity.json"), {"deye1": None}).bound(
        "deye1"
    )
    observation = {
        "child_exit": proc.returncode,
        "plant_revision": again.revision,
        "accepted_expected_serial": again.plant.inverters[0].expected_serial,
        "binding_after_restart": binding,
    }
    assert again.revision == 1 and again.plant.inverters[0].expected_serial is None
    assert binding == "2401234567", observation
