# SPDX-License-Identifier: EUPL-1.2
"""Pages « Codes-barres et étiquettes » (lot L8). Générer et imprimer sont deux parcours distincts ; aucune page
d'aperçu n'écrit en base, et seul un clic sur un bouton de confirmation attribue des codes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from biblyo.core import flash
from biblyo.core.errors import BusinessError, FormErrors, PermissionDenied
from biblyo.core.i18n import _
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.catalogue import listing
from biblyo.modules.catalogue.rules import COPY_STATUSES
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.holdings import repository as holdings_repo
from biblyo.modules.holdings import rules as holdings_rules
from biblyo.modules.holdings import service as holdings
from biblyo.modules.labels import layout, render, service, sheets
from biblyo.modules.labels.service import Selection

router = APIRouter()

CanLabel = Annotated[AuthContext, require("holdings.edit")]  # attribuer des codes : comme sur la fiche d'un exemplaire


def _labels_access(auth: Annotated[AuthContext, require("catalogue.view")]) -> AuthContext:
    """Vue d'ensemble et choix des livres : qui gère les exemplaires, ou qui a le droit d'imprimer (rôle d'appoint
    « Impression des étiquettes » coché sur un bénévole choisi)."""
    if not (auth.can("holdings.edit") or auth.can("labels.print")):
        raise PermissionDenied("labels.print")
    return auth


CanSee = Annotated[AuthContext, Depends(_labels_access)]
CanPrint = Annotated[AuthContext, require("labels.print")]  # imprimer : permission prévue dès la révision 0001
#: Générer en masse (tous les codes manquants, une sélection) : gestion du catalogue (arbitrage L8, 18 novembre).
CanGenerate = Annotated[AuthContext, require("catalogue.edit")]
CanSettings = Annotated[AuthContext, require("admin.settings")]
PREVIEW_PAGES = 3
LIST_SIZE = 60
PDF_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.render(request, template, context, status_code=status)
    return response


def _go(request: Request, path: str, code: str) -> Response:
    response = RedirectResponse(path, status_code=303)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def _text(form: FormData, name: str, limit: int = 40) -> str:
    value = form.get(name)
    return value.strip()[:limit] if isinstance(value, str) else ""


# --- Vue d'ensemble ---------------------------------------------------------------------------------------------------
@router.get("/etiquettes", include_in_schema=False)
def overview_page(request: Request, auth: CanSee) -> HTMLResponse:
    with request.app.state.database.session() as session:
        context = {
            "overview": service.overview(session),
            "batches": service.recent_batches(session),
            "prefs": service.preferences(session),
            "can_settings": auth.can("admin.settings"),
            "can_print": auth.can("labels.print"),
            "can_generate": auth.can("catalogue.edit"),
            "can_hold": auth.can("holdings.edit"),
        }
    return _render(request, "labels/pages/index.html", context)


# --- Générer : tous les codes manquants, ou ceux d’une sélection ---------------------------------------------------
def _generation_page(
    request: Request, selection: Selection | None, *, message: str = "", status: int = 200
) -> HTMLResponse:
    database = request.app.state.database
    try:
        plan = holdings.generation_plan(database, _ids(request, selection) if selection is not None else None)
    except BusinessError as exc:
        plan, message, status = None, message or exc.message, 400 if status == 200 else status
    with database.session() as session:
        totals = holdings.barcode_totals(session)
        sample = holdings_repo.briefs(session, list(plan.copy_ids[:8])) if plan else {}
    context = {
        "plan": plan,
        "totals": totals,
        "sample": [sample[item] for item in (plan.copy_ids[:8] if plan else ()) if item in sample],
        "selection": selection,
        "message": message,
        "excluded": [
            _("{n} « {status} »").format(n=count, status=COPY_STATUSES.get(status, status))
            for status, count in sorted(totals.excluded.items())
        ],
    }
    return _render(request, "labels/pages/generate.html", context, status)


def _ids(request: Request, selection: Selection) -> list[int]:
    with request.app.state.database.session() as session:
        return service.select_copies(session, selection)


@router.get("/etiquettes/generer", include_in_schema=False)
def generate_all_page(request: Request, _auth: CanGenerate) -> HTMLResponse:
    """Simulation de « Générer tous les codes manquants » : rien n'est écrit, la séquence n'avance pas."""
    return _generation_page(request, None)


@router.post("/etiquettes/generer", include_in_schema=False)
def generate_confirm(request: Request, auth: CanGenerate, form: Form) -> Response:
    fingerprint = _text(form, "empreinte", 64)
    scope = _text(form, "portee")
    selection = Selection.read(form) if scope == "selection" else None
    if not fingerprint:
        raise HTTPException(status_code=400)
    database = request.app.state.database
    if _text(form, "simuler") == "1":
        return _generation_page(request, selection)
    try:
        ids = _ids(request, selection) if selection is not None else None
        motive = "attribution_selection" if selection is not None else "attribution_globale"
        holdings.assign_planned(database, ids, fingerprint, auth.user_id, motive=motive)
    except BusinessError as exc:
        status = 409 if isinstance(exc, ConflictError) else 400
        return _generation_page(request, selection, message=exc.message, status=status)
    return _go(request, "/etiquettes?genere=1", "labels_generated")


@router.post("/etiquettes/selection/generer", include_in_schema=False)
def generate_selection_page(request: Request, _auth: CanGenerate, form: Form) -> HTMLResponse:
    """Simulation pour une sélection : un exemplaire qui a déjà un code le garde."""
    return _generation_page(request, Selection.read(form))


@router.post("/etiquettes/exemplaire/{copy_id:int}/code", include_in_schema=False)
def assign_one(request: Request, copy_id: int, auth: CanLabel, form: Form) -> Response:
    """« Générer pour ce livre » : depuis sa fiche, un seul code, à sa version affichée."""
    version = _text(form, "version", 9)
    if not version.isdecimal():
        raise HTTPException(status_code=400)
    try:
        holdings.assign_barcodes(request.app.state.database, {copy_id: int(version)}, auth.user_id)
    except BusinessError as exc:
        return _copy_error(request, copy_id, exc)
    return _go(request, f"/exemplaires/{copy_id}", "labels_copy_code")


def _copy_error(request: Request, copy_id: int, exc: BusinessError) -> HTMLResponse:
    return _render(
        request,
        "labels/pages/message.html",
        {"title": _("Code non attribué"), "message": exc.message, "back": f"/exemplaires/{copy_id}"},
        409 if isinstance(exc, ConflictError) else 400,
    )


# --- Choisir les livres -----------------------------------------------------------------------------------------------
def _selection_context(request: Request, selection: Selection) -> dict[str, Any]:
    with request.app.state.database.session() as session:
        ids = service.select_copies(session, selection)
        shown = holdings_repo.briefs(session, ids[:LIST_SIZE])
        places = listing._place_options(session, selection.emplacement)
        series_label = listing._ref_label(session, "serie", int(selection.serie)) if selection.serie.isdigit() else ""
        with_code = sum(1 for item in shown.values() if item.barcode and item.barcode != item.inventory_number)
    return {
        "selection": selection,
        "total": len(ids),
        "rows": [shown[item] for item in ids[:LIST_SIZE] if item in shown],
        "more": max(0, len(ids) - LIST_SIZE),
        "places": places,
        "series_label": series_label,
        "orders": service.ORDERS,
        "code_filters": service.CODE_FILTERS,
        "with_code_shown": with_code,
    }


@router.get("/etiquettes/selection", include_in_schema=False)
def selection_page(request: Request, _auth: CanSee) -> HTMLResponse:
    """Étape 1 : filtres du catalogue (recherche, série, tome, cote, emplacement, code). Ne lit que la base."""
    return _render(
        request, "labels/pages/selection.html", _selection_context(request, Selection.read(request.query_params))
    )


# --- Imprimer : planche, modèle, cases déjà utilisées, aperçu ---------------------------------------------------------
def _occupied(form: Any, sheet: sheets.Sheet) -> frozenset[int]:
    raw = form.getlist("occupee") if hasattr(form, "getlist") else []
    chosen = {int(item) for item in raw if isinstance(item, str) and item.isdecimal() and len(item) < 4}
    start = form.get("depart") if hasattr(form, "get") else None
    if isinstance(start, str) and start.isdecimal() and len(start) < 4:
        chosen |= set(range(1, int(start)))
    return frozenset(position for position in chosen if 1 <= position <= sheet.count)


def _print_choices(request: Request, form: Any, prefs: service.Preferences) -> tuple[sheets.Sheet, str, frozenset[str]]:
    code = form.get("planche") if hasattr(form, "get") else None
    code = code if isinstance(code, str) and (code in sheets.KNOWN_BY_CODE or code == sheets.CUSTOM) else prefs.sheet
    sheet = prefs.current_sheet() if code == prefs.sheet or code == sheets.CUSTOM else sheets.KNOWN_BY_CODE[code]
    model = form.get("modele") if hasattr(form, "get") else None
    model = (
        model if isinstance(model, str) and (model in layout.MODELS or model == layout.CUSTOM_MODEL) else prefs.model
    )
    if model == layout.CUSTOM_MODEL:
        chosen = form.getlist("element") if hasattr(form, "getlist") else []
        elements = (
            frozenset(item for item in chosen if item in layout.ELEMENTS) if chosen else prefs.elements_for(model)
        )
    else:
        elements = layout.MODELS[model][1]
    return sheet, model, elements


def _origin(form: Any) -> str:
    value = form.get("origine") if hasattr(form, "get") else None
    return value if value in ("copy", "resume", "reprint") else ""


def _parent(form: Any) -> str:
    value = form.get("parent") if hasattr(form, "get") else None
    return value if isinstance(value, str) and value.isdecimal() and len(value) < 10 else ""


def _print_page(request: Request, form: Any, *, step: int, message: str = "", status: int = 200) -> HTMLResponse:
    database = request.app.state.database
    selection = Selection.read(form)
    with database.session() as session:
        prefs = service.preferences(session)
        sheet, model, elements = _print_choices(request, form, prefs)
        occupied = _occupied(form, sheet)
        ids = service.select_copies(session, selection)
        context: dict[str, Any] = {
            "step": step,
            "selection": selection,
            "total": len(ids),
            "chosen_sheet": sheet,
            "sheet_list": sheets.KNOWN,
            "custom_ready": bool(prefs.custom),
            "model": model,
            "models": layout.MODELS,
            "model_help": layout.MODEL_HELP,
            "elements": elements,
            "element_labels": layout.ELEMENTS,
            "occupied": occupied,
            "prefs": prefs,
            "verification": sheets.VERIFICATION_LABELS,
            "message": message,
            "plan": None,
            "previews": [],
            "origin": _origin(form),
            "parent": _parent(form),
        }
        if step == 3 and not message:
            request_ = service.Request(tuple(ids), sheet, model, elements, occupied, prefs.offset)
            try:
                plan, _logo, missing = service.build_plan(session, request_)
            except BusinessError as exc:
                context["message"], status = exc.message, 400
            else:
                logo_url = "/identite/logo.webp" if "logo" in elements and plan.pages[0][0].drawn.logos else ""
                context.update(
                    plan=plan,
                    missing=len(missing),
                    previews=[
                        render.sheet_svg(
                            plan,
                            index,
                            title=_("Feuille {n} sur {total}").format(n=index + 1, total=len(plan.pages)),
                            logo_url=logo_url,
                        )
                        for index in range(min(PREVIEW_PAGES, len(plan.pages)))
                    ],
                    dropped=[layout.ELEMENTS[name] for name in plan.dropped],
                    zoom=render.label_svg(plan, title=_("Première étiquette, agrandie"), logo_url=logo_url),
                )
    return _render(request, "labels/pages/print.html", context, status)


@router.get("/etiquettes/imprimer", include_in_schema=False)
def print_from_link(request: Request, _auth: CanPrint) -> HTMLResponse:
    """Depuis la fiche d'un exemplaire ou la fin d'une génération : directement au choix de la planche."""
    return _print_page(request, request.query_params, step=2)


@router.post("/etiquettes/imprimer", include_in_schema=False)
def print_step(request: Request, _auth: CanPrint, form: Form) -> HTMLResponse:
    """Étapes 2 et 3 de l'assistant. L'aperçu lit la base et dessine : il n'écrit rien, ne génère aucun code."""
    step = 3 if _text(form, "etape") == "3" else 2
    return _print_page(request, form, step=step)


