# SPDX-License-Identifier: EUPL-1.2
"""Palette ⌘K : lecteurs trouvés selon le rôle, sans coordonnée, sans terme dans une adresse, sans énumération."""

from __future__ import annotations

import logging
import re
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from biblyo.core.db import Database, make_engine
from biblyo.modules.patrons import service
from biblyo.modules.patrons.service import (
    AddressInput,
    AnonymizeInput,
    GuardianInput,
    HouseholdChoice,
    PersonInput,
    Registration,
)

from ..factories import PASSWORD, make_notice, make_user

URL = "/ui/recherche/lecteurs"
EMAIL = "lea.martin@example.org"
PHONE = "0612345678"
STREET = "4 rue des Saules"
CITY = "Vandœuvre"


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


def _register(database, first, last, category="adult", **options) -> int:
    fields = {key: options.pop(key) for key in ("birth", "email", "phone", "address", "notes") if key in options}
    person = PersonInput(first_name=first, last_name=last, category=category, **fields)
    return service.register(database, Registration(person=person, homonyms_confirmed=True, **options), None)


@pytest.fixture
def people(database):
    adult = _register(
        database,
        "Léa",
        "Martin",
        birth="1990",
        email=EMAIL,
        phone=PHONE,
        address=AddressInput(line1=STREET, postal_code="54500", city=CITY),
        notes="Note interne",
        household=HouseholdChoice(new_name="Famille Martin", kind="family"),
    )
    minor = _register(
        database,
        "Léa",
        "Martin",
        "minor",
        birth="2016",
        guardian=GuardianInput(number="P-000001", can_borrow_for=True, is_contact=True),
    )
    other = _register(database, "Anne", "Durand")
    return {"adult": adult, "minor": minor, "other": other}


def _ids(page: str) -> list[int]:
    return [int(item) for item in re.findall(r'href="/lecteurs/(\d+)"', page)]


def _search(browser, query: str):
    return browser.post(URL, data={"q": query})


# --- Rôles ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["admin", "librarian", "volunteer"])
def test_roles_that_may_see_readers_find_them(client_as, people, role):
    response = _search(client_as(role, f"c-{role}"), "Durand")
    assert response.status_code == 200 and _ids(response.text) == [people["other"]]
    assert "<html" not in response.text


def test_viewer_is_refused_and_is_not_offered_the_search(client_as, people):
    viewer = client_as("viewer")
    response = _search(viewer, "Durand")
    assert response.status_code == 403 and "Durand" not in response.text
    assert URL not in viewer.get("/").text


def test_anonymous_visitor_is_refused(client, people):
    assert client.post(URL, data={"q": "Durand"}).status_code in (303, 401, 403)


@pytest.mark.parametrize("role", ["admin", "librarian", "volunteer"])
def test_palette_is_offered_to_those_allowed(client_as, role):
    assert f'hx-post="{URL}"' in client_as(role, f"c-{role}").get("/").text


# --- Ce qu'on peut chercher -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["Durand", "durand", "Anne", "anne dur", "P-000003", "p3", "L-000003", "l-000003"])
def test_search_by_name_first_name_number_or_active_card(client_as, people, query):
    assert _ids(_search(client_as("volunteer"), query).text) == [people["other"]]


def test_accents_are_ignored(client_as, people):
    assert set(_ids(_search(client_as("volunteer"), "LEA MARTIN").text)) == {people["adult"], people["minor"]}


@pytest.mark.parametrize("role", ["admin", "librarian", "volunteer"])
@pytest.mark.parametrize("query", [EMAIL, PHONE, "06 12 34 56 78", "+33612345678", STREET, "Saules", "54500"])
def test_contacts_are_never_searched_even_with_the_permission(client_as, people, role, query):
    assert _ids(_search(client_as(role, f"c-{role}"), query).text) == []


