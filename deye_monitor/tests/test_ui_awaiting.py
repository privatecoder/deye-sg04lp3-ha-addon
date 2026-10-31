"""First start in the web interface: an inverter without a saved selection asks for one; the quick
actions "Basics only" / "Deselect all" are ordinary deviations from the template."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.async_api import Browser, expect

from tests.test_ui import browser, open_setup, open_ui  # noqa: F401
from tests.ui_helpers import ui_stack

pytestmark = pytest.mark.ui

BASIC = {
    "serial_number",
    "operating_state",
    "active_warnings",
    "active_faults",
}


async def test_overview_asks_for_a_selection_and_the_setup_page_explains_it(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False, require_choice=True) as stack:
        s = await open_ui(browser, stack)
        card = s.page.locator("article.inverter").first
        await expect(card.locator(".awaiting")).to_contain_text("Bitte Sensoren auswählen")
        await card.get_by_role("link", name="Auswahl jetzt treffen").click()
        await expect(s.page.locator("section.step").first).to_be_visible()
        await expect(s.page.locator(".banner.awaiting")).to_contain_text(
            "Noch keine Auswahl gespeichert"
        )
        await s.check()


async def test_basics_only_saves_exactly_the_four_basic_sensors(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False, require_choice=True) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        await expect(
            page.locator(".sensor input:checked").first
        ).to_be_visible()  # the template proposal
        await page.locator('button[data-quick="basic"]').click()
        await expect(page.locator(".sensor input:checked")).to_have_count(4)
        await expect(page.get_by_text("4 Sensoren", exact=False).first).to_be_visible()
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        sel = stack.plant.manager.selection("deye1")
        assert sel.chosen and sel.effective == BASIC
        assert stack.plant.rig.inv("deye1").selection == BASIC
        assert not stack.plant.rig.inv("deye1").awaiting_selection
        await expect(page.locator(".banner.awaiting")).to_be_hidden()
        # the quick action left an ordinary deviation: a plant feature change keeps it
        assert len(sel.removed) > 30
        await s.check()


async def test_deselect_all_then_add_a_sensor(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(tmp_path, running=False, require_choice=True) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        await page.locator('button[data-quick="none"]').click()
        await expect(page.locator(".sensor input:checked")).to_have_count(0)
        await page.locator('.sensor[data-key="battery_soc"] input').check()
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        assert stack.plant.manager.selection("deye1").effective == {"battery_soc"}


async def test_the_template_can_be_accepted_unchanged(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(tmp_path, running=False, require_choice=True) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        await page.get_by_role("button", name="Vorlage unverändert übernehmen").click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        sel = stack.plant.manager.selection("deye1")
        assert sel.chosen and sel.effective == sel.template and not sel.removed and not sel.added
        await expect(page.locator(".banner.awaiting")).to_be_hidden()
        await s.check()


async def test_nothing_changes_without_the_require_choice_policy(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack)
        await expect(s.page.locator(".awaiting")).to_have_count(0)


async def test_the_template_is_accepted_with_the_prefilled_answers(
    browser: Browser, tmp_path: Any
) -> None:
    # the device suggests "no battery" (R111 = 2): the answer is shown pre-filled and counts as
    # unsaved, yet accepting the template unchanged must be possible and take the answer along
    async with ui_stack(tmp_path, require_choice=True) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        await expect(page.locator(".prefilled").first).to_be_visible()
        button = page.get_by_role("button", name="Vorlage unverändert übernehmen")
        await expect(button).to_be_enabled()
        await button.click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        sel = stack.plant.manager.selection("deye1")
        assert sel.chosen and not sel.added and not sel.removed
        assert sel.features.battery is False  # the displayed answer was saved ...
        assert "battery" in sel.confirmed  # ... as confirmed
        await expect(page.locator(".banner.awaiting")).to_be_hidden()
        await s.check()


async def test_the_template_button_waits_for_the_save_when_sensors_were_changed(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False, require_choice=True) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        await page.locator('button[data-quick="none"]').click()
        button = page.get_by_role("button", name="Vorlage unverändert übernehmen")
        await expect(button).to_be_disabled()
        await expect(page.locator(".banner.awaiting")).to_contain_text("bitte unten speichern")
        await page.locator('button[data-quick="none"]').click()  # idempotent: still deviating
        assert not stack.plant.manager.selection("deye1").chosen


async def test_a_capability_conflict_shows_the_raw_values(browser: Browser, tmp_path: Any) -> None:
    from tests.device_helpers import device_regs, ep, inverter
    from tests.selection_helpers import A

    banks = {"gw1": device_regs(A, r0=0x0600, r22=0x0503, r24=7)}
    async with ui_stack(
        tmp_path,
        endpoints=[ep("gw1", host="192.0.2.11")],
        inverters=[inverter("deye1", "gw1")],
        banks=banks,
    ) as stack:
        s = await open_ui(browser, stack)
        detail = s.page.locator("article.inverter .hint-detail")
        await expect(detail.filter(has_text="R0=0x0600, expected 0x0500")).to_have_count(1)
        await expect(detail.filter(has_text="R22 reports 5 MPPT")).to_have_count(1)
        await expect(detail.filter(has_text="R24")).to_have_count(0)  # unknown code, no conflict
        await expect(s.page.locator("article.inverter .hint.info")).to_contain_text(
            "Unbekannter Batterieeingangs-Code 7"
        )
