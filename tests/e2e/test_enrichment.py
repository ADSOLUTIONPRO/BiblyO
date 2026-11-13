# SPDX-License-Identifier: EUPL-1.2
"""Enrichir une fiche dans un vrai navigateur : chercher, comparer, cocher, prévisualiser, confirmer.

Les sources sont remplacées par des réponses enregistrées : aucun appel réseau réel.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect

from ..factories import make_notice
from .conftest import ADMIN, Server, watch

pytestmark = pytest.mark.e2e
BLOCKING = {"serious", "critical"}
WIDE = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


@pytest.fixture(params=("wide", "phone"))
def page(request: pytest.FixtureRequest, browser: Browser, server: Server) -> Iterator[Page]:
    context = browser.new_context(
        storage_state=server.storage[ADMIN], viewport=WIDE if request.param == "wide" else PHONE
    )
    instance = context.new_page()
    yield instance
    context.close()


def _accessible(page: Page) -> None:
    for _attempt in range(50):  # fin de l'apparition d'un toast, sinon axe mesure un bouton encore transparent
        if page.evaluate("document.getAnimations().every(item => item.playState === 'finished')"):
            break
        page.wait_for_timeout(20)
    violations = [v for v in Axe().run(page).response["violations"] if v["impact"] in BLOCKING]
    assert not violations, [(v["id"], [n["html"][:160] for n in v["nodes"]]) for v in violations]


def test_enrich_a_record_field_by_field(page: Page, server: Server) -> None:
    problems = watch(page)
    work = make_notice(server.settings, "Le Petit Prince", isbn="9782070612758", publisher="Galimard")
    page.goto(f"{server.base_url}/catalogue/notices/{work}")
    page.get_by_role("link", name="Enrichir").first.click()
    expect(page.locator("h1")).to_contain_text("Enrichir")
    _accessible(page)

    page.get_by_role("button", name="Rechercher", exact=True).click()
    expect(page.locator("#enr-resultats")).to_be_visible()
    expect(page.locator("#enr-sources")).to_be_visible()
    expect(page.get_by_text("Correspondance exacte").first).to_be_visible()
    expect(page.get_by_text("BnF").first).to_be_visible()
    _accessible(page)

    page.get_by_role("link", name="Comparer et choisir").first.click()
    expect(page.locator("h1")).to_contain_text("Comparer")
    thumbnail = page.locator("img.enr-cover")
    expect(thumbnail).to_be_visible()
    for _ in range(50):  # servie par biblyO, en WebP : l'image doit être décodée par le navigateur
        if thumbnail.evaluate("image => image.complete && image.naturalWidth > 0"):
            break
        page.wait_for_timeout(100)
    else:
        raise AssertionError("vignette de couverture non décodée")
    expect(page.locator("#champ-year")).to_be_checked()  # vide et correspondance exacte : précoché
    expect(page.locator("#champ-publisher")).not_to_be_checked()  # « Galimard » est rempli : jamais précoché
    expect(page.get_by_text("Non copiable").first).to_be_visible()  # résumé Electre
    _accessible(page)

    page.locator("#champ-cover").check()
    page.get_by_role("button", name="Prévisualiser").click()
    expect(page.locator("h1")).to_contain_text("Aperçu")
    expect(page.get_by_text("Les exemplaires, les cotes, le rangement")).to_be_visible()
    page.get_by_role("button", name="Confirmer l’application").click()
    expect(page).to_have_url(f"{server.base_url}/catalogue/notices/{work}?onglet=enrichissement")
    expect(page.locator("#toasts")).to_contain_text("appliqués")
    page.goto(f"{server.base_url}/catalogue/notices/{work}?onglet=notice")
    expect(page.locator("body")).to_contain_text("Galimard")  # l'éditeur saisi n'a pas été remplacé
    expect(page.locator("body")).to_contain_text("2007")
    expect(page.locator("img.cover-img[src^='/medias/']").first).to_be_attached()  # couverture de la BnF rangée
    assert problems == []


def test_tab_undo_and_sources_settings(page: Page, server: Server) -> None:
    problems = watch(page)
    work = make_notice(server.settings, "Le Petit Prince", isbn="9782070612758")
    page.goto(f"{server.base_url}/catalogue/notices/{work}/enrichir")
    page.get_by_role("button", name="Rechercher", exact=True).click()
    page.get_by_role("link", name="Comparer et choisir").first.click()
    page.get_by_role("button", name="Prévisualiser").click()
    page.get_by_role("button", name="Confirmer l’application").click()
    expect(page.get_by_role("heading", name="Provenance des données")).to_be_visible()
    expect(page.get_by_text("tel que repris").first).to_be_visible()
    _accessible(page)
    page.get_by_role("button", name="Annuler cet enrichissement").click()
    expect(page.locator("#toasts")).to_contain_text("rétablies")
    expect(page.get_by_role("button", name="Annuler cet enrichissement")).to_have_count(0)
    page.goto(f"{server.base_url}/admin/sources")
    expect(page.locator("h1")).to_contain_text("Sources bibliographiques")
    _accessible(page)
    page.get_by_role("button", name="Tester la connexion").first.click()
    expect(page.get_by_text("répond").first).to_be_visible()
    assert problems == []


def test_create_a_record_from_a_free_search(page: Page, server: Server) -> None:
    problems = watch(page)
    page.goto(f"{server.base_url}/sources")
    expect(page.locator("h1")).to_contain_text("Recherche bibliographique")
    _accessible(page)
    page.locator("#isbn").fill("9782012101333")
    page.get_by_role("button", name="Rechercher", exact=True).click()
    expect(page.locator("#src-resultats")).to_be_visible()
    _accessible(page)
    page.get_by_role("link", name="Créer une notice").first.click()
    expect(page.locator("h1")).to_contain_text("Créer une notice")
    _accessible(page)
    page.get_by_role("button", name="Continuer vers le formulaire").click()
    expect(page.get_by_text("Prérempli depuis la").first).to_be_visible()
    expect(page.locator("#titre")).to_have_value("Astérix le Gaulois")
    expect(page.locator("#serie")).to_have_value("")  # jamais préremplie
    page.locator("#type").select_option(label="Bande dessinée")
    _accessible(page)
    page.get_by_role("button", name="Créer la notice").click()
    expect(page.locator("h1")).to_have_text("Astérix le Gaulois")
    page.goto(page.url.split("?")[0] + "?onglet=enrichissement")
    expect(page.get_by_text("tel que repris").first).to_be_visible()
    assert problems == []
