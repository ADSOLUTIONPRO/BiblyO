# SPDX-License-Identifier: EUPL-1.2
"""Après l'import (lot L6, arbitrages du 13 novembre) : mise en rayon par lot, présence vérifiée, codes-barres en lot,
prêt et retour par le numéro d'inventaire sans code-barres. Classeur fabriqué : aucune donnée du Club."""

from __future__ import annotations

import re
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import LocationInput
from biblyo.modules.imports import apply as importer
from biblyo.modules.imports import profile as profiles
from biblyo.modules.imports import reader, simulate

from .. import import_samples as samples
from .test_circulation_counter import _adult, _form, _scan, _state


def _rows(settings, query: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


@pytest.fixture
def database(settings):
    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, 'true', '2026-11-13')", (importer.ENABLED_SETTING,)
    )
    connection.commit()
    connection.close()
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


@pytest.fixture
def imported(settings, database):
    main = reader.read(samples.workbook(), "catalogue.xlsx")
    profile = profiles.detect(main.sheets[0])
    with database.session() as session:
        sim = simulate.simulate(main, "Feuil1", profile, existing=simulate.snapshot(session))
        session.rollback()
    importer.apply(database, settings, importer.ApplyRequest(main, "Feuil1", profile, sim.plan_sha256, None))
    return dict(_rows(settings, "SELECT inventory_number, id FROM copies"))


