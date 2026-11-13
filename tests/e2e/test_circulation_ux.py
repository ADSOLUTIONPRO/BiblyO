# SPDX-License-Identifier: EUPL-1.2
"""UX-Circulation 2.0 dans un vrai navigateur : prêter et rendre sans connaître un seul code. Personnes inventées."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.patrons import service
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice
from .conftest import ADMIN, VOLUNTEER, Server, watch

pytestmark = pytest.mark.e2e
BLOCKING = {"serious", "critical"}
WIDE = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def shop(server: Server) -> Iterator[SimpleNamespace]:
    database = Database(server.settings.database_path)
    path = server.settings.database_path

    def execute(query: str, *params) -> list[tuple]:
        connection = sqlite3.connect(path)
        try:
            found = connection.execute(query, params).fetchall()
            connection.commit()
            return found
        finally:
            connection.close()

    def adult(first: str, last: str) -> int:
        person = PersonInput(first_name=first, last_name=last, category="adult", email=f"{first.lower()}@example.org")
        return service.register(database, Registration(person=person, homonyms_confirmed=True), None)

    def book(title: str, count: int = 1) -> tuple[int, list[int]]:
        work = make_notice(server.settings, title)
        edition = execute("SELECT id FROM editions WHERE work_id = ?", work)[0][0]
        ids = catalogue.add_copies(database, edition, count, "", None)
        for copy_id in ids:
            execute("UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
        return work, ids

    yield SimpleNamespace(adult=adult, book=book, execute=execute)
    database.dispose()


@pytest.fixture
def page(browser: Browser, server: Server) -> Iterator[Page]:
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport=WIDE)
    instance = context.new_page()
    yield instance
    context.close()


def _pick_reader(page: Page, name: str) -> None:
    """Chercher un lecteur par son nom, comme on le ferait de vive voix, puis cliquer sur sa ligne."""
    page.locator("#cp-lecteur-q").fill(name)
    row = page.locator("#cp-lecteurs").get_by_role("button", name=f"Choisir {name}")
    expect(row).to_have_count(1)
    row.click()
    # « Changer de lecteur » n'existe qu'une fois le lecteur choisi : le nom seul figure aussi dans la liste
    expect(page.get_by_role("button", name="Changer de lecteur")).to_be_visible()
    expect(page.locator(".desk-reader").get_by_text(name, exact=True)).to_be_visible()
    # Fin réelle de l'échange : après la mise en place, le focus revient au scanner. Avant, un champ tout juste
    # inséré peut ne pas encore réagir à la frappe automatique, bien plus rapide qu'une personne.
    expect(page.locator("#cp-saisie")).to_be_focused()


def test_from_the_catalogue_to_a_loan_without_any_code(page: Page, server: Server, shop) -> None:
    """AC1, AC5, AC9 : catalogue → Prêter → lecteur choisi dans la liste → Confirmer."""
    problems = watch(page)
    shop.adult("Odile", "Sanscode")
    work, _ids = shop.book("Les Chemins de sable")
    page.goto(f"{server.base_url}/catalogue/notices/{work}")
    page.get_by_role("link", name="Prêter", exact=True).click()
    expect(page.locator(".desk-books").get_by_text("Les Chemins de sable")).to_be_visible()  # le livre reste visible
    expect(page.locator("#cp-lecteurs .desk-pick").first).to_be_visible()  # la liste est là avant toute saisie
    _pick_reader(page, "Odile Sanscode")
    expect(page.locator(".desk-books").get_by_text("Les Chemins de sable")).to_be_visible()
    page.get_by_role("button", name="Confirmer le prêt du livre").click()
    done = page.locator(".desk-done")
    expect(done.get_by_text("Prêt enregistré")).to_be_visible()
    expect(done.get_by_role("link", name="Les Chemins de sable", exact=True)).to_be_visible()
    expect(done.get_by_text("Odile Sanscode")).to_be_visible()
    expect(done.get_by_role("link", name="Nouveau prêt")).to_be_visible()
    assert not problems, problems


def test_from_a_reader_to_a_loan_of_two_books(page: Page, server: Server, shop) -> None:
    """AC2, AC10 : fiche lecteur → Nouveau prêt → deux livres ajoutés depuis le catalogue du comptoir."""
    reader = shop.adult("Paulin", "Deuxlivres")
    shop.book("La Rivière lente")
    shop.book("Le Pont bleu")
    page.goto(f"{server.base_url}/lecteurs/{reader}")
    page.get_by_role("link", name="Nouveau prêt").click()
    expect(page.locator(".desk-reader").get_by_text("Paulin Deuxlivres", exact=True)).to_be_visible()
    for title in ("La Rivière lente", "Le Pont bleu"):
        page.locator("#cp-livre-q").fill(title)
        page.locator("#cp-livres").get_by_role("button", name=f"Ajouter « {title} »").click()
        expect(page.locator(".desk-books .counter-line").get_by_text(title)).to_be_visible()
        expect(page.locator("#cp-saisie")).to_be_focused()
    page.get_by_role("button", name="Confirmer le prêt de 2 livres").click()
    expect(page.locator(".desk-done").get_by_text("Prêt enregistré")).to_be_visible()
    assert shop.execute("SELECT COUNT(*) FROM loans WHERE patron_id = ?", reader) == [(2,)]


def test_the_reader_search_reacts_while_typing_and_keeps_the_last_answer(page: Page, server: Server, shop) -> None:
    """AC4 : pas besoin d'Entrée ; une réponse obsolète ne remplace jamais la plus récente."""
    shop.adult("Gaspard", "Frappevite")
    shop.adult("Gaspard", "Autrement")
    page.goto(f"{server.base_url}/comptoir")
    field = page.locator("#cp-lecteur-q")
    field.press_sequentially("Gaspard Frappevite", delay=15)
    rows = page.locator("#cp-lecteurs .desk-pick")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Frappevite")
    expect(field).to_be_focused()  # la frappe n'est jamais interrompue par le scanner
    assert not page.locator('input[name="lecteur"]').count()  # rien n'est choisi d'office


