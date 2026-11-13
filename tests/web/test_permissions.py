# SPDX-License-Identifier: EUPL-1.2
"""Permissions contrôlées côté serveur : matrice rôle × route, fermé par défaut."""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from biblyo.app import CORE_PUBLIC_PATHS, create_app
from biblyo.core.modules import Module, ModuleError, Permission
from biblyo.core.permissions import require, require_session, route_table
from biblyo.modules.registry import all_modules

ROLES = ("admin", "librarian", "volunteer", "viewer")
ADMIN_ONLY = (
    ("GET", "/admin/utilisateurs"),
    ("GET", "/admin/utilisateurs/nouveau"),
    ("POST", "/admin/utilisateurs/nouveau"),
    ("GET", "/admin/utilisateurs/{id}"),
    ("POST", "/admin/utilisateurs/{id}"),
    ("POST", "/admin/utilisateurs/{id}/mot-de-passe"),
)
ANY_ROLE = (("GET", "/"), ("GET", "/compte"))


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY)
def test_user_administration_matrix(client_as, role, method, path):
    browser = client_as(role)
    target = path.format(id=browser.user_id)
    data = {"nom": "Essai", "identifiant": "essai", "roles": ["viewer"], "actif": "1"} if method == "POST" else None
    response = browser.request(method, target, data=data)
    if role == "admin":
        assert response.status_code in (200, 303, 400), f"{role} {method} {target}: {response.status_code}"
    else:
        assert response.status_code == 403, f"{role} {method} {target}: {response.status_code}"
        assert "Accès refusé" in response.text


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(("method", "path"), ANY_ROLE)
def test_pages_for_any_connected_role(client_as, role, method, path):
    assert client_as(role).request(method, path).status_code == 200


def test_volunteer_refused_on_direct_url(client_as, settings):
    """Critère du PC2 : refus par appel direct, sans passer par un bouton masqué."""
    browser = client_as("volunteer", username="camille")
    target = client_as("admin", username="dominique")
    assert browser.get("/admin/utilisateurs").status_code == 403
    response = browser.post(
        f"/admin/utilisateurs/{browser.user_id}",
        data={"nom": "Camille", "roles": ["admin"], "actif": "1"},
    )
    assert response.status_code == 403
    assert browser.post(f"/admin/utilisateurs/{target.user_id}/mot-de-passe").status_code == 403
    connection = sqlite3.connect(settings.database_path)
    roles = connection.execute(
        "SELECT code FROM roles JOIN user_roles ON roles.id = role_id WHERE user_id = ?", (browser.user_id,)
    ).fetchall()
    connection.close()
    assert roles == [("volunteer",)]


def test_anonymous_is_refused_on_every_protected_route(app, client, client_as):
    token = client_as("admin").headers["X-CSRF-Token"]
    for route, permissions in route_table(app):
        if not permissions:
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            client.cookies.set("biblyo_csrf", token)
            response = client.request(method, path, headers={"X-CSRF-Token": token})
            assert response.status_code == 303, f"{method} {path}: {response.status_code}"
            assert response.headers["location"].startswith("/connexion")


def test_public_routes_are_exactly_the_declared_list(app):
    public = {route.path for route, permissions in route_table(app) if not permissions}
    # Le logo de la bibliothèque est public comme son nom : il s’affiche aussi avant la connexion (lot L6)
    assert public == {"/sante", "/a-propos", "/connexion", "/installation", "/identite/logo.webp"}
    assert set(CORE_PUBLIC_PATHS) <= public


def test_every_permission_used_is_declared(app):
    declared = {p.code for m in app.state.modules for p in m.permissions}
    used = {p for _, permissions in route_table(app) for p in permissions if p}
    assert used
    assert used <= declared


def test_app_refuses_an_unprotected_route(settings):
    router = APIRouter()

    @router.get("/oubliee")
    def forgotten() -> None:
        return None

    with pytest.raises(ModuleError, match="sans protection"):
        create_app(settings, modules=[Module("essai", "Essai", router=router)])


def test_app_refuses_an_undeclared_permission(settings):
    router = APIRouter()

    @router.get("/route", dependencies=[require("essai.inconnue")])
    def route() -> None:
        return None

    with pytest.raises(ModuleError, match="non déclarée"):
        create_app(settings, modules=[Module("essai", "Essai", router=router)])


def test_new_module_route_is_enforced_without_more_code(settings, client_as):
    router = APIRouter()

    @router.get("/essai", dependencies=[require("essai.voir")])
    def route() -> str:
        return "vu"

    module = Module("essai", "Essai", router=router, permissions=(Permission("essai.voir", "Voir"),))
    app = create_app(settings, modules=[*all_modules(), module])
    volunteer = client_as("volunteer")
    admin = client_as("admin")
    with TestClient(app, follow_redirects=False) as test_client:
        test_client.cookies.update(volunteer.cookies)
        assert test_client.get("/essai").status_code == 403
        test_client.cookies.clear()
        test_client.cookies.update(admin.cookies)
        assert test_client.get("/essai").status_code == 200


def test_session_only_marker_is_not_a_permission():
    router = APIRouter()

    @router.get("/s", dependencies=[require_session()])
    def route() -> None:
        return None

    assert route is not None


def _role_permissions(settings, code):
    connection = sqlite3.connect(settings.database_path)
    rows = connection.execute(
        "SELECT permission FROM role_permissions JOIN roles ON roles.id = role_id WHERE code = ?", (code,)
    ).fetchall()
    connection.close()
    return {row[0] for row in rows}


def test_provided_roles_match_the_specification(settings):
    assert _role_permissions(settings, "admin") == {"*"}
    assert _role_permissions(settings, "viewer") == {"catalogue.view"}
    assert _role_permissions(settings, "volunteer") == {
        "catalogue.view",
        "patrons.view",
        "circulation.lend",
        "circulation.renew",
        "inventory.run",
        "holdings.edit",
        "patrons.register",  # révision 0007 : inscrire, sans voir les coordonnées ensuite
    }
    librarian = _role_permissions(settings, "librarian")
    assert not librarian & {"admin.users", "admin.roles", "admin.modules", "admin.backups", "*"}
    assert {"patrons.register", "patrons.export", "patrons.view_personal", "patrons.edit"} <= librarian
    assert "patrons.anonymize" not in librarian  # réservée à l'administrateur habilité (L3, D12)
    assert {"admin.settings", "admin.identity", "admin.sources", "admin.audit", "circulation.override"} <= librarian