@router.post("/etiquettes/lots", include_in_schema=False)
def batch_create(request: Request, auth: CanPrint, form: Form) -> Response:
    """« Préparer le PDF » : enregistre le lot (préparé, pas imprimé), puis ouvre sa page pour le télécharger."""
    database = request.app.state.database
    selection = Selection.read(form)
    origin = _origin(form) or "selection"
    parent = _parent(form)
    with database.session() as session:
        prefs = service.preferences(session)
        ids = service.select_copies(session, selection)
    sheet, model, elements = _print_choices(request, form, prefs)
    request_ = service.Request(tuple(ids), sheet, model, elements, _occupied(form, sheet), prefs.offset)
    try:
        batch_id, _pdf = service.prepare_batch(
            database,
            request_,
            origin=origin if origin in ("copy", "resume", "reprint") else "selection",
            user_id=auth.user_id,
            parent_id=int(parent) if parent.isdecimal() else None,
        )
    except BusinessError as exc:
        return _print_page(request, form, step=3, message=exc.message, status=400)
    return RedirectResponse(f"/etiquettes/lots/{batch_id}?nouveau=1", status_code=303)


# --- Lots ----------------------------------------------------------------------------------------------------------
def _batch_or_404(request: Request, batch_id: int) -> service.BatchView:
    with request.app.state.database.session() as session:
        try:
            return service.batch_view(session, batch_id)
        except BusinessError as exc:
            raise HTTPException(status_code=404) from exc


