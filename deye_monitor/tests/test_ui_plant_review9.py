"""Form-state regressions of the plant setup (review 9): saved ports and acknowledged changes."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Browser, expect

from tests.plant_helpers import plant_ui
from tests.test_ui import browser  # noqa: F401
from tests.test_ui_plant import field, open_page, pick_transport

pytestmark = pytest.mark.ui

# (transport, port): every default of one transport used as a deliberate port of another one
PORTS = [
    ("modbus_tcp", 8899),
    ("modbus_tcp", 502),
    ("rtu_over_tcp", 8899),
    ("rtu_over_tcp", 1502),
    ("solarman_v5_tcp", 502),
    ("solarman_v5_tcp", 8899),
]


async def test_editing_keeps_a_saved_port_for_every_transport_and_port(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        for n, (transport, port) in enumerate(PORTS):
            body = {
                "id": f"c{n}",
                "transport": transport,
                "host": f"gw{n}.example.test",
                "port": port,
            }
            if transport == "solarman_v5_tcp":
                body["logger_serial"] = 1234567890
            # no inverter uses these connections: no socket is opened
            status, reply = await rig.call("POST", "/api/plant/endpoints", body)
            assert status == 200, reply
        page = (await open_page(browser, url + "#/plant")).page
        for n, (_transport, port) in enumerate(PORTS):
            card = page.locator(f'article[data-endpoint="c{n}"]')
            await card.get_by_role("button", name="Bearbeiten", exact=True).click()
            await expect(field(page, "port")).to_have_value(str(port))
            await page.locator("dialog").get_by_role("button", name="Abbrechen").click()
        # opening and saving without a change keeps the connection path as it was
        await (
            page.locator('article[data-endpoint="c0"]')
            .get_by_role("button", name="Bearbeiten", exact=True)
            .click()
        )
        await page.locator("dialog .btn.primary").click()
        await expect(page.locator("dialog")).to_have_count(0)
        assert rig.store.plant.endpoint("c0").port == 8899


async def test_a_default_port_follows_a_real_transport_change_only(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (_rig, url):
        page = (await open_page(browser, url)).page
        port = field(page, "port")
        await pick_transport(page, "modbus_tcp")
        await expect(port).to_have_value("502")
        await pick_transport(page, "solarman_v5_tcp")
        await expect(port).to_have_value("8899")  # still the default of the previous transport
        await port.fill("1502")  # a deliberate port stays
        await pick_transport(page, "modbus_tcp")
        await expect(port).to_have_value("1502")


async def test_a_saved_connection_is_not_reported_as_a_failed_save_when_refresh_fails(
    browser: Browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with plant_ui(tmp_path, monkeypatch) as (rig, url):
        page = (await open_page(browser, url)).page
        posts: list[str] = []
        failing = True

        async def route(r: object) -> None:
            request = r.request  # type: ignore[attr-defined]
            if request.method == "GET" and failing:
                await r.fulfill(  # type: ignore[attr-defined]
                    status=500, json={"error": {"key": "internal_error", "params": {}}}
                )
            else:
                if request.method == "POST" and request.url.endswith("/api/plant/endpoints"):
                    posts.append(request.url)
                await r.continue_()  # type: ignore[attr-defined]

        await pick_transport(page, "modbus_tcp")
        await field(page, "host").fill("127.0.0.1")  # no inverter: no socket
        # only now: the periodic refresh of the page would fight with the failing reads
        await page.route("**/api/inverters", route)
        await page.route("**/api/plant/endpoints", route)
        next_button = page.locator("#wiz-next")
        await next_button.click()
        warn = page.locator(".wizard .banner.warn[role=alert]")
        await expect(warn).to_be_visible()
        assert [e.id for e in rig.store.plant.endpoints] == ["gw1"]
        assert rig.service.revision == 1
        await expect(page.locator(".wizard .banner.error")).to_have_count(0)
        # saving again does not send the change again
        await next_button.click()
        await expect(warn).to_be_visible()
        assert len(posts) == 1, posts
        # reading works again: the assistant goes on with the accepted connection
        failing = False
        # the page is still shifting while the periodic refresh fails: no actionability wait
        await page.locator("#plant-reload").dispatch_event("click")
        await expect(page.locator(".stepper li.current")).to_contain_text("Wechselrichter")
        assert len(posts) == 1, posts
