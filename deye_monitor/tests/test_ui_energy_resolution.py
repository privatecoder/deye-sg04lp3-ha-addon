"""Browser tests (``-m ui``) of the energy-resolution diagnostics (0.5.1): the information line
on the sensor page and the plausibility hint, whose button changes the override only on a click."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, expect

from deye_monitor.device import InverterState
from tests.plant_helpers import A, PlantRig, plant_ui
from tests.test_ui import browser  # noqa: F401
from tests.test_ui_plant import open_page

pytestmark = pytest.mark.ui


async def running_inverter(rig: PlantRig, *, override: str | None, r65: int = 0xFFFF) -> None:
    await rig.add_server("gw1", "modbus_tcp", A, r65=r65)
    status, body = await rig.call("POST", "/api/plant/endpoints", rig.endpoint_body("gw1"))
    assert status == 200, body
    inverter: dict[str, Any] = {"id": "deye1", "name": "Deye 1", "endpoint": "gw1", "unit_id": 1}
    if override is not None:
        inverter["energy_resolution"] = override
    status, body = await rig.call("POST", "/api/plant/inverters", inverter)
    assert status == 200, body
    status, body = await rig.call(
        "POST", "/api/inverters/deye1/sensors", {"choose": True}, revision=rig.manager.revision
    )
    assert status == 200, body
    await rig.wait_state("deye1", InverterState.RUNNING)


def judge(
    rig: PlantRig, monkeypatch: pytest.MonkeyPatch, state: str, ratio: float | None = None
) -> None:
    """The accumulator is covered by ``test_energy_check``; here the status carries its verdict."""
    assert rig.app.runtime is not None
    plausibility = rig.app.runtime.inverters["deye1"].plausibility
    verdict = {"state": state, "ratio": ratio, "integrated_kwh": 5.0, "counter_kwh": 50.0}
    monkeypatch.setattr(plausibility, "view", lambda now=None: verdict)


def override_of(rig: PlantRig) -> str:
    return rig.service.plant.inverters[0].energy_resolution.value


@pytest.mark.parametrize(
    ("override", "line"),
    [
        (None, "0.1 kWh pro Schritt, Quelle: Profilstandard 0,1 kWh."),
        ("1", "1 kWh pro Schritt, Quelle: deine Wahl (Override)."),
    ],
)
async def test_the_sensor_page_shows_resolution_and_source(
    browser: Browser,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str | None,
    line: str,
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await running_inverter(rig, override=override)
        s = await open_page(browser, url)
        await expect(s.page.locator("article.inverter").first).to_be_visible()
        # a diagnostic of the sensor page, not another line on the overview card
        await expect(s.page.locator("article.inverter").first).not_to_contain_text(
            "Energieauflösung:"
        )
        await s.page.goto(s.page.url.split("#")[0] + "#/inv/deye1")
        info = s.page.locator(".hint.info").filter(has_text="Energieauflösung:")
        await expect(info).to_have_count(1)
        await expect(info).to_contain_text(line)


async def test_a_too_high_counter_offers_the_other_factor_and_changes_nothing_until_clicked(
    browser: Browser,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await running_inverter(rig, override="1")
        judge(rig, monkeypatch, "counter_too_high", 10.0)
        s = await open_page(browser, url)
        card = s.page.locator("article.inverter").first
        hint = card.locator(".hint.energy").filter(has_text="Der Tageszähler")
        await expect(hint).to_contain_text("etwa 10-mal so hoch")
        await expect(hint).to_contain_text("es wird nichts automatisch geändert")
        await expect(hint.get_by_role("button", name="0,1 kWh wählen")).to_be_visible()
        await expect(hint.get_by_role("button", name="1 kWh wählen", exact=True)).to_have_count(0)
        await asyncio.sleep(1.5)  # nothing happens by itself
        assert override_of(rig) == "1"
        await hint.get_by_role("button", name="0,1 kWh wählen").click()
        await expect(s.page.locator(".toast.ok")).to_be_visible()
        assert override_of(rig) == "0.1"


async def test_a_too_low_counter_offers_one_kwh_and_nothing_else(
    browser: Browser,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await running_inverter(rig, override="0.1")
        judge(rig, monkeypatch, "counter_too_low", 0.1)
        s = await open_page(browser, url)
        hint = s.page.locator("article.inverter .hint.energy").filter(has_text="Der Tageszähler")
        await expect(hint).to_contain_text("nur etwa 0.1-mal so hoch")
        await expect(hint.get_by_role("button", name="1 kWh wählen", exact=True)).to_be_visible()
        await expect(hint.get_by_role("button", name="0,1 kWh wählen")).to_have_count(0)
        await asyncio.sleep(1.5)
        assert override_of(rig) == "0.1"


@pytest.mark.parametrize("state", ["unknown", "plausible"])
async def test_no_hint_without_a_verdict_of_a_wrong_factor(
    browser: Browser,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await running_inverter(rig, override="1")
        judge(rig, monkeypatch, state, 1.0 if state == "plausible" else None)
        s = await open_page(browser, url)
        await expect(s.page.locator("article.inverter").first).to_be_visible()
        await expect(s.page.get_by_text("Der Tageszähler")).to_have_count(0)
        assert override_of(rig) == "1"
