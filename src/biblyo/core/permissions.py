# SPDX-License-Identifier: EUPL-1.2
"""Contrôle d'accès côté serveur : `require()` sur les routes, `can()` dans les gabarits.

Fermé par défaut : `create_app` refuse de démarrer si une route n'est ni protégée ni déclarée publique.
Masquer un bouton n'est jamais une protection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.routing import iter_route_contexts
from starlette.routing import Mount

from biblyo.core.errors import LoginRequired, PasswordChangeRequired, PermissionDenied
from biblyo.core.modules import ModuleError
from biblyo.core.security.sessions import AuthContext, get_auth

SESSION_ONLY = ""  # marqueur : une session suffit, sans permission particulière


def _guard(permission: str, *, allow_pending_password: bool) -> Callable[[Request], AuthContext]:
    def dependency(request: Request) -> AuthContext:
        auth = get_auth(request)
        if auth is None:
            raise LoginRequired
        if auth.must_change_password and not allow_pending_password:
            raise PasswordChangeRequired
        if permission and not auth.can(permission):
            raise PermissionDenied
        return auth

    dependency.__permission__ = permission  # type: ignore[attr-defined]
    return dependency


def require(permission: str) -> Any:
    """Dépendance : session valide et permission `module.action`."""
    return Depends(_guard(permission, allow_pending_password=False))


def require_session(*, allow_pending_password: bool = False) -> Any:
    """Dépendance : session valide, quel que soit le rôle."""
    return Depends(_guard(SESSION_ONLY, allow_pending_password=allow_pending_password))


def _permissions_of(dependant: Any) -> list[str]:
    found: list[str] = []
    pending = list(dependant.dependencies)
    while pending:
        current = pending.pop()
        marker = getattr(current.call, "__permission__", None)
        if marker is not None:
            found.append(marker)
        pending.extend(current.dependencies)
    return found


def route_table(app: FastAPI) -> list[tuple[Any, list[str]]]:
    """Toutes les routes réelles et leurs protections, routeurs inclus. Une route non FastAPI compte comme nue."""
    table: list[tuple[Any, list[str]]] = []
    for context in iter_route_contexts(app.routes):
        if isinstance(context.original_route, Mount):
            continue  # fichiers statiques : ils ne passent pas par les routeurs et ne modifient rien
        dependant = getattr(context, "dependant", None)
        table.append((context, _permissions_of(dependant) if dependant is not None else []))
    return table


def assert_routes_protected(app: FastAPI, public_paths: Iterable[str], known_permissions: Iterable[str]) -> None:
    """Refuse le démarrage sur une route sans protection, ou protégée par une permission qui n'existe pas."""
    public = set(public_paths)
    known = set(known_permissions)
    table = route_table(app)
    if not table:
        raise ModuleError("Aucune route trouvée : le contrôle des protections ne peut pas s'exécuter.")
    for route, permissions in table:
        if route.path in public:
            continue
        if not permissions:
            raise ModuleError(f"Route sans protection : {route.path}")
        unknown = {p for p in permissions if p and p not in known}
        if unknown:
            raise ModuleError(f"Route {route.path} : permission non déclarée {sorted(unknown)}")
