# SPDX-License-Identifier: EUPL-1.2
"""Connexion, installation, compte et utilisateurs. Les routes décident de la réponse, jamais d'une règle."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from biblyo.core import flash
from biblyo.core.clock import now_iso
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _
from biblyo.core.permissions import require, require_session
from biblyo.core.security import install as install_tokens
from biblyo.core.security import sessions
from biblyo.core.security.ratelimit import client_ip
from biblyo.core.security.sessions import AuthContext
from biblyo.core.useragent import describe_agent
from biblyo.modules.admin import repository as repo
from biblyo.modules.admin import service

router = APIRouter()

LOGIN_ERROR = _("Identifiant ou mot de passe incorrect.")
TOO_MANY_ATTEMPTS = _("Trop de tentatives. Réessayez dans quelques minutes.")
LOGIN_KEY_MAX_LENGTH = 64  # longueur maximale d'un identifiant : la mémoire du limiteur reste bornée

Field = Annotated[str, Form()]
Session = Annotated[AuthContext, require_session()]
SessionPendingPassword = Annotated[AuthContext, require_session(allow_pending_password=True)]
UserAdmin = Annotated[AuthContext, require("admin.users")]


def _render(request: Request, template: str, context: dict[str, Any] | None = None, status: int = 200) -> HTMLResponse:
    return request.app.state.templates.render(request, template, context or {}, status_code=status)


def _ip(request: Request) -> str:
    return client_ip(request, request.app.state.settings.trusted_proxy_list)


def _agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _login_key(ip: str, username: str) -> str:
    """Clé de limitation : l'adresse et l'identifiant visé, car plusieurs postes peuvent partager une adresse."""
    return f"{ip}|{username.strip().casefold()[:LOGIN_KEY_MAX_LENGTH]}"


def _go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _safe_next(target: str) -> str:
    """Seule une adresse interne est acceptée après la connexion : pas de redirection ouverte."""
    if any(ord(character) < 32 for character in target):  # un navigateur retire tabulations et sauts de ligne
        return "/"
    parts = urlsplit(target)
    if not target.startswith("/") or target.startswith(("//", "/\\")) or parts.scheme or parts.netloc:
        return "/"
    return target


# --- À propos ---------------------------------------------------------------------------------------------------------
@router.get("/a-propos", include_in_schema=False)
def about(request: Request) -> HTMLResponse:
    return _render(request, "admin/pages/about.html")


# --- Connexion --------------------------------------------------------------------------------------------------------
@router.get("/connexion", include_in_schema=False)
def login_page(request: Request, suivant: str = "/") -> Response:
    if sessions.get_auth(request):
        return _go("/")
    with request.app.state.database.session() as session:
        if install_tokens.is_open(session):
            return _go("/installation")
    return _render(request, "admin/pages/login.html", {"next": _safe_next(suivant), "error": None, "username": ""})


@router.post("/connexion", include_in_schema=False)
def login_submit(request: Request, identifiant: Field = "", mot_de_passe: Field = "", suivant: Field = "/") -> Response:
    app = request.app
    ip = _ip(request)
    target = _safe_next(suivant)
    limiter = app.state.login_limiter
    address_limiter = app.state.login_address_limiter
    key = _login_key(ip, identifiant)
    if limiter.blocked(key) or address_limiter.blocked(ip):
        context = {"next": target, "error": TOO_MANY_ATTEMPTS, "username": identifiant}
        return _render(request, "admin/pages/login.html", context, status=429)
    user_id = service.authenticate(app.state.database, identifiant.strip(), mot_de_passe, ip)
    if user_id is None:
        limiter.record(key)
        address_limiter.record(ip)
        context = {"next": target, "error": LOGIN_ERROR, "username": identifiant}
        return _render(request, "admin/pages/login.html", context, status=400)
    limiter.reset(key)  # le plafond par adresse reste : une connexion réussie ne l'efface pas
    raw = service.open_session(app.state.database, user_id, app.state.settings, ip, _agent(request))
    response = _go(target)
    sessions.set_cookie(response, raw, app.state.settings)
    return response


@router.post("/deconnexion", include_in_schema=False)
def logout(request: Request, auth: SessionPendingPassword) -> Response:
    service.close_session(request.app.state.database, auth.session_id, auth.user_id)
    response = _go("/connexion")
    sessions.clear_cookie(response, request.app.state.settings)
    return response


# --- Première installation --------------------------------------------------------------------------------------------
@router.get("/installation", include_in_schema=False)
def install_page(request: Request) -> Response:
    database = request.app.state.database
    with database.session() as session:
        if not install_tokens.is_open(session):
            return _go("/connexion")
    install_tokens.announce_token_if_needed(database)
    return _render(request, "admin/pages/install.html", {"errors": {}, "values": {}})


@router.post("/installation", include_in_schema=False)
def install_submit(
    request: Request,
    jeton: Field = "",
    bibliotheque: Field = "",
    nom: Field = "",
    identifiant: Field = "",
    mot_de_passe: Field = "",
    confirmation: Field = "",
) -> Response:
    app = request.app
    ip = _ip(request)
    limiter = app.state.install_limiter
    values = {"bibliotheque": bibliotheque, "nom": nom, "identifiant": identifiant}
    if limiter.blocked(ip):
        errors = {"token": TOO_MANY_ATTEMPTS}
        return _render(request, "admin/pages/install.html", {"errors": errors, "values": values}, status=429)
    form = service.InstallForm(jeton, bibliotheque, nom, identifiant, mot_de_passe, confirmation)
    try:
        raw = service.install(app.state.database, form, app.state.settings, ip, _agent(request))
    except service.InstallationClosed:
        return _go("/connexion")
    except FormErrors as exc:
        if "token" in exc.fields:
            limiter.record(ip)
        return _render(request, "admin/pages/install.html", {"errors": exc.fields, "values": values}, status=400)
    response = _go("/")
    sessions.set_cookie(response, raw, app.state.settings)
    flash.set_flash(response, "installed", app.state.settings)
    return response


# --- Mon compte -------------------------------------------------------------------------------------------------------
def _account_context(request: Request, auth: AuthContext, **extra: Any) -> dict[str, Any]:
    with request.app.state.database.session() as session:
        rows = repo.list_sessions(session, auth.user_id, now_iso())
        session_rows = [
            {
                "id": row.id,
                "created_at": row.created_at,
                "last_seen_at": row.last_seen_at,
                "ip": row.ip,
                "browser": describe_agent(row.user_agent),
                "current": row.id == auth.session_id,
            }
            for row in rows
        ]
    return {"sessions": session_rows, "profile_errors": {}, "password_errors": {}, **extra}


@router.get("/compte", include_in_schema=False)
def account_page(request: Request, auth: SessionPendingPassword) -> HTMLResponse:
    return _render(request, "admin/pages/account.html", _account_context(request, auth))


@router.post("/compte/profil", include_in_schema=False)
def account_profile(request: Request, auth: SessionPendingPassword, nom: Field = "") -> Response:
    try:
        service.update_profile(request.app.state.database, auth.user_id, nom)
    except FormErrors as exc:
        context = _account_context(request, auth, profile_errors=exc.fields)
        return _render(request, "admin/pages/account.html", context, status=400)
    response = _go("/compte")
    flash.set_flash(response, "profile_updated", request.app.state.settings)
    return response


@router.post("/compte/mot-de-passe", include_in_schema=False)
def account_password(
    request: Request, auth: SessionPendingPassword, actuel: Field = "", nouveau: Field = "", confirmation: Field = ""
) -> Response:
    app = request.app
    try:
        raw = service.change_password(
            app.state.database,
            auth.user_id,
            actuel,
            nouveau,
            confirmation,
            app.state.settings,
            _ip(request),
            _agent(request),
        )
    except FormErrors as exc:
        context = _account_context(request, auth, password_errors=exc.fields)
        return _render(request, "admin/pages/account.html", context, status=400)
    response = _go("/compte")
    sessions.set_cookie(response, raw, app.state.settings)
    flash.set_flash(response, "password_changed", app.state.settings)
    return response


@router.post("/compte/sessions/{session_id}/fermer", include_in_schema=False)
def account_close_session(request: Request, session_id: int, auth: SessionPendingPassword) -> Response:
    service.close_session(request.app.state.database, session_id, auth.user_id)  # limité à ses propres sessions
    response = _go("/connexion" if session_id == auth.session_id else "/compte")
    if session_id == auth.session_id:
        sessions.clear_cookie(response, request.app.state.settings)
    else:
        flash.set_flash(response, "session_revoked", request.app.state.settings)
    return response


# --- Utilisateurs -----------------------------------------------------------------------------------------------------
@router.get("/admin/utilisateurs", include_in_schema=False)
def users_page(request: Request, _auth: UserAdmin) -> HTMLResponse:
    with request.app.state.database.session() as session:
        users = [
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "is_active": u.is_active,
                "must_change_password": u.must_change_password,
                "last_login_at": u.last_login_at,
                "roles": [r.label for r in u.roles],
            }
            for u in repo.list_users(session)
        ]
    return _render(request, "admin/pages/users.html", {"users": users})


def _roles_for_form(request: Request) -> list[dict[str, str]]:
    with request.app.state.database.session() as session:
        return [{"code": r.code, "label": r.label} for r in repo.list_roles(session)]


@router.get("/admin/utilisateurs/nouveau", include_in_schema=False)
def user_new_page(request: Request, _auth: UserAdmin) -> HTMLResponse:
    context = {"roles": _roles_for_form(request), "errors": {}, "values": {"selected": []}, "account": None}
    return _render(request, "admin/pages/user_form.html", context)


@router.post("/admin/utilisateurs/nouveau", include_in_schema=False)
def user_create(
    request: Request,
    auth: UserAdmin,
    identifiant: Field = "",
    nom: Field = "",
    roles: Annotated[list[str] | None, Form()] = None,
) -> Response:
    chosen = roles or []
    try:
        temporary = service.create_user(request.app.state.database, auth.user_id, identifiant, nom, chosen)
    except FormErrors as exc:
        context = {
            "roles": _roles_for_form(request),
            "errors": exc.fields,
            "values": {"identifiant": identifiant, "nom": nom, "selected": chosen},
            "account": None,
        }
        return _render(request, "admin/pages/user_form.html", context, status=400)
    context = {"username": identifiant.strip(), "temporary": temporary}
    return _render(request, "admin/pages/user_password.html", context)


def _load_account(request: Request, user_id: int) -> dict[str, Any]:
    with request.app.state.database.session() as session:
        account = repo.get_user(session, user_id)
        if account is None:
            raise HTTPException(status_code=404)
        return {
            "id": account.id,
            "username": account.username,
            "display_name": account.display_name,
            "is_active": account.is_active,
            "roles": [r.code for r in account.roles],
        }


@router.get("/admin/utilisateurs/{user_id}", include_in_schema=False)
def user_edit_page(request: Request, user_id: int, _auth: UserAdmin) -> HTMLResponse:
    account = _load_account(request, user_id)
    context = {
        "roles": _roles_for_form(request),
        "errors": {},
        "values": {"nom": account["display_name"], "selected": account["roles"]},
        "account": account,
    }
    return _render(request, "admin/pages/user_form.html", context)


@router.post("/admin/utilisateurs/{user_id}", include_in_schema=False)
def user_update(
    request: Request,
    user_id: int,
    auth: UserAdmin,
    nom: Field = "",
    actif: Field = "",
    roles: Annotated[list[str] | None, Form()] = None,
) -> Response:
    account = _load_account(request, user_id)
    chosen = roles or []
    active = actif == "1"
    try:
        service.update_user(request.app.state.database, auth.user_id, user_id, nom, chosen, active)
    except (FormErrors, BusinessError) as exc:
        errors = exc.fields if isinstance(exc, FormErrors) else {"form": exc.message}
        context = {
            "roles": _roles_for_form(request),
            "errors": errors,
            "values": {"nom": nom, "selected": chosen},
            "account": {**account, "is_active": active},
        }
        return _render(request, "admin/pages/user_form.html", context, status=400)
    response = _go("/admin/utilisateurs")
    flash.set_flash(response, "user_updated", request.app.state.settings)
    return response


@router.post("/admin/utilisateurs/{user_id}/mot-de-passe", include_in_schema=False)
def user_reset_password(request: Request, user_id: int, auth: UserAdmin) -> HTMLResponse:
    account = _load_account(request, user_id)
    temporary = service.reset_password(request.app.state.database, auth.user_id, user_id)
    context = {"username": account["username"], "temporary": temporary}
    return _render(request, "admin/pages/user_password.html", context)
