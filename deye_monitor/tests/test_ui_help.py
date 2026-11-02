"""Browser tests (``-m ui``) of the (i) help popover: open, close on Escape, outside click and
when another opens, keyboard use, focus return, 390 px width, light and dark. Fake devices only."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Browser, Locator, Page, expect

from tests.plant_helpers import plant_ui
from tests.test_ui import browser, open_setup, open_ui
from tests.test_ui_plant import open_page
from tests.ui_helpers import ui_stack
from tests.write_helpers import build_plant, fast

pytestmark = pytest.mark.ui
FIXTURES = (browser,)  # the pytest fixture comes from tests.test_ui


async def no_sideways_scroll(page: Page) -> None:
    wide = await page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )
    assert not wide


async def inside_viewport(page: Page, pop: Locator) -> None:
    box = await pop.bounding_box()
    width = await page.evaluate("window.innerWidth")
    assert box is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= width, box


async def test_the_help_in_the_dialog_opens_closes_and_returns_focus(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(
            rig, inverters=("deye1",), operations=["grid_charge_enable", "battery_charge_current"]
        )
        fast(rig)
        s = await open_page(browser, url + "#/inv/deye1", lang="en-US", width=390)
        page = s.page
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        first = dialog.locator('article[data-operation="grid_charge_enable"] .help-btn')
        second = dialog.locator('article[data-operation="battery_charge_current"] .help-btn')
        await expect(first).to_have_attribute("aria-expanded", "false")
        await expect(first).to_have_attribute("aria-label", "About “GRID charge enable (R130)”")

        # opens on click: the button names the note it controls
        await first.click()
        await expect(first).to_have_attribute("aria-expanded", "true")
        pop_id = await first.get_attribute("aria-controls")
        assert pop_id
        pop = page.locator(f"#{pop_id}")
        await expect(pop).to_be_visible()
        await expect(pop).to_contain_text("charging the battery from the grid")
        await inside_viewport(page, pop)
        await no_sideways_scroll(page)

        # another one opens: the first closes
        await second.click()
        await expect(first).to_have_attribute("aria-expanded", "false")
        await expect(pop).to_be_hidden()
        await expect(second).to_have_attribute("aria-expanded", "true")
        second_pop = page.locator(f"#{await second.get_attribute('aria-controls')}")
        await expect(second_pop).to_be_visible()

        # Escape closes only the note, the dialog stays; focus returns to the button
        await page.keyboard.press("Escape")
        await expect(second_pop).to_be_hidden()
        await expect(second).to_have_attribute("aria-expanded", "false")
        await expect(second).to_be_focused()
        await expect(dialog).to_be_visible()

        # a click or tap outside closes it, and the dialog stays
        await first.click()
        await expect(pop).to_be_visible()
        await dialog.locator(".write-scope").click()
        await expect(pop).to_be_hidden()
        await expect(dialog).to_be_visible()

        # a second click on the same button toggles it
        await first.click()
        await expect(pop).to_be_visible()
        await first.click()
        await expect(pop).to_be_hidden()

        # Escape without an open note closes the dialog as before
        await page.keyboard.press("Escape")
        await expect(dialog).to_have_count(0)
        await s.check()


async def test_the_keyboard_opens_the_help_and_escape_returns_focus(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, inverters=("deye1",), operations=["grid_charge_enable"])
        fast(rig)
        s = await open_page(browser, url + "#/inv/deye1", lang="de-DE", width=390)
        page = s.page
        await page.locator("#open-writes").click()
        button = page.locator('dialog article[data-operation="grid_charge_enable"] .help-btn')
        await button.focus()
        await page.keyboard.press("Enter")
        await expect(button).to_have_attribute("aria-expanded", "true")
        pop = page.locator(f"#{await button.get_attribute('aria-controls')}")
        await expect(pop).to_contain_text("Laden der Batterie aus dem Netz")
        await page.keyboard.press("Escape")
        await expect(pop).to_be_hidden()
        await expect(button).to_be_focused()
        await page.keyboard.press("Space")
        await expect(pop).to_be_visible()
        # focus moving elsewhere closes the note
        await page.locator("dialog .icon-btn").focus()
        await expect(pop).to_be_hidden()
        await no_sideways_scroll(page)
        await s.check()


async def test_the_help_in_the_release_list_does_not_tick_the_box(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, enable=())
        fast(rig)
        s = await open_page(browser, url + "#/plant", lang="en-US", width=390)
        page = s.page
        card = page.locator('article.inverter[data-inverter="deye1"]')
        await card.locator("details.writes-section summary").click()
        label = card.locator(
            "label.write-op", has=page.locator('input[data-operation="bms_loss_fault"]')
        )
        box = label.locator("input")
        await expect(box).not_to_be_checked()
        button = label.locator(".help-btn")
        await button.click()
        await expect(box).not_to_be_checked()
        pop = page.locator(f"#{await button.get_attribute('aria-controls')}")
        await expect(pop).to_contain_text("trade-off")
        await inside_viewport(page, pop)
        # a click into the note is not a click on the row
        await pop.click()
        await expect(box).not_to_be_checked()
        await no_sideways_scroll(page)
        await s.check()


async def test_the_sensor_picker_has_help_for_the_unclear_sensors_only(
    browser: Browser, tmp_path: Path
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack, lang="en-US", width=390)
        await open_setup(s)
        page = s.page
        unclear = page.locator('.sensor[data-key="relay_grid"]')
        obvious = page.locator('.sensor[data-key="battery_voltage"]')
        assert await unclear.count() == 1 and await obvious.count() == 1
        await expect(obvious.locator(".help-btn")).to_have_count(0)
        await page.evaluate(
            "document.querySelectorAll('details.group').forEach(d => d.open = true)"
        )
        button = unclear.locator(".help-btn")
        await button.scroll_into_view_if_needed()
        checked = await unclear.locator("input").is_checked()
        await button.click()
        assert await unclear.locator("input").is_checked() == checked  # the label is not toggled
        pop = page.locator(f"#{await button.get_attribute('aria-controls')}")
        await expect(pop).to_contain_text("not mechanical feedback")
        await inside_viewport(page, pop)
        await page.keyboard.press("Escape")
        await expect(pop).to_be_hidden()
        await no_sideways_scroll(page)
        await s.check()


async def test_the_inverter_page_explains_the_protection_word_sensors(
    browser: Browser, tmp_path: Path
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack, lang="en-US", width=390)
        await open_setup(s)
        page = s.page
        await page.evaluate(
            "document.querySelectorAll('details.group').forEach(d => d.open = true)"
        )
        for key, phrase in (
            ("anti_islanding_enable_raw", "not enabled"),
            ("rcd_enable_raw", "zero from nonzero"),
            ("track_grid_phase_raw", "default"),
        ):
            row = page.locator(f'.sensor[data-key="{key}"]')
            assert await row.count() == 1
            assert not await row.locator("input").is_checked()  # default off
            button = row.locator(".help-btn")
            await button.scroll_into_view_if_needed()
            await button.click()
            pop = page.locator(f"#{await button.get_attribute('aria-controls')}")
            await expect(pop).to_contain_text(phrase)
            await inside_viewport(page, pop)
            await page.keyboard.press("Escape")
            await expect(pop).to_be_hidden()
        await no_sideways_scroll(page)
        await s.check()


async def test_the_popover_follows_the_dark_theme(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, inverters=("deye1",), operations=["grid_charge_enable"])
        fast(rig)
        colors = {}
        for scheme in ("light", "dark"):
            s = await open_page(
                browser, url + "#/inv/deye1", lang="en-US", width=390, scheme=scheme
            )
            page = s.page
            await page.locator("#open-writes").click()
            button = page.locator('dialog article[data-operation="grid_charge_enable"] .help-btn')
            await button.click()
            pop = page.locator(f"#{await button.get_attribute('aria-controls')}")
            await expect(pop).to_be_visible()
            colors[scheme] = await pop.evaluate(
                "el => [getComputedStyle(el).backgroundColor, getComputedStyle(el).color]"
            )
            await s.check()
        assert colors["light"] != colors["dark"]


async def inside_visible_area(page: Page, pop: Locator) -> None:
    box = await pop.bounding_box()
    size = await page.evaluate("[window.innerWidth, window.innerHeight]")
    assert box is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= size[0], box
    assert box["y"] >= 0 and box["y"] + box["height"] <= size[1] + 0.5, (box, size)


@pytest.mark.parametrize("lang", ["en-US", "de-DE"])
@pytest.mark.parametrize("height", [600, 300])
async def test_the_help_stays_inside_a_short_390_px_screen_and_is_described_by_the_trigger(
    browser: Browser, tmp_path: Path, lang: str, height: int
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack, lang=lang, width=390, height=height)
        await open_setup(s)
        page = s.page
        await page.evaluate(
            "document.querySelectorAll('details.group').forEach(d => d.open = true)"
        )
        row = page.locator('.sensor[data-key="remote_run_enable"]')
        button = row.locator(".help-btn")
        await button.scroll_into_view_if_needed()
        await expect(button).not_to_have_attribute("aria-describedby", r"^.+$")
        await button.click()
        pop = page.locator(f"#{await button.get_attribute('aria-controls')}")
        await expect(pop).to_be_visible()
        await inside_visible_area(page, pop)
        # the trigger is described by the note: its accessible description is the note text
        assert await button.get_attribute("aria-describedby") == await pop.get_attribute("id")
        described = await page.evaluate(
            "document.getElementById(document.querySelector('.help-btn[aria-expanded=true]')"
            ".getAttribute('aria-describedby')).textContent"
        )
        assert described.strip() == (await pop.text_content() or "").strip()
        assert "R80" in (await pop.text_content() or "")  # the "Register R80" line
        await page.keyboard.press("Escape")
        await expect(pop).to_be_hidden()
        await expect(button).to_be_focused()
        await expect(button).not_to_have_attribute("aria-describedby", r"^.+$")
        await s.check()
