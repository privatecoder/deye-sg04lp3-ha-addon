"""Plant-total inputs in the sensor list: the chip, the inline warning and the "only 0" hint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, expect

from deye_monitor.observation import Observation
from tests.test_ui import browser, open_setup, open_ui  # noqa: F401
from tests.ui_helpers import ui_stack

pytestmark = pytest.mark.ui

VIEW = {
    "active": True,
    "series": "g0",
    "master": "deye1",
    "values": {},
    "members": [
        {"id": "deye1", "name": "Deye 1", "online": True, "stale": False, "role": "master"},
        {"id": "deye2", "name": "Deye 2", "online": True, "stale": False, "role": "slave"},
    ],
}


class StubPublisher:
    def __init__(self, view: dict[str, Any]) -> None:
        self.view = view

    def totals_view(self) -> dict[str, Any]:
        return self.view


def row(page: Any, key: str) -> Any:
    return page.locator(f'.sensor[data-key="{key}"]')


async def test_plant_total_inputs_are_marked_and_a_deselection_is_explained(
    browser: Browser,  # noqa: F811
    tmp_path: Path,
) -> None:
    async with ui_stack(tmp_path) as stack:
        stack.api.publisher = StubPublisher(VIEW)  # type: ignore[assignment]
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        await expect(row(page, "load_power_total").locator(".chip.plant-total")).to_have_text(
            "Anlagensumme"
        )
        await expect(row(page, "load_power_total").locator(".plant-tip")).to_contain_text(
            "Wird für die Anlagensumme verwendet"
        )
        await expect(row(page, "bms_soc").locator(".chip.plant-total")).to_be_visible()
        await expect(row(page, "serial_number").locator(".chip.plant-total")).to_have_count(0)
        await expect(row(page, "load_power_total").locator(".plant-warn")).to_be_hidden()
        await row(page, "load_power_total").locator("input").uncheck()
        await expect(row(page, "load_power_total").locator(".plant-warn")).to_contain_text(
            "unbekannt"
        )
        # the SOC has two sources: the warning comes only when neither is selected
        await row(page, "bms_soc").locator("input").uncheck()
        await expect(row(page, "bms_soc").locator(".plant-warn")).to_contain_text("Batterie-SOC")
        await row(page, "battery_soc").locator("input").check()
        await expect(row(page, "bms_soc").locator(".plant-warn")).to_be_hidden()
        await s.check()


async def test_no_marks_without_plant_totals(browser: Browser, tmp_path: Path) -> None:  # noqa: F811
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        await expect(s.page.locator(".chip.plant-total")).to_have_count(0)
        await s.check()


async def test_only_zero_hint_is_not_shown_for_plant_total_inputs(
    browser: Browser,  # noqa: F811
    tmp_path: Path,
) -> None:
    async with ui_stack(tmp_path) as stack:
        stack.api.publisher = StubPublisher(VIEW)  # type: ignore[assignment]
        inv = stack.plant.rig.inv("deye1")
        for key in ("load_power_total", "grid_voltage_a"):
            obs = Observation(watch_zero=True)
            for _ in range(40):
                obs.record(0)
            inv.observations[key] = obs
        s = await open_ui(browser, stack)
        card = s.page.locator('article.inverter[data-id="deye1"], article.inverter').first
        await expect(card).to_contain_text("Netzspannung")  # still reported for a plain sensor
        await expect(card).not_to_contain_text("Lastleistung gesamt")
        await s.check()
