# SPDX-License-Identifier: EUPL-1.2
"""Écran « Enrichir » d'une fiche : critères, sources, résultats, comparaison, différences, aperçu, confirmation.

Les critères voyagent en POST, puis la page se recharge sur la recherche enregistrée : un rechargement ou un retour
arrière ne relance aucun appel aux sources. Toute écriture passe par le service, en une transaction, et exige la
permission `enrichment.review`.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from biblyo.core import flash
from biblyo.core.errors import BusinessError, FormErrors, PermissionDenied
from biblyo.core.i18n import _
from biblyo.core.media import STORED_MIME, InvalidImage, preview_webp
from biblyo.core.permissions import require
from biblyo.core.security.ratelimit import FailureLimiter
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.catalogue import repository as catalogue_repo
from biblyo.modules.catalogue.rules import CONTRIBUTOR_ROLES
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.enrichment import cover_search, covers, creation, service
from biblyo.modules.enrichment import repository as repo
from biblyo.modules.enrichment import settings as settings_service
from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.transport import HttpTransport, RecordedTransport
from biblyo.modules.enrichment.diffing import NEVER_PRECHECKED, FieldDiff
from biblyo.modules.enrichment.matching import LEVELS
from biblyo.modules.enrichment.models import EditionIsbnCandidate
from biblyo.modules.enrichment.service import Acceptance, Criteria
from biblyo.modules.enrichment.sources import SourceHub, contact_of
from biblyo.modules.enrichment.tab import FIELD_LABELS

router = APIRouter()
CanSearch = Annotated[AuthContext, require("enrichment.search")]
CanReview = Annotated[AuthContext, require("enrichment.review")]
SEARCHES_PER_MINUTE = 30
CLASS_LABELS: dict[str, tuple[str, str]] = {
    "exact": (_("Correspondance exacte"), "ok"),
    "strong": (_("Correspondance forte"), "info"),
    "uncertain": (_("Correspondance incertaine"), "due"),
    "multiple": (_("Plusieurs candidats"), "due"),
    "none": (_("Aucun résultat"), "neutral"),
    "incomplete": (_("Recherche incomplète"), "late"),
    "excluded": (_("Écarté"), "neutral"),
}
STATE_LABELS: dict[str, str] = {
    "found": _("{n} résultat(s)"),
    "not_found": _("Aucun résultat"),
    "unavailable": _("Indisponible"),
    "timeout": _("Trop lente : délai dépassé"),
    "throttled": _("Trop de requêtes : la source demande une pause"),
    "refused": _("Requête refusée par la source"),
    "invalid_response": _("Réponse illisible"),
    "disabled": _("Désactivée"),
    "not_needed": _("Non interrogée : la source prioritaire a tranché"),
}
SOURCE_LABELS = {"bnf": _("BnF"), "sudoc": _("Sudoc"), "openlibrary": _("Open Library")}


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.render(request, template, context, status_code=status)
    return response


def _go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _go_with_flash(request: Request, path: str, code: str) -> Response:
    response = _go(path)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def hub_of(request: Request) -> SourceHub:
    """Un concentrateur par application, créé au premier besoin : régulateur et mise de côté partagés."""
    state = request.app.state
    hub = getattr(state, "enrichment_hub", None)
    if hub is None:
        settings = state.settings
        transport: HttpTransport | None = None
        if settings.enrichment_fixtures is not None and not settings.is_production:
            transport = RecordedTransport(settings.enrichment_fixtures)  # type: ignore[assignment]
        hub = SourceHub(state.database, transport=transport)
        state.enrichment_hub = hub
    return hub


def _limiter(request: Request) -> FailureLimiter:
    state = request.app.state
    limiter = getattr(state, "enrichment_limiter", None)
    if limiter is None:
        limiter = FailureLimiter(SEARCHES_PER_MINUTE, 60.0)
        state.enrichment_limiter = limiter
    return limiter


def _base(work_id: int) -> str:
    return f"/catalogue/notices/{work_id}/enrichir"


def _load(request: Request, work_id: int, edition_id: int | None) -> tuple[repo.Target, list[repo.ProvenanceView]]:
    with request.app.state.database.session() as session:
        notice = catalogue_repo.load_notice(session, work_id)
        if notice is None:
            raise HTTPException(status_code=404)
        edition = next((e for e in notice.editions if e.id == edition_id), notice.primary)
        target = repo.load_target(session, edition.id)
        if target is None:
            raise HTTPException(status_code=404)
        provenance = repo.provenance(session, target.values, notice.id, edition.id)
    return target, provenance


def _criteria_from(form: FormData, fallback: Criteria) -> Criteria:
    """Un champ absent du formulaire garde la valeur de la fiche ; un champ vidé reste vide."""

    def text(name: str, current: str, limit: int = 200) -> str:
        value = form.get(name)
        if value is None:
            return current
        return " ".join(value.split())[:limit] if isinstance(value, str) else ""

    return Criteria(
        isbn=text("isbn", fallback.isbn),
        title=text("titre", fallback.title),
        author=text("auteur", fallback.author),
        publisher=text("editeur", fallback.publisher),
        year=text("annee", fallback.year, 4),
        series=text("serie", fallback.series),
        volume=text("tome", fallback.volume, 20),
    )


def _reason(proposal: repo.ProposalView) -> str:
    """Pourquoi un candidat est écarté : la condition éliminatoire, sinon le score et ce qui le fait baisser."""
    explanation = proposal.explanation
    if explanation.get("excluded"):
        return str(explanation["excluded"])
    conflicting = [c["note"] for c in explanation.get("criteria", []) if c.get("points") == 0]
    detail = " ; ".join(conflicting[:3])
    return _("score {n} sur 100 : {detail}").format(
        n=proposal.score, detail=detail or _("trop peu de critères concordants")
    )


def _search_context(request: Request, target: repo.Target | None, search_id: int | None) -> dict[str, Any]:
    if search_id is None:
        return {"search": None, "groups": [], "compare": [], "excluded": []}
    with request.app.state.database.session() as session:
        views = repo.history(session, target.edition.id if target else None, search_id=search_id)
    if not views:
        raise HTTPException(status_code=404)
    view = views[0]
    order = {"exact": 0, "strong": 1, "uncertain": 2, "multiple": 3}
    proposals = sorted(view.proposals, key=lambda p: (order.get(p.match_class, 9), -p.score, p.id))
    excluded = [{"proposal": p, "reason": _reason(p)} for p in proposals if p.match_class in ("excluded", "none")]
    proposals = [p for p in proposals if p.match_class not in ("excluded", "none")]
    groups: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        group = seen.get(proposal.group_key)
        if group is None:
            group = {"key": proposal.group_key, "lead": proposal, "others": [], "class": proposal.match_class}
            seen[proposal.group_key] = group
            groups.append(group)
        else:
            group["others"].append(proposal)
    return {"search": view, "groups": groups, "compare": groups[:3], "excluded": excluded}


@router.get("/catalogue/notices/{work_id}/enrichir", include_in_schema=False)
def enrich_page(
    request: Request,
    work_id: int,
    auth: CanSearch,
    edition: Annotated[int | None, Query()] = None,
    recherche: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    target, provenance = _load(request, work_id, edition)
    configs = hub_of(request).metadata_configs()
    return _render(
        request,
        "enrichment/pages/enrich.html",
        {
            "target": target,
            "notice": target.notice,
            "edition": target.edition,
            "criteria": Criteria.from_fiche(target.fiche),
            "configs": configs,
            "levels": LEVELS,
            "class_labels": CLASS_LABELS,
            "state_labels": STATE_LABELS,
            "source_labels": SOURCE_LABELS,
            "provenance": provenance,
            "base": _base(work_id),
            "errors": {},
            **_search_context(request, target, recherche),
        },
    )


@router.post("/catalogue/notices/{work_id}/enrichir", include_in_schema=False)
def run_search(request: Request, work_id: int, auth: CanSearch, form: Form) -> Response:
    edition_raw = form.get("edition")
    edition_id = int(edition_raw) if isinstance(edition_raw, str) and edition_raw.isdigit() else None
    target, _provenance = _load(request, work_id, edition_id)
    limiter = _limiter(request)
    key = f"enrichment:{auth.user_id}"
    base = f"{_base(work_id)}?edition={target.edition.id}"
    if limiter.blocked(key):
        return _go_with_flash(request, base, "enrichment_limited")
    limiter.record(key)
    criteria = _criteria_from(form, Criteria.from_fiche(target.fiche))
    chosen = tuple(str(v) for v in form.getlist("sources") if isinstance(v, str))
    try:
        outcome = service.search(
            request.app.state.database,
            hub_of(request),
            edition_id=target.edition.id,
            criteria=criteria,
            sources=chosen or None,
            user_id=auth.user_id,
            refresh=form.get("actualiser") == "1",
        )
    except BusinessError:
        return _go_with_flash(request, base, "enrichment_failed")
    return _go(f"{base}&recherche={outcome.search_id}#resultats")


def _acceptance_from(form: FormData) -> Acceptance:
    fields = frozenset(str(v) for v in form.getlist("champ") if isinstance(v, str))
    people: list[tuple[str, str]] = []
    for raw in form.getlist("contributeur"):
        if not isinstance(raw, str) or not raw:
            continue
        role = form.get(f"role_{raw}")
        people.append((raw, role if isinstance(role, str) and role in CONTRIBUTOR_ROLES else "author"))
    return Acceptance(
        fields=fields,
        contributors=tuple(people),
        confirm_isbn=form.get("isbn_verifie") == "1",
        replace_cover=form.get("couverture_remplacer") == "1",
    )


def _proposal_context(
    request: Request, work_id: int, proposal_id: int, acceptance: Acceptance | None
) -> dict[str, Any]:
    try:
        target, candidate, assessment, diffs = service.preview(request.app.state.database, proposal_id=proposal_id)
    except BusinessError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    if target.notice.id != work_id:
        raise HTTPException(status_code=404)
    checked = acceptance.fields if acceptance else frozenset(d.field for d in diffs if d.prechecked)
    people = {name for name, _role in acceptance.contributors} if acceptance else set()
    roles = dict(acceptance.contributors) if acceptance else {}
    return {
        "target": target,
        "notice": target.notice,
        "edition": target.edition,
        "candidate": candidate,
        "assessment": assessment,
        "diffs": diffs,
        "work_diffs": [d for d in diffs if d.level == "work"],
        "edition_diffs": [d for d in diffs if d.level == "edition"],
        "aside_diffs": [d for d in diffs if d.level == "aside"],
        "checked": checked,
        "people": people,
        "roles": roles,
        "confirm_isbn": acceptance.confirm_isbn if acceptance else False,
        "role_options": list(CONTRIBUTOR_ROLES.items()),
        "class_labels": CLASS_LABELS,
        "source_labels": SOURCE_LABELS,
        "levels": LEVELS,
        "proposal_id": proposal_id,
        "base": _base(work_id),
        "never_prechecked": NEVER_PRECHECKED,
        "cover_offered": covers.offered(candidate),
        "replace_cover": acceptance.replace_cover if acceptance else False,
    }


@router.get("/catalogue/notices/{work_id}/enrichir/propositions/{proposal_id}", include_in_schema=False)
def proposal_page(request: Request, work_id: int, proposal_id: int, auth: CanSearch) -> Response:
    try:
        context = _proposal_context(request, work_id, proposal_id, None)
    except HTTPException as exc:
        if exc.status_code == 409:
            return _go_with_flash(request, _base(work_id), "enrichment_done")
        raise
    return _render(request, "enrichment/pages/proposal.html", context)


def _fillable(diffs: list[FieldDiff]) -> frozenset[str]:
    return frozenset(d.field for d in diffs if d.action == "fill" and d.field not in NEVER_PRECHECKED)


@router.post("/catalogue/notices/{work_id}/enrichir/propositions/{proposal_id}", include_in_schema=False)
def proposal_action(request: Request, work_id: int, proposal_id: int, auth: CanReview, form: Form) -> Response:
    action = form.get("action")
    acceptance = _acceptance_from(form)
    if action == "cocher_vides":
        context = _proposal_context(request, work_id, proposal_id, acceptance)
        checked = acceptance.fields | _fillable(context["diffs"])
        context["checked"] = checked
        return _render(request, "enrichment/pages/proposal.html", context)
    if action == "apercu":
        context = _proposal_context(request, work_id, proposal_id, acceptance)
        changes = [d for d in context["diffs"] if d.field in acceptance.fields and d.applicable]
        context["changes"] = changes
        context["acceptance"] = acceptance
        context["filled"] = sum(1 for d in changes if d.action == "fill")
        context["replaced"] = sum(1 for d in changes if d.action == "replace")
        context["added"] = sum(1 for d in changes if d.action == "add")
        return _render(request, "enrichment/pages/preview.html", context)
    if action == "appliquer":
        try:
            result = service.apply(
                request.app.state.database,
                proposal_id=proposal_id,
                acceptance=acceptance,
                user_id=auth.user_id,
                hub=hub_of(request),
                media_dir=request.app.state.settings.media_dir,
            )
        except ConflictError:
            return _go_with_flash(request, _base(work_id), "enrichment_stale")
        except BusinessError:
            return _go_with_flash(request, _base(work_id), "enrichment_done")
        if "cover" in acceptance.fields and result.cover_status not in ("", "found"):
            code = "enrichment_cover_failed" if result.applied else "enrichment_nothing"
            return _go_with_flash(request, f"/catalogue/notices/{work_id}?onglet=enrichissement", code)
        code = (
            "enrichment_applied"
            if result.applied and not result.rejected
            else "enrichment_partial"
            if result.applied
            else "enrichment_nothing"
        )
        return _go_with_flash(request, f"/catalogue/notices/{work_id}?onglet=enrichissement", code)
    raise HTTPException(status_code=400, detail=_("Action inconnue."))


COVER_PREVIEW_PATH = "/catalogue/notices/{work_id}/enrichir/propositions/{proposal_id}/couverture"


@router.get(COVER_PREVIEW_PATH, include_in_schema=False)
def proposal_cover(request: Request, work_id: int, proposal_id: int, auth: CanSearch) -> Response:
    """Vignette de la couverture proposée, contrôlée et réencodée par biblyO : le navigateur ne joint pas la source."""
    try:
        target, candidate, _assessment, _diffs = service.preview(request.app.state.database, proposal_id=proposal_id)
    except BusinessError:
        raise HTTPException(status_code=404) from None
    if target.notice.id != work_id or not covers.offered(candidate):
        raise HTTPException(status_code=404)
    key = covers.ark_for(candidate) or candidate.external_id
    cache = _preview_cache(request)
    data = cache.get(key)
    if data is None:
        result = covers.fetch(hub_of(request), candidate, original=False)
        if result.status != "found":
            raise HTTPException(status_code=404)
        try:
            data = preview_webp(result.data)
        except InvalidImage:
            raise HTTPException(status_code=404) from None
        cache.put(key, data)
    return Response(data, media_type=STORED_MIME, headers={"Cache-Control": "private, max-age=3600"})


def _preview_cache(request: Request) -> covers.PreviewCache:
    state = request.app.state
    cache = getattr(state, "enrichment_previews", None)
    if cache is None:
        cache = covers.PreviewCache()
        state.enrichment_previews = cache
    return cache


def source_label(code: str) -> str:
    return SOURCE_LABELS.get(code, code)


def class_of(value: str) -> tuple[str, str]:
    return CLASS_LABELS.get(value, (value, "neutral"))


def with_class(assessment: Any, match_class: str) -> Any:
    return replace(assessment, match_class=match_class)


# --- Annulation d'un enrichissement ----------------------------------------------------------------------------------
def _shown(value: Any) -> str:
    """Une valeur conservée, telle qu'on peut la lire : listes jointes, empreinte d'image nommée."""
    if value is None or value == "" or value == []:
        return _("vide")
    if isinstance(value, list):
        return ", ".join(f"{v[0]} ({v[1]})" if isinstance(v, list) and len(v) == 2 else str(v) for v in value)
    text = str(value)
    if len(text) == 64 and all(c in "0123456789abcdef" for c in text):
        return _("une image (couverture)")
    return text


def _owned_edition(request: Request, work_id: int, edition_id: int | None) -> int:
    if edition_id is None:
        raise HTTPException(status_code=404)
    with request.app.state.database.session() as session:
        notice = catalogue_repo.load_notice(session, work_id)
    if notice is None or all(e.id != edition_id for e in notice.editions):
        raise HTTPException(status_code=404)
    return edition_id


@router.post("/catalogue/notices/{work_id}/enrichir/operations/{operation_id}/annuler", include_in_schema=False)
def revert_operation(request: Request, work_id: int, operation_id: str, auth: CanReview, form: Form) -> Response:
    database = request.app.state.database
    edition_id = _owned_edition(request, work_id, service.operation_edition(database, operation_id))
    back = f"/catalogue/notices/{work_id}?onglet=enrichissement&edition={edition_id}"
    second_round = form.get("choix") == "1"
    forced = frozenset(str(v) for v in form.getlist("forcer") if isinstance(v, str))
    try:
        result = service.revert(database, operation_id=operation_id, user_id=auth.user_id, force=forced)
    except BusinessError:
        return _go_with_flash(request, back, "enrichment_done")
    if result.conflicts and not second_round:
        return _render(
            request,
            "enrichment/pages/revert.html",
            {
                "conflicts": result.conflicts,
                "restored_labels": [FIELD_LABELS.get(f, f) for f in result.restored],
                "labels": FIELD_LABELS,
                "shown": _shown,
                "back": back,
                "action": f"{_base(work_id)}/operations/{operation_id}/annuler",
            },
        )
    code = "enrichment_reverted" if not result.conflicts else "enrichment_reverted_partial"
    if result.nothing_to_do:
        code = "enrichment_done"
    return _go_with_flash(request, back, code)


# --- ISBN proposés ----------------------------------------------------------------------------------------------------
@router.post("/catalogue/notices/{work_id}/enrichir/isbn/{candidate_id}", include_in_schema=False)
def decide_isbn(request: Request, work_id: int, candidate_id: int, auth: CanReview, form: Form) -> Response:
    database = request.app.state.database
    with database.session() as session:
        row = session.get(EditionIsbnCandidate, candidate_id)
        edition_id = row.edition_id if row else None
    edition_id = _owned_edition(request, work_id, edition_id)
    back = f"/catalogue/notices/{work_id}?onglet=enrichissement&edition={edition_id}"
    decision = form.get("decision")
    if decision not in ("confirmer", "rejeter"):
        raise HTTPException(status_code=400)
    try:
        status = service.decide_isbn(
            database, candidate_id=candidate_id, confirm=decision == "confirmer", user_id=auth.user_id
        )
    except BusinessError:
        return _go_with_flash(request, back, "enrichment_done")
    return _go_with_flash(request, back, "isbn_confirmed" if status == "confirmed" else "isbn_rejected")


# --- Réglages des sources (E4) ----------------------------------------------------------------------------------------
CanConfigure = Annotated[AuthContext, require("admin.sources")]


def _contact(request: Request) -> str:
    with request.app.state.database.session() as session:
        return contact_of(session)


def _sources_page(
    request: Request,
    *,
    probe: settings_service.ProbeResult | None = None,
    errors: dict[str, str] | None = None,
    form: FormData | None = None,
    status: int = 200,
) -> HTMLResponse:
    hub = hub_of(request)
    return _render(
        request,
        "enrichment/pages/sources.html",
        {
            "configs": hub.configs(),
            "contact": _contact(request),
            "contact_required": registry.CONTACT_REQUIRED,
            "cached": settings_service.cache_counts(request.app.state.database),
            "labels": {
                "bnf": _("BnF, catalogue général"),
                "sudoc": _("Sudoc (Abes)"),
                "openlibrary": _("Open Library (Internet Archive)"),
            },
            "cover_sources": registry.COVER_SOURCES,
            "cover_only": {c.code for c in hub.cover_configs()} - {c.code for c in hub.metadata_configs()},
            "state_labels": STATE_LABELS,
            "floors": {
                "interval": settings_service.INTERVAL_FLOOR_MS,
                "batch": settings_service.BATCH_INTERVAL_FLOOR_MS,
            },
            "probe": probe,
            "errors": errors or {},
            "form": {k: v for k, v in form.items() if isinstance(v, str)} if form else {},
        },
        status,
    )


@router.get("/admin/sources", include_in_schema=False)
def sources_page(request: Request, auth: CanConfigure) -> HTMLResponse:
    return _sources_page(request)


@router.post("/admin/sources", include_in_schema=False)
def sources_save(request: Request, auth: CanConfigure, form: Form) -> Response:
    database = request.app.state.database
    hub = hub_of(request)
    tested = form.get("tester")
    if isinstance(tested, str) and tested:
        return _sources_page(request, probe=settings_service.probe(hub, tested))
    emptied = form.get("vider")
    if isinstance(emptied, str) and emptied:
        settings_service.clear_cache(database, emptied, auth.user_id)
        return _go_with_flash(request, "/admin/sources", "sources_cache_cleared")
    codes = [str(v) for v in form.getlist("source") if isinstance(v, str)]

    def text(name: str) -> str:
        value = form.get(name)
        return value if isinstance(value, str) else ""

    rows = [
        settings_service.SourceInput(
            code=code,
            enabled=text(f"{code}_active") == "1",
            interval_ms=text(f"{code}_intervalle"),
            batch_interval_ms=text(f"{code}_lot"),
            timeout_ms=text(f"{code}_delai"),
            covers_enabled=text(f"{code}_couvertures") == "1",
        )
        for code in codes
    ]
    order = sorted(codes, key=lambda c: (int(text(f"{c}_ordre")) if text(f"{c}_ordre").isdigit() else 99, c))
    try:
        settings_service.save(database, rows, order, auth.user_id, contact=text("contact"))
    except FormErrors as exc:
        return _sources_page(request, errors=exc.fields, form=form, status=400)
    return _go_with_flash(request, "/admin/sources", "sources_saved")


# --- Recherche libre et création d'une notice (E3, PA-2) -------------------------------------------------------------
@router.get("/sources", include_in_schema=False)
def free_search_page(
    request: Request, auth: CanSearch, recherche: Annotated[int | None, Query()] = None
) -> HTMLResponse:
    context = _search_context(request, None, recherche)
    criteria = Criteria()
    if context["search"] is not None:
        saved = context["search"].criteria
        criteria = Criteria(**{k: str(v) for k, v in saved.items() if k in Criteria.__dataclass_fields__})
    return _render(
        request,
        "enrichment/pages/sources_search.html",
        {
            "criteria": criteria,
            "configs": hub_of(request).metadata_configs(),
            "levels": LEVELS,
            "class_labels": CLASS_LABELS,
            "state_labels": STATE_LABELS,
            "source_labels": SOURCE_LABELS,
            "base": "/sources",
            **context,
        },
    )


@router.post("/sources", include_in_schema=False)
def free_search_run(request: Request, auth: CanSearch, form: Form) -> Response:
    limiter = _limiter(request)
    key = f"enrichment:{auth.user_id}"
    if limiter.blocked(key):
        return _go_with_flash(request, "/sources", "enrichment_limited")
    limiter.record(key)
    criteria = _criteria_from(form, Criteria())
    if not any((criteria.isbn, criteria.title, criteria.author, criteria.series)):
        return _go_with_flash(request, "/sources", "enrichment_empty")
    chosen = tuple(str(v) for v in form.getlist("sources") if isinstance(v, str))
    outcome = service.search_free(
        request.app.state.database,
        hub_of(request),
        criteria=criteria,
        sources=chosen or None,
        user_id=auth.user_id,
        refresh=form.get("actualiser") == "1",
    )
    return _go(f"/sources?recherche={outcome.search_id}#resultats")


@router.get("/sources/propositions/{proposal_id}/creer", include_in_schema=False)
def create_from_result(request: Request, proposal_id: int, auth: CanSearch) -> HTMLResponse:
    if not auth.can("catalogue.edit"):
        raise PermissionDenied()
    planned = creation.plan(request.app.state.database, proposal_id)
    if planned is None:
        raise HTTPException(status_code=404)
    return _render(
        request,
        "enrichment/pages/create.html",
        {"plan": planned, "class_labels": CLASS_LABELS, "role_labels": dict(CONTRIBUTOR_ROLES)},
    )


# --- Recherche de couvertures (conception 17 § 3 et 4) --------------------------------------------------------------
COVER_IMAGE_PATH = "/catalogue/couvertures/image"
COVER_SIZES = ("vignette", "grande")


def cover_search_ready(auth: AuthContext | None) -> bool:
    """La page Couverture du catalogue montre la recherche à qui peut interroger les sources."""
    return auth is not None and auth.can("enrichment.search")


def _cover_previews(request: Request) -> cover_search.Previews:
    return cover_search.Previews(_preview_cache(request))


def _can_edit(auth: AuthContext) -> None:
    if not auth.can("catalogue.edit"):
        raise PermissionDenied("catalogue.edit")


@router.get("/catalogue/notices/{work_id}/couverture/propositions", include_in_schema=False)
def cover_proposals(
    request: Request, work_id: int, auth: CanSearch, edition: Annotated[int | None, Query()] = None
) -> Response:
    _can_edit(auth)
    target, _provenance = _load(request, work_id, edition)
    page = f"/catalogue/notices/{work_id}/couverture?edition={target.edition.id}"
    if request.headers.get("HX-Request") != "true":
        return _go(page + "&rechercher=1")
    limiter = _limiter(request)
    key = f"enrichment:{auth.user_id}"
    if limiter.blocked(key):
        return _render(
            request, "enrichment/fragments/cover_proposals.html", {"limited": True, "outcome": None, "page": page}
        )
    limiter.record(key)
    outcome = cover_search.search(hub_of(request), _cover_previews(request), target.fiche)
    return _render(
        request,
        "enrichment/fragments/cover_proposals.html",
        {
            "limited": False,
            "outcome": outcome,
            "page": page,
            "work_id": work_id,
            "edition_id": target.edition.id,
            "match_labels": cover_search.MATCH_LABELS,
            "needs_confirmation": cover_search.NEEDS_CONFIRMATION,
            "source_labels": SOURCE_LABELS,
            "image_path": COVER_IMAGE_PATH,
        },
    )


@router.get(COVER_IMAGE_PATH, include_in_schema=False)
def cover_image(
    request: Request,
    auth: CanSearch,
    source: Annotated[str, Query()] = "",
    ref: Annotated[str, Query()] = "",
    ean: Annotated[str, Query()] = "",
    taille: Annotated[str, Query()] = "vignette",
) -> Response:
    """Image d'une proposition, contrôlée et réencodée par biblyO : le navigateur ne joint jamais la source."""
    if taille not in COVER_SIZES or not cover_search.valid_reference(source, ref):
        raise HTTPException(status_code=404)
    data = cover_search.preview(hub_of(request), _cover_previews(request), source, ref, ean, taille)
    if data is None:
        raise HTTPException(status_code=404)
    return Response(data, media_type=STORED_MIME, headers={"Cache-Control": "private, max-age=3600"})


