# SPDX-License-Identifier: EUPL-1.2
"""Matrice des droits de **toutes** les routes des prêts, rôle par rôle, et emprunteur masqué sur toutes les voies."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.core.permissions import route_table
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.circulation import rules, service
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import LocationInput
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice

VIEW, LEND, RENEW, HISTORY, SETTINGS = (
    "patrons.view",
    "circulation.lend",
    "circulation.renew",
    "circulation.history",
    "circulation.settings",
)
EVERYONE = {"admin", "librarian", "volunteer"}
HISTORIANS = {"admin", "librarian"}
ROLES = ("admin", "librarian", "volunteer", "viewer")
#: Chaque route, la protection posée sur elle, et les rôles livrés qui passent. Une route non décrite fait échouer.
MATRIX: dict[tuple[str, str], tuple[set[str], set[str]]] = {
    ("GET", "/comptoir"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/scan"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/choix"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/etat"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/lecteurs"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/livres"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/livres/{work_id:int}/exemplaires"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/rendre/{loan_id:int}"): ({LEND}, EVERYONE),
    ("POST", "/comptoir/valider"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/renouveler/{loan_id}"): ({RENEW}, EVERYONE),
    ("POST", "/comptoir/annuler/{operation_id:int}"): ({VIEW}, EVERYONE),
    ("GET", "/comptoir/livre/{work_id:int}"): ({VIEW}, EVERYONE),
    ("GET", "/comptoir/livre/{work_id:int}/preter"): ({VIEW}, EVERYONE),
    ("POST", "/comptoir/livre/{work_id:int}/retour"): ({LEND}, EVERYONE),
    ("GET", "/comptoir/retours"): ({LEND}, EVERYONE),
    ("POST", "/comptoir/retours/scan"): ({LEND}, EVERYONE),
    ("POST", "/comptoir/retours/{loan_id:int}/annuler"): ({LEND}, EVERYONE),
    ("POST", "/comptoir/retours/recherche"): ({LEND}, EVERYONE),
    ("POST", "/comptoir/retours/pret/{loan_id:int}"): ({LEND}, EVERYONE),
    ("GET", "/prets"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/recherche"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/{loan_id:int}/retour"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/{loan_id:int}/renouveler"): ({HISTORY}, HISTORIANS),
    ("GET", "/prets/a-rappeler"): ({HISTORY}, HISTORIANS),
    ("GET", "/prets/{loan_id:int}"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/{loan_id:int}/echeance"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/{loan_id:int}/date-retour"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/{loan_id:int}/annuler"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/{loan_id:int}/lecteur"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/{loan_id:int}/rappel"): ({HISTORY}, HISTORIANS),
    ("POST", "/prets/{loan_id:int}/coordonnees"): ({HISTORY}, HISTORIANS),
    ("GET", "/prets/regles"): ({SETTINGS}, HISTORIANS),
    ("POST", "/prets/regles"): ({SETTINGS}, HISTORIANS),
    ("POST", "/prets/regles/ajouter"): ({SETTINGS}, HISTORIANS),
    ("POST", "/prets/regles/supprimer"): ({SETTINGS}, HISTORIANS),
}
BORROWER = ("Hortense", "Valmont")


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
def desk(settings, database):
    work = make_notice(settings, "Le Château des Carpathes")
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    copy_id, spare = catalogue.add_copies(database, edition, 2, "", None)
    for item in (copy_id, spare):
        _execute(settings, "UPDATE copies SET status = 'available' WHERE id = ?", item)
    room = holdings.create_location(database, "room", None, LocationInput("Salle"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble"), None)
    shelf = holdings.create_location(database, "shelf", furniture, LocationInput("Étagère"), None)
    holdings.shelve(database, [copy_id], shelf, user_id=None)
    person = PersonInput(first_name=BORROWER[0], last_name=BORROWER[1], category="adult", card_number="HV-1")
    patron = patrons.register(database, Registration(person=person), None)
    lent = service.lend(
        database, LendRequest(patron, (LendItem(copy_id),), rules.MODE_SELF), Actor(None, frozenset({LEND}))
    )
    code = _rows(settings, "SELECT barcode FROM copies WHERE id = ?", copy_id)[0][0]
    return {
        "work": work,
        "copy": copy_id,
        "code": code,
        "patron": patron,
        "loan": lent.loan_ids[0],
        "operation": lent.operation_id,
        "furniture": furniture,
    }


def _concrete(path: str, desk: dict) -> str:
    return (
        path.replace("{loan_id:int}", str(desk["loan"]))
        .replace("{loan_id}", str(desk["loan"]))
        .replace("{operation_id:int}", str(desk["operation"]))
        .replace("{work_id:int}", str(desk["work"]))
    )


def test_every_route_is_in_the_matrix_with_its_protection(app):
    found = {
        (method, route.path): set(permissions)
        for route, permissions in route_table(app)
        if route.path.startswith(("/comptoir", "/prets"))
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    assert found == {key: markers for key, (markers, _roles) in MATRIX.items()}


@pytest.mark.parametrize("role", ROLES)
def test_each_role_is_refused_exactly_where_it_should(client_as, desk, role):
    browser = client_as(role, f"compte-{role}")
    for (method, path), (_markers, allowed) in MATRIX.items():
        data = {"lecteur": str(desk["patron"])} if method == "POST" else None
        response = browser.request(method, _concrete(path, desk), data=data, follow_redirects=False)
        assert (response.status_code == 403) is (role not in allowed), (
            f"{role} {method} {path} : {response.status_code}"
        )


def test_the_volunteer_never_sees_the_holder_on_any_path(client_as, desk):
    """D11 bis : page, fragment, recherche, fiche d'exemplaire, étagère, liste, notice, accueil, palette, retour."""
    browser = client_as("volunteer")
    hx = {"HX-Request": "true"}
    requests = (
        ("POST", "/comptoir/scan", {"code": desk["code"]}, None),
        ("POST", "/comptoir/scan", {"code": desk["code"]}, hx),
        ("GET", f"/exemplaires/{desk['copy']}", None, None),
        ("GET", f"/emplacements/{desk['furniture']}", None, None),
        ("GET", f"/emplacements/{desk['furniture']}?vue=liste", None, None),
        ("GET", f"/catalogue/notices/{desk['work']}?onglet=exemplaires", None, None),
        ("GET", "/catalogue?view=list", None, None),
        ("POST", "/ui/recherche", {"q": "Carpathes"}, None),
        ("GET", "/", None, None),
        ("GET", f"/comptoir/livre/{desk['work']}", None, None),
        ("GET", f"/comptoir?exemplaire={desk['copy']}&livre={desk['work']}", None, None),
        ("POST", "/comptoir/retours/scan", {"code": desk["code"]}, hx),
    )
    for method, path, data, headers in requests:
        response = browser.request(method, path, data=data, headers=headers)
        assert response.status_code == 200, (path, response.status_code)
        # UX-Circulation 2.0 : le comptoir liste les lecteurs en service, comme l'annuaire que ce compte voit déjà,
        # sans aucun lien avec l'exemplaire sorti. Hors de cette liste, rien ne doit désigner le détenteur.
        text = re.sub(r'<section class="card desk-reader".*?</section>', "", response.text, count=1, flags=re.S)
        for secret in (*BORROWER, "HV-1", "P-000001", f"/prets/{desk['loan']}"):
            assert secret not in text, f"{path} : {secret}"