def test_the_scanner_still_works_after_a_click_in_a_list(page: Page, server: Server, shop) -> None:
    """AC13 : après un choix à la souris, la douchette écrit de nouveau dans le champ du scanner."""
    shop.adult("Rémi", "Souris")
    _work, (copy_id,) = shop.book("Le Phare d’hiver")
    code = shop.execute("SELECT barcode FROM copies WHERE id = ?", copy_id)[0][0]
    page.goto(f"{server.base_url}/comptoir")
    _pick_reader(page, "Rémi Souris")
    expect(page.locator("#cp-saisie")).to_be_focused()
    page.keyboard.type(code, delay=0)
    page.keyboard.press("Enter")
    expect(page.locator(".desk-books .counter-line")).to_have_count(1)
    expect(page.locator("#cp-saisie")).to_have_value("")


def test_next_page_of_readers_keeps_the_focus_in_the_list(page: Page, server: Server, shop) -> None:
    for index in range(22):
        shop.adult(f"Prénom{index:02d}", "Pageur")
    page.goto(f"{server.base_url}/comptoir")
    page.locator("#cp-lecteur-q").fill("Pageur")
    expect(page.locator("#cp-lecteurs .desk-pick")).to_have_count(20)
    page.get_by_role("button", name="Lecteurs suivants").click()
    expect(page.locator("#cp-lecteurs .desk-pick")).to_have_count(2)
    expect(page.locator("#cp-lecteur-q")).to_be_focused()


def test_a_book_with_several_copies_lets_one_be_chosen(page: Page, server: Server, shop) -> None:
    _work, ids = shop.book("Trois Exemplaires", 3)
    page.goto(f"{server.base_url}/comptoir")
    page.locator("#cp-livre-q").fill("Trois Exemplaires")
    page.get_by_role("button", name="Choisir l’exemplaire de « Trois Exemplaires »").click()
    adds = page.locator(".copy-pick").get_by_role("button", name="Ajouter l’exemplaire")
    expect(adds).to_have_count(3)
    adds.nth(1).click()
    expect(page.locator(".desk-books .counter-line")).to_have_count(1)
    assert page.locator('input[name="ex"]').get_attribute("value") == str(ids[1])


