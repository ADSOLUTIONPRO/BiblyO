# SPDX-License-Identifier: EUPL-1.2
"""Navigation : filtrée par permission, sans lien mort, modules à venir signalés et absents en production."""

from __future__ import annotations

import re

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from biblyo.app import create_app
from biblyo.core.modules import Module, NavEntry, Permission
from biblyo.core.navigation import PLANNED, PLANNED_BY_CODE, build_navigation
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.registry import all_modules

ROLES = ("admin", "librarian", "volunteer", "viewer")
#: Entrées de la barre latérale attendues pour chaque rôle, modules à venir compris.
EXPECTED = {
    "admin": {
        "Tableau de bord", "Statistiques", "Catalogue", "Séries & collections", "Étagères", "Prêts & retours",
        "Lecteurs & familles", "Enrichissement", "Inventaire", "Import & export",
        "Utilisateurs", "Paramètres", "À propos",
    },
    "librarian": {
        "Tableau de bord", "Statistiques", "Catalogue", "Séries & collections", "Étagères", "Prêts & retours",
        "Lecteurs & familles", "Enrichissement", "Inventaire", "Import & export", "Paramètres", "À propos",
    },
    "volunteer": {
        "Tableau de bord", "Statistiques", "Catalogue", "Séries & collections", "Étagères", "Prêts & retours",
        "Lecteurs & familles", "Inventaire", "À propos",
    },
    "viewer": {"Tableau de bord", "Statistiques", "Catalogue", "Séries & collections", "Étagères", "À propos"},
}  # fmt: skip


def _side(html: str) -> str:
    return html[html.index('class="side"') : html.index('class="main"')]


def _entries(html: str) -> set[str]:
    return {label.replace("&amp;", "&") for label in re.findall(r'<span class="lb">([^<]+)</span>', _side(html))}


def _auth(permissions: set[str]) -> AuthContext:
    return AuthContext(1, "c", "C", (), (), frozenset(permissions), 1, "t", False)


@pytest.mark.parametrize("role", ROLES)
def test_entries_by_role(client_as, role):
    assert _entries(client_as(role).get("/").text) == EXPECTED[role]


@pytest.mark.parametrize("role", ROLES)
def test_every_displayed_link_answers_200_for_this_role(client_as, role):
    browser = client_as(role)
    html = browser.get("/").text
    side = _side(html)
    bottom = html[html.index('class="bnav') : html.index('id="toasts"')]
    palette = html[html.index('id="palette"') :]
    sheet = html[html.index('id="plus"') : html.index('id="raccourcis"')]
    top = html[html.index('class="top"') : html.index('id="contenu"')]
    links = {
        href
        for block in (side, bottom, palette, sheet, top)
        for href in re.findall(r'href="(/[^"]*)"', block)
        if not href.startswith("/static/")
    }
    assert "/" in links
    for link in sorted(links):
        assert browser.get(link).status_code == 200, f"{role} : {link}"


def test_planned_entries_say_so(client_as):
    side = _side(client_as("admin").get("/").text)
    shelves = re.search(r'<a class="nav-i[^>]*href="/a-venir/statistiques".*?</a>', side, re.S).group(0)
    assert "soon" in shelves
    assert "À venir" in shelves
    assert 'title="Statistiques (À venir)"' in shelves


def test_real_entries_are_not_marked_planned(client_as):
    side = _side(client_as("admin").get("/").text)
    users = re.search(r'<a class="nav-i[^>]*href="/admin/utilisateurs".*?</a>', side, re.S).group(0)
    assert "À venir" not in users
    assert " soon" not in users


def test_planned_page_shows_no_fake_data(client_as):
    response = client_as("admin").get("/a-venir/statistiques")
    assert response.status_code == 200
    assert "Ce module n’est pas encore disponible" in response.text
    main = response.text[response.text.index('id="contenu"') : response.text.index('class="foot"')]
    for marker in ("<table", "<form", "<input", 'class="kpi'):
        assert marker not in main


def test_planned_page_checks_the_permission(client_as):
    # Enrichissement (L5), import et paramètres (L6) sont livrés : l'inventaire, encore à venir, sert d'exemple
    assert client_as("viewer").get("/a-venir/inventaire").status_code == 403
    assert client_as("volunteer").get("/a-venir/inventaire").status_code == 200
    for delivered in ("enrichissement", "imports", "parametres"):
        assert client_as("librarian").get(f"/a-venir/{delivered}").status_code == 404
    assert client_as("volunteer").get("/parametres").status_code == 403
    assert client_as("librarian").get("/parametres").status_code == 200


def test_unknown_planned_page_is_404(client_as):
    assert client_as("admin").get("/a-venir/inconnu").status_code == 404


def test_every_planned_module_has_a_page(client_as):
    browser = client_as("admin")
    for module in PLANNED:
        assert browser.get(module.path).status_code == 200, module.code


