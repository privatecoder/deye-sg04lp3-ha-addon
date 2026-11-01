"""Browser end-to-end test of the write layer v1 (``-m ui``) on isolated fake inverters: release
per inverter, preview, confirmation, result with read-back and peers, audit list. Never a real
device: the plant rig only knows local fake servers."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, Page, expect

from tests.plant_helpers import plant_ui
from tests.test_ui import browser, shot  # noqa: F401
from tests.test_ui_plant import open_page
from tests.write_helpers import FAST, build_plant, fast, fc16, settle

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


# every element inside the dialog that sticks out past the dialog's content box (or scrolls itself)
_OVERFLOWING = """(() => {
  const d = document.querySelector('dialog');
  const box = d.getBoundingClientRect();
  const bad = [];
  if (d.scrollWidth > d.clientWidth) bad.push('dialog ' + d.scrollWidth + '>' + d.clientWidth);
  for (const e of d.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.width && (r.right > box.right + 0.5 || e.scrollWidth > e.clientWidth + 0.5))
      bad.push(e.tagName.toLowerCase() + '.' + e.className + ' ' +
               (e.textContent || '').trim().slice(0, 40));
  }
  return bad;
})()"""


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
        await expect(dialog.locator(".banner.error")).to_contain_text(
            "must stay above “Smart Load OFF SOC (R135)” (currently 20)"
        )
        # the switch is a select; R130 is written as the whole word 0 or 1
        bit = dialog.locator('article.write-row[data-operation="grid_charge_enable"]')
        await bit.locator("select").select_option("1")
        await bit.get_by_role("button", name="Preview").click()
        await expect(dialog.locator("section.write-preview .change")).to_have_text("off → on")
        await expect(dialog.locator("section.write-preview")).to_contain_text(
            "0x0000 → 0x0001 (mask 0x0001)"
        )
        await dialog.get_by_role("button", name="Write now").click()
        await expect(dialog.locator(".write-status")).to_have_text("Read-back confirmed")
        assert rig.servers["gw1"].responder.regs[130] == 0x0001
        bad = await page.evaluate(_OVERFLOWING)
        assert not bad, "\n".join(bad)
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


async def test_the_later_observation_and_reading_the_others_again(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, regs={"deye2": {"r128": 20}})
        fast(rig, replace(FAST, late_observe_delay_s=2.0))
        s = await open_page(browser, url + "#/inv/deye1")
        page = s.page
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        row = dialog.locator('article.write-row[data-operation="grid_charge_current"]')
        await row.locator("input[type=text]").fill("50")
        await row.get_by_role("button", name="Vorschau").click()
        await dialog.get_by_role("button", name="Jetzt schreiben").click()
        result = dialog.locator("section.write-result")
        peer = result.locator('tr[data-peer="deye2"]')
        await expect(result.locator("thead")).to_contain_text("Vorher")
        await expect(result.locator("thead")).to_contain_text("Nach 1 s")
        await expect(result.locator("thead")).to_contain_text("Nach 2 s")
        await expect(peer.locator(".peer-state")).to_have_text("anders")
        await expect(peer.locator(".late-countdown")).to_contain_text("wird gelesen in")
        rig.servers["gw2"].responder.regs[128] = 50  # the other unit follows
        await expect(peer.locator("td.late-state")).to_contain_text("50 A (0x0032) · gleich")
        # the first observation stays what it was
        await expect(peer.locator(".peer-state")).to_have_text("anders")
        await result.get_by_role("button", name="Andere erneut lesen").click()
        await expect(result.locator(".reread li")).to_contain_text("50 A (0x0032) · gleich")
        # the same button is in the log entry
        await expect(dialog.locator(".audit-table button[data-reread]")).to_have_count(1)
        assert len(fc16(rig.servers["gw1"])) == 1 and fc16(rig.servers["gw2"]) == []
        await s.check()


async def roles_known(rig: Any) -> None:
    """The status view knows the role once the settings were polled (the UI reads it from there)."""
    for _ in range(200):
        _status, body = await rig.call("GET", "/api/inverters")
        roles = [(i["status"].get("identity") or {}).get("role") for i in body["inverters"]]
        if len(roles) == 2 and all(roles):
            return
        await asyncio.sleep(0.1)
    raise AssertionError("roles were not read")


async def test_a_slave_cannot_be_written_and_points_to_the_master(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(
            rig,
            enable=("deye1", "deye2"),
            operations=["grid_charge_current"],
            regs={"deye1": {"r336": 0x0001}, "deye2": {"r336": 0x0003}},
        )
        await roles_known(rig)
        s = await open_page(browser, url + "#/inv/deye1")
        page = s.page
        notice = page.locator(".writes-panel .slave-locked")
        await expect(notice).to_contain_text("verteilt der Master diese Einstellungen")
        await expect(notice.locator("a.master-link")).to_have_attribute("href", "#/inv/deye2")
        await expect(page.locator("#open-writes")).to_be_disabled()
        await shot(page, "writes-slave-de")
        # the master offers the dialog and says that it distributes the value
        master_view = await open_page(browser, url + "#/inv/deye2")
        page = master_view.page
        await expect(page.locator("#open-writes")).to_be_enabled()
        await expect(page.locator(".writes-panel .master-hint")).to_contain_text("an die Slaves")
        # plant page: the release of a slave cannot be extended
        plant_view = await open_page(browser, url + "#/plant")
        page = plant_view.page
        slave = page.locator('article.inverter[data-inverter="deye1"] details.writes-section')
        await expect(slave.locator(".slave-locked")).to_be_visible()
        await expect(slave.locator('input[data-operation="grid_charge_current"]')).to_be_disabled()
        master = page.locator('article.inverter[data-inverter="deye2"] details.writes-section')
        await expect(master.locator('input[data-operation="grid_charge_current"]')).to_be_enabled()
        assert all(f.responder.writes == [] for f in rig.servers.values())
        await s.check()
        await master_view.check()
        await plant_view.check()


async def test_the_picker_notices_and_the_missing_sensors(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, enable=())
        s = await open_page(browser, url + "#/inv/deye1")
        page = s.page
        grid = page.locator('.sensor[data-key="grid_selected_power_total"]')
        grid_notice = await grid.locator(".sensor-notice").text_content() or ""
        assert "nicht mit ihr zusammenzählen" in grid_notice
        assert await grid.locator(".chip.hint-notice").count() == 1
        bms = page.locator('.sensor[data-key="bms_discharge_voltage"] .sensor-notice')
        assert "BMS-Protokoll" in (await bms.text_content() or "")
        for key in (
            "temp_dc_transformer",
            "bms_offgrid_charge_limit",
            "bms_offgrid_discharge_limit_raw",
            "load_frequency",
        ):
            assert await page.locator(f'.sensor[data-key="{key}"]').count() == 0, key
        # the settings section sits directly below the page head, above the steps
        order = await page.evaluate(
            "[...document.querySelectorAll('#view > *, main > *')].map(e => e.className)"
        )
        panel = await page.evaluate(
            "(() => { const p = document.querySelector('.writes-panel');"
            " const s = document.querySelector('[data-group]');"
            " const after = p && s && p.compareDocumentPosition(s);"
            " return !!(after & Node.DOCUMENT_POSITION_FOLLOWING);"
            " })()"
        )
        assert panel, order
        await s.check()


async def test_groups_tou_slots_weekdays_clock_and_a_time_write(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(
            rig,
            inverters=("deye1",),
            operations=[
                "battery_low_soc",
                "smart_load_off_soc",
                "tou_enable",
                "tou_slot1_start",
                "tou_slot1_power",
                "tou_slot2_start",
                "tou_slot2_grid_charge",
            ],
            regs={"deye1": {"r146": 0x008A}},  # Monday, Wednesday, Sunday
        )
        fast(rig)
        s = await open_page(browser, url + "#/inv/deye1")
        s.expected.append("422")  # the refused crossing is a provoked failure
        page = s.page
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        groups = dialog.locator("section.write-group")
        await expect(groups).to_have_count(3)
        await expect(groups.nth(0)).to_contain_text("Batterie-Schutz")
        await expect(groups.nth(1)).to_contain_text("Smart Load")
        await expect(groups.nth(2)).to_contain_text("Zeitplan ToU")
        tou = dialog.locator('section.write-group[data-group="tou"]')
        # read-only context: effective weekday filter and the inverter clock
        await expect(tou.locator(".weekday-list li.on")).to_have_text(["Mo", "Mi", "So"])
        await expect(tou.locator(".clock-value")).to_have_text("2026-11-01 12:30:15")
        await expect(tou.locator(".tou-info input, .tou-info button")).to_have_count(0)
        # two slot rows, each with its own settings
        await expect(tou.locator("section.tou-slot")).to_have_count(2)
        slot1 = tou.locator('section.tou-slot[data-slot="1"]')
        await expect(slot1.locator("article.write-row")).to_have_count(2)
        await expect(
            slot1.locator('[data-operation="tou_slot1_start"] .current-value')
        ).to_have_text("00:00")
        await expect(
            slot1.locator('[data-operation="tou_slot1_power"] .current-value')
        ).to_have_text("1000 W")
        # a time that crosses the next slot is refused with a plain text and sends nothing
        start2 = tou.locator('article.write-row[data-operation="tou_slot2_start"]')
        await start2.locator("input[type=text]").fill("10:00")
        await start2.get_by_role("button", name="Vorschau").click()
        await expect(dialog.locator(".banner.error")).to_contain_text(
            "Startzeiten müssen aufsteigen"
        )
        assert rig.servers["gw1"].responder.writes == []
        # a valid time goes through preview and confirmation
        await start2.locator("input[type=text]").fill("09:55")
        await start2.get_by_role("button", name="Vorschau").click()
        preview = dialog.locator("section.write-preview")
        await expect(preview.locator(".change")).to_have_text("05:00 → 09:55")
        await expect(preview).to_contain_text("0x01F4 → 0x03BB")
        await preview.get_by_role("button", name="Jetzt schreiben").click()
        await expect(dialog.locator(".write-status")).to_have_text("Rücklesewert bestätigt")
        assert rig.servers["gw1"].responder.regs[149] == 955
        assert [r.start for r in rig.servers["gw1"].responder.writes] == [149]
        await shot(page, "writes-tou-de")
        await s.check()


async def test_all_settings_on_a_phone_in_english(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, inverters=("deye1",))
        fast(rig)
        s = await open_page(browser, url + "#/plant", lang="en-US", width=390)
        page = s.page
        release_box = page.locator('article.inverter[data-inverter="deye1"] details.writes-section')
        await expect(release_box.locator("input[data-operation]")).to_have_count(38)
        await expect(release_box.locator("div.write-group")).to_have_count(4)
        await expect(
            release_box.locator('input[data-operation="tou_slot3_grid_charge"]')
        ).to_be_checked()
        await expect(release_box).to_contain_text("allow GRID charging")
        await page.goto(url + "#/inv/deye1")
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        tou = dialog.locator('section.write-group[data-group="tou"]')
        await expect(tou.locator("section.tou-slot")).to_have_count(6)
        await expect(tou.locator("article.write-row")).to_have_count(25)
        await expect(tou.locator(".weekday-list li.on")).to_have_count(7)
        await expect(
            dialog.locator('section.write-group[data-group="grid_charge"]')
        ).to_contain_text("GRID charge start SOC")
        # nothing in the dialog is wider than the phone
        assert (
            await page.evaluate(
                "(d => d.scrollWidth > d.clientWidth)(document.querySelector('dialog'))"
            )
        ) is False
        await shot(page, "writes-tou-phone-en")
        assert all(f.responder.writes == [] for f in rig.servers.values())
        await s.check()


async def test_gated_settings_show_their_reason_in_the_dialog(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(
            rig,
            inverters=("deye1",),
            operations=[
                "battery_low_soc",
                "grid_charge_enable",
                "tou_slot1_start",
                "smart_load_off_voltage",
            ],
            regs={"deye1": {"r111": 0, "r130": 4, "r146": 0x0100}},
        )
        s = await open_page(browser, url + "#/inv/deye1")
        page = s.page
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        low = dialog.locator('article.write-row[data-operation="battery_low_soc"]')
        await expect(low).to_contain_text("nicht im SOC-Modus (R111 muss 1 sein)")
        await expect(low.locator("input")).to_have_count(0)
        await expect(dialog.locator('[data-operation="grid_charge_enable"]')).to_contain_text(
            "mehr als 0 oder 1"
        )
        await expect(dialog.locator('[data-operation="tou_slot1_start"]')).to_contain_text(
            "Spanien-Modus"
        )
        await expect(dialog.locator(".tou-info .banner")).to_contain_text("Spanien-Modus")
        volt = dialog.locator('[data-operation="smart_load_off_voltage"]')
        await expect(volt.locator(".current-value")).to_have_text("45.00 V")
        assert all(f.responder.writes == [] for f in rig.servers.values())
        await s.check()
