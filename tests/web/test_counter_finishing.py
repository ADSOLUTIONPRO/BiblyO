# SPDX-License-Identifier: EUPL-1.2
"""Finitions d'UX-Circulation 2.0 relevées à la validation graphique : U15 à U19, chacune avec sa preuve."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.circulation import rules, service
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice

RIGHTS = frozenset({rules.LEND, rules.RENEW})


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _execute(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _book(settings, database, title):
    work = make_notice(settings, title)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    (copy_id,) = catalogue.add_copies(database, edition, 1, "", None)
    _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return work, copy_id


def _person(database, first, last):
    person = PersonInput(first_name=first, last_name=last, category="adult", email=f"{first.lower()}@example.org")
    return patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _hidden(html):
    return [
        pair
        for pair in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html)
        if pair[0] not in ("_csrf", "code")
    ]


def _form(pairs):
    found: dict[str, list[str]] = {}
    for name, value in pairs:
        found.setdefault(name, []).append(value)
    return found


@pytest.fixture
def desk(settings, database):
    """Un lecteur et deux prêts : « Premier » prêté d'abord, avec l'échéance la plus lointaine ; puis « Second »."""
    reader = _person(database, "Claire", "Roux")
    _work_a, first = _book(settings, database, "Premier prêté")
    _work_b, second = _book(settings, database, "Second prêté")
    one = service.lend(database, LendRequest(reader, (LendItem(first),), rules.MODE_SELF), Actor(None, RIGHTS))
    two = service.lend(database, LendRequest(reader, (LendItem(second),), rules.MODE_SELF), Actor(None, RIGHTS))
    _execute(settings, "UPDATE loans SET due_on = '2026-12-20' WHERE id = ?", one.loan_ids[0])
    _execute(settings, "UPDATE loans SET due_on = '2026-12-01' WHERE id = ?", two.loan_ids[0])
    return {"reader": reader, "first": one.loan_ids[0], "second": two.loan_ids[0]}


def _titles(html):
    table = html[html.index('id="cp-cours"') :]
    return re.findall(r"<td>((?:Premier|Second) prêté)</td>", table)


def _renew(browser, page_html, loan_id):
    return browser.post(f"/comptoir/renouveler/{loan_id}", data=_form(_hidden(page_html))).text


# --- U15 : le renouvellement s'annonce, après la réponse du serveur -----------------------------------------------
def test_a_renewal_says_the_title_the_old_and_the_new_due_date(client_as, settings, desk):
    browser = client_as("volunteer")
    page = browser.get(f"/comptoir?lecteur={desk['reader']}").text
    html = _renew(browser, page, desk["first"])
    new_due = _rows(settings, "SELECT due_on FROM loans WHERE id = ?", desk["first"])[0][0]
    shown = f"{new_due[8:10]}/{new_due[5:7]}/{new_due[:4]}"
    message = f"« Premier prêté » renouvelé : retour le 20/12/2026, désormais le {shown}."
    assert message in html
    assert re.search(r'<div class="callout m-ok" role="status">', html)  # annoncée, de ton positif
    assert f"<span hidden data-annonce>{message}</span>" in html  # recopiée dans la région permanente du comptoir
    assert "Renouvelé" in html and 'class="row-done"' in html


def test_a_refused_renewal_is_never_announced_as_done(client_as, settings, desk):
    _execute(settings, "UPDATE loans SET renewals = 5 WHERE id = ?", desk["first"])
    browser = client_as("volunteer")
    page = browser.get(f"/comptoir?lecteur={desk['reader']}").text
    html = _renew(browser, page, desk["first"])
    assert "renouvelé :" not in html and "Nombre maximal de renouvellements" in html
    assert _rows(settings, "SELECT due_on FROM loans WHERE id = ?", desk["first"]) == [("2026-12-20",)]


def test_the_counter_page_has_a_permanent_announcement_region(client_as):
    html = client_as("volunteer").get("/comptoir").text
    assert re.search(r'<div class="sr" id="cp-annonce" aria-live="polite" aria-atomic="true"', html)


# --- U16 : ordre stable, aucune répétition -------------------------------------------------------------------------
def test_renewing_never_moves_a_line(client_as, desk):
    browser = client_as("volunteer")
    page = browser.get(f"/comptoir?lecteur={desk['reader']}").text
    before = _titles(page)
    assert before == ["Premier prêté", "Second prêté"]  # dans l'ordre des prêts, pas des échéances
    after = _titles(_renew(browser, page, desk["second"]))  # son échéance dépasse désormais celle du premier
    assert after == before


def test_a_repeated_click_does_not_renew_twice(client_as, settings, desk):
    browser = client_as("volunteer")
    page = browser.get(f"/comptoir?lecteur={desk['reader']}").text
    done = _renew(browser, page, desk["first"])
    assert ("vient_renouvele", str(desk["first"])) in _hidden(done)
    assert 'disabled aria-label="Renouvelé à l’instant : Premier prêté"' in done
    again = _renew(browser, done, desk["first"])  # le même geste répété, sur l'écran rafraîchi
    assert "vient d’être renouvelé : rien n’a été refait" in again
    assert _rows(settings, "SELECT renewals FROM loans WHERE id = ?", desk["first"]) == [(1,)]


def test_a_double_submission_of_the_same_screen_is_refused_by_the_server(client_as, settings, desk):
    browser = client_as("volunteer")
    page = browser.get(f"/comptoir?lecteur={desk['reader']}").text
    _renew(browser, page, desk["first"])
    stale = _renew(browser, page, desk["first"])  # la même requête, rejouée (double clic, file htmx)
    assert "La situation vient de changer" in stale
    assert _rows(settings, "SELECT renewals FROM loans WHERE id = ?", desk["first"]) == [(1,)]


def test_another_gesture_allows_a_wanted_second_renewal(client_as, settings, desk):
    browser = client_as("volunteer")
    page = browser.get(f"/comptoir?lecteur={desk['reader']}").text
    done = _renew(browser, page, desk["first"])
    fresh = browser.get(f"/comptoir?lecteur={desk['reader']}").text  # l'écran rouvert : plus de marqueur
    assert "vient_renouvele" not in fresh and "Renouvelé à l’instant" not in fresh
    assert done != fresh


# --- U17 : adhésion échue, avertissement seulement -----------------------------------------------------------------
def test_an_expired_membership_is_shown_on_the_card_and_in_the_summary_without_blocking(client_as, settings, database):
    reader = _person(database, "Échu", "Adhérent")
    _execute(settings, "UPDATE patrons SET membership_expires_on = '2020-01-01' WHERE id = ?", reader)
    _work, copy_id = _book(settings, database, "Livre permis")
    browser = client_as("volunteer")
    empty = browser.get(f"/comptoir?lecteur={reader}").text
    card = empty[empty.index('id="cp-lecteur"') : empty.index('id="cp-liste"')]
    recap = empty[empty.index('id="cp-recap"') :]
    assert "Adhésion échue" in card and "le prêt reste possible" in recap.split("</aside>")[0]
    ready = browser.post("/comptoir/etat", data={"lecteur": str(reader), "ex": str(copy_id)}).text
    assert re.search(r'<button type="submit" class="btn btn-p btn-lg"[^>]*>Confirmer le prêt du livre', ready)
    assert "disabled>Confirmer" not in ready.replace("\n", "")
    done = browser.post("/comptoir/valider", data=_form(_hidden(ready))).text
    assert "Prêt enregistré" in done and _rows(settings, "SELECT COUNT(*) FROM loans") == [(1,)]


# --- U18 : un même statut, une même couleur ------------------------------------------------------------------------
def test_loan_statuses_use_the_same_colours_everywhere(client_as, settings, database, desk):
    _work, copy_id = _book(settings, database, "Livre rendu")
    service.lend(database, LendRequest(desk["reader"], (LendItem(copy_id),), rules.MODE_SELF), Actor(None, RIGHTS))
    service.return_copy(database, copy_id, Actor(None, RIGHTS))
    counter = client_as("volunteer").get(f"/comptoir?lecteur={desk['reader']}").text
    librarian = client_as("librarian")
    loans = librarian.get("/prets").text
    history = librarian.get("/prets?vue=historique").text
    work = _rows(settings, "SELECT e.work_id FROM loans l JOIN copies c ON c.id = l.copy_id JOIN editions e ON "
                 "e.id = c.edition_id WHERE l.id = ?", desk["first"])[0][0]  # fmt: skip
    tab = librarian.get(f"/catalogue/notices/{work}?onglet=prets").text
    sheet = librarian.get(f"/prets/{desk['first']}").text
    for html in (counter, loans, tab, sheet):
        assert re.search(r'class="badge m-info"><i aria-hidden="true"></i>En cours', html)
        assert "badge m-loan" not in html  # plus aucune pastille vert-bleu
    assert re.search(r'class="badge m-neutral"><i aria-hidden="true"></i>Rendu', history)
    assert not re.search(r'class="badge m-ok"><i aria-hidden="true"></i>(Rendu|En cours)', loans + history + counter)


# --- U19 : cinq lecteurs d'abord sur téléphone --------------------------------------------------------------------
def test_more_readers_are_offered_on_demand_and_search_still_covers_everyone(client_as, database):
    for index in range(8):
        _person(database, f"Lecteur{index}", "Téléphone")
    browser = client_as("volunteer")
    page = browser.get("/comptoir").text
    assert '<ul class="desk-pick-list" aria-label="Lecteurs">' in page and "Voir 3 autres lecteurs" in page
    everyone = browser.post("/comptoir/lecteurs", data={"lecteurs_tous": "1"}).text
    assert '<ul class="desk-pick-list all" aria-label="Lecteurs">' in everyone and "autres lecteurs" not in everyone
    found = browser.post("/comptoir/lecteurs", data={"lecteur_q": "Lecteur7"}).text
    assert found.count('name="choisir_lecteur"') == 1  # le huitième, caché d'abord, se trouve par la recherche


def test_only_a_renewal_is_copied_to_the_announcement_region(client_as):
    """Les autres messages portent déjà role="alert" ou role="status" : les recopier les ferait lire deux fois."""
    html = client_as("volunteer").post("/comptoir/scan", data={"code": "INCONNU-1"}).text
    assert "Code inconnu" in html and "data-annonce" not in html
