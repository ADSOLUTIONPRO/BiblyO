# SPDX-License-Identifier: EUPL-1.2
"""Parcours du lot L8 dans un vrai navigateur : génération globale, impression sur une feuille entamée, PDF relu,
scanner universel avec une douchette HID simulée (frappe très rapide puis Entrée ou Tab), comptoir, assistant de la
douchette, téléphone. Données inventées ; avec `BIBLYO_CAPTURES=<dossier>`, chaque étape est enregistrée en image."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue

from .. import pdf_reader
from ..factories import make_notice
from .conftest import ADMIN, VOLUNTEER, Server, watch

pytestmark = pytest.mark.e2e
CAPTURES = os.environ.get("BIBLYO_CAPTURES", "")
SCANNER_DELAY = 8  # ms entre deux caractères : une douchette USB typique ; une main tape à plus de 80 ms


def _capture(page: Page, name: str) -> None:
    if CAPTURES:
        folder = Path(CAPTURES)
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f"{name}.png"), full_page=True)


def _axe(page: Page) -> list[str]:
    """Violations graves ou critiques de l'écran tel qu'il est, données et fenêtre ouvertes comprises."""
    results = Axe().run(page).response["violations"]
    return [
        f"{violation['id']} : {node['target']}"
        for violation in results
        if violation["impact"] in ("serious", "critical")
        for node in violation["nodes"]
    ]


def _sql(server: Server, query: str, *params) -> list[tuple]:
    connection = sqlite3.connect(server.settings.database_path)
    try:
        rows = connection.execute(query, params).fetchall()
        connection.commit()
        return rows
    finally:
        connection.close()


def _books(server: Server, prefix: str, count: int) -> list[int]:
    """Des BD inventées, chacune avec un exemplaire « en place », **sans code-barres**, comme celles du Club."""
    database = Database(server.settings.database_path)
    ids = []
    for number in range(1, count + 1):
        work = make_notice(server.settings, f"{prefix} {number}", series=prefix, volume=str(number), authors=("Essai",))
        edition = _sql(server, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
        (copy_id,) = catalogue.add_copies(database, edition, 1, "", None)
        ids.append(copy_id)
    database.dispose()
    marks = ",".join("?" * len(ids))
    _sql(server, f"UPDATE copies SET barcode = NULL, status = 'available' WHERE id IN ({marks})", *ids)  # noqa: S608
    for copy_id in ids:
        _sql(server, "UPDATE copies SET inventory_number = ? WHERE id = ?", f"{9000 + copy_id}", copy_id)
    return ids


def _scanner(page: Page, code: str, suffix: str = "Enter") -> None:
    """Une douchette HID : le code tapé en quelques millisecondes, puis la touche finale."""
    page.keyboard.type(code, delay=SCANNER_DELAY)
    page.keyboard.press(suffix)


def _context(browser: Browser, server: Server, user: str = ADMIN, width: int = 1440):
    return browser.new_context(
        storage_state=server.storage[user], viewport={"width": width, "height": 900}, accept_downloads=True
    )


def test_generation_globale_puis_impression_sur_feuille_entamee(browser: Browser, server: Server) -> None:
    copies = _books(server, "Aventures de Boule", 9)
    context = _context(browser, server)
    page = context.new_page()
    problems = watch(page)
    page.goto(f"{server.base_url}/etiquettes")
    _capture(page, "l8-01-etiquettes")
    page.get_by_role("link", name="Voir la simulation").click()
    expect(page.get_by_role("heading", name="code(s) à attribuer")).to_be_visible()
    before = _sql(server, "SELECT COUNT(*) FROM copies WHERE barcode IS NOT NULL")
    _capture(page, "l8-02-simulation")
    assert _sql(server, "SELECT COUNT(*) FROM copies WHERE barcode IS NOT NULL") == before  # rien d'écrit
    page.get_by_role("button", name="Attribuer ces").click()
    done = "Les codes-barres manquants ont été attribués. Vous pouvez maintenant imprimer"
    expect(page.get_by_role("heading", name=done)).to_be_visible()
    _capture(page, "l8-03-codes-attribues")
    marks = ",".join("?" * len(copies))
    codes = dict(_sql(server, f"SELECT id, barcode FROM copies WHERE id IN ({marks})", *copies))  # noqa: S608
    assert all(codes.values())

    page.goto(f"{server.base_url}/etiquettes/selection?q=Boule&code=a-etiqueter")
    expect(page.get_by_role("heading", name="9 exemplaire(s)")).to_be_visible()
    page.get_by_role("button", name="Imprimer leurs étiquettes").click()
    page.get_by_label("Avery L7160 · J8160").check()
    for position in range(1, 8):  # sept étiquettes déjà décollées
        page.locator(f"label[for=case-{position}]").click()
    _capture(page, "l8-04-planche-entamee")
    page.get_by_role("button", name="Voir l’aperçu").click()
    expect(page.locator(".sheet-svg").first).to_be_visible()
    assert page.locator(".sheet-svg").first.locator(".lslot-occupied").count() == 7
    expect(page.locator(".label-svg")).to_be_visible()
    _capture(page, "l8-05-apercu-a4")
    assert _axe(page) == []
    page.get_by_role("button", name="Préparer le PDF").click()
    expect(page.get_by_role("link", name="Télécharger le PDF")).to_be_visible()
    with page.expect_download() as download:
        page.get_by_role("link", name="Télécharger le PDF").click()
    data = Path(download.value.path()).read_bytes()
    printed = [code.text for sheet in pdf_reader.pages(data) for code in sheet.codes]
    assert sorted(printed) == sorted(codes.values())
    batch = _sql(server, "SELECT id FROM label_batches ORDER BY id DESC LIMIT 1")[0][0]
    first = _sql(server, "SELECT sheet_number, position FROM label_batch_items WHERE batch_id = ? AND rank = 1", batch)
    assert first == [(1, 8)]  # commence à la case 8
    page.get_by_role("button", name="Imprimées et collées").click()
    expect(page.get_by_text("Imprimé et collé").first).to_be_visible()
    _capture(page, "l8-06-lot-confirme")
    assert problems == []
    context.close()


def test_douchette_hors_de_tout_champ_ouvre_la_fiche(browser: Browser, server: Server) -> None:
    (copy_id,) = _books(server, "Les Tuniques", 1)
    _sql(server, "UPDATE copies SET barcode = 'EX-900001' WHERE id = ?", copy_id)
    context = _context(browser, server)
    page = context.new_page()
    problems = watch(page)
    dialog = page.locator("dialog#scanner")
    page.goto(f"{server.base_url}/catalogue")
    page.locator("h1").click()  # aucun champ actif
    _scanner(page, "EX-900001")
    page.wait_for_timeout(300)
    expect(dialog).to_be_hidden()  # poste pas encore déclaré poste de prêt : la lecture hors champ ne fait rien
    page.goto(f"{server.base_url}/scanner/douchette")
    page.get_by_label("Ce poste est un poste de prêt de la bibliothèque").check()
    expect(page.get_by_text("Lecture partout : active sur ce poste.")).to_be_visible()
    page.goto(f"{server.base_url}/catalogue")
    page.locator("h1").click()
    _scanner(page, "EX-900001")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading", name="Les Tuniques 1")).to_be_visible()
    expect(dialog.get_by_role("link", name="Prêter")).to_be_visible()
    _capture(page, "l8-07-scan-fiche-immediate")
    assert _axe(page) == []
    page.keyboard.press("Escape")
    # Une saisie dans un champ n'est jamais interceptée, même très rapide : le formulaire reçoit son Entrée.
    page.goto(f"{server.base_url}/etiquettes/selection")
    page.locator("input#q").click()
    _scanner(page, "EX-900001")
    page.wait_for_url("**/etiquettes/selection?q=EX-900001*")
    expect(dialog).to_be_hidden()
    # Une carte « P-… » lue hors d'un champ n'ouvre pas le prêt par le raccourci « P ».
    page.goto(f"{server.base_url}/catalogue")
    page.locator("h1").click()
    _scanner(page, "P-000999")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text("Code inconnu")).to_be_visible()
    assert page.url.endswith("/catalogue")
    # Le bouton permanent ouvre la même fenêtre, et Tab suffit à valider.
    page.keyboard.press("Escape")
    page.get_by_role("link", name="Scanner").first.click()
    expect(dialog).to_be_visible()
    _scanner(page, "EX-900001", "Tab")
    expect(dialog.get_by_role("heading", name="Les Tuniques 1")).to_be_visible()
    assert problems == []
    context.close()


def test_un_isbn_scanne_demande_le_choix_de_l_exemplaire(browser: Browser, server: Server) -> None:
    database = Database(server.settings.database_path)
    work = make_notice(server.settings, "Le Pays maudit", isbn="9782800109015", series="Les Hommes", volume="1")
    edition = _sql(server, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    (copy_id,) = catalogue.add_copies(database, edition, 1, "", None)
    database.dispose()
    context = _context(browser, server)
    page = context.new_page()
    searches = _sql(server, "SELECT COUNT(*) FROM enrichment_searches")
    page.goto(f"{server.base_url}/scanner")
    _scanner(page, "9782800109015")
    expect(page.get_by_text("pas l’étiquette d’un exemplaire")).to_be_visible()
    expect(page.get_by_role("link", name="Prêter")).to_have_count(0)
    _capture(page, "l8-08-isbn-choix")
    assert _axe(page) == []
    page.get_by_role("button", name="Choisir l’exemplaire").click()
    expect(page.get_by_role("link", name="Modifier l’exemplaire")).to_be_visible()
    page.locator("#scan-page-code").focus()
    _scanner(page, "9782800152851")  # absent du catalogue
    expect(page.get_by_text("pas au catalogue")).to_be_visible()
    expect(page.get_by_role("button", name="Rechercher à la BnF et au Sudoc")).to_be_visible()
    _capture(page, "l8-09-isbn-inconnu")
    assert _sql(server, "SELECT COUNT(*) FROM enrichment_searches") == searches  # aucune recherche lancée seule
    assert copy_id
    context.close()


def test_comptoir_avec_douchette(browser: Browser, server: Server) -> None:
    copies = _books(server, "Spirou au comptoir", 2)
    for number, copy_id in enumerate(copies, start=1):
        _sql(server, "UPDATE copies SET barcode = ? WHERE id = ?", f"EX-91000{number}", copy_id)
    from biblyo.modules.patrons import service as patrons
    from biblyo.modules.patrons.service import PersonInput, Registration

    database = Database(server.settings.database_path)
    person = PersonInput(first_name="Lou", last_name="Martin", category="adult", card_number="CARTE-L8",
                         email="lou@example.org", phone="0601020304")  # fmt: skip
    patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)
    database.dispose()
    context = _context(browser, server, VOLUNTEER)
    page = context.new_page()
    problems = watch(page)
    page.goto(f"{server.base_url}/comptoir")
    expect(page.locator("#cp-saisie")).to_be_focused()
    _scanner(page, "CARTE-L8")
    expect(page.get_by_text("Lou Martin").first).to_be_visible()
    _scanner(page, "EX-910001", "Tab")  # douchette réglée en Tab
    _scanner(page, "EX-910001")  # lue deux fois par erreur
    _scanner(page, "EX-910002")
    expect(page.get_by_role("button", name="Confirmer le prêt de 2 livres")).to_be_visible()
    expect(page.locator("#cp-saisie")).to_be_focused()
    _capture(page, "l8-10-comptoir-douchette")
    page.get_by_role("button", name="Confirmer le prêt de 2 livres").click()
    expect(page.get_by_text("Prêt enregistré")).to_be_visible()
    marks = ",".join("?" * len(copies))
    query = f"SELECT COUNT(*) FROM loans WHERE copy_id IN ({marks}) AND returned_at IS NULL"  # noqa: S608
    assert _sql(server, query, *copies) == [(2,)]
    assert problems == []
    context.close()


def test_assistant_de_la_douchette(browser: Browser, server: Server) -> None:
    context = _context(browser, server, VOLUNTEER)
    page = context.new_page()
    problems = watch(page)
    page.goto(f"{server.base_url}/scanner/douchette")
    field = page.locator("#essai")
    result = page.locator("#douchette-resultat")
    field.focus()
    _scanner(page, "BIBLYO-TEST")
    expect(result.get_by_text("Douchette prête")).to_be_visible()
    _capture(page, "l8-11-douchette-prete")
    field.focus()
    _scanner(page, "BIBLYO)TEST")  # douchette en QWERTY sur un poste AZERTY
    expect(result.get_by_text("Code non reconnu")).to_be_visible()
    expect(result.get_by_text("AZERTY").first).to_be_visible()
    field.focus()
    page.keyboard.type("BIBLYO-TEST", delay=SCANNER_DELAY)  # sans touche finale
    expect(result.get_by_text("Suffixe non détecté")).to_be_visible()
    _capture(page, "l8-12-douchette-suffixe")
    for code in ("BIBLYO-TEST", "9782123456803", "BIBLYO-INCONNU"):  # trois lectures de suite
        field.focus()
        _scanner(page, code)
        expect(field).to_have_value("")
    checklist = page.locator("#essai-liste")
    expect(checklist.locator(".setup-check.is-ok")).to_have_count(6)  # restent : étiquette d'un livre, inventaire
    _capture(page, "l8-13-douchette-liste")
    assert _axe(page) == []
    assert problems == []
    context.close()


@pytest.mark.parametrize("path", ["/etiquettes", "/etiquettes/reglages", "/scanner", "/scanner/douchette",
                                  "/scanner/inventaire", "/etiquettes/imprimer?code=avec"])  # fmt: skip
def test_telephone_sans_defilement_horizontal(browser: Browser, server: Server, path: str) -> None:
    context = _context(browser, server, width=390)
    page = context.new_page()
    response = page.goto(f"{server.base_url}{path}")
    assert response is not None and response.status == 200, path  # une page d'erreur ne déborde pas non plus
    page.wait_for_load_state("networkidle")
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"{path} déborde de {overflow} px"
    name = path.strip("/").replace("/", "-").replace("?", "-").replace("=", "-")
    _capture(page, f"l8-tel-{name}")
    context.close()
