# SPDX-License-Identifier: EUPL-1.2
"""Le comptoir dans un vrai navigateur : douchette, clavier seul, téléphone, accessibilité. Personnes inventées."""

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
from .conftest import ADMIN, NEXT_STOP, VOLUNTEER, Server, watch

pytestmark = pytest.mark.e2e
BLOCKING = {"serious", "critical"}
WIDE = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def desk(server: Server) -> Iterator[SimpleNamespace]:
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

    def adult(first: str, last: str, card: str) -> int:
        person = PersonInput(
            first_name=first, last_name=last, category="adult", email=f"{first.lower()}@example.org", card_number=card
        )
        return service.register(database, Registration(person=person, homonyms_confirmed=True), None)

    def copies(title: str, count: int) -> list[str]:
        work = make_notice(server.settings, title)
        edition = execute("SELECT id FROM editions WHERE work_id = ?", work)[0][0]
        ids = catalogue.add_copies(database, edition, count, "", None)
        codes = []
        for copy_id in ids:
            execute("UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
            codes.append(execute("SELECT barcode FROM copies WHERE id = ?", copy_id)[0][0])
        return codes

    yield SimpleNamespace(adult=adult, copies=copies, execute=execute)
    database.dispose()


@pytest.fixture
def page(browser: Browser, server: Server) -> Iterator[Page]:
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport=WIDE)
    instance = context.new_page()
    yield instance
    context.close()


def _open(page: Page, server: Server) -> None:
    page.goto(f"{server.base_url}/comptoir")
    expect(page.locator("#cp-saisie")).to_be_focused()


def _scan(page: Page, code: str) -> None:
    """Une douchette : le code, puis Entrée, sans aucun délai."""
    page.locator("#cp-saisie").press_sequentially(code + "\n", delay=0)


def test_a_scanner_chains_the_codes_without_delay(page: Page, server: Server, desk) -> None:
    problems = watch(page)
    desk.adult("Basile", "Quignon", "CQ-1")
    codes = desk.copies("Les Bijoux de la Castafiore", 3)
    _open(page, server)
    for code in [codes[0], "CQ-1", codes[1], codes[2]]:
        _scan(page, code)  # sans attendre la réponse : les codes partent un par un, dans l'ordre
    expect(page.locator(".counter-line")).to_have_count(3)
    expect(page.locator(".desk-reader").get_by_text("Basile Quignon", exact=True)).to_be_visible()
    expect(page.locator("#cp-saisie")).to_be_focused()
    expect(page.locator("#cp-saisie")).to_have_value("")
    page.get_by_role("button", name="Confirmer le prêt").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    expect(page.locator(".counter-line")).to_have_count(0)
    assert not problems


def test_an_ambiguity_holds_the_following_scans(page: Page, server: Server, desk) -> None:
    (first,) = desk.copies("Vol 714", 1)
    (second,) = desk.copies("Coke en stock", 1)
    patron = desk.adult("Ambre", "Ventadour", "CV-1")
    desk.execute("UPDATE patron_cards SET number = ? WHERE patron_id = ?", first, patron)
    sent: list[str] = []
    page.on("request", lambda request: sent.append(request.url))
    _open(page, server)
    _douchette(page, first)
    expect(page.get_by_role("heading", name="Quel résultat ?")).to_be_visible()
    expect(page.locator("#cp-saisie")).to_be_focused()  # jamais le premier choix : son Entrée choisirait en silence
    _douchette(page, second)  # la douchette continue : rien ne s'ajoute, rien n'est choisi
    expect(page.get_by_text("1 code en attente.")).to_be_visible()
    expect(page.locator(".counter-line")).to_have_count(0)
    assert not [url for url in sent if url.endswith("/comptoir/choix")]
    page.locator(".counter-choices").get_by_role("button", name="Exemplaire").click()
    expect(page.locator(".counter-line")).to_have_count(2)
    assert not [url for url in sent if first in url or second in url]  # jamais dans une adresse


