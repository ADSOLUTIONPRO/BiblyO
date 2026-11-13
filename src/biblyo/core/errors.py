# SPDX-License-Identifier: EUPL-1.2
"""Erreurs de l'application et leurs réponses : toujours en français, jamais de détail technique."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from biblyo.core.security.headers import SECURITY_HEADERS

if TYPE_CHECKING:
    from biblyo.core.templating import Templates

log = logging.getLogger("biblyo.errors")


class BusinessError(Exception):
    """Règle métier refusée : le message s'adresse à l'utilisateur."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FormErrors(BusinessError):
    """Saisie refusée : un message par champ, à réafficher dans le formulaire."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__(next(iter(fields.values()), ""))
        self.fields = fields


class LoginRequired(Exception):
    """Aucune session valide : redirection vers la connexion ou l'installation."""


class PasswordChangeRequired(Exception):
    """Le compte doit changer son mot de passe avant de continuer."""


class PermissionDenied(Exception):
    """L'utilisateur connecté n'a pas la permission demandée."""


class CsrfFailed(Exception):
    """Jeton CSRF absent ou incorrect."""


def _redirect(request: Request, path: str) -> Response:
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": path})
    return RedirectResponse(path, status_code=303)


def install_error_handlers(app: FastAPI, templates: Templates) -> None:
    def page(request: Request, template: str, status: int) -> HTMLResponse:
        return templates.render(request, template, {}, status_code=status)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _: LoginRequired) -> Response:
        target = "/connexion"
        if request.method == "GET" and not request.headers.get("HX-Request") and request.url.path != "/":
            target = f"/connexion?suivant={request.url.path}"
        return _redirect(request, target)

    @app.exception_handler(PasswordChangeRequired)
    async def _password_change(request: Request, _: PasswordChangeRequired) -> Response:
        return _redirect(request, "/compte")

    @app.exception_handler(PermissionDenied)
    async def _denied(request: Request, _: PermissionDenied) -> Response:
        return page(request, "errors/403.html", 403)

    @app.exception_handler(CsrfFailed)
    async def _csrf(request: Request, _: CsrfFailed) -> Response:
        return page(request, "errors/403_csrf.html", 403)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, _exc: RequestValidationError) -> Response:
        return HTMLResponse("Requête invalide.", status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return page(request, "errors/404.html", 404)
        if exc.status_code == 405:
            return HTMLResponse("Méthode non autorisée.", status_code=405, headers=exc.headers)
        return HTMLResponse("Requête refusée.", status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> Response:
        log.exception("Erreur inattendue sur %s %s", request.method, request.url.path, exc_info=exc)
        # Ce gestionnaire s'exécute hors des middlewares : les en-têtes de sécurité sont posés ici.
        response = page(request, "errors/500.html", 500)
        response.headers.update(SECURITY_HEADERS)
        response.headers["Cache-Control"] = "no-store"
        return response
