# SPDX-License-Identifier: EUPL-1.2
"""Fiches archivées et anonymisées, fermées sans la permission, sur toutes les voies. Recherche et pagination."""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.patrons import service
from biblyo.modules.patrons.service import (
    AnonymizeInput,
    GuardianInput,
    HouseholdChoice,
    PersonInput,
    Registration,
    Rights,
)

from ..factories import PASSWORD, make_user

ARCHIVED_NAME = "Blanche Archivée"
ALL_BUT_ARCHIVE = (
    "patrons.view",
    "patrons.register",
    "patrons.view_personal",
    "patrons.edit",
    "patrons.export",
)


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
    fields = {key: options.pop(key) for key in ("birth", "email") if key in options}
    person = PersonInput(first_name=first, last_name=last, category=category, **fields)
    return service.register(database, Registration(person=person, homonyms_confirmed=True, **options), None)


@pytest.fixture
def world(database, settings):
    active = _register(database, "Paul", "Vivant")
    withdrawn = _register(
        database,
        "Blanche",
        "Archivée",
        email="blanche@example.org",
        household=HouseholdChoice(new_name="Famille Mixte", kind="family"),
    )
    minor = _register(database, "Tom", "Vivant", "minor", birth="2016", guardian=GuardianInput(number="P-000001"))
    service.add_guardian(database, minor, withdrawn, Rights("parent", True, True, 2), None)
    service.add_member(database, 1, active, "member", None)
    gone = _register(database, "Gaston", "Effacé")
    service.anonymize(
        database, gone, AnonymizeInput("P-000004", PASSWORD, "request", ""), make_user(settings, "chef", "admin")
    )
    service.change_status(database, withdrawn, "archive", None, None)
    return {"active": active, "withdrawn": withdrawn, "minor": minor, "gone": gone, "home": 1}


def _custom(settings, client_as, code: str, permissions: tuple[str, ...]):
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO roles (code, label, is_system, created_at) VALUES (?, ?, 0, '2026-11-09')", (code, code)
    )
    role = connection.execute("SELECT id FROM roles WHERE code = ?", (code,)).fetchone()[0]
    for permission in permissions:
        connection.execute("INSERT INTO role_permissions (role_id, permission) VALUES (?, ?)", (role, permission))
    connection.commit()
    connection.close()
    return client_as(code)


def _ids(page: str) -> set[int]:
    return {int(item) for item in re.findall(r'href="/lecteurs/(\d+)"', page)}


# --- Vue « Archivés », quatre rôles -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("role", "status", "expected"),
    [
        ("admin", 200, {"withdrawn", "gone"}),
        ("librarian", 200, {"withdrawn"}),
        ("volunteer", 403, None),
        ("viewer", 403, None),
    ],
)
def test_archives_view_by_role(client_as, world, role, status, expected):
    browser = client_as(role, f"c-{role}")
    for response in (
        browser.get("/lecteurs", params={"vue": "archives"}),
        browser.post("/lecteurs/recherche", data={"vue": "archives"}),
    ):
        assert response.status_code == status
        if expected is not None:
            assert _ids(response.text) == {world[name] for name in expected}
        else:
            assert ARCHIVED_NAME not in response.text


@pytest.mark.parametrize(("role", "offered"), [("admin", True), ("librarian", True), ("volunteer", False)])
def test_archives_chip_follows_the_permission(client_as, role, offered):
    assert ('value="archives"' in client_as(role, f"c-{role}").get("/lecteurs").text) is offered


@pytest.mark.parametrize(
    ("role", "archived", "anonymized"),
    [("admin", 200, 200), ("librarian", 200, 403), ("volunteer", 403, 403), ("viewer", 403, 403)],
)
def test_direct_address_by_role(client_as, world, role, archived, anonymized):
    browser = client_as(role, f"c-{role}")
    assert browser.get(f"/lecteurs/{world['withdrawn']}").status_code == archived
    assert browser.get(f"/lecteurs/{world['gone']}").status_code == anonymized


