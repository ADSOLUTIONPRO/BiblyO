# SPDX-License-Identifier: EUPL-1.2
"""Page Catalogue : grille, liste, adresse rejouable, sans JavaScript, recherches enregistrées, palette."""

from __future__ import annotations

import re
import sqlite3

import pytest

from ..factories import make_notice, make_user

ROLES = ("admin", "librarian", "volunteer", "viewer")


@pytest.mark.parametrize("role", ROLES)
def test_every_role_with_view_can_list(client_as, settings, role):
    make_notice(settings, "Lisible")
    page = client_as(role).get("/catalogue")
    assert page.status_code == 200 and "Lisible" in page.text


def test_list_requires_login(client):
    assert client.get("/catalogue").status_code in (303, 401)
    assert client.post("/ui/recherche", data={"q": "a"}).status_code in (303, 401, 403)


def test_grid_is_the_default_and_list_is_in_the_address(client_as, settings):
    make_notice(settings, "Une notice", isbn="9782070408504")
    browser = client_as("admin")
    grid = browser.get("/catalogue")
    assert 'class="cgrid"' in grid.text and "<table" not in grid.text
    listed = browser.get("/catalogue?view=list")
    assert "<table" in listed.text and "9782070408504" in listed.text
    assert re.search(r'value="list"[^>]*aria-pressed="true"', listed.text)


def test_results_are_links_and_work_without_javascript(client_as, settings):
    work = make_notice(settings, "Une notice", categories="Roman")
    page = client_as("admin").get("/catalogue?cat=Roman&q=notice")
    assert f'href="/catalogue/notices/{work}"' in page.text
    assert 'method="get"' in page.text and 'action="/catalogue"' in page.text
    assert "<noscript>" in page.text


def test_page_links_keep_the_criteria(client_as, settings):
    for index in range(50):
        make_notice(settings, f"Titre {index:02d}", categories="Roman")
    page = client_as("admin").get("/catalogue?cat=roman&view=list")
    assert 'href="/catalogue?view=list&amp;cat=roman&amp;page=2"' in page.text
    assert 'aria-current="page">1<' in page.text


def test_filter_state_is_rendered_back(client_as, settings):
    make_notice(settings, "Une notice", document_type="bd", categories="Roman")
    page = client_as("admin").get("/catalogue?type=bd&cat=roman&q=une&tri=recents")
    assert '<option value="bd" selected>' in page.text
    assert '<option value="roman" selected>' in page.text
    assert 'value="une"' in page.text
    assert '<option value="recents" selected>' in page.text


def test_htmx_response_pushes_the_clean_address(client_as, settings):
    make_notice(settings, "Une notice")
    page = client_as("admin").get("/catalogue?q=une&type=&cat=&view=grid", headers={"HX-Request": "true"})
    assert page.headers["HX-Push-Url"] == "/catalogue?q=une"
    assert "HX-Push-Url" not in client_as("admin").get("/catalogue?q=une").headers


def test_no_result_and_empty_catalogue_are_told_apart(client_as, settings):
    browser = client_as("admin")
    assert "Le catalogue est vide" in browser.get("/catalogue").text
    make_notice(settings, "Une notice")
    assert "Aucun résultat" in browser.get("/catalogue?q=zzzz").text


def test_hostile_query_is_escaped_in_the_page(client_as, settings):
    make_notice(settings, "Une notice")
    page = client_as("admin").get("/catalogue", params={"q": '"><script>alert(1)</script>'})
    assert page.status_code == 200
    assert "<script>alert(1)" not in page.text


def test_homonym_filter_shows_a_removable_chip(client_as, settings):
    make_notice(settings, "Une notice", authors=("Jean Dupont",))
    connection = sqlite3.connect(settings.database_path)
    agent = connection.execute("SELECT id FROM agents").fetchone()[0]
    connection.close()
    page = client_as("admin").get(f"/catalogue?contributeur={agent}")
    assert "Jean Dupont" in page.text
    assert f'name="contributeur" value="{agent}"' in page.text
    assert 'href="/catalogue"' in page.text


# --- Recherches enregistrées ------------------------------------------------------------------------------------------
def _saved(settings):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute("SELECT id, user_id, name, criteria FROM saved_searches ORDER BY id").fetchall()
    finally:
        connection.close()