@router.get("/etiquettes/lots/{batch_id:int}", include_in_schema=False)
def batch_page(request: Request, batch_id: int, _auth: CanPrint) -> HTMLResponse:
    view = _batch_or_404(request, batch_id)
    sheets_used = sorted({item.sheet_number for item in view.items})
    return _render(
        request,
        "labels/pages/batch.html",
        {
            "view": view,
            "sheets_used": sheets_used,
            "fresh": request.query_params.get("nouveau") == "1",
            "model_labels": {key: value[0] for key, value in layout.MODELS.items()},
        },
    )


@router.get("/etiquettes/lots/{batch_id:int}/etiquettes.pdf", include_in_schema=False)
def batch_download(request: Request, batch_id: int, _auth: CanPrint) -> Response:
    try:
        data = service.batch_pdf(request.app.state.database, batch_id)
    except BusinessError as exc:
        return _render(
            request,
            "labels/pages/message.html",
            {"title": _("PDF non refait"), "message": exc.message, "back": f"/etiquettes/lots/{batch_id}"},
            409,
        )
    headers = {**PDF_HEADERS, "Content-Disposition": f'attachment; filename="etiquettes-lot-{batch_id}.pdf"'}
    return Response(data, media_type="application/pdf", headers=headers)


@router.post("/etiquettes/lots/{batch_id:int}/etat", include_in_schema=False)
def batch_status(request: Request, batch_id: int, auth: CanPrint, form: Form) -> Response:
    status = _text(form, "etat")
    try:
        service.set_batch_status(request.app.state.database, batch_id, status, auth.user_id)
    except BusinessError as exc:
        return _render(
            request,
            "labels/pages/message.html",
            {"title": _("Lot inchangé"), "message": exc.message, "back": f"/etiquettes/lots/{batch_id}"},
            400,
        )
    code = "labels_batch_confirmed" if status == "confirmed" else "labels_batch_abandoned"
    return _go(request, f"/etiquettes/lots/{batch_id}", code)


