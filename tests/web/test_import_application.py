# SPDX-License-Identifier: EUPL-1.2
"""Écrans de l'application et des exports (lot L6, T5 à T7) : confirmation, permissions, lot, origine de
l'exemplaire, export complet et au format d'origine, confidentialité, protection contre les formules.

Classeurs fabriqués (`tests/import_samples.py`) : aucune donnée du Club."""

from __future__ import annotations

import io
import re
import sqlite3
import zipfile

from openpyxl import load_workbook

from biblyo.modules.imports import apply as importer

from .. import import_samples as samples

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _query(settings, sql: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _enable(settings, value: str = "true") -> None:
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, '2026-11-13T00:00:00Z')",
        (importer.ENABLED_SETTING, value),
    )
    connection.commit()
    connection.close()


def _deposit(browser, rows=None) -> str:
    files = {
        "principal": ("catalogue.xlsx", samples.workbook(rows=rows), XLSX),
        "complementaire": ("enrichi.xlsx", samples.workbook(enriched=True, rows=rows), XLSX),
    }
    response = browser.post("/imports", files=files, data={"profil": ""})
    assert response.status_code == 303, response.text
    return re.fullmatch(r"/imports/([0-9a-f]{32})", response.headers["location"]).group(1)


def _plan(page: str) -> str:
    return re.search(r'name="plan" value="([0-9a-f]{64})"', page).group(1)


def _apply(browser, settings) -> int:
    token = _deposit(browser)
    page = browser.get(f"/imports/{token}/simulation").text
    response = browser.post(f"/imports/{token}/appliquer", data={"plan": _plan(page), "confirmer": "1"})
    assert response.status_code == 303, response.text
    return int(re.fullmatch(r"/imports/lots/(\d+)", response.headers["location"]).group(1))


def test_applying_requires_the_permission_the_instance_switch_and_the_confirmation(client_as, settings):
    librarian = client_as("librarian")
    token = _deposit(librarian)
    page = librarian.get(f"/imports/{token}/simulation").text
    assert "désactivée sur cette instance" in page and 'name="plan"' not in page
    _enable(settings)
    page = librarian.get(f"/imports/{token}/simulation").text
    plan = _plan(page)
    refused = librarian.post(f"/imports/{token}/appliquer", data={"plan": plan})
    assert refused.status_code == 400 and "Cochez la case" in refused.text
    stale = librarian.post(f"/imports/{token}/appliquer", data={"plan": "0" * 64, "confirmer": "1"})
    assert stale.status_code == 409 and "plan a changé" in stale.text
    before = _query(settings, "SELECT COUNT(*) FROM copies")[0][0]
    volunteer = client_as("volunteer")
    assert volunteer.post(f"/imports/{token}/appliquer", data={"plan": plan, "confirmer": "1"}).status_code == 403
    assert _query(settings, "SELECT COUNT(*) FROM copies")[0][0] == before


def test_a_confirmed_plan_is_applied_once_and_its_batch_is_shown(client_as, settings):
    _enable(settings)
    librarian = client_as("librarian")
    before = _query(settings, "SELECT COUNT(*) FROM copies")[0][0]
    batch_id = _apply(librarian, settings)
    assert _query(settings, "SELECT COUNT(*) FROM copies")[0][0] == before + 23
    page = librarian.get(f"/imports/lots/{batch_id}")
    assert page.status_code == 200
    assert "Réconciliation" in page.text and "écart" not in page.text.split("Réconciliation")[1].split("</table>")[0]
    assert "avant-import" in page.text  # nom de la sauvegarde prise avant l'écriture
    listing = librarian.get("/imports").text
    assert f"/imports/lots/{batch_id}" in listing
    again = _deposit(librarian)  # le même fichier, déposé de nouveau : la simulation le bloque
    page = librarian.get(f"/imports/{again}/simulation").text
    assert "application bloquée" in page
    assert _query(settings, "SELECT COUNT(*) FROM import_batches WHERE status = 'applied'") == [(1,)]


def test_the_copy_page_shows_its_origin_and_the_original_values(client_as, settings):
    _enable(settings)
    librarian = client_as("librarian")
    _apply(librarian, settings)
    copy_id = _query(settings, "SELECT id FROM copies WHERE inventory_number = '14'")[0][0]
    page = librarian.get(f"/exemplaires/{copy_id}").text
    assert "Ligne 15 du classeur catalogue.xlsx" in page
    assert "Numéro d&#39;inventaire" in page or "Numéro d’inventaire" in page
    assert "à revoir" in page  # précision sur l'état, valeur d'origine
    assert "Aucun, à attribuer" in page or "il sera attribué à l’étiquetage" in page
    volunteer = client_as("volunteer")
    assert volunteer.get(f"/exemplaires/{copy_id}").status_code == 200  # la provenance n'est pas personnelle


