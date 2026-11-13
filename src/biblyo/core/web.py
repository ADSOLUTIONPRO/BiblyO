# SPDX-License-Identifier: EUPL-1.2
"""Raccords entre la sécurité et les gabarits : ce que chaque page reçoit, ce que chaque réponse pose."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from biblyo.core import flash
from biblyo.core.htmx import is_htmx
from biblyo.core.navigation import navigation_for
from biblyo.core.security import csrf, sessions
from biblyo.core.settings_store import get_setting
from biblyo.core.text import initials

log = logging.getLogger("biblyo.web")

THEME_COOKIE = "biblyo_theme"
THEMES = frozenset({"light", "dark"})
PRODUCT_NAME = "biblyO"


def _library_name(request: Request) -> tuple[str, str]:
    """Nom de la bibliothèque et empreinte de son logo (vide s'il n'y en a pas)."""
    app = request.app
    if not app.state.schema.current().ok:
        return PRODUCT_NAME, ""
    try:
        with app.state.database.session() as session:
            name = str(get_setting(session, "library.name", PRODUCT_NAME) or PRODUCT_NAME)
            return name, str(get_setting(session, "library.logo_sha", "") or "")
    except SQLAlchemyError:
        log.exception("Lecture du nom de la bibliothèque impossible")
        return PRODUCT_NAME, ""


def _layout(request: Request, authenticated: bool) -> str:
    if is_htmx(request):
        return "layouts/fragment.html"
    return "layouts/app.html" if authenticated else "layouts/auth.html"


def page_context(request: Request) -> dict[str, Any]:
    auth = sessions.get_auth(request)

    def can(permission: str) -> bool:
        return auth is not None and auth.can(permission)

    library, logo_sha = _library_name(request)
    theme = request.cookies.get(THEME_COOKIE)
    settings = request.app.state.settings
    return {
        "user": auth,
        "can": can,
        "csrf_token": csrf.token_for_template(request),
        "flash": flash.read_flash(request),
        "layout": _layout(request, auth is not None),
        "nav": navigation_for(request, auth),
        "library_name": library,
        "library_logo": f"/identite/logo.webp?v={logo_sha[:12]}" if logo_sha else "",
        "library_initials": initials(library)[:2].upper() or "B",
        "user_initials": initials(auth.display_name)[:2].upper() if auth else "",
        "theme": theme if theme in THEMES else None,
        "source_url": settings.source_url,
        "cookie_secure": settings.cookie_secure,
    }


def finish_page(request: Request, response: HTMLResponse) -> None:
    settings = request.app.state.settings
    csrf.set_cookie_if_new(request, response, settings)
    flash.clear_if_read(request, response, settings)
    refreshed = getattr(request.state, "refresh_session_cookie", None)
    if refreshed:
        sessions.set_cookie(response, refreshed, settings)
    response.headers.append("Vary", "HX-Request")
