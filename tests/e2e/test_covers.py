# SPDX-License-Identifier: EUPL-1.2
"""Couvertures dans un vrai navigateur (conception 17) : ajout depuis la fiche, aperçu, confirmation, catalogue,
suppression et rétablissement, recherche et choix d'une proposition, glisser-déposer, thème sombre.

Les sources sont remplacées par des réponses enregistrées : aucun appel réseau réel."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect

from ..factories import make_notice
from .conftest import ADMIN, Server, watch

pytestmark = pytest.mark.e2e
BLOCKING = {"serious", "critical"}
WIDE = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
IMAGE = Path(__file__).parent.parent / "fixtures" / "couverture-essai.jpg"


@pytest.fixture(params=("wide", "phone"))
def page(request: pytest.FixtureRequest, browser: Browser, server: Server) -> Iterator[Page]:
    context = browser.new_context(
        storage_state=server.storage[ADMIN], viewport=WIDE if request.param == "wide" else PHONE
    )
    instance = context.new_page()
    yield instance
    context.close()


def _settle(page: Page) -> None:
    for _attempt in range(50):  # fin des animations (fenêtre, toast), sinon axe mesure un texte encore transparent
        if page.evaluate("document.getAnimations().every(item => item.playState === 'finished')"):
            break
        page.wait_for_timeout(20)


def _accessible(page: Page) -> None:
    _settle(page)
    violations = [v for v in Axe().run(page).response["violations"] if v["impact"] in BLOCKING]
    assert not violations, [(v["id"], [n["html"][:160] for n in v["nodes"]]) for v in violations]


def _decoded(image) -> None:
    for _ in range(50):
        if image.evaluate("image => image.complete && image.naturalWidth > 0"):
            return
        image.page.wait_for_timeout(100)
    raise AssertionError("image non décodée")


def _no_horizontal_scroll(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_add_a_cover_from_a_file_then_remove_and_restore_it(page: Page, server: Server) -> None:
    problems = watch(page)
    work = make_notice(server.settings, "Le Grand Meaulnes", authors=("Alain-Fournier",))
    page.goto(f"{server.base_url}/catalogue/notices/{work}")
    page.locator(".notice-hero").get_by_role("link", name="Ajouter une couverture").first.click()
    expect(page.locator("h1")).to_contain_text("Couverture de « Le Grand Meaulnes »")
    _no_horizontal_scroll(page)
    _accessible(page)

    upload = page.locator("form[action$='/couverture/fichier']")
    upload.locator("#couverture").set_input_files(IMAGE)
    expect(upload.locator("[data-dropzone-name]")).to_have_text("couverture-essai.jpg")
    upload.get_by_label("Photo prise pour le Club").check()
    upload.get_by_role("button", name="Voir l’aperçu").click()
    expect(page.locator("h1")).to_have_text("Aperçu de la couverture")
    _decoded(page.locator(".couv-compare img.cover-img"))
    _no_horizontal_scroll(page)
    _accessible(page)

    page.get_by_role("button", name="Enregistrer la couverture").click()
    expect(page.locator("#toasts")).to_contain_text("La couverture a été enregistrée")
    _decoded(page.locator(".notice-hero img.cover-img"))
    page.goto(f"{server.base_url}/catalogue?q=Meaulnes")
    _decoded(page.locator(".bcard img.cover-img").first)

    page.goto(f"{server.base_url}/catalogue/notices/{work}/couverture")
    expect(page.locator(".couv-history")).to_contain_text("Fichier importé")
    page.get_by_role("button", name="Supprimer la couverture").click()
    dialog = page.locator("dialog#couv-supprimer")
    expect(dialog).to_be_visible()
    _accessible(page)
    dialog.get_by_role("button", name="Supprimer la couverture").click()
    expect(page.locator("#toasts")).to_contain_text("retirée")
    expect(page.locator(".couv-current img.cover-img")).to_have_count(0)
    page.get_by_role("button", name="Rétablir la couverture précédente").click()
    expect(page.locator("#toasts")).to_contain_text("rétablie")
    _decoded(page.locator(".couv-current img.cover-img"))
    assert problems == []


def test_search_compare_and_choose_a_cover(page: Page, server: Server) -> None:
    problems = watch(page)
    work = make_notice(server.settings, "Le Petit Prince", authors=("Antoine de Saint-Exupéry",), isbn="9782070612758")
    page.goto(f"{server.base_url}/catalogue/notices/{work}/couverture")
    page.locator("#couv-recherche").locator("xpath=..").get_by_role("button", name="Rechercher").click()
    proposal = page.locator("#couv-propositions .couv-prop").first
    expect(proposal).to_be_visible()
    expect(proposal).to_contain_text("Même édition établie : ISBN identique")
    _decoded(proposal.locator("img.cover-img"))
    _no_horizontal_scroll(page)
    _accessible(page)

    proposal.locator(".couv-thumb").click()
    zoom = page.locator("dialog#couv-prop-1")
    expect(zoom).to_be_visible()
    _decoded(zoom.locator("img.cover-img"))
    _accessible(page)
    page.keyboard.press("Escape")
    expect(zoom).to_be_hidden()

    proposal.get_by_role("button", name="Choisir").click()
    expect(page.locator("h1")).to_have_text("Aperçu de la couverture")
    expect(page.get_by_text("Conditions de la source")).to_be_visible()
    page.get_by_role("button", name="Enregistrer la couverture").click()
    expect(page.locator("#toasts")).to_contain_text("La couverture a été enregistrée")
    _decoded(page.locator(".notice-hero img.cover-img"))
    assert problems == []


def test_a_dropped_file_fills_the_field(browser: Browser, server: Server) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport=WIDE)
    page = context.new_page()
    work = make_notice(server.settings, "Livre déposé")
    page.goto(f"{server.base_url}/catalogue/notices/{work}/couverture")
    data = base64.b64encode(IMAGE.read_bytes()).decode()
    page.evaluate(
        """data => {
            const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
            const transfer = new DataTransfer();
            transfer.items.add(new File([bytes], "deposee.jpg", { type: "image/jpeg" }));
            const zone = document.querySelector("[data-dropzone]");
            zone.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: transfer }));
            zone.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer }));
        }""",
        data,
    )
    expect(page.locator("[data-dropzone-name]")).to_have_text("deposee.jpg")
    page.locator("form[action$='/couverture/fichier']").get_by_label("Photo prise pour le Club").check()
    page.get_by_role("button", name="Voir l’aperçu", exact=True).click()
    expect(page.locator("h1")).to_have_text("Aperçu de la couverture")
    context.close()


def test_the_cover_page_in_the_dark_theme(browser: Browser, server: Server) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport=PHONE, color_scheme="dark")
    page = context.new_page()
    work = make_notice(server.settings, "Livre en thème sombre")
    page.goto(f"{server.base_url}/catalogue/notices/{work}/couverture")
    expect(page.locator("h1")).to_contain_text("Couverture de")
    _no_horizontal_scroll(page)
    _accessible(page)
    upload = page.locator("form[action$='/couverture/fichier']")
    upload.locator("#couverture").set_input_files(IMAGE)
    upload.get_by_label("Image fournie ou autorisée par l’éditeur ou l’auteur").check()
    upload.get_by_role("button", name="Voir l’aperçu").click()
    expect(page.locator("h1")).to_have_text("Aperçu de la couverture")
    _accessible(page)
    context.close()
