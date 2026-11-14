# SPDX-License-Identifier: EUPL-1.2
"""Enrichir une fiche dans un vrai navigateur : chercher, comparer, cocher, prévisualiser, confirmer.

Les sources sont remplacées par des réponses enregistrées : aucun appel réseau réel.
"""

from __future__ import annotations

import re
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


def _decoded(page: Page, image) -> None:
    expect(image).to_be_visible()
    for _ in range(50):  # servie par biblyO, en WebP : l'image doit être décodée par le navigateur
        if image.evaluate("image => image.complete && image.naturalWidth > 0"):
            return
        page.wait_for_timeout(100)
    raise AssertionError("couverture non décodée")


def test_enrich_a_book_with_the_assistant(page: Page, server: Server) -> None:
    problems = watch(page)
    work = make_notice(server.settings, "Le Petit Prince", isbn="9782070612758", publisher="Galimard")
    page.goto(f"{server.base_url}/catalogue/notices/{work}")
    page.get_by_role("link", name="Enrichir").first.click()
    expect(page.locator("h1")).to_contain_text("Enrichir un livre")
    expect(page.get_by_text("Il manque :")).to_be_visible()
    _accessible(page)

    page.get_by_role("button", name="Rechercher la couverture et les informations").click()
    expect(page.locator("#propositions")).to_be_visible()
    expect(page.get_by_text("Même édition").first).to_be_visible()
    _decoded(page, page.locator(".asst-thumb img").first)
    expect(page.locator(".enr-score")).to_have_count(0)  # plus de score au centre de l'écran
    _accessible(page)

    page.get_by_role("link", name="Choisir cette proposition").first.click()
    expect(page.locator("h1")).to_contain_text("Est-ce bien votre livre")
    _decoded(page, page.locator(".asst-head img.cover-img"))
    expect(page.get_by_text("Manquant : sera ajouté").first).to_be_visible()
    expect(page.get_by_text("Différent : votre valeur est gardée").first).to_be_visible()  # Galimard
    _accessible(page)
    page.get_by_role("button", name="Ajouter les informations manquantes").click()
    expect(page).to_have_url(f"{server.base_url}/catalogue/notices/{work}?onglet=enrichissement")
    expect(page.locator("#toasts")).to_contain_text("complétée")
    page.goto(f"{server.base_url}/catalogue/notices/{work}?onglet=notice")
    expect(page.locator("body")).to_contain_text("Galimard")  # l'éditeur saisi n'a pas été remplacé
    expect(page.locator("body")).to_contain_text("2007")
    assert problems == []


def test_add_only_the_cover_then_undo_and_settings(page: Page, server: Server) -> None:
    problems = watch(page)
    work = make_notice(server.settings, "Le Petit Prince", isbn="9782070612758")
    page.goto(f"{server.base_url}/catalogue/notices/{work}/enrichir")
    page.get_by_role("button", name="Rechercher la couverture et les informations").click()
    page.get_by_role("button", name="Ajouter seulement la couverture").first.click()
    expect(page.locator("h1")).to_contain_text("couverture")
    page.get_by_role("button", name="Enregistrer la couverture").click()
    expect(page.locator("img.cover-img[src^='/medias/']").first).to_be_attached()  # couverture de la BnF rangée

    page.goto(f"{server.base_url}/catalogue/notices/{work}/enrichir")
    page.get_by_role("button", name="Rechercher la couverture et les informations").click()
    page.get_by_role("link", name="Choisir cette proposition").first.click()
    page.get_by_role("button", name="Ajouter les informations manquantes").click()
    expect(page.get_by_role("heading", name="Provenance des données")).to_be_visible()
    expect(page.get_by_text("tel que repris").first).to_be_visible()
    _accessible(page)
    page.get_by_role("button", name="Annuler cet enrichissement").first.click()
    expect(page.locator("#toasts")).to_contain_text("rétablies")

    page.goto(f"{server.base_url}/parametres/enrichissement")
    expect(page.locator("h1")).to_contain_text("Enrichissement et couvertures")
    _accessible(page)
    page.get_by_role("button", name="Tester").first.click()
    expect(page.get_by_text("répond normalement").first).to_be_visible()
    assert problems == []