@router.post("/etiquettes/lots/{batch_id:int}/reprendre", include_in_schema=False)
def batch_resume(request: Request, batch_id: int, _auth: CanPrint, form: Form) -> HTMLResponse:
    """Reprendre une impression interrompue (à partir d'une étiquette) ou tout réimprimer : mêmes codes, nouvelle
    feuille. On repasse par l'étape 2 pour dire quelles cases de la feuille sont déjà utilisées."""
    rank = _text(form, "a_partir", 5)
    try:
        request_, view = service.resume_request(
            request.app.state.database, batch_id, int(rank) if rank.isdecimal() else 1
        )
    except BusinessError as exc:
        return _render(
            request,
            "labels/pages/message.html",
            {"title": _("Reprise impossible"), "message": exc.message, "back": f"/etiquettes/lots/{batch_id}"},
            409,
        )
    values = _ResumeForm(request_, view, "resume" if rank not in ("", "1") else "reprint")
    return _print_page(request, values, step=2)


class _ResumeForm:
    """Les valeurs d'une reprise, lues comme un formulaire par l'étape 2."""

    def __init__(self, request_: service.Request, view: service.BatchView, origin: str) -> None:
        self._values: dict[str, list[str]] = {
            "exemplaire": [str(item) for item in request_.copy_ids],
            "planche": [view.sheet.code],
            "modele": [view.model],
            "element": sorted(view.elements),
            "origine": [origin],
            "parent": [str(view.batch.id)],
        }

    def get(self, name: str, default: Any = None) -> Any:
        values = self._values.get(name)
        return values[0] if values else default

    def getlist(self, name: str) -> list[str]:
        return list(self._values.get(name, []))


