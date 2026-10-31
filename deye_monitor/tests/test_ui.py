"""Browser end-to-end tests of the Ingress UI (``-m ui``; skipped without a local browser)."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, Page, async_playwright, expect
from playwright.async_api import Error as PlaywrightError

from deye_monitor.selection import Edit
from tests.ui_helpers import UiStack, ui_stack

pytestmark = pytest.mark.ui

SHOTS = Path(__file__).resolve().parents[2] / ".orch" / "ui-screenshots"
CSP_PROBE = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => {
  window.__csp.push(e.violatedDirective + ' ' + e.blockedURI);
});
"""


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        for channel in ("chrome", None):
            try:
                instance = await pw.chromium.launch(channel=channel)
            except PlaywrightError:
                continue
            try:
                yield instance
            finally:
                await instance.close()
            return
        if os.environ.get("DEYE_UI_REQUIRED"):  # CI: a missing browser is a failure, not a skip
            pytest.fail("no local Chrome/Chromium for Playwright (DEYE_UI_REQUIRED is set)")
        pytest.skip("no local Chrome/Chromium for Playwright")


class Session:
    """A page with CSP and console watchers; ``check()`` fails on any violation or script error."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.errors: list[str] = []
        self.expected: list[str] = []  # console texts of failures the test provokes on purpose
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: self.errors.append(f"console: {m.text}") if m.type == "error" else None,
        )

    async def check(self) -> None:
        violations: list[str] = await self.page.evaluate("window.__csp || []")
        assert violations == [], violations
        unexpected = [e for e in self.errors if not any(x in e for x in self.expected)]
        assert unexpected == [], unexpected


async def open_ui(
    browser: Browser,
    stack: UiStack,
    *,
    lang: str = "de-DE",
    scheme: str = "light",
    width: int = 1100,
    height: int = 900,
) -> Session:
    context = await browser.new_context(
        locale=lang,
        color_scheme=scheme,  # type: ignore[arg-type]
        viewport={"width": width, "height": height},
    )
    await context.add_init_script(CSP_PROBE)
    page = await context.new_page()
    session = Session(page)
    await page.goto(stack.url)
    await expect(page.locator("article.inverter").first).to_be_visible()
    return session


async def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=False)


def row(page: Page, key: str) -> Any:
    return page.locator(f'.sensor[data-key="{key}"]')


async def open_setup(s: Session, inverter: str = "deye1") -> None:
    await s.page.goto(s.page.url.split("#")[0] + f"#/inv/{inverter}")
    await expect(s.page.locator("section.step").first).to_be_visible()


# -- scenarios --------------------------------------------------------------------------------


async def test_overview_shows_state_path_and_hints(browser: Browser, tmp_path: Path) -> None:
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack)
        cards = s.page.locator("article.inverter")
        await expect(cards).to_have_count(2)
        first = cards.first
        await expect(first.locator(".chip")).to_have_text("Online")
        await expect(first).to_contain_text("2401234567")
        await expect(first).to_contain_text("Modbus-TCP-Gateway")
        await expect(first).to_contain_text("Hauptweg")
        await expect(first.locator(".light")).to_be_visible()
        await shot(s.page, "overview-light-de")
        await s.check()


async def test_confirming_features_changes_the_template_and_saves(
    browser: Browser, tmp_path: Path
) -> None:
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        # The device says "no battery": the answer is pre-filled and marked as to be confirmed.
        await expect(page.locator(".chip.prefilled")).to_be_visible()
        await expect(page.locator(".chip.prefilled")).to_have_text(
            "aus einer Geräteeinstellung gelesen – bitte bestätigen"
        )
        await expect(row(page, "battery_soc").locator("input")).not_to_be_checked()
        await expect(row(page, "grid_voltage_a").locator("input")).to_be_checked()
        # Island operation hides the grid values and brings the load output.
        await page.get_by_label("Nichts – Inselbetrieb").check()
        await expect(row(page, "grid_voltage_a").locator("input")).not_to_be_checked()
        await expect(row(page, "load_voltage_a").locator("input")).to_be_checked()
        await expect(page.locator(".change-box")).to_be_visible()
        await shot(page, "setup-light-de")
        before = stack.revision
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        assert stack.revision == before + 1
        features = stack.plant.manager.selection("deye1").features
        assert features.utility_grid is False and features.battery is False
        # A reload shows the saved plant, nothing pending.
        await page.reload()
        await expect(page.locator(".dirty-note")).to_have_text("Alles gespeichert")
        await expect(page.get_by_label("Nichts – Inselbetrieb")).to_be_checked()
        await s.check()


async def test_add_deselect_and_reset_a_sensor(browser: Browser, tmp_path: Path) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        manager = stack.plant.manager
        # deselect a template sensor
        await row(page, "grid_voltage_a").locator("input").uncheck()
        await expect(row(page, "grid_voltage_a")).to_contain_text("abgewählt")
        # add an expert raw value via search
        await page.locator("#step2-title").scroll_into_view_if_needed()
        await shot(page, "setup-sensors-light-de")
        await page.get_by_label("Sensoren durchsuchen").fill("R722")
        await expect(row(page, "pv_ext_r722")).to_be_visible()
        await row(page, "pv_ext_r722").locator("input").check()
        await expect(row(page, "pv_ext_r722")).to_contain_text("zusätzlich gewählt")
        await page.get_by_label("Sensoren durchsuchen").fill("")
        before = stack.revision
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        assert stack.revision == before + 1
        sel = manager.selection("deye1")
        assert "grid_voltage_a" in sel.removed and "pv_ext_r722" in sel.added
        # reset the deselected one individually
        await (
            row(page, "grid_voltage_a")
            .get_by_role("button", name=re.compile("zurücksetzen"))
            .click()
        )
        await expect(row(page, "grid_voltage_a").locator("input")).to_be_checked()
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok").nth(1)).to_be_visible()
        assert stack.revision == before + 2
        sel = manager.selection("deye1")
        assert "grid_voltage_a" not in sel.removed and "pv_ext_r722" in sel.added
        await s.check()


async def test_stale_revision_keeps_the_draft(browser: Browser, tmp_path: Path) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        s.expected.append("409")  # the browser logs the rejected save
        await row(page, "grid_voltage_a").locator("input").uncheck()
        # somebody else saves in the meantime
        await stack.plant.manager.change(
            stack.revision, {"deye1": Edit(remove=frozenset({"battery_soc"}))}
        )
        await page.get_by_role("button", name="Speichern", exact=True).click()
        alert = page.get_by_role("alert").filter(has_text="inzwischen geändert")
        await expect(alert).to_contain_text("inzwischen geändert")
        # the draft is still there
        await expect(row(page, "grid_voltage_a").locator("input")).not_to_be_checked()
        await page.get_by_role("button", name="Neu laden, Änderungen behalten").click()
        await expect(alert).to_have_count(0)
        await expect(row(page, "battery_soc").locator("input")).not_to_be_checked()  # theirs
        await expect(row(page, "grid_voltage_a").locator("input")).not_to_be_checked()  # mine
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        sel = stack.plant.manager.selection("deye1")
        assert {"grid_voltage_a", "battery_soc"} <= sel.removed
        await s.check()


async def test_transfer_preview_and_execute(browser: Browser, tmp_path: Path) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        await stack.plant.manager.change(
            0, {"deye1": Edit(features={"battery": False}, remove=frozenset({"grid_voltage_a"}))}
        )
        s = await open_ui(browser, stack)
        page = s.page
        await page.get_by_role("button", name="Auf andere übertragen …").first.click()
        dialog = page.locator("dialog[open]")
        await expect(dialog).to_be_visible()
        assert await dialog.get_by_role("button", name="Vorschau").count() == 0  # no extra gate
        target = dialog.locator("section.target")  # the preview loads by itself
        await expect(target).to_have_count(1)
        await expect(target).to_contain_text("fallen weg")
        await shot(page, "transfer-light-de")
        before = stack.revision
        execute = dialog.get_by_role("button", name="Übertragen", exact=True)
        await expect(execute).to_be_enabled()
        await execute.click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        assert stack.revision == before + 1
        copied = stack.plant.manager.selection("deye2")
        assert copied.features.battery is False and "grid_voltage_a" in copied.removed
        await s.check()


async def test_transfer_follows_its_options_with_a_fresh_preview(
    browser: Browser, tmp_path: Path
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        page = s.page
        await page.get_by_role("button", name="Auf andere übertragen …").first.click()
        dialog = page.locator("dialog[open]")
        execute = dialog.get_by_role("button", name="Übertragen", exact=True)
        await expect(dialog.locator("section.target")).to_have_count(1)
        await expect(execute).to_be_enabled()
        await dialog.get_by_role("checkbox").first.uncheck()  # the only target: nothing to send
        await expect(execute).to_be_disabled()
        await expect(dialog.locator(".banner.error")).to_be_visible()
        await dialog.get_by_role("checkbox").first.check()
        await expect(dialog.locator("section.target")).to_have_count(1)
        await expect(execute).to_be_enabled()
        await s.check()


async def test_overview_hints_open_and_are_ordered_warnings_first(
    browser: Browser, tmp_path: Path
) -> None:
    async with ui_stack(tmp_path) as stack:
        context = await browser.new_context(locale="de-DE", viewport={"width": 1100, "height": 900})
        page = await context.new_page()

        async def crowded(route: Any) -> None:
            response = await route.fetch()
            data = await response.json()
            status = data["inverters"][0]["status"]
            status["hints"] = [
                {
                    "key": k,
                    "state": "zero_only_inconclusive",
                    "responses": 88,
                    "daylight_samples": 0,
                }
                for k in (
                    "battery_voltage",
                    "grid_voltage_a",
                    "grid_frequency",
                    "pv1_voltage",
                    "pv2_voltage",
                    "load_voltage_a",
                )
            ]
            status["capability_conflicts"] = [{"code": "device_type", "detail": "R0 says 0x1234"}]
            status["identity"] = {
                **status["identity"],
                "capabilities_unreported": ["mppt_phases"],
                "capabilities_unknown": {"battery_input_code": 5},
                "capability_words": {"22": 0xFFFF, "24": 5},
            }
            await route.fulfill(response=response, json=data)

        await page.route("**/api/inverters", crowded)
        await page.goto(stack.url)
        card = page.locator("article.inverter").first
        items = card.locator("ul.hints li")
        await expect(items).to_have_count(4)
        await expect(items.nth(0)).to_have_class(re.compile("conflict"))  # warnings first
        await expect(items.nth(1)).to_contain_text("R22=0xFFFF, R24=5")  # one merged info hint
        assert await card.get_by_text("Unbekannter Batterieeingangs-Code").count() == 0
        toggle = card.get_by_role("button", name="4 weitere Hinweise anzeigen")
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await toggle.focus()
        await page.keyboard.press("Enter")  # keyboard operable
        await expect(items).to_have_count(8)
        await expect(
            card.get_by_role("button", name="Weniger Hinweise anzeigen")
        ).to_have_attribute("aria-expanded", "true")
        await expect(items.nth(7)).to_contain_text("nur 0 geliefert")  # observations come last
        await page.keyboard.press("Enter")
        await expect(items).to_have_count(4)
        await context.close()


async def test_language_switch_and_persistence(browser: Browser, tmp_path: Path) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack, lang="de-DE")
        page = s.page
        await expect(page.locator("h1")).to_have_text("Deine Inverter")
        await page.get_by_role("button", name="English").click()
        await expect(page.locator("h1")).to_have_text("Your inverters")
        await expect(page.locator("html")).to_have_attribute("lang", "en")
        await page.reload()
        await expect(page.locator("h1")).to_have_text("Your inverters")
        await s.check()


async def test_dialog_is_keyboard_operable(browser: Browser, tmp_path: Path) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        page = s.page
        await page.locator("details.maintenance > summary").focus()
        await page.keyboard.press("Enter")
        opener = page.get_by_role("button", name="Identität neu binden …")
        await opener.focus()
        await page.keyboard.press("Enter")
        dialog = page.locator("dialog[open]")
        await expect(dialog).to_be_visible()
        assert await page.evaluate("document.activeElement.closest('dialog') !== null")
        go = dialog.get_by_role("button", name="Neu binden")
        await expect(go).to_be_disabled()
        # Tab stays inside the dialog
        for _ in range(8):
            await page.keyboard.press("Tab")
            assert await page.evaluate("document.activeElement.closest('dialog') !== null")
        await dialog.get_by_role("textbox").fill("deye1")
        await expect(go).to_be_enabled()
        await page.keyboard.press("Escape")
        await expect(dialog).to_have_count(0)
        assert await page.evaluate("document.activeElement.textContent.includes('neu binden')")
        await s.check()


async def test_maintenance_rebind_needs_the_typed_confirmation(
    browser: Browser, tmp_path: Path
) -> None:
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack)
        page = s.page
        await page.locator("details.maintenance > summary").click()
        await shot(page, "maintenance-light-de")
        await page.get_by_role("button", name="Identität neu binden …").click()
        dialog = page.locator("dialog[open]")
        await dialog.get_by_role("textbox").fill("falsch")
        await expect(dialog.get_by_role("button", name="Neu binden")).to_be_disabled()
        await dialog.get_by_role("textbox").fill("deye1")
        await dialog.get_by_role("button", name="Neu binden").click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        await s.check()


async def test_dark_mode_renders_and_is_switchable(browser: Browser, tmp_path: Path) -> None:
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack, scheme="dark")
        page = s.page
        dark_bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert dark_bg == "rgb(17, 20, 24)", dark_bg
        await shot(page, "overview-dark-de")
        await open_setup(s)
        await shot(page, "setup-dark-de")
        await page.get_by_role(
            "button", name=re.compile("Darstellung wechseln")
        ).click()  # -> light
        light_bg = await page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert light_bg == "rgb(244, 245, 247)", light_bg
        await s.check()


async def test_phone_width_has_no_horizontal_scroll(browser: Browser, tmp_path: Path) -> None:
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack, width=390, height=800)
        page = s.page
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await shot(page, "overview-phone-de")
        await open_setup(s)
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        await shot(page, "setup-phone-de")
        await s.check()


async def test_without_a_home_assistant_user_the_ui_is_read_only(
    browser: Browser, tmp_path: Path
) -> None:
    async with ui_stack(tmp_path, user=False, running=False) as stack:
        s = await open_ui(browser, stack)
        page = s.page
        await expect(page.locator(".banner.info").first).to_contain_text("nicht als Home-Assistant")
        await expect(
            page.get_by_role("button", name="Auf andere übertragen …").first
        ).to_be_disabled()
        await open_setup(s)
        await expect(row(page, "grid_voltage_a").locator("input")).to_be_disabled()
        await s.check()
