# SPDX-License-Identifier: EUPL-1.2
"""Écran « Paramètres » (lot L6, finalisation) : identité, logo, sauvegardes, contrôle de tous les réglages."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from biblyo.core import audit, flash, rubrics
from biblyo.core.backup import BackupError, create_backup, verify_backup
from biblyo.core.errors import BusinessError, FormErrors, PermissionDenied
from biblyo.core.i18n import _
from biblyo.core.media import MAX_UPLOAD_BYTES
from biblyo.core.permissions import require, require_session
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.admin import parametres

router = APIRouter()
SignedIn = Annotated[AuthContext, require_session()]
CanIdentity = Annotated[AuthContext, require("admin.identity")]
CanBackup = Annotated[AuthContext, require("admin.backups")]


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


PAGES: dict[str, str] = {
    "": "admin/pages/settings.html",
    "bibliotheque": "admin/pages/settings_library.html",
    "catalogue": "admin/pages/settings_catalogue.html",
    "prets": "admin/pages/settings_loans.html",
    "sauvegardes": "admin/pages/settings_backups.html",
    "utilisateurs": "admin/pages/settings_users.html",
    "avance": "admin/pages/settings_advanced.html",
}


def _go(request: Request, code: str, rubric: str = "") -> Response:
    response = RedirectResponse(f"/parametres/{rubric}" if rubric else "/parametres", status_code=303)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def _summaries(session: Session, overview: parametres.Overview) -> dict[str, str]:
    """Une phrase utile par rubrique, sur la page d'accueil des paramètres."""
    from biblyo.modules.enrichment.sources import contact_of

    me = parametres.identity(session)
    todo = sum(1 for check in overview.checks if check.state == "todo")
    sources = session.execute(text("SELECT code FROM metadata_sources WHERE is_enabled = 1 ORDER BY priority")).all()
    labels = {"bnf": "BnF", "sudoc": "Sudoc", "openlibrary": "Open Library"}
    users = int(session.execute(text("SELECT COUNT(*) FROM users WHERE is_active = 1")).scalar() or 0)
    last = overview.backups[0]["name"] if overview.backups else ""
    enrichment = (
        _("Sources actives : {list}").format(list=", ".join(labels.get(c, c) for (c,) in sources))
        if sources
        else _("Aucune source active")
    )
    if not contact_of(session):
        enrichment += " · " + _("adresse de contact à renseigner pour Open Library")
    return {
        "bibliotheque": me.name or _("Nom à renseigner"),
        "catalogue": _("Listes utilisées pour classer et retrouver les livres"),
        "enrichissement": enrichment,
        "prets": _("Règles de prêt par catégorie de lecteurs"),
        "sauvegardes": _("Dernière : {name}").format(name=last) if last else _("Aucune sauvegarde pour l’instant"),
        "utilisateurs": _("{n} compte(s) actif(s)").format(n=users),
        "avance": _("{n} réglage(s) à faire").format(n=todo) if todo else _("Tous les réglages nécessaires sont faits"),
    }


def _page(
    request: Request,
    rubric: str = "",
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, str] | None = None,
    message: str = "",
    notice: str = "",
    status: int = 200,
) -> HTMLResponse:
    settings = request.app.state.settings
    with request.app.state.database.session() as session:
        me = parametres.identity(session)
        summary = parametres.overview(session, settings)
        summaries = _summaries(session, summary) if not rubric else {}
    context: dict[str, Any] = {
        "identity": me,
        "values": values or {"nom": me.name, "courriel": me.email, "telephone": me.phone, "adresse": me.address},
        "errors": errors or {},
        "message": message,
        "notice": notice,
        "overview": summary,
        "summaries": summaries,
        "rubric": rubric,
    }
    response: HTMLResponse = request.app.state.templates.render(request, PAGES[rubric], context, status_code=status)
    return response