@router.post("/catalogue/notices/{work_id}/couverture/proposition", include_in_schema=False)
def cover_choose(request: Request, work_id: int, auth: CanSearch, form: Form) -> Response:
    """La proposition choisie devient un aperçu du catalogue ; rien n'est rattaché avant la confirmation."""
    from biblyo.core.clock import now_iso
    from biblyo.modules.catalogue import covers as catalogue_covers

    _can_edit(auth)
    edition_raw = form.get("edition_id")
    edition_id = int(edition_raw) if isinstance(edition_raw, str) and edition_raw.isdigit() else None
    target, _provenance = _load(request, work_id, edition_id)
    source = str(form.get("source") or "")
    reference = str(form.get("ref") or "")
    ean = str(form.get("ean") or "")
    match = str(form.get("match") or "")
    page = f"/catalogue/notices/{work_id}/couverture?edition={target.edition.id}"
    if not cover_search.valid_reference(source, reference) or match not in cover_search.MATCH_LABELS:
        raise HTTPException(status_code=404)
    previews = _cover_previews(request)
    original = previews.get(source, reference, "originale")
    if original is None:
        result = cover_search.fetch_original(hub_of(request), source, reference, ean)
        if result.status != "found":
            return _go_with_flash(request, page, "enrichment_cover_failed")
        original = result.data
    try:
        staged = catalogue_covers.stage(
            request.app.state.settings.media_dir,
            original,
            in_use=catalogue_covers.in_use_checker(request.app.state.database),
            user_id=auth.user_id,
            work_id=work_id,
            edition_id=target.edition.id,
            kind="source",
            source_code=source,
            reference=reference,
            origin_url=str(form.get("notice") or "")[:500] or cover_search.public_url(source, reference),
            license_note=cover_search.licence_note(source, now_iso()),
            match=match,
            edition_text=str(form.get("edition_text") or "")[:300],
            needs_rights=source == "openlibrary",
        )
    except InvalidImage:
        return _go_with_flash(request, page, "enrichment_cover_failed")
    return _go(f"/catalogue/notices/{work_id}/couverture/apercu/{staged.token}")
