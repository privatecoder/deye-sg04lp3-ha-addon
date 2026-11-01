"""Browser end-to-end test of the write layer v1 (``-m ui``) on isolated fake inverters: release
per inverter, preview, confirmation, result with read-back and peers, audit list. Never a real
device: the plant rig only knows local fake servers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.async_api import Browser, Page, expect

from tests.plant_helpers import plant_ui
from tests.test_ui import browser, shot  # noqa: F401
from tests.test_ui_plant import open_page
from tests.write_helpers import build_plant, fast, fc16, settle

pytestmark = pytest.mark.ui


async def release(page: Page, inverter: str, operations: list[str]) -> None:
    card = page.locator(f'article.inverter[data-inverter="{inverter}"]')
    section = card.locator("details.writes-section")
    await section.locator("summary").click()
    await section.locator(f"#writes-allow-{inverter}").check()
    for key in operations:
        await section.locator(f'input[data-operation="{key}"]').check()
    await (
        section.get_by_role("button")
        .filter(has_text=re.compile("Freigabe speichern|Save release"))
        .click()
    )
    await expect(page.locator(".toast.ok")).to_be_visible()


async def test_release_preview_confirm_and_result(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, enable=())
        s = await open_page(browser, url + "#/plant")
        page = s.page
        card = page.locator('article.inverter[data-inverter="deye1"]')
        await expect(card.locator("details.writes-section summary")).to_contain_text("aus")

        await release(page, "deye1", ["grid_charge_current", "grid_charge_enable"])
        await settle(rig, ("deye1", "deye2"))
        cfg = {i.id: i for i in rig.service.plant.inverters}
        assert cfg["deye1"].allow_writes and cfg["deye2"].allow_writes is False
        assert cfg["deye1"].enabled_write_operations == (
            "grid_charge_current",
            "grid_charge_enable",
        )
        await expect(card.locator("details.writes-section summary")).to_contain_text(
            "2 Einstellung"
        )
        await shot(page, "writes-plant-release-de")

        await page.goto(url + "#/inv/deye1")
        await expect(page.locator("#open-writes")).to_be_enabled()
        await expect(page.locator(".writes-panel")).to_contain_text(
            "1 Einstellung(en) freigegeben".replace("1 ", "2 ")
        )
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        await expect(dialog.locator(".write-scope")).to_contain_text(
            "Schreibt nur diesen Wechselrichter. Andere Wechselrichter werden beobachtet; "
            "gleiche Werte beweisen keine Synchronisierung."
        )
        row = dialog.locator('article.write-row[data-operation="grid_charge_current"]')
        await expect(row.locator(".current-value")).to_have_text("20 A")
        await expect(
            dialog.locator('article.write-row[data-operation="battery_charge_current"]')
        ).to_have_count(0)
        await row.locator("input[type=text]").fill("50")
        await row.get_by_role("button", name="Vorschau").click()

        preview = dialog.locator("section.write-preview")
        await expect(preview).to_contain_text("noch nichts gesendet")
        await expect(preview.locator(".change")).to_have_text("20 A → 50 A")
        await expect(preview).to_contain_text("0x0014 → 0x0032")
        await expect(preview.locator('tr[data-peer="deye2"]')).to_contain_text("20 A (0x0014)")
        await shot(page, "writes-preview-de")
        assert all(
            s.responder.writes == [] for s in rig.servers.values()
        )  # a preview sends nothing

        fast(rig)  # the contract delays are not needed for the test
        await preview.get_by_role("button", name="Jetzt schreiben").click()
        result = dialog.locator("section.write-result")
        await expect(result.locator(".write-status")).to_have_text("Rücklesewert bestätigt")
        await expect(result.locator(".readback")).to_contain_text("50 A (Rohwort 0x0032)")
        await expect(result.locator('tr[data-peer="deye2"] .peer-state')).to_have_text("anders")
        await expect(result).to_contain_text("MCU1")
        await expect(dialog.locator(".audit-table tbody tr")).to_have_count(1)
        await shot(page, "writes-result-de")
        assert len(fc16(rig.servers["gw1"])) == 1 and rig.servers["gw2"].responder.writes == []
        assert rig.servers["gw1"].responder.regs[128] == 50

        await result.locator('button[data-verify="confirmed"]').click()
        await expect(result).to_contain_text("Deine Prüfung")
        # the fresh value of the setting is shown again afterwards
        await expect(row.locator(".current-value")).to_have_text("50 A")
        await page.keyboard.press("Escape")
        await expect(page.locator("dialog")).to_have_count(0)
        await s.check()


async def test_english_texts_the_bit_operation_and_a_rejected_value(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(
            rig, inverters=("deye1",), operations=["grid_charge_enable", "smart_load_on_soc"]
        )
        fast(rig)
        s = await open_page(browser, url + "#/inv/deye1", lang="en-US", width=390)
        page = s.page
        s.expected.append("422")  # the refused threshold is a provoked failure
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        await expect(dialog.locator(".write-scope")).to_have_text(
            "Writes this inverter only. Other inverters are observed; "
            "matching values do not prove synchronization."
        )
        # a Smart Load threshold that breaks OFF < ON is refused with a plain text
        soc = dialog.locator('article.write-row[data-operation="smart_load_on_soc"]')
        await soc.locator("input[type=text]").fill("5")
        await soc.get_by_role("button", name="Preview").click()
        await expect(dialog.locator(".banner.error")).to_contain_text("OFF must stay below ON")
        # the bit operation is a select and keeps the other bits
        bit = dialog.locator('article.write-row[data-operation="grid_charge_enable"]')
        await bit.locator("select").select_option("1")
        await bit.get_by_role("button", name="Preview").click()
        await expect(dialog.locator("section.write-preview .change")).to_have_text("off → on")
        await expect(dialog.locator("section.write-preview")).to_contain_text(
            "0x0004 → 0x0005 (mask 0x0001)"
        )
        await dialog.get_by_role("button", name="Write now").click()
        await expect(dialog.locator(".write-status")).to_have_text("Read-back confirmed")
        assert rig.servers["gw1"].responder.regs[130] == 0x0005
        assert (
            await page.evaluate(
                "(d => d.scrollWidth > d.clientWidth)(document.querySelector('dialog'))"
            )
        ) is False
        await shot(page, "writes-result-phone-en")
        await s.check()


async def test_an_inverter_without_release_offers_no_way_to_change_settings(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, enable=("deye1",), operations=["grid_charge_current"])
        s = await open_page(browser, url + "#/inv/deye2")
        page = s.page
        await expect(page.locator("#open-writes")).to_be_disabled()
        await expect(page.locator(".writes-panel")).to_contain_text("nicht freigegeben")
        await s.check()
        released = await open_page(browser, url + "#/inv/deye1")
        await expect(released.page.locator("#open-writes")).to_be_enabled()
        assert all(f.responder.writes == [] for f in rig.servers.values())
        await released.check()