def test_return_from_the_book_page_is_confirmed_first(page: Page, server: Server, shop) -> None:
    shop.adult("Yvette", "Rendlivre")
    work, _ids = shop.book("Le Retour du phare")
    page.goto(f"{server.base_url}/comptoir/livre/{work}/preter")
    _pick_reader(page, "Yvette Rendlivre")
    page.get_by_role("button", name="Confirmer le prêt du livre").click()
    expect(page.locator(".desk-done")).to_be_visible()
    page.goto(f"{server.base_url}/catalogue/notices/{work}")
    page.get_by_role("link", name="Enregistrer le retour").click()
    page.get_by_role("button", name="Enregistrer le retour").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")  # Échap ferme sans rien écrire
    expect(dialog).to_be_hidden()
    assert shop.execute(
        "SELECT COUNT(*) FROM loans WHERE returned_at IS NOT NULL AND copy_id IN "
        "(SELECT c.id FROM copies c JOIN editions e ON e.id = c.edition_id WHERE e.work_id = ?)",
        work,
    ) == [(0,)]
    page.get_by_role("button", name="Enregistrer le retour").click()
    page.get_by_role("dialog").get_by_role("button", name="Confirmer le retour").click()
    expect(page.get_by_role("heading", name="Retour enregistré")).to_be_visible()
    expect(page.get_by_role("link", name="Localiser l’emplacement")).to_have_count(0)  # pas de place : le texte le dit
    expect(page.get_by_text("Cet exemplaire n’a pas de place attribuée.")).to_be_visible()


def test_quick_return_without_code(page: Page, server: Server, shop) -> None:
    shop.adult("Armand", "Rapide")
    _work, (copy_id,) = shop.book("Le Vent du large")
    page.goto(f"{server.base_url}/comptoir")
    _pick_reader(page, "Armand Rapide")
    page.locator("#cp-livre-q").fill("Le Vent du large")
    page.locator("#cp-livres").get_by_role("button", name="Ajouter « Le Vent du large »").click()
    page.get_by_role("button", name="Confirmer le prêt du livre").click()
    expect(page.locator(".desk-done")).to_be_visible()
    page.goto(f"{server.base_url}/comptoir/retours")
    page.locator("#cp-retour-q").fill("Vent du large")
    page.locator("#retours-trouves").get_by_role("button", name="Enregistrer le retour de Le Vent du large").click()
    expect(page.locator("#retours-lignes").get_by_text("Le Vent du large")).to_be_visible()
    assert "Armand" not in page.locator("main").inner_text()  # bénévole : l'emprunteur n'est jamais nommé
    assert shop.execute("SELECT returned_at IS NOT NULL FROM loans WHERE copy_id = ?", copy_id) == [(1,)]


def test_the_counter_on_a_phone_keeps_confirmation_in_reach(browser: Browser, server: Server, shop) -> None:
    """AC14 : une colonne, aucun défilement horizontal, la confirmation au-dessus de la barre de navigation."""
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport=PHONE)
    page = context.new_page()
    shop.adult("Téo", "Téléphone")
    work, _ids = shop.book("Petit écran")
    page.goto(f"{server.base_url}/comptoir/livre/{work}/preter")
    _pick_reader(page, "Téo Téléphone")
    button = page.get_by_role("button", name="Confirmer le prêt du livre")
    expect(button).to_be_in_viewport()
    bar = page.locator(".desk-side .counter-bar").bounding_box()
    nav = page.locator(".bnav").bounding_box()
    assert bar["y"] + bar["height"] <= nav["y"] + 1, (bar, nav)
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0
    button.click()
    expect(page.locator(".desk-done").get_by_text("Prêt enregistré")).to_be_visible()
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_new_screens_have_no_serious_accessibility_issue(browser: Browser, server: Server, shop, theme: str) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport=WIDE, color_scheme=theme)
    page = context.new_page()
    shop.adult("Axelle", f"Contraste{theme}")
    work, ids = shop.book(f"Lisible {theme}", 2)
    for path in (
        "/comptoir",
        f"/comptoir?exemplaire={ids[0]}&livre={work}",
        f"/comptoir/livre/{work}",
        "/catalogue",
        "/catalogue?view=list",
        f"/catalogue/notices/{work}",
        "/comptoir/retours",
    ):
        page.goto(f"{server.base_url}{path}")
        results = Axe().run(page)
        serious = [item for item in results.response["violations"] if item["impact"] in BLOCKING]
        assert not serious, (path, serious)
    context.close()


