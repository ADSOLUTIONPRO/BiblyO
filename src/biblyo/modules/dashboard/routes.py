# SPDX-License-Identifier: EUPL-1.2
"""Accueil, et page des modules à venir. Rien de fictif : seules des données réelles sont affichées."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from biblyo.core.backup import list_backups
from biblyo.core.clock import now
from biblyo.core.errors import PermissionDenied
from biblyo.core.i18n import TIMEZONE, _
from biblyo.core.models import User
from biblyo.core.navigation import PLANNED_BY_CODE
from biblyo.core.permissions import require_session
from biblyo.core.security.sessions import AuthContext

router = APIRouter()
Session = Annotated[AuthContext, require_session()]
EVENING_HOUR = 18


def _admin_figures(request: Request, auth: AuthContext) -> dict[str, Any]:
    figures: dict[str, Any] = {}
    if auth.can("admin.users"):
        with request.app.state.database.session() as session:
            statement = select(func.count()).select_from(User).where(User.is_active == True)  # noqa: E712
            figures["active_users"] = session.scalar(statement)
    if auth.can("admin.backups"):
        archives = list_backups(request.app.state.settings)
        figures["backups"] = len(archives)
        figures["last_backup"] = datetime.fromtimestamp(archives[0].stat().st_mtime, tz=UTC) if archives else None
    return figures


def _module_sections(request: Request, auth: AuthContext) -> dict[str, Any]:
    """Blocs de l'accueil, fournis par chaque module selon les droits du compte."""
    context: dict[str, Any] = {}
    with request.app.state.database.session() as session:
        for module in request.app.state.modules:
            provider = module.extras.get("dashboard")
            if provider:
                context.update(provider(session, auth))
    return context


@router.get("/", include_in_schema=False)
def home(request: Request, auth: Session) -> HTMLResponse:
    moment = now().astimezone(TIMEZONE)
    first_name = auth.display_name.split()[0] if auth.display_name.split() else auth.username
    context = {
        **_module_sections(request, auth),
        "figures": _admin_figures(request, auth),
        "greeting": _("Bonjour, {name}").format(name=first_name)
        if moment.hour < EVENING_HOUR
        else _("Bonsoir, {name}").format(name=first_name),
        "today": moment,
    }
    return request.app.state.templates.render(request, "dashboard/pages/home.html", context)


@router.get("/a-venir/{code}", include_in_schema=False)
def planned(request: Request, code: str, auth: Session) -> HTMLResponse:
    module = PLANNED_BY_CODE.get(code)
    if module is None or not request.app.state.settings.planned_modules_visible:
        raise HTTPException(status_code=404)
    if not auth.can(module.permission):
        raise PermissionDenied
    return request.app.state.templates.render(request, "dashboard/pages/planned.html", {"module": module})