def test_every_route_of_a_withdrawn_reader_is_closed(app, settings, client_as, world):
    """Un compte qui a tout sauf `patrons.archive` : chaque route désignant cette fiche répond 403, sans rien écrire."""
    browser = _custom(settings, client_as, "sans_archive", ALL_BUT_ARCHIVE)
    before = _rows(settings, "SELECT count(*) FROM audit_log")
    closed = 0
    from biblyo.core.permissions import route_table

    for route, _permissions in route_table(app):
        if "{patron_id:int}" not in route.path:
            continue
        path = route.path.replace("{patron_id:int}", str(world["withdrawn"]))
        path = re.sub(r"\{\w+:int\}", "1", path)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            response = browser.request(method, path)
            assert response.status_code == 403, f"{method} {path} : {response.status_code}"
            assert "blanche@example.org" not in response.text and ARCHIVED_NAME not in response.text
            closed += 1
    assert closed >= 20
    assert _rows(settings, "SELECT count(*) FROM audit_log") == before


def test_with_the_permission_the_same_routes_open(client_as, world):
    librarian = client_as("librarian")
    assert librarian.post(f"/lecteurs/{world['withdrawn']}/coordonnees/afficher").status_code == 200
    assert librarian.get(f"/lecteurs/{world['withdrawn']}/export.json").status_code == 200


# --- Fragments, listes et liens ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["volunteer"])
def test_no_fragment_or_list_reveals_an_archived_reader(client_as, world, role):
    browser = client_as(role, f"c-{role}")
    pages = {
        "recherche": browser.post("/lecteurs/recherche", data={"q": "Archivée"}).text,
        "numero": browser.post("/lecteurs/recherche", data={"q": "P-000002"}).text,
        "selecteur": browser.post("/lecteurs/choix", data={"choix_q": "Archivée"}).text,
        "selecteur_numero": browser.post("/lecteurs/choix", data={"choix_q": "P-000002"}).text,
        "palette": browser.post("/ui/recherche/lecteurs", data={"q": "Archivée"}).text,
        "foyers": browser.get("/lecteurs", params={"vue": "foyers"}).text,
        "foyer_par_membre": browser.post("/lecteurs/recherche", data={"q": "Archivée", "vue": "foyers"}).text,
        "choix_foyer": browser.post("/foyers/choix", data={"choix_q": "Mixte"}).text,
        "fiche_foyer": browser.get(f"/foyers/{world['home']}").text,
        "fiche_mineur": browser.get(f"/lecteurs/{world['minor']}").text,
    }
    for name, page in pages.items():
        assert ARCHIVED_NAME not in page and "Archivée" not in page.replace('value="Archivée"', ""), name
        assert world["withdrawn"] not in _ids(page), name
    assert "correspond aussi" not in pages["recherche"]
    assert "Aucun résultat" in browser.post("/lecteurs/recherche", data={"q": "Archivée", "vue": "foyers"}).text


def test_marks_never_name_an_archived_guardian(client_as, database, world):
    service.remove_guardian(database, world["minor"], world["active"], None)
    page = client_as("librarian").get("/lecteurs", params={"vue": "mineurs"}).text
    assert "resp. Blanche" not in page


def test_a_homonym_flag_does_not_betray_an_archived_reader(client_as, database, world):
    twin = _register(database, "Blanche", "Archivée")
    volunteer = client_as("volunteer", "vol")
    assert "Homonyme" not in volunteer.post("/lecteurs/recherche", data={"q": "Blanche"}).text
    assert twin in _ids(volunteer.post("/lecteurs/recherche", data={"q": "Blanche"}).text)
    assert "Homonyme" in client_as("librarian", "bib").post("/lecteurs/recherche", data={"q": "Blanche"}).text


def test_registration_warns_about_a_hidden_homonym_without_its_details(client_as, settings, world):
    volunteer = client_as("volunteer")
    data = {"categorie": "adult", "prenom": "Blanche", "nom": "Archivée"}
    review = volunteer.post("/lecteurs/nouveau", data={**data, "etape": "verifier"})
    assert "Une fiche archivée porte aussi ce nom" in review.text
    assert "P-000002" not in review.text and "L-000002" not in review.text
    refused = volunteer.post("/lecteurs/nouveau", data={**data, "etape": "enregistrer"})
    assert refused.status_code == 400
    assert _rows(settings, "SELECT count(*) FROM patrons") == [(4,)]
    assert (
        volunteer.post("/lecteurs/nouveau", data={**data, "etape": "enregistrer", "homonymes_ok": "1"}).status_code
        == 303
    )


