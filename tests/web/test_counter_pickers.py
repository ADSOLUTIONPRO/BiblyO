# SPDX-License-Identifier: EUPL-1.2
"""UX-Circulation 2.0, UX2 et UX3 : choisir un lecteur et des livres dans des listes, sans jamais taper de code."""

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


def _book(settings, database, title, count=1, **options):
    work = make_notice(settings, title, **options)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None)
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return work, ids


def _person(database, first, last, category="adult", card="", email=""):
    email = email or (f"{first.lower()}.{last.lower()}@example.org" if category == "adult" else "")
    person = PersonInput(first_name=first, last_name=last, category=category, email=email, card_number=card)
    return patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _hidden(html):
    return re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html)


def _state(html):
    return [pair for pair in _hidden(html) if pair[0] not in ("_csrf", "code")]


def _readers(html):
    return re.findall(r'name="choisir_lecteur" value="(\d+)"', html)


def _token(html):
    return re.search(r'name="jeton" value="([^"]+)"', html).group(1)


def _form(pairs):
    found: dict[str, list[str]] = {}
    for name, value in pairs:
        found.setdefault(name, []).append(value)
    return found


# --- Lecteurs : la liste d'emblée, la recherche en direct ---------------------------------------------------------
def test_the_counter_lists_readers_before_anything_is_typed(database, client_as):
    ids = [_person(database, first, "Lecteur") for first in ("Zoé", "Alain", "Mia")]
    html = client_as("volunteer").get("/comptoir").text
    assert "Choisir un lecteur" in html and sorted(_readers(html)) == sorted(str(item) for item in ids)
    assert 'name="lecteur" value=' not in html  # rien n'est choisi d'office


def test_the_list_is_paginated_on_the_server(database, client_as):
    for index in range(23):
        _person(database, f"Prénom{index:02d}", "Paginé")
    browser = client_as("volunteer")
    first = browser.get("/comptoir").text
    assert len(_readers(first)) == 20 and "Lecteurs suivants" in first and "Page 1 / 2" in first
    second = browser.post("/comptoir/lecteurs", data={"lecteur_page": "2"}).text
    assert len(_readers(second)) == 3 and not set(_readers(second)) & set(_readers(first))


@pytest.mark.parametrize("typed", ["helene", "HÉLÈNE", "Hélène Faure", "faure", "P-000001", "CARTE-HF"])
def test_search_tolerates_accents_and_case_and_finds_numbers_and_cards(database, client_as, typed):
    target = _person(database, "Hélène", "Faure", card="CARTE-HF")
    _person(database, "Bruno", "Autre")
    html = client_as("volunteer").post("/comptoir/lecteurs", data={"lecteur_q": typed}).text
    assert _readers(html) == [str(target)]


def test_the_search_is_posted_and_never_enters_an_address(client_as):
    browser = client_as("volunteer")
    assert browser.get("/comptoir/lecteurs?lecteur_q=Faure").status_code == 405
    response = browser.post("/comptoir/lecteurs", data={"lecteur_q": "Faure"})
    assert "Faure" not in str(response.url)


def test_homonyms_are_both_listed_marked_and_never_chosen(database, client_as):
    first = _person(database, "Paul", "Martin", email="p1@example.org")
    second = _person(database, "Paul", "Martin", email="p2@example.org")
    html = client_as("volunteer").post("/comptoir/lecteurs", data={"lecteur_q": "Paul Martin"}).text
    assert set(_readers(html)) == {str(first), str(second)} and html.count("Homonyme") == 2
    assert 'name="lecteur" value=' not in html


def test_a_single_result_is_not_chosen_without_a_click(database, client_as):
    _person(database, "Unique", "Seul")
    html = client_as("volunteer").post("/comptoir/lecteurs", data={"lecteur_q": "Unique"}).text
    assert len(_readers(html)) == 1 and 'name="lecteur" value=' not in html


def test_no_result_and_no_reader_are_said_plainly(database, client_as):
    browser = client_as("volunteer")
    assert "Aucun lecteur n’est encore inscrit" in browser.get("/comptoir").text
    _person(database, "Ana", "Présente")
    html = browser.post("/comptoir/lecteurs", data={"lecteur_q": "Introuvable"}).text
    assert "Aucun lecteur ne correspond à cette recherche" in html and "Introuvable" not in html