def test_the_whole_counter_works_with_the_keyboard_alone(page: Page, server: Server, desk) -> None:
    desk.adult("Clarisse", "Oudin", "CO-1")
    (code,) = desk.copies("Objectif Lune", 1)
    _open(page, server)
    _scan(page, "CO-1")
    expect(page.get_by_text("Clarisse Oudin")).to_be_visible()
    _scan(page, code)
    expect(page.locator(".counter-line")).to_have_count(1)
    button = page.get_by_role("button", name="Confirmer le prêt")
    button.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()


def test_a_copy_on_loan_never_shows_its_holder(page: Page, server: Server, desk) -> None:
    desk.adult("Delphine", "Pasquier", "DP-1")
    (code,) = desk.copies("On a marché sur la Lune", 1)
    _open(page, server)
    _scan(page, code)
    _scan(page, "DP-1")
    expect(page.locator(".counter-line")).to_have_count(1)
    page.get_by_role("button", name="Confirmer le prêt").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    _scan(page, code)
    expect(page.get_by_text("déjà en prêt")).to_be_visible()
    # Hors de la liste des lecteurs (où chaque lecteur en service figure, sans lien avec ce livre), personne n'est nommé
    rest = page.evaluate(
        "() => { const zone = document.querySelector('#comptoir-etat').cloneNode(true);"
        " zone.querySelectorAll('.desk-reader').forEach((el) => el.remove()); return zone.textContent; }"
    )
    assert "Pasquier" not in rest and "DP-1" not in rest


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_counter_has_no_serious_accessibility_issue(browser: Browser, server: Server, desk, theme: str) -> None:
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport=WIDE, color_scheme=theme)
    page = context.new_page()
    desk.adult(f"Eugène{theme}", "Roussel", f"ER-{theme[:1]}")
    codes = desk.copies(f"Tintin {theme}", 1)
    _open(page, server)
    _scan(page, f"ER-{theme[:1]}")
    _scan(page, codes[0])
    expect(page.locator(".counter-line")).to_have_count(1)
    results = Axe().run(page)
    serious = [item for item in results.response["violations"] if item["impact"] in BLOCKING]
    assert not serious, serious
    context.close()


def test_counter_on_a_phone(browser: Browser, server: Server, desk) -> None:
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport=PHONE)
    page = context.new_page()
    desk.adult("Fabien", "Lacaze", "FL-1")
    (code,) = desk.copies("Le Temple du Soleil", 1)
    desk.execute("UPDATE copies SET condition = 'damaged' WHERE barcode = ?", code)  # le plus long message d'une ligne
    _open(page, server)
    _scan(page, "FL-1")
    _scan(page, code)
    expect(page.locator(".counter-line")).to_have_count(1)
    expect(page.get_by_text("État signalé")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    button = page.get_by_role("button", name="Confirmer le prêt")
    expect(button).to_be_visible()
    assert button.bounding_box()["height"] >= 44
    context.close()


def test_a_return_scan_says_where_to_shelve_and_can_be_undone(browser: Browser, server: Server, desk) -> None:
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport=WIDE)
    page = context.new_page()
    desk.adult("Gisèle", "Marchand", "GM-1")
    first, second = desk.copies("Tintin au Tibet", 2)
    _open(page, server)
    for code in ["GM-1", first, second]:
        _scan(page, code)
    expect(page.locator(".counter-line")).to_have_count(2)
    page.get_by_role("button", name="Confirmer le prêt").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    page.goto(f"{server.base_url}/comptoir/retours")
    expect(page.locator("#cp-saisie")).to_be_focused()
    _scan(page, first)
    _scan(page, second)
    expect(page.locator(".return-line")).to_have_count(2)
    assert "Marchand" not in page.locator("#retours-lignes").inner_text()  # un bénévole ne voit pas l'emprunteur
    expect(page.locator("#cp-saisie")).to_be_focused()
    page.locator(".return-line").first.get_by_role("button", name="Annuler").click()
    expect(page.get_by_text("prêt court à nouveau")).to_be_visible()
    expect(page.locator(".return-line")).to_have_count(2)
    context.close()


