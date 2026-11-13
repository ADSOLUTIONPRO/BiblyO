# SPDX-License-Identifier: EUPL-1.2
"""Écran « Paramètres » (lot L6, finalisation) : identité, logo, sauvegardes, contrôle de tous les réglages."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData, UploadFile

from biblyo.core import audit, flash
from biblyo.core.backup import BackupError, create_backup, verify_backup
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _
from biblyo.core.media import MAX_UPLOAD_BYTES
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.admin import parametres

router = APIRouter()
CanSettings = Annotated[AuthContext, require("admin.settings")]
CanIdentity = Annotated[AuthContext, require("admin.identity")]
CanBackup = Annotated[AuthContext, require("admin.backups")]


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _text(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _go(request: Request, code: str) -> Response:
    response = RedirectResponse("/parametres", status_code=303)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def _page(
    request: Request,
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
    context: dict[str, Any] = {
        "identity": me,
        "values": values or {"nom": me.name, "courriel": me.email, "telephone": me.phone, "adresse": me.address},
        "errors": errors or {},
        "message": message,
        "notice": notice,
        "overview": summary,
    }
    response: HTMLResponse = request.app.state.templates.render(
        request, "admin/pages/settings.html", context, status_code=status
    )
    return response


@router.get("/parametres", include_in_schema=False)
def settings_page(request: Request, _auth: CanSettings) -> HTMLResponse:
    return _page(request)


@router.post("/parametres/identite", include_in_schema=False)
def identity_save(request: Request, auth: CanIdentity, form: Form) -> Response:
    values = {key: _text(form, key) for key in ("nom", "courriel", "telephone", "adresse")}
    try:
        parametres.save_identity(request.app.state.database, values, auth.user_id)
    except FormErrors as exc:
        return _page(request, errors=exc.fields, values=values, status=400)
    return _go(request, "settings_identity_saved")


@router.post("/parametres/logo", include_in_schema=False)
def logo_save(request: Request, auth: CanIdentity, form: Form) -> Response:
    upload = form.get("logo")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return _page(request, message=_("Choisissez une image (PNG, JPEG ou WebP)."), status=400)
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        parametres.save_logo(request.app.state.database, data, auth.user_id)
    except BusinessError as exc:
        return _page(request, message=exc.message, status=400)
    return _go(request, "settings_logo_saved")


@router.post("/parametres/logo/retirer", include_in_schema=False)
def logo_remove(request: Request, auth: CanIdentity) -> Response:
    parametres.remove_logo(request.app.state.database, auth.user_id)
    return _go(request, "settings_logo_removed")


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
        return _page(request, message=str(exc), status=500)
    with request.app.state.database.session(write=True) as session:
        audit.record(session, "backup.create", "backup", None, user_id=auth.user_id, details={"archive": info.name})
    return _go(request, "settings_backup_created")


@router.post("/parametres/sauvegardes/verifier", include_in_schema=False)
def backup_verify(request: Request, _auth: CanBackup, form: Form) -> Response:
    path = parametres.backup_path(request.app.state.settings, _text(form, "archive"))
    if path is None:
        raise HTTPException(status_code=404)
    try:
        manifest = verify_backup(path)
    except BackupError as exc:
        return _page(request, message=_("Sauvegarde invalide : {e}").format(e=exc), status=409)
    revision = manifest.get("schema_revision", "?")
    return _page(request, notice=_("« {n} » vérifiée : archive intacte, base saine, révision {r}.").format(
        n=path.name, r=revision
    ))  # fmt: skip