@pytest.fixture
def shelf(database):
    room = holdings.create_location(database, "room", None, LocationInput("Salle BD"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble 1", code="M1"), None)
    return holdings.create_location(database, "shelf", furniture, LocationInput("Étagère A", capacity="5"), None)


def _versions(settings, ids):
    marks = ",".join("?" * len(ids))
    return dict(_rows(settings, f"SELECT id, version FROM copies WHERE id IN ({marks})", *ids))  # noqa: S608


def test_imported_copies_are_found_filtered_and_searched(client_as, imported):
    browser = client_as("librarian")
    page = browser.get("/emplacements/non-ranges?origine=importes").text
    assert "23 exemplaire(s)" in page and "importé" in page and "sans code-barres" in page
    found = browser.get("/emplacements/non-ranges?origine=importes&q=Marins").text
    assert "3 exemplaire(s)" in found  # par série, forme naturelle comprise
    by_inventory = browser.get("/emplacements/non-ranges?q=17").text
    assert "1 exemplaire(s)" in by_inventory and "Cinq fois" in by_inventory
    by_call = browser.get("/emplacements/non-ranges?q=rec/05").text
    assert "5 exemplaire(s)" in by_call


def test_bulk_shelving_keeps_preparation_unless_verified_then_presence_is_confirmed(
    client_as, settings, imported, shelf
):
    browser = client_as("librarian")
    first, second, third = imported["1"], imported["2"], imported["3"]
    preview = browser.get(f"/emplacements/deplacer-groupe?exemplaire={first}&exemplaire={second}&lieu={shelf}")
    assert preview.status_code == 200 and "statut après : En préparation" in preview.text
    versions = _versions(settings, [first, second])
    moved = browser.post(
        "/emplacements/deplacer-groupe",
        data={"lieu": shelf, "exemplaire": [first, second], **{f"v_{k}": v for k, v in versions.items()}},
    )
    assert moved.status_code == 303
    assert _rows(settings, "SELECT DISTINCT status, location_id FROM copies WHERE id IN (?, ?)", first, second) == [
        ("in_processing", shelf)
    ]  # une place, mais aucune fausse disponibilité
    waiting = browser.get("/emplacements/non-ranges?etat=en_preparation&origine=importes").text
    assert "23 exemplaire(s)" in waiting  # rangés ou non, tous encore à vérifier
    verified = browser.get(f"/emplacements/deplacer-groupe?exemplaire={third}&lieu={shelf}&verifie=1")
    assert "statut après : En place" in verified.text
    browser.post(
        "/emplacements/deplacer-groupe",
        data={"lieu": shelf, "exemplaire": [third], "verifie": "1", f"v_{third}": _versions(settings, [third])[third]},
    )
    assert _rows(settings, "SELECT status FROM copies WHERE id = ?", third) == [("available",)]
    page = browser.get(f"/emplacements/verifier?exemplaire={first}&exemplaire={imported['4']}").text
    assert "Sans place : rangez-le d’abord." in page and "Je confirme" not in page
    versions = _versions(settings, [first, second])
    done = browser.post(
        "/emplacements/verifier", data={"exemplaire": [first, second], **{f"v_{k}": v for k, v in versions.items()}}
    )
    assert done.status_code == 303
    assert _rows(settings, "SELECT DISTINCT status FROM copies WHERE id IN (?, ?)", first, second) == [("available",)]
    history = browser.get(f"/exemplaires/{first}").text
    assert "Présence vérifiée : en place" in history and "Ligne 2 du classeur" in history
    # Une affectation erronée se corrige : retirée de sa place, l'exemplaire garde inventaire, cote et origine
    browser.post(f"/exemplaires/{first}/sans-place", data={"version": _versions(settings, [first])[first]})
    assert _rows(settings, "SELECT location_id, inventory_number, call_number FROM copies WHERE id = ?", first) == [
        (None, "1", "MAR/01")
    ]


def test_barcodes_are_assigned_in_a_batch_only_where_missing(client_as, settings, imported):
    browser = client_as("librarian")
    ids = [imported["5"], imported["6"]]
    _rows(settings, "SELECT 1")
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE copies SET barcode = 'LAB-0006' WHERE id = ?", (ids[1],))
    connection.commit()
    connection.close()
    preview = browser.get(f"/emplacements/codes-barres?exemplaire={ids[0]}&exemplaire={ids[1]}").text
    assert "EX-000001" in preview and "déjà attribué, inchangé" in preview
    assert _rows(settings, "SELECT last_value FROM sequences WHERE name = 'copy_barcode'") == [(0,)]  # rien consommé
    versions = _versions(settings, ids)
    response = browser.post(
        "/emplacements/codes-barres", data={"exemplaire": ids, **{f"v_{k}": v for k, v in versions.items()}}
    )
    assert response.status_code == 303
    assert _rows(settings, "SELECT inventory_number, barcode FROM copies WHERE id IN (?, ?) ORDER BY id", *ids) == [
        ("5", "EX-000001"),
        ("6", "LAB-0006"),
    ]
    assert client_as("volunteer").get(f"/emplacements/codes-barres?exemplaire={ids[0]}").status_code == 200
    assert client_as("viewer").get(f"/emplacements/codes-barres?exemplaire={ids[0]}").status_code == 403


def test_a_copy_without_barcode_is_lent_and_returned_by_its_inventory_number(client_as, settings, database, imported):
    copy_id = imported["9"]
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE copies SET status = 'available' WHERE id = ?", (copy_id,))  # rangé et vérifié
    connection.commit()
    connection.close()
    assert _rows(settings, "SELECT barcode FROM copies WHERE id = ?", copy_id) == [(None,)]
    patron = _adult(database, card="CARTE-9")
    browser = client_as("volunteer")
    html = _scan(browser, None, "CARTE-9")
    html = _scan(browser, html, "9")  # le numéro d'inventaire tapé au clavier
    assert ("ex", str(copy_id)) in _state(html)
    done = browser.post("/comptoir/valider", data=_form(_state(html)))
    assert done.status_code == 200 and "Prêt enregistré" in done.text
    assert _rows(settings, "SELECT copy_id, patron_id FROM loans WHERE returned_at IS NULL") == [(copy_id, patron)]
    back = browser.post("/comptoir/retours/scan", data={"code": "9"}, headers={"HX-Request": "true"})
    assert back.status_code == 200
    assert _rows(settings, "SELECT COUNT(*) FROM loans WHERE returned_at IS NULL") == [(0,)]
    assert not re.search(r"\bNone\b", back.text)
