# SPDX-License-Identifier: EUPL-1.2
"""Accessibilité automatique (axe) : aucune violation grave ou critique, dans les deux thèmes."""

from __future__ import annotations

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser

from .conftest import ADMIN, Server

pytestmark = pytest.mark.e2e

CONNECTED = (
    "/",
    "/compte",
    "/admin/utilisateurs",
    "/admin/utilisateurs/nouveau",
    "/_composants",
    "/a-venir/statistiques",
    "/emplacements",
    "/lecteurs",
    "/lecteurs?vue=foyers",
)
BLOCKING = {"serious", "critical"}


def _violations(page) -> list[str]:
    results = Axe().run(page).response["violations"]
    return [
        f"{violation['id']} ({violation['impact']}) : {node['target']}"
        for violation in results
        if violation["impact"] in BLOCKING
        for node in violation["nodes"]
    ]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1440])
def test_connected_screens(browser: Browser, server: Server, theme: str, width: int) -> None:
    context = browser.new_context(
        storage_state=server.storage[ADMIN],
        bypass_csp=True,
        viewport={"width": width, "height": 900},
        color_scheme=theme,
    )
    page = context.new_page()
    problems = {}
    for path in CONNECTED:
        page.goto(f"{server.base_url}{path}")
        page.wait_for_load_state("networkidle")
        found = _violations(page)
        if found:
            problems[path] = found
    context.close()
    assert problems == {}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_anonymous_screens(browser: Browser, server: Server, theme: str) -> None:
    context = browser.new_context(bypass_csp=True, color_scheme=theme)
    page = context.new_page()
    problems = {}
    for path in ("/connexion", "/a-propos", "/nexiste-pas"):
        page.goto(f"{server.base_url}{path}")
        page.wait_for_load_state("networkidle")
        found = _violations(page)
        if found:
            problems[path] = found
    context.close()
    assert problems == {}


def test_install_and_maintenance_screens(browser: Browser, empty_server: Server) -> None:
    context = browser.new_context(bypass_csp=True)
    page = context.new_page()
    page.goto(f"{empty_server.base_url}/")
    assert _violations(page) == []
    assert empty_server.biblyo("db", "upgrade").returncode == 0
    page.wait_for_timeout(2100)
    page.goto(f"{empty_server.base_url}/installation")
    assert page.locator("#jeton").is_visible()
    assert _violations(page) == []
    context.close()