def test_saved_search_round_trip(client_as, settings):
    browser = client_as("volunteer")
    response = browser.post("/catalogue/recherches", data={"nom": "Romans", "criteres": "cat=roman&view=list&page=4"})
    assert response.status_code == 303
    assert response.headers["location"] == "/catalogue?view=list&cat=roman"
    rows = _saved(settings)
    assert len(rows) == 1 and rows[0][2] == "Romans" and "page" not in rows[0][3]
    page = browser.get("/catalogue")
    assert "Romans" in page.text
    assert re.search(r'href="/catalogue\?[^"]*cat=roman', page.text)


def test_saving_again_updates(client_as, settings):
    browser = client_as("volunteer")
    browser.post("/catalogue/recherches", data={"nom": "Romans", "criteres": "cat=roman"})
    browser.post("/catalogue/recherches", data={"nom": "Romans", "criteres": "cat=essai"})
    rows = _saved(settings)
    assert len(rows) == 1 and "essai" in rows[0][3]


def test_invalid_saved_search_name_is_explained(client_as):
    page = client_as("volunteer").post("/catalogue/recherches", data={"nom": "  ", "criteres": "q=a"})
    assert page.status_code == 400 and "Indiquez un nom" in page.text


def test_stored_criteria_are_filtered_on_the_way_in(client_as, settings):
    client_as("volunteer").post(
        "/catalogue/recherches", data={"nom": "x", "criteres": "tri=hack&evil=1&annee_min=abcd&q=ok"}
    )
    assert _saved(settings)[0][3] == '{"q": "ok"}'


def test_saved_searches_are_isolated_between_accounts(client_as, settings, client):
    client_as("volunteer", username="anne").post("/catalogue/recherches", data={"nom": "Privée", "criteres": "q=a"})
    row = _saved(settings)[0]
    other = client_as("volunteer", username="paul")
    assert "Privée" not in other.get("/catalogue").text
    assert other.post(f"/catalogue/recherches/{row[0]}/supprimer").status_code == 404
    assert len(_saved(settings)) == 1


def test_owner_deletes_a_saved_search(client_as, settings):
    browser = client_as("volunteer")
    browser.post("/catalogue/recherches", data={"nom": "À retirer", "criteres": "q=a"})
    assert browser.post(f"/catalogue/recherches/{_saved(settings)[0][0]}/supprimer").status_code == 303
    assert _saved(settings) == []


def test_saved_search_routes_require_csrf(client_as):
    browser = client_as("volunteer")
    browser.headers.pop("X-CSRF-Token", None)
    assert browser.post("/catalogue/recherches", data={"nom": "x", "criteres": ""}).status_code == 403


def test_deleting_the_account_deletes_its_searches(client_as, settings):
    user_id = make_user(settings, "ephemere", "volunteer")
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "INSERT INTO saved_searches (user_id, name, criteria, created_at) VALUES (?, 'x', '{}', 'now')", (user_id,)
    )
    connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    connection.commit()
    assert connection.execute("SELECT COUNT(*) FROM saved_searches").fetchone()[0] == 0
    connection.close()


# --- Palette ⌘K -------------------------------------------------------------------------------------------------------
def test_palette_finds_notices_with_no_layout(client_as, settings):
    work = make_notice(settings, "Le Cœur des œuvres", series="Astérix", volume="2")
    page = client_as("viewer").post("/ui/recherche", data={"q": "coeur"})
    assert page.status_code == 200
    assert f'href="/catalogue/notices/{work}"' in page.text and "data-server" in page.text
    assert "<html" not in page.text


def test_palette_is_empty_without_a_real_query(client_as):
    page = client_as("viewer").post("/ui/recherche", data={"q": "***"})
    assert page.status_code == 200 and "data-server" not in page.text


def test_palette_hides_archived_notices(client_as, settings):
    from biblyo.core.db import Database
    from biblyo.modules.catalogue import service

    work = make_notice(settings, "Retirée")
    database = Database(settings.database_path)
    service.archive_work(database, work, None)
    database.dispose()
    assert "data-server" not in client_as("viewer").post("/ui/recherche", data={"q": "retiree"}).text


def test_palette_input_only_searches_for_those_allowed(client_as):
    assert 'hx-post="/ui/recherche"' in client_as("viewer").get("/").text
