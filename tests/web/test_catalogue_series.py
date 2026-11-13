# SPDX-License-Identifier: EUPL-1.2
"""Pages Séries et Contributeurs : rôles, CSRF, confirmation des tomes, homonymes."""

from __future__ import annotations

import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service

from ..factories import make_notice

ROLES = ("admin", "librarian", "volunteer", "viewer")
EDITORS = ("admin", "librarian")


def _series_id(settings, title):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute("SELECT id FROM series WHERE title = ?", (title,)).fetchone()[0]
    finally:
        connection.close()


def _count(settings, table):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    finally:
        connection.close()


@pytest.mark.parametrize("role", ROLES)
def test_reading_is_open_to_every_role(client_as, settings, role):
    make_notice(settings, "Tome 1", series="Astérix", volume="1", authors=("René Goscinny",))
    browser = client_as(role)
    series = _series_id(settings, "Astérix")
    for path in ("/catalogue/series", f"/catalogue/series/{series}", "/catalogue/contributeurs"):
        assert browser.get(path).status_code == 200, path


def test_reading_requires_a_session(client):
    for path in ("/catalogue/series", "/catalogue/contributeurs"):
        assert client.get(path).status_code in (303, 401)


@pytest.mark.parametrize("role", ROLES)
def test_writes_need_catalogue_edit(client_as, settings, role):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    series = _series_id(settings, "Astérix")
    browser = client_as(role)
    routes = [
        ("/catalogue/series", {"titre": "Neuve"}),
        (f"/catalogue/series/{series}", {"titre": "Astérix", "etat": "ongoing"}),
        (f"/catalogue/series/{series}/tomes-confirmes", {"premier": "2"}),
        (f"/catalogue/series/{series}/tomes-confirmes/retirer", {"numero": "2"}),
    ]
    for path, data in routes:
        status = browser.post(path, data=data).status_code
        if role in EDITORS:
            assert status == 303, (role, path, status)
        else:
            assert status == 403, (role, path, status)
    deleting = browser.post(f"/catalogue/series/{series}/supprimer")
    assert deleting.status_code == (400 if role in EDITORS else 403)


