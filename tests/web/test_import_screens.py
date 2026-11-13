# SPDX-License-Identifier: EUPL-1.2
"""Écrans de l'import (lot L6, T3 et T4) : dépôt, correspondance, simulation, rapports, permissions.

La règle centrale est vérifiée ici : déposer un fichier, régler sa correspondance et le simuler n'écrivent rien
dans la base. Seul l'enregistrement d'un profil le fait, et il est journalisé."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.modules.imports import store

from .. import import_samples as samples

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
WATCHED = (
    "works", "editions", "copies", "series", "agents", "publishers", "collections", "work_contributors",
    "edition_identifiers", "edition_isbn_candidates", "import_profiles", "import_batches", "import_rows",
    "import_links", "import_proposals", "audit_log",
)  # fmt: skip


def _counts(settings):
    connection = sqlite3.connect(settings.database_path)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 liste fixe
            for table in WATCHED
        }
    finally:
        connection.close()


def _deposit(browser, *, enriched=True):
    files = {"principal": ("catalogue.xlsx", samples.workbook(), XLSX)}
    if enriched:
        files["complementaire"] = ("enrichi.xlsx", samples.workbook(enriched=True), XLSX)
    response = browser.post("/imports", files=files, data={"profil": ""})
    assert response.status_code == 303, response.text
    token = re.fullmatch(r"/imports/([0-9a-f]{32})", response.headers["location"]).group(1)
    return token


def _mapping_form(page: str) -> dict[str, str]:
    """Les choix de l'écran, tels qu'un navigateur les enverrait."""
    form: dict[str, str] = {}
    for name, body in re.findall(r'<select class="in[^"]*" id="[^"]+" name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        chosen = re.search(r'<option value="([^"]*)" selected', body) or re.search(r'<option value="([^"]*)"', body)
        form[name.replace("&#39;", "'").replace("&amp;", "&")] = chosen.group(1)
    return form


def test_only_accounts_allowed_to_simulate_see_the_screens(client, client_as):
    assert client.get("/imports").status_code in (302, 303)
    assert client_as("volunteer").get("/imports").status_code == 403
    assert client_as("viewer").get("/imports").status_code == 403
    page = client_as("librarian").get("/imports")
    assert page.status_code == 200 and "Simuler un import" in page.text
    assert (
        client_as("volunteer").post("/imports", files={"principal": ("c.xlsx", samples.workbook(), XLSX)}).status_code
        == 403
    )


def test_deposit_mapping_and_simulation_write_nothing_in_the_database(client_as, settings):
    browser = client_as("librarian")
    before = _counts(settings)
    token = _deposit(browser)
    page = browser.get(f"/imports/{token}")
    assert page.status_code == 200
    for header in (*samples.HEADERS, *samples.EXTRA):
        assert header.replace("'", "&#39;") in page.text
    assert "Arbitré" in page.text and "D-L6-3" in page.text
    form = _mapping_form(page.text)
    assert (
        form["col::Numéro d'inventaire"] == "inventory" and form["col::ISBN (référence à vérifier)"] == "isbn_proposed"
    )
    assert browser.post(f"/imports/{token}", data={**form, "action": "enregistrer"}).status_code == 303
    response = browser.post(f"/imports/{token}", data={**form, "action": "simuler"})
    assert response.headers["location"] == f"/imports/{token}/simulation"
    for view in ("lignes", "anomalies", "livres", "series", "cotes", "propositions", "differences", "colonnes", "noms"):
        result = browser.get(f"/imports/{token}/simulation?vue={view}")
        assert result.status_code == 200, (view, result.text[:400])
    summary = browser.get(f"/imports/{token}/simulation").text
    assert "Rien n’a été écrit" in summary and "conforme" in summary
    filtered = browser.get(f"/imports/{token}/simulation?vue=lignes&code=date_format_cell").text
    assert "I14" in filtered and "1 ligne(s)" in filtered
    xlsx = browser.get(f"/imports/{token}/rapport.xlsx")
    assert xlsx.status_code == 200 and xlsx.headers["content-type"] == XLSX
    assert xlsx.headers["cache-control"] == "no-store"
    csv = browser.get(f"/imports/{token}/lignes.csv")
    assert csv.status_code == 200 and csv.content.startswith("﻿".encode())
    assert _counts(settings) == before


def test_a_deposit_is_private_and_expires(client_as, settings):
    owner, other = client_as("librarian"), client_as("librarian")
    token = _deposit(owner, enriched=False)
    assert other.get(f"/imports/{token}").status_code == 404
    assert other.get(f"/imports/{token}/simulation").status_code == 404
    assert other.get(f"/imports/{token}/rapport.xlsx").status_code == 404
    folder = settings.data_dir / store.STAGING_DIR / token
    assert folder.is_dir()
    deposit = store.load(settings.data_dir, token, owner.user_id)
    assert store.expire(settings.data_dir, now=deposit.created + store.LIFETIME_S + 1) == 1
    assert not folder.exists() and owner.get(f"/imports/{token}").status_code == 404


def test_abandoning_a_deposit_deletes_its_files(client_as, settings):
    browser = client_as("librarian")
    token = _deposit(browser)
    response = browser.post(f"/imports/{token}", data={"action": "abandonner"})
    assert response.status_code == 303 and response.headers["location"] == "/imports"
    assert not (settings.data_dir / store.STAGING_DIR / token).exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"col::Cote": "inventory"}, "ne peut recevoir qu&#39;une colonne"),
        ({"col::Numéro d'inventaire": "keep"}, "numéro d&#39;inventaire"),
        ({"codes_barres": "column"}, "Code-barres déjà attribué"),
        ({"prefixe": "EX <script>"}, "Préfixe de 12 caractères"),
    ],
)
def test_an_incoherent_mapping_is_refused_with_its_reason(client_as, change, message):
    browser = client_as("librarian")
    token = _deposit(browser, enriched=False)
    form = _mapping_form(browser.get(f"/imports/{token}").text)
    response = browser.post(f"/imports/{token}", data={**form, **change, "action": "simuler"})
    assert response.status_code == 400 and message in response.text


def test_a_mapping_profile_is_saved_reused_and_deleted_with_an_audit_trail(client_as, settings):
    browser = client_as("librarian")
    token = _deposit(browser, enriched=False)
    form = _mapping_form(browser.get(f"/imports/{token}").text)
    form["etat::nettoyable"] = "damaged"
    assert browser.post(f"/imports/{token}", data={**form, "action": "profil", "nom_profil": ""}).status_code == 400
    response = browser.post(f"/imports/{token}", data={**form, "action": "profil", "nom_profil": "Fichier du club"})
    assert response.status_code == 303
    connection = sqlite3.connect(settings.database_path)
    try:
        profile_id, mapping = connection.execute("SELECT id, mapping FROM import_profiles").fetchone()
        actions = [
            r[0] for r in connection.execute("SELECT action FROM audit_log WHERE entity_type = 'import_profile'")
        ]
    finally:
        connection.close()
    assert '"nettoyable":"damaged"' in mapping and actions == ["import.profile_saved"]
    index = browser.get("/imports").text
    assert "Fichier du club" in index
    again = browser.post(
        "/imports", files={"principal": ("c.xlsx", samples.workbook(), XLSX)}, data={"profil": str(profile_id)}
    )
    page = browser.get(again.headers["location"]).text
    assert re.search(r'name="etat::nettoyable"[^>]*>.*?<option value="damaged" selected', page, re.S)
    assert browser.post(f"/imports/profils/{profile_id}/supprimer").status_code == 303
    assert _counts(settings)["import_profiles"] == 0


@pytest.mark.parametrize(
    ("filename", "data", "message"),
    [
        ("vieux.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 100, "Ancien format Excel"),
        ("vide.xlsx", b"", "Le fichier est vide"),
        ("image.png", b"\x89PNG\r\n\x1a\n" + b"0" * 50, "ni un classeur"),
    ],
)
def test_a_refused_file_is_explained(client_as, filename, data, message):
    response = client_as("librarian").post(
        "/imports", files={"principal": (filename, data, "application/octet-stream")}
    )
    assert response.status_code == 400 and message in response.text


def test_the_navigation_leads_to_the_import_screen(client_as):
    page = client_as("librarian").get("/imports").text
    assert 'href="/imports"' in page and "/a-venir/imports" not in page