def test_the_original_format_restores_every_cell_and_lists_legitimate_changes(client_as, settings):
    _enable(settings)
    librarian = client_as("librarian")
    batch_id = _apply(librarian, settings)
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE copies SET call_number = 'MAR/99' WHERE inventory_number = '1'")
    connection.commit()
    connection.close()
    response = librarian.get(f"/imports/lots/{batch_id}/format-origine.xlsx")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    book = load_workbook(io.BytesIO(response.content))
    sheet = book["Feuil1"]
    assert [c.value for c in sheet[1]][:13] == list(samples.HEADERS)
    assert sheet["A2"].value == "Marins (Les)"  # forme d'origine, pas l'affichage naturel
    assert sheet["J2"].value == 1 and isinstance(sheet["J2"].value, int)  # type d'origine gardé
    assert sheet["I2"].value == "MAR/99"  # modification légitime, exportée…
    changes = [[c.value for c in row] for row in book["Modifications"].iter_rows(min_row=2)]
    assert ["I2", 2, "Cote", "MAR/01", "MAR/99", "Cote modifiée dans biblyO"] in changes  # …et déclarée
    assert "Compléments et propositions" in book.sheetnames
    assert _query(settings, "SELECT action FROM audit_log WHERE action = 'import.export_original'")


def test_the_full_export_is_complete_confidential_and_safe(client_as, settings):
    _enable(settings)
    librarian = client_as("librarian")
    rows = [tuple(r) for r in samples.ROWS]
    rows[0] = (*rows[0][:9], "007", *rows[0][10:])  # inventaire à zéros initiaux, en texte
    token = _deposit(librarian, rows=rows)
    page = librarian.get(f"/imports/{token}/simulation").text
    applied = librarian.post(f"/imports/{token}/appliquer", data={"plan": _plan(page), "confirmer": "1"})
    assert applied.status_code == 303
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE works SET title = '=HYPERLINK(\"https://x.invalid\")' WHERE title = 'Le vent tourne'")
    connection.commit()
    connection.close()
    xlsx = librarian.get("/imports/export/catalogue.xlsx")
    assert xlsx.status_code == 200 and xlsx.headers["cache-control"] == "no-store"
    book = load_workbook(io.BytesIO(xlsx.content))
    assert {"exemplaires", "oeuvres", "editions", "series", "lignes_import", "lisez_moi"} <= set(book.sheetnames)
    assert not {"lecteurs", "prets", "comptes"} & set(book.sheetnames)
    copies = list(book["exemplaires"].iter_rows(values_only=True))
    headers = copies[0]
    assert "007" in [r[headers.index("numero_inventaire")] for r in copies[1:]]  # texte : le zéro initial reste
    titles = [
        c for row in book["oeuvres"].iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")
    ]
    assert titles and all(c.data_type == "s" for c in titles)  # jamais une formule
    raw = list(book["lignes_import"].iter_rows(values_only=True))
    assert "Marins (Les)" in raw[1]  # valeur d'origine, colonne par en-tête d'origine
    archive = zipfile.ZipFile(io.BytesIO(librarian.get("/imports/export/catalogue-csv.zip").content))
    text = archive.read("oeuvres.csv").decode("utf-8")
    assert text.startswith("\ufeff") and "'=HYPERLINK" in text and ";=HYPERLINK" not in text
    assert "007" in archive.read("exemplaires.csv").decode("utf-8-sig")
    assert client_as("volunteer").get("/imports/export/catalogue.xlsx").status_code == 403
    assert client_as("viewer").get("/imports/export/catalogue-csv.zip").status_code == 403
    assert _query(settings, "SELECT COUNT(*) FROM audit_log WHERE action = 'import.export_catalogue'") == [(2,)]


def test_the_legacy_reconstruction_is_refused_at_deposit(client_as):
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Inventaire", "Code-barres futur", "Titre", "Ligne Excel"])
    book.active.append([1, "CSN-000001", "Un titre", 7])
    out = io.BytesIO()
    book.save(out)
    response = client_as("librarian").post(
        "/imports", files={"principal": ("Classement.xlsx", out.getvalue(), XLSX)}, data={"profil": ""}
    )
    assert response.status_code == 400 and "reconstruction de l’ancienne application" in response.text
