# SPDX-License-Identifier: EUPL-1.2
"""Matrice des permissions sur **toutes** les routes des lecteurs, HTML brut et journal au peigne fin."""

from __future__ import annotations

import logging
import re
import sqlite3

import pytest

from biblyo.app import create_app
from biblyo.core.db import Database
from biblyo.core.logs import PrivateQueryFilter
from biblyo.core.permissions import route_table
from biblyo.modules.patrons import service
from biblyo.modules.patrons.service import AddressInput, GuardianInput, HouseholdChoice, PersonInput, Registration

EMAIL = "camille.vidal@example.org"
PHONE = "06 12 34 56 78"
E164 = "+33612345678"
STREET = "8 chemin des Aulnes"
NOTE = "Rappeler avant midi"
FULL_BIRTH = "14/02/1975"
PROTECTED = (EMAIL, PHONE, E164, STREET, NOTE, FULL_BIRTH, "1975-02-14", "54600")

VIEW, REGISTER, PERSONAL, EDIT, ARCHIVE, EXPORT, ANONYMIZE = (
    "patrons.view",
    "patrons.register",
    "patrons.view_personal",
    "patrons.edit",
    "patrons.archive",
    "patrons.export",
    "patrons.anonymize",
)
ROLES: dict[str, set[str]] = {
    "admin": {VIEW, REGISTER, PERSONAL, EDIT, ARCHIVE, EXPORT, ANONYMIZE},
    "librarian": {VIEW, REGISTER, PERSONAL, EDIT, ARCHIVE, EXPORT},
    "volunteer": {VIEW, REGISTER},
    "viewer": set(),
}
#: Chaque route et ce qu'elle exige. Une route ajoutée sans être décrite ici fait échouer le test.
MATRIX: dict[tuple[str, str], set[str]] = {
    ("GET", "/lecteurs"): {VIEW},
    ("POST", "/lecteurs/recherche"): {VIEW},
    ("POST", "/lecteurs/choix"): {VIEW},
    ("POST", "/foyers/choix"): {VIEW},
    ("POST", "/ui/recherche/lecteurs"): {VIEW},
    ("GET", "/lecteurs/nouveau"): {REGISTER},
    ("POST", "/lecteurs/nouveau"): {REGISTER},
    ("GET", "/lecteurs/{patron_id:int}"): {VIEW},
    ("POST", "/lecteurs/{patron_id:int}/majorite"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/renouveler"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/carte"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/coordonnees/afficher"): {PERSONAL},
    ("GET", "/lecteurs/{patron_id:int}/coordonnees/masquer"): {PERSONAL},
    ("GET", "/lecteurs/{patron_id:int}/modifier"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/modifier"): {EDIT},
    ("GET", "/lecteurs/{patron_id:int}/coordonnees"): {PERSONAL, EDIT},
    ("POST", "/lecteurs/{patron_id:int}/coordonnees"): {PERSONAL, EDIT},
    ("POST", "/lecteurs/{patron_id:int}/foyers"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/foyers/{household_id:int}/retirer"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/responsables"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/responsables/{guardian_id:int}"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/responsables/{guardian_id:int}/retirer"): {EDIT},
    ("POST", "/lecteurs/{patron_id:int}/statut"): {ARCHIVE},
    ("POST", "/lecteurs/{patron_id:int}/conservation"): {ANONYMIZE},
    ("POST", "/lecteurs/{patron_id:int}/conservation/{hold_id:int}/lever"): {ANONYMIZE},
    ("GET", "/lecteurs/{patron_id:int}/anonymiser"): {ANONYMIZE},
    ("POST", "/lecteurs/{patron_id:int}/anonymiser"): {ANONYMIZE},
    ("GET", "/lecteurs/{patron_id:int}/export"): {EXPORT},
    ("GET", "/lecteurs/{patron_id:int}/export.json"): {EXPORT},
    ("GET", "/lecteurs/a-traiter"): {EDIT},
    ("GET", "/foyers/nouveau"): {REGISTER},
    ("POST", "/foyers/nouveau"): {REGISTER},
    ("GET", "/foyers/structure"): {EDIT},
    ("POST", "/foyers/structure"): {EDIT},
    ("GET", "/foyers/{household_id:int}"): {VIEW},
    ("POST", "/foyers/{household_id:int}/modifier"): {EDIT},
    ("POST", "/foyers/{household_id:int}/membres"): {EDIT},
    ("POST", "/foyers/{household_id:int}/membres/{patron_id:int}/role"): {EDIT},
    ("POST", "/foyers/{household_id:int}/membres/{patron_id:int}/retirer"): {EDIT},
    ("POST", "/foyers/{household_id:int}/membres/{patron_id:int}/structure"): {EDIT},
    ("POST", "/foyers/{household_id:int}/archiver"): {ARCHIVE},
    ("POST", "/foyers/{household_id:int}/reactiver"): {ARCHIVE},
    ("POST", "/foyers/{household_id:int}/coordonnees/afficher"): {PERSONAL},
    ("GET", "/foyers/{household_id:int}/coordonnees/masquer"): {PERSONAL},
    ("GET", "/foyers/{household_id:int}/coordonnees"): {PERSONAL, EDIT},
    ("POST", "/foyers/{household_id:int}/coordonnees"): {PERSONAL, EDIT},
}


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


@pytest.fixture
def people(database):
    adult = service.register(
        database,
        Registration(
            person=PersonInput(
                first_name="Camille",
                last_name="Vidal",
                category="adult",
                birth=FULL_BIRTH,
                email=EMAIL,
                phone=PHONE,
                address=AddressInput(line1=STREET, postal_code="54600", city="Villers"),
                notes=NOTE,
            ),
            household=HouseholdChoice(new_name="Famille Vidal", kind="family"),
        ),
        None,
    )
    minor = service.register(
        database,
        Registration(
            person=PersonInput(first_name="Jules", last_name="Vidal", category="minor", birth="14/02/2015"),
            household=HouseholdChoice(household_id=1, role="child"),
            guardian=GuardianInput(number="P-000001", can_borrow_for=True, is_contact=True),
        ),
        None,
    )
    return {"adult": adult, "minor": minor, "household": 1}


def _concrete(path: str, people: dict[str, int]) -> str:
    return (
        path.replace("{patron_id:int}", str(people["adult"]))
        .replace("{household_id:int}", str(people["household"]))
        .replace("{guardian_id:int}", str(people["adult"]))
        .replace("{hold_id:int}", "1")
    )


def test_every_route_is_in_the_matrix_with_the_right_permissions(app):
    found = {
        (method, route.path): set(permissions)
        for route, permissions in route_table(app)
        if route.path.startswith(("/lecteurs", "/foyers", "/ui/recherche/lecteurs"))
        for method in route.methods - {"HEAD", "OPTIONS"}
    }
    assert found == MATRIX


@pytest.mark.parametrize("role", list(ROLES))
def test_each_role_is_refused_exactly_where_it_should(client_as, people, role):
    browser = client_as(role, f"compte-{role}")
    for (method, path), needed in MATRIX.items():
        response = browser.request(method, _concrete(path, people), follow_redirects=False)
        allowed = needed <= ROLES[role]
        assert (response.status_code == 403) is not allowed, f"{role} {method} {path} : {response.status_code}"


@pytest.mark.parametrize("role", ["volunteer"])
def test_raw_html_holds_nothing_protected_without_the_permission(client_as, people, role):
    browser = client_as(role, "vol")
    adult, minor, household = people["adult"], people["minor"], people["household"]
    requests = (
        ("GET", "/lecteurs", None),
        ("POST", "/lecteurs/recherche", {"q": "Vidal"}),
        ("GET", "/lecteurs?vue=foyers", None),
        ("GET", "/lecteurs?vue=mineurs", None),
        ("POST", "/lecteurs/choix", {"choix_q": "Vidal"}),
        ("POST", "/foyers/choix", {"choix_q": "Vidal"}),
        ("POST", "/ui/recherche/lecteurs", {"q": "Vidal"}),
        ("GET", f"/lecteurs/{adult}", None),
        ("GET", f"/lecteurs/{minor}", None),
        ("GET", f"/foyers/{household}", None),
    )
    for method, path, data in requests:
        page = browser.request(method, path, data=data).text
        for secret in PROTECTED:
            assert secret not in page, f"{path} : {secret}"


def test_full_birth_of_a_minor_is_kept_from_a_volunteer(client_as, people):
    page = client_as("volunteer").get(f"/lecteurs/{people['minor']}").text
    assert "14/02/2015" not in page and "2015" in page


def test_journal_holds_no_personal_value(client_as, settings, people):
    librarian = client_as("librarian")
    librarian.post(f"/lecteurs/{people['adult']}/coordonnees/afficher")
    librarian.get(f"/lecteurs/{people['adult']}/coordonnees")
    librarian.post(
        f"/lecteurs/{people['adult']}/modifier",
        data={"version": "1", "prenom": "Camille", "nom": "Vidal-Roy", "naissance": "1975", "observations": "Autre"},
    )
    connection = sqlite3.connect(settings.database_path)
    rows = connection.execute("SELECT action, details FROM audit_log").fetchall()
    connection.close()
    assert len(rows) >= 5
    text = " ".join(str(item) for row in rows for item in row)
    for secret in (*PROTECTED, "Vidal", "Camille", "Autre", "Jules"):
        assert secret not in text, secret


def test_a_search_never_reaches_an_address(client_as, people):
    librarian = client_as("librarian")
    for query in ("Vidal", EMAIL, PHONE, "+33 6 12 34 56 78", "L-000001"):
        response = librarian.post("/lecteurs/recherche", data={"q": query}, headers={"HX-Request": "true"})
        assert response.status_code == 200, query
        assert response.headers["HX-Replace-Url"] == "/lecteurs", query
        assert "HX-Push-Url" not in response.headers, query


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", path, "1.1", 200),
        None,
    )


def test_access_log_keeps_only_the_path_of_reader_pages():
    record = _access_record("/lecteurs?q=camille.vidal%40example.org")
    assert PrivateQueryFilter().filter(record) is True
    assert "example.org" not in record.getMessage() and "/lecteurs" in record.getMessage()
    other = _access_record("/catalogue?q=tintin")
    PrivateQueryFilter().filter(other)
    assert "tintin" in other.getMessage()


def test_the_filter_is_installed_with_the_application(settings):
    create_app(settings)
    logger = logging.getLogger("uvicorn.access")
    assert sum(isinstance(item, PrivateQueryFilter) for item in logger.filters) == 1


def test_no_template_of_the_module_prints_a_contact_value_outside_the_reserved_fragments():
    from pathlib import Path

    import biblyo.modules.patrons as package

    root = Path(package.__file__).parent / "templates"
    reserved = {
        "contacts.html",
        "household_contacts.html",
        "register.html",
        "family_new.html",
        "edit_contacts.html",
        "household_contacts_edit.html",
    }
    pattern = re.compile(r"line\.(display|value|line1|postal_code)|\.search_value|patron\.birth\b|patron\.notes\b")
    for template in root.rglob("*.html"):
        if template.name not in reserved:
            assert not pattern.search(template.read_text(encoding="utf-8")), template.name