def test_family_registration_warns_too(client_as, world):
    data = {"etape": "verifier", "nom_foyer": "Famille A", "a0_prenom": "Blanche", "a0_nom": "Archivée"}
    review = client_as("volunteer").post("/foyers/nouveau", data=data).text
    assert "Une fiche archivée porte aussi ce nom" in review and "P-000002" not in review


def test_an_archived_reader_cannot_be_linked_by_its_identifier(settings, client_as, world):
    browser = _custom(settings, client_as, "sans_archive", ALL_BUT_ARCHIVE)
    assert (
        browser.post(f"/foyers/{world['home']}/membres", data={"membre_id": str(world["withdrawn"])}).status_code == 400
    )
    response = browser.post(
        f"/lecteurs/{world['minor']}/responsables", data={"resp_id": str(world["withdrawn"]), "relation": "parent"}
    )
    assert response.status_code == 400


def test_export_of_a_minor_hides_an_archived_guardian(settings, client_as, world):
    browser = _custom(settings, client_as, "sans_archive", ALL_BUT_ARCHIVE)
    data = browser.get(f"/lecteurs/{world['minor']}/export.json").json()
    assert [item["nom"] for item in data["responsables"]] == ["Paul Vivant"]
    assert "Archivée" not in json.dumps(data, ensure_ascii=False)


def test_contacts_of_a_minor_skip_an_archived_guardian(settings, client_as, world):
    browser = _custom(settings, client_as, "sans_archive", ALL_BUT_ARCHIVE)
    assert "blanche@example.org" not in browser.post(f"/lecteurs/{world['minor']}/coordonnees/afficher").text


# --- Recherche en POST ------------------------------------------------------------------------------------------------
def test_a_term_in_the_address_is_ignored(client_as, world):
    page = client_as("librarian").get("/lecteurs", params={"q": "Vivant"}).text
    assert 'name="q" value=""' in page or 'value="Vivant"' not in page
    assert _ids(page) == {world["active"], world["minor"]}  # la liste entière : le paramètre ne filtre rien


@pytest.mark.parametrize("path", ["/lecteurs/recherche", "/lecteurs/choix", "/foyers/choix", "/ui/recherche/lecteurs"])
def test_searches_cannot_be_made_by_get(client_as, world, path):
    assert client_as("admin").get(path, params={"q": "Vivant", "choix_q": "Vivant"}).status_code == 405


def test_a_search_requires_the_anti_forgery_token(client_as, world):
    browser = client_as("librarian")
    del browser.headers["X-CSRF-Token"]
    assert browser.post("/lecteurs/recherche", data={"q": "Vivant"}).status_code in (400, 403)


def test_the_term_appears_nowhere_but_the_field(client_as, world):
    response = client_as("librarian").post(
        "/lecteurs/recherche", data={"q": "Vivant", "vue": "tous"}, headers={"HX-Request": "true"}
    )
    assert response.headers["HX-Replace-Url"] == "/lecteurs"
    assert not re.findall(r'(?:href|action|hx-post|hx-get)="[^"]*Vivant[^"]*"', response.text)
    assert "q=Vivant" not in response.text


def test_the_form_posts_and_the_picker_only_sends_its_own_text(client_as, world):
    page = client_as("librarian").get("/lecteurs").text
    assert 'hx-post="/lecteurs/recherche"' in page and 'method="post"' in page
    form = client_as("librarian", "b2").get("/lecteurs/nouveau").text
    assert 'hx-params="resp_id_texte"' in form and 'hx-post="/lecteurs/choix' in form
    assert 'hx-params="foyer_id_texte"' in form  # deux sélecteurs sur la page : chacun n'envoie que le sien


def test_search_keeps_view_and_size(client_as, world):
    response = client_as("librarian").post(
        "/lecteurs/recherche", data={"q": "Vivant", "vue": "mineurs", "par_page": "50"}, headers={"HX-Request": "true"}
    )
    assert _ids(response.text) == {world["minor"]}
    assert response.headers["HX-Replace-Url"] == "/lecteurs?vue=mineurs&par_page=50"


def test_browsing_without_a_term_keeps_a_shareable_address(client_as, world):
    response = client_as("librarian").post(
        "/lecteurs/recherche", data={"q": "", "vue": "adultes"}, headers={"HX-Request": "true"}
    )
    assert response.headers["HX-Push-Url"] == "/lecteurs?vue=adultes"