def test_returns_on_a_phone(browser: Browser, server: Server, desk) -> None:
    context = browser.new_context(storage_state=server.storage[VOLUNTEER], viewport=PHONE)
    page = context.new_page()
    page.goto(f"{server.base_url}/comptoir/retours")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    results = Axe().run(page)
    assert not [item for item in results.response["violations"] if item["impact"] in BLOCKING]
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_lists_and_copy_sheet_have_no_serious_accessibility_issue(
    browser: Browser, server: Server, desk, theme: str
) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport=WIDE, color_scheme=theme)
    page = context.new_page()
    desk.adult(f"Hugo{theme}", "Bastide", f"HB-{theme[:1]}")
    (code,) = desk.copies(f"Les Cigares {theme}", 1)
    _open(page, server)
    _scan(page, f"HB-{theme[:1]}")
    _scan(page, code)
    expect(page.locator(".counter-line")).to_have_count(1)
    page.get_by_role("button", name="Confirmer le prêt").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    copy_id = desk.execute("SELECT id FROM copies WHERE barcode = ?", code)[0][0]
    for path in ("/prets", "/prets?vue=historique", f"/exemplaires/{copy_id}", "/"):
        page.goto(f"{server.base_url}{path}")
        results = Axe().run(page)
        serious = [item for item in results.response["violations"] if item["impact"] in BLOCKING]
        assert not serious, (path, serious)
    page.goto(f"{server.base_url}/prets")
    expect(page.get_by_text(f"Hugo{theme} Bastide")).to_be_visible()
    context.close()


def test_reminders_page_shows_contacts_on_request(browser: Browser, server: Server, desk) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport=WIDE)
    page = context.new_page()
    desk.adult("Irène", "Courtial", "IC-1")
    (code,) = desk.copies("L’Île Noire", 1)
    _open(page, server)
    _scan(page, "IC-1")
    _scan(page, code)
    expect(page.locator(".counter-line")).to_have_count(1)
    page.get_by_role("button", name="Confirmer le prêt").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    late = "UPDATE loans SET due_on = '2020-02-01' WHERE copy_id = (SELECT id FROM copies WHERE barcode = ?)"
    desk.execute(late, code)
    page.goto(f"{server.base_url}/prets/a-rappeler")
    card = page.locator(".remind-card", has_text="L’Île Noire")
    expect(card).to_be_visible()
    assert "irène@example.org" not in card.inner_text()
    card.get_by_role("button", name="Afficher les coordonnées").click()
    expect(card.get_by_text("irène@example.org")).to_be_visible()
    results = Axe().run(page)
    assert not [item for item in results.response["violations"] if item["impact"] in BLOCKING]
    card.get_by_label("Résultat").select_option("message_left")
    card.get_by_role("button", name="Noter le rappel").click()
    expect(page.get_by_text("Le rappel est noté")).to_be_visible()
    context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_loan_rules_page(browser: Browser, server: Server, theme: str) -> None:
    context = browser.new_context(storage_state=server.storage[ADMIN], viewport=WIDE, color_scheme=theme)
    page = context.new_page()
    page.goto(f"{server.base_url}/prets/regles")
    expect(page.get_by_role("heading", name="Durées et plafonds")).to_be_visible()
    results = Axe().run(page)
    assert not [item for item in results.response["violations"] if item["impact"] in BLOCKING]
    context.close()
    phone = browser.new_context(storage_state=server.storage[ADMIN], viewport=PHONE, color_scheme=theme)
    small = phone.new_page()
    small.goto(f"{server.base_url}/prets/regles")
    assert small.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    phone.close()


