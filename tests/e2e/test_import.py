# SPDX-License-Identifier: EUPL-1.2
"""Import dans un vrai navigateur (lot L6, T3 et T4) : dépôt de deux classeurs, correspondance, simulation, vues.

Les classeurs sont fabriqués (`tests/import_samples.py`) : aucune donnée du Club. Avec `BIBLYO_CAPTURES=<dossier>`,
les écrans sont aussi enregistrés en images, pour le rapport de recette."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect

from .. import import_samples as samples
from .conftest import ADMIN, Server, watch

pytestmark = pytest.mark.e2e
BLOCKING = {"serious", "critical"}
SIZES = {"wide": {"width": 1440, "height": 900}, "phone": {"width": 390, "height": 844}}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CAPTURES = os.environ.get("BIBLYO_CAPTURES", "")


@pytest.fixture(params=tuple(SIZES))
def page(request: pytest.FixtureRequest, browser: Browser, server: Server) -> Iterator[Page]:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport=SIZES[request.param])
    instance = context.new_page()
    instance.size_name = request.param  # type: ignore[attr-defined]
    yield instance
    context.close()


def _accessible(page: Page) -> None:
    for _attempt in range(50):
        if page.evaluate("document.getAnimations().every(item => item.playState === 'finished')"):
            break
        page.wait_for_timeout(20)
    violations = [v for v in Axe().run(page).response["violations"] if v["impact"] in BLOCKING]
    assert not violations, [(v["id"], [n["html"][:160] for n in v["nodes"]]) for v in violations]


def _capture(page: Page, name: str) -> None:
    if CAPTURES:
        folder = Path(CAPTURES)
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f"{name}-{page.size_name}.png"), full_page=True)  # type: ignore[attr-defined]


TEXTS = """() => { const out = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const r = document.createRange(); r.selectNodeContents(w.currentNode);
    const b = r.getBoundingClientRect(); if (b.right > document.documentElement.clientWidth + 1 && b.width > 0)
      out.push(w.currentNode.parentElement.tagName + '.' + w.currentNode.parentElement.className + ' «' +
        w.currentNode.textContent.trim().slice(0, 40) + '» ' + Math.round(b.right)); }
  return out.slice(0, 6); }"""
WIDER = """() => [...document.querySelectorAll('body *')]
  .filter(e => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1 && !e.closest('.tabs'))
  .slice(0, 6).map(e => e.tagName + '.' + e.className + ' ' + Math.round(e.getBoundingClientRect().right))"""


def _no_horizontal_scroll(page: Page) -> None:
    wide = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert wide, (
        page.evaluate(
            "[document.documentElement.scrollWidth, window.innerWidth, document.documentElement.clientWidth]"
        ),
        page.evaluate(WIDER),
        page.evaluate(TEXTS),
    )


def test_deposit_map_and_simulate_two_workbooks(page: Page, server: Server) -> None:
    problems = watch(page)
    page.goto(f"{server.base_url}/imports")
    expect(page.locator("h1")).to_contain_text("Import & export")
    _accessible(page)
    _capture(page, "01-depot")
    page.set_input_files("#principal", {"name": "catalogue.xlsx", "mimeType": XLSX, "buffer": samples.workbook()})
    page.set_input_files(
        "#complementaire", {"name": "enrichi.xlsx", "mimeType": XLSX, "buffer": samples.workbook(enriched=True)}
    )
    page.get_by_role("button", name="Lire le fichier").click()
    expect(page.locator("h1")).to_contain_text("Correspondance des colonnes")
    expect(page.locator("#toasts")).to_contain_text("Fichier lu")
    # Le toast se retire seul après 6 s : fermé ici pour qu'axe ne le mesure pas au moment où il disparaît
    page.locator("#toasts [data-close-toast]").first.click()
    expect(page.locator(".toast")).to_have_count(0)
    expect(page.get_by_text("Colonnes : 15")).to_be_visible()
    expect(page.get_by_text("Arbitré").first).to_be_visible()
    _capture(page, "02-correspondance")
    _accessible(page)
    _no_horizontal_scroll(page)
    page.get_by_label("Destination de la colonne Collection").select_option("keep")
    page.get_by_role("button", name="Enregistrer la correspondance").click()
    expect(page.locator("#toasts")).to_contain_text("correspondance a été enregistrée")
    expect(page.get_by_label("Destination de la colonne Collection")).to_have_value("keep")
    page.get_by_label("Destination de la colonne Collection").select_option("collection")
    page.get_by_role("button", name="Lancer la simulation").click()
    expect(page.locator("h1")).to_contain_text("Simulation de l’import")
    expect(page.get_by_text("Rien n’a été écrit")).to_be_visible()
    expect(page.get_by_text("conforme").first).to_be_visible()
    _accessible(page)
    _no_horizontal_scroll(page)
    _capture(page, "03-simulation-lignes")
    for tab, heading, name in (
        ("Anomalies", "Cotes", "04-simulation-anomalies"),
        ("Différences", "Rita Rive", "05-simulation-differences"),
        ("Colonnes", "ISBN (référence à vérifier)", "06-simulation-colonnes"),
    ):
        page.get_by_role("link", name=tab, exact=True).click()
        expect(page.get_by_text(heading).first).to_be_visible()
        _accessible(page)
        _no_horizontal_scroll(page)
        _capture(page, name)
    page.goto(f"{server.base_url}{page.url.split(server.base_url)[1].split('?')[0]}?vue=lignes&code=date_format_cell")
    expect(page.get_by_text("I14").first).to_be_visible()
    with page.expect_download() as download:
        page.get_by_role("link", name="Rapport .xlsx").click()
    assert download.value.suggested_filename.endswith(".xlsx")
    assert problems == []


@pytest.fixture
def import_server(tmp_path: Path, browser: Browser) -> Iterator[Server]:
    """Instance à part, où l'application des imports est activée : l'instance commune des essais reste intacte."""
    import sqlite3

    from biblyo.core.migrate import upgrade

    from ..factories import make_user
    from .conftest import _settings, login, start_server, stop_server

    settings = _settings(tmp_path)
    upgrade(settings)
    make_user(settings, ADMIN, "admin", display_name="Camille Essai")
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('imports.application_enabled', 'true', '2026-11-13')"
    )
    connection.commit()
    connection.close()
    instance = start_server(settings, tmp_path)
    context = browser.new_context()
    login(context.new_page(), instance.base_url, ADMIN)
    instance.storage[ADMIN] = tmp_path / "admin.json"
    context.storage_state(path=instance.storage[ADMIN])
    context.close()
    yield instance
    stop_server(instance)


@pytest.mark.parametrize("size", tuple(SIZES))
def test_apply_a_confirmed_plan_then_see_the_batch_and_the_origin_of_a_copy(
    browser: Browser, import_server: Server, size: str
) -> None:
    context = browser.new_context(storage_state=import_server.storage[ADMIN], viewport=SIZES[size])
    page = context.new_page()
    page.size_name = size  # type: ignore[attr-defined]
    problems = watch(page)
    base = import_server.base_url
    page.goto(f"{base}/imports")
    page.set_input_files("#principal", {"name": "catalogue.xlsx", "mimeType": XLSX, "buffer": samples.workbook()})
    page.set_input_files(
        "#complementaire", {"name": "enrichi.xlsx", "mimeType": XLSX, "buffer": samples.workbook(enriched=True)}
    )
    page.get_by_role("button", name="Lire le fichier").click()
    expect(page.locator("h1")).to_contain_text("Correspondance des colonnes")
    page.locator("#toasts [data-close-toast]").first.click()
    page.get_by_role("button", name="Lancer la simulation").click()
    expect(page.get_by_role("heading", name="Appliquer l’import")).to_be_visible()
    _accessible(page)
    _no_horizontal_scroll(page)
    _capture(page, "07-confirmation")
    page.get_by_label("J’ai relu cette simulation", exact=False).check()
    page.get_by_role("button", name="Appliquer l’import").click()
    expect(page.locator("h1")).to_contain_text("Lot d’import n°")
    page.locator("#toasts [data-close-toast]").first.click()
    expect(page.locator(".toast")).to_have_count(0)
    expect(page.get_by_text("Réconciliation, vérifiée avant la validation")).to_be_visible()
    expect(page.get_by_text("écart", exact=True)).to_have_count(0)
    _accessible(page)
    _no_horizontal_scroll(page)
    _capture(page, "08-lot")
    page.goto(f"{base}/exemplaires/14")
    expect(page.get_by_role("heading", name="Origine")).to_be_visible()
    expect(page.get_by_text("Ligne 15 du classeur catalogue.xlsx", exact=False)).to_be_visible()
    _accessible(page)
    _no_horizontal_scroll(page)
    _capture(page, "09-origine-exemplaire")
    assert problems == []
    context.close()
