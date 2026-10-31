"""Browser tests of the plant setup (T14, ``-m ui``): assistant, connection and inverter forms with
their drop-downs, the read-only connection check and the live changes behind them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import pytest
from playwright.async_api import Browser, Page, expect

from deye_monitor.device import InverterState
from tests.fakes.servers import Behavior, FakePty
from tests.plant_helpers import A, B, PlantRig, plant_ui, serial_symlink
from tests.test_ui import CSP_PROBE, SHOTS, Session, browser, shot  # noqa: F401

pytestmark = pytest.mark.ui

GW = {"id": "gw1", "transport": "modbus_tcp", "host": "192.0.2.10", "port": 502}


async def open_page(
    browser: Browser,
    url: str,
    *,
    lang: str = "de-DE",
    width: int = 1100,
    scheme: Literal["light", "dark"] = "light",
) -> Session:
    context = await browser.new_context(
        locale=lang, color_scheme=scheme, viewport={"width": width, "height": 900}
    )
    await context.add_init_script(CSP_PROBE)
    page = await context.new_page()
    session = Session(page)
    await page.goto(url)
    return session


async def seed(rig: PlantRig, *, inverter: bool = True, second_gateway: bool = False) -> None:
    await rig.add_server("gw1", "modbus_tcp")
    status, body = await rig.call("POST", "/api/plant/endpoints", rig.endpoint_body("gw1"))
    assert status == 200, body
    if second_gateway:
        await rig.add_server("gw2", "modbus_tcp", B)
        status, body = await rig.call("POST", "/api/plant/endpoints", rig.endpoint_body("gw2"))
        assert status == 200, body
    if inverter:
        status, body = await rig.call(
            "POST",
            "/api/plant/inverters",
            {"id": "deye1", "name": "Deye 1", "endpoint": "gw1", "unit_id": 1},
        )
        assert status == 200, body


async def pick_transport(page: Page, transport: str) -> None:
    await page.locator(f'label.transport-option[data-transport="{transport}"]').click()


def field(page: Page, name: str) -> Any:
    return page.locator(f'dialog [data-field="{name}"], .wizard [data-field="{name}"]').locator(
        "input, select"
    )


async def test_the_assistant_leads_from_an_empty_plant_to_the_sensor_selection(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        server = await rig.add_server("gw1", "modbus_tcp")
        s = await open_page(browser, url)
        page = s.page
        # an empty plant starts the assistant by itself
        await expect(page.locator(".stepper li.current")).to_contain_text("Verbindung")
        await expect(page.locator("nav.app-nav")).to_be_visible()
        # the transport is a choice with an explanation; nothing is chosen for the user
        await expect(page.locator("label.transport-option")).to_have_count(4)
        await expect(page.locator(".transport-option").first).to_contain_text("Port 502")
        await expect(page.locator("#wiz-next")).to_be_visible()
        await shot(page, "plant-wizard-1-de")
        await pick_transport(page, "modbus_tcp")
        await field(page, "host").fill("127.0.0.1")
        await field(page, "port").fill(str(server.port))
        await page.locator("details.advanced summary").first.click()
        await field(page, "timeout_s").fill("1")
        await field(page, "request_gap_ms").fill("0")
        await page.locator("#wiz-next").click()
        # step 2: the connection is a drop-down of the connections that exist now
        await expect(page.locator(".stepper li.current")).to_contain_text("Wechselrichter")
        select = page.locator('[data-field="endpoint"] select')
        await expect(select.locator("option")).to_have_count(2)  # "—" and gw1
        await expect(select).to_have_value("gw1")
        await expect(field(page, "unit_id")).to_have_value("1")  # the default
        await field(page, "name").fill("Deye Keller")
        await expect(field(page, "id")).to_have_value("deye_keller")  # proposed from the name
        await expect(page.locator(".writes-note")).to_contain_text("nur")
        await shot(page, "plant-wizard-2-de")
        await page.locator("#wiz-next").click()
        # step 3: the read-only check runs by itself and shows the serial number
        await expect(page.locator(".stepper li.current")).to_contain_text("Prüfen")
        result = page.locator(".test-result.ok")
        await expect(result).to_contain_text("Der Wechselrichter antwortet")
        await expect(result).to_contain_text(A)
        await expect(result).to_contain_text("2 MPPT")
        await shot(page, "plant-wizard-3-de")
        await page.locator("#wiz-next").click()
        await expect(page.locator("#wiz-sensors")).to_be_visible()
        await shot(page, "plant-wizard-4-de")
        await page.locator("#wiz-sensors").click()
        # ... straight into the sensor selection of the new inverter (it waits for a choice)
        await expect(page.locator(".banner.awaiting")).to_be_visible()
        await page.locator('button[data-quick="basic"]').click()
        await page.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        sel = rig.manager.selection("deye_keller")
        assert sel.chosen and len(sel.effective) == 6
        assert rig.service.plant.inverters[0].id == "deye_keller"
        await rig.wait_state("deye_keller", InverterState.RUNNING)
        await s.check()


async def test_the_transport_choice_shows_only_the_fields_that_belong_to_it(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pty = FakePty()
    pty.start()
    try:
        by_id = serial_symlink(tmp_path, pty)
        async with plant_ui(tmp_path, monkeypatch, serial_dir=by_id) as (rig, url):
            await seed(rig, inverter=False)
            s = await open_page(browser, url + "#/plant")
            page = s.page
            await expect(page.locator("article.endpoint")).to_have_count(1)
            await page.locator("#add-endpoint").click()
            dialog = page.locator("dialog")
            await expect(dialog.locator(".transport-fields")).to_be_hidden()  # nothing chosen yet
            await pick_transport(page, "modbus_tcp")
            await expect(field(page, "host")).to_be_visible()
            await expect(field(page, "port")).to_have_value("502")
            await expect(dialog.locator('[data-field="logger_serial"]')).to_be_hidden()
            await expect(dialog.locator('[data-field="serial_device"]')).to_be_hidden()
            await expect(field(page, "id")).to_have_value("gw2")  # gw1 exists
            await pick_transport(page, "solarman_v5_tcp")
            await expect(field(page, "port")).to_have_value("8899")
            await expect(dialog.locator('[data-field="logger_serial"]')).to_be_visible()
            await expect(dialog.locator('[data-field="logger_serial"] .help')).to_contain_text(
                "nicht die des Wechselrichters"
            )
            await expect(field(page, "id")).to_have_value("logger1")
            await pick_transport(page, "rtu_over_tcp")
            await expect(field(page, "port")).to_have_value("502")
            await pick_transport(page, "serial_rtu")
            await expect(dialog.locator('[data-field="host"]')).to_be_hidden()
            device = dialog.locator('[data-field="serial_device"] select')
            await expect(device.locator("option")).to_have_count(2)  # the prompt + the one device
            await expect(device.locator("option").nth(1)).to_have_text(f"{by_id}/usb-test-if00")
            await expect(field(page, "baudrate")).to_have_value("9600")
            await shot(page, "plant-endpoint-serial-de")
            # saving without a device is refused on the spot, with the field marked
            await dialog.get_by_role("button", name="Speichern", exact=True).click()
            await expect(
                dialog.locator('[data-field="serial_device"] .field-error')
            ).to_be_visible()
            assert rig.service.revision == 1
            await device.select_option(f"{by_id}/usb-test-if00")
            await dialog.get_by_role("button", name="Speichern", exact=True).click()
            await expect(page.locator("article.endpoint")).to_have_count(2)
            assert rig.service.revision == 2
            assert rig.service.plant.endpoint("usb1").serial_device == f"{by_id}/usb-test-if00"
            await s.check()
    finally:
        pty.close()


async def test_the_plant_page_edits_and_deletes_with_dropdowns_and_guards(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig, second_gateway=True)
        await rig.wait_state("deye1", InverterState.RUNNING)
        s = await open_page(browser, url)
        page = s.page
        await expect(page.locator("article.inverter")).to_have_count(1)
        await page.locator("#nav-plant").click()
        await expect(page.locator("article.plant-item.inverter")).to_have_count(1)
        await shot(page, "plant-page-light-de")
        # a connection that an inverter uses cannot be deleted
        used = page.locator('article.endpoint[data-endpoint="gw1"]')
        await expect(used.get_by_role("button", name="Löschen")).to_be_disabled()
        await expect(used).to_contain_text("deye1")
        # edit the inverter: both connection drop-downs offer both connections
        card = page.locator('article.inverter[data-inverter="deye1"]')
        await card.get_by_role("button", name="Bearbeiten").click()
        dialog = page.locator("dialog")
        await expect(dialog.locator('[data-field="endpoint"] option')).to_have_count(3)
        await expect(dialog.locator('[data-field="fallback_endpoint"] option')).to_have_count(3)
        await expect(field(page, "id")).to_have_attribute("readonly", "")  # immutable
        await expect(dialog.locator('[data-field="fallback_unit_id"]')).to_be_hidden()
        await field(page, "name").fill("Keller")
        await dialog.locator('[data-field="fallback_endpoint"] select').select_option("gw2")
        await expect(dialog.locator('[data-field="fallback_unit_id"]')).to_be_visible()
        await expect(field(page, "fallback_unit_id")).to_have_value("1")
        await shot(page, "plant-inverter-dialog-de")
        await dialog.get_by_role("button", name="Speichern", exact=True).click()
        await expect(card).to_contain_text("Keller")
        await expect(card).to_contain_text("gw2 · 1")
        inverter = rig.service.plant.inverters[0]
        assert (inverter.name, inverter.fallback_endpoint, inverter.fallback_unit_id) == (
            "Keller",
            "gw2",
            1,
        )
        assert (
            rig.app.runtime is not None and rig.app.runtime.inverters["deye1"].cfg.name == "Keller"
        )
        # the connection check of a saved inverter goes through its running connection
        await card.get_by_role("button", name="Verbindung prüfen").click()
        await page.locator("dialog .test-panel button").click()
        # primary and fallback are both checked, one result block per route
        await expect(page.locator("dialog .test-route")).to_have_count(2)
        await expect(
            page.locator("dialog .test-route").nth(0).locator(".test-result.ok")
        ).to_contain_text(A)
        await expect(page.locator("dialog .test-route").nth(1)).to_contain_text(
            "Ersatzverbindung: gw2"
        )
        await expect(page.locator("dialog .test-result").nth(0)).to_contain_text(
            "laufende Verbindung"
        )
        await page.locator("dialog .modal-body > .actions .btn").click()
        # delete the inverter (the binding stays unless the box is ticked)
        await card.get_by_role("button", name="Löschen").click()
        await expect(page.locator("dialog")).to_contain_text("Seriennummer-Bindung")
        await page.locator("dialog").get_by_role("button", name="Wechselrichter löschen").click()
        await expect(page.locator("article.plant-item.inverter")).to_have_count(0)
        assert rig.service.plant.inverters == ()
        # now the connections can go, one by one
        for endpoint in ("gw2", "gw1"):
            card = page.locator(f'article.endpoint[data-endpoint="{endpoint}"]')
            await card.get_by_role("button", name="Löschen").click()
            await page.locator("dialog").get_by_role("button", name="Verbindung löschen").click()
            await expect(card).to_have_count(0)
        assert rig.service.plant.endpoints == ()
        await s.check()


async def test_a_wrong_transport_is_explained_by_the_check(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        server = await rig.add_server("gw1", "rtu_over_tcp")  # a transparent gateway
        s = await open_page(browser, url + "#/plant")
        page = s.page
        await page.locator("#add-endpoint").click()
        await pick_transport(page, "modbus_tcp")  # ... but "Modbus TCP" is chosen
        await field(page, "host").fill("127.0.0.1")
        await field(page, "port").fill(str(server.port))
        await page.locator("dialog details.advanced summary").click()
        await field(page, "timeout_s").fill("1")
        await page.locator("dialog .test-panel button").click()
        fail = page.locator("dialog .test-result.fail")
        await expect(fail).to_contain_text("Keine brauchbare Antwort", timeout=15000)
        await expect(fail).to_contain_text("Falscher Transport")
        await shot(page, "plant-check-fail-de")
        assert rig.service.revision == 0  # a check changes nothing
        await s.check()


async def test_an_unreachable_host_is_reported_as_such(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (_rig, url):
        s = await open_page(browser, url + "#/plant")
        page = s.page
        await page.locator("#add-endpoint").click()
        await pick_transport(page, "solarman_v5_tcp")
        await field(page, "host").fill("127.0.0.1")
        await field(page, "port").fill("1")
        await field(page, "logger_serial").fill("1234567890")
        await page.locator("dialog details.advanced summary").click()
        await field(page, "timeout_s").fill("1")
        await page.locator("dialog .test-panel button").click()
        await expect(page.locator("dialog .test-result.fail")).to_contain_text(
            "nicht erreichbar", timeout=15000
        )
        await s.check()


async def test_server_findings_mark_the_field(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig)
        s = await open_page(browser, url + "#/plant")
        s.expected.append("422")  # the refused saves are provoked on purpose
        page = s.page
        await page.get_by_role("button", name="Wechselrichter hinzufügen").click()
        await field(page, "name").fill("Second")
        await field(page, "id").fill("deye2")
        await dialog_select(page, "endpoint", "gw1")  # unit 1 on gw1 is taken by deye1
        await page.locator("dialog").get_by_role("button", name="Speichern", exact=True).click()
        message = page.locator("dialog .messages .banner")
        await expect(message).to_contain_text("dieselbe Modbus-Adresse 1")
        assert len(rig.service.plant.inverters) == 1
        await field(page, "unit_id").fill("0")
        await page.locator("dialog").get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator('dialog [data-field="unit_id"] .field-error')).to_be_visible()
        await s.check()


async def dialog_select(page: Page, name: str, value: str) -> None:
    await page.locator(f'dialog [data-field="{name}"] select').select_option(value)


async def test_english_and_dark_mode_and_phone_width(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig)
        s = await open_page(browser, url + "#/plant", lang="en-US", width=390, scheme="dark")
        page = s.page
        await expect(page.locator("h1")).to_have_text("Set up plant")
        await expect(page.get_by_role("button", name="Add connection")).to_be_visible()
        overflow = await page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        assert not overflow
        await shot(page, "plant-page-phone-dark-en")
        await page.get_by_role("button", name="Add connection").click()
        await expect(page.locator("dialog")).to_contain_text("How is the inverter connected?")
        overflow = await page.evaluate(
            "(d => d.scrollWidth > d.clientWidth)(document.querySelector('dialog'))"
        )
        assert not overflow
        await s.check()


async def test_the_dialog_is_keyboard_operable(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig, inverter=False)
        s = await open_page(browser, url + "#/plant")
        page = s.page
        await page.locator("#add-endpoint").focus()
        await page.keyboard.press("Enter")
        await expect(page.locator("dialog")).to_be_visible()
        await page.keyboard.press("Escape")
        await expect(page.locator("dialog")).to_have_count(0)
        await expect(page.locator("#add-endpoint")).to_be_focused()
        await s.check()


async def test_without_a_home_assistant_user_the_plant_is_read_only(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch, user=False) as (rig, url):
        await seed(rig)
        s = await open_page(browser, url + "#/plant")
        page = s.page
        await expect(page.locator("#add-endpoint")).to_be_disabled()
        await expect(
            page.locator("article.inverter").get_by_role("button", name="Löschen")
        ).to_be_disabled()
        await expect(page.locator(".banner.info")).to_be_visible()
        await s.check()


async def test_an_unusable_r65_asks_for_the_energy_resolution_and_the_choice_is_saved(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await rig.add_server("gw1", "modbus_tcp", A, r65=0xFFFF, r0=0xFFFF, r22=0xFFFF, r24=0xFFFF)
        status, body = await rig.call("POST", "/api/plant/endpoints", rig.endpoint_body("gw1"))
        assert status == 200, body
        status, body = await rig.call(
            "POST",
            "/api/plant/inverters",
            {"id": "deye1", "name": "Deye 1", "endpoint": "gw1", "unit_id": 1},
        )
        assert status == 200, body
        status, body = await rig.call(
            "POST",
            "/api/inverters/deye1/sensors",
            {"choose": True},
            revision=rig.manager.revision,
        )
        assert status == 200, body
        await rig.wait_state("deye1", InverterState.RUNNING)
        s = await open_page(browser, url)
        card = s.page.locator("article.inverter").first
        await expect(card).to_contain_text("keine gültige Energieauflösung")
        await expect(card).to_contain_text("Hardwareangaben vom Gerät nicht gemeldet")
        await expect(card.locator(".hint-detail")).to_have_count(0)  # not reported is no conflict
        # the connection check does not show the raw 0xFFFF as 255 MPPT / 255 phases
        await s.page.locator("#nav-plant").click()
        await (
            s.page.locator("article.plant-item.inverter")
            .get_by_role("button", name="Verbindung prüfen")
            .click()
        )
        await s.page.locator("dialog .test-panel button").click()
        result = s.page.locator("dialog .test-result.ok")
        await expect(result).to_contain_text(
            "MPPT/Phasen nicht gemeldet · laut Profil 2 MPPT · 3 Phasen"
        )
        await expect(result).not_to_contain_text("255")
        await s.page.locator("dialog .modal-body > .actions .btn").click()
        await s.page.locator("a.brand").click()
        await card.get_by_role("button", name="0,1 kWh wählen").click()
        await expect(s.page.locator(".toast.ok")).to_be_visible()
        assert rig.service.plant.inverters[0].energy_resolution.value == "0.1"


async def test_the_identifier_of_an_existing_connection_and_inverter_is_visibly_locked(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig)
        page = (await open_page(browser, url + "#/plant")).page
        dialog = page.locator("dialog")
        edits = (
            page.locator('article.endpoint[data-endpoint="gw1"]'),
            page.locator('article.inverter[data-inverter="deye1"]'),
        )
        for card in edits:
            await card.get_by_role("button", name="Bearbeiten").click()
            ident = field(page, "id")
            await expect(ident).to_have_attribute("readonly", "")
            await expect(ident).to_have_attribute("aria-readonly", "true")
            await expect(ident).to_have_class(re.compile(r"\blocked\b"))
            await expect(dialog.locator('[data-field="id"] .help')).to_contain_text("🔒")
            await expect(dialog.locator('[data-field="id"] .help')).to_contain_text(
                "nicht mehr ändern"
            )
            await dialog.get_by_role("button", name="Abbrechen").click()


async def test_the_modbus_address_has_one_place_and_follows_the_connection_type(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig, inverter=False)
        await rig.add_server("lg1", "solarman_v5_tcp")
        status, body = await rig.call(
            "POST", "/api/plant/endpoints", rig.endpoint_body("lg1", "solarman_v5_tcp")
        )
        assert status == 200, body
        page = (await open_page(browser, url + "#/plant")).page
        await page.get_by_role("button", name="Wechselrichter hinzufügen").click()
        dialog = page.locator("dialog")
        fixed = dialog.locator('[data-fixed="unit_id"]')
        unit = dialog.locator('[data-field="unit_id"]')
        await field(page, "name").fill("Slave")

        await dialog_select(page, "endpoint", "gw1")  # a gateway: the address is the inverter's
        await expect(unit).to_be_visible()
        await expect(unit).to_contain_text("Modbus SN")
        await expect(fixed).to_be_hidden()
        await field(page, "unit_id").fill("3")

        await dialog_select(page, "endpoint", "lg1")  # a logger: exactly one inverter, address 1
        await expect(unit).to_be_hidden()
        await expect(fixed).to_be_visible()
        await expect(fixed).to_have_text("Adresse: 1 (fest bei Solarman-Loggern)")

        await dialog_select(page, "endpoint", "gw1")
        await field(page, "unit_id").fill("3")
        await dialog_select(page, "fallback_endpoint", "lg1")
        fallback_fixed = dialog.locator('[data-fixed="fallback_unit_id"]')
        await expect(fallback_fixed).to_be_visible()  # never copied from the primary
        await expect(dialog.locator('[data-field="fallback_unit_id"]')).to_be_hidden()

        await dialog.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        saved = rig.service.plant.inverters[0]
        assert (saved.unit_id, saved.fallback_endpoint, saved.fallback_unit_id) == (3, "lg1", 1)


async def test_a_logger_address_other_than_one_is_an_advanced_override(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await rig.add_server("lg1", "solarman_v5_tcp")
        status, body = await rig.call(
            "POST", "/api/plant/endpoints", rig.endpoint_body("lg1", "solarman_v5_tcp")
        )
        assert status == 200, body
        page = (await open_page(browser, url + "#/plant")).page
        await page.get_by_role("button", name="Wechselrichter hinzufügen").click()
        dialog = page.locator("dialog")
        await field(page, "name").fill("Slave")
        await dialog_select(page, "endpoint", "lg1")
        override = dialog.locator('[data-field="unit_override"]')
        await expect(override).to_be_hidden()  # inside the closed "Advanced" section
        await dialog.locator("details.advanced summary").click()
        await expect(override).to_be_visible()
        await field(page, "unit_override").fill("2")  # the fake logger serves unit 1 only
        await dialog.get_by_role("button", name="Verbindung prüfen").click()
        result = dialog.locator(".test-result.fail")
        await expect(result).to_be_visible()
        await expect(result.locator(".hint.warn")).to_contain_text("nur auf Adresse 1")
        await dialog.get_by_role("button", name="Speichern", exact=True).click()
        await expect(page.locator(".toast.ok")).to_be_visible()
        assert rig.service.plant.inverters[0].unit_id == 2


async def test_the_connection_test_shows_no_stored_address(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig)  # deye1 uses gw1 with address 1
        await rig.add_server("lg1", "solarman_v5_tcp")
        status, body = await rig.call(
            "POST", "/api/plant/endpoints", rig.endpoint_body("lg1", "solarman_v5_tcp")
        )
        assert status == 200, body
        page = (await open_page(browser, url + "#/plant")).page
        # a connection that inverters already use: each is tested with its own address
        await (
            page.locator('article.endpoint[data-endpoint="gw1"]')
            .get_by_role("button", name="Bearbeiten")
            .click()
        )
        dialog = page.locator("dialog")
        await expect(dialog.locator('[data-test-address="free"]')).to_be_hidden()
        await expect(dialog.locator('[data-test-address="users"]')).to_contain_text("Deye 1 (1)")
        await dialog.get_by_role("button", name="Verbindung prüfen").click()
        await expect(dialog.locator(".test-route")).to_have_count(1)
        await expect(dialog.locator(".route-title")).to_contain_text("Adresse 1")
        await dialog.get_by_role("button", name="Abbrechen").click()
        # a logger needs no address; a fresh connection offers one that is not saved
        await (
            page.locator('article.endpoint[data-endpoint="lg1"]')
            .get_by_role("button", name="Bearbeiten")
            .click()
        )
        await expect(dialog.locator('[data-test-address="free"]')).to_be_hidden()
        await expect(dialog.locator('[data-test-address="users"]')).to_be_hidden()
        await dialog.get_by_role("button", name="Abbrechen").click()
        await page.get_by_role("button", name="Verbindung hinzufügen").click()
        free = dialog.locator('[data-test-address="free"]')
        await expect(free).to_be_visible()
        await expect(free).to_contain_text("wird nicht gespeichert")


async def test_check_connection_tests_every_route_with_its_own_address(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig, inverter=False)
        await rig.add_server("lg1", "solarman_v5_tcp")
        status, body = await rig.call(
            "POST", "/api/plant/endpoints", rig.endpoint_body("lg1", "solarman_v5_tcp")
        )
        assert status == 200, body
        rig.servers["lg1"].default = Behavior(close=True)  # the fallback logger is broken
        page = (await open_page(browser, url + "#/plant")).page
        await page.get_by_role("button", name="Wechselrichter hinzufügen").click()
        await field(page, "name").fill("Master")
        await dialog_select(page, "endpoint", "gw1")
        await dialog_select(page, "fallback_endpoint", "lg1")
        await page.locator("dialog").get_by_role("button", name="Verbindung prüfen").click()
        routes = page.locator("dialog .test-route")
        await expect(routes).to_have_count(2, timeout=20000)
        await expect(routes.nth(0)).to_contain_text("Hauptverbindung: gw1")
        await expect(routes.nth(0)).to_contain_text("Adresse 1")
        await expect(routes.nth(0).locator(".test-result.ok")).to_be_visible()
        await expect(routes.nth(1)).to_contain_text("Ersatzverbindung: lg1")
        await expect(
            routes.nth(1).locator(".test-result.fail")
        ).to_be_visible()  # visible although the primary works
        await expect(routes.nth(1)).to_contain_text("2 Versuche")


async def no_stray_text(page: Page) -> None:
    await expect(page.locator("#view h1")).to_be_visible()
    text = await page.evaluate("document.body.innerText")
    assert "null" not in text
    assert "undefined" not in text


@pytest.mark.parametrize("endpoints", [False, True])
async def test_the_plant_page_without_inverters_shows_no_stray_null(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoints: bool
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        if endpoints:
            await seed(rig, inverter=False)
        s = await open_page(browser, url + "#/plant")
        await expect(s.page.locator("#add-inverter")).to_be_visible()
        await no_stray_text(s.page)


async def test_the_overview_without_inverters_shows_no_stray_null(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        await seed(rig, inverter=False)
        s = await open_page(browser, url)
        await expect(s.page.locator("section.empty")).to_be_visible()
        await no_stray_text(s.page)