def test_series_forms_need_a_csrf_token(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    series = _series_id(settings, "Astérix")
    browser = client_as("admin")
    browser.headers.pop("X-CSRF-Token", None)
    assert browser.post(f"/catalogue/series/{series}/tomes-confirmes", data={"premier": "2"}).status_code == 403
    assert _count(settings, "series_expected_volumes") == 0


def test_forms_are_hidden_from_readers(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    series = _series_id(settings, "Astérix")
    page = client_as("viewer").get(f"/catalogue/series/{series}")
    assert "Confirmer des tomes parus" not in page.text and "Modifier la série" not in page.text


def test_confirmation_shows_up_with_three_states(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    make_notice(settings, "Tome 4", series="Astérix", volume="4")
    series = _series_id(settings, "Astérix")
    browser = client_as("admin")
    page = browser.get(f"/catalogue/series/{series}")
    assert "tome gap" in page.text and "tome miss" not in page.text
    answer = browser.post(f"/catalogue/series/{series}/tomes-confirmes", data={"premier": "2", "note": "Éditeur"})
    assert answer.status_code == 303
    page = browser.get(f"/catalogue/series/{series}")
    assert "tome miss" in page.text and "tome gap" in page.text
    assert "Éditeur" in page.text and "Tome manquant confirmé" in page.text
    browser.post(f"/catalogue/series/{series}/tomes-confirmes/retirer", data={"numero": "2"})
    assert _count(settings, "series_expected_volumes") == 0


def test_invalid_confirmation_is_explained(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    series = _series_id(settings, "Astérix")
    page = client_as("admin").post(f"/catalogue/series/{series}/tomes-confirmes", data={"premier": "3", "dernier": "1"})
    assert page.status_code == 400 and "doit suivre" in page.text


def test_create_series_and_refuse_a_duplicate(client_as, settings):
    browser = client_as("admin")
    assert browser.post("/catalogue/series", data={"titre": "Les Rives"}).status_code == 303
    page = browser.post("/catalogue/series", data={"titre": "les rives"})
    assert page.status_code == 400 and "déjà ce titre" in page.text
    assert _count(settings, "series") == 1


def test_update_series(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    series = _series_id(settings, "Astérix")
    browser = client_as("admin")
    response = browser.post(
        f"/catalogue/series/{series}", data={"titre": "Astérix le Gaulois", "etat": "complete", "notes": "Note"}
    )
    assert response.status_code == 303
    page = browser.get(f"/catalogue/series/{series}")
    assert "Astérix le Gaulois" in page.text and "Terminée" in page.text and "Note" in page.text


def test_empty_series_can_be_deleted_but_a_used_one_cannot(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    series = _series_id(settings, "Astérix")
    browser = client_as("admin")
    assert "Supprimer la série" not in browser.get(f"/catalogue/series/{series}").text
    refused = browser.post(f"/catalogue/series/{series}/supprimer")
    assert refused.status_code == 400 and "ne peut pas être supprimée" in refused.text
    browser.post("/catalogue/series", data={"titre": "Vide"})
    empty = _series_id(settings, "Vide")
    assert "Supprimer la série" in browser.get(f"/catalogue/series/{empty}").text
    assert browser.post(f"/catalogue/series/{empty}/supprimer").status_code == 303


def test_unknown_series_is_404(client_as):
    browser = client_as("admin")
    assert browser.get("/catalogue/series/999").status_code == 404
    assert browser.post("/catalogue/series/999/tomes-confirmes", data={"premier": "1"}).status_code == 404


def test_list_filters_and_search_stay_in_the_address(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    page = client_as("admin").get("/catalogue/series?filtre=completes&q=ast")
    assert 'href="/catalogue/series?filtre=completes&amp;q=ast"' in page.text
    assert "Astérix" in page.text
    assert client_as("admin").get("/catalogue/series?filtre=inconnu&page=abc").status_code == 200


def test_strip_is_described_without_color_alone(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    make_notice(settings, "Tome 3", series="Astérix", volume="3")
    page = client_as("admin").get("/catalogue/series")
    assert "Tome 1 : présent" in page.text
    assert "Tome 2 : à vérifier" in page.text
    assert "Présent" in page.text and "Manquant confirmé" in page.text and "À vérifier" in page.text


@pytest.mark.parametrize("path", ["/catalogue/series", "/catalogue/series/{id}"])
def test_a_full_percentage_with_uncertain_numbers_is_qualified_on_screen(client_as, settings, path):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    make_notice(settings, "Tome 3", series="Astérix", volume="3")
    page = client_as("viewer").get(path.format(id=_series_id(settings, "Astérix")))
    assert "100 % des tomes confirmés · 1 numéro à vérifier, non compté" in page.text
    assert "pct-ring uncertain" in page.text and "pct-ring m-ok" not in page.text
    assert "100%<small>?</small>" in page.text
    assert "Complète" not in page.text.replace("Complètes", "")  # ni badge, ni libellé de série complète
    assert "tome gap" in page.text


def test_a_complete_series_is_not_qualified(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    page = client_as("viewer").get("/catalogue/series")
    assert "100 % des tomes attendus" in page.text and "à vérifier, non" not in page.text
    assert "pct-ring m-ok" in page.text and "pct-ring m-ok uncertain" not in page.text
    assert "Complète jusqu’au t. 1" in page.text


def test_a_series_with_confirmed_gaps_only_is_blue(client_as, settings):
    make_notice(settings, "Tome 1", series="Astérix", volume="1")
    series = _series_id(settings, "Astérix")
    client_as("admin").post(f"/catalogue/series/{series}/tomes-confirmes", data={"premier": "2"})
    page = client_as("viewer").get("/catalogue/series")
    assert "pct-ring m-ser" in page.text and "uncertain" not in page.text
    assert "1 manquant confirmé" in page.text and "Complète jusqu" not in page.text


def test_the_complete_filter_leaves_uncertain_series_out(client_as, settings):
    make_notice(settings, "Tome 1", series="Sûre", volume="1")
    make_notice(settings, "Tome 2", series="Sûre", volume="2")
    make_notice(settings, "Tome 1", series="Douteuse", volume="1")
    make_notice(settings, "Tome 3", series="Douteuse", volume="3")
    page = client_as("viewer").get("/catalogue/series?filtre=completes")
    assert "Sûre" in page.text and "Douteuse" not in page.text
    page = client_as("viewer").get("/catalogue/series?filtre=a-verifier")
    assert "Douteuse" in page.text and "Sûre" not in page.text


def test_editions_and_copies_keep_a_complete_series_complete(client_as, settings):
    database = Database(settings.database_path)
    work = make_notice(settings, "Tome 1", series="Astérix", volume="1")
    make_notice(settings, "Tome 2", series="Astérix", volume="2")
    service.add_edition(database, work, service.EditionInput(publisher="Autre"), None)
    connection = sqlite3.connect(settings.database_path)
    edition = connection.execute("SELECT id FROM editions WHERE work_id = ? AND is_primary = 1", (work,)).fetchone()[0]
    connection.close()
    service.add_copies(database, edition, 3, "", None)
    database.dispose()
    page = client_as("viewer").get("/catalogue/series")
    assert "Complète jusqu’au t. 2" in page.text and "Doublons ou éditions" in page.text
    assert 'data-plus="3"' in page.text
    assert "Astérix" in client_as("viewer").get("/catalogue/series?filtre=doublons").text


def _agent_ids(settings, name):
    connection = sqlite3.connect(settings.database_path)
    try:
        return [row[0] for row in connection.execute("SELECT id FROM agents WHERE display_name = ?", (name,))]
    finally:
        connection.close()


def test_homonyms_are_separate_pages_that_point_to_each_other(client_as, settings):
    first = make_notice(settings, "Premier livre", authors=("Jean Dupont",))
    database = Database(settings.database_path)
    second = service.create_notice(
        database,
        service.IdentityInput("Second livre", 1, contributors=(service.ContributorInput("Jean Dupont", choice="new"),)),
        service.SeriesInput(),
        service.ContentInput(),
        service.EditionInput(),
        None,
    )
    database.dispose()
    a, b = _agent_ids(settings, "Jean Dupont")
    browser = client_as("viewer")
    page_a = browser.get(f"/catalogue/contributeurs/{a}")
    page_b = browser.get(f"/catalogue/contributeurs/{b}")
    assert f"/catalogue/notices/{first}" in page_a.text and f"/catalogue/notices/{second}" not in page_a.text
    assert f"/catalogue/notices/{second}" in page_b.text
    assert f"/catalogue/contributeurs/{b}" in page_a.text
    assert f"/catalogue/contributeurs/{a}" in page_b.text


def test_contributor_list_and_search(client_as, settings):
    make_notice(settings, "Un", authors=("René Goscinny",))
    make_notice(settings, "Deux", authors=("Albert Uderzo",))
    page = client_as("viewer").get("/catalogue/contributeurs?q=rene")
    assert "René Goscinny" in page.text and "Albert Uderzo" not in page.text


def test_contributor_routes_do_not_shadow_form_helpers(client_as):
    browser = client_as("admin")
    assert browser.get("/catalogue/contributeurs/ligne?prefixe=contributeur").status_code == 200
    assert browser.get("/catalogue/contributeurs/choix?prefixe=contributeur&contributeur_nom=x").status_code == 200


def test_unknown_contributor_is_404(client_as):
    assert client_as("viewer").get("/catalogue/contributeurs/999").status_code == 404


def test_hero_links_to_the_contributor(client_as, settings):
    work = make_notice(settings, "Un", authors=("René Goscinny",))
    agent = _agent_ids(settings, "René Goscinny")[0]
    assert f"/catalogue/contributeurs/{agent}" in client_as("viewer").get(f"/catalogue/notices/{work}").text