@pytest.mark.parametrize("status", ["archived", "anonymized"])
def test_withdrawn_readers_are_neither_listed_nor_choosable(settings, database, client_as, status):
    patron = _person(database, "Retiré", "Ailleurs")
    _execute(settings, "UPDATE patrons SET status = ? WHERE id = ?", status, patron)
    browser = client_as("volunteer")
    assert str(patron) not in _readers(browser.get("/comptoir").text)
    html = browser.post("/comptoir/etat", data={"choisir_lecteur": str(patron)}).text
    assert ("lecteur", str(patron)) not in _state(html) and "Code inconnu" in html


def test_states_of_a_reader_are_shown_in_the_list(settings, database, client_as):
    child = _person(database, "Petit", "Seul", category="minor")
    adult = _person(database, "Échu", "Adhérent")
    _execute(settings, "UPDATE patrons SET membership_expires_on = '2020-01-01' WHERE id = ?", adult)
    html = client_as("volunteer").get("/comptoir").text
    child_row = html[html.index(f'name="choisir_lecteur" value="{child}"') :][:1500]
    adult_row = html[html.index(f'name="choisir_lecteur" value="{adult}"') :][:1500]
    assert "Mineur" in child_row and "Responsable à renseigner" in child_row
    assert "Adhésion échue" in adult_row and "0 prêt" in adult_row


def test_the_list_shows_no_contact_details(database, client_as):
    _person(database, "Claire", "Contact", email="claire.secret@example.org")
    html = client_as("volunteer").get("/comptoir").text
    assert "claire.secret@example.org" not in html


