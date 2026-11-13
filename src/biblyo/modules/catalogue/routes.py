# SPDX-License-Identifier: EUPL-1.2
"""Fiches du catalogue : consultation, création, modification par section, éditions et exemplaires.

Les routes décident de la réponse, le service des règles. Chaque route exige sa permission, côté serveur.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from starlette.datastructures import FormData, UploadFile

from biblyo.core import flash, remote
from biblyo.core.clock import now_iso
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _
from biblyo.core.media import MAX_UPLOAD_BYTES, STORED_MIME, resolve_media_path
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext, get_auth
from biblyo.core.text import normalize
from biblyo.modules.catalogue import covers, forms, listing, search, series_view, service, taxonomy
from biblyo.modules.catalogue import repository as repo
from biblyo.modules.catalogue.models import Agent, Collection, Copy, Edition, Publisher, Series, Tag
from biblyo.modules.catalogue.rules import (
    AUDIENCES,
    CONTRIBUTOR_ROLES,
    LANGUAGES,
    SERIES_STATUSES,
    InvalidVolume,
    parse_volume,
)
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.holdings import repository as holdings

router = APIRouter()

CanView = Annotated[AuthContext, require("catalogue.view")]
CanEdit = Annotated[AuthContext, require("catalogue.edit")]
CanArchive = Annotated[AuthContext, require("catalogue.archive")]
CanHold = Annotated[AuthContext, require("holdings.edit")]

TABS = ("apercu", "notice", "editions", "exemplaires", "prets", "enrichissement")
SECTIONS = ("identification", "serie", "publication", "contenu")
ORIGINS = {"manual": _("Saisie manuelle"), "import": _("Import"), "bnf": _("BnF"), "sudoc": _("Sudoc")}
MAX_SUGGESTIONS = 12
PALETTE_RESULTS = 8


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _go_with_flash(request: Request, path: str, code: str) -> Response:
    response = _go(path)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.render(request, template, context, status_code=status)
    return response


def _choices(session: Any, *, keep_type: int | None = None, keep_section: int | None = None) -> dict[str, Any]:
    sections = repo.sections(session, include_id=keep_section)
    return {
        "type_options": [("", _("Choisir…"))]
        + [(str(item.id), item.label) for item in repo.document_types(session, include_id=keep_type)],
        "section_options": [("", _("Aucune"))] + [(str(item.id), item.label) for item in sections],
        "audience_options": [("", _("Non renseigné")), *AUDIENCES.items()],
        "language_options": list(LANGUAGES.items()),
        "role_options": list(CONTRIBUTOR_ROLES.items()),
    }


def _sort_options(criteria: listing.Criteria) -> list[tuple[str, str]]:
    """L'option vide est le tri par défaut : la pertinence s'il y a une recherche, sinon le titre."""
    has_place = criteria.emplacement.isdigit()
    sorts = {key: label for key, label in listing.SORTS.items() if key != listing.PHYSICAL_ORDER or has_place}
    if search.build_match(criteria.q) is not None:
        return [("", listing.RELEVANCE_LABEL), *sorts.items()]
    return [("", sorts["titre"]), *((key, label) for key, label in sorts.items() if key != "titre")]


# --- Liste du catalogue -----------------------------------------------------------------------------------------------
def _catalogue_page(request: Request, auth: AuthContext, *, message: str = "", status: int = 200) -> HTMLResponse:
    criteria = listing.Criteria.from_params(request.query_params)
    with request.app.state.database.session() as session:
        result = listing.run(session, criteria)
        options = listing.filter_options(session, criteria)
        saved = repo.saved_searches(session, auth.user_id)
        catalogue_total = repo.active_work_count(session)
    context = {
        "criteria": criteria,
        "result": result,
        "options": options,
        "saved": saved,
        "catalogue_total": catalogue_total,
        "message": message,
        "sort_options": _sort_options(criteria),
        "meta_options": list(listing.META.items()),
        "copies_options": list(listing.COPIES.items()),
        "status_options": list(listing.STATUSES.items()),
        "language_options": list(LANGUAGES.items()),
        "audience_options": list(AUDIENCES.items()),
        "planned_visible": request.app.state.settings.planned_modules_visible,
    }
    response = _render(request, "catalogue/pages/catalogue.html", context, status)
    if request.headers.get("HX-Request") == "true":
        response.headers["HX-Push-Url"] = criteria.url()  # l'adresse propre, sans critère vide
    return response


@router.get("/catalogue", include_in_schema=False)
def catalogue_page(request: Request, auth: CanView) -> HTMLResponse:
    return _catalogue_page(request, auth)


def _criteria_from_form(form: FormData) -> listing.Criteria:
    """Les critères à enregistrer arrivent de la page affichée, sous forme d'adresse : ils repassent par la lecture."""
    return listing.Criteria.from_params(dict(parse_qsl(forms.first(form, "criteres"))))


@router.post("/catalogue/recherches", include_in_schema=False)
def save_search(request: Request, auth: CanView, form: Form) -> Response:
    criteria = _criteria_from_form(form)
    try:
        service.save_search(request.app.state.database, auth.user_id, forms.first(form, "nom"), criteria.as_dict())
    except BusinessError as exc:
        return _catalogue_page(request, auth, message=exc.message, status=400)
    return _go_with_flash(request, criteria.url(page=1), "search_saved")


@router.post("/catalogue/recherches/{search_id}/supprimer", include_in_schema=False)
def delete_search(request: Request, search_id: int, auth: CanView) -> Response:
    if not service.delete_search(request.app.state.database, auth.user_id, search_id):
        raise HTTPException(status_code=404)
    return _go_with_flash(request, "/catalogue", "search_deleted")


