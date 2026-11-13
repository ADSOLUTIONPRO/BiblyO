# SPDX-License-Identifier: EUPL-1.2
"""CSRF : jeton de session une fois connecté, double envoi par cookie sur les pages anonymes.

Appliqué par une dépendance globale de l'application : aucune route qui modifie quelque chose n'y échappe.
"""

from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from biblyo.config import Settings
from biblyo.core.errors import CsrfFailed
from biblyo.core.security.sessions import get_auth

COOKIE_NAME = "biblyo_csrf"
FIELD_NAME = "_csrf"
HEADER_NAME = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def _supplied_token(request: Request) -> str | None:
    header = request.headers.get(HEADER_NAME)
    if header:
        return header
    try:
        form = await request.form()
    except HTTPException:
        raise  # corps trop volumineux : refusé comme tel
    except Exception as exc:  # corps illisible : traité comme une requête sans jeton
        raise CsrfFailed from exc
    value = form.get(FIELD_NAME)
    return value if isinstance(value, str) else None


async def csrf_protect(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    auth = await run_in_threadpool(get_auth, request)
    expected = auth.csrf_token if auth else request.cookies.get(COOKIE_NAME)
    supplied = await _supplied_token(request)
    if not expected or not supplied or not secrets.compare_digest(expected.encode(), supplied.encode()):
        raise CsrfFailed


def token_for_template(request: Request) -> str:
    auth = get_auth(request)
    if auth:
        return auth.csrf_token
    token = request.cookies.get(COOKIE_NAME) or getattr(request.state, "new_csrf_token", None)
    if token is None:
        token = secrets.token_urlsafe(32)
        request.state.new_csrf_token = token
    return token


def set_cookie_if_new(request: Request, response: Response, settings: Settings) -> None:
    token = getattr(request.state, "new_csrf_token", None)
    if token:
        response.set_cookie(COOKIE_NAME, token, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure)