def _reachable(auth: AuthContext, rubric: str) -> None:
    if rubric and not rubrics.allowed(auth, rubric):
        raise PermissionDenied(rubric)
    if not rubric and not rubrics.visible(auth):
        raise PermissionDenied("admin.settings")


@router.get("/parametres", include_in_schema=False)
def settings_page(request: Request, auth: SignedIn) -> HTMLResponse:
    _reachable(auth, "")
    return _page(request)


def _rubric_route(rubric: str) -> None:
    """Une adresse explicite par rubrique : « /parametres/enrichissement » appartient au module Enrichissement."""

    def page(request: Request, auth: SignedIn) -> HTMLResponse:
        _reachable(auth, rubric)
        return _page(request, rubric)

    router.add_api_route(f"/parametres/{rubric}", page, methods=["GET"], include_in_schema=False)


for _rubric in PAGES:
    if _rubric:
        _rubric_route(_rubric)


@router.post("/parametres/identite", include_in_schema=False)
def identity_save(request: Request, auth: CanIdentity, form: Form) -> Response:
    values = {key: _text(form, key) for key in ("nom", "courriel", "telephone", "adresse")}
    try:
        parametres.save_identity(request.app.state.database, values, auth.user_id)
    except FormErrors as exc:
        return _page(request, "bibliotheque", errors=exc.fields, values=values, status=400)
    return _go(request, "settings_identity_saved", "bibliotheque")


@router.post("/parametres/logo", include_in_schema=False)
def logo_save(request: Request, auth: CanIdentity, form: Form) -> Response:
    upload = form.get("logo")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return _page(request, "bibliotheque", message=_("Choisissez une image (PNG, JPEG ou WebP)."), status=400)
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        parametres.save_logo(request.app.state.database, data, auth.user_id)
    except BusinessError as exc:
        return _page(request, "bibliotheque", message=exc.message, status=400)
    return _go(request, "settings_logo_saved", "bibliotheque")


@router.post("/parametres/logo/retirer", include_in_schema=False)
def logo_remove(request: Request, auth: CanIdentity) -> Response:
    parametres.remove_logo(request.app.state.database, auth.user_id)
    return _go(request, "settings_logo_removed", "bibliotheque")


@router.get("/identite/logo.webp", include_in_schema=False)
def logo(request: Request) -> Response:
    """Logo de la bibliothèque : public, comme son nom, affiché aussi sur la page de connexion."""
    with request.app.state.database.session() as session:
        found = parametres.logo_bytes(session)
    if found is None:
        raise HTTPException(status_code=404)
    data, digest = found
    if request.headers.get("if-none-match") == f'"{digest}"':
        return Response(status_code=304)
    return Response(
        data, media_type="image/webp", headers={"ETag": f'"{digest}"', "Cache-Control": "public, max-age=3600"}
    )


@router.post("/parametres/sauvegardes", include_in_schema=False)
def backup_create(request: Request, auth: CanBackup) -> Response:
    try:
        info = create_backup(request.app.state.settings, "manuelle")
    except BackupError as exc:
        return _page(request, "sauvegardes", message=str(exc), status=500)
    with request.app.state.database.session(write=True) as session:
        audit.record(session, "backup.create", "backup", None, user_id=auth.user_id, details={"archive": info.name})
    return _go(request, "settings_backup_created", "sauvegardes")


@router.post("/parametres/sauvegardes/verifier", include_in_schema=False)
def backup_verify(request: Request, _auth: CanBackup, form: Form) -> Response:
    path = parametres.backup_path(request.app.state.settings, _text(form, "archive"))
    if path is None:
        raise HTTPException(status_code=404)
    try:
        manifest = verify_backup(path)
    except BackupError as exc:
        return _page(request, "sauvegardes", message=_("Sauvegarde invalide : {e}").format(e=exc), status=409)
    revision = manifest.get("schema_revision", "?")
    notice = _("« {n} » vérifiée : archive intacte, base saine, révision {r}.").format(n=path.name, r=revision)
    return _page(request, "sauvegardes", notice=notice)