@router.post("/ui/recherche", include_in_schema=False)
def palette_search(request: Request, _auth: CanView, form: Form) -> HTMLResponse:
    """Notices proposées par la palette ⌘K : les plus pertinentes, parmi les notices actives.

    **POST** : on y tape aussi le nom d'un lecteur, qui ne doit passer dans aucune adresse ni aucun journal d'accès."""
    q = forms.first(form, "q")[:200]
    cards: list[repo.CardView] = []
    if search.build_match(q) is not None:
        with request.app.state.database.session() as session:
            cards = listing.run(session, listing.Criteria(q=q), page_size=PALETTE_RESULTS).cards
    return _render(request, "catalogue/fragments/palette_results.html", {"cards": cards})


# --- Séries -----------------------------------------------------------------------------------------------------------
def _series_page(
    request: Request, *, errors: dict[str, str] | None = None, form: FormData | None = None, status: int = 200
) -> HTMLResponse:
    params = request.query_params
    query = params.get("q", "").strip()[:200]
    filter_code = params.get("filtre", "")
    filter_code = filter_code if filter_code in series_view.FILTERS else ""
    page = params.get("page", "")
    current = int(page) if page.isdigit() and len(page) < 9 else 1
    with request.app.state.database.session() as session:
        result = series_view.list_series(session, query, filter_code, current)
    context = {
        "result": result,
        "window": listing.page_window(result.page, result.pages),
        "filters": series_view.FILTERS,
        "filter_code": filter_code,
        "query": query,
        "errors": errors or {},
        "values": {"titre": forms.first(form, "titre")} if form is not None else {},
    }
    return _render(request, "catalogue/pages/series_list.html", context, status)


@router.get("/catalogue/series", include_in_schema=False)
def series_page(request: Request, _auth: CanView) -> HTMLResponse:
    return _series_page(request)


@router.post("/catalogue/series", include_in_schema=False)
def create_series(request: Request, auth: CanEdit, form: Form) -> Response:
    edit = service.SeriesEdit(forms.first(form, "titre"), forms.first(form, "etat") or "unknown")
    try:
        series_id = service.create_series(request.app.state.database, edit, auth.user_id)
    except FormErrors as exc:
        return _series_page(request, errors=exc.fields, form=form, status=400)
    return _go_with_flash(request, f"/catalogue/series/{series_id}", "series_created")