# --- Finitions de la validation graphique : U15, U16, U19 ----------------------------------------------------------
def test_a_renewal_is_announced_keeps_its_place_and_cannot_be_repeated(page: Page, server: Server, shop) -> None:
    reader = shop.adult("Rosine", "Renouvelle")
    _w1, (first,) = shop.book("Premier à rendre")
    _w2, (second,) = shop.book("Second à rendre")
    for copy_id in (first, second):
        page.goto(f"{server.base_url}/comptoir?lecteur={reader}")
        page.locator("#cp-livre-q").fill("à rendre")
        page.locator(f'button[name="ajouter"][value="{copy_id}"]').click()
        page.get_by_role("button", name="Confirmer le prêt du livre").click()
        expect(page.locator(".desk-done")).to_be_visible()
    shop.execute("UPDATE loans SET due_on = '2026-12-01' WHERE copy_id = ?", second)
    page.goto(f"{server.base_url}/comptoir?lecteur={reader}")
    rows = page.locator(".desk-loans tbody tr")
    before = rows.all_inner_texts()
    button = rows.nth(1).get_by_role("button", name="Renouveler Second à rendre")
    button.dblclick()  # le geste répété qu'il faut neutraliser
    expect(
        page.locator(".desk-loans tbody tr").nth(1).get_by_role("button", name=re.compile("Renouvelé"))
    ).to_be_disabled()
    message = page.locator("#comptoir-etat .callout.m-ok")
    expect(message).to_contain_text("« Second à rendre » renouvelé : retour le 01/12/2026, désormais le")
    expect(page.locator("#cp-annonce")).to_contain_text("« Second à rendre » renouvelé")
    after = page.locator(".desk-loans tbody tr").all_inner_texts()
    assert [line.split("\t")[0] for line in after] == [line.split("\t")[0] for line in before]  # rien n'a bougé
    assert shop.execute("SELECT renewals FROM loans WHERE copy_id = ?", second) == [(1,)]
    assert shop.execute("SELECT renewals FROM loans WHERE copy_id = ?", first) == [(0,)]


def test_on_a_phone_five_readers_come_first_and_more_on_demand(browser: Browser, server: Server, shop) -> None:
    for index in range(21):  # de quoi remplir une page et en ouvrir une seconde
        shop.adult(f"Mobile{index:02d}", "Cinq")
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport=PHONE)
    page = context.new_page()
    page.goto(f"{server.base_url}/comptoir")
    visible = page.locator("#cp-lecteurs .desk-pick:visible")
    expect(visible).to_have_count(5)
    expect(page.get_by_role("button", name="Lecteurs suivants")).to_be_hidden()  # ne sauterait plus les masqués
    page.get_by_role("button", name=re.compile(r"Voir \d+ autres lecteurs")).click()
    expect(page.get_by_role("button", name="Lecteurs suivants")).to_be_visible()
    expect(page.locator("#cp-lecteurs .desk-pick-list.all")).to_be_visible()
    assert page.locator("#cp-lecteurs .desk-pick:visible").count() > 5
    expect(page.locator("#cp-lecteurs .desk-pick-list > li:nth-child(6) .desk-pick")).to_be_focused()
    context.close()


def test_with_the_keyboard_open_the_focused_field_stays_above_the_confirmation_bar(
    browser: Browser, server: Server, shop
) -> None:
    """Clavier virtuel simulé : l'écran utile réduit à 460 px. Le champ qui a le focus reste au-dessus de la barre."""
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport={"width": 390, "height": 460})
    page = context.new_page()
    reader = shop.adult("Clara", "Clavier")
    _work, (copy_id,) = shop.book("Petit clavier")
    page.goto(f"{server.base_url}/comptoir?lecteur={reader}&exemplaire={copy_id}")
    field = page.locator(f'input[name="retour_{copy_id}"]')
    field.focus()
    page.wait_for_timeout(200)
    box = field.bounding_box()
    bar = page.locator(".desk-side .counter-bar").bounding_box()
    assert box["y"] + box["height"] <= bar["y"], (box, bar)  # le champ n'est pas caché par la barre
    expect(page.get_by_role("button", name="Confirmer le prêt du livre")).to_be_in_viewport()
    context.close()
