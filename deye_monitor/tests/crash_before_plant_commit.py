"""Own subprocess: exit at the actual plant commit boundary. No device loop gets to run."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from deye_monitor.app import Application
from deye_monitor.config import AppConfig, PlantConfig
from deye_monitor.device import RelaySink
from deye_monitor.plant import PlantService, PlantStore
from deye_monitor.selection import SelectionManager, SelectionStore, specs_from_config

directory = Path(sys.argv[1])
os.environ["DEYE_MONITOR_DATA"] = str(directory)
store = PlantStore(directory)
cfg = AppConfig().with_plant(store.plant)
manager = SelectionManager(SelectionStore(directory, specs_from_config(cfg), require_choice=True))
app = Application(cfg, "reviewcut", cfg.mqtt, manager, RelaySink())
app.install(cfg)  # builds only; the previous generation is not polled in this subprocess
service = PlantService(store, cfg, manager, app)


def cut(*args: Any) -> None:
    os._exit(79)


store.commit = cut  # type: ignore[method-assign,assignment]


def change(plant: PlantConfig) -> PlantConfig:
    inv = plant.inverters[0].model_copy(update={"expected_serial": "2409876543"})
    return plant.model_copy(update={"inverters": (inv,)})


asyncio.run(service.change(store.revision, change))