def test_current_entry_is_marked(client_as):
    side = _side(client_as("admin").get("/admin/utilisateurs").text)
    assert side.count('aria-current="page"') == 1
    assert re.search(r'href="/admin/utilisateurs"[^>]*aria-current="page"', side)


def test_sub_pages_keep_the_entry_current(client_as):
    side = _side(client_as("admin").get("/admin/utilisateurs/nouveau").text)
    assert re.search(r'href="/admin/utilisateurs"[^>]*aria-current="page"', side)


def test_groups_keep_their_order(client_as):
    side = _side(client_as("admin").get("/").text)
    positions = [side.index(label) for label in ("Pilotage", "Collection", "Circulation", "Maintenance")]
    assert positions == sorted(positions)


def test_quick_actions_are_filtered_and_lead_to_the_planned_page(client_as):
    librarian = client_as("librarian").get("/").text
    top = librarian[librarian.index('class="top"') : librarian.index('id="contenu"')]
    assert 'href="/comptoir/retours"' in top
    assert 'href="/comptoir"' in top
    assert 'href="/catalogue/notices/nouvelle"' in top  # l'ajout d'un livre existe : il n'est plus « à venir »
    assert 'aria-label="Ajouter, à venir"' not in top
    viewer = client_as("viewer").get("/").text
    viewer_top = viewer[viewer.index('class="top"') : viewer.index('id="contenu"')]
    assert "Prêt" not in viewer_top
    assert "Ajouter" not in viewer_top


class TestProduction:
    @pytest.fixture
    def production(self, settings, client_as):
        browser = client_as("admin")
        settings.env = "production"
        with TestClient(create_app(settings), raise_server_exceptions=False, follow_redirects=False) as test_client:
            test_client.cookies.update(browser.cookies)
            test_client.headers["X-CSRF-Token"] = browser.headers["X-CSRF-Token"]
            yield test_client

    def test_planned_modules_are_hidden(self, production):
        html = production.get("/").text
        assert _entries(html) == {
            "Tableau de bord",
            "Catalogue",
            "Séries & collections",
            "Étagères",
            "Prêts & retours",
            "Lecteurs & familles",
            "Enrichissement",  # lot L5
            "Import & export",  # lot L6
            "Utilisateurs",
            "Paramètres",  # lot L6, finalisation
            "À propos",
        }
        assert 'href="/catalogue/notices/nouvelle"' in html  # une action réelle reste proposée en production
        assert "/a-venir/" not in html
        assert "À venir" not in html

    def test_planned_pages_are_404(self, production):
        assert production.get("/a-venir/statistiques").status_code == 404

    def test_no_dead_link(self, production):
        html = production.get("/").text
        links = {href for href in re.findall(r'href="(/[^"]*)"', html) if not href.startswith("/static/")}
        assert links
        for link in sorted(links):
            assert production.get(link).status_code == 200, link

    def test_explicit_setting_can_show_them(self, settings):
        settings.show_planned_modules = True
        assert settings.planned_modules_visible


def test_fictional_module_appears_in_place_without_touching_the_layout(settings, client_as):
    router = APIRouter()

    @router.get("/essai", dependencies=[require("essai.voir")])
    def page() -> str:
        return "ok"

    module = Module(
        "essai",
        "Essai",
        router=router,
        permissions=(Permission("essai.voir", "Voir"),),
        nav=(NavEntry("Module d’essai", "/essai", "star", "enr", "collection", order=15, permission="essai.voir"),),
    )
    app = create_app(settings, modules=[*all_modules(), module])
    admin = client_as("admin")
    viewer = client_as("viewer")
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.cookies.update(admin.cookies)
        side = _side(test_client.get("/").text)
        assert side.index("Catalogue") < side.index("Module d’essai") < side.index("Séries &amp; collections")
        test_client.cookies.clear()
        test_client.cookies.update(viewer.cookies)
        assert "Module d’essai" not in test_client.get("/").text


def test_entry_without_permission_is_never_built(settings):
    modules = [Module("a", "A", nav=(NavEntry("Secret", "/secret", "key", "io", "pilotage", permission="a.voir"),))]
    assert "/secret" not in {item.path for item in build_navigation(modules, _auth(set()), "/", settings).all_items}
    allowed = build_navigation(modules, _auth({"a.voir"}), "/", settings)
    assert "/secret" in {item.path for item in allowed.all_items}


def test_planned_module_codes_are_unique():
    assert len(PLANNED_BY_CODE) == len(PLANNED)
    for module in PLANNED:
        assert module.summary
        assert module.permission


def test_declared_shortcuts_are_listed_and_unique(client_as):
    html = client_as("admin").get("/").text
    side = _side(html)
    shortcuts = re.findall(r'data-goto="(\w)"', side)
    assert len(shortcuts) == len(set(shortcuts))
    help_box = html[html.index('id="raccourcis"') :]
    for letter in shortcuts:
        assert f"<kbd>{letter.upper()}</kbd>" in help_box
