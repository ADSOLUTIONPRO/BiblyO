# SPDX-License-Identifier: EUPL-1.2
"""Rangement à l'écran : mode Rangement, tri avec aperçu, déplacement groupé, scan et annulation."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.holdings import repository as repo
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import LocationInput

from ..factories import make_notice


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


@pytest.fixture
def place(database):
    room = holdings.create_location(database, "room", None, LocationInput("Salle"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble"), None)
    shelves = [
        holdings.create_location(database, "shelf", furniture, LocationInput(f"Étagère {n}", capacity="3"), None)
        for n in (1, 2)
    ]
    return SimpleNamespace(room=room, furniture=furniture, shelves=shelves)


def _copies(settings, database, title="Titre", count=1, **options):
    work = make_notice(settings, title, **options)
    connection = sqlite3.connect(settings.database_path)
    edition = connection.execute("SELECT id FROM editions WHERE work_id = ?", (work,)).fetchone()[0]
    connection.close()
    return catalogue.add_copies(database, edition, count, "", None)


def _order(database, location):
    with database.session() as session:
        return repo.shelf_order(session, location)


def _version(settings, table, row):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(f"SELECT version FROM {table} WHERE id = ?", (row,)).fetchone()[0]  # noqa: S608
    finally:
        connection.close()


# --- Permissions --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("role", "expected"), [("admin", 200), ("librarian", 200), ("volunteer", 200), ("viewer", 403)]
)
def test_tools_by_role(client_as, settings, database, place, role, expected):
    browser = client_as(role)
    assert browser.get("/emplacements/scanner").status_code == expected
    assert browser.get(f"/emplacements/{place.shelves[0]}/trier").status_code == expected
    copies = _copies(settings, database)
    assert browser.get("/emplacements/deplacer-groupe", params={"exemplaire": copies}).status_code == expected


@pytest.mark.parametrize(
    "path",
    ["/emplacements/ranger", "/emplacements/scanner", "/emplacements/scanner/annuler", "/emplacements/deplacer-groupe"],
)
def test_viewer_cannot_write(client_as, path):
    assert client_as("viewer").post(path, data={}).status_code == 403


def test_viewer_is_not_offered_the_arrange_mode(client_as, settings, database, place):
    copies = _copies(settings, database)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    browser = client_as("viewer")
    page = browser.get(f"/emplacements/{place.furniture}").text
    assert "Rangement" not in page and "Ranger en scannant" not in page
    forced = browser.get(f"/emplacements/{place.furniture}?mode=rangement").text
    assert "Choisir pour le déplacer" not in forced and 'draggable="true"' not in forced


# --- Mode Rangement, sans JavaScript ------------------------------------------------------------------------------
def test_arrange_by_keyboard_flow(client_as, settings, database, place):
    copies = _copies(settings, database, "Ordre", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    browser = client_as("volunteer")
    base = f"/emplacements/{place.shelves[0]}?mode=rangement"
    page = browser.get(base).text
    assert f"choisi={copies[2]}" in page and 'draggable="true"' in page and "Choisir pour le déplacer" in page
    assert 'class="ins"' not in page  # rien à placer tant qu'aucun dos n'est choisi
    chosen = browser.get(f"{base}&choisi={copies[2]}").text
    assert "Placer avant « Ordre », position 1" in chosen and "Placer à la fin de Étagère 2" in chosen
    assert chosen.count('type="submit" form="f-ins-') == 2 + 2  # deux autres dos, plus la fin de chaque niveau
    data = {
        "copie": copies[2],
        "version": _version(settings, "copies", copies[2]),
        "lieu": place.shelves[0],
        "version_lieu": _version(settings, "locations", place.shelves[0]),
        "rang": 1,
        "retour": place.shelves[0],
    }
    response = browser.post("/emplacements/ranger", data=data)
    assert response.status_code == 303 and response.headers["location"].endswith(f"#dos-{copies[2]}")
    assert _order(database, place.shelves[0]) == [copies[2], copies[0], copies[1]]


def test_arrange_to_another_shelf_end(client_as, settings, database, place):
    copies = _copies(settings, database, "Ailleurs", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    data = {"copie": copies[0], "lieu": place.shelves[1], "rang": 1, "retour": place.furniture}
    assert client_as("admin").post("/emplacements/ranger", data=data).status_code == 303
    assert _order(database, place.shelves[1]) == [copies[0]]
    assert _order(database, place.shelves[0]) == [copies[1]]


def test_arranging_on_an_outdated_view_is_refused(client_as, settings, database, place):
    copies = _copies(settings, database, "Périmé", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    shown = _version(settings, "locations", place.shelves[0])
    holdings.reorder(database, place.shelves[0], list(reversed(copies)), None, None)
    data = {"copie": copies[0], "lieu": place.shelves[0], "version_lieu": shown, "rang": 1, "retour": place.shelves[0]}
    page = client_as("admin").post("/emplacements/ranger", data=data)
    assert page.status_code == 409 and "Rien n’a été enregistré" in page.text
    assert _order(database, place.shelves[0]) == list(reversed(copies))


def test_arrange_refuses_bad_requests(client_as, settings, database, place):
    browser = client_as("admin")
    assert browser.post("/emplacements/ranger", data={"copie": "x"}).status_code == 400
    (copy,) = _copies(settings, database)
    holdings.change_status(database, copy, "lost", user_id=None)
    data = {"copie": copy, "lieu": place.shelves[0], "rang": 1, "retour": place.shelves[0]}
    page = browser.post("/emplacements/ranger", data=data)
    assert page.status_code == 400 and "déclarez-le d’abord retrouvé" in page.text


# --- Tri avec aperçu ----------------------------------------------------------------------------------------------
def test_sort_preview_changes_nothing_then_apply(client_as, settings, database, place):
    (third,) = _copies(settings, database, "Tome trois", series="Lanfeust", volume="3")
    (first,) = _copies(settings, database, "Tome un", series="Lanfeust", volume="1")
    holdings.shelve(database, [third, first], place.shelves[0], user_id=None)
    browser = client_as("volunteer")
    page = browser.get(f"/emplacements/{place.shelves[0]}/trier?tri=serie")
    assert "2 exemplaires changeraient de position." in page.text and "Appliquer ce tri" in page.text
    assert _order(database, place.shelves[0]) == [third, first]
    version = _version(settings, "locations", place.shelves[0])
    data = {"tri": "serie", "version_lieu": version}
    assert browser.post(f"/emplacements/{place.shelves[0]}/trier", data=data).status_code == 303
    assert _order(database, place.shelves[0]) == [first, third]
    outdated = browser.post(f"/emplacements/{place.shelves[0]}/trier", data=data)
    assert outdated.status_code == 409 and "vient d’être modifié" in outdated.text


def test_sort_of_an_ordered_shelf_offers_nothing(client_as, settings, database, place):
    copies = _copies(settings, database, "Seul")
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    page = client_as("admin").get(f"/emplacements/{place.shelves[0]}/trier?tri=titre").text
    assert "déjà dans cet ordre" in page and "Appliquer" not in page


def test_sort_needs_a_level(client_as, place):
    browser = client_as("admin")
    assert browser.get(f"/emplacements/{place.furniture}/trier").status_code == 404
    assert browser.post(f"/emplacements/{place.shelves[0]}/trier", data={"tri": "n"}).status_code == 400


# --- Déplacement groupé -------------------------------------------------------------------------------------------
def test_group_move_shows_consequences_before_confirming(client_as, settings, database, place):
    copies = _copies(settings, database, "Groupe", 4)
    holdings.shelve(database, copies[:2], place.shelves[0], user_id=None)
    holdings.shelve(database, copies[2:3], place.shelves[1], user_id=None)
    browser = client_as("volunteer")
    selection = {"exemplaire": [copies[0], copies[3]], "lieu": place.shelves[1], "rang": "1"}
    page = browser.get("/emplacements/deplacer-groupe", params=selection).text
    assert "Confirmer le déplacement" in page and "Quitte Salle / Meuble / Étagère 1" in page
    assert "Sans place jusqu’ici" in page and "1 exemplaire(s) déjà sur place se décaleront." in page
    assert _order(database, place.shelves[1]) == [copies[2]]  # l'aperçu n'a rien changé
    data = {"exemplaire": selection["exemplaire"], "lieu": place.shelves[1], "rang": "1"}
    data[f"v_{copies[0]}"] = _version(settings, "copies", copies[0])
    data["version_lieu"] = _version(settings, "locations", place.shelves[1])
    assert browser.post("/emplacements/deplacer-groupe", data=data).status_code == 303
    assert _order(database, place.shelves[1]) == [copies[0], copies[3], copies[2]]
    assert _order(database, place.shelves[0]) == [copies[1]]


def test_group_move_over_capacity_is_announced(client_as, settings, database, place):
    copies = _copies(settings, database, "Trop", 4)
    selection = {"exemplaire": copies, "lieu": place.shelves[0]}
    page = client_as("admin").get("/emplacements/deplacer-groupe", params=selection).text
    assert "dépassera sa capacité de 1 place(s)" in page and "Confirmer le déplacement" in page


def test_group_move_with_a_lost_copy_is_blocked_and_explained(client_as, settings, database, place):
    copies = _copies(settings, database, "Mélange", 2)
    holdings.change_status(database, copies[1], "lost", user_id=None)
    browser = client_as("admin")
    selection = {"exemplaire": copies, "lieu": place.shelves[0]}
    page = browser.get("/emplacements/deplacer-groupe", params=selection).text
    assert "Rien ne sera déplacé" in page and "Confirmer le déplacement" not in page
    refused = browser.post("/emplacements/deplacer-groupe", data=selection)
    assert refused.status_code == 400 and _order(database, place.shelves[0]) == []


def test_group_move_outdated_is_refused(client_as, settings, database, place):
    copies = _copies(settings, database, "Vieux", 2)
    data = {"exemplaire": copies, "lieu": place.shelves[0], f"v_{copies[0]}": 99}
    page = client_as("admin").post("/emplacements/deplacer-groupe", data=data)
    assert page.status_code == 409


def test_group_move_needs_a_selection(client_as):
    assert client_as("admin").get("/emplacements/deplacer-groupe").status_code == 400


def test_list_view_offers_selection_to_editors_only(client_as, settings, database, place):
    copies = _copies(settings, database, "Liste", 1)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    url = f"/emplacements/{place.furniture}?vue=liste"
    assert 'name="exemplaire"' in client_as("volunteer").get(url).text
    assert 'name="exemplaire"' not in client_as("viewer", username="v2").get(url).text


# --- Ranger en scannant -------------------------------------------------------------------------------------------
def test_scan_session(client_as, settings, database, place):
    copies = _copies(settings, database, "Scanné", 2)
    browser = client_as("volunteer")
    for code in ("ex-000002", "EX 000001"):
        response = browser.post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": code})
        assert response.status_code == 303 and response.cookies.get("biblyo_flash") == "scan_ok"
    assert _order(database, place.shelves[0]) == [copies[1], copies[0]]
    page = browser.get(f"/emplacements/scanner?lieu={place.shelves[0]}").text
    assert "Scanné" in page and "position 2" in page and page.count("/emplacements/scanner/annuler") == 2
    assert 'autofocus' in page


def test_second_scan_moves_nothing(client_as, settings, database, place):
    copies = _copies(settings, database, "Deux fois", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    response = client_as("admin").post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": "EX-000001"})
    assert response.cookies.get("biblyo_flash") == "scan_same"
    assert _order(database, place.shelves[0]) == copies


def test_scan_problems_are_explained(client_as, settings, database, place):
    browser = client_as("admin")
    assert "Choisissez d’abord" in browser.post("/emplacements/scanner", data={"code": "EX-1"}).text
    assert "Scannez ou saisissez" in browser.post("/emplacements/scanner", data={"lieu": place.shelves[0]}).text
    unknown = browser.post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": "EX-999"})
    assert unknown.status_code == 400 and "Aucun exemplaire ne porte le code" in unknown.text
    (copy,) = _copies(settings, database)
    holdings.change_status(database, copy, "lost", user_id=None)
    lost = browser.post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": "EX-000001"})
    assert lost.status_code == 400 and "déclarez-le d’abord retrouvé" in lost.text


def test_scan_over_capacity_is_signalled(client_as, settings, database, place):
    _copies(settings, database, "Plein", 4)
    browser = client_as("admin")
    for code in ("EX-000001", "EX-000002", "EX-000003"):
        browser.post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": code})
    last = browser.post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": "EX-000004"})
    assert last.cookies.get("biblyo_flash") == "scan_over"


def _entry(settings):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute("SELECT max(id) FROM audit_log WHERE action = 'copy.shelve_scan'").fetchone()[0]
    finally:
        connection.close()


def test_undo_a_scan_restores_place_and_status(client_as, settings, database, place):
    (copy,) = _copies(settings, database, "Annulé")
    browser = client_as("volunteer")
    browser.post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": "EX-000001"})
    undo = browser.post("/emplacements/scanner/annuler", data={"entree": _entry(settings), "lieu": place.shelves[0]})
    assert undo.status_code == 303
    page = browser.get(f"/exemplaires/{copy}").text
    assert "En préparation" in page and "Non rangé" in page and "Rangement annulé" in page


def test_undo_is_refused_once_the_copy_moved_again(client_as, settings, database, place):
    (copy,) = _copies(settings, database, "Rebougé")
    browser = client_as("volunteer")
    browser.post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": "EX-000001"})
    entry = _entry(settings)
    holdings.shelve(database, [copy], place.shelves[1], user_id=None)
    assert "/emplacements/scanner/annuler" not in browser.get("/emplacements/scanner").text
    refused = browser.post("/emplacements/scanner/annuler", data={"entree": entry})
    assert refused.status_code == 409
    assert _order(database, place.shelves[1]) == [copy]


def test_undo_is_personal(client_as, settings, database, place):
    _copies(settings, database, "À moi")
    client_as("volunteer").post("/emplacements/scanner", data={"lieu": place.shelves[0], "code": "EX-000001"})
    other = client_as("librarian")
    assert other.post("/emplacements/scanner/annuler", data={"entree": _entry(settings)}).status_code == 404
    assert "À moi" not in other.get("/emplacements/scanner").text


# --- A14 : sans effet, par chaque voie ----------------------------------------------------------------------------
def _footprint(settings):
    connection = sqlite3.connect(settings.database_path)
    try:
        return (
            connection.execute("SELECT id, location_id, shelf_position, version, updated_at FROM copies").fetchall(),
            connection.execute("SELECT id, version, updated_at FROM locations").fetchall(),
            connection.execute("SELECT count(*) FROM audit_log WHERE action LIKE 'copy.%'").fetchone()[0],
        )
    finally:
        connection.close()


def test_drag_or_keyboard_onto_the_right_neighbour_changes_nothing(client_as, settings, database, place):
    copies = _copies(settings, database, "Voisin", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    before = _footprint(settings)
    data = {
        "copie": copies[0],
        "version": _version(settings, "copies", copies[0]),
        "lieu": place.shelves[0],
        "version_lieu": _version(settings, "locations", place.shelves[0]),
        "rang": 2,  # « placer avant le dos au rang 2 » : c'est sa place actuelle
        "retour": place.shelves[0],
    }
    response = client_as("volunteer").post("/emplacements/ranger", data=data)
    assert response.status_code == 303 and response.cookies.get("biblyo_flash") == "copy_unchanged"
    assert _footprint(settings) == before


def test_form_move_to_the_current_place_changes_nothing(client_as, settings, database, place):
    copies = _copies(settings, database, "Formulaire", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    before = _footprint(settings)
    data = {"emplacement": place.shelves[0], "rang": "2", "version": _version(settings, "copies", copies[1])}
    response = client_as("volunteer").post(f"/exemplaires/{copies[1]}/deplacer", data=data)
    assert response.status_code == 303 and response.cookies.get("biblyo_flash") == "copy_unchanged"
    assert _footprint(settings) == before


def test_group_move_to_the_current_place_changes_nothing(client_as, settings, database, place):
    copies = _copies(settings, database, "Groupe", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    browser = client_as("volunteer")
    selection = {"exemplaire": copies, "lieu": place.shelves[0], "rang": "1"}
    preview = browser.get("/emplacements/deplacer-groupe", params=selection)
    assert "rien ne changera" in preview.text and "Confirmer le déplacement" not in preview.text
    before = _footprint(settings)
    response = browser.post("/emplacements/deplacer-groupe", data=selection)
    assert response.cookies.get("biblyo_flash") == "group_unchanged"
    assert _footprint(settings) == before


def test_scan_of_a_copy_already_there_changes_nothing(client_as, settings, database, place):
    copies = _copies(settings, database, "Scan", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    before = _footprint(settings)
    scan = {"lieu": place.shelves[0], "code": "EX-000001"}
    response = client_as("volunteer").post("/emplacements/scanner", data=scan)
    assert response.cookies.get("biblyo_flash") == "scan_same"
    assert _footprint(settings) == before