def test_direct_shortcuts_open_the_counter_and_returns(page: Page, server: Server) -> None:
    page.goto(f"{server.base_url}/")
    page.keyboard.press("p")
    expect(page).to_have_url(f"{server.base_url}/comptoir")
    page.locator("#cp-saisie").blur()  # dans le champ du comptoir, « r » s'écrit : il faut d'abord en sortir
    page.keyboard.press("r")
    expect(page).to_have_url(f"{server.base_url}/comptoir/retours")


def test_a_scan_after_a_button_is_still_a_scan(page: Page, server: Server, desk) -> None:
    """Sous WebKit et Firefox, un bouton cliqué ne prend pas le focus : le scan suivant ne doit pas partir vers lui."""
    desk.adult("Jules", "Ferrand", "JF-1")
    first, second = desk.copies("Le Crabe aux pinces d’or", 2)
    _open(page, server)
    _scan(page, "JF-1")
    _scan(page, first)
    page.get_by_role("button", name="Confirmer le prêt").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    expect(page.locator(".counter-line")).to_have_count(0)
    _scan(page, second)  # sans toucher au focus, comme une douchette
    expect(page.locator(".counter-line")).to_have_count(1)
    expect(page.get_by_text("Prêt enregistré")).to_have_count(0)
    page.goto(f"{server.base_url}/comptoir/retours")
    _scan(page, first)
    page.locator(".return-line").first.get_by_role("button", name="Annuler").click()
    expect(page.get_by_text("prêt court à nouveau")).to_be_visible()
    _scan(page, second)
    expect(page.get_by_text("rien à rendre")).to_be_visible()


# --- Non-régression du correctif de focus et de scan (PC4) -----------------------------------------------------------
PREVIOUS_STOP = "Alt+Shift+Tab" if NEXT_STOP == "Alt+Tab" else "Shift+Tab"


def _douchette(page: Page, code: str) -> None:
    """Une douchette écrit là où se trouve le focus, sans le choisir et sans attendre : un focus perdu, ou un code qui
    part pendant une autre requête, fait échouer le parcours."""
    page.keyboard.type(code, delay=0)
    page.keyboard.press("Enter")


def _posts(page: Page) -> list[str]:
    sent: list[str] = []
    page.on("request", lambda request: sent.append(request.url.split("/", 3)[3]) if request.method == "POST" else None)
    return sent


def _only_scans(sent: list[str], *allowed: str) -> None:
    """Toute requête qui n'est pas un scan correspond à un geste explicite, et à lui seul."""
    unexpected = [path for path in sent if path not in ("comptoir/scan", "comptoir/retours/scan", *allowed)]
    assert not unexpected, unexpected


def test_scan_after_renewal_unknown_code_and_duplicate(page: Page, server: Server, desk) -> None:
    patron_code = "KR-1"
    desk.adult("Karim", "Renard", patron_code)
    first, second = desk.copies("L’Étoile mystérieuse", 2)
    _open(page, server)
    _douchette(page, patron_code)
    _douchette(page, first)
    page.get_by_role("button", name="Confirmer le prêt").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    sent = _posts(page)
    _douchette(page, patron_code)
    page.get_by_role("button", name=re.compile("Renouveler")).click()
    _douchette(page, "INCONNU-404")  # aussitôt après le renouvellement, sans attendre ni toucher au focus
    expect(page.get_by_text("Code inconnu")).to_be_visible()
    _douchette(page, second)  # juste après une erreur
    _douchette(page, second)  # deux fois le même exemplaire
    expect(page.locator(".counter-line")).to_have_count(1)
    expect(page.get_by_text("déjà dans la liste")).to_be_visible()
    renewals = [path for path in sent if path.startswith("comptoir/renouveler/")]
    assert len(renewals) == 1
    _only_scans(sent, *renewals)


