# SPDX-License-Identifier: EUPL-1.2
"""Pages des exemplaires et des emplacements. Toute écriture passe par `service`."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from biblyo.core import flash
from biblyo.core.errors import BusinessError, FormErrors, PermissionDenied
from biblyo.core.i18n import _
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.catalogue.rules import COPY_STATUS_BADGES, COPY_STATUSES
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.holdings import availability, pages, rules, service
from biblyo.modules.holdings import repository as repo
from biblyo.modules.holdings.drawing import UnitView
from biblyo.modules.holdings.models import Location
from biblyo.modules.holdings.rules import GridBox

router = APIRouter()

CanView = Annotated[AuthContext, require("catalogue.view")]
CanHold = Annotated[AuthContext, require("holdings.edit")]


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _first(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _int(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() and len(text) < 12 else None


def _go(request: Request, path: str, code: str) -> Response:
    response = RedirectResponse(path, status_code=303)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.render(request, template, context, status_code=status)
    return response


def _options(mapping: dict[str, str], empty: str) -> list[tuple[str, str]]:
    return [("", empty), *mapping.items()]


def _copy_values(sheet: repo.CopySheet) -> dict[str, str]:
    copy = sheet.copy
    return {
        "code_barres": copy.barcode or "",
        "inventaire": copy.inventory_number,
        "cote": copy.call_number or "",
        "etat": copy.condition or "",
        "note_etat": copy.condition_note or "",
        "mode_acquisition": copy.acquisition_mode or "",
        "date_acquisition": copy.acquired_on or "",
        "note_acquisition": copy.acquisition_note or "",
        "prix": rules.format_price(copy.price_cents),
        "provenance": copy.provenance or "",
        "notes": copy.notes or "",
    }


def _copy_page(
    request: Request,
    copy_id: int,
    *,
    errors: dict[str, str] | None = None,
    form: FormData | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    from biblyo.modules.imports.provenance import copy_origin

    with request.app.state.database.session() as session:
        sheet = repo.copy_sheet(session, copy_id)
        loan = repo.on_loan(session, [copy_id]).get(copy_id)
        state = availability.for_copies(session, [copy_id]).get(copy_id)
        origin = copy_origin(session, copy_id) if sheet is not None else None
    if sheet is None:
        raise HTTPException(status_code=404)
    values = _copy_values(sheet)
    loanable = sheet.copy.is_loanable
    if form is not None:
        values = {key: _first(form, key) for key in values}
        loanable = _first(form, "sur_place") != "1"
    actions = [
        {"code": code, "label": rules.STATUS_ACTION_LABELS[code], "permission": rules.STATUS_PERMISSIONS[code]}
        for code in rules.actions_for(sheet.copy.status)
    ]
    context = {
        "record": sheet,
        "copy": sheet.copy,
        "values": values,
        "loanable": loanable,
        "errors": errors or {},
        "message": message,
        "actions": actions,
        "status_label": COPY_STATUSES.get(sheet.copy.status, sheet.copy.status),
        "status_badge": COPY_STATUS_BADGES.get(sheet.copy.status, "neutral"),
        "state": state,
        "loan": loan,  # le gabarit n'en tire un lien vers l'emprunteur qu'avec circulation.history (R27)
        "condition_options": _options(rules.CONDITIONS, _("Non renseigné")),
        "condition_label": rules.CONDITIONS.get(sheet.copy.condition or "", ""),
        "acquisition_options": _options(rules.ACQUISITION_MODES, _("Non renseigné")),
        "acquisition_label": rules.ACQUISITION_MODES.get(sheet.copy.acquisition_mode or "", ""),
        "price_text": rules.format_price(sheet.copy.price_cents),
        "origin": origin,  # lot L6 : ligne du classeur importé et valeurs d'origine
    }
    return _render(request, "holdings/pages/copy.html", context, status)


@router.get("/exemplaires/{copy_id}", include_in_schema=False)
def copy_page(request: Request, copy_id: int, _auth: CanView) -> HTMLResponse:
    return _copy_page(request, copy_id)


@router.post("/exemplaires/{copy_id}", include_in_schema=False)
def update_copy(request: Request, copy_id: int, auth: CanHold, form: Form) -> Response:
    data = service.CopyInput(
        barcode=_first(form, "code_barres"),
        inventory_number=_first(form, "inventaire"),
        call_number=_first(form, "cote"),
        is_loanable=_first(form, "sur_place") != "1",
        condition=_first(form, "etat"),
        condition_note=_first(form, "note_etat"),
        acquisition_mode=_first(form, "mode_acquisition"),
        acquired_on=_first(form, "date_acquisition"),
        acquisition_note=_first(form, "note_acquisition"),
        price=_first(form, "prix"),
        provenance=_first(form, "provenance"),
        notes=_first(form, "notes"),
    )
    version = _int(_first(form, "version"))
    try:
        service.update_copy(request.app.state.database, copy_id, data, version, auth.user_id)
    except FormErrors as exc:
        return _copy_page(request, copy_id, errors=exc.fields, form=form, status=400)
    except ConflictError as exc:
        return _copy_page(request, copy_id, form=form, message=exc.message, status=409)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    return _go(request, f"/exemplaires/{copy_id}", "copy_updated")


@router.post("/exemplaires/{copy_id}/statut", include_in_schema=False)
def change_status(request: Request, copy_id: int, auth: CanHold, form: Form) -> Response:
    action = _first(form, "action")
    permission = rules.STATUS_PERMISSIONS.get(action)
    if permission is None:
        raise HTTPException(status_code=400)
    if not auth.can(permission):
        raise PermissionDenied
    version = _int(_first(form, "version"))
    try:
        service.change_status(
            request.app.state.database,
            copy_id,
            action,
            reason=_first(form, "motif") or _first(form, "precision"),
            expected_version=version,
            user_id=auth.user_id,
        )
    except FormErrors as exc:
        return _copy_page(request, copy_id, errors=exc.fields, status=400)
    except ConflictError as exc:
        return _copy_page(request, copy_id, message=exc.message, status=409)
    except BusinessError as exc:
        return _copy_page(request, copy_id, message=exc.message, status=400)
    return _go(request, f"/exemplaires/{copy_id}", "copy_status")


@router.post("/exemplaires/{copy_id}/sans-place", include_in_schema=False)
def unshelve(request: Request, copy_id: int, auth: CanHold, form: Form) -> Response:
    version = _int(_first(form, "version"))
    expected = {copy_id: version} if version is not None else None
    try:
        service.unshelve(request.app.state.database, [copy_id], expected_versions=expected, user_id=auth.user_id)
    except ConflictError as exc:
        return _copy_page(request, copy_id, message=exc.message, status=409)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    return _go(request, f"/exemplaires/{copy_id}", "copy_unshelved")


# --- Déplacer sans JavaScript (D4) ------------------------------------------------------------------------------------
def _move_page(
    request: Request,
    copy_id: int,
    *,
    selected: str = "",
    rank: str = "",
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        sheet = repo.copy_sheet(session, copy_id)
        if sheet is None:
            raise HTTPException(status_code=404)
        places = repo.place_choices(session)
        path = sheet.where.path or None
    context = {
        "record": sheet,
        "copy": sheet.copy,
        "current_path": path,
        "place_options": [(str(place.id), place.label) for place in places],
        "selected": selected or (str(sheet.copy.location_id) if sheet.copy.location_id else ""),
        "rank": rank,
        "message": message,
        "shelvable": sheet.copy.status not in rules.NOT_SHELVABLE,
        "status_label": COPY_STATUSES.get(sheet.copy.status, sheet.copy.status),
    }
    return _render(request, "holdings/pages/copy_move.html", context, status)


@router.get("/exemplaires/{copy_id}/deplacer", include_in_schema=False)
def move_page(request: Request, copy_id: int, _auth: CanHold) -> HTMLResponse:
    return _move_page(request, copy_id)


@router.post("/exemplaires/{copy_id}/deplacer", include_in_schema=False)
def move_copy(request: Request, copy_id: int, auth: CanHold, form: Form) -> Response:
    selected, rank = _first(form, "emplacement"), _first(form, "rang").strip()
    location_id, position, version = _int(selected), _int(rank), _int(_first(form, "version"))
    if location_id is None:
        return _move_page(request, copy_id, rank=rank, message=_("Choisissez un emplacement."), status=400)
    if rank and not position:
        message = _("Indiquez un rang à partir de 1, ou laissez vide pour ranger à la fin.")
        return _move_page(request, copy_id, selected=selected, rank=rank, message=message, status=400)
    expected = {copy_id: version} if version is not None else None
    try:
        result = service.shelve(
            request.app.state.database,
            [copy_id],
            location_id,
            position=position,
            expected_versions=expected,
            user_id=auth.user_id,
        )
    except ConflictError as exc:
        return _move_page(request, copy_id, selected=selected, rank=rank, message=exc.message, status=409)
    except BusinessError as exc:
        return _move_page(request, copy_id, selected=selected, rank=rank, message=exc.message, status=400)
    if not result.changed:
        return _go(request, f"/exemplaires/{copy_id}", "copy_unchanged")
    return _go(request, f"/exemplaires/{copy_id}", "copy_moved_over" if result.fill.state == "over" else "copy_moved")


# --- Étagères : arbre, salles, meubles --------------------------------------------------------------------------------
CanManage = Annotated[AuthContext, require("locations.manage")]


def _shell(session: Session, *, current: int | None = None) -> dict[str, Any]:
    """Ce que toutes les pages Étagères partagent : l'arbre et le nombre d'exemplaires sans place."""
    return {
        "tree": pages.location_tree(session, current=current),
        "unshelved_count": repo.unshelved_count(session),
        "current_id": current,
    }


@router.get("/emplacements", include_in_schema=False)
def places_home(request: Request, _auth: CanView) -> HTMLResponse:
    with request.app.state.database.session() as session:
        rooms = [item for item in session.scalars(select(Location).order_by(Location.path)) if item.kind == "room"]
        plans = [pages.room_plan(session, room) for room in rooms]
        context = {**_shell(session), "plans": plans}
    return _render(request, "holdings/pages/places.html", context)


@router.get("/emplacements/localiser", include_in_schema=False)
def locate(request: Request, _auth: CanView) -> Response:
    """Un seul résultat : on y va. Plusieurs : on choisit. Aucun : on le dit."""
    query = request.query_params.get("q", "").strip()[:200]
    with request.app.state.database.session() as session:
        found = pages.find_copies(session, query)
        if len(found) == 1:
            return RedirectResponse(found[0].href, status_code=303)
        context = {**_shell(session), "found": found, "query": query}
    return _render(request, "holdings/pages/locate.html", context)


@router.get("/emplacements/non-ranges", include_in_schema=False)
def unshelved_page(request: Request, _auth: CanView) -> HTMLResponse:
    """« À ranger » : exemplaires sans place, ou encore « En préparation » ; filtrés, cherchés, choisis en lot."""
    params = request.query_params
    state = params.get("etat", "non_ranges")
    chosen = pages.ShelveFilter(
        state=state if state in pages.TO_SHELVE_STATES else "non_ranges",
        origin="importes" if params.get("origine") == "importes" else "",
        query=params.get("q", "")[:120],
        page=max(_int(params.get("page", "1")) or 1, 1),
    )
    with request.app.state.database.session() as session:
        items, total = pages.to_shelve(session, chosen)
        context = {
            **_shell(session),
            "copies": items,
            "total": total,
            "chosen": chosen,
            "pages_count": max(1, -(-total // pages.TO_SHELVE_PER_PAGE)),
            "all_checked": params.get("tout") == "1",
            "query_base": urlencode(
                {k: v for k, v in (("etat", chosen.state), ("origine", chosen.origin), ("q", chosen.query)) if v}
            ),
        }
    return _render(request, "holdings/pages/unshelved.html", context)


# --- Lot L6 : confirmer la présence, attribuer des codes-barres en lot ----------------------------------------------
@router.get("/emplacements/verifier", include_in_schema=False)
def presence_page(request: Request, _auth: CanHold) -> HTMLResponse:
    copy_ids = _ids(request.query_params.getlist("exemplaire"))
    if not copy_ids:
        raise HTTPException(status_code=400)
    return _presence_page(request, copy_ids)


def _presence_page(request: Request, copy_ids: list[int], *, message: str = "", status: int = 200) -> HTMLResponse:
    try:
        lines = service.presence_plan(request.app.state.database, copy_ids)
    except BusinessError as exc:
        lines, message, status = [], exc.message, 400
    with request.app.state.database.session() as session:
        context = {**_shell(session), "lines": lines, "message": message}
    return _render(request, "holdings/pages/presence.html", context, status)


@router.post("/emplacements/verifier", include_in_schema=False)
def presence_confirm(request: Request, auth: CanHold, form: Form) -> Response:
    copy_ids = _ids(form.getlist("exemplaire"))
    expected = {item: value for item in copy_ids if (value := _int(_first(form, f"v_{item}"))) is not None}
    if not copy_ids or len(expected) != len(copy_ids):
        raise HTTPException(status_code=400)
    try:
        service.confirm_presence(request.app.state.database, expected, auth.user_id)
    except BusinessError as exc:
        status = 409 if isinstance(exc, ConflictError) else 400
        return _presence_page(request, copy_ids, message=exc.message, status=status)
    return _go(request, "/emplacements/non-ranges?etat=en_preparation", "presence_confirmed")


@router.get("/emplacements/codes-barres", include_in_schema=False)
def barcodes_page(request: Request, _auth: CanHold) -> HTMLResponse:
    copy_ids = _ids(request.query_params.getlist("exemplaire"))
    if not copy_ids:
        raise HTTPException(status_code=400)
    return _barcodes_page(request, copy_ids)


def _barcodes_page(request: Request, copy_ids: list[int], *, message: str = "", status: int = 200) -> HTMLResponse:
    try:
        lines = service.barcode_plan(request.app.state.database, copy_ids)
    except BusinessError as exc:
        lines, message, status = [], exc.message, 400
    with request.app.state.database.session() as session:
        context = {**_shell(session), "lines": lines, "message": message}
    return _render(request, "holdings/pages/barcodes.html", context, status)


@router.post("/emplacements/codes-barres", include_in_schema=False)
def barcodes_assign(request: Request, auth: CanHold, form: Form) -> Response:
    copy_ids = _ids(form.getlist("exemplaire"))
    expected = {item: value for item in copy_ids if (value := _int(_first(form, f"v_{item}"))) is not None}
    if not copy_ids or len(expected) != len(copy_ids):
        raise HTTPException(status_code=400)
    try:
        service.assign_barcodes(request.app.state.database, expected, auth.user_id)
    except BusinessError as exc:
        status = 409 if isinstance(exc, ConflictError) else 400
        return _barcodes_page(request, copy_ids, message=exc.message, status=status)
    return _go(request, "/emplacements/non-ranges?etat=en_preparation", "barcodes_assigned")


# --- Créer et modifier un lieu ----------------------------------------------------------------------------------------
_LOCATION_FIELDS = ("nom", "code", "capacite", "indication", "notes")


def _location_input(form: FormData) -> service.LocationInput:
    return service.LocationInput(
        name=_first(form, "nom"),
        code=_first(form, "code"),
        capacity=_first(form, "capacite"),
        content_hint=_first(form, "indication"),
        notes=_first(form, "notes"),
    )


def _new_page(
    request: Request,
    kind: str,
    parent: str,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, str] | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    if kind not in rules.KINDS:
        raise HTTPException(status_code=404)
    with request.app.state.database.session() as session:
        context = {
            **_shell(session, current=_int(parent)),
            "kind": kind,
            "kind_label": rules.KINDS[kind],
            "kinds": [(code, label) for code, label in rules.KINDS.items()],
            "parents": pages.parent_choices(session, kind),
            "parent": parent,
            "values": values or {},
            "errors": errors or {},
            "message": message,
        }
    return _render(request, "holdings/pages/place_new.html", context, status)


@router.get("/emplacements/nouveau", include_in_schema=False)
def new_place(request: Request, _auth: CanManage) -> HTMLResponse:
    params = request.query_params
    return _new_page(request, params.get("type", "room"), params.get("parent", ""))


@router.post("/emplacements/nouveau", include_in_schema=False)
def create_place(request: Request, auth: CanManage, form: Form) -> Response:
    kind, parent = _first(form, "type"), _first(form, "parent")
    values = {key: _first(form, key) for key in (*_LOCATION_FIELDS, "niveaux", "capacite_niveaux")}
    database = request.app.state.database
    wanted = _first(form, "niveaux").strip()
    try:
        created = service.create_location(database, kind, _int(parent), _location_input(form), auth.user_id)
        if kind == "furniture" and wanted:
            service.create_levels(database, created, _int(wanted) or 0, _first(form, "capacite_niveaux"), auth.user_id)
    except FormErrors as exc:
        return _new_page(request, kind, parent, errors=exc.fields, values=values, status=400)
    except BusinessError as exc:
        return _new_page(request, kind, parent, values=values, message=exc.message, status=400)
    return _go(request, f"/emplacements/{created}", "place_created")


def _edit_page(
    request: Request,
    location_id: int,
    *,
    errors: dict[str, str] | None = None,
    form: FormData | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        location = session.get(Location, location_id)
        if location is None:
            raise HTTPException(status_code=404)
        values = {
            "nom": location.name,
            "code": location.code or "",
            "capacite": "" if location.capacity is None else str(location.capacity),
            "indication": location.content_hint or "",
            "notes": location.notes or "",
        }
        if form is not None:
            values = {key: _first(form, key) for key in _LOCATION_FIELDS}
        children = repo.children(session, location.id)
        context = {
            **_shell(session, current=location.id),
            "place": location,
            "kind_label": rules.KINDS[location.kind],
            "is_level": rules.is_level(location.kind),
            "values": values,
            "errors": errors or {},
            "message": message,
            "children": children,
            "parents": pages.parent_choices(session, location.kind, exclude=location.id),
            "parent": str(location.parent_id or ""),
            "contents": repo.copies_in(session, repo.descendant_ids(session, location.id)),
        }
    return _render(request, "holdings/pages/place_edit.html", context, status)


@router.get("/emplacements/{location_id}/modifier", include_in_schema=False)
def edit_place(request: Request, location_id: int, _auth: CanManage) -> HTMLResponse:
    return _edit_page(request, location_id)


@router.post("/emplacements/{location_id}/modifier", include_in_schema=False)
def update_place(request: Request, location_id: int, auth: CanManage, form: Form) -> Response:
    version = _int(_first(form, "version"))
    try:
        service.update_location(request.app.state.database, location_id, _location_input(form), version, auth.user_id)
    except FormErrors as exc:
        return _edit_page(request, location_id, errors=exc.fields, form=form, status=400)
    except ConflictError as exc:
        return _edit_page(request, location_id, form=form, message=exc.message, status=409)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    return _go(request, f"/emplacements/{location_id}", "place_updated")


def _place_action(request: Request, location_id: int, action: Callable[[], object], code: str, target: str) -> Response:
    try:
        action()
    except ConflictError as exc:
        return _edit_page(request, location_id, message=exc.message, status=409)
    except BusinessError as exc:
        return _edit_page(request, location_id, message=exc.message, status=400)
    return _go(request, target, code)


@router.post("/emplacements/{location_id}/deplacer", include_in_schema=False)
def move_place(request: Request, location_id: int, auth: CanManage, form: Form) -> Response:
    database, parent = request.app.state.database, _int(_first(form, "parent"))
    version = _int(_first(form, "version"))
    return _place_action(
        request,
        location_id,
        lambda: service.move_location(database, location_id, parent, version, auth.user_id),
        "place_updated",
        f"/emplacements/{location_id}",
    )


@router.post("/emplacements/{location_id}/activation", include_in_schema=False)
def toggle_place(request: Request, location_id: int, auth: CanManage, form: Form) -> Response:
    database, active = request.app.state.database, _first(form, "actif") == "1"
    version = _int(_first(form, "version"))
    return _place_action(
        request,
        location_id,
        lambda: service.set_location_active(database, location_id, active, version, auth.user_id),
        "place_updated",
        f"/emplacements/{location_id}",
    )


@router.post("/emplacements/{location_id}/supprimer", include_in_schema=False)
def delete_place(request: Request, location_id: int, auth: CanManage, form: Form) -> Response:
    version = _int(_first(form, "version"))
    try:
        everything = _first(form, "ensemble") == "1"
        database = request.app.state.database
        parent = service.delete_location(database, location_id, version, auth.user_id, with_children=everything)
    except ConflictError as exc:
        return _edit_page(request, location_id, message=exc.message, status=409)
    except BusinessError as exc:
        return _edit_page(request, location_id, message=exc.message, status=400)
    return _go(request, f"/emplacements/{parent}" if parent else "/emplacements", "place_deleted")


@router.post("/emplacements/{location_id}/ordre", include_in_schema=False)
def reorder_levels(request: Request, location_id: int, auth: CanManage, form: Form) -> Response:
    """Monter ou descendre un niveau d'un cran : un bouton, sans JavaScript."""
    child, step = _int(_first(form, "lieu")), -1 if _first(form, "sens") == "monter" else 1
    database = request.app.state.database
    with database.session() as session:
        order = [item.id for item in repo.children(session, location_id)]
    if child not in order:
        raise HTTPException(status_code=404)
    index = order.index(child)
    other = index + step
    if 0 <= other < len(order):
        order[index], order[other] = order[other], order[index]
    return _place_action(
        request,
        location_id,
        lambda: service.reorder_children(database, location_id, order, None, auth.user_id),
        "place_updated",
        f"/emplacements/{location_id}/modifier",
    )