def test_real_comic_cases_are_never_confirmed_silently(page: Page, server: Server) -> None:
    problems = watch(page)
    talon = make_notice(
        server.settings, "Cerveau-choc", authors=("Greg",), publisher="Dargaud", series="Achille Talon", volume="1"
    )
    page.goto(f"{server.base_url}/catalogue/notices/{talon}/enrichir")
    page.get_by_role("button", name="Rechercher la couverture et les informations").click()
    expect(page.get_by_text("Édition probable").first).to_be_visible()
    expect(page.get_by_text("Les Idées d'Achille Talon, cerveau-choc").first).to_be_visible()
    expect(page.get_by_text("Pas d’image").first).to_be_visible()  # jamais une illustration générique
    _accessible(page)
    page.get_by_role("link", name="Choisir cette proposition").first.click()
    page.get_by_role("button", name="Ajouter les informations manquantes").click()
    expect(page.locator("#toasts")).to_contain_text("J’ai vérifié")  # validation explicite exigée
    page.go_back()
    page.get_by_role("link", name="Retour aux propositions").click()
    page.get_by_role("button", name="Ce n’est pas mon livre").first.click()
    expect(page.locator("#toasts")).to_contain_text("ne sera plus montrée")

    duc = make_notice(
        server.settings,
        "Du scénario à la réalisation",
        authors=("Bernard Duc",),
        publisher="Glénat",
        document_type="livre",
    )
    page.goto(f"{server.base_url}/catalogue/notices/{duc}/enrichir")
    page.get_by_role("button", name="Rechercher la couverture et les informations").click()
    expect(page.get_by_text("DL 1981").first).to_be_visible()
    expect(page.get_by_text("1990").first).to_be_visible()
    expect(page.get_by_text("Plusieurs éditions se ressemblent").first).to_be_visible()
    _accessible(page)
    assert problems == []


def test_enrich_the_library_from_the_catalogue(page: Page, server: Server) -> None:
    """La recherche avance au rythme réel des sources (5 s par appel à la BnF en lot) : on ne l'attend pas jusqu'au
    bout ici ; la validation par petits lots est éprouvée par les tests web, qui la terminent."""
    problems = watch(page)
    make_notice(server.settings, "Le Petit Prince", isbn="9782070612758")
    page.goto(f"{server.base_url}/catalogue")
    page.get_by_role("link", name="Enrichir la bibliothèque").click()
    expect(page.locator("h1")).to_contain_text("Enrichir la bibliothèque")
    _accessible(page)
    launch = page.get_by_role("button", name="Lancer la recherche")
    if launch.count():  # sinon une recherche est déjà en cours (variante précédente du même parcours)
        launch.click()
    expect(page.get_by_text("livre(s)").first).to_be_visible()  # « 1 livre(s) sur 2 » ou « 2 livre(s) examiné(s) »
    _accessible(page)
    if page.get_by_role("button", name="Mettre en pause").count():  # encore en cours : on l'arrête proprement
        page.get_by_role("button", name="Annuler la recherche").click()
        # annulée, ou terminée entre-temps (« ne peut pas changer d'état ») : deux issues correctes
        expect(page.locator("#toasts")).to_contain_text(re.compile("annulée|ne peut pas changer"))
    page.goto(f"{server.base_url}/enrichissement")
    expect(page.get_by_role("button", name="Lancer la recherche")).to_be_visible()
    _accessible(page)
    assert problems == []


def test_settings_rubrics_are_readable_and_accessible(page: Page, server: Server) -> None:
    problems = watch(page)
    page.goto(f"{server.base_url}/parametres")
    expect(page.locator("h1")).to_contain_text("Paramètres")
    _accessible(page)
    for path in ("bibliotheque", "catalogue", "prets", "sauvegardes", "utilisateurs", "avance"):
        page.goto(f"{server.base_url}/parametres/{path}")
        expect(page.locator(".set-nav [aria-current=page]")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), path  # rien ne déborde
        _accessible(page)
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