def test_chained_loans_and_returns_without_reload(page: Page, server: Server, desk) -> None:
    readers = [("Lou", "Chaine", "LC-1"), ("Max", "Chaine", "LC-2"), ("Noa", "Chaine", "LC-3")]
    for first_name, last_name, card in readers:
        desk.adult(first_name, last_name, card)
    codes = desk.copies("Tintin en Amérique", 3)
    _open(page, server)
    sent = _posts(page)
    for (_first, _last, card), code in zip(readers, codes, strict=True):
        _douchette(page, card)
        _douchette(page, code)
        expect(page.locator(".counter-line")).to_have_count(1)
        button = page.get_by_role("button", name="Confirmer le prêt")
        button.focus()
        page.keyboard.press("Enter")  # au clavier
        expect(page.get_by_text("Prêt enregistré")).to_be_visible()
        expect(page.locator(".counter-line")).to_have_count(0)
    assert sent.count("comptoir/valider") == 3
    _only_scans(sent, "comptoir/valider")
    page.goto(f"{server.base_url}/comptoir/retours")
    sent = _posts(page)
    for code in codes:
        _douchette(page, code)
    expect(page.locator(".return-line")).to_have_count(3)
    page.locator(".return-line").first.get_by_role("button", name="Annuler").click()
    expect(page.get_by_text("prêt court à nouveau")).to_be_visible()
    _douchette(page, codes[-1])  # juste après « Annuler » : le livre se rend de nouveau
    expect(page.locator(".return-line")).to_have_count(4)
    cancels = [path for path in sent if path.endswith("/annuler")]
    assert len(cancels) == 1
    _only_scans(sent, *cancels)


def test_keyboard_tab_and_shift_tab_keep_the_scan_safe(page: Page, server: Server, desk) -> None:
    desk.adult("Pia", "Tabulation", "PT-1")
    first, second = desk.copies("Le Sceptre d’Ottokar", 2)
    _open(page, server)
    sent = _posts(page)
    _douchette(page, "PT-1")
    _douchette(page, first)
    field = page.locator("#cp-saisie")
    expect(page.locator(".counter-line")).to_have_count(1)
    page.keyboard.press(NEXT_STOP)
    expect(field).not_to_be_focused()
    page.keyboard.press(PREVIOUS_STOP)
    expect(field).to_be_focused()
    _douchette(page, second)
    expect(page.locator(".counter-line")).to_have_count(2)
    focused = "document.activeElement && document.activeElement.textContent.trim().startsWith('Confirmer le prêt')"
    for _step in range(40):  # au clavier jusqu'au bouton Confirmer
        if page.evaluate(focused):
            break
        page.keyboard.press(NEXT_STOP)
    page.keyboard.press("Enter")
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    expect(field).to_be_focused()  # le focus revient au champ : la douchette écrit au bon endroit
    assert sent.count("comptoir/valider") == 1
    _only_scans(sent, "comptoir/valider", "comptoir/etat")


# --- Prêter depuis la fiche d'un livre --------------------------------------------------------------------------------
def _work_of(desk, code: str) -> int:
    query = "SELECT e.work_id FROM copies c JOIN editions e ON e.id = c.edition_id WHERE c.barcode = ?"
    return desk.execute(query, code)[0][0]


def test_lend_from_a_book_with_one_copy_then_back_to_the_book(page: Page, server: Server, desk) -> None:
    problems = watch(page)
    desk.adult("Romain", "Fiche", "RF-1")
    (code,) = desk.copies("Le Petit Vingtième", 1)
    work = _work_of(desk, code)
    page.goto(f"{server.base_url}/catalogue/notices/{work}")
    page.get_by_role("link", name="Prêter").click()
    expect(page.locator(".counter-line")).to_have_count(1)
    expect(page.get_by_text("Choisissez d’abord le lecteur")).to_be_visible()
    expect(page.get_by_role("button", name="Confirmer le prêt")).to_be_disabled()
    expect(page.locator("#cp-saisie")).to_be_focused()
    _douchette(page, "RF-1")
    expect(page.get_by_text("1 livre · Romain Fiche")).to_be_visible()
    page.get_by_role("button", name="Confirmer le prêt").click()  # jamais validé sans ce geste
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    page.get_by_role("link", name="Revenir à « Le Petit Vingtième »").click()
    expect(page).to_have_url(f"{server.base_url}/catalogue/notices/{work}")
    assert not problems