def _series_detail_page(
    request: Request,
    series_id: int,
    *,
    errors: dict[str, str] | None = None,
    form: FormData | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        found = series_view.detail(session, series_id)
    if found is None:
        raise HTTPException(status_code=404)
    context = {
        "detail": found,
        "errors": errors or {},
        "form_titre": forms.first(form, "titre") if form is not None else found.card.title,
        "form_notes": forms.first(form, "notes") if form is not None else (found.card.notes or ""),
        "form_etat": forms.first(form, "etat") if form is not None else found.card.status,
        "message": message,
        "status_options": list(SERIES_STATUSES.items()),
    }
    return _render(request, "catalogue/pages/series_detail.html", context, status)


@router.get("/catalogue/series/{series_id}", include_in_schema=False)
def series_detail(request: Request, series_id: int, _auth: CanView) -> HTMLResponse:
    return _series_detail_page(request, series_id)


@router.post("/catalogue/series/{series_id}", include_in_schema=False)
def update_series(request: Request, series_id: int, auth: CanEdit, form: Form) -> Response:
    edit = service.SeriesEdit(forms.first(form, "titre"), forms.first(form, "etat"), forms.first(form, "notes"))
    try:
        service.update_series_record(request.app.state.database, series_id, edit, auth.user_id)
    except FormErrors as exc:
        return _series_detail_page(request, series_id, errors=exc.fields, form=form, status=400)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    return _go_with_flash(request, f"/catalogue/series/{series_id}", "series_updated")


@router.post("/catalogue/series/{series_id}/supprimer", include_in_schema=False)
def delete_series(request: Request, series_id: int, auth: CanEdit) -> Response:
    try:
        service.delete_series(request.app.state.database, series_id, auth.user_id)
    except BusinessError as exc:
        return _series_detail_page(request, series_id, message=exc.message, status=400)
    return _go_with_flash(request, "/catalogue/series", "series_deleted")


@router.post("/catalogue/series/{series_id}/tomes-confirmes", include_in_schema=False)
def confirm_volumes(request: Request, series_id: int, auth: CanEdit, form: Form) -> Response:
    try:
        service.confirm_volumes(
            request.app.state.database,
            series_id,
            forms.first(form, "premier"),
            forms.first(form, "dernier"),
            forms.first(form, "note"),
            auth.user_id,
        )
    except FormErrors as exc:
        return _series_detail_page(request, series_id, errors=exc.fields, form=form, status=400)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    return _go_with_flash(request, f"/catalogue/series/{series_id}", "volumes_confirmed")


@router.post("/catalogue/series/{series_id}/tomes-confirmes/retirer", include_in_schema=False)
def unconfirm_volume(request: Request, series_id: int, auth: CanEdit, form: Form) -> Response:
    try:
        number = parse_volume(forms.first(form, "numero")).number
    except InvalidVolume:
        number = None
    if number is None:
        raise HTTPException(status_code=404)
    service.unconfirm_volume(request.app.state.database, series_id, number, auth.user_id)
    return _go_with_flash(request, f"/catalogue/series/{series_id}", "volume_unconfirmed")


# --- Création ---------------------------------------------------------------------------------------------------------
def _new_page(
    request: Request,
    values: dict[str, str],
    rows: dict[str, Any],
    errors: dict[str, str],
    status: int = 200,
    prefill: dict[str, Any] | None = None,
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        choices = _choices(session)
    context = {"values": values, "errors": errors, "rows": rows, "prefill": prefill, **choices}
    return _render(request, "catalogue/pages/notice_new.html", context, status)


def _from_source(request: Request, proposal: int, edition_verified: bool, isbn_verified: bool) -> dict[str, Any] | None:
    """Ce qu'un résultat d'une source bibliographique propose pour ce formulaire (module d'enrichissement)."""
    database = request.app.state.database
    provided = _provided(request, "notice_prefill", database, proposal, edition_verified, isbn_verified)
    return provided if isinstance(provided, dict) else None


@router.get("/catalogue/notices/nouvelle", include_in_schema=False)
def new_notice(
    request: Request,
    _auth: CanEdit,
    depuis: Annotated[int | None, Query()] = None,
    edition_verifiee: Annotated[str, Query()] = "",
    isbn_verifie: Annotated[str, Query()] = "",
) -> Response:
    blank = forms._with_spare([], 2)
    if depuis is None:
        return _new_page(request, {"langue": "fre"}, {"work": blank, "edition": forms._with_spare([], 1)}, {})
    provided = _from_source(request, depuis, edition_verifiee == "1", isbn_verifie == "1")
    if provided is None:
        raise HTTPException(status_code=404)
    if "confirm" in provided:
        return _go(provided["confirm"])
    values = {"langue": "fre", **provided["values"], "depuis": str(depuis), "isbn_verifie": isbn_verifie}
    with request.app.state.database.session() as session:
        people = [
            {"nom": name, "role": role, "choix": "", "dates": dates, "matches": forms.person_choices(session, name)}
            for name, role, dates in provided["contributors"]
        ]
    rows = {"work": forms._with_spare(people, 1), "edition": forms._with_spare([], 1)}
    return _new_page(request, values, rows, {}, prefill=provided["banner"])


@router.post("/catalogue/notices", include_in_schema=False)
def create_notice(request: Request, auth: CanEdit, form: Form) -> Response:
    try:
        work_id = service.create_notice(
            request.app.state.database,
            forms.identity_from(form),
            forms.series_from(form),
            forms.content_from(form),
            forms.edition_from(form),
            auth.user_id,
        )
    except FormErrors as exc:
        with request.app.state.database.session() as session:
            rows = {
                "work": forms.rows_from_form(session, form, forms.WORK_PREFIX),
                "edition": forms.rows_from_form(session, form, forms.EDITION_PREFIX),
            }
        source = forms.optional_int(forms.first(form, "depuis"))
        provided = _from_source(request, source, True, forms.first(form, "isbn_verifie") == "1") if source else None
        banner = provided.get("banner") if provided else None
        return _new_page(request, forms.form_values(form), rows, exc.fields, 400, prefill=banner)
    source = forms.optional_int(forms.first(form, "depuis"))
    if source is not None:
        _provided(
            request,
            "notice_created",
            request.app.state.database,
            source,
            work_id,
            forms.first(form, "isbn_verifie") == "1",
            auth.user_id,
        )
    return _go_with_flash(request, f"/catalogue/notices/{work_id}", "notice_created")


# --- Fiche ------------------------------------------------------------------------------------------------------------
def _tab_links(notice: repo.NoticeView, active: str, edition_id: int | None) -> list[dict[str, Any]]:
    base = f"/catalogue/notices/{notice.id}"

    def href(tab: str) -> str:
        return base if tab == "apercu" else f"{base}?onglet={tab}"

    return [
        {"label": _("Aperçu"), "href": href("apercu"), "current": active == "apercu", "count": None},
        {"label": _("Notice"), "href": href("notice"), "current": active == "notice", "count": None},
        {
            "label": _("Éditions"),
            "href": href("editions"),
            "current": active == "editions",
            "count": len(notice.editions),
        },
        {
            "label": _("Exemplaires"),
            "href": href("exemplaires"),
            "current": active == "exemplaires",
            "count": len(notice.copies),
        },
        {"label": _("Prêts"), "href": href("prets"), "current": active == "prets", "count": None},
        {
            "label": _("Enrichissement"),
            "href": href("enrichissement"),
            "current": active == "enrichissement",
            "count": None,
        },
    ]


def _provided(request: Request, name: str, *arguments: Any) -> Any:
    """Ce qu'un autre module ajoute à la fiche (les prêts, en L4). Rien s'il n'est pas installé."""
    for module in request.app.state.modules:
        provider = module.extras.get(name)
        if callable(provider):
            return provider(*arguments)
    return None


def _notice_page(
    request: Request,
    work_id: int,
    *,
    tab: str = "apercu",
    editing: str = "",
    edition_id: int | None = None,
    errors: dict[str, str] | None = None,
    form: FormData | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        notice = repo.load_notice(session, work_id)
        if notice is None:
            raise HTTPException(status_code=404)
        selected = next((item for item in notice.editions if item.id == edition_id), notice.primary)
        places = {item.id: holdings.localise(session, item.id) for item in notice.copies if item.location_id}
        book_loans = _provided(request, "book_loans", session, get_auth(request), work_id) if tab == "prets" else None
        enrichment = (
            _provided(request, "enrichment_tab", session, get_auth(request), work_id, selected.id)
            if tab == "enrichissement"
            else None
        )
        context: dict[str, Any] = {
            "notice": notice,
            "places": places,
            "edition": selected,
            "tab": tab,
            "tabs_links": _tab_links(notice, tab, edition_id),
            "editing": editing,
            "errors": errors or {},
            "message": message,
            "origin_label": ORIGINS.get(selected.origin, selected.origin),
            "planned_visible": request.app.state.settings.planned_modules_visible,
            "book_loans": book_loans,
            **(enrichment or {"enrichment": None}),
            "rows": {},
            "values": {},
            **_choices(
                session, keep_type=notice.document_type.id, keep_section=notice.section.id if notice.section else None
            ),
        }
        if editing:
            if form is not None:
                context["values"] = forms.form_values(form)
                context["rows"] = {
                    "work": forms.rows_from_form(session, form, forms.WORK_PREFIX),
                    "edition": forms.rows_from_form(session, form, forms.EDITION_PREFIX),
                }
            else:
                context["values"] = {
                    "identification": forms.identity_values(notice),
                    "serie": forms.series_values(notice),
                    "contenu": forms.content_values(notice),
                    "publication": forms.edition_values(selected),
                }.get(editing, {})
                context["rows"] = {
                    "work": forms.rows_from_people(session, notice.contributors),
                    "edition": forms.rows_from_people(session, selected.contributors),
                }
    return _render(request, "catalogue/pages/notice.html", context, status)


@router.get("/catalogue/notices/{work_id}", include_in_schema=False)
def notice_page(
    request: Request,
    work_id: int,
    auth: CanView,
    onglet: Annotated[str, Query()] = "apercu",
    modifier: Annotated[str, Query()] = "",
    edition: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    tab = onglet if onglet in TABS else "apercu"
    editing = modifier if modifier in SECTIONS and auth.can("catalogue.edit") else ""
    return _notice_page(request, work_id, tab="notice" if editing else tab, editing=editing, edition_id=edition)


@router.post("/catalogue/notices/{work_id}/sections/{section}", include_in_schema=False)
def save_section(request: Request, work_id: int, section: str, auth: CanEdit, form: Form) -> Response:
    if section not in SECTIONS:
        raise HTTPException(status_code=404)
    database = request.app.state.database
    version = forms.form_version(form)
    edition_id = forms.optional_int(forms.first(form, "edition_id"))
    try:
        if section == "identification":
            service.update_identity(database, work_id, forms.identity_from(form), version, auth.user_id)
        elif section == "serie":
            service.update_series(database, work_id, forms.series_from(form), version, auth.user_id)
        elif section == "contenu":
            service.update_content(database, work_id, forms.content_from(form), version, auth.user_id)
        else:
            if edition_id is None:
                raise HTTPException(status_code=404)
            service.update_edition(database, edition_id, forms.edition_from(form), version, auth.user_id)
    except FormErrors as exc:
        return _notice_page(
            request,
            work_id,
            tab="notice",
            editing=section,
            edition_id=edition_id,
            errors=exc.fields,
            form=form,
            status=400,
        )
    except ConflictError as exc:
        return _notice_page(
            request, work_id, tab="notice", editing=section, edition_id=edition_id, message=exc.message, status=409
        )
    except BusinessError:
        raise HTTPException(status_code=404) from None
    suffix = f"&edition={edition_id}" if section == "publication" and edition_id else ""
    return _go_with_flash(request, f"/catalogue/notices/{work_id}?onglet=notice{suffix}", "notice_updated")


def _action(request: Request, work_id: int, call: Any, code: str, *, tab: str = "apercu", target: str = "") -> Response:
    """Une action sans saisie : refusée, elle réaffiche la fiche et dit pourquoi."""
    try:
        call()
    except BusinessError as exc:
        return _notice_page(request, work_id, tab=tab, message=exc.message, status=400)
    return _go_with_flash(request, target or f"/catalogue/notices/{work_id}?onglet={tab}", code)


@router.post("/catalogue/notices/{work_id}/archiver", include_in_schema=False)
def archive_notice(request: Request, work_id: int, auth: CanArchive) -> Response:
    database = request.app.state.database
    return _action(request, work_id, lambda: service.archive_work(database, work_id, auth.user_id), "notice_archived")


@router.post("/catalogue/notices/{work_id}/desarchiver", include_in_schema=False)
def unarchive_notice(request: Request, work_id: int, auth: CanArchive) -> Response:
    database = request.app.state.database
    return _action(request, work_id, lambda: service.unarchive_work(database, work_id, auth.user_id), "notice_restored")


@router.post("/catalogue/notices/{work_id}/supprimer", include_in_schema=False)
def delete_notice(request: Request, work_id: int, auth: CanArchive) -> Response:
    database = request.app.state.database
    return _action(
        request,
        work_id,
        lambda: service.delete_work(database, work_id, auth.user_id),
        "notice_deleted",
        target="/catalogue",
    )


# --- Éditions ---------------------------------------------------------------------------------------------------------
def _edition_page(
    request: Request, work_id: int, values: dict[str, str], rows: list[Any], errors: dict[str, str], status: int = 200
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        notice = repo.load_notice(session, work_id)
        if notice is None:
            raise HTTPException(status_code=404)
        choices = _choices(session)
    context = {"notice": notice, "values": values, "rows": {"edition": rows}, "errors": errors, **choices}
    return _render(request, "catalogue/pages/edition_new.html", context, status)


@router.get("/catalogue/notices/{work_id}/editions/nouvelle", include_in_schema=False)
def new_edition(request: Request, work_id: int, _auth: CanEdit) -> HTMLResponse:
    return _edition_page(request, work_id, {"langue": "fre"}, forms._with_spare([], 1), {})


@router.post("/catalogue/notices/{work_id}/editions/nouvelle", include_in_schema=False)
def create_edition(request: Request, work_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    try:
        service.add_edition(database, work_id, forms.edition_from(form), auth.user_id)
    except FormErrors as exc:
        with database.session() as session:
            rows = forms.rows_from_form(session, form, forms.EDITION_PREFIX)
        return _edition_page(request, work_id, forms.form_values(form), rows, exc.fields, 400)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    return _go_with_flash(request, f"/catalogue/notices/{work_id}?onglet=editions", "edition_added")


def _edition_work(request: Request, edition_id: int) -> int:
    with request.app.state.database.session() as session:
        edition = session.get(Edition, edition_id)
        if edition is None:
            raise HTTPException(status_code=404)
        work_id: int = edition.work_id
    return work_id


def _edition_action(request: Request, edition_id: int, call: Any, code: str) -> Response:
    work_id = _edition_work(request, edition_id)
    return _action(request, work_id, call, code, tab="editions")


@router.post("/catalogue/editions/{edition_id}/principale", include_in_schema=False)
def primary_edition(request: Request, edition_id: int, auth: CanEdit) -> Response:
    database = request.app.state.database
    return _edition_action(
        request, edition_id, lambda: service.set_primary_edition(database, edition_id, auth.user_id), "edition_primary"
    )


@router.post("/catalogue/editions/{edition_id}/archiver", include_in_schema=False)
def archive_edition(request: Request, edition_id: int, auth: CanArchive) -> Response:
    database = request.app.state.database
    return _edition_action(
        request, edition_id, lambda: service.archive_edition(database, edition_id, auth.user_id), "edition_archived"
    )


@router.post("/catalogue/editions/{edition_id}/desarchiver", include_in_schema=False)
def unarchive_edition(request: Request, edition_id: int, auth: CanArchive) -> Response:
    database = request.app.state.database
    return _edition_action(
        request, edition_id, lambda: service.unarchive_edition(database, edition_id, auth.user_id), "edition_restored"
    )


@router.post("/catalogue/editions/{edition_id}/supprimer", include_in_schema=False)
def delete_edition(request: Request, edition_id: int, auth: CanArchive) -> Response:
    database = request.app.state.database
    return _edition_action(
        request, edition_id, lambda: service.delete_edition(database, edition_id, auth.user_id), "edition_deleted"
    )


# --- Couvertures ------------------------------------------------------------------------------------------------------
@router.post("/catalogue/editions/{edition_id}/couverture", include_in_schema=False)
def upload_cover(request: Request, edition_id: int, auth: CanEdit, form: Form) -> Response:
    work_id = _edition_work(request, edition_id)
    upload = form.get("couverture")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return _notice_page(request, work_id, tab="editions", message=_("Choisissez un fichier image."), status=400)
    data = upload.file.read(MAX_UPLOAD_BYTES + 1)
    settings = request.app.state.settings
    database = request.app.state.database
    return _action(
        request,
        work_id,
        lambda: service.set_cover(database, edition_id, data, settings.media_dir, auth.user_id),
        "cover_saved",
        tab="editions",
    )


@router.post("/catalogue/editions/{edition_id}/couverture/retirer", include_in_schema=False)
def remove_cover(request: Request, edition_id: int, auth: CanEdit) -> Response:
    database = request.app.state.database
    return _action(
        request,
        _edition_work(request, edition_id),
        lambda: service.remove_cover(database, edition_id, auth.user_id),
        "cover_removed",
        tab="editions",
    )


# --- Page Couverture (conception 17 § 3) : fichier, adresse, recherche, aperçu, historique ----------------------
COVER_CONFIRM_MATCHES = covers.NEEDS_CONFIRMATION


def _cover_base(work_id: int) -> str:
    return f"/catalogue/notices/{work_id}/couverture"


def _cover_page(
    request: Request,
    work_id: int,
    edition_id: int | None,
    *,
    errors: dict[str, str] | None = None,
    form: FormData | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    auth = get_auth(request)
    with request.app.state.database.session() as session:
        notice = repo.load_notice(session, work_id)
        if notice is None:
            raise HTTPException(status_code=404)
        edition = next((item for item in notice.editions if item.id == edition_id), notice.primary)
        history = covers.history(session, edition.id, edition.cover.media_id if edition.cover else None)
    search_ready = bool(_provided(request, "cover_search_ready", auth))
    context = {
        "notice": notice,
        "edition": edition,
        "history": history,
        "base": _cover_base(work_id),
        "rights": covers.RIGHTS,
        "source_labels": covers.SOURCE_LABELS,
        "errors": errors or {},
        "form": {k: v for k, v in form.items() if isinstance(v, str)} if form else {},
        "message": message,
        "search_ready": search_ready and auth is not None and auth.can("catalogue.edit"),
        "search_now": request.query_params.get("rechercher") == "1",
        "max_bytes": MAX_UPLOAD_BYTES,
    }
    return _render(request, "catalogue/pages/cover.html", context, status)


def _cover_edition(request: Request, work_id: int, value: str) -> int:
    edition_id = forms.optional_int(value)
    if edition_id is None or _edition_work(request, edition_id) != work_id:
        raise HTTPException(status_code=404)
    return edition_id


@router.get("/catalogue/notices/{work_id}/couverture", include_in_schema=False)
def cover_page(
    request: Request, work_id: int, _auth: CanView, edition: Annotated[int | None, Query()] = None
) -> HTMLResponse:
    return _cover_page(request, work_id, edition)


def _stage_or_page(
    request: Request, work_id: int, edition_id: int, form: FormData, data_of: Any, **meta: Any
) -> Response:
    """Range l'aperçu et l'ouvre ; un refus réaffiche la page Couverture avec son message."""
    settings = request.app.state.settings
    auth = get_auth(request)
    try:
        data, extra = data_of()
        staged = covers.stage(
            settings.media_dir,
            data,
            in_use=covers.in_use_checker(request.app.state.database),
            user_id=auth.user_id if auth else None,
            work_id=work_id,
            edition_id=edition_id,
            **meta,
            **extra,
        )
    except FormErrors as exc:
        return _cover_page(request, work_id, edition_id, errors=exc.fields, form=form, status=400)
    except BusinessError as exc:
        return _cover_page(request, work_id, edition_id, message=exc.message, form=form, status=400)
    return _go(f"{_cover_base(work_id)}/apercu/{staged.token}")


@router.post("/catalogue/notices/{work_id}/couverture/fichier", include_in_schema=False)
def cover_from_file(request: Request, work_id: int, auth: CanEdit, form: Form) -> Response:
    edition_id = _cover_edition(request, work_id, forms.first(form, "edition_id"))
    errors: dict[str, str] = {}
    note = covers.rights_note(forms.first(form, "droit"), forms.first(form, "droit_detail"), errors=errors)
    upload = form.get("couverture")

    def data_of() -> tuple[bytes, dict[str, Any]]:
        if not isinstance(upload, UploadFile) or not upload.filename:
            errors["couverture"] = _("Choisissez ou déposez un fichier image.")
        covers.form_errors(errors)
        assert isinstance(upload, UploadFile)
        return upload.file.read(MAX_UPLOAD_BYTES + 1), {}

    return _stage_or_page(request, work_id, edition_id, form, data_of, kind="upload", rights_note=note)


@router.post("/catalogue/notices/{work_id}/couverture/adresse", include_in_schema=False)
def cover_from_url(request: Request, work_id: int, auth: CanEdit, form: Form) -> Response:
    edition_id = _cover_edition(request, work_id, forms.first(form, "edition_id"))
    errors: dict[str, str] = {}
    note = covers.rights_note(forms.first(form, "droit"), forms.first(form, "droit_detail"), errors=errors)
    address = forms.first(form, "adresse").strip()

    def data_of() -> tuple[bytes, dict[str, Any]]:
        if not address:
            errors["adresse"] = _("Indiquez l’adresse de l’image, en https://.")
        covers.form_errors(errors)
        fetch = getattr(request.app.state, "fetch_remote_image", None) or remote.fetch_image
        image = fetch(address)
        return image.body, {"reference": address[:2048], "origin_url": image.url[:2048]}

    return _stage_or_page(request, work_id, edition_id, form, data_of, kind="url", rights_note=note)


def _preview_page(
    request: Request,
    staged: covers.Staged,
    *,
    errors: dict[str, str] | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        notice = repo.load_notice(session, staged.work_id)
        if notice is None:
            raise HTTPException(status_code=404)
        edition = next((item for item in notice.editions if item.id == staged.edition_id), None)
        if edition is None:
            raise HTTPException(status_code=404)
    context = {
        "notice": notice,
        "edition": edition,
        "staged": staged,
        "base": _cover_base(staged.work_id),
        "rights": covers.SOURCE_RIGHTS if staged.needs_rights else covers.RIGHTS,
        "kind_labels": covers.KIND_LABELS,
        "source_labels": covers.SOURCE_LABELS,
        "needs_match": staged.match in COVER_CONFIRM_MATCHES,
        "match_labels": covers.MATCH_LABELS,
        "errors": errors or {},
        "message": message,
    }
    return _render(request, "catalogue/pages/cover_preview.html", context, status)


def _staged(request: Request, work_id: int, token: str) -> covers.Staged:
    auth = get_auth(request)
    staged = covers.load(request.app.state.settings.media_dir, token, auth.user_id if auth else None)
    if staged is None or staged.work_id != work_id:
        raise HTTPException(status_code=404)
    return staged


@router.get("/catalogue/notices/{work_id}/couverture/apercu/{token}", include_in_schema=False)
def cover_preview(request: Request, work_id: int, token: str, _auth: CanEdit) -> HTMLResponse:
    return _preview_page(request, _staged(request, work_id, token))


@router.post("/catalogue/notices/{work_id}/couverture/apercu/{token}", include_in_schema=False)
def cover_confirm(request: Request, work_id: int, token: str, auth: CanEdit, form: Form) -> Response:
    staged = _staged(request, work_id, token)
    settings = request.app.state.settings
    target = f"{_cover_base(work_id)}?edition={staged.edition_id}"
    if forms.first(form, "action") == "abandonner":
        covers.discard(settings.media_dir, token, in_use=covers.in_use_checker(request.app.state.database))
        return _go_with_flash(request, target, "cover_discarded")
    errors: dict[str, str] = {}
    note = staged.rights_note
    if staged.needs_rights:
        note = covers.rights_note(
            forms.first(form, "droit"), forms.first(form, "droit_detail"), errors=errors, allowed=covers.SOURCE_RIGHTS
        )
    if staged.match in COVER_CONFIRM_MATCHES and forms.first(form, "correspondance") != "1":
        errors["correspondance"] = _("Confirmez que cette image correspond bien à votre édition.")
    if errors:
        return _preview_page(request, staged, errors=errors, status=400)
    expected = forms.optional_int(forms.first(form, "attendue"))
    origin = staged.source_code if staged.kind == "source" else staged.kind
    provenance = service.CoverOrigin(
        origin=origin,
        origin_url=staged.origin_url,
        source_code=staged.source_code,
        external_id=staged.reference,
        license_note=" ".join(part for part in (staged.license_note, note if staged.needs_rights else "") if part)
        or note,
        retrieved_at=now_iso() if staged.kind != "upload" else "",
    )
    change = service.CoverChangeInput(
        staged.kind,
        source_code=staged.source_code or None,
        reference=staged.reference or None,
        rights_note=note or None,
    )
    try:
        service.save_cover(
            request.app.state.database,
            staged.edition_id,
            staged.stored,
            auth.user_id,
            provenance=provenance,
            change=change,
            expected_media_id=expected,
            replace=forms.first(form, "remplacer") == "1",
        )
    except service.CoverConflict as exc:
        return _preview_page(request, staged, message=exc.message, status=409)
    except BusinessError as exc:
        return _preview_page(request, staged, message=exc.message, status=400)
    covers.discard(settings.media_dir, token)
    return _go_with_flash(request, f"/catalogue/notices/{work_id}?edition={staged.edition_id}", "cover_saved")


@router.post("/catalogue/notices/{work_id}/couverture/supprimer", include_in_schema=False)
def cover_delete(request: Request, work_id: int, auth: CanEdit, form: Form) -> Response:
    edition_id = _cover_edition(request, work_id, forms.first(form, "edition_id"))
    try:
        service.remove_cover(request.app.state.database, edition_id, auth.user_id)
    except BusinessError as exc:
        return _cover_page(request, work_id, edition_id, message=exc.message, status=400)
    return _go_with_flash(request, f"{_cover_base(work_id)}?edition={edition_id}", "cover_removed")


@router.post("/catalogue/notices/{work_id}/couverture/retablir", include_in_schema=False)
def cover_restore(request: Request, work_id: int, auth: CanEdit, form: Form) -> Response:
    edition_id = _cover_edition(request, work_id, forms.first(form, "edition_id"))
    try:
        service.restore_previous_cover(request.app.state.database, edition_id, auth.user_id)
    except BusinessError as exc:
        return _cover_page(request, work_id, edition_id, message=exc.message, status=409)
    return _go_with_flash(request, f"{_cover_base(work_id)}?edition={edition_id}", "cover_restored")


@router.get("/medias/{name}", include_in_schema=False)
def media_file(request: Request, name: str, _auth: CanView) -> FileResponse:
    """Image rangée par le service. Le nom est contrôlé : une empreinte, jamais un chemin."""
    path = resolve_media_path(request.app.state.settings.media_dir, name)
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type=STORED_MIME, headers={"Cache-Control": "private, max-age=31536000, immutable"})


# --- Exemplaires : le noyau de L1 ------------------------------------------------------------------------------------
@router.post("/catalogue/notices/{work_id}/exemplaires", include_in_schema=False)
def add_copies(request: Request, work_id: int, auth: CanHold, form: Form) -> Response:
    edition_id = forms.optional_int(forms.first(form, "edition_id"))
    quantity = forms.optional_int(forms.first(form, "quantite")) or 0
    if edition_id is None or _edition_work(request, edition_id) != work_id:
        raise HTTPException(status_code=404)
    database = request.app.state.database
    try:
        service.add_copies(database, edition_id, quantity, forms.first(form, "note"), auth.user_id)
    except FormErrors as exc:
        return _notice_page(request, work_id, tab="exemplaires", errors=exc.fields, status=400)
    except BusinessError as exc:
        return _notice_page(request, work_id, tab="exemplaires", message=exc.message, status=400)
    return _go_with_flash(request, f"/catalogue/notices/{work_id}?onglet=exemplaires", "copies_added")


@router.post("/catalogue/exemplaires/{copy_id}/supprimer", include_in_schema=False)
def delete_copy(request: Request, copy_id: int, auth: CanHold) -> Response:
    try:
        work_id = service.delete_copy(request.app.state.database, copy_id, auth.user_id)
    except service.CopyNotFound:
        raise HTTPException(status_code=404) from None
    except BusinessError as exc:
        work_id = _copy_work(request, copy_id)
        return _notice_page(request, work_id, tab="exemplaires", message=exc.message, status=400)
    return _go_with_flash(request, f"/catalogue/notices/{work_id}?onglet=exemplaires", "copy_deleted")


def _copy_work(request: Request, copy_id: int) -> int:
    with request.app.state.database.session() as session:
        work_id = session.scalar(
            select(Edition.work_id).join(Copy, Copy.edition_id == Edition.id).where(Copy.id == copy_id)
        )
    if work_id is None:
        raise HTTPException(status_code=404)
    return int(work_id)


# --- Aides à la saisie ------------------------------------------------------------------------------------------------
SUGGESTIONS = {
    "editeurs": (Publisher.name, Publisher.name_norm, None),
    "collections": (Collection.name, Collection.name_norm, None),
    "series": (Series.title, Series.title_norm, None),
    "categories": (Tag.label, Tag.label_norm, "category"),
    "genres": (Tag.label, Tag.label_norm, "genre"),
    "mots-cles": (Tag.label, Tag.label_norm, "keyword"),
}


@router.get("/catalogue/suggestions/{kind}", include_in_schema=False)
def suggestions(request: Request, kind: str, _auth: CanEdit) -> HTMLResponse:
    """Options d'une liste de saisie. La valeur tapée arrive sous le nom du champ, quel qu'il soit."""
    if kind not in SUGGESTIONS:
        raise HTTPException(status_code=404)
    typed = next(iter(request.query_params.values()), "")
    label, key, tag_kind = SUGGESTIONS[kind]
    pattern = normalize(typed.split(",")[-1])
    options: list[str] = []
    if pattern:
        statement = select(label).where(key.like(f"{pattern}%")).order_by(key).limit(MAX_SUGGESTIONS)
        if tag_kind:
            statement = statement.where(Tag.kind == tag_kind)
        with request.app.state.database.session() as session:
            options = list(session.scalars(statement))
    return _render(request, "catalogue/fragments/options.html", {"options": options})


@router.get("/catalogue/contributeurs/choix", include_in_schema=False)
def person_choice(request: Request, _auth: CanEdit, prefixe: Annotated[str, Query()]) -> HTMLResponse:
    if prefixe not in forms.PREFIXES:
        raise HTTPException(status_code=404)
    name = request.query_params.get(f"{prefixe}_nom", "")
    with request.app.state.database.session() as session:
        matches = forms.person_choices(session, name)
    return _render(request, "catalogue/fragments/person_choice.html", {"prefix": prefixe, "matches": matches})


@router.get("/catalogue/contributeurs/ligne", include_in_schema=False)
def person_row(request: Request, _auth: CanEdit, prefixe: Annotated[str, Query()]) -> HTMLResponse:
    if prefixe not in forms.PREFIXES:
        raise HTTPException(status_code=404)
    context = {"prefix": prefixe, "role_options": list(CONTRIBUTOR_ROLES.items())}
    return _render(request, "catalogue/fragments/person_row.html", context)


# --- Contributeurs : liste et fiche, après les routes « choix » et « ligne » ---------------------------------------
CONTRIBUTORS_PAGE = 40


@router.get("/catalogue/contributeurs", include_in_schema=False)
def contributors_page(request: Request, _auth: CanView) -> HTMLResponse:
    params = request.query_params
    query = params.get("q", "").strip()[:200]
    page = params.get("page", "")
    current = int(page) if page.isdigit() and len(page) < 9 else 1
    term = normalize(query)
    with request.app.state.database.session() as session:
        statement = select(Agent)
        if term:
            statement = statement.where(Agent.name_norm.contains(term, autoescape=True))
        total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        pages = max(1, -(-total // CONTRIBUTORS_PAGE))
        current = min(max(current, 1), pages)
        ids = statement.order_by(Agent.sort_name, Agent.id).limit(CONTRIBUTORS_PAGE)
        agents = list(session.scalars(ids.offset((current - 1) * CONTRIBUTORS_PAGE)))
        counts = repo.agent_work_counts(session, [agent.id for agent in agents])
        homonyms = repo.homonym_ids(session, agents)
    context = {
        "rows": [(agent, counts.get(agent.id, 0)) for agent in agents],
        "homonyms": homonyms,
        "total": total,
        "query": query,
        "page": current,
        "pages": pages,
        "window": listing.page_window(current, pages),
    }
    return _render(request, "catalogue/pages/contributors.html", context)


@router.get("/catalogue/contributeurs/{agent_id}", include_in_schema=False)
def contributor_page(request: Request, agent_id: int, _auth: CanView) -> HTMLResponse:
    with request.app.state.database.session() as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(status_code=404)
        work_ids = repo.works_of_agent(session, agent_id)
        cards = repo.load_cards(session, work_ids)
        homonyms = [other for other in repo.agents_named(session, agent.display_name) if other.id != agent_id]
    context = {"agent": agent, "cards": cards, "homonyms": homonyms}
    return _render(request, "catalogue/pages/contributor.html", context)


# --- Listes du catalogue : types, sections, catégories, genres, mots-clés, éditeurs, collections ----------------------
TAXONOMY_ACTIONS = ("creer", "renommer", "fusionner", "activer", "desactiver", "parent")


def _taxonomy_page(request: Request, kind: taxonomy.Kind, *, message: str = "", status: int = 200) -> HTMLResponse:
    params = request.query_params
    query = params.get("q", "").strip()[:200]
    page = params.get("page", "")
    current = int(page) if page.isdigit() and len(page) < 9 else 1
    with request.app.state.database.session() as session:
        rows = taxonomy.rows(session, kind)
    term = normalize(query)
    selected = [row for row in rows if not term or term in normalize(row.label)]
    pages = max(1, -(-len(selected) // taxonomy.PAGE_SIZE))
    current = min(max(current, 1), pages)
    start = (current - 1) * taxonomy.PAGE_SIZE
    context = {
        "kind": kind,
        "kinds": list(taxonomy.KINDS.values()),
        "rows": selected[start : start + taxonomy.PAGE_SIZE],
        "all_rows": rows,
        "total": len(selected),
        "query": query,
        "page": current,
        "pages": pages,
        "window": listing.page_window(current, pages),
        "message": message,
    }
    return _render(request, "catalogue/pages/taxonomy.html", context, status)


def _taxonomy_kind(code: str) -> taxonomy.Kind:
    kind = taxonomy.KINDS.get(code)
    if kind is None:
        raise HTTPException(status_code=404)
    return kind


@router.get("/admin/catalogue", include_in_schema=False)
def taxonomy_index(_auth: CanEdit) -> RedirectResponse:
    return _go("/admin/catalogue/categories")


@router.get("/admin/catalogue/{code}", include_in_schema=False)
def taxonomy_page(request: Request, code: str, _auth: CanEdit) -> HTMLResponse:
    return _taxonomy_page(request, _taxonomy_kind(code))


@router.post("/admin/catalogue/{code}", include_in_schema=False)
def taxonomy_action(request: Request, code: str, auth: CanEdit, form: Form) -> Response:
    kind = _taxonomy_kind(code)
    database = request.app.state.database
    action = forms.first(form, "action")
    item = forms.optional_int(forms.first(form, "element"))
    target = forms.optional_int(forms.first(form, "cible"))
    if action not in TAXONOMY_ACTIONS:
        raise HTTPException(status_code=404)
    try:
        if action == "creer":
            taxonomy.create(database, kind, forms.first(form, "libelle"), auth.user_id, target)
        elif item is None:
            raise BusinessError(_("Cet élément n’existe plus."))
        elif action == "renommer":
            taxonomy.rename(database, kind, item, forms.first(form, "libelle"), auth.user_id)
        elif action == "fusionner":
            if target is None:
                raise BusinessError(_("Choisissez l’élément dans lequel fusionner."))
            taxonomy.merge(database, kind, item, target, auth.user_id)
            return _go_with_flash(request, f"/admin/catalogue/{kind.code}", "taxonomy_merged")
        elif action == "parent":
            if kind.code != "sections":
                raise HTTPException(status_code=404)
            taxonomy.set_parent(database, item, target, auth.user_id)
        else:
            taxonomy.set_active(database, kind, item, action == "activer", auth.user_id)
    except BusinessError as exc:
        return _taxonomy_page(request, kind, message=exc.message, status=400)
    return _go_with_flash(request, f"/admin/catalogue/{kind.code}", "taxonomy_updated")
