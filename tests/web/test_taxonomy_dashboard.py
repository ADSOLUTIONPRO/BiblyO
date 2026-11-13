# SPDX-License-Identifier: EUPL-1.2
"""Tableau de bord réel et pages des listes du catalogue : chiffres exacts, liens préfiltrés, droits, CSRF."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service, taxonomy

from ..factories import make_notice

ROLES = ("admin", "librarian", "volunteer", "viewer")
EDITORS = ("admin", "librarian")


def _edition(settings, work):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute("SELECT id FROM editions WHERE work_id = ?", (work,)).fetchone()[0]
    finally:
        connection.close()


def _kpi(html, label):
    return re.search(rf'</i>{label}</span>\s*<span class="v">([^<]+)', html).group(1).strip()


def _populate(settings):
    database = Database(settings.database_path)
    full = make_notice(
        settings,
        "Complète",
        authors=("René Goscinny",),
        series="Astérix",
        volume="1",
        isbn="9782070408504",
        publisher="Dargaud",
        collection="Pilote",
        year="1961",
        summary="Résumé",
        categories="BD",
        pages="48",
    )
    make_notice(settings, "Nue")
    second = make_notice(settings, "Autre", series="Astérix", volume="3", summary="Autre résumé")
    service.add_copies(database, _edition(settings, full), 2, "", None)
    service.add_copies(database, _edition(settings, second), 1, "", None)
    archived = make_notice(settings, "Archivée")
    service.archive_work(database, archived, None)
    series = service.create_series(database, service.SeriesEdit("Vide"), None)
    service.confirm_volumes(database, series, "1", "", "", None)
    database.dispose()


def test_dashboard_figures_are_exact(client_as, settings):
    _populate(settings)
    html = client_as("viewer").get("/").text
    assert _kpi(html, "Titres") == "3"
    assert _kpi(html, "Exemplaires") == "3"
    assert _kpi(html, "Séries") == "2"
    assert _kpi(html, "Contributeurs") == "1"
    assert "Votre bibliothèque est prête" not in html


def test_dashboard_quality_lines_link_to_prefiltered_lists(client_as, settings):
    _populate(settings)
    html = client_as("viewer").get("/").text
    for href in (
        "/catalogue?meta=sans-isbn&amp;view=list",
        "/catalogue?meta=incomplet&amp;view=list",
        "/catalogue?meta=sans-couverture",
        "/catalogue?meta=sans-resume&amp;view=list",
        "/catalogue/series?filtre=incompletes",
    ):
        assert f'href="{href}"' in html, href


def test_quality_counts_match_the_linked_lists(client_as, settings):
    _populate(settings)
    browser = client_as("viewer")
    html = browser.get("/").text
    for label, target in (
        ("Sans ISBN", "/catalogue?meta=sans-isbn"),
        ("Sans résumé", "/catalogue?meta=sans-resume"),
        ("Sans couverture", "/catalogue?meta=sans-couverture"),
    ):
        number = int(re.search(rf'>\s*(\d+)\s*</span>\s*<span class="grow b">{label}', html).group(1))
        listed = browser.get(target).text
        assert f"{number} notice" in listed, (label, number)


def test_series_tile_counts_the_incomplete_ones(client_as, settings):
    _populate(settings)
    assert "1 incomplète" in client_as("viewer").get("/").text


def test_recent_entries_link_to_notices(client_as, settings):
    work = make_notice(settings, "Tout neuf")
    html = client_as("viewer").get("/").text
    assert "Dernières entrées au catalogue" in html and f"/catalogue/notices/{work}" in html
    assert 'href="/catalogue?tri=recents"' in html


def test_navigation_counter_is_real(client_as, settings):
    make_notice(settings, "Un")
    make_notice(settings, "Deux")
    assert '<span class="ct">2</span>' in client_as("viewer").get("/").text


def test_empty_catalogue_keeps_the_welcome(client_as):
    html = client_as("viewer").get("/").text
    assert "Votre bibliothèque est prête" in html and "Dernières entrées" not in html


# --- Listes -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("code", taxonomy.KINDS)
def test_taxonomy_pages_need_catalogue_edit(client_as, role, code):
    response = client_as(role).get(f"/admin/catalogue/{code}")
    assert response.status_code == (200 if role in EDITORS else 403)


def test_taxonomy_index_redirects(client_as):
    response = client_as("admin").get("/admin/catalogue")
    assert response.status_code == 303 and response.headers["location"] == "/admin/catalogue/categories"


def test_taxonomy_requires_a_session_and_a_known_kind(client, client_as):
    assert client.get("/admin/catalogue/categories").status_code in (303, 401)
    assert client_as("admin").get("/admin/catalogue/inconnu").status_code == 404
    assert client_as("admin").post("/admin/catalogue/inconnu", data={"action": "creer"}).status_code == 404


def test_taxonomy_write_requires_permission_and_csrf(client_as, settings):
    make_notice(settings, "Un", categories="BD, Roman")
    connection = sqlite3.connect(settings.database_path)
    tag = connection.execute("SELECT id FROM tags WHERE label = 'BD'").fetchone()[0]
    connection.close()
    data = {"action": "renommer", "element": str(tag), "libelle": "Nouveau"}
    assert client_as("volunteer").post("/admin/catalogue/categories", data=data).status_code == 403
    browser = client_as("admin")
    browser.headers.pop("X-CSRF-Token", None)
    assert browser.post("/admin/catalogue/categories", data=data).status_code == 403


def test_create_rename_merge_through_the_page(client_as, settings):
    make_notice(settings, "Un", categories="BD")
    make_notice(settings, "Deux", categories="Bande dessinée")
    connection = sqlite3.connect(settings.database_path)
    source = connection.execute("SELECT id FROM tags WHERE label = 'BD'").fetchone()[0]
    target = connection.execute("SELECT id FROM tags WHERE label = 'Bande dessinée'").fetchone()[0]
    connection.close()
    browser = client_as("librarian")
    page = browser.get("/admin/catalogue/categories")
    assert "BD" in page.text and "Fusionner dans" in page.text
    response = browser.post(
        "/admin/catalogue/categories", data={"action": "fusionner", "element": str(source), "cible": str(target)}
    )
    assert response.status_code == 303
    created = browser.post("/admin/catalogue/sections", data={"action": "creer", "libelle": "Jeunesse"})
    assert created.status_code == 303
    assert "Jeunesse" in browser.get("/admin/catalogue/sections").text


def test_errors_are_explained_on_the_page(client_as, settings):
    make_notice(settings, "Un", categories="BD, Roman")
    connection = sqlite3.connect(settings.database_path)
    tag = connection.execute("SELECT id FROM tags WHERE label = 'BD'").fetchone()[0]
    connection.close()
    page = client_as("admin").post(
        "/admin/catalogue/categories", data={"action": "renommer", "element": str(tag), "libelle": "roman"}
    )
    assert page.status_code == 400 and "fusionnez" in page.text


def test_unknown_action_is_404(client_as):
    assert client_as("admin").post("/admin/catalogue/categories", data={"action": "pirater"}).status_code == 404


def test_section_parent_field_only_for_sections(client_as):
    response = client_as("admin").post(
        "/admin/catalogue/categories", data={"action": "parent", "element": "1", "cible": "2"}
    )
    assert response.status_code == 404
