# SPDX-License-Identifier: EUPL-1.2
"""UX-Circulation 2.0, UX1 : la même disponibilité partout, sans détenteur, et les actions au bon endroit."""

from __future__ import annotations

import re
import sqlite3

import pytest
from sqlalchemy import event

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.circulation import rules, service
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest
from biblyo.modules.holdings import availability
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import LocationInput
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice

RIGHTS = frozenset({rules.LEND, rules.RENEW})
HOLDER = ("Hortense", "Valmont")


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


def _book(settings, database, count, title, **options):
    work = make_notice(settings, title, **options)
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    ids = catalogue.add_copies(database, edition, count, "", None) if count else []
    for copy_id in ids:
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", copy_id)
    return work, ids


def _holder(database):
    person = PersonInput(first_name=HOLDER[0], last_name=HOLDER[1], category="adult", email="hv@example.org")
    return patrons.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _lend(database, patron, *copies):
    request = LendRequest(patron, tuple(LendItem(item) for item in copies), rules.MODE_SELF)
    return service.lend(database, request, Actor(None, RIGHTS))


@pytest.fixture
def shelf(settings, database):
    """Quatre livres : libre, sorti à l'heure, sorti en retard, en réparation ; plus un tome sorti dans une série."""
    free, (free_copy,) = _book(settings, database, 1, "Le Phare", series="Les Îles", volume="1")
    out, (out_copy,) = _book(settings, database, 1, "La Marée", series="Les Îles", volume="2")
    late, (late_a, late_b) = _book(settings, database, 2, "Le Ressac")
    repair, (repair_copy,) = _book(settings, database, 1, "L’Écume")
    _execute(settings, "UPDATE copies SET status = 'in_repair' WHERE id = ?", repair_copy)
    room = holdings.create_location(database, "room", None, LocationInput("Salle"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble"), None)
    level = holdings.create_location(database, "shelf", furniture, LocationInput("Niveau 1"), None)
    holdings.shelve(database, [free_copy, out_copy, late_a], level, user_id=None)
    holder = _holder(database)
    loans = _lend(database, holder, out_copy, late_a).loan_ids
    _execute(settings, "UPDATE loans SET due_on = '2020-01-10' WHERE id = ?", loans[1])
    return {
        "free": free,
        "out": out,
        "late": late,
        "repair": repair,
        "free_copy": free_copy,
        "out_copy": out_copy,
        "late_copy": late_a,
        "late_free": late_b,
        "loans": loans,
        "furniture": furniture,
        "series": _rows(settings, "SELECT id FROM series")[0][0],
    }


# --- Le service : une requête pour une page entière -----------------------------------------------------------------
def test_counts_come_from_one_grouped_query(database, shelf):
    statements: list[str] = []

    def _count(_conn, _cursor, statement, *_args):
        statements.append(statement)

    with database.session() as session:
        event.listen(session.get_bind(), "before_cursor_execute", _count)
        try:
            found = availability.for_works(session, [shelf["free"], shelf["out"], shelf["late"], shelf["repair"]])
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", _count)
    assert len(statements) == 1
    assert found[shelf["free"]].segments == [("1 disponible", "ok")]
    assert found[shelf["out"]].segments == [("1 emprunté", "info")]
    assert found[shelf["late"]].segments == [("1 disponible", "ok"), ("1 en retard", "late")]
    assert found[shelf["repair"]].segments == [("1 indisponible", "neutral")]


# --- Catalogue ------------------------------------------------------------------------------------------------------
def test_the_grid_shows_availability_and_a_lend_link_without_hover(client_as, shelf):
    html = client_as("volunteer").get("/catalogue").text
    for label in ("1 disponible", "Emprunté · retour le", "Indisponible", "1 en retard"):
        assert label in html, label
    assert f'href="/comptoir/livre/{shelf["free"]}/preter"' in html
    assert f'href="/comptoir/livre/{shelf["out"]}/preter"' not in html  # rien à prêter : pas de lien
    assert " ".join(HOLDER) not in html and HOLDER[1] not in html


def test_the_list_has_an_availability_column_and_actions(client_as, shelf):
    html = client_as("volunteer").get("/catalogue?view=list").text
    assert ">Disponibilité<" in html and "1 emprunté" in html and "1 en retard" in html
    assert f'href="/comptoir/livre/{shelf["free"]}/preter"' in html
    assert f"/catalogue/notices/{shelf['late']}?onglet=exemplaires" in html


def test_no_lend_link_for_an_account_that_cannot_lend(client_as, shelf):
    html = client_as("viewer").get("/catalogue").text
    assert "1 disponible" in html and "/preter" not in html


# --- Fiche, fiche d'exemplaire, page du livre -----------------------------------------------------------------------
def test_the_notice_says_how_many_copies_are_available_out_and_late(client_as, shelf):
    html = client_as("volunteer").get(f"/catalogue/notices/{shelf['late']}").text
    assert "1 disponible" in html and "1 en retard" in html and "sur 2" in html
    assert "Enregistrer le retour" in html and HOLDER[1] not in html
    lend = re.search(r'<a href="([^"]+)" class="btn btn-p">.*?<span class="lbl">Prêter</span>', html, re.S)
    assert lend and lend.group(1) == f"/comptoir?exemplaire={shelf['late_free']}&amp;livre={shelf['late']}"


def test_the_overview_no_longer_says_in_place_for_a_copy_that_is_out(client_as, shelf):
    html = client_as("volunteer").get(f"/catalogue/notices/{shelf['out']}").text
    where = html[html.index('id="titre-trouver"') :]
    assert "Emprunté · retour le" in where and "En place" not in where[: where.index("</section>")]


def test_copies_tab_offers_lend_or_return_per_copy(client_as, shelf):
    html = client_as("volunteer").get(f"/catalogue/notices/{shelf['late']}?onglet=exemplaires").text
    assert f"/comptoir?exemplaire={shelf['late_free']}&amp;livre={shelf['late']}" in html
    assert f"/comptoir/livre/{shelf['late']}#retour-{shelf['late_copy']}" in html
    assert "En retard · " in html and ">Circulation<" in html


@pytest.mark.parametrize(
    ("key", "button"), [("free_copy", "Prêter cet exemplaire"), ("out_copy", "Enregistrer le retour")]
)
def test_the_copy_sheet_offers_the_right_action(client_as, shelf, key, button):
    html = client_as("volunteer").get(f"/exemplaires/{shelf[key]}").text
    assert button in html and HOLDER[1] not in html
    assert button not in client_as("viewer").get(f"/exemplaires/{shelf[key]}").text


def test_lend_from_a_card_goes_straight_to_the_counter_when_one_copy_can_leave(client_as, shelf):
    browser = client_as("volunteer")
    response = browser.get(f"/comptoir/livre/{shelf['free']}/preter", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/comptoir?exemplaire={shelf['free_copy']}&livre={shelf['free']}"
    response = browser.get(f"/comptoir/livre/{shelf['out']}/preter", follow_redirects=False)
    assert response.headers["location"] == f"/comptoir/livre/{shelf['out']}"


# --- Retour depuis la page du livre : confirmé, ciblé, sans détenteur -----------------------------------------------
def test_return_from_the_book_page_closes_that_loan_and_says_where_to_shelve(client_as, settings, shelf):
    browser = client_as("volunteer")
    page = browser.get(f"/comptoir/livre/{shelf['out']}").text
    assert "Confirmer le retour" in page and f'name="pret" value="{shelf["loans"][0]}"' in page
    response = browser.post(
        f"/comptoir/livre/{shelf['out']}/retour",
        data={"exemplaire_rendu": str(shelf["out_copy"]), "pret": str(shelf["loans"][0])},
    )
    assert response.status_code == 200
    html = response.text
    assert "Retour enregistré" in html and "Salle / Meuble / Niveau 1" in html and "Localiser l’emplacement" in html
    assert HOLDER[1] not in html and "Disponible" in html
    assert _rows(settings, "SELECT returned_at IS NOT NULL FROM loans WHERE id = ?", shelf["loans"][0]) == [(1,)]


def test_a_second_return_records_nothing_more(client_as, settings, shelf):
    browser = client_as("volunteer")
    data = {"exemplaire_rendu": str(shelf["out_copy"]), "pret": str(shelf["loans"][0])}
    browser.post(f"/comptoir/livre/{shelf['out']}/retour", data=data)
    before = _rows(settings, "SELECT COUNT(*) FROM loan_events")
    html = browser.post(f"/comptoir/livre/{shelf['out']}/retour", data=data).text
    assert "rien à rendre" in html and _rows(settings, "SELECT COUNT(*) FROM loan_events") == before


def test_a_stale_return_never_closes_a_newer_loan(client_as, settings, database, shelf):
    """L'écran montrait un prêt ; entre-temps, il a été rendu et l'exemplaire est reparti : le nouveau prêt reste."""
    browser = client_as("volunteer")
    stale = shelf["loans"][0]
    service.return_copy(database, shelf["out_copy"], Actor(None, RIGHTS))
    fresh = _lend(database, _holder(database), shelf["out_copy"]).loan_ids[0]
    response = browser.post(
        f"/comptoir/livre/{shelf['out']}/retour", data={"exemplaire_rendu": str(shelf["out_copy"]), "pret": str(stale)}
    )
    assert response.status_code == 409 and "La situation vient de changer" in response.text
    assert _rows(settings, "SELECT returned_at FROM loans WHERE id = ?", fresh) == [(None,)]


def test_a_copy_of_another_book_is_refused(client_as, shelf):
    response = client_as("volunteer").post(
        f"/comptoir/livre/{shelf['free']}/retour", data={"exemplaire_rendu": str(shelf["out_copy"])}
    )
    assert response.status_code == 400


# --- Séries et étagères ---------------------------------------------------------------------------------------------
def test_a_tome_that_is_out_stays_present_in_its_series(client_as, shelf):
    html = client_as("volunteer").get(f"/catalogue/series/{shelf['series']}").text
    assert "Tome 2 : présent, tous ses exemplaires sont sortis" in html and 'class="tome cv-' in html
    assert "manquant" not in html.split("Tome 2")[1][:80]
    assert "Sorti : toujours dans la collection" in html


def test_an_overdue_spine_is_marked_differently_from_an_out_one(client_as, shelf):
    html = client_as("volunteer").get(f"/emplacements/{shelf['furniture']}").text
    assert "st-out st-late" in html and "sorti, en retard, retour prévu le" in html
    assert "sorti, retour le" in html and "Sorti, en retard" in html  # la légende le dit aussi
    assert HOLDER[1] not in html


# --- La disponibilité suit chaque opération, partout (consigne UX5, point 2) ----------------------------------------
def _everywhere(browser, shelf_fixture, title):
    """Le libellé de disponibilité de « Le Phare » sur chaque vue qui le montre."""
    found = {}
    pages = {
        "grille": "/catalogue",
        "liste": "/catalogue?view=list",
        "recherche": f"/catalogue?q={title}",
        "fiche": f"/catalogue/notices/{shelf_fixture['free']}",
    }
    for name, path in pages.items():
        html = browser.get(path).text
        if name == "fiche":
            found[name] = html
            continue
        start = html.index(f"/catalogue/notices/{shelf_fixture['free']}")
        found[name] = html[start : start + 3000]
    found["etagere"] = browser.get(f"/emplacements/{shelf_fixture['furniture']}").text
    return found


def test_availability_follows_lend_renew_return_and_cancel_on_every_view(client_as, database, settings, shelf):
    browser = client_as("volunteer")
    copy_id = shelf["free_copy"]
    spine = f'id="dos-{copy_id}"'

    def spine_of(html):
        start = html.index(spine)
        return html[start : html.index("</li>", start)]

    views = _everywhere(browser, shelf, "Phare")
    assert all("1 disponible" in views[name] for name in ("grille", "liste", "recherche", "fiche"))
    assert "st-out" not in spine_of(views["etagere"])

    reader = _holder(database)
    lent = _lend(database, reader, copy_id)
    views = _everywhere(browser, shelf, "Phare")
    for name in ("grille", "recherche"):
        assert "Emprunté · retour le" in views[name], name
    assert "1 emprunté" in views["liste"] and "1 emprunté" in views["fiche"]
    assert "st-out" in spine_of(views["etagere"]) and "sorti, retour le" in spine_of(views["etagere"])

    _execute(settings, "UPDATE loans SET due_on = '2020-01-10' WHERE id = ?", lent.loan_ids[0])
    views = _everywhere(browser, shelf, "Phare")
    assert (
        "En retard" in views["grille"] and "1 en retard" in views["liste"] and "st-late" in spine_of(views["etagere"])
    )

    service.renew(database, lent.loan_ids[0], Actor(None, RIGHTS))
    views = _everywhere(browser, shelf, "Phare")
    assert "Emprunté · retour le" in views["grille"] and "st-late" not in spine_of(views["etagere"])

    service.return_copy(database, copy_id, Actor(None, RIGHTS))
    views = _everywhere(browser, shelf, "Phare")
    assert all("1 disponible" in views[name] for name in ("grille", "liste", "recherche", "fiche"))
    assert "st-out" not in spine_of(views["etagere"])

    again = _lend(database, reader, copy_id)
    librarian = Actor(None, RIGHTS | {rules.OVERRIDE})
    service.cancel_lend(database, again.operation_id, librarian, reason_code="scan_error")
    views = _everywhere(browser, shelf, "Phare")
    assert all("1 disponible" in views[name] for name in ("grille", "liste", "recherche", "fiche"))
    assert HOLDER[1] not in " ".join(views.values())


def test_a_lent_copy_keeps_its_place_and_its_call_number(settings, database, shelf):
    """Un prêt ne déplace rien : la place et la cote enregistrées restent les mêmes, avant, pendant et après."""
    query = "SELECT location_id, shelf_position, call_number FROM copies WHERE id = ?"
    before = _rows(settings, query, shelf["free_copy"])
    _lend(database, _holder(database), shelf["free_copy"])
    during = _rows(settings, query, shelf["free_copy"])
    service.return_copy(database, shelf["free_copy"], Actor(None, RIGHTS))
    assert before == during == _rows(settings, query, shelf["free_copy"])


# --- Prêter, Retour, Renouveler selon les droits (consigne UX5, point 4) ---------------------------------------------
ACTIONS = {  # rôle : (Prêter et Retour sur les fiches et au comptoir, Renouveler au comptoir, actions de la liste)
    "admin": (True, True, True),
    "librarian": (True, True, True),
    "volunteer": (True, True, False),
    "viewer": (False, False, False),
}


@pytest.mark.parametrize("role", list(ACTIONS))
def test_actions_follow_the_permissions(client_as, shelf, role):
    lend, renew, listing_actions = ACTIONS[role]
    browser = client_as(role)
    notice = browser.get(f"/catalogue/notices/{shelf['late']}").text
    copies = browser.get(f"/catalogue/notices/{shelf['late']}?onglet=exemplaires").text
    copy_sheet = browser.get(f"/exemplaires/{shelf['late_copy']}").text
    grid = browser.get("/catalogue").text
    assert ('<span class="lbl">Prêter</span>' in notice) is lend
    assert ("Enregistrer le retour" in notice) is lend
    assert (f"#retour-{shelf['late_copy']}" in copies) is lend
    assert ("Enregistrer le retour" in copy_sheet) is lend
    assert ("/preter" in grid) is lend
    counter = browser.get("/comptoir")
    assert (counter.status_code == 200) is lend
    if lend:
        patron = re.search(r'name="choisir_lecteur" value="(\d+)"', counter.text).group(1)
        desk = browser.get(f"/comptoir?lecteur={patron}").text
        assert ("Renouveler" in desk) is renew and 'name="rendre"' in desk
    loans = browser.get("/prets")
    assert (loans.status_code == 200) is listing_actions
    if listing_actions:
        assert "/renouveler" in loans.text and "/retour" in loans.text
