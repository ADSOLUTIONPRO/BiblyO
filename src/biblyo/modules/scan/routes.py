# SPDX-License-Identifier: EUPL-1.2
"""Scanner universel (lot L8) : identifier un code partout dans biblyO, choisir un mode de travail, configurer la
douchette. **Identifier n'écrit rien.** Les modes qui écrivent (comptoir, retours, rangement) sont les écrans
existants, avec leurs règles ; l'inventaire n'écrit que sur une confirmation explicite, par l'opération existante."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from starlette.datastructures import FormData

from biblyo.core.i18n import _
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.catalogue import listing
from biblyo.modules.holdings.models import Location
from biblyo.modules.labels.layout import test_symbols
from biblyo.modules.labels.render import symbol_svg
from biblyo.modules.scan import actions, diagnosis, resolver

router = APIRouter()

CanScan = Annotated[AuthContext, require("catalogue.view")]
MAX_SEEN = 500
RESULT = "scan/fragments/result.html"
INVENTORY_LIST = "scan/fragments/inventory_list.html"
TEST_RESULT = "scan/fragments/test_result.html"


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.render(request, template, context, status_code=status)
    return response


def _text(form: Any, name: str, limit: int = 64) -> str:
    value = form.get(name)
    return value.strip()[:limit] if isinstance(value, str) else ""


def _int(text: str) -> int | None:
    return int(text) if text.isdecimal() and len(text) < 10 else None


def _mode(value: str) -> str:
    return value if value in actions.MODES else "recherche"


def _result_context(found: resolver.Identification | None, auth: AuthContext, mode: str) -> dict[str, Any]:
    copy_actions = actions.copy_actions(found.copy, auth, mode) if found and found.copy else []
    return {
        "found": found,
        "mode": mode,
        "actions_list": copy_actions,
        "can_search_sources": auth.can("enrichment.search"),
        "can_create": auth.can("catalogue.edit"),
        "can_lend": auth.can("circulation.lend"),
    }


@router.get("/scanner", include_in_schema=False)
def scanner_page(request: Request, auth: CanScan) -> HTMLResponse:
    mode = _mode(request.query_params.get("mode", ""))
    return _render(
        request,
        "scan/pages/scanner.html",
        {**_result_context(None, auth, mode), "modes": actions.modes_for(auth), "code": ""},
    )


@router.post("/scanner/identifier", include_in_schema=False)
def identify(request: Request, auth: CanScan, form: Form) -> HTMLResponse:
    """Ce que désigne le code lu, ou l'exemplaire choisi dans une liste. Lecture seule, aucun appel extérieur."""
    mode = _mode(_text(form, "mode", 20))
    chosen = _int(_text(form, "exemplaire", 10))
    code = _text(form, "code")
    with request.app.state.database.session() as session:
        if chosen is not None:
            found: resolver.Identification | None = resolver.by_copy(session, chosen, auth)
        else:
            found = resolver.identify(session, code, auth) if code else None
    context = _result_context(found, auth, mode)
    if request.headers.get("HX-Request"):
        return _render(request, RESULT, context)
    return _render(request, "scan/pages/scanner.html", {**context, "modes": actions.modes_for(auth), "code": code})


# --- Inventaire : vérifier la présence, scan après scan, sans rien écrire -------------------------------------------
def _seen(form: Any) -> list[int]:
    values = form.getlist("vu") if hasattr(form, "getlist") else []
    ids = [_int(item) for item in values if isinstance(item, str)]
    return list(dict.fromkeys(item for item in ids if item is not None))[:MAX_SEEN]


def _under(session: Any, location_id: int | None) -> set[int]:
    """Le lieu contrôlé et tout ce qu'il contient : une salle compte ses meubles et leurs étagères."""
    if location_id is None:
        return set()
    places = listing._places_under(str(location_id), "lieux_inventaire")
    return set(session.scalars(select(places.c.id)))


