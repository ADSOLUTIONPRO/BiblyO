# SPDX-License-Identifier: EUPL-1.2
"""Annuaire et sélecteur : recherche, vues, homonymes, aucune coordonnée dans le HTML. Personnes inventées."""

from __future__ import annotations

import re

import pytest

from biblyo.core.db import Database
from biblyo.modules.patrons import repository as repo
from biblyo.modules.patrons import service
from biblyo.modules.patrons.models import Patron
from biblyo.modules.patrons.rules import CARD_ENDED_NOTE
from biblyo.modules.patrons.service import (
    AddressInput,
    GuardianInput,
    HouseholdChoice,
    PersonInput,
    Registration,
)

EMAIL = "paul.martin@example.org"
PHONE = "06 12 34 56 78"
STREET = "12 rue des Tilleuls"


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _adult(database, first="Paul", last="Martin", **extra) -> int:
    person = PersonInput(first_name=first, last_name=last, category="adult", **extra)
    return service.register(database, Registration(person=person, homonyms_confirmed=True), None)


def _minor(database, first="Léa", last="Martin", **options) -> int:
    person = PersonInput(first_name=first, last_name=last, category="minor", birth="2016")
    return service.register(database, Registration(person=person, homonyms_confirmed=True, **options), None)


@pytest.fixture
def people(database):
    paul = _adult(
        database,
        email=EMAIL,
        phone=PHONE,
        address=AddressInput(line1=STREET, postal_code="54000", city="Nancy"),
    )
    other = _adult(database, "Paul", "Martin")
    anne = _adult(database, "Anne", "Durand")
    lea = _minor(
        database,
        household=HouseholdChoice(new_name="Famille Martin", kind="family", role="child"),
        guardian=GuardianInput(number="P-000001", relation="parent", can_borrow_for=True, is_contact=True),
    )
    alone = _minor(database, "Tom", "Roux")
    return {"paul": paul, "other": other, "anne": anne, "lea": lea, "alone": alone}


def _links(page: str) -> set[str]:
    return set(re.findall(r'href="/lecteurs/(\d+)"', page))


def test_entry_is_in_the_navigation_for_who_may_see_it(client_as):
    assert 'href="/lecteurs"' in client_as("volunteer").get("/").text
    assert 'href="/lecteurs"' not in client_as("viewer").get("/").text
    assert "/a-venir/lecteurs" not in client_as("admin").get("/").text


@pytest.mark.parametrize("role", ["viewer"])
def test_directory_and_picker_are_refused_without_the_permission(client_as, people, role):
    browser = client_as(role)
    assert browser.get("/lecteurs").status_code == 403
    assert browser.post("/lecteurs/choix", data={"choix_q": "Martin"}).status_code == 403


def test_empty_directory(client_as):
    page = client_as("admin").get("/lecteurs").text
    assert "Aucun lecteur" in page and "0 lecteur" in page


def test_directory_lists_living_readers_with_their_marks(client_as, people):
    page = client_as("volunteer").get("/lecteurs").text
    assert _links(page) == {str(item) for item in people.values()}
    assert "P-000001" in page and "L-000001" in page
    assert "naissance 2016" in page and "Nancy" in page
    assert "Famille Martin" in page
    assert "resp. Paul Martin" in page


def test_homonyms_are_flagged_and_never_merged(client_as, people):
    page = client_as("volunteer").post("/lecteurs/recherche", data={"q": "Paul Martin"}).text
    assert _links(page) == {str(people["paul"]), str(people["other"])}
    assert page.count("Homonyme") == 2
    assert "P-000001" in page and "P-000002" in page


def test_minor_without_guardian_is_flagged(client_as, people):
    page = client_as("volunteer").get("/lecteurs", params={"vue": "responsable"}).text
    assert _links(page) == {str(people["alone"])}
    assert "Responsable à renseigner" in page


def test_minor_with_an_authorised_reachable_guardian_is_not_flagged(database, people):
    with database.session() as session:
        rows = repo.directory(session, "responsable", 1, service.today()).rows
    assert [row.id for row in rows] == [people["alone"]]


@pytest.mark.parametrize(
    ("view", "expected"),
    [("adultes", {"paul", "other", "anne"}), ("mineurs", {"lea", "alone"}), ("tous", None), ("inconnue", None)],
)
def test_views(client_as, people, view, expected):
    page = client_as("volunteer").get("/lecteurs", params={"vue": view}).text
    wanted = set(people) if expected is None else expected
    assert _links(page) == {str(people[name]) for name in wanted}


def test_archived_are_apart_and_counted_when_a_search_matches_them(client_as, database, people):
    service.change_status(database, people["anne"], "archive", None, None)
    browser = client_as("librarian")
    assert str(people["anne"]) not in _links(browser.get("/lecteurs").text)
    assert _links(browser.get("/lecteurs", params={"vue": "archives"}).text) == {str(people["anne"])}
    page = browser.post("/lecteurs/recherche", data={"q": "Durand"}).text
    assert _links(page) == set() and "1 fiche archivée correspond aussi" in page


def test_expired_membership_view(client_as, database, people):
    connection_page = client_as("volunteer").get("/lecteurs", params={"vue": "adhesion"}).text
    assert _links(connection_page) == set()
    with database.session(write=True) as session:
        patron = session.get(Patron, people["anne"])
        patron.membership_expires_on = "2020-01-31"
    assert _links(client_as("librarian", "bib").get("/lecteurs", params={"vue": "adhesion"}).text) == {
        str(people["anne"])
    }


