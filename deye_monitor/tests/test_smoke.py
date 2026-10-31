import importlib

import pytest

import deye_monitor

MODULES = [
    "config", "scheduler", "device", "mqtt", "diagnostics", "app",
    "protocol.modbus", "protocol.solarman_v5", "protocol.guard",
    "transport", "profiles",
]  # fmt: skip


def test_version() -> None:
    assert deye_monitor.__version__ == "0.2.13"


@pytest.mark.parametrize("name", MODULES)
def test_modules_importable(name: str) -> None:
    importlib.import_module(f"deye_monitor.{name}")


async def test_asyncio_mode_works() -> None:
    assert True
