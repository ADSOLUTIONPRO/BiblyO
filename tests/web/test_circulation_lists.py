# SPDX-License-Identifier: EUPL-1.2
"""Listes des prêts, fiche lecteur, disponibilité, dos « sorti », tableau de bord : rôle par rôle, dans le HTML brut."""

from __future__ import annotations

import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.circulation import rules, service
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import LocationInput
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice

RIGHTS = frozenset({rules.LEND, rules.RENEW})
NAME = "Anne Durand"


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


def _copies(settings, database, count=1, title="Le Lotus bleu"):
    work = make_notice(settings, title)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None)
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return work, ids


def _adult(database, first="Anne", last="Durand"):
    person = PersonInput(first_name=first, last_name=last, category="adult", email=f"{first.lower()}@example.org")
    return patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _lend(database, patron, *copies):
    request = LendRequest(patron, tuple(LendItem(item) for item in copies), rules.MODE_SELF)
    return service.lend(database, request, Actor(None, RIGHTS))


@pytest.fixture
def lent(settings, database):
    """Un lecteur, deux exemplaires en prêt dont un en retard, un prêt terminé, et un exemplaire sur une étagère."""
    work, (first, second, third) = _copies(settings, database, 3)
    patron = _adult(database)
    room = holdings.create_location(database, "room", None, LocationInput("Salle"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble"), None)
    shelf = holdings.create_location(database, "shelf", furniture, LocationInput("Étagère 1"), None)
    holdings.shelve(database, [first], shelf, user_id=None)
    loans = _lend(database, patron, first, second).loan_ids
    _execute(settings, "UPDATE loans SET due_on = '2020-01-10' WHERE id = ?", loans[1])
    done = _lend(database, patron, third).loan_ids[0]
    service.return_copy(database, third, Actor(None, RIGHTS))
    return {
        "work": work,
        "copies": (first, second, third),
        "patron": patron,
        "loans": loans,
        "done": done,
        "shelf": shelf,
        "furniture": furniture,
    }


# --- Disponibilité réelle, sans détenteur (D11 bis, R27) ------------------------------------------------------------
@pytest.mark.parametrize("role", ["volunteer", "librarian", "admin"])
def test_copy_sheet_says_on_loan_with_its_due_date(client_as, lent, role):
    html = client_as(role).get(f"/exemplaires/{lent['copies'][0]}").text
    assert "Emprunté · retour le" in html and NAME not in html


def test_only_history_links_to_the_loan_from_the_copy(client_as, lent):
    path = f"/exemplaires/{lent['copies'][0]}"
    assert "/prets/" not in client_as("volunteer").get(path).text
    assert f"/prets/{lent['loans'][0]}" in client_as("librarian").get(path).text


def test_notice_shows_copies_on_loan_without_holder(client_as, lent):
    html = client_as("volunteer").get(f"/catalogue/notices/{lent['work']}?onglet=exemplaires").text
    # UX-Circulation 2.0 : l'exemplaire en retard se distingue de celui qui court encore
    assert html.count("Emprunté · retour le") == 1 and html.count("En retard · ") == 1 and NAME not in html
    assert "1 emprunté" in html and "1 en retard" in html  # synthèse de l'en-tête, sans double compte


def test_a_spine_on_loan_keeps_its_place_and_says_so(client_as, lent):
    for place in (lent["shelf"], lent["furniture"]):
        for view in ("", "?vue=liste"):
            html = client_as("volunteer").get(f"/emplacements/{place}{view}").text
            assert "sorti, retour le" in html.lower() and NAME not in html
    assert "st-out" in client_as("volunteer").get(f"/emplacements/{lent['furniture']}").text


# --- Listes (CIR-08) --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("role", "status"), [("admin", 200), ("librarian", 200), ("volunteer", 403), ("viewer", 403)])
def test_the_lists_require_the_history_right(client_as, lent, role, status):
    assert client_as(role).get("/prets").status_code == status


@pytest.mark.parametrize(("view", "count"), [("cours", 2), ("retards", 1), ("historique", 1)])
def test_each_view_lists_its_loans(client_as, lent, view, count):
    html = client_as("librarian").get(f"/prets?vue={view}").text
    assert html.count('href="/prets/') - html.count('href="/prets/regles') >= count
    assert f"{count} prêt" in html and NAME in html


def test_an_unknown_view_falls_back_to_current_loans(client_as, lent):
    assert client_as("librarian").get("/prets?vue=tout").status_code == 200


def test_a_page_beyond_the_last_comes_back(client_as, lent):
    assert client_as("librarian").get("/prets?page=999").status_code == 200


def test_a_withdrawn_reader_stays_closed_in_the_lists(settings, client_as, lent):
    _execute(settings, "UPDATE patrons SET status = 'anonymized' WHERE id = ?", lent["patron"])
    browser = client_as("librarian")  # sans patrons.anonymize depuis la révision 0007
    html = browser.get("/prets").text
    assert "Fiche fermée" in html and NAME not in html
    assert browser.get(f"/prets?lecteur={lent['patron']}").status_code == 403


def test_the_lists_of_one_reader(client_as, lent, settings, database):
    other = _adult(database, "Marc", "Roy")
    _, (copy_id,) = _copies(settings, database, 1, "Autre titre")
    _lend(database, other, copy_id)
    html = client_as("librarian").get(f"/prets?lecteur={lent['patron']}").text
    assert NAME in html and "Marc Roy" not in html.split("</h1>", 1)[1].split("Tous les lecteurs")[1]


# --- Fiche lecteur (P2) -----------------------------------------------------------------------------------------------
def test_a_volunteer_sees_only_the_counts_on_the_sheet(client_as, lent):
    html = client_as("volunteer").get(f"/lecteurs/{lent['patron']}").text
    card = html[html.index('id="titre-prets"') :].split("</section>")[0]
    assert "2 prêts en cours" in card and "1 en retard" in card
    assert "Le Lotus bleu" not in card and "/prets/" not in card and "À venir" not in card


def test_a_librarian_sees_the_detail_and_the_history_link(client_as, lent):
    html = client_as("librarian").get(f"/lecteurs/{lent['patron']}").text
    card = html[html.index('id="titre-prets"') :].split("</section>")[0]
    assert "Le Lotus bleu" in card and f"/prets/{lent['loans'][0]}" in card
    assert "Derniers prêts terminés" in card and f"/prets?vue=historique&amp;lecteur={lent['patron']}" in card


def test_a_guardian_sheet_says_how_many_loans_it_answers_for(settings, database, client_as):
    _, (copy_id,) = _copies(settings, database)
    parent = _adult(database)
    child = patrons.register(
        database, Registration(person=PersonInput(first_name="Léa", last_name="Durand", category="minor")), None
    )
    _execute(
        settings,
        "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank, version,"
        " created_at, updated_at) VALUES (?, ?, 'parent', 1, 1, 1, 1, 'x', 'x')",
        child,
        parent,
    )
    request = LendRequest(child, (LendItem(copy_id),), rules.MODE_WITH_ADULT, parent)
    service.lend(database, request, Actor(None, RIGHTS))
    assert "Répond de 1 prêt en cours" in client_as("volunteer").get(f"/lecteurs/{parent}").text


# --- Tableau de bord (CIR-08) -----------------------------------------------------------------------------------------
def test_the_dashboard_counts_for_a_volunteer_without_names(client_as, lent):
    html = client_as("volunteer").get("/").text
    assert "Prêts en cours" in html and "En retard" in html and "Retards à suivre" not in html
    assert NAME not in html and "Le Lotus bleu" not in html.split("Prêts en cours")[1][:2000]


def test_the_dashboard_lists_late_loans_with_history(client_as, lent):
    html = client_as("librarian").get("/").text
    assert "Retards à suivre" in html and NAME in html and "/prets?vue=retards" in html


def test_the_dashboard_says_nothing_of_loans_to_a_viewer(client_as, lent):
    assert "Prêts en cours" not in client_as("viewer").get("/").text


# --- Palette ⌘K -------------------------------------------------------------------------------------------------------
def test_the_palette_offers_the_loan_commands_by_role(client_as):
    volunteer = client_as("volunteer").get("/").text
    librarian = client_as("librarian").get("/").text
    viewer = client_as("viewer").get("/").text
    assert 'href="/comptoir/retours"' in volunteer and 'href="/prets?vue=retards"' not in volunteer
    assert 'href="/prets?vue=retards"' in librarian
    assert "Retour rapide" not in viewer


def test_direct_shortcuts_are_offered_only_to_lenders(client_as):
    volunteer = client_as("volunteer").get("/").text
    assert 'data-shortcut-key="p"' in volunteer and 'data-shortcut-key="r"' in volunteer
    assert "data-shortcut-key" not in client_as("viewer").get("/").text


def test_a_copy_on_loan_offers_no_status_nor_place_change(client_as, lent):
    html = client_as("librarian").get(f"/exemplaires/{lent['copies'][0]}").text
    assert "son statut ne change qu’après son retour" in html and "garde sa place jusqu’à son retour" in html
    assert "/statut" not in html and "/sans-place" not in html
    free = client_as("librarian").get(f"/exemplaires/{lent['copies'][2]}").text  # rendu : tout redevient possible
    assert "/statut" in free
