# SPDX-License-Identifier: EUPL-1.2
"""UX-Circulation 2.0, UX4 : Prêter, Retour et Renouveler au bon endroit, selon l'état et les droits."""

from __future__ import annotations

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


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _execute(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def lent(settings, database):
    work = make_notice(settings, "Le Grand Voyage")
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    (copy_id,) = catalogue.add_copies(database, edition, 1, "", None)
    _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    person = PersonInput(first_name="Irène", last_name="Valade", category="adult", email="iv@example.org")
    patron = patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)
    request = LendRequest(patron, (LendItem(copy_id),), rules.MODE_SELF)
    loan = service.lend(database, request, Actor(None, RIGHTS)).loan_ids[0]
    code = _rows(settings, "SELECT barcode FROM copies WHERE id = ?", copy_id)[0][0]
    return {"work": work, "copy": copy_id, "patron": patron, "loan": loan, "code": code}


def _returned(settings, loan):
    return _rows(settings, "SELECT returned_at IS NOT NULL FROM loans WHERE id = ?", loan)[0][0] == 1


# --- Fiche lecteur ----------------------------------------------------------------------------------------------------
def test_the_reader_sheet_offers_a_new_loan(client_as, lent):
    html = client_as("volunteer").get(f"/lecteurs/{lent['patron']}").text
    assert f'href="/comptoir?lecteur={lent["patron"]}"' in html and "Nouveau prêt" in html
    assert "Prochaine échéance" in html and "Retours et renouvellements au comptoir" in html
    assert "Le Grand Voyage" not in html  # le bénévole n'a que le nombre et la date (P2, D11 bis)
    assert "Nouveau prêt" not in client_as("viewer").get(f"/lecteurs/{lent['patron']}").text


def test_return_from_the_reader_sheet_with_history(client_as, settings, lent):
    browser = client_as("librarian")
    html = browser.get(f"/lecteurs/{lent['patron']}").text
    assert f'action="/prets/{lent["loan"]}/retour"' in html and "Confirmer le retour" in html
    response = browser.post(
        f"/prets/{lent['loan']}/retour", data={"retour": f"/lecteurs/{lent['patron']}"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == f"/lecteurs/{lent['patron']}"
    assert _returned(settings, lent["loan"])


def test_a_return_already_recorded_is_said_and_changes_nothing(client_as, settings, database, lent):
    service.return_copy(database, lent["copy"], Actor(None, RIGHTS))
    events = _rows(settings, "SELECT COUNT(*) FROM loan_events")
    response = client_as("librarian").post(f"/prets/{lent['loan']}/retour", data={})
    assert response.status_code == 400 and "rien à rendre" in response.text
    assert _rows(settings, "SELECT COUNT(*) FROM loan_events") == events


@pytest.mark.parametrize("target", ["https://ailleurs.example/", "//ailleurs.example/", "/admin/utilisateurs"])
def test_the_return_never_redirects_elsewhere(client_as, lent, target):
    response = client_as("librarian").post(
        f"/prets/{lent['loan']}/retour", data={"retour": target}, follow_redirects=False
    )
    assert response.headers["location"] == f"/prets/{lent['loan']}"


def test_the_volunteer_cannot_use_the_history_actions(client_as, settings, lent):
    browser = client_as("volunteer")
    assert browser.post(f"/prets/{lent['loan']}/retour").status_code == 403
    assert browser.post(f"/prets/{lent['loan']}/renouveler").status_code == 403
    assert not _returned(settings, lent["loan"])


# --- Liste des prêts ------------------------------------------------------------------------------------------------
def test_the_loans_list_has_actions_and_covers(client_as, lent):
    html = client_as("librarian").get("/prets").text
    for marker in (
        f'action="/prets/{lent["loan"]}/retour"',
        f'action="/prets/{lent["loan"]}/renouveler"',
        f'href="/prets/{lent["loan"]}"',
        f'href="/catalogue/notices/{lent["work"]}"',
        'class="mini"',
    ):
        assert marker in html, marker


def test_renew_from_the_list(client_as, settings, lent):
    before = _rows(settings, "SELECT due_on FROM loans WHERE id = ?", lent["loan"])[0][0]
    response = client_as("librarian").post(
        f"/prets/{lent['loan']}/renouveler", data={"retour": "/prets?vue=cours"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/prets?vue=cours"
    assert _rows(settings, "SELECT renewals FROM loans WHERE id = ?", lent["loan"]) == [(1,)]
    assert _rows(settings, "SELECT due_on FROM loans WHERE id = ?", lent["loan"])[0][0] >= before


@pytest.mark.parametrize("typed", ["grand voyage", "Valade", "irene", "{code}"])
def test_search_the_loans_by_title_reader_or_code(client_as, lent, typed):
    response = client_as("librarian").post("/prets/recherche", data={"q": typed.format(code=lent["code"])})
    assert "Le Grand Voyage" in response.text and "Valade" not in str(response.url)


def test_a_search_without_result_says_so(client_as, lent):
    html = client_as("librarian").post("/prets/recherche", data={"q": "Aucun titre pareil"}).text
    assert "0 résultat" in html and "Le Grand Voyage" not in html


# --- Retour rapide sans code ----------------------------------------------------------------------------------------
def test_quick_return_finds_a_book_by_its_title_without_naming_the_holder(client_as, settings, lent):
    browser = client_as("volunteer")
    html = browser.post("/comptoir/retours/recherche", data={"q_retour": "grand voyage"}).text
    assert "Le Grand Voyage" in html and "Valade" not in html and "Irène" not in html
    done = browser.post(f"/comptoir/retours/pret/{lent['loan']}", headers={"HX-Request": "true"}).text
    assert "Le Grand Voyage" in done and "Valade" not in done and _returned(settings, lent["loan"])


def test_quick_return_by_reader_name_needs_the_history_right(client_as, lent):
    volunteer = client_as("volunteer").post("/comptoir/retours/recherche", data={"q_retour": "Valade"}).text
    assert "Le Grand Voyage" not in volunteer and "Aucun livre sorti" in volunteer
    librarian = client_as("librarian").post("/comptoir/retours/recherche", data={"q_retour": "Valade"}).text
    assert "Le Grand Voyage" in librarian and "Irène Valade" in librarian


def test_quick_return_never_closes_a_newer_loan(client_as, settings, database, lent):
    service.return_copy(database, lent["copy"], Actor(None, RIGHTS))
    fresh = service.lend(
        database, LendRequest(lent["patron"], (LendItem(lent["copy"]),), rules.MODE_SELF), Actor(None, RIGHTS)
    ).loan_ids[0]
    html = client_as("volunteer").post(f"/comptoir/retours/pret/{lent['loan']}").text
    assert "La situation vient de changer" in html and not _returned(settings, fresh)