def test_the_holder_is_visible_with_the_history_right(client_as, desk):
    browser = client_as("librarian")
    assert f"/prets/{desk['loan']}" in browser.get(f"/exemplaires/{desk['copy']}").text
    assert " ".join(BORROWER) in browser.get(f"/prets/{desk['loan']}").text


def test_the_journal_of_loans_holds_no_title_reader_or_card(client_as, settings, desk):
    browser = client_as("librarian")
    browser.post(f"/prets/{desk['loan']}/rappel", data={"canal": "phone", "resultat": "reached"})
    browser.post(f"/prets/{desk['loan']}/echeance", data={"echeance": "2026-11-20"})
    browser.post("/comptoir/retours/scan", data={"code": desk["code"]})
    rows = _rows(settings, "SELECT action, details FROM audit_log WHERE action LIKE 'loan.%'")
    assert {row[0] for row in rows} >= {"loan.lend", "loan.reminder", "loan.due_changed", "loan.return"}
    text = " ".join(str(item) for row in rows for item in row)
    for secret in (*BORROWER, "HV-1", "Carpathes", desk["code"]):
        assert secret not in text, secret


def test_the_reader_list_of_the_counter_never_points_to_the_holder(client_as, desk):
    """La liste des lecteurs du comptoir ne dit jamais qui a quel livre : ni titre, ni code, ni lien vers un prêt."""
    browser = client_as("volunteer")
    for response in (
        browser.post("/comptoir/scan", data={"code": desk["code"]}),
        browser.post("/comptoir/lecteurs", data={"lecteur_q": "Valmont"}),
    ):
        listing = re.search(r'<ul class="desk-pick-list" aria-label="Lecteurs">.*?</ul>', response.text, re.S)
        assert listing is not None
        for secret in ("Carpathes", desk["code"], "/prets/", "En retard", "Emprunté"):
            assert secret not in listing.group(0), secret
