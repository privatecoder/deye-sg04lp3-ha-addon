"""Browser tests (``-m ui``) of the 0.5.0 surface: the new installation questions, the new
setting rows and the suggestion from the plant description. Fake devices only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Browser, expect

from tests.plant_helpers import plant_ui
from tests.test_ui import browser, open_setup, open_ui, shot
from tests.test_ui_plant import open_page
from tests.ui_helpers import ui_stack
from tests.write_helpers import build_plant, fast

pytestmark = pytest.mark.ui
FIXTURES = (browser,)  # the pytest fixture comes from tests.test_ui

QUESTIONS = (
    "export_policy",
    "external_measurement",
    "external_signal_use",
    "battery_control_basis",
    "tou_used",
)


async def test_the_new_questions_are_asked_and_saved(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        for name in QUESTIONS:
            await expect(page.locator(f'fieldset[data-feature="{name}"]')).to_be_visible()
        # unanswered: nothing is chosen for the user
        measurement = page.locator('fieldset[data-feature="external_measurement"]')
        await expect(measurement.locator("input:checked")).to_have_value("unknown")
        await measurement.get_by_label("CT (Stromwandler)", exact=False).check()
        tou = page.locator('fieldset[data-feature="tou_used"]')
        await tou.get_by_label("Ja, Zeitplan", exact=False).check()
        # the sensor preview follows the answers
        await expect(page.locator('[data-key="ct_a_selfcheck_success"] input')).to_be_checked()
        await expect(page.locator('[data-key="tou_enable"] input')).to_be_checked()
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        feats = stack.plant.manager.selection("deye1").features
        assert (feats.external_measurement, feats.tou_used) == ("ct", True)
        assert feats.export_policy == "unknown"
        await s.check()


async def test_the_installation_limits_and_the_ev_charger_are_entered_and_saved(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        box = page.locator('input[data-number="export_limit_w"]')
        await expect(box).to_have_value("")  # not entered: the settings that need it stay locked
        grid = page.locator('input[data-number="grid_limit_w"]')
        grid_note = page.locator('[data-feature="numbers"] .number-field').nth(1).locator(".note")
        await grid.fill("12")  # below the smallest accepted rating: said so, nothing is stored
        await expect(grid_note).to_be_visible()
        await grid.fill("")  # blank = not entered
        await expect(grid_note).to_be_hidden()
        reserve = page.locator('input[data-number="ev_offgrid_reserve_soc"]')
        reserve_note = (
            page.locator('[data-feature="numbers"] .number-field').nth(4).locator(".note")
        )
        await reserve.fill("150")  # an SOC above 100 %
        await expect(reserve_note).to_be_visible()
        await reserve.fill("40")
        await expect(reserve_note).to_be_hidden()
        await box.fill("0")  # an explicit zero export allowance is a stored value, not blank
        note = page.locator('[data-feature="numbers"] .number-field').first.locator(".note")
        await expect(note).to_be_hidden()
        await box.fill("9000")
        await expect(note).to_be_hidden()
        await box.fill("0")
        ev = page.locator('fieldset[data-feature="ev_charger"]')
        await ev.get_by_label("Am LOAD-Anschluss", exact=False).check()
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        feats = stack.plant.manager.selection("deye1").features
        assert (feats.export_limit_w, feats.ev_charger) == (0, "load_port")
        assert feats.ev_offgrid_reserve_soc == 40
        assert feats.grid_limit_w is None and feats.generator_capacity_w is None
        await s.check()


async def test_questions_hide_when_they_do_not_apply(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        battery = page.locator('fieldset[data-feature="battery"]')
        await battery.get_by_label("Nein", exact=False).first.check()
        await expect(page.locator('fieldset[data-feature="tou_used"]')).to_be_hidden()
        await expect(page.locator('fieldset[data-feature="battery_control_basis"]')).to_be_hidden()
        await expect(page.locator('fieldset[data-feature="external_signal_use"]')).to_be_visible()


async def test_new_setting_rows_groups_and_the_suggestion(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(rig, enable=())
        fast(rig)
        s = await open_page(browser, url + "#/plant", lang="en-US")
        page = s.page
        card = page.locator('article.inverter[data-inverter="deye1"]')
        section = card.locator("details.writes-section")
        await section.locator("summary").click()
        for group, key in (
            ("island", "force_off_grid"),
            ("island", "signal_island_mode"),
            ("pv", "mppt_scan"),
        ):
            row = section.locator(
                f'div.write-group[data-group="{group}"] input[data-operation="{key}"]'
            )
            await expect(row).to_be_visible()
            await expect(row).not_to_be_checked()
        await expect(section.locator("div.write-group[data-group=island] h4")).to_have_text(
            "Island operation and signals"
        )
        await expect(section).to_contain_text("N-PE bond")
        await expect(section).to_contain_text("0xFFFF")
        # a never answered description suggests nothing: the raw defaults are no answers
        await expect(section.locator("[data-suggestion]")).to_contain_text("no suggestion")
        await expect(section.locator('[data-action="apply-suggestion"]')).to_have_count(0)
        answers = {k: True for k in ("battery", "bms", "utility_grid", "mppt1_used", "mppt2_used")}
        status, body = await rig.call(
            "PUT",
            "/api/inverters/deye1/features",
            {"features": answers},
            revision=rig.service.manager.revision,
        )
        assert status == 200, body
        await page.reload()
        section = page.locator('article.inverter[data-inverter="deye1"] details.writes-section')
        await section.locator("summary").click()
        # battery, BMS, public grid and both inputs, answered: five settings are suggested
        await expect(section.locator("[data-suggestion] .help")).to_contain_text("5 settings")
        await section.locator('[data-action="apply-suggestion"]').click()
        for key in ("force_off_grid", "signal_island_mode", "mppt_scan"):
            await expect(section.locator(f'input[data-operation="{key}"]')).to_be_checked()
        await expect(
            section.locator('input[data-operation="grid_charge_enable"]')
        ).not_to_be_checked()
        assert not rig.service.plant.inverters[0].enabled_write_operations  # nothing saved yet
        await shot(page, "writes-suggestion-en")
        await s.check()


async def test_the_new_switches_in_the_dialog(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await build_plant(
            rig,
            inverters=("deye1",),
            operations=["signal_island_mode", "mppt_scan", "force_off_grid"],
            regs={"deye1": {"r178": 0x2AAA, "r179": 0, "r341": 0x0060}},
        )
        fast(rig)
        s = await open_page(browser, url + "#/inv/deye1", lang="en-US")
        page = s.page
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        island = dialog.locator('article.write-row[data-operation="signal_island_mode"]')
        await expect(island).to_contain_text("only the field 0x0300")
        await island.locator("select").select_option("1")
        await island.get_by_role("button", name="Preview").click()
        await expect(dialog.locator("section.write-preview .change")).to_have_text("off → on")
        await expect(dialog.locator("section.write-preview")).to_contain_text(
            "0x2AAA → 0x2BAA (mask 0x0300)"
        )
        await dialog.get_by_role("button", name="Write now").click()
        await expect(dialog.locator(".write-status")).to_have_text("Read-back confirmed")
        assert rig.servers["gw1"].responder.regs[178] == 0x2BAA
        await s.check()


async def test_the_rendered_ranges_show_the_ev_floor_and_a_zero_cap(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        regs = {"r111": 1, "r259": 0x32DD, "r142": 0, "r143": 0}
        await build_plant(rig, inverters=("deye1",), regs={"deye1": regs})
        inv = rig.app.runtime.inverters["deye1"] if rig.app.runtime else None
        assert inv is not None
        inv.features = inv.features.model_copy(
            update={"ev_offgrid_reserve_soc": 65, "export_limit_w": 0}
        )
        fast(rig)
        s = await open_page(browser, url + "#/inv/deye1", lang="en-US")
        page = s.page
        await page.locator("#open-writes").click()
        dialog = page.locator("dialog")
        ev = dialog.locator('article.write-row[data-operation="ev_offgrid_soc"]')
        await expect(ev).to_contain_text("Allowed: 70 to 100 %")  # 65 aligned up to the 10 step
        export = dialog.locator('article.write-row[data-operation="export_max_power"]')
        await expect(export).to_contain_text("Allowed: 0 to 0 W")
        await expect(export).not_to_contain_text("(rated power of the model)")
        await s.check()
