# SPDX-License-Identifier: EUPL-1.2
"""Pages des emplacements : arbre, salles, meubles, gestion des lieux, plan, permissions."""

from __future__ import annotations

import re
import sqlite3
from types import SimpleNamespace

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import service as catalogue
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
    room = holdings.create_location(database, "room", None, LocationInput("Salle jeunesse"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble BD 1", code="BD1"), None)
    shelves = [
        holdings.create_location(database, "shelf", furniture, LocationInput(f"Étagère {n}", capacity="3"), None)
        for n in (1, 2)
    ]
    return SimpleNamespace(room=room, furniture=furniture, shelves=shelves)


def _copies(settings, database, title="Le Lotus bleu", count=1, **options):
    work = make_notice(settings, title, **options)
    connection = sqlite3.connect(settings.database_path)
    edition = connection.execute("SELECT id FROM editions WHERE work_id = ?", (work,)).fetchone()[0]
    connection.close()
    return catalogue.add_copies(database, edition, count, "", None)


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def test_shelves_are_in_the_navigation(client_as):
    page = client_as("viewer").get("/").text
    assert re.search(r'<a class="nav-i[^>]*href="/emplacements"', page)
    assert "/a-venir/etageres" not in page


def test_empty_state(client_as):
    page = client_as("admin").get("/emplacements").text
    assert "Aucun emplacement" in page and "Nouveau lieu" in page


@pytest.mark.parametrize(
    ("role", "view", "manage"),
    [("admin", 200, 200), ("librarian", 200, 200), ("volunteer", 200, 403), ("viewer", 200, 403)],
)
def test_places_by_role(client_as, place, role, view, manage):
    browser = client_as(role)
    paths = ("/emplacements", f"/emplacements/{place.room}", f"/emplacements/{place.furniture}")
    for path in paths:
        assert browser.get(path).status_code == view, path
    assert browser.get("/emplacements/nouveau").status_code == manage
    assert browser.get(f"/emplacements/{place.room}/modifier").status_code == manage


@pytest.mark.parametrize(
    ("role", "expected"), [("admin", 303), ("librarian", 303), ("volunteer", 403), ("viewer", 403)]
)
def test_writing_places_needs_the_permission(client_as, role, expected):
    response = client_as(role).post("/emplacements/nouveau", data={"type": "room", "nom": f"Salle {role}"})
    assert response.status_code == expected


@pytest.mark.parametrize(
    "path",
    ["modifier", "deplacer", "activation", "supprimer", "ordre", "plan"],
)
def test_volunteer_cannot_touch_the_structure(client_as, place, path):
    assert client_as("volunteer").post(f"/emplacements/{place.room}/{path}", data={}).status_code == 403


def test_tree_counts_and_unshelved(client_as, settings, database, place):
    copies = _copies(settings, database, count=3)
    holdings.shelve(database, copies[:2], place.shelves[0], user_id=None)
    page = client_as("viewer").get(f"/emplacements/{place.shelves[0]}").text
    assert "2/3" in page and 'aria-current="page"' in page
    assert re.search(r"Non rangés</span><span class=\"c\">1", page)
    loose = client_as("volunteer", username="v2").get("/emplacements/non-ranges").text
    assert "Le Lotus bleu" in loose and "Ranger…" in loose


def test_lost_copies_are_not_unshelved(client_as, settings, database):
    (copy,) = _copies(settings, database)
    holdings.change_status(database, copy, "lost", user_id=None)
    assert "Tout est rangé" in client_as("viewer").get("/emplacements/non-ranges").text


def test_create_a_room_a_furniture_and_its_levels(client_as, settings):
    browser = client_as("librarian")
    room = browser.post("/emplacements/nouveau", data={"type": "room", "nom": "Salle fictive"})
    assert room.status_code == 303
    room_id = int(room.headers["location"].rsplit("/", 1)[1])
    response = browser.post(
        "/emplacements/nouveau",
        data={
            "type": "furniture",
            "parent": room_id,
            "nom": "Meuble A",
            "code": "ma",
            "indication": "A–F",
            "niveaux": "3",
            "capacite_niveaux": "20",
        },
    )
    assert response.status_code == 303
    assert _rows(settings, "SELECT name, capacity, path FROM locations WHERE kind='shelf' ORDER BY sort_order") == [
        ("Étagère 1", 20, "Salle fictive / Meuble A / Étagère 1"),
        ("Étagère 2", 20, "Salle fictive / Meuble A / Étagère 2"),
        ("Étagère 3", 20, "Salle fictive / Meuble A / Étagère 3"),
    ]
    page = browser.get(response.headers["location"]).text
    assert "Meuble A" in page and "Étagère 3 · 0/20" in page


def test_form_errors_are_shown(client_as, place):
    page = client_as("admin").post(
        "/emplacements/nouveau", data={"type": "furniture", "parent": place.room, "nom": "Meuble BD 1", "code": "bd1"}
    )
    assert page.status_code == 400
    assert "porte déjà ce nom" in page.text and "déjà celui de" in page.text and 'value="Meuble BD 1"' in page.text


def test_incoherent_parent_is_explained(client_as, place):
    page = client_as("admin").post("/emplacements/nouveau", data={"type": "shelf", "parent": place.room, "nom": "X"})
    assert page.status_code == 400
    assert "se place sous" in page.text


def test_unknown_kind_and_place(client_as):
    browser = client_as("admin")
    assert browser.get("/emplacements/nouveau?type=grenier").status_code == 404
    assert browser.get("/emplacements/999").status_code == 404
    assert browser.get("/emplacements/999/modifier").status_code == 404


def test_edit_a_place_and_outdated_form(client_as, settings, place):
    browser = client_as("admin")
    data = {"nom": "Salle BD", "version": "1"}
    assert browser.post(f"/emplacements/{place.room}/modifier", data=data).status_code == 303
    assert _rows(settings, "SELECT path FROM locations WHERE id = ?", place.shelves[0]) == [
        ("Salle BD / Meuble BD 1 / Étagère 1",)
    ]
    outdated = browser.post(f"/emplacements/{place.room}/modifier", data={"nom": "Autre", "version": "1"})
    assert outdated.status_code == 409 and "vient d’être modifié" in outdated.text


def test_non_empty_place_is_kept(client_as, settings, database, place):
    (copy,) = _copies(settings, database)
    holdings.shelve(database, [copy], place.shelves[0], user_id=None)
    browser = client_as("admin")
    page = browser.post(f"/emplacements/{place.furniture}/supprimer", data={"ensemble": "1"})
    assert page.status_code == 400 and "contient encore 1 exemplaire" in page.text
    off = browser.post(f"/emplacements/{place.furniture}/activation", data={"actif": "0"})
    assert off.status_code == 400 and "encore 1 exemplaire" in off.text
    assert len(_rows(settings, "SELECT id FROM locations")) == 4


def test_delete_needs_an_explicit_confirmation_for_the_contents(client_as, settings, place):
    browser = client_as("admin")
    refused = browser.post(f"/emplacements/{place.furniture}/supprimer", data={})
    assert refused.status_code == 400 and "confirmez la suppression de l’ensemble" in refused.text
    done = browser.post(f"/emplacements/{place.furniture}/supprimer", data={"ensemble": "1"})
    assert done.status_code == 303 and done.headers["location"] == f"/emplacements/{place.room}"
    assert len(_rows(settings, "SELECT id FROM locations")) == 1


def test_cycle_is_refused_on_screen(client_as, place):
    page = client_as("admin").post(f"/emplacements/{place.room}/deplacer", data={"parent": place.furniture})
    assert page.status_code == 400 and "à l’intérieur de lui-même" in page.text


def test_deactivate_then_reactivate(client_as, settings, place):
    browser = client_as("admin")
    assert browser.post(f"/emplacements/{place.shelves[1]}/activation", data={"actif": "0"}).status_code == 303
    assert "désactivé" in browser.get(f"/emplacements/{place.shelves[1]}").text
    assert browser.post(f"/emplacements/{place.shelves[1]}/activation", data={"actif": "1"}).status_code == 303
    assert _rows(settings, "SELECT is_active FROM locations WHERE id = ?", place.shelves[1]) == [(1,)]


def test_levels_move_up_and_down_without_javascript(client_as, settings, place):
    browser = client_as("admin")
    data = {"lieu": place.shelves[1], "sens": "monter"}
    assert browser.post(f"/emplacements/{place.furniture}/ordre", data=data).status_code == 303
    order = _rows(settings, "SELECT id FROM locations WHERE parent_id = ? ORDER BY sort_order", place.furniture)
    assert [row[0] for row in order] == [place.shelves[1], place.shelves[0]]
    assert browser.post(f"/emplacements/{place.furniture}/ordre", data={"lieu": 999}).status_code == 404


def test_room_plan_is_edited_without_javascript(client_as, settings, database, place):
    second = holdings.create_location(database, "furniture", place.room, LocationInput("Meuble 2"), None)
    browser = client_as("admin")
    page = browser.get(f"/emplacements/{place.room}?edition=plan&meuble={second}").text
    assert 'name="cellule"' in page and "Poser à la ligne 2, colonne 1" in page
    placed = browser.post(
        f"/emplacements/{place.room}/plan",
        data={"meuble": second, "cellule": "2,1", "largeur": "2", "hauteur": "1"},
    )
    assert placed.status_code == 303
    assert _rows(settings, "SELECT grid_row, grid_col FROM locations WHERE id = ?", second) == [(2, 1)]
    refused = browser.post(
        f"/emplacements/{place.room}/plan",
        data={"meuble": second, "cellule": "1,2", "largeur": "2", "hauteur": "1"},
    )
    assert refused.status_code == 400 and "occupée par « Meuble BD 1 »" in refused.text
    assert _rows(settings, "SELECT grid_row, grid_col FROM locations WHERE id = ?", second) == [(2, 1)]
    assert browser.post(f"/emplacements/{place.room}/plan", data={"meuble": "x"}).status_code == 400


def test_furniture_drawing_shows_the_real_order(client_as, settings, database, place):
    copies = _copies(settings, database, "Astérix", 2, series="Astérix", volume="1")
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    page = client_as("viewer").get(f"/emplacements/{place.shelves[0]}").text
    assert 'aria-label="Position 1 : Astérix, tome 1, cote BD AST 1, en place"' in page
    assert 'aria-label="Position 2 : Astérix, tome 1, cote BD AST 1, en place"' in page
    assert "lvl focus" in page and "1 place" not in page
    assert page.count('class="slot"') == 1 + 3  # une place libre sur la première, trois sur la seconde


def test_viewer_sees_no_management_link(client_as, place):
    page = client_as("viewer").get(f"/emplacements/{place.room}").text
    assert "/modifier" not in page and "Nouveau lieu" not in page and "Modifier le plan" not in page


# --- Vue Liste, localisation (T7) ---------------------------------------------------------------------------------
def test_list_view_is_equivalent_to_the_drawing(client_as, settings, database, place):
    copies = _copies(settings, database, "Astérix", 2, series="Astérix", volume="1")
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.change_status(database, copies[1], "repair", user_id=None)
    page = client_as("viewer").get(f"/emplacements/{place.furniture}?vue=liste").text
    assert 'class="spn' not in page and "<table" in page
    assert "Exemplaires de Étagère 1, dans l’ordre réel" in page
    assert "BD AST 1" in page and "En réparation" in page and "EX-000002" in page
    assert "Aucun exemplaire sur ce niveau." in page


def test_series_grouping_can_be_switched_off(client_as, settings, database, place):
    copies = _copies(settings, database, "Astérix", 2, series="Astérix", volume="1")
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    browser = client_as("viewer")
    assert "g-start" in browser.get(f"/emplacements/{place.furniture}").text
    assert "g-start" not in browser.get(f"/emplacements/{place.furniture}?series=0").text


def test_locate_highlights_the_spine_and_its_neighbours(client_as, settings, database, place):
    (before,) = _copies(settings, database, "Avant")
    (target,) = _copies(settings, database, "La Cible")
    (after,) = _copies(settings, database, "Après")
    holdings.shelve(database, [before, target, after], place.shelves[1], user_id=None)
    page = client_as("viewer").get(f"/emplacements/{place.shelves[1]}?localiser={target}").text
    assert page.count('aria-current="location"') == 1 and "spn" in page and " hit" in page
    assert f'id="dos-{target}"' in page
    assert "Après « Avant »" in page and "avant « Après »" in page and "position 2" in page


@pytest.mark.parametrize("query", ["EX-000001", "ex000001", "La Cible", "BD CIB"])
def test_locate_by_code_title_or_call_number(client_as, settings, database, place, query):
    (target,) = _copies(settings, database, "La Cible", authors=())
    holdings.shelve(database, [target], place.shelves[0], user_id=None)
    response = client_as("viewer").get("/emplacements/localiser", params={"q": query})
    assert response.status_code == 303
    assert response.headers["location"] == f"/emplacements/{place.shelves[0]}?localiser={target}#dos-{target}"


def test_locate_several_or_nothing(client_as, settings, database, place):
    copies = _copies(settings, database, "Les Jumeaux", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    browser = client_as("viewer")
    several = browser.get("/emplacements/localiser", params={"q": "Jumeaux"})
    assert several.status_code == 200 and several.text.count(f"?localiser={copies[0]}") == 1
    assert "Aucun exemplaire trouvé" in browser.get("/emplacements/localiser", params={"q": "introuvable"}).text
    assert "Que cherchez-vous" in browser.get("/emplacements/localiser").text


def test_locating_an_unshelved_copy_leads_to_its_sheet(client_as, settings, database):
    (copy,) = _copies(settings, database, "Sans place")
    response = client_as("viewer").get("/emplacements/localiser", params={"q": "EX-000001"})
    assert response.headers["location"] == f"/exemplaires/{copy}"


def test_notice_links_to_the_located_spine(client_as, settings, database, place):
    work = make_notice(settings, "Liée")
    connection = sqlite3.connect(settings.database_path)
    edition = connection.execute("SELECT id FROM editions WHERE work_id = ?", (work,)).fetchone()[0]
    connection.close()
    (copy,) = catalogue.add_copies(database, edition, 1, "", None)
    holdings.shelve(database, [copy], place.shelves[0], user_id=None)
    page = client_as("viewer").get(f"/catalogue/notices/{work}").text
    assert f"/emplacements/{place.shelves[0]}?localiser={copy}#dos-{copy}" in page


def test_virtual_place_lists_its_copies(client_as, settings, database, place):
    virtual = holdings.create_location(database, "virtual", place.room, LocationInput("Exposition"), None)
    (copy,) = _copies(settings, database, "Exposé")
    holdings.shelve(database, [copy], virtual, user_id=None)
    page = client_as("viewer").get(f"/emplacements/{virtual}").text
    assert "Exposé" in page and "Exemplaires présents" in page


# --- Catalogue par étagère (T9, CAT-13) ---------------------------------------------------------------------------
def _titles_in_order(page: str) -> list[str]:
    return re.findall(r'<a class="t" href="/catalogue/notices/\d+">([^<]+)</a>', page)


def test_catalogue_in_physical_order(client_as, settings, database, place):
    (zebre,) = _copies(settings, database, "Zèbre")
    (abeille,) = _copies(settings, database, "Abeille")
    (milieu,) = _copies(settings, database, "Milieu")
    holdings.shelve(database, [zebre, abeille], place.shelves[0], user_id=None)
    holdings.shelve(database, [milieu], place.shelves[1], user_id=None)
    browser = client_as("viewer")
    url = f"/catalogue?view=list&emplacement={place.furniture}"
    assert _titles_in_order(browser.get(f"{url}&tri=rayon").text) == ["Zèbre", "Abeille", "Milieu"]
    assert _titles_in_order(browser.get(url).text) == ["Abeille", "Milieu", "Zèbre"]  # par défaut, le titre
    holdings.reorder_children(database, place.furniture, list(reversed(place.shelves)), None, None)
    assert _titles_in_order(browser.get(f"{url}&tri=rayon").text) == ["Milieu", "Zèbre", "Abeille"]


def test_shelf_order_follows_the_real_position(client_as, settings, database, place):
    (one,) = _copies(settings, database, "Un")
    (two,) = _copies(settings, database, "Deux")
    holdings.shelve(database, [one, two], place.shelves[0], user_id=None)
    holdings.shelve(database, [two], place.shelves[0], position=1, user_id=None)
    page = client_as("viewer").get(f"/catalogue?view=list&emplacement={place.shelves[0]}&tri=rayon").text
    assert _titles_in_order(page) == ["Deux", "Un"]


def test_shelf_order_is_only_offered_with_a_place(client_as, place):
    browser = client_as("viewer")
    assert "Ordre sur l’étagère" not in browser.get("/catalogue?view=list").text
    assert "Ordre sur l’étagère" not in browser.get("/catalogue?emplacement=non-ranges").text
    assert "Ordre sur l’étagère" in browser.get(f"/catalogue?emplacement={place.shelves[0]}").text
    assert browser.get("/catalogue?view=list&tri=rayon").status_code == 200  # sans emplacement : ignoré


def test_shelf_page_links_to_the_catalogue(client_as, place):
    page = client_as("viewer").get(f"/emplacements/{place.shelves[0]}").text
    assert f"/catalogue?view=list&amp;emplacement={place.shelves[0]}&amp;tri=rayon" in page
