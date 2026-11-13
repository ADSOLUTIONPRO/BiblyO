# SPDX-License-Identifier: EUPL-1.2
"""Exemplaires et étagères dans un vrai navigateur : aucune violation axe grave, aucun débordement, parcours réels."""

from __future__ import annotations

import re
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import Browser, Page, expect

from biblyo.core.db import Database
from biblyo.modules.catalogue import repository as repo
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.service import LocationInput

from ..factories import make_notice
from .conftest import ADMIN, Server

pytestmark = pytest.mark.e2e
BLOCKING = {"serious", "critical"}


@pytest.fixture(scope="module")
def shelved(server: Server) -> Iterator[SimpleNamespace]:
    """Jeu fictif : une salle, un meuble, deux étagères, des exemplaires rangés et d'autres non."""
    settings = server.settings
    database = Database(settings.database_path)
    room = holdings.create_location(database, "room", None, LocationInput("Salle d'essai e2e"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble e2e"), None)
    shelves = [
        holdings.create_location(database, "shelf", furniture, LocationInput(f"Niveau {n}", capacity="8"), None)
        for n in (1, 2)
    ]
    work = make_notice(
        settings,
        "Exemplaires d'un titre assez long pour éprouver un petit écran",
        series="Essais",
        volume="3",
        publisher="Éditions Fictives",
    )
    with database.session() as session:
        notice = repo.load_notice(session, work)
        assert notice is not None
        edition = notice.primary.id
    copies = catalogue.add_copies(database, edition, 3, "", None)
    holdings.shelve(database, copies[:2], shelves[0], user_id=None)
    yield SimpleNamespace(room=room, furniture=furniture, shelves=shelves, work=work, copies=copies)
    database.dispose()


def _serious(page: Page) -> list[str]:
    results = Axe().run(page).response["violations"]
    return [
        f"{item['id']} ({item['impact']}) : {node['target']}"
        for item in results
        if item["impact"] in BLOCKING
        for node in item["nodes"]
    ]


def _pages(data: SimpleNamespace) -> tuple[str, ...]:
    return (
        f"/catalogue/notices/{data.work}",
        f"/catalogue/notices/{data.work}?onglet=exemplaires",
        f"/exemplaires/{data.copies[0]}",
        f"/exemplaires/{data.copies[2]}/deplacer",
        "/catalogue?view=list",
        "/emplacements",
        "/emplacements/non-ranges",
        "/emplacements/nouveau?type=furniture",
        f"/emplacements/{data.room}",
        f"/emplacements/{data.room}?edition=plan&meuble={data.furniture}",
        f"/emplacements/{data.furniture}",
        f"/emplacements/{data.shelves[0]}",
        f"/emplacements/{data.furniture}/modifier",
        f"/emplacements/{data.furniture}?vue=liste",
        f"/emplacements/{data.shelves[0]}?localiser={data.copies[1]}",
        "/emplacements/localiser?q=Exemplaires",
        f"/emplacements/{data.shelves[0]}?mode=rangement",
        f"/emplacements/{data.shelves[0]}?mode=rangement&choisi={data.copies[0]}",
        f"/emplacements/{data.shelves[0]}/trier?tri=titre",
        f"/emplacements/deplacer-groupe?exemplaire={data.copies[0]}&lieu={data.shelves[1]}",
        "/emplacements/scanner",
        f"/catalogue?view=list&emplacement={data.shelves[0]}&tri=rayon",
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [390, 1024, 1440])
def test_copy_screens_are_accessible_without_overflow(
    browser: Browser, server: Server, shelved: SimpleNamespace, width: int, theme: str
) -> None:
    context = browser.new_context(
        storage_state=server.storage[ADMIN],
        bypass_csp=True,
        viewport={"width": width, "height": 900},
        color_scheme=theme,
    )
    page = context.new_page()
    problems: dict[str, list[str]] = {}
    for path in _pages(shelved):
        response = page.goto(f"{server.base_url}{path}")
        assert response is not None and response.ok, f"{path} : {response.status if response else 'sans réponse'}"
        page.wait_for_load_state("networkidle")
        found = _serious(page)
        extra = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if extra > 0:
            found.append(f"débordement de {extra} px")
        if found:
            problems[path] = found
    context.close()
    assert problems == {}


def test_move_a_copy_with_the_form(admin_page: Page, server: Server, shelved: SimpleNamespace) -> None:
    admin_page.goto(f"{server.base_url}/exemplaires/{shelved.copies[2]}/deplacer")
    admin_page.get_by_label("Emplacement").select_option(str(shelved.shelves[1]))
    admin_page.get_by_label("Rang sur l’étagère").fill("1")
    admin_page.get_by_role("button", name="Ranger ici").click()
    admin_page.wait_for_url(f"{server.base_url}/exemplaires/{shelved.copies[2]}")
    assert "Niveau 2" in admin_page.locator("main").inner_text()
    assert "position 1" in admin_page.locator("main").inner_text()


def _fresh_shelf(server: Server, name: str, count: int) -> SimpleNamespace:
    """Un meuble neuf, deux niveaux, des exemplaires rangés dans l'ordre : chaque parcours est indépendant."""
    settings = server.settings
    database = Database(settings.database_path)
    room = holdings.create_location(database, "room", None, LocationInput(f"Salle {name}"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput(f"Meuble {name}"), None)
    second = holdings.create_location(database, "furniture", room, LocationInput(f"Second {name}"), None)
    shelves = holdings.create_levels(database, furniture, 2, "10", None)
    work = make_notice(settings, f"Parcours {name}")
    with database.session() as session:
        notice = repo.load_notice(session, work)
        assert notice is not None
        edition = notice.primary.id
    copies = catalogue.add_copies(database, edition, count, "", None)
    holdings.shelve(database, copies, shelves[0], user_id=None)
    return SimpleNamespace(
        database=database, room=room, furniture=furniture, second=second, shelves=shelves, copies=copies
    )


def _order(data: SimpleNamespace, level: int) -> list[int]:
    from biblyo.modules.holdings import repository as places

    with data.database.session() as session:
        return places.shelf_order(session, level)


def test_arrange_with_the_keyboard_alone(admin_page: Page, server: Server) -> None:
    """Sans souris : choisir un dos, tabuler jusqu'à la place voulue, valider."""
    data = _fresh_shelf(server, "clavier", 3)
    admin_page.goto(f"{server.base_url}/emplacements/{data.shelves[0]}?mode=rangement")
    admin_page.locator(f"#dos-{data.copies[2]} a.spn").focus()
    admin_page.keyboard.press("Enter")
    expect(admin_page).to_have_url(re.compile(f"choisi={data.copies[2]}"))
    target = admin_page.locator(f"#dos-{data.copies[0]} button.ins")
    target.focus()
    expect(target).to_be_focused()
    admin_page.keyboard.press("Enter")
    expect(admin_page.locator(".toast").first).to_be_visible()
    assert _order(data, data.shelves[0]) == [data.copies[2], data.copies[0], data.copies[1]]


def test_drag_a_spine_before_another(admin_page: Page, server: Server) -> None:
    data = _fresh_shelf(server, "glisser", 3)
    admin_page.goto(f"{server.base_url}/emplacements/{data.shelves[0]}?mode=rangement")
    admin_page.drag_and_drop(f"#dos-{data.copies[2]} a.spn", f"#dos-{data.copies[0]} a.spn")
    expect(admin_page.locator(".toast").first).to_be_visible()
    assert _order(data, data.shelves[0]) == [data.copies[2], data.copies[0], data.copies[1]]


def test_drag_a_spine_to_another_level(admin_page: Page, server: Server) -> None:
    data = _fresh_shelf(server, "niveau", 2)
    admin_page.set_viewport_size({"width": 1440, "height": 1400})  # les deux niveaux à l'écran, sans défilement
    admin_page.goto(f"{server.base_url}/emplacements/{data.furniture}?mode=rangement")
    admin_page.drag_and_drop(f"#dos-{data.copies[0]} a.spn", f"#lv-{data.shelves[1]}-t")
    expect(admin_page.locator(".toast").first).to_be_visible()
    assert _order(data, data.shelves[1]) == [data.copies[0]]
    assert _order(data, data.shelves[0]) == [data.copies[1]]


def test_scan_keeps_the_focus_for_the_next_code(admin_page: Page, server: Server) -> None:
    data = _fresh_shelf(server, "scan", 1)
    with data.database.session() as session:
        work_edition = repo.load_notice(session, make_notice(server.settings, "À scanner")).primary.id
    (copy,) = catalogue.add_copies(data.database, work_edition, 1, "", None)
    with data.database.session() as session:
        from biblyo.modules.catalogue.models import Copy

        code = session.get(Copy, copy).barcode
    admin_page.goto(f"{server.base_url}/emplacements/scanner?lieu={data.shelves[1]}")
    field = admin_page.get_by_label("Code-barres ou numéro d’inventaire")
    expect(field).to_be_focused()
    field.type(code)
    admin_page.keyboard.press("Enter")
    expect(admin_page.locator(".toast").first).to_be_visible()
    expect(admin_page.get_by_label("Code-barres ou numéro d’inventaire")).to_be_focused()
    assert _order(data, data.shelves[1]) == [copy]
    expect(admin_page.locator("main")).to_contain_text("À scanner")


def test_arrows_move_between_the_furniture_of_a_room(admin_page: Page, server: Server) -> None:
    data = _fresh_shelf(server, "fleches", 1)
    admin_page.set_viewport_size({"width": 1440, "height": 900})
    admin_page.goto(f"{server.base_url}/emplacements/{data.room}")
    tiles = admin_page.locator("#plan .room-plan a.furn")
    tiles.first.focus()
    admin_page.keyboard.press("ArrowRight")
    expect(tiles.nth(1)).to_be_focused()
    admin_page.keyboard.press("ArrowLeft")
    expect(tiles.first).to_be_focused()


def test_move_works_without_javascript(browser: Browser, server: Server) -> None:
    """D4 : le déplacement et le mode Rangement passent par de simples formulaires."""
    data = _fresh_shelf(server, "sansjs", 2)
    context = browser.new_context(storage_state=server.storage[ADMIN], java_script_enabled=False)
    page = context.new_page()
    page.goto(f"{server.base_url}/emplacements/{data.shelves[0]}?mode=rangement&choisi={data.copies[1]}")
    page.locator(f"#dos-{data.copies[0]} button.ins").click()
    page.wait_for_load_state("load")
    context.close()
    assert _order(data, data.shelves[0]) == [data.copies[1], data.copies[0]]


def test_dropping_a_spine_on_its_right_neighbour_changes_nothing(admin_page: Page, server: Server) -> None:
    """A14 : déposer un dos juste avant son voisin de droite le laisse à sa place, sans rien écrire."""
    from biblyo.modules.catalogue.models import Copy

    data = _fresh_shelf(server, "immobile", 3)
    with data.database.session() as session:
        versions = [session.get(Copy, item).version for item in data.copies]
    admin_page.goto(f"{server.base_url}/emplacements/{data.shelves[0]}?mode=rangement")
    admin_page.drag_and_drop(f"#dos-{data.copies[0]} a.spn", f"#dos-{data.copies[1]} a.spn")
    expect(admin_page.locator(".toast").first).to_contain_text("Rien n’a changé")
    assert _order(data, data.shelves[0]) == data.copies
    with data.database.session() as session:
        assert [session.get(Copy, item).version for item in data.copies] == versions