# --- Homonymes --------------------------------------------------------------------------------------------------------
def test_homonyms_are_distinct_results(client_as, people):
    page = _search(client_as("volunteer"), "Léa Martin").text
    assert len(_ids(page)) == 2 and page.count("Homonyme") == 2
    for mark in ("P-000001", "L-000001", "naissance 1990", "Famille Martin", "P-000002", "naissance 2016"):
        assert mark in page, mark
    assert "resp. Léa Martin" in page
    assert "Adulte" in page and "Mineur" in page


# --- Cartes -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reason", ["lost", "revoked", "damaged"])
def test_a_card_out_of_service_leads_to_nobody(client_as, database, people, reason):
    service.end_card(database, people["other"], reason, replace=False, user_id=None)
    assert _ids(_search(client_as("volunteer"), "L-000003").text) == []


def test_a_replaced_card_is_found_only_under_its_new_number(client_as, database, people):
    new_number = service.end_card(database, people["other"], "lost", replace=True, user_id=None)
    browser = client_as("volunteer")
    assert _ids(_search(browser, "L-000003").text) == []
    page = _search(browser, new_number).text
    assert _ids(page) == [people["other"]] and "L-000003" not in page


# --- Archivés et anonymisés -------------------------------------------------------------------------------------------
def test_archived_readers_need_the_archive_permission(client_as, database, people):
    service.change_status(database, people["other"], "archive", None, None)
    volunteer = client_as("volunteer", "vol")
    for query in ("Durand", "P-000003", "L-000003"):
        assert _ids(_search(volunteer, query).text) == [], query
    librarian = client_as("librarian", "bib")
    page = _search(librarian, "Durand").text
    assert _ids(page) == [people["other"]] and "Archivé" in page


def test_anonymized_reader_is_found_only_by_number_with_the_permission(client_as, database, settings, people):
    admin_id = make_user(settings, "chef", "admin")
    service.anonymize(database, people["other"], AnonymizeInput("P-000003", PASSWORD, "request", ""), admin_id)
    admin = client_as("admin", "chef2")
    assert _ids(_search(admin, "Durand").text) == []
    assert _ids(_search(admin, "L-000003").text) == []  # sa carte ne désigne plus personne
    page = _search(admin, "P-000003").text
    assert _ids(page) == [people["other"]] and "Lecteur anonymisé" in page and "Durand" not in page
    for role in ("librarian", "volunteer"):
        assert _ids(_search(client_as(role, f"c-{role}"), "P-000003").text) == [], role


# --- Aucune fuite -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["admin", "librarian", "volunteer"])
@pytest.mark.parametrize("query", ["Léa Martin", "P-000001", "L-000001"])
def test_no_contact_or_private_detail_in_the_fragment(client_as, people, role, query):
    page = _search(client_as(role, f"c-{role}"), query).text
    assert _ids(page)
    for secret in (EMAIL, "example.org", PHONE, "06 12 34", "+33612", STREET, "Saules", "54500", CITY, "Note interne"):
        assert secret not in page, secret


def test_the_full_birth_date_never_appears(client_as, database):
    _register(database, "Bruno", "Tessier", birth="14/02/1975")
    page = _search(client_as("librarian"), "Tessier").text
    assert "1975" in page and "14/02" not in page and "1975-02" not in page


def test_names_are_escaped(client_as, database):
    _register(database, "<script>alert(1)</script>", "Tessier")
    assert "<script>alert(1)" not in _search(client_as("volunteer"), "Tessier").text


# --- Aucun terme conservé ---------------------------------------------------------------------------------------------
def test_get_is_not_a_way_in(client_as, people):
    browser = client_as("admin")
    assert browser.get(f"{URL}?q=Durand").status_code == 405
    assert browser.get("/ui/recherche?q=Durand").status_code == 405


def test_the_palette_sends_its_text_in_the_body_only(client_as):
    page = client_as("admin").get("/").text
    palette = page[page.index('id="palette"') :]
    assert 'hx-post="/ui/recherche"' in palette and "hx-get" not in palette[: palette.index("</dialog>")]
    assert "hx-push-url" not in palette[: palette.index("</dialog>")]
    assert 'autocomplete="off"' in palette[: palette.index("</dialog>")]