def test_choosing_a_reader_keeps_the_basket(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    reader = _person(database, "Claire", "Roux")
    html = client_as("volunteer").post("/comptoir/etat", data={"ex": str(copy_id), "choisir_lecteur": str(reader)}).text
    assert ("lecteur", str(reader)) in _state(html) and ("ex", str(copy_id)) in _state(html)
    assert "Changer de lecteur" in html and "Choisir un lecteur" not in html


# --- Livres : le catalogue au comptoir ------------------------------------------------------------------------------
def test_an_empty_basket_shows_the_catalogue_at_once(settings, database, client_as):
    _book(settings, database, "Le Secret")
    html = client_as("volunteer").get("/comptoir").text
    assert 'id="cp-livres"' in html and "Le Secret" in html and "1 disponible" in html


@pytest.mark.parametrize("typed", ["Secret", "secret", "Durand", "Les Îles"])
def test_books_are_found_by_title_author_or_series(settings, database, client_as, typed):
    _book(settings, database, "Le Secret", authors=("Annie Durand",), series="Les Îles", volume="3")
    _book(settings, database, "Autre chose")
    html = client_as("volunteer").post("/comptoir/livres", data={"livre_q": typed}).text
    assert "Le Secret" in html and "Autre chose" not in html


def test_books_are_found_by_barcode_too(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    code = _rows(settings, "SELECT barcode FROM copies WHERE id = ?", copy_id)[0][0]
    html = client_as("volunteer").post("/comptoir/livres", data={"livre_q": code}).text
    assert "Le Secret" in html


def test_a_single_loanable_copy_is_added_in_one_click(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    browser = client_as("volunteer")
    page = browser.get("/comptoir").text
    assert f'name="ajouter" value="{copy_id}"' in page
    html = browser.post("/comptoir/etat", data=_form([*_state(page), ("ajouter", str(copy_id))])).text
    assert ("ex", str(copy_id)) in _state(html) and _rows(settings, "SELECT COUNT(*) FROM loans") == [(0,)]
    assert ("parcourir", "1") in _state(html)  # le catalogue reste ouvert pour un second livre


def test_several_copies_are_chosen_one_by_one_with_their_place(settings, database, client_as):
    work, ids = _book(settings, database, "Le Secret", count=3)
    browser = client_as("volunteer")
    page = browser.get("/comptoir").text
    assert "Choisir l’exemplaire" in page and 'name="ajouter"' not in page.split("Le Secret")[1][:2000]
    copies = browser.post(f"/comptoir/livres/{work}/exemplaires", data={"ex": str(ids[0])}).text
    assert copies.count('name="ajouter"') == 2 and "Dans la sélection" in copies and "Sans place attribuée" in copies


def test_a_copy_that_cannot_leave_says_why_and_cannot_be_added(settings, database, client_as):
    work, (copy_id,) = _book(settings, database, "En réparation")
    _execute(settings, "UPDATE copies SET status = 'in_repair' WHERE id = ?", copy_id)
    browser = client_as("volunteer")
    copies = browser.post(f"/comptoir/livres/{work}/exemplaires").text
    assert "réparation" in copies and 'name="ajouter"' not in copies
    html = browser.post("/comptoir/etat", data={"ajouter": str(copy_id)}).text  # identifiant forgé
    assert ("ex", str(copy_id)) not in _state(html) and "réparation" in html


def test_the_same_copy_is_never_added_twice(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    html = client_as("volunteer").post("/comptoir/etat", data={"ex": str(copy_id), "ajouter": str(copy_id)}).text
    assert _state(html).count(("ex", str(copy_id))) == 1 and "déjà dans la liste" in html


def test_the_basket_shows_cover_author_tome_and_availability(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret", authors=("Annie Durand",), series="Îles", volume="7")
    html = client_as("volunteer").post("/comptoir/etat", data={"ex": str(copy_id)}).text
    zone = html[html.index('id="cp-liste"') :]
    assert "Annie Durand" in zone and "tome 7" in zone and "Disponible, vérifié" in zone and 'class="mini"' in zone


# --- Le parcours entier, sans code ----------------------------------------------------------------------------------
def test_from_a_book_to_a_confirmed_loan_without_any_code(settings, database, client_as):
    """AC1 : livre → Prêter → choisir le lecteur dans la liste → vérifier → confirmer."""
    work, (copy_id,) = _book(settings, database, "Le Secret")
    reader = _person(database, "Claire", "Roux")
    browser = client_as("volunteer")
    lend = browser.get(f"/comptoir/livre/{work}/preter", follow_redirects=False).headers["location"]
    html = browser.get(lend).text
    assert "Le Secret" in html and str(reader) in _readers(html)
    html = browser.post("/comptoir/etat", data=_form([*_state(html), ("choisir_lecteur", str(reader))])).text
    assert "Confirmer le prêt du livre" in html and "Claire Roux" in html
    done = browser.post("/comptoir/valider", data=_form(_state(html))).text
    assert "Prêt enregistré" in done and "Claire Roux" in done and "Le Secret" in done and "retour le" in done
    assert "Nouveau prêt" in done and f'/catalogue/notices/{work}"' in done and "Revenir au catalogue" in done
    assert _rows(settings, "SELECT patron_id, copy_id FROM loans") == [(reader, copy_id)]


def test_from_a_reader_to_a_confirmed_loan_without_any_code(settings, database, client_as):
    """AC2 : lecteur → Nouveau prêt → livres choisis au catalogue du comptoir → confirmer « 2 livres »."""
    _first, (one,) = _book(settings, database, "Premier")
    _second, (two,) = _book(settings, database, "Second")
    reader = _person(database, "Paul", "Lecteur")
    browser = client_as("volunteer")
    html = browser.get(f"/comptoir?lecteur={reader}").text
    assert ("lecteur", str(reader)) in _state(html) and 'id="cp-livres"' in html
    html = browser.post("/comptoir/etat", data=_form([*_state(html), ("ajouter", str(one))])).text
    html = browser.post("/comptoir/etat", data=_form([*_state(html), ("ajouter", str(two))])).text
    assert "Confirmer le prêt de 2 livres" in html
    done = browser.post("/comptoir/valider", data=_form(_state(html))).text
    assert "Prêt enregistré" in done and "Premier" in done and "Second" in done
    assert _rows(settings, "SELECT COUNT(*) FROM loans WHERE patron_id = ?", reader) == [(2,)]


def test_a_withdrawn_reader_in_the_address_selects_nobody(settings, database, client_as):
    patron = _person(database, "Archivé", "Adresse")
    _execute(settings, "UPDATE patrons SET status = 'archived' WHERE id = ?", patron)
    html = client_as("volunteer").get(f"/comptoir?lecteur={patron}").text
    assert ("lecteur", str(patron)) not in _state(html)


def test_a_basket_that_became_stale_is_refused_at_confirmation(settings, database, client_as):
    """Le contrôle du sélecteur est refait à la validation : le livre parti entre-temps n'est pas prêté deux fois."""
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    reader, other = _person(database, "Claire", "Roux"), _person(database, "Rapide", "Autre")
    browser = client_as("volunteer")
    html = browser.post("/comptoir/etat", data={"ex": str(copy_id), "choisir_lecteur": str(reader)}).text
    service.lend(database, LendRequest(other, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, RIGHTS))
    done = browser.post("/comptoir/valider", data=_form(_state(html))).text
    assert "Prêt enregistré" not in done and "déjà en prêt" in done
    assert _rows(settings, "SELECT patron_id FROM loans") == [(other,)]


def test_a_double_click_records_once(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    reader = _person(database, "Claire", "Roux")
    browser = client_as("volunteer")
    html = browser.post("/comptoir/etat", data={"ex": str(copy_id), "choisir_lecteur": str(reader)}).text
    data = _form(_state(html))
    browser.post("/comptoir/valider", data=data)
    again = browser.post("/comptoir/valider", data=data).text
    assert "déjà enregistré" in again and _rows(settings, "SELECT COUNT(*) FROM loans") == [(1,)]


def test_new_loan_forgets_the_preparation(client_as):
    html = client_as("volunteer").get("/comptoir?nouveau=1").text
    assert "data-nouveau" in html


# --- Rendre depuis le comptoir (P2) ---------------------------------------------------------------------------------
def test_return_from_the_counter_asks_then_records(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    reader = _person(database, "Claire", "Roux")
    loan = service.lend(database, LendRequest(reader, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, RIGHTS))
    loan_id = loan.loan_ids[0]
    browser = client_as("volunteer")
    html = browser.get(f"/comptoir?lecteur={reader}").text
    assert f'name="rendre" value="{loan_id}"' in html
    asked = browser.post("/comptoir/etat", data=_form([*_state(html), ("rendre", str(loan_id))])).text
    assert "Confirmer le retour" in asked and _rows(settings, "SELECT returned_at FROM loans") == [(None,)]
    done = browser.post(f"/comptoir/rendre/{loan_id}", data=_form(_state(asked))).text
    assert "Retour enregistré : « Le Secret »" in done and "sans place attribuée" in done
    assert _rows(settings, "SELECT returned_at IS NOT NULL FROM loans") == [(1,)]


def test_return_from_the_counter_only_for_the_chosen_reader(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    reader, other = _person(database, "Claire", "Roux"), _person(database, "Autre", "Lecteur")
    loan = service.lend(database, LendRequest(reader, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, RIGHTS))
    response = client_as("volunteer").post(f"/comptoir/rendre/{loan.loan_ids[0]}", data={"lecteur": str(other)})
    assert response.status_code == 403
    assert _rows(settings, "SELECT returned_at FROM loans") == [(None,)]


def test_a_scan_never_returns_a_book(settings, database, client_as):
    _work, (copy_id,) = _book(settings, database, "Le Secret")
    reader = _person(database, "Claire", "Roux")
    loan = service.lend(database, LendRequest(reader, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, RIGHTS))
    data = {"lecteur": str(reader), "code": "QUELQUE-CHOSE"}
    html = client_as("volunteer").post(f"/comptoir/rendre/{loan.loan_ids[0]}", data=data).text
    assert "Un scan était en cours" in html and _rows(settings, "SELECT returned_at FROM loans") == [(None,)]


def test_the_viewer_has_no_counter(client_as):
    for path in ("/comptoir/lecteurs", "/comptoir/livres"):
        assert client_as("viewer").post(path, data={}).status_code == 403