def test_lend_from_a_book_with_several_copies(page: Page, server: Server, desk) -> None:
    codes = desk.copies("Tintin et les Picaros", 3)
    work = _work_of(desk, codes[0])
    page.goto(f"{server.base_url}/catalogue/notices/{work}")
    page.get_by_role("link", name="Prêter").click()
    expect(page.get_by_role("heading", name="Prêter ou rendre « Tintin et les Picaros »")).to_be_visible()
    boxes = page.locator('input[name="exemplaire"]')
    expect(boxes).to_have_count(3)
    expect(boxes.nth(0)).to_be_checked()
    boxes.nth(1).check()
    page.get_by_role("button", name="Prêter les exemplaires cochés").click()
    expect(page.locator(".counter-line")).to_have_count(2)


def test_the_reader_already_chosen_is_proposed_when_adding_from_a_book(page: Page, server: Server, desk) -> None:
    desk.adult("Sacha", "Reprise", "SR-1")
    (first,) = desk.copies("Le Trésor de Rackham le Rouge", 1)
    (second,) = desk.copies("Les Bijoux de Bianca", 1)
    _open(page, server)
    _douchette(page, "SR-1")
    _douchette(page, first)
    expect(page.locator(".counter-line")).to_have_count(1)
    page.goto(f"{server.base_url}/catalogue/notices/{_work_of(desk, second)}")
    page.get_by_role("link", name="Prêter").click()
    # Proposé, jamais repris en silence : le lecteur n'est choisi qu'au clic (UX-Circulation 2.0, 6.3)
    expect(page.get_by_text("Un prêt était en préparation pour Sacha Reprise")).to_be_visible()
    expect(page.get_by_role("button", name="Confirmer le prêt")).to_be_disabled()
    page.get_by_role("button", name="Reprendre ce lecteur").click()
    expect(page.locator(".counter-line")).to_have_count(2)
    expect(page.get_by_role("button", name="Confirmer le prêt de 2 livres")).to_be_enabled()


def test_a_book_whose_copy_cannot_leave_explains_why(page: Page, server: Server, desk) -> None:
    (code,) = desk.copies("Le Lac aux requins", 1)
    desk.execute("UPDATE copies SET status = 'in_repair' WHERE barcode = ?", code)
    page.goto(f"{server.base_url}/catalogue/notices/{_work_of(desk, code)}")
    page.get_by_role("link", name="Prêter").click()
    expect(page.get_by_text("Aucun exemplaire ne peut sortir maintenant")).to_be_visible()
    expect(page.get_by_text("Cet exemplaire est « en réparation »")).to_be_visible()
    expect(page.get_by_text("Indisponible · en réparation")).to_be_visible()
    expect(page.get_by_role("button", name="Prêter les exemplaires cochés")).to_have_count(0)


def test_the_loans_tab_of_a_book_for_a_volunteer(page: Page, server: Server, desk) -> None:
    desk.adult("Victor", "Onglet", "VO-1")
    (code,) = desk.copies("L’Affaire Tournesol", 1)
    _open(page, server)
    _douchette(page, "VO-1")
    _douchette(page, code)
    page.get_by_role("button", name="Confirmer le prêt").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    page.goto(f"{server.base_url}/catalogue/notices/{_work_of(desk, code)}?onglet=prets")
    expect(page.get_by_role("heading", name="En prêt")).to_be_visible()
    assert "Victor Onglet" not in page.locator("main").inner_text()
    results = Axe().run(page)
    assert not [item for item in results.response["violations"] if item["impact"] in BLOCKING]