def test_a_search_without_javascript_works(client_as, world):
    response = client_as("librarian").post("/lecteurs/recherche", data={"q": "Tom"})
    assert response.status_code == 200 and "<html" in response.text and _ids(response.text) == {world["minor"]}


def test_a_very_long_term_is_cut_and_harmless(client_as, world):
    assert client_as("librarian").post("/lecteurs/recherche", data={"q": "x" * 100000}).status_code == 200


# --- Pagination -------------------------------------------------------------------------------------------------------
@pytest.fixture
def crowd(settings):
    """250 lecteurs et 130 foyers, insérés directement : la volumétrie est l'objet du test, pas l'inscription."""
    moment = "2026-01-01T00:00:00Z"
    connection = sqlite3.connect(settings.database_path)
    connection.executemany(
        "INSERT INTO patrons (public_id, number, first_name, last_name, name_norm, sort_key, category, status,"
        " registered_on, version, created_at, updated_at)"
        " VALUES (?, ?, 'Foule', ?, ?, ?, 'adult', 'active', ?, 1, ?, ?)",
        [
            (
                f"p{i}",
                f"P-9{i:05d}",
                f"Nom{i:03d}",
                f"foule nom{i:03d}",
                f"nom{i:03d} foule",
                moment[:10],
                moment,
                moment,
            )
            for i in range(250)
        ],
    )
    connection.executemany(
        "INSERT INTO households (public_id, kind, name, name_norm, version, created_at, updated_at)"
        " VALUES (?, 'family', ?, ?, 1, ?, ?)",
        [(f"h{i}", f"Foyer {i:03d}", f"foyer {i:03d}", moment, moment) for i in range(130)],
    )
    connection.commit()
    connection.close()


def _count(page: str) -> int:
    return len(_ids(page))


def test_default_is_twenty_five(client_as, crowd):
    assert _count(client_as("volunteer").get("/lecteurs").text) == 25


@pytest.mark.parametrize(
    ("asked", "expected"),
    [
        ("25", 25),
        ("50", 50),
        ("100", 100),
        ("101", 100),
        ("100000", 100),
        ("0", 25),
        ("-5", 25),
        ("abc", 25),
        ("1e9", 25),
        ("", 25),
        ("7", 7),
    ],
)
def test_page_size_limits(client_as, crowd, asked, expected):
    browser = client_as("volunteer")
    assert _count(browser.get("/lecteurs", params={"par_page": asked}).text) == expected
    assert _count(browser.post("/lecteurs/recherche", data={"q": "foule", "par_page": asked}).text) == expected


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ("1", 100),
        ("2", 100),
        ("3", 50),
        ("4", 50),
        ("999999", 50),
        ("0", 100),
        ("-1", 100),
        ("abc", 100),
        ("9" * 30, 100),
    ],
)
def test_pages_out_of_bounds(client_as, crowd, page, expected):
    browser = client_as("volunteer")
    assert _count(browser.get("/lecteurs", params={"par_page": "100", "page": page}).text) == expected
    found = browser.post("/lecteurs/recherche", data={"q": "foule", "par_page": "100", "page": page})
    assert found.status_code == 200 and _count(found.text) == expected


def test_the_last_page_is_reported_when_asked_beyond(client_as, crowd):
    page = client_as("volunteer").get("/lecteurs", params={"page": "999999"}).text
    assert re.search(r'<span aria-current="page">10</span>', page)


def test_no_page_repeats_a_reader(client_as, crowd):
    browser = client_as("volunteer")
    seen: list[int] = []
    for number in (1, 2, 3):
        seen += sorted(_ids(browser.get("/lecteurs", params={"par_page": "100", "page": str(number)}).text))
    assert len(seen) == len(set(seen)) == 250


def test_households_are_limited_the_same_way(client_as, crowd):
    browser = client_as("volunteer")
    pattern = r'href="/foyers/\d+"'
    assert len(re.findall(pattern, browser.get("/lecteurs", params={"vue": "foyers"}).text)) == 25
    assert len(re.findall(pattern, browser.get("/lecteurs", params={"vue": "foyers", "par_page": "500"}).text)) == 100


def test_pagination_buttons_do_not_carry_the_term(client_as, crowd):
    page = client_as("volunteer").post("/lecteurs/recherche", data={"q": "foule"}).text
    assert 'name="page" value="2"' in page and "q=foule" not in page