def test_response_never_rewrites_the_address(client_as, people):
    assert "HX-Push-Url" not in _search(client_as("volunteer"), "Durand").headers


def test_search_terms_never_reach_the_journal(client_as, settings, people):
    browser = client_as("admin")
    for query in ("Durand", "Léa Martin", "P-000003"):
        _search(browser, query)
    text_log = " ".join(str(item) for row in _rows(settings, "SELECT action, details FROM audit_log") for item in row)
    assert "Durand" not in text_log and "palette" not in text_log


def test_nothing_is_logged_with_the_term(client_as, people, caplog):
    with caplog.at_level(logging.DEBUG):
        _search(client_as("volunteer"), "Durand")
    assert "Durand" not in caplog.text


def test_sql_errors_do_not_copy_what_was_searched(settings):
    engine = make_engine(settings.database_path)
    with pytest.raises(OperationalError) as caught, engine.connect() as connection:
        connection.execute(text("SELECT * FROM table_absente WHERE nom = :nom"), {"nom": "Durand-Secret"})
    engine.dispose()
    assert "Durand-Secret" not in str(caught.value)


def test_a_too_long_text_is_cut_and_harmless(client_as, people):
    assert _search(client_as("volunteer"), "x" * 5000).status_code == 200


# --- Trop large, énumération ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["", "  ", "a", "la", "%", "_", "%%%", "***", "' OR 1=1 --"])
def test_too_broad_or_meaningless_texts_search_nothing(client_as, people, query):
    assert _ids(_search(client_as("volunteer"), query).text) == []


def test_results_are_capped_without_saying_how_many(client_as, database):
    for index in range(12):
        _register(database, f"Prenom{index:02d}", "Lemoine")
    page = _search(client_as("volunteer"), "Lemoine").text
    assert len(_ids(page)) == 8 and "précisez" in page
    assert not re.search(r"\b12\b", page)


def test_exactly_the_cap_says_nothing_more(client_as, database):
    for index in range(8):
        _register(database, f"Prenom{index:02d}", "Lemoine")
    page = _search(client_as("volunteer"), "Lemoine").text
    assert len(_ids(page)) == 8 and "précisez" not in page


def test_massive_enumeration_is_stopped_and_traced_once(app, client_as, settings, people):
    app.state.search_limiter.limit = 3
    browser = client_as("volunteer", "vol")
    for number in (1, 2, 3):
        assert _ids(_search(browser, f"P-{number:06d}").text)
    for _attempt in range(5):
        stopped = _search(browser, "P-000001").text
        assert _ids(stopped) == [] and "Patientez" in stopped
    rows = _rows(settings, "SELECT entity_type, details FROM audit_log WHERE action = 'patron.search_limited'")
    assert rows == [("user", None)]


def test_the_limit_applies_per_account(app, client_as, people):
    app.state.search_limiter.limit = 2
    first = client_as("volunteer", "un")
    for _attempt in range(3):
        _search(first, "Durand")
    assert "Patientez" in _search(first, "Durand").text
    assert _ids(_search(client_as("volunteer", "deux"), "Durand").text) == [people["other"]]


def test_empty_text_does_not_consume_the_allowance(app, client_as, people):
    app.state.search_limiter.limit = 1
    browser = client_as("volunteer")
    for _attempt in range(5):
        _search(browser, "")
    assert _ids(_search(browser, "Durand").text) == [people["other"]]


# --- La recherche des ouvrages ne bouge pas -------------------------------------------------------------------------
def test_notices_still_found_by_the_palette(client_as, settings):
    work = make_notice(settings, "Le Secret de la licorne")
    page = client_as("viewer").post("/ui/recherche", data={"q": "licorne"}).text
    assert f'href="/catalogue/notices/{work}"' in page and "data-server" in page
    assert client_as("viewer", "v2").get("/ui/recherche?q=licorne").status_code == 405
