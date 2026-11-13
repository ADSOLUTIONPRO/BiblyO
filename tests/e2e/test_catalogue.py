# SPDX-License-Identifier: EUPL-1.2
"""Catalogue dans un vrai navigateur : largeurs, recherche en direct, vues, filtres, sans JavaScript, palette, axe."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect

from ..factories import make_notice
from .conftest import ADMIN, VOLUNTEER, Server, watch

pytestmark = pytest.mark.e2e
BLOCKING = {"serious", "critical"}


@pytest.fixture(scope="module")
def notices(server: Server) -> Iterator[dict[str, int]]:
    settings = server.settings
    found = {
        "coeur": make_notice(
            settings,
            "Le Cœur des œuvres",
            document_type="livre",
            authors=("Jeanne Martin",),
            isbn="978-2-07-040850-4",
            publisher="Gallimard",
            year="1998",
            categories="Roman",
            genres="Récit",
        ),
        "serie": make_notice(
            settings,
            "Astérix le Gaulois",
            series="Une série au titre assez long pour un petit écran",
            volume="1",
            categories="Aventure",
        ),
    }
    for index in range(55):
        make_notice(settings, f"Remplissage {index:02d}", categories="Aventure", document_type="bd")
    yield found


def _paths(notices: dict[str, int]) -> tuple[str, ...]:
    return (
        "/catalogue",
        "/catalogue?view=list",
        "/catalogue?q=coeur&statut=toutes",
        "/catalogue?annee_min=1990&cat=roman",
        f"/catalogue/notices/{notices['coeur']}",
        f"/catalogue/notices/{notices['coeur']}?onglet=editions",
        f"/catalogue/notices/{notices['serie']}",
        f"/catalogue/notices/{notices['serie']}/couverture",
        f"/catalogue/notices/{notices['coeur']}/couverture",
        "/catalogue/notices/nouvelle",
        "/catalogue/series",
        "/catalogue/series?filtre=a-verifier",
        "/catalogue/series/1",
        "/catalogue/contributeurs",
        "/catalogue/contributeurs/1",
        "/",
        "/admin/catalogue/categories",
        "/admin/catalogue/sections",
    )


@pytest.mark.parametrize("width", [320, 390, 1024, 1440])
def test_no_horizontal_overflow(admin_page: Page, server: Server, notices: dict[str, int], width: int) -> None:
    admin_page.set_viewport_size({"width": width, "height": 800})
    for path in _paths(notices):
        admin_page.goto(f"{server.base_url}{path}")
        overflow = admin_page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 0, f"{path} déborde de {overflow} px à {width} px"


@pytest.mark.parametrize("width", [390, 1024, 1440])
def test_notice_actions_follow_the_information(admin_page: Page, server: Server, notices, width: int) -> None:
    """Fiche : les actions suivent les informations, sans grand vide ; sur téléphone, une seule colonne."""
    admin_page.set_viewport_size({"width": width, "height": 900})
    admin_page.goto(f"{server.base_url}/catalogue/notices/{notices['serie']}")
    facts = admin_page.locator(".notice-hero .notice-facts").bounding_box()
    actions = admin_page.locator(".notice-hero .hero-acts").bounding_box()
    cover = admin_page.locator(".notice-hero .notice-zoom").bounding_box()
    gap = actions["y"] - (facts["y"] + facts["height"])
    assert 0 <= gap <= 24, f"{gap} px entre les informations et les actions"
    if width == 390:
        assert cover["width"] <= 160
        assert abs((cover["x"] + cover["width"] / 2) - width / 2) < 20  # couverture centrée
        assert actions["y"] > cover["y"] + cover["height"]
    else:
        assert actions["x"] > cover["x"] + cover["width"]  # à droite de la couverture
        assert actions["y"] < cover["y"] + cover["height"]  # à sa hauteur, pas en dessous


@pytest.mark.parametrize("width", [390, 1024, 1440])
@pytest.mark.parametrize("with_image", [True, False])
def test_cover_zoom_is_accessible(
    browser: Browser, server: Server, notices, tmp_path, width: int, with_image: bool
) -> None:
    """Un clic sur la couverture ouvre une fenêtre nommée, au clavier, sans défilement piégé, refermée par Échap."""
    from PIL import Image

    work = notices["serie"] if with_image else notices["coeur"]
    if with_image:  # couverture posée par la page Couverture, avec un compte qui peut modifier la fiche
        editor = browser.new_context(storage_state=server.storage[ADMIN], viewport={"width": 1440, "height": 900})
        setup = editor.new_page()
        setup.goto(f"{server.base_url}/catalogue/notices/{work}/couverture")
        if setup.locator(".couv-current img.cover-img").count() == 0:
            image = tmp_path / "couverture.png"
            Image.new("RGB", (800, 1200), (46, 90, 136)).save(image, "PNG")
            upload = setup.locator("form[action$='/couverture/fichier']")
            upload.locator("input[type=file]").set_input_files(image)
            upload.get_by_label("Photo prise pour le Club").check()
            upload.get_by_role("button", name="Voir l’aperçu", exact=True).click()
            setup.get_by_role("button", name="Enregistrer la couverture").click()
            setup.wait_for_url(re.compile(r"/catalogue/notices/\d+\?edition="))
        editor.close()
    # Agrandissement vu par une bénévole : un clic sur la couverture l'ouvre. Pour qui peut modifier la fiche, ce
    # clic ouvre la page Couverture, et « Agrandir » reste un bouton à part (conception 17 § 3).
    context = browser.new_context(
        storage_state=server.storage[VOLUNTEER], bypass_csp=True, viewport={"width": width, "height": 800}
    )
    page = context.new_page()
    page.goto(f"{server.base_url}/catalogue/notices/{work}")
    opener = page.locator(".notice-hero .notice-zoom")
    opener.click()
    dialog = page.get_by_role("dialog", name=re.compile(r"^Couverture : "))
    expect(dialog).to_be_visible()
    assert page.evaluate("document.getElementById('zoom-couverture').contains(document.activeElement)")
    if with_image:
        expect(dialog.get_by_role("img", name=re.compile("^Couverture de "))).to_be_visible()
    else:
        expect(dialog.get_by_text("Aucune image jointe")).to_be_visible()
    for _attempt in range(50):  # fin de l'animation d'ouverture, sinon axe mesure un texte encore transparent
        if page.evaluate("document.getAnimations().every(item => item.playState === 'finished')"):
            break
        page.wait_for_timeout(20)
    body = "document.querySelector('#zoom-couverture .modal-b')"
    scrolls = page.evaluate(f"{body}.scrollHeight > {body}.clientHeight")
    assert not scrolls, "la fenêtre défile sans élément focalisable"
    found = [
        f"{violation['id']} ({violation['impact']}) : {node['target']} {node.get('any', [{}])[0].get('message', '')}"
        for violation in Axe().run(page).response["violations"]
        if violation["impact"] in BLOCKING
        for node in violation["nodes"]
    ]
    assert found == []
    # Bouton de fermeture nommé, focus enfermé : la page derrière la fenêtre est inerte.
    close = dialog.get_by_role("button", name="Fermer")
    expect(close).to_be_visible()
    for key in ("Tab", "Tab", "Shift+Tab", "Shift+Tab", "Tab"):
        page.keyboard.press(key)
        outside = page.evaluate(
            "(() => { const a = document.activeElement; const d = document.getElementById('zoom-couverture');"
            " return a !== null && a !== document.body && !d.contains(a); })()"
        )
        assert not outside, f"le focus a quitté la fenêtre après {key}"
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    # Même retour du focus par le bouton Fermer, au clavier.
    page.keyboard.press("Enter")
    expect(dialog).to_be_visible()
    close.focus()
    page.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    context.close()


def test_notice_cards_do_not_stretch(admin_page: Page, server: Server, notices) -> None:
    admin_page.goto(f"{server.base_url}/catalogue/notices/{notices['coeur']}")
    heights = admin_page.evaluate(
        "[...document.querySelectorAll('.notice-split > *')].map(e => Math.round(e.getBoundingClientRect().height))"
    )
    assert len(heights) == 2 and heights[0] != heights[1]


def test_live_search_updates_results_address_and_keeps_focus(admin_page: Page, server: Server, notices) -> None:
    problems = watch(admin_page)
    admin_page.goto(f"{server.base_url}/catalogue")
    field = admin_page.locator("#cat-q")
    field.click()
    field.press_sequentially("coeur")
    expect(admin_page).to_have_url(re.compile(r"\?q=coeur$"))
    expect(admin_page.locator(".bcard")).to_have_count(1)
    expect(admin_page.locator("#cat-q")).to_be_focused()
    expect(admin_page.locator("#cat-q")).to_have_value("coeur")
    assert problems == []


def test_filters_and_views_keep_the_state_in_the_address(admin_page: Page, server: Server, notices) -> None:
    problems = watch(admin_page)
    base = server.base_url
    admin_page.goto(f"{base}/catalogue")
    admin_page.select_option("select[name=cat]", "roman")
    expect(admin_page).to_have_url(re.compile(r"cat=roman"))
    expect(admin_page.locator(".bcard")).to_have_count(1)
    admin_page.get_by_role("button", name="Liste").click()
    expect(admin_page).to_have_url(re.compile(r"view=list"))
    expect(admin_page.locator("table.tbl tbody tr")).to_have_count(1)
    expect(admin_page.locator("select[name=cat]")).to_have_value("roman")

    url = admin_page.url
    admin_page.goto(url)
    expect(admin_page.locator("table.tbl tbody tr")).to_have_count(1)
    admin_page.go_back()
    assert problems == []


def test_pagination(admin_page: Page, server: Server, notices) -> None:
    admin_page.goto(f"{server.base_url}/catalogue?cat=aventure")
    expect(admin_page.locator(".bcard")).to_have_count(48)
    admin_page.locator(".pager a[rel=next]").click()
    expect(admin_page).to_have_url(re.compile(r"page=2"))
    expect(admin_page.locator(".bcard")).to_have_count(8)
    expect(admin_page.locator(".pager a[rel=next]")).to_have_count(0)


def test_search_works_without_javascript(browser: Browser, server: Server, notices) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], java_script_enabled=False)
    page = context.new_page()
    page.goto(f"{server.base_url}/catalogue")
    page.fill("#cat-q", "cœur")
    page.get_by_role("button", name="Filtrer").click()
    assert "q=c%C5%93ur" in page.url
    expect(page.locator(".bcard")).to_have_count(1)
    page.get_by_role("button", name="Liste").click()
    assert "view=list" in page.url
    expect(page.locator("table.tbl tbody tr")).to_have_count(1)
    context.close()


def test_saved_search_from_the_interface(admin_page: Page, server: Server, notices) -> None:
    admin_page.goto(f"{server.base_url}/catalogue?cat=roman")
    admin_page.locator(".save-search summary").click()
    admin_page.fill("input[name=nom]", "Romans e2e")
    admin_page.locator(".save-search button[type=submit]").click()
    admin_page.wait_for_url(re.compile(r"cat=roman"))
    admin_page.goto(f"{server.base_url}/catalogue")
    admin_page.get_by_role("link", name="Romans e2e").click()
    expect(admin_page).to_have_url(re.compile(r"cat=roman"))
    admin_page.get_by_role("button", name="Supprimer la recherche Romans e2e").click()
    admin_page.wait_for_load_state("networkidle")
    expect(admin_page.get_by_role("link", name="Romans e2e")).to_have_count(0)


def test_palette_finds_a_notice_by_isbn(admin_page: Page, server: Server, notices) -> None:
    problems = watch(admin_page)
    admin_page.goto(f"{server.base_url}/")
    admin_page.locator(".cmdk-btn").click()
    box = admin_page.locator("#palette input[name=q]")
    box.fill("978-2-07-040850-4")
    result = admin_page.locator("#palette a[data-server]")
    expect(result).to_have_count(1)
    expect(result).to_be_visible()
    box.press("Enter")
    admin_page.wait_for_url(f"{server.base_url}/catalogue/notices/{notices['coeur']}")
    assert problems == []


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1440])
def test_accessibility(browser: Browser, server: Server, notices, theme: str, width: int) -> None:
    context = browser.new_context(
        storage_state=server.storage[ADMIN],
        bypass_csp=True,
        viewport={"width": width, "height": 900},
        color_scheme=theme,
    )
    page = context.new_page()
    problems = {}
    for path in _paths(notices):
        page.goto(f"{server.base_url}{path}")
        page.wait_for_load_state("networkidle")
        found = [
            f"{violation['id']} ({violation['impact']}) : {node['target']}"
            for violation in Axe().run(page).response["violations"]
            if violation["impact"] in BLOCKING
            for node in violation["nodes"]
        ]
        if found:
            problems[path] = found
    context.close()
    assert problems == {}