# --- Réglages : planche, modèle, calibration, feuille test, préfixe -------------------------------------------------
def _settings_page(
    request: Request,
    auth: AuthContext,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, str] | None = None,
    status: int = 200,
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        prefs = service.preferences(session)
        sequence = holdings.sequence_state(session)
    current = prefs.current_sheet() if prefs.sheet == sheets.CUSTOM else None
    context = {
        "prefs": prefs,
        "sheet_list": sheets.KNOWN,
        "custom": values or (sheets.custom_values(current) if current else {}),
        "models": layout.MODELS,
        "model_help": layout.MODEL_HELP,
        "element_labels": layout.ELEMENTS,
        "verification": sheets.VERIFICATION_LABELS,
        "errors": errors or {},
        "sequence": sequence,
        "prefix_warnings": holdings_rules.prefix_problems(sequence.prefix, sequence.padding)[1],
        "can_prefix": auth.can("admin.settings"),
        "current_sheet": prefs.current_sheet(),
    }
    return _render(request, "labels/pages/settings.html", context, status)


@router.get("/etiquettes/reglages", include_in_schema=False)
def settings_page(request: Request, auth: CanPrint) -> HTMLResponse:
    return _settings_page(request, auth)


@router.post("/etiquettes/reglages", include_in_schema=False)
def settings_save(request: Request, auth: CanPrint, form: Form) -> Response:
    sheet = _text(form, "planche")
    custom = sheets.CustomInput(
        _text(form, "colonnes"),
        _text(form, "lignes"),
        _text(form, "largeur"),
        _text(form, "hauteur"),
        _text(form, "marge_haut"),
        _text(form, "marge_gauche"),
        _text(form, "espace_horizontal"),
        _text(form, "espace_vertical"),
    )
    elements = [item for item in form.getlist("element") if isinstance(item, str)]
    try:
        offset = service.parse_offset(_text(form, "decalage_horizontal"), _text(form, "decalage_vertical"))
        service.save_preferences(
            request.app.state.database,
            sheet=sheet,
            custom=custom if sheet == sheets.CUSTOM else None,
            model=_text(form, "modele"),
            elements=elements,
            offset=offset,
            user_id=auth.user_id,
        )
    except FormErrors as exc:
        values = {name: _text(form, name) for name in sheets.custom_values(sheets.KNOWN[0])}
        return _settings_page(request, auth, errors=exc.fields, values=values, status=400)
    return _go(request, "/etiquettes/reglages", "labels_settings_saved")


@router.post("/etiquettes/reglages/verifiee", include_in_schema=False)
def settings_verified(request: Request, auth: CanPrint, form: Form) -> Response:
    code = _text(form, "planche")
    if code not in sheets.KNOWN_BY_CODE and code != sheets.CUSTOM:
        raise HTTPException(status_code=400)
    if _text(form, "calibration") != "1" or _text(form, "douchette") != "1":
        message = _("Pour certifier une planche : la feuille test imprimée à 100 % doit tomber juste, et la douchette "
                    "doit en avoir lu le code. Cochez les deux cases.")  # fmt: skip
        return _settings_page(request, auth, errors={"certifier": message}, status=400)
    service.mark_sheet_verified(request.app.state.database, code, auth.user_id)
    return _go(request, "/etiquettes/reglages", "labels_sheet_verified")


@router.get("/etiquettes/feuille-test.pdf", include_in_schema=False)
def test_sheet(request: Request, _auth: CanPrint) -> Response:
    """La feuille test de la planche choisie, avec la calibration en cours. N'écrit rien."""
    with request.app.state.database.session() as session:
        prefs = service.preferences(session)
    code = request.query_params.get("planche", "")
    sheet = sheets.KNOWN_BY_CODE.get(code) or prefs.current_sheet()
    data = render.test_sheet_pdf(sheet, offset=prefs.offset)
    headers = {**PDF_HEADERS, "Content-Disposition": f'attachment; filename="feuille-test-{sheet.code}.pdf"'}
    return Response(data, media_type="application/pdf", headers=headers)


@router.post("/etiquettes/prefixe", include_in_schema=False)
def prefix_change(request: Request, auth: CanSettings, form: Form) -> Response:
    """Préfixe des prochains codes : changement explicite, confirmé, journalisé (D-L8-2)."""
    if _text(form, "confirmer") != "1":
        return _settings_page(
            request, auth, errors={"prefixe": _("Cochez la case pour confirmer le changement.")}, status=400
        )
    try:
        holdings.change_barcode_prefix(
            request.app.state.database, _text(form, "prefixe", 20), _text(form, "actuel", 20), auth.user_id
        )
    except FormErrors as exc:
        return _settings_page(request, auth, errors=exc.fields, status=400)
    except BusinessError as exc:
        return _settings_page(request, auth, errors={"prefixe": exc.message}, status=409)
    return _go(request, "/etiquettes/reglages", "labels_prefix_changed")