def test_search_by_number_and_by_card(client_as, people):
    browser = client_as("volunteer")
    assert _links(browser.post("/lecteurs/recherche", data={"q": "p-000003"}).text) == {str(people["anne"])}
    assert _links(browser.post("/lecteurs/recherche", data={"q": "L-000003"}).text) == {str(people["anne"])}


def test_search_by_an_old_card_says_so(client_as, database, people):
    service.end_card(database, people["anne"], "lost", replace=True, user_id=None)
    page = client_as("volunteer").post("/lecteurs/recherche", data={"q": "L-000003"}).text
    assert _links(page) == {str(people["anne"])}
    assert CARD_ENDED_NOTE in page


def test_search_is_accent_and_case_insensitive(client_as, database):
    target = _adult(database, "Noémie", "Lefèvre")
    assert _links(client_as("volunteer").post("/lecteurs/recherche", data={"q": "noemie LEFEVRE"}).text) == {
        str(target)
    }


def test_volunteer_cannot_probe_a_contact_through_search(client_as, people):
    volunteer = client_as("volunteer", "vol")
    assert _links(volunteer.post("/lecteurs/recherche", data={"q": EMAIL}).text) == set()
    assert _links(volunteer.post("/lecteurs/recherche", data={"q": PHONE}).text) == set()
    assert volunteer.post("/lecteurs/choix", data={"choix_q": EMAIL}).text.count('role="option"') == 0


def test_librarian_can_search_by_contact(client_as, people):
    librarian = client_as("librarian", "bib")
    assert _links(librarian.post("/lecteurs/recherche", data={"q": EMAIL}).text) == {str(people["paul"])}
    assert _links(librarian.post("/lecteurs/recherche", data={"q": PHONE}).text) == {str(people["paul"])}
    assert f'data-id="{people["paul"]}"' in librarian.post("/lecteurs/choix", data={"choix_q": EMAIL}).text


@pytest.mark.parametrize("role", ["volunteer", "librarian", "admin"])
@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("GET", "/lecteurs", None),
        ("POST", "/lecteurs/recherche", {"q": "Martin"}),
        ("GET", "/lecteurs?vue=foyers", None),
        ("POST", "/lecteurs/choix", {"choix_q": "Martin"}),
    ],
)
def test_no_contact_ever_travels_in_a_list(client_as, people, role, method, path, data):
    page = client_as(role, f"compte-{role}").request(method, path, data=data).text
    for secret in (EMAIL, "example.org", "+33612345678", "06 12 34 56 78", STREET, "Tilleuls", "54000"):
        assert secret not in page, f"{role} {path} : {secret}"


def test_household_view_finds_by_household_or_member(client_as, people):
    browser = client_as("volunteer")
    page = browser.get("/lecteurs", params={"vue": "foyers"}).text
    assert "Famille Martin" in page and f'href="/lecteurs/{people["lea"]}"' in page and "Enfant" in page
    assert "Famille Martin" in browser.post("/lecteurs/recherche", data={"vue": "foyers", "q": "Léa"}).text
    assert "Aucun résultat" in browser.post("/lecteurs/recherche", data={"vue": "foyers", "q": "zzz"}).text


def test_register_buttons_follow_the_permission(client_as, people):
    assert 'href="/lecteurs/nouveau"' in client_as("volunteer", "vol").get("/lecteurs").text
    settings_free = client_as("librarian", "bib").get("/lecteurs").text
    assert 'href="/foyers/nouveau"' in settings_free


def test_directory_is_never_cached(client_as, people):
    assert client_as("volunteer").get("/lecteurs").headers["Cache-Control"] == "no-store"
    assert client_as("volunteer", "vol2").post("/lecteurs/choix").headers["Cache-Control"] == "no-store"


def test_names_are_escaped(client_as, database):
    _adult(database, "<script>alert(1)</script>", "Martin")
    browser = client_as("volunteer")
    assert "<script>alert(1)" not in browser.get("/lecteurs").text
    assert "<script>alert(1)" not in browser.post("/lecteurs/choix", data={"choix_q": "Martin"}).text


def test_picker_shows_what_tells_homonyms_apart(client_as, people):
    page = client_as("volunteer").post("/lecteurs/choix", data={"choix_q": "Martin"}).text
    assert page.count('role="option"') == 3
    assert page.count("Homonyme") == 2  # les deux Paul Martin ; Léa porte le même nom de famille, pas le même nom
    assert "P-000001" in page and "L-000001" in page and "naissance 2016" in page and "resp. Paul Martin" in page


def test_picker_can_be_limited_to_active_adults(client_as, database, people):
    service.change_status(database, people["other"], "archive", None, None)
    page = client_as("volunteer").post("/lecteurs/choix", params={"adultes": "1"}, data={"choix_q": "Martin"}).text
    assert re.findall(r'data-id="(\d+)"', page) == [str(people["paul"])]


def test_picker_without_text_or_match(client_as, people):
    browser = client_as("volunteer")
    assert 'role="option"' not in browser.post("/lecteurs/choix").text
    assert "Aucun lecteur ne correspond" in browser.post("/lecteurs/choix", data={"choix_q": "zzz"}).text


def test_anonymized_reader_is_found_only_by_number(client_as, database, settings, people):
    from ..factories import PASSWORD, make_user

    admin = make_user(settings, "chef", "admin")
    service.anonymize(
        database,
        people["anne"],
        service.AnonymizeInput("P-000003", PASSWORD, "request", ""),
        admin,
    )
    browser = client_as("volunteer")
    assert _links(browser.post("/lecteurs/recherche", data={"q": "Durand"}).text) == set()
    admin_view = client_as("admin", "chef2").get("/lecteurs", params={"vue": "archives"}).text
    assert "Durand" not in admin_view and "Lecteur anonymisé" in admin_view