def _inventory(request: Request, auth: AuthContext, form: Any) -> dict[str, Any]:
    seen = _seen(form)
    place = _int(_text(form, "lieu", 10))
    code = _text(form, "code")
    added = _int(_text(form, "ajouter", 10))
    notice, kind, found = "", "info", None
    with request.app.state.database.session() as session:
        if added is not None:
            found = resolver.by_copy(session, added, auth)
        elif code:
            found = resolver.identify(session, code, auth)
        if found is not None and found.copy is not None and found.kind == resolver.KIND_COPY:
            if found.copy.id in seen:
                notice, kind = _("Déjà compté : « {t} ».").format(t=found.copy.title), "due"
            elif len(seen) >= MAX_SEEN:
                notice, kind = _("Au plus 500 livres par liste : confirmez celle-ci, puis recommencez."), "late"
            else:
                seen.append(found.copy.id)
                notice, kind = _("Compté : « {t} ».").format(t=found.copy.title), "ok"
            found = None
        elif found is not None and found.kind == resolver.KIND_READER:
            notice, kind, found = _("C’est une carte de lecteur : rien n’est compté."), "due", None
        elif found is not None and found.kind == resolver.KIND_UNKNOWN:
            notice, kind = _("Code inconnu : « {c} ».").format(c=found.text), "late"
        views = resolver.copy_views(session, list(reversed(seen)))
        inside = _under(session, place)
        places = [item for item in listing._place_options(session, str(place or "")) if item[0] != listing.UNSHELVED]
        location = session.get(Location, place) if place is not None else None
        place_name = location.path if location is not None else ""
    lines = []
    for view in views:
        if view.loan_id is not None:
            remark, tone = _("Noté en prêt : son retour n’a pas été enregistré ?"), "late"
        elif view.status == "missing":
            remark, tone = _("Noté introuvable : déclarez-le retrouvé depuis sa fiche."), "due"
        elif view.status in ("withdrawn", "lost"):
            remark, tone = _("Noté « {s} » : à vérifier.").format(s=view.status_label), "late"
        elif place is not None and view.location_id not in inside:
            remark = _("Rangé ailleurs : {p}").format(p=view.path) if view.path else _("Noté non rangé")
            tone = "due"
        elif view.status == "in_processing" and view.location_id is not None:
            remark, tone = _("Présence à confirmer"), "info"
        elif view.location_id is None:
            remark, tone = _("Noté non rangé"), "due"
        else:
            remark, tone = _("En place"), "ok"
        lines.append((view, remark, tone))
    confirmable = [
        view for view in views if view.status == "in_processing" and view.location_id is not None and not view.loan_id
    ]
    return {
        "seen": seen,
        "lines": lines,
        "confirmable": confirmable,
        "place": place,
        "place_name": place_name,
        "places": places,
        "notice": notice,
        "notice_kind": kind,
        "found": found,
        "can_confirm": auth.can("holdings.edit"),
    }


@router.get("/scanner/inventaire", include_in_schema=False)
def inventory_page(request: Request, auth: CanScan) -> HTMLResponse:
    context = _inventory(request, auth, request.query_params)
    return _render(request, "scan/pages/inventory.html", {**context, "modes": actions.modes_for(auth)})


@router.post("/scanner/inventaire", include_in_schema=False)
def inventory_scan(request: Request, auth: CanScan, form: Form) -> HTMLResponse:
    """Un scan de plus dans la liste. Aucun prêt, aucun statut changé : la liste voyage dans le formulaire."""
    context = _inventory(request, auth, form)
    if request.headers.get("HX-Request"):
        return _render(request, INVENTORY_LIST, context)
    return _render(request, "scan/pages/inventory.html", {**context, "modes": actions.modes_for(auth)})


# --- Configurer ma douchette ---------------------------------------------------------------------------------------
def _setup_context(auth: AuthContext, progress: diagnosis.Progress) -> dict[str, Any]:
    code, isbn, unknown = test_symbols()
    return {
        "test_svgs": [
            (symbol_svg(code, label=_("Code d’essai Code 128 : {c}").format(c=code.text)),
             _("Code 128 : celui des étiquettes de biblyO")),
            (symbol_svg(isbn, label=_("Code d’essai EAN-13 : {c}").format(c=isbn.text)),
             _("EAN-13 : celui des ISBN au dos des livres")),
            (symbol_svg(unknown, label=_("Code inconnu d’essai : {c}").format(c=unknown.text)),
             _("Code inconnu : aucun livre ne le porte")),
        ],
        "modes": actions.modes_for(auth),
        "can_print": auth.can("labels.print"),
        "checks": diagnosis.CHECKS,
        "progress": progress,
        "result": None,
    }  # fmt: skip


def _progress(form: Any) -> diagnosis.Progress:
    done = form.getlist("fait") if hasattr(form, "getlist") else []
    series = _int(_text(form, "serie", 3)) or 0
    stamp = _text(form, "serie_t", 12)
    last = int(stamp) if stamp.isdecimal() else 0  # un instant en secondes : dix chiffres
    return diagnosis.Progress(frozenset(item for item in done if item in diagnosis.CHECKS), series, last)


@router.get("/scanner/douchette", include_in_schema=False)
def setup_page(request: Request, auth: CanScan) -> HTMLResponse:
    return _render(request, "scan/pages/setup.html", _setup_context(auth, diagnosis.Progress(frozenset(), 0, 0)))


@router.post("/scanner/douchette/essai", include_in_schema=False)
def setup_try(request: Request, auth: CanScan, form: Form) -> HTMLResponse:
    """Le diagnostic d'un essai et la liste de mise en service mise à jour. N'écrit rien en base."""
    raw = form.get("essai")
    raw = raw[:128] if isinstance(raw, str) else ""
    suffix = _text(form, "suffixe", 8)
    suffix = suffix if suffix in ("enter", "tab", "none") else ""
    interval = _int(_text(form, "intervalle", 6)) or 0
    with request.app.state.database.session() as session:
        found = diagnosis.diagnose(session, raw, suffix, interval, auth)
    progress = diagnosis.progress(_progress(form), found, suffix, int(time.time()))
    context = {
        **_setup_context(auth, progress),
        **_result_context(found.identification, auth, "recherche"),
        "result": found,
    }
    if request.headers.get("HX-Request"):
        return _render(request, TEST_RESULT, context)
    return _render(request, "scan/pages/setup.html", context)
