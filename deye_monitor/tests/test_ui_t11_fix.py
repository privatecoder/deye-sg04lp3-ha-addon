"""Regressions of code review 7 (R7-01..04): browser tests on answer order and status shapes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from playwright.async_api import Browser, Page, Route, expect

from deye_monitor.selection import Edit
from tests.device_helpers import device_regs, ep, inverter
from tests.selection_helpers import A
from tests.test_ui import SHOTS, Session, browser, open_setup, open_ui, row  # noqa: F401
from tests.ui_helpers import ui_stack

pytestmark = pytest.mark.ui

IDS = [f"deye{i}" for i in range(1, 5)]


def four_devices(tmp_path: Any, **kw: Any) -> Any:
    return ui_stack(
        tmp_path,
        endpoints=[ep(f"g{i}", host=f"192.0.2.{i}") for i in range(1, 5)],
        inverters=[inverter(iid, f"g{i}") for i, iid in enumerate(IDS, 1)],
        banks={f"g{i}": device_regs(f"240123456{i}") for i in range(1, 5)},
        **kw,
    )


class Gate:
    """Holds the responses of one route until released (the server already handled the call)."""

    def __init__(self) -> None:
        self.entered: list[asyncio.Event] = []
        self.release = asyncio.Event()

    async def handler(self, route: Route) -> None:
        response = await route.fetch()
        event = asyncio.Event()
        self.entered.append(event)
        event.set()
        await self.release.wait()
        await route.fulfill(response=response)

    async def first(self) -> None:
        for _ in range(100):
            if self.entered:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("the call never reached the server")


# -- R7-01 ----------------------------------------------------------------------------------


async def test_pending_energy_change_keeps_the_overview_renderable(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path) as stack:
        inv = stack.plant.rig.inv("deye1")
        inv._observe_energy(0x0104, A, inv.clock())
        inv._observe_energy(0x0104, A, inv.clock())
        inv._observe_energy(0x0105, A, inv.clock())
        snap = inv.energy.snapshot()
        assert isinstance(snap["pending_change"], float)
        s = await open_ui(browser, stack)
        card = s.page.locator("article.inverter").first
        await expect(card).to_contain_text("Neue Energieauflösung")
        await expect(card).to_contain_text("nicht verfügbar")
        await expect(card).not_to_contain_text("bleiben die Energiewerte unverändert")
        await expect(s.page.locator("article.inverter")).to_have_count(2)
        await s.check()


async def test_blocked_energy_keeps_the_overview_renderable(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path) as stack:
        stack.plant.rig.inv("deye1").energy.blocked = "energy_store_corrupt"
        s = await open_ui(browser, stack)
        await expect(s.page.locator("article.inverter")).to_have_count(2)
        await s.check()


async def test_a_render_failure_is_not_reported_as_a_lost_connection(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack)
        page = s.page

        async def broken(route: Route) -> None:
            response = await route.fetch()
            body = (await response.text()).replace('"status"', '"state_"', 1)
            await route.fulfill(response=response, body=body)

        await page.route("**/api/inverters", broken)
        await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        await expect(page.locator("#conn")).to_be_hidden()
        await expect(page.get_by_role("alert").filter(has_text="Anzeige")).to_be_visible()
        s.errors.clear()  # the provoked failure is the point of this test


# -- R7-02 ----------------------------------------------------------------------------------


async def open_transfer(s: Session) -> Any:
    await s.page.get_by_role("button", name="Auf andere übertragen …").first.click()
    return s.page.locator("dialog[open]")


async def test_transfer_edit_invalidates_an_inflight_preview(
    browser: Browser, tmp_path: Any
) -> None:
    async with four_devices(tmp_path, running=False) as stack:
        await stack.plant.manager.change(0, {"deye1": Edit(features={"battery": False})})
        s = await open_ui(browser, stack)
        gate = Gate()
        await s.page.route("**/api/transfer/preview", gate.handler)
        dialog = await open_transfer(s)  # the preview of all three targets starts by itself
        await gate.first()
        target4 = dialog.locator("fieldset label.option").filter(has_text="deye4").locator("input")
        await target4.uncheck()  # the answer in flight must never cover this
        gate.release.set()
        execute = dialog.get_by_role("button", name="Übertragen", exact=True)
        await expect(dialog.locator("section.target")).to_have_count(2)  # the newer preview
        await expect(execute).to_be_enabled()
        await execute.click()
        await expect(s.page.locator(".toast.ok")).to_be_visible()
        assert stack.plant.manager.selection("deye4").features.battery is True


async def test_transfer_executes_exactly_the_previewed_payload(
    browser: Browser, tmp_path: Any
) -> None:
    async with four_devices(tmp_path, running=False) as stack:
        await stack.plant.manager.change(0, {"deye1": Edit(features={"battery": False})})
        s = await open_ui(browser, stack)
        dialog = await open_transfer(s)
        target4 = dialog.locator("fieldset label.option").filter(has_text="deye4").locator("input")
        await target4.uncheck()
        await expect(dialog.locator("section.target")).to_have_count(2)
        execute = dialog.get_by_role("button", name="Übertragen", exact=True)
        await expect(execute).to_be_enabled()
        gate = Gate()
        await s.page.route("**/api/transfer", gate.handler)
        await execute.click()
        await gate.first()
        # while the call runs, nothing in the dialog can be changed any more
        await expect(target4).to_be_disabled()
        gate.release.set()
        await expect(s.page.locator(".toast.ok")).to_be_visible()
        assert stack.plant.manager.selection("deye2").features.battery is False
        assert stack.plant.manager.selection("deye3").features.battery is False
        assert stack.plant.manager.selection("deye4").features.battery is True


async def test_older_preview_answer_never_replaces_a_newer_one(
    browser: Browser, tmp_path: Any
) -> None:
    async with four_devices(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        gate = Gate()
        await s.page.route("**/api/transfer/preview", gate.handler)
        dialog = await open_transfer(s)
        await gate.first()
        await (
            dialog.locator("fieldset label.option")
            .filter(has_text="deye2")
            .locator("input")
            .uncheck()
        )
        await expect(dialog.get_by_role("button", name="Übertragen", exact=True)).to_be_disabled()
        gate.release.set()
        await s.page.wait_for_timeout(300)
        await expect(dialog.locator("section.target")).to_have_count(2)  # never the older three
        await expect(dialog.get_by_role("button", name="Übertragen", exact=True)).to_be_enabled()


# -- R7-03 ----------------------------------------------------------------------------------


async def hold_save(page: Page) -> Gate:
    gate = Gate()
    await page.route("**/api/inverters/deye1/sensors", gate.handler)
    return gate


async def test_save_keeps_edits_made_while_it_is_in_flight(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        await row(page, "grid_voltage_a").locator("input").uncheck()
        gate = await hold_save(page)
        save = page.get_by_role("button", name="Speichern", exact=True)
        await save.click()
        await gate.first()
        await expect(save).to_be_disabled()  # still busy, not re-enabled by a repaint
        await row(page, "grid_voltage_b").locator("input").uncheck()
        await expect(save).to_be_disabled()
        gate.release.set()
        await expect(page.locator(".toast.ok")).to_be_visible()
        # the later edit is neither lost nor claimed as saved
        await expect(row(page, "grid_voltage_b").locator("input")).not_to_be_checked()
        selection = stack.plant.manager.selection("deye1")
        assert "grid_voltage_a" in selection.removed
        assert "grid_voltage_b" not in selection.removed
        await expect(save).to_be_enabled()
        await page.unroute("**/api/inverters/deye1/sensors")
        await save.click()
        await expect(page.locator(".toast.ok").nth(1)).to_be_visible()
        assert "grid_voltage_b" in stack.plant.manager.selection("deye1").removed
        await s.check()


async def test_reverting_a_sensor_while_saving_is_kept(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        box = row(page, "grid_voltage_a").locator("input")
        await box.uncheck()
        gate = await hold_save(page)
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await gate.first()
        await box.check()  # takes the very sensor back that is being saved
        gate.release.set()
        await expect(page.locator(".toast.ok")).to_be_visible()
        await expect(box).to_be_checked()
        await expect(page.get_by_role("button", name="Speichern", exact=True)).to_be_enabled()
        await page.unroute("**/api/inverters/deye1/sensors")
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok").nth(1)).to_be_visible()
        assert "grid_voltage_a" not in stack.plant.manager.selection("deye1").removed


async def test_feature_answer_while_saving_is_kept(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        parallel = page.locator('fieldset[data-feature="parallel"]')
        await parallel.get_by_label("Ja", exact=False).first.check()
        gate = await hold_save(page)
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await gate.first()
        await parallel.get_by_label("Nein", exact=False).first.check()
        gate.release.set()
        await expect(page.locator(".toast.ok")).to_be_visible()
        assert stack.plant.manager.selection("deye1").features.parallel is True
        await expect(parallel.get_by_label("Nein", exact=False).first).to_be_checked()
        await expect(page.get_by_role("button", name="Speichern", exact=True)).to_be_enabled()


# -- R7-04 ----------------------------------------------------------------------------------


async def test_explicit_default_answer_survives_reloading_suggestions(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path) as stack:  # R111=2 suggests "no battery"
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        battery = page.locator('fieldset[data-feature="battery"]')
        await expect(battery.get_by_label("Nein, ohne Akku")).to_be_checked()
        await battery.get_by_label("Ja, mit Akku").check()  # equals the stored default
        save = page.get_by_role("button", name="Speichern", exact=True)
        await expect(save).to_be_enabled()
        await save.click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        selection = stack.plant.manager.selection("deye1")
        assert selection.features.battery is True and "battery" in selection.confirmed
        await page.reload()
        await expect(page.locator("section.step").first).to_be_visible()
        await expect(battery.get_by_label("Ja, mit Akku")).to_be_checked()
        await expect(battery).to_contain_text("Die Geräteeinstellung meldet")
        assert stack.plant.manager.selection("deye1").features.battery is True


async def test_unconfirmed_features_are_still_prefilled_and_saved_as_confirmed(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        battery = page.locator('fieldset[data-feature="battery"]')
        await expect(battery.get_by_label("Nein, ohne Akku")).to_be_checked()
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        selection = stack.plant.manager.selection("deye1")
        assert selection.features.battery is False and "battery" in selection.confirmed
        await page.reload()
        await expect(battery.get_by_label("Nein, ohne Akku")).to_be_checked()


async def test_four_island_devices_layout_and_selection(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(
        tmp_path,
        endpoints=[ep(f"g{i}", host=f"192.0.2.{i}") for i in range(1, 5)],
        inverters=[
            inverter(iid, f"g{i}", name="Master" if i == 1 else f"Slave {i - 1}")
            for i, iid in enumerate(IDS, 1)
        ],
        banks={f"g{i}": device_regs(f"240123456{i}", r336=3 if i == 1 else 1) for i in range(1, 5)},
    ) as stack:
        features: dict[str, bool | str] = {
            "utility_grid": False,
            "grid_port_use": "unused",
            "battery": True,
            "bms": True,
            "gen_port_mode": "unused",
            "parallel": True,
        }
        await stack.plant.manager.change(0, {i: Edit(features=features) for i in IDS})
        s = await open_ui(browser, stack, width=1280, height=1000)
        page = s.page
        await expect(page.locator("article.inverter")).to_have_count(4)
        SHOTS.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(SHOTS / "four-island-overview-de.png"))
        await open_setup(s)
        await expect(page.get_by_label("Nichts – Inselbetrieb")).to_be_checked()
        await expect(row(page, "grid_voltage_a").locator("input")).not_to_be_checked()
        await expect(row(page, "load_voltage_a").locator("input")).to_be_checked()
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(SHOTS / "four-island-setup-phone-de.png"))
        await s.check()


# -- N4-02 ----------------------------------------------------------------------------------


async def test_open_maintenance_confirmation_keeps_its_original_action_counter(
    browser: Browser, tmp_path: Any
) -> None:
    import aiohttp

    async with ui_stack(tmp_path) as stack:
        s = await open_ui(browser, stack)
        page = s.page
        await page.locator("details.maintenance > summary").click()
        await page.get_by_role("button", name="Identität neu binden …").click()
        dialog = page.locator("dialog[open]")
        await dialog.get_by_role("textbox").fill("deye1")
        original = stack.api.action_counter
        epoch = stack.plant.rig.inv("deye1").lifecycle_epoch
        # another session changes store state while this confirmation stays open
        async with aiohttp.ClientSession() as http:
            async with http.get(stack.url + "api/session") as res:
                session = await res.json()
            async with http.post(
                stack.url + "api/stores/reset",
                json={"kind": "energy", "scope": "inverter", "inverter": "deye2", "confirm": True},
                headers={"X-CSRF-Token": session["csrf"], "If-Match": f'"{original}"'},
            ) as res:
                assert res.status == 200
        assert stack.api.action_counter > original
        replies: list[int] = []
        page.on(
            "response",
            lambda r: replies.append(r.status) if r.url.endswith("/deye1/rebind") else None,
        )
        await dialog.get_by_role("button", name="Neu binden", exact=True).click()
        await page.wait_for_timeout(500)
        assert replies == [409], replies  # no silent retry with a fresh counter
        assert stack.plant.rig.inv("deye1").lifecycle_epoch == epoch
        await expect(dialog).to_be_visible()
        await expect(dialog).to_contain_text("Der Zustand hat sich")
        await expect(dialog.get_by_role("button", name="Neu binden", exact=True)).to_be_disabled()
        s.expected.append("409")
        await s.check()


# -- N5-04: edits while the follow-up GET after a successful save is running ----------------------


async def hold_refresh(page: Page) -> Gate:
    gate = Gate()
    await page.route("**/api/inverters", gate.handler)
    return gate


async def test_edit_during_post_save_refresh_is_preserved(browser: Browser, tmp_path: Any) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        first = row(page, "grid_voltage_a").locator("input")
        await first.uncheck()
        gate = await hold_refresh(page)
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await gate.first()
        await first.check()  # the POST succeeded, the stored state is not loaded yet
        gate.release.set()
        await expect(page.locator(".toast.ok")).to_be_visible()
        assert "grid_voltage_a" in stack.plant.manager.selection("deye1").removed
        await expect(first).to_be_checked()
        save = page.get_by_role("button", name="Speichern", exact=True)
        await expect(save).to_be_enabled()  # the late edit is a pending draft, not "all saved"
        await page.unroute("**/api/inverters")
        await save.click()
        await expect(page.locator(".toast.ok").nth(1)).to_be_visible()
        assert "grid_voltage_a" not in stack.plant.manager.selection("deye1").removed


async def test_feature_edit_during_post_save_refresh_is_preserved(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        parallel = page.locator('fieldset[data-feature="parallel"]')
        await parallel.get_by_label("Ja", exact=False).first.check()
        gate = await hold_refresh(page)
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await gate.first()
        await parallel.get_by_label("Nein", exact=False).first.check()  # back to the old value
        gate.release.set()
        await expect(page.locator(".toast.ok")).to_be_visible()
        assert stack.plant.manager.selection("deye1").features.parallel is True
        await expect(parallel.get_by_label("Nein", exact=False).first).to_be_checked()
        await expect(page.get_by_role("button", name="Speichern", exact=True)).to_be_enabled()


async def test_a_failing_refresh_after_a_successful_save_keeps_later_edits(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        first = row(page, "grid_voltage_a").locator("input")
        await first.uncheck()
        gate = Gate()

        async def failing(route: Route) -> None:
            await route.fetch()
            gate.entered.append(asyncio.Event())
            await gate.release.wait()
            await route.abort()

        await page.route("**/api/inverters", failing)
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await gate.first()
        await row(page, "grid_voltage_b").locator("input").uncheck()
        gate.release.set()
        await expect(page.locator(".banner.error")).to_be_visible()
        assert "grid_voltage_a" in stack.plant.manager.selection("deye1").removed
        await expect(row(page, "grid_voltage_b").locator("input")).not_to_be_checked()
        await expect(page.get_by_role("button", name="Speichern", exact=True)).to_be_enabled()
        s.errors.clear()  # the aborted request is the point of this test


async def test_language_switch_while_the_refresh_is_running_keeps_the_draft(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        first = row(page, "grid_voltage_a").locator("input")
        await first.uncheck()
        gate = await hold_refresh(page)
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await gate.first()
        await row(page, "grid_voltage_b").locator("input").uncheck()
        await page.locator('button.seg[lang="en"]').click()
        gate.release.set()
        await expect(page.locator(".toast.ok")).to_be_visible()
        await expect(row(page, "grid_voltage_b").locator("input")).not_to_be_checked()
        await page.unroute("**/api/inverters")
        await page.get_by_role("button", name="Save", exact=True).click()
        await expect(page.locator(".toast.ok").nth(1)).to_be_visible()
        assert "grid_voltage_b" in stack.plant.manager.selection("deye1").removed


# -- N6-03: a counter-edit after a failed reload of the saved state ------------------------------


async def fail_refresh(page: Page) -> None:
    async def fail(route: Route) -> None:
        await route.fulfill(status=500, json={"error": {"key": "internal_error", "params": {}}})

    await page.route("**/api/inverters", fail)


async def test_reverting_after_a_failed_saved_state_refresh_is_still_an_edit(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        box = row(page, "grid_voltage_a").locator("input")
        await box.uncheck()
        await fail_refresh(page)
        save = page.get_by_role("button", name="Speichern", exact=True)
        await save.click()
        await expect(page.locator(".banner.error")).to_be_visible()
        assert "grid_voltage_a" in stack.plant.manager.selection("deye1").removed  # saved
        await box.check()  # the base shown is still the OLD one: this is a real edit
        await expect(save).to_be_enabled()
        await page.unroute("**/api/inverters")
        await save.click()
        await expect(page.locator(".toast.ok").first).to_be_visible()
        assert "grid_voltage_a" not in stack.plant.manager.selection("deye1").removed
        await expect(box).to_be_checked()
        s.errors.clear()  # the provoked 500 is the point of this test


async def test_feature_counter_edit_after_a_failed_refresh_is_kept_and_a_later_poll_heals(
    browser: Browser, tmp_path: Any
) -> None:
    async with ui_stack(tmp_path, running=False) as stack:
        s = await open_ui(browser, stack)
        await open_setup(s)
        page = s.page
        parallel = page.locator('fieldset[data-feature="parallel"]')
        await parallel.get_by_label("Ja", exact=False).first.check()
        await fail_refresh(page)
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".banner.error")).to_be_visible()
        assert stack.plant.manager.selection("deye1").features.parallel is True
        await parallel.get_by_label("Nein", exact=False).first.check()  # back to the old value
        save = page.get_by_role("button", name="Speichern", exact=True)
        await expect(save).to_be_enabled()
        await save.click()  # another failing refresh does not lose the edit either
        await expect(page.locator(".banner.error")).to_be_visible()
        assert stack.plant.manager.selection("deye1").features.parallel is False
        s.errors.clear()