@router.post("/emplacements/{room_id}/plan", include_in_schema=False)
def place_on_plan(request: Request, room_id: int, auth: CanManage, form: Form) -> Response:
    """Pose le meuble choisi sur une case du plan. Une place occupée est refusée, jamais partagée (D5)."""
    furniture = _int(_first(form, "meuble"))
    cell = _first(form, "cellule").split(",")
    row, col = _int(cell[0]), _int(cell[-1])
    width, height = _int(_first(form, "largeur")), _int(_first(form, "hauteur"))
    if furniture is None or row is None or col is None or width is None or height is None:
        raise HTTPException(status_code=400)
    try:
        service.place_furniture(
            request.app.state.database, furniture, GridBox(row, col, width, height), None, auth.user_id
        )
    except BusinessError as exc:
        return _location_page(request, room_id, auth, editing=True, selected=furniture, message=exc.message, status=400)
    return _go(request, f"/emplacements/{room_id}?edition=plan&meuble={furniture}#plan", "place_updated")


def _location_page(
    request: Request,
    location_id: int,
    auth: AuthContext,
    *,
    editing: bool = False,
    selected: int | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    params = request.query_params
    with request.app.state.database.session() as session:
        location = session.get(Location, location_id)
        if location is None:
            raise HTTPException(status_code=404)
        chain = list(reversed(repo.ancestors(session, location.id)))
        context: dict[str, Any] = {
            **_shell(session, current=location.id),
            "place": location,
            "chain": chain,
            "trail": [
                (_("Étagères"), "/emplacements"),
                *((item.name, f"/emplacements/{item.id}") for item in chain[:-1]),
                (location.name, ""),
            ],
            "kind_label": rules.KINDS[location.kind],
            "message": message,
            "editing": False,
        }
        room = next((item for item in reversed(chain) if item.kind == "room"), None)
        furniture = next((item for item in chain if item.kind == "furniture"), None)
        if location.kind == "site":
            rooms = [item for item in repo.children(session, location.id) if item.kind == "room"]
            context["plans"] = [pages.room_plan(session, item) for item in rooms]
        if room is not None:
            context["room"] = pages.room_plan(session, room, current=furniture.id if furniture else None)
            context["room_id"] = room.id
        if location.kind == "room":
            editing = editing or params.get("edition") == "plan"
            context["editing"] = editing
            if editing:
                chosen = selected if selected is not None else _int(params.get("meuble", ""))
                context["selected"] = next((tile for tile in context["room"].tiles if tile.id == chosen), None)
                context["free_cells"] = pages.free_cells(context["room"])
        hit = _int(params.get("localiser", ""))
        located = repo.localise(session, hit) if hit is not None else None
        if located is not None and located.location_id is None:
            located = None
        context["located"] = located
        if furniture is not None:
            as_list = params.get("vue") == "liste"
            group = params.get("series") != "0"
            base = f"/emplacements/{location.id}"
            context.update(
                as_list=as_list,
                group_series=group,
                views=[
                    {"label": _("Meuble"), "href": f"{base}?series={int(group)}", "current": not as_list},
                    {"label": _("Liste"), "href": f"{base}?vue=liste", "current": as_list},
                ],
                series_toggle=f"{base}?series={int(not group)}",
                unit=pages.unit_view(
                    session,
                    furniture,
                    focus=location.id if rules.is_level(location.kind) else None,
                    hit=located.copy.id if located is not None else None,
                    group_series=group,
                    complete=as_list,
                ),
                selectable=as_list and auth.can("holdings.edit"),
                can_arrange=auth.can("holdings.edit"),
            )
            arranging = not as_list and params.get("mode") == "rangement" and auth.can("holdings.edit")
            context["arranging"] = arranging
            picked = params.get("choisi", "")
            context["arrange"] = _arrange(auth, context["unit"], location.id, picked) if arranging else None
        if location.kind == "virtual":
            context["virtual_copies"] = pages.copies_at(session, location.id)
    return _render(request, "holdings/pages/place.html", context, status)


def _arrange(auth: AuthContext, unit: UnitView, place_id: int, chosen: str) -> dict[str, Any]:
    """Le mode Rangement : le dos choisi, et de quoi poster sa nouvelle place."""
    wanted = _int(chosen)
    spine = next((item for level in unit.levels for item in level.spines if item.copy_id == wanted), None)
    return {
        "base": f"/emplacements/{place_id}?mode=rangement",
        "place": place_id,
        "selected": spine.copy_id if spine else None,
        "selected_title": spine.title if spine else "",
        "selected_version": spine.version if spine else 0,
        "csrf": auth.csrf_token,
    }


@router.get("/emplacements/{location_id:int}", include_in_schema=False)
def location_page(request: Request, location_id: int, auth: CanView) -> HTMLResponse:
    return _location_page(request, location_id, auth)


# --- Rangement à l'écran : formulaire, clavier et glisser-déposer postent ici -----------------------------------------
@router.post("/emplacements/ranger", include_in_schema=False)
def arrange_copy(request: Request, auth: CanHold, form: Form) -> Response:
    copy_id, location_id = _int(_first(form, "copie")), _int(_first(form, "lieu"))
    rank, version = _int(_first(form, "rang")), _int(_first(form, "version"))
    level_version, back = _int(_first(form, "version_lieu")), _int(_first(form, "retour"))
    if copy_id is None or location_id is None or rank is None or back is None:
        raise HTTPException(status_code=400)
    expected = {copy_id: version} if version is not None else None
    try:
        result = service.shelve(
            request.app.state.database,
            [copy_id],
            location_id,
            before=rank,
            expected_versions=expected,
            expected_location_version=level_version,
            user_id=auth.user_id,
        )
    except BusinessError as exc:
        status = 409 if isinstance(exc, ConflictError) else 400
        return _location_page(request, back, auth, message=exc.message, status=status)
    code = "copy_moved_over" if result.fill.state == "over" else "copy_moved"
    if not result.changed:
        code = "copy_unchanged"
    return _go(request, f"/emplacements/{back}?mode=rangement#dos-{copy_id}", code)


# --- Tri automatique, avec aperçu -------------------------------------------------------------------------------------
def _sort_page(
    request: Request, level_id: int, mode: str, back: int | None, *, message: str = "", status: int = 200
) -> HTMLResponse:
    database = request.app.state.database
    try:
        preview = service.sort_preview(database, level_id, mode)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    with database.session() as session:
        location = session.get(Location, level_id)
        context = {
            **_shell(session, current=level_id),
            "place": location,
            "preview": preview,
            "before": pages.titles_of(session, preview.before),
            "after": pages.titles_of(session, preview.after),
            "modes": list(rules.SORTS.items()),
            "mode": mode,
            "back": back or level_id,
            "message": message,
        }
    return _render(request, "holdings/pages/sort.html", context, status)


@router.get("/emplacements/{level_id}/trier", include_in_schema=False)
def sort_page(request: Request, level_id: int, _auth: CanHold) -> HTMLResponse:
    params = request.query_params
    mode = params.get("tri", "serie")
    return _sort_page(request, level_id, mode if mode in rules.SORTS else "serie", _int(params.get("retour", "")))


@router.post("/emplacements/{level_id}/trier", include_in_schema=False)
def apply_sort(request: Request, level_id: int, auth: CanHold, form: Form) -> Response:
    mode, version, back = _first(form, "tri"), _int(_first(form, "version_lieu")), _int(_first(form, "retour"))
    if mode not in rules.SORTS:
        raise HTTPException(status_code=400)
    try:
        service.apply_sort(request.app.state.database, level_id, mode, version, auth.user_id)
    except ConflictError as exc:
        return _sort_page(request, level_id, mode, back, message=exc.message, status=409)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    return _go(request, f"/emplacements/{back or level_id}?mode=rangement", "shelf_sorted")


# --- Déplacer plusieurs exemplaires : choisir, voir ce qui va se passer, confirmer ------------------------------------
def _place_options(places: Sequence[repo.PlaceChoice]) -> list[tuple[str, str]]:
    return [("", _("Choisissez…")), *((str(item.id), item.label) for item in places)]


def _ids(values: Sequence[Any]) -> list[int]:
    found = [_int(value) for value in values if isinstance(value, str)]
    return list(dict.fromkeys(item for item in found if item is not None))


def _group_page(
    request: Request,
    copy_ids: list[int],
    selected: str,
    rank: str,
    *,
    verified: bool = False,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    database = request.app.state.database
    location_id, position = _int(selected), _int(rank)
    plan = None
    try:
        if location_id is not None:
            plan = service.plan_shelving(
                database, copy_ids, location_id, position=position, keep_processing=not verified
            )
    except BusinessError as exc:
        message = exc.message
        status = 400
    with database.session() as session:
        context = {
            **_shell(session),
            "copies": pages.titles_of(session, copy_ids),
            "copy_ids": copy_ids,
            "place_options": _place_options(repo.place_choices(session)),
            "selected": selected,
            "rank": rank,
            "plan": plan,
            "verified": verified,
            "status_labels": COPY_STATUSES,
            "message": message,
        }
    return _render(request, "holdings/pages/group_move.html", context, status)


@router.get("/emplacements/deplacer-groupe", include_in_schema=False)
def group_move_page(request: Request, _auth: CanHold) -> HTMLResponse:
    params = request.query_params
    copy_ids = _ids(params.getlist("exemplaire"))
    if not copy_ids:
        raise HTTPException(status_code=400)
    return _group_page(
        request, copy_ids, params.get("lieu", ""), params.get("rang", ""), verified=params.get("verifie") == "1"
    )


@router.post("/emplacements/deplacer-groupe", include_in_schema=False)
def group_move(request: Request, auth: CanHold, form: Form) -> Response:
    copy_ids = _ids(form.getlist("exemplaire"))
    selected, rank = _first(form, "lieu"), _first(form, "rang")
    location_id = _int(selected)
    if not copy_ids or location_id is None:
        raise HTTPException(status_code=400)
    expected = {item: value for item in copy_ids if (value := _int(_first(form, f"v_{item}"))) is not None}
    verified = _first(form, "verifie") == "1"
    try:
        result = service.shelve(
            request.app.state.database,
            copy_ids,
            location_id,
            position=_int(rank),
            expected_versions=expected,
            expected_location_version=_int(_first(form, "version_lieu")),
            keep_processing=not verified,  # « En place » seulement si le rangement est vérifié (13 novembre)
            user_id=auth.user_id,
        )
    except BusinessError as exc:
        status = 409 if isinstance(exc, ConflictError) else 400
        return _group_page(request, copy_ids, selected, rank, verified=verified, message=exc.message, status=status)
    code = "group_moved_over" if result.fill.state == "over" else "group_moved"
    if not result.changed:
        code = "group_unchanged"
    return _go(request, f"/emplacements/{location_id}", code)


# --- Ranger en scannant (D12) -----------------------------------------------------------------------------------------
def _scan_page(
    request: Request, auth: AuthContext, selected: str, *, message: str = "", status: int = 200
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        places = repo.place_choices(session)
        context = {
            **_shell(session),
            "place_options": _place_options(places),
            "selected": selected,
            "log": repo.scan_log(session, auth.user_id),
            "message": message,
        }
    return _render(request, "holdings/pages/scan.html", context, status)


@router.get("/emplacements/scanner", include_in_schema=False)
def scan_page(request: Request, auth: CanHold) -> HTMLResponse:
    return _scan_page(request, auth, request.query_params.get("lieu", ""))


@router.post("/emplacements/scanner", include_in_schema=False)
def scan(request: Request, auth: CanHold, form: Form) -> Response:
    selected, code = _first(form, "lieu"), _first(form, "code")
    location_id = _int(selected)
    if location_id is None:
        return _scan_page(request, auth, selected, message=_("Choisissez d’abord l’étagère à remplir."), status=400)
    if not code.strip():
        return _scan_page(request, auth, selected, message=_("Scannez ou saisissez un code."), status=400)
    try:
        result = service.shelve_by_code(request.app.state.database, code, location_id, user_id=auth.user_id)
    except BusinessError as exc:
        return _scan_page(request, auth, selected, message=exc.message, status=400)
    flag = "scan_same" if result.already_there else "scan_over" if result.fill.state == "over" else "scan_ok"
    return _go(request, f"/emplacements/scanner?lieu={location_id}", flag)


@router.post("/emplacements/scanner/annuler", include_in_schema=False)
def undo_scan(request: Request, auth: CanHold, form: Form) -> Response:
    selected, entry = _first(form, "lieu"), _int(_first(form, "entree"))
    database = request.app.state.database
    with database.session() as session:
        found = repo.scan_entry(session, entry, auth.user_id) if entry is not None else None
    if found is None:
        raise HTTPException(status_code=404)
    copy_id, data = found
    try:
        service.undo_shelving(
            database,
            copy_id,
            previous_location_id=data.get("de_id"),  # type: ignore[arg-type]
            previous_position=data.get("de_rang"),  # type: ignore[arg-type]
            previous_status=str(data.get("statut_avant")),
            expected_version=int(data.get("version", 0)),  # type: ignore[call-overload]
            user_id=auth.user_id,
        )
    except BusinessError as exc:
        return _scan_page(request, auth, selected, message=exc.message, status=409)
    return _go(request, f"/emplacements/scanner?lieu={selected}", "scan_undone")
