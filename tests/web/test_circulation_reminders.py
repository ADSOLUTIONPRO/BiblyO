# SPDX-License-Identifier: EUPL-1.2
"""« À rappeler » et rappels notés (D15) : qui joindre, coordonnées à la demande, valeurs structurées, aucun envoi."""

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
PARENT_EMAIL = "parent.contact@example.org"
OTHER_EMAIL = "parent.discret@example.org"


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


def _copies(settings, database, count, title):
    work = make_notice(settings, title)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None)
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return ids


def _person(database, first, last, category="adult", email=""):
    person = PersonInput(first_name=first, last_name=last, category=category, email=email)
    return patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)


@pytest.fixture
def due(settings, database):
    """Un enfant en retard (deux responsables, un seul joignable), un adulte proche de l'échéance, un prêt lointain."""
    late, soon, far = (_copies(settings, database, 1, title)[0] for title in ("Retard", "Bientôt", "Lointain"))
    child = _person(database, "Léa", "Durand", "minor")
    contact = _person(database, "Anne", "Durand", email=PARENT_EMAIL)
    silent = _person(database, "Paul", "Durand", email=OTHER_EMAIL)
    for guardian, is_contact in ((contact, 1), (silent, 0)):
        _execute(
            settings,
            "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank,"
            " version, created_at, updated_at) VALUES (?, ?, 'parent', 1, ?, 1, 1, 'x', 'x')",
            child,
            guardian,
            is_contact,
        )
    adult = _person(database, "Marc", "Roy", email="marc@example.org")
    actor = Actor(None, RIGHTS)
    child_loan = service.lend(
        database, LendRequest(child, (LendItem(late),), rules.MODE_WITH_ADULT, contact), actor
    ).loan_ids[0]
    adult_loan = service.lend(database, LendRequest(adult, (LendItem(soon),), rules.MODE_SELF), actor).loan_ids[0]
    far_loan = service.lend(database, LendRequest(adult, (LendItem(far),), rules.MODE_SELF), actor).loan_ids[0]
    _execute(settings, "UPDATE loans SET due_on = '2020-01-10' WHERE id = ?", child_loan)
    _execute(settings, "UPDATE loans SET due_on = ? WHERE id = ?", service.today().isoformat(), adult_loan)
    return {"child": child_loan, "adult": adult_loan, "far": far_loan, "child_id": child}


@pytest.mark.parametrize(("role", "status"), [("admin", 200), ("librarian", 200), ("volunteer", 403), ("viewer", 403)])
def test_the_list_requires_the_history_right(client_as, due, role, status):
    assert client_as(role).get("/prets/a-rappeler").status_code == status


def test_only_late_or_soon_loans_are_listed(client_as, due):
    html = client_as("librarian").get("/prets/a-rappeler").text
    assert f"/prets/{due['child']}" in html and f"/prets/{due['adult']}" in html
    assert f"/prets/{due['far']}" not in html


def test_a_minor_is_reached_through_contactable_guardians_only(client_as, due):
    html = client_as("librarian").get("/prets/a-rappeler").text
    assert "Anne Durand" in html and "Paul Durand" not in html
    assert PARENT_EMAIL not in html  # aucune coordonnée sans le demander


def test_the_page_says_nothing_is_sent_and_offers_no_free_text(client_as, due):
    html = client_as("librarian").get("/prets/a-rappeler").text
    assert "n’envoie aucun message" in html
    forms = re.findall(r'<form class="remind-form".*?</form>', html, re.S)
    assert forms and all("<textarea" not in form for form in forms)
    for form in forms:
        types = set(re.findall(r'<input[^>]*type="([a-z]+)"', form))
        assert types <= {"hidden", "date"}
    assert not re.search(r">\s*Envoyer", html)


def test_noting_a_reminder_keeps_structured_values_only(settings, client_as, due):
    browser = client_as("librarian")
    data = {
        "date": service.today().isoformat(),
        "canal": "phone",
        "resultat": "message_left",
        "retour": "liste",
        "note": "Rappeler demain, numéro 0612345678",
    }
    response = browser.post(f"/prets/{due['child']}/rappel", data=data)
    assert response.status_code == 303 and response.headers["location"] == "/prets/a-rappeler"
    assert _rows(settings, "SELECT channel, result, user_id IS NOT NULL FROM loan_reminders") == [
        ("phone", "message_left", 1)
    ]
    dump = "\n".join(sqlite3.connect(settings.database_path).iterdump())
    assert "0612345678" not in dump and "Rappeler demain" not in dump
    html = browser.get("/prets/a-rappeler").text
    assert "Dernier rappel" in html and "Message laissé" in html


@pytest.mark.parametrize(
    ("canal", "resultat", "date"),
    [("sms", "reached", ""), ("phone", "merci", ""), ("phone", "reached", "2099-01-01"), ("", "", "")],
)
def test_a_value_outside_the_lists_is_refused(settings, client_as, due, canal, resultat, date):
    response = client_as("librarian").post(
        f"/prets/{due['child']}/rappel", data={"canal": canal, "resultat": resultat, "date": date}
    )
    assert response.status_code == 400 and _rows(settings, "SELECT COUNT(*) FROM loan_reminders") == [(0,)]


def test_a_volunteer_cannot_note_a_reminder(client_as, due):
    data = {"canal": "phone", "resultat": "reached"}
    assert client_as("volunteer").post(f"/prets/{due['child']}/rappel", data=data).status_code == 403


def test_coordinates_are_shown_on_request_and_logged(settings, client_as, due):
    browser = client_as("librarian")
    response = browser.post(f"/prets/{due['child']}/coordonnees")
    assert response.status_code == 200 and PARENT_EMAIL in response.text and OTHER_EMAIL not in response.text
    logged = _rows(settings, "SELECT entity_id FROM audit_log WHERE action = 'patron.contacts_shown'")
    assert logged == [(due["child_id"],)]


def test_coordinates_require_the_personal_right(settings, client_as, due):
    _execute(
        settings, "INSERT INTO roles (id, code, label, is_system, created_at) VALUES (9, 'suivi', 'Suivi', 0, 'x')"
    )
    for permission in ("circulation.history", "patrons.view"):
        _execute(settings, "INSERT INTO role_permissions (role_id, permission) VALUES (9, ?)", permission)
    browser = client_as("suivi")
    assert browser.get("/prets/a-rappeler").status_code == 200
    assert "Afficher les coordonnées" not in browser.get("/prets/a-rappeler").text
    assert browser.post(f"/prets/{due['child']}/coordonnees").status_code == 403
    assert client_as("volunteer").post(f"/prets/{due['child']}/coordonnees").status_code == 403


def test_the_loan_sheet_lists_the_reminders(client_as, due):
    browser = client_as("librarian")
    browser.post(f"/prets/{due['adult']}/rappel", data={"canal": "in_person", "resultat": "return_promised"})
    html = browser.get(f"/prets/{due['adult']}").text
    assert "Rappels notés" in html and "En personne" in html and "Retour promis" in html


def test_a_reminder_blocks_the_cancellation_of_the_loan(settings, client_as, due):
    browser = client_as("librarian")
    browser.post(f"/prets/{due['adult']}/rappel", data={"canal": "phone", "resultat": "reached"})
    response = browser.post(f"/prets/{due['adult']}/annuler", data={"motif": "scan_error"})
    assert response.status_code == 400
    assert _rows(settings, "SELECT cancelled_at FROM loans WHERE id = ?", due["adult"]) == [(None,)]
