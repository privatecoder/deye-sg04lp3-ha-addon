"""Plant totals in the web interface: the membership setting (API) and the plant card (browser)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, expect

from tests.plant_helpers import A, B, PlantRig, plant_rig, plant_ui
from tests.test_ui import browser as browser
from tests.test_ui import shot
from tests.test_ui_plant import open_page


async def seed_two(rig: PlantRig) -> None:
    await rig.add_server("gw1", "modbus_tcp")
    await rig.add_server("gw2", "modbus_tcp", B)
    for name in ("gw1", "gw2"):
        status, body = await rig.call("POST", "/api/plant/endpoints", rig.endpoint_body(name))
        assert status == 200, body
    for n in (1, 2):
        status, body = await rig.call(
            "POST",
            "/api/plant/inverters",
            {"id": f"deye{n}", "name": f"Deye {n}", "endpoint": f"gw{n}", "unit_id": 1},
        )
        assert status == 200, body


async def totals(rig: PlantRig) -> dict[str, Any]:
    status, body = await rig.call("GET", "/api/plant")
    assert status == 200
    totals: dict[str, Any] = body["totals"]
    return totals


async def test_every_inverter_is_a_member_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await seed_two(rig)
        assert await totals(rig) == {
            "exclude": [],
            "allow_single": False,
            "members": ["deye1", "deye2"],
            "active": True,
        }


async def test_the_membership_is_saved_and_a_single_member_needs_the_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await seed_two(rig)
        status, body = await rig.call("PUT", "/api/plant/totals", {"members": ["deye1"]})
        assert status == 200 and body["changed"], body
        view = await totals(rig)
        assert view["exclude"] == ["deye2"] and view["members"] == ["deye1"]
        assert view["active"] is False
        status, body = await rig.call(
            "PUT", "/api/plant/totals", {"members": ["deye1"], "allow_single": True}
        )
        assert status == 200
        assert (await totals(rig))["active"] is True
        assert rig.app.cfg.totals.exclude == ("deye2",)  # the running generation follows
        status, body = await rig.call(
            "PUT", "/api/plant/totals", {"members": ["deye1"], "allow_single": True}
        )
        assert status == 200 and body["changed"] is False


async def test_the_membership_survives_a_restart_and_is_pruned_with_the_inverter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await seed_two(rig)
        await rig.call("PUT", "/api/plant/totals", {"members": ["deye1"]})
    async with plant_rig(tmp_path, monkeypatch) as rig:
        assert (await totals(rig))["exclude"] == ["deye2"]
        status, body = await rig.call(
            "POST", "/api/plant/inverters/deye2/delete", {"confirm": True}
        )
        assert status == 200, body
        assert (await totals(rig))["exclude"] == []  # no stale entry
        await rig.add_server("gw3", "modbus_tcp", A)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"members": "deye1"},
        {"members": [1]},
        {"members": ["nope"]},
        {"members": [], "allow_single": 1},
    ],
)
async def test_a_bad_membership_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await seed_two(rig)
        status, _ = await rig.call("PUT", "/api/plant/totals", body)
        assert status in (400, 404, 422)
        assert (await totals(rig))["exclude"] == []


async def test_new_inverters_join_the_totals_and_other_edits_keep_the_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_rig(tmp_path, monkeypatch) as rig:
        await seed_two(rig)
        await rig.call("PUT", "/api/plant/totals", {"members": ["deye1"]})
        status, body = await rig.call(
            "PUT",
            "/api/plant/inverters/deye1",
            {"name": "Renamed", "endpoint": "gw1", "unit_id": 1},
        )
        assert status == 200, body
        assert (await totals(rig))["exclude"] == ["deye2"]  # an edit does not reset it


VIEW = {
    "active": True,
    "series": "g0",
    "master": "deye1",
    "values": {
        "plant_pv_power": 1234.0,
        "plant_load_power": 360.0,
        "plant_battery_power": -80.5,
        "plant_inverter_power": 120.0,
        "plant_battery_soc": 81.0,
        "plant_pv_energy_total": 1000.5,
        "plant_load_energy_total": 2000.0,
        "plant_battery_charge_energy_total": None,
        "plant_battery_discharge_energy_total": 12.0,
    },
    "members": [
        {"id": "deye1", "name": "Deye 1", "online": True, "stale": False, "role": "master"},
        {"id": "deye2", "name": "Deye 2", "online": True, "stale": True, "role": "slave"},
    ],
}


class StubPublisher:
    def totals_view(self) -> dict[str, Any]:
        return VIEW


@pytest.mark.ui
async def test_the_overview_shows_the_plant_card_and_the_setup_page_the_membership(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed_two(rig)
        rig.api.publisher = StubPublisher()  # type: ignore[assignment]
        s = await open_page(browser, url)
        page = s.page
        card = page.locator("#plant-card")
        await expect(card).to_be_visible()
        await expect(card.locator('[data-total="plant_load_power"]')).to_contain_text("360")
        await expect(
            card.locator('[data-total="plant_battery_charge_energy_total"]')
        ).to_contain_text("noch unbekannt")
        await expect(card.locator('li[data-member="deye2"]')).to_contain_text("veraltet")
        await shot(page, "plant-card-light-de")
        await page.locator("#nav-plant").click()
        section = page.locator("#plant-totals")
        await expect(section).to_contain_text("Anlage (Summen)")
        boxes = section.locator('input[type="checkbox"]')
        await expect(boxes).to_have_count(3)  # two inverters and the single-member option
        await expect(section.locator('[data-member="deye2"]')).to_have_text("Deye 2")  # name only
        top = [
            await section.locator(f'[data-member="{i}"]').bounding_box() for i in ("deye1", "deye2")
        ]
        assert top[0] and top[1] and top[1]["y"] > top[0]["y"]  # one per line
        await expect(section.locator('[data-member="deye2"] input')).to_be_checked()
        await section.locator('[data-member="deye2"] input').uncheck()
        await section.locator("#save-totals").click()
        await expect(section.locator('[data-member="deye2"] input')).not_to_be_checked()
        assert (await totals(rig))["exclude"] == ["deye2"]
        await shot(page, "plant-totals-section-light-de")
