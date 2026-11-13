# SPDX-License-Identifier: EUPL-1.2
"""Pages des lecteurs et des foyers. Toute écriture passe par `service` ; chaque route vérifie sa permission.

**Aucune coordonnée ne quitte le serveur sans `patrons.view_personal`** : ni dans une page, ni dans un attribut, ni
dans un fragment. Les listes et le sélecteur n'en transportent jamais : les coordonnées ne s'affichent que sur la
fiche, à la demande.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from starlette.datastructures import FormData

from biblyo.core import flash
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _
from biblyo.core.pagination import page_numbers
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext, get_auth
from biblyo.modules.patrons import repository as repo
from biblyo.modules.patrons import rules, service
from biblyo.modules.patrons.models import Patron
from biblyo.modules.patrons.service import (
    AddressInput,
    ConflictError,
    ContactsEdit,
    FamilyAdult,
    FamilyRegistration,
    GuardianInput,
    HouseholdChoice,
    IdentityEdit,
    PersonInput,
    Registration,
    RegistrationRefused,
    Review,
)


def visibility_of(auth: AuthContext) -> repo.Visibility:
    """Mêmes règles partout : palette, annuaire, sélecteurs, fiches, foyers, fragments et exports."""
    return repo.Visibility(archived=auth.can("patrons.archive"), anonymized=auth.can("patrons.anonymize"))


def _withdrawn_patrons_stay_closed(request: Request) -> None:
    """Toute route qui désigne une fiche, y compris les fragments et l'export : sans la permission, c'est non.

    Sur le routeur plutôt que route par route : une adresse ajoutée demain est couverte d'office."""
    identifiers = [request.path_params[key] for key in ("patron_id", "guardian_id") if key in request.path_params]
    auth = get_auth(request)
    if not identifiers or auth is None:
        return
    visibility = visibility_of(auth)
    with request.app.state.database.session() as session:
        statuses = session.scalars(select(Patron.status).where(Patron.id.in_(identifiers))).all()
    if any(not visibility.allows(status) for status in statuses):
        raise HTTPException(status_code=403)


router = APIRouter(dependencies=[Depends(_withdrawn_patrons_stay_closed)])

CanView = Annotated[AuthContext, require("patrons.view")]
CanRegister = Annotated[AuthContext, require("patrons.register")]
CanEdit = Annotated[AuthContext, require("patrons.edit")]
CanSeePersonal = Annotated[AuthContext, require("patrons.view_personal")]
CanArchive = Annotated[AuthContext, require("patrons.archive")]
CanExport = Annotated[AuthContext, require("patrons.export")]
CanAnonymize = Annotated[AuthContext, require("patrons.anonymize")]

#: Vues de l'annuaire, dans l'ordre des puces.
VIEWS: dict[str, str] = {
    "tous": _("Tous"),
    "adultes": _("Adultes"),
    "mineurs": _("Mineurs"),
    "foyers": _("Foyers et structures"),
    "responsable": _("Responsable à renseigner"),
    "adhesion": _("Adhésion échue"),
    "archives": _("Archivés"),
}
PICKER_RESULTS = 12


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _first(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _typed(form: FormData) -> str:
    """Le texte d'un sélecteur. Chaque sélecteur n'envoie que son propre champ (`hx-params`), quel qu'en soit le nom."""
    return next((value.strip()[:200] for value in form.values() if isinstance(value, str)), "")


def _version(form: FormData) -> int | None:
    text = _first(form, "version")
    return int(text) if text.isdigit() and len(text) < 10 else None


def _go(request: Request, path: str, code: str) -> Response:
    response = RedirectResponse(path, status_code=303)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.render(request, template, context, status_code=status)
    return response


def directory_url(view: str = "tous", page: int = 1, size: int = rules.DEFAULT_PAGE_SIZE) -> str:
    """L'adresse d'une vue : le filtre et la page. **Jamais le texte cherché**, qui voyage dans le corps."""
    params: dict[str, str] = {}
    if view != "tous":
        params["vue"] = view
    if page > 1:
        params["page"] = str(page)
    if size != rules.DEFAULT_PAGE_SIZE:
        params["par_page"] = str(size)
    return "/lecteurs" + (f"?{urlencode(params)}" if params else "")


# --- Annuaire ---------------------------------------------------------------------------------------------------------
PAGE_SIZES = (25, 50, 100)
SEARCH_LENGTH = 200


def _directory(request: Request, auth: AuthContext, view: str, query: str, page: int, size: int) -> HTMLResponse:
    visibility = visibility_of(auth)
    if view not in VIEWS:
        view = "tous"
    if view == "archives" and not visibility.archived:
        raise HTTPException(status_code=403)
    personal = auth.can("patrons.view_personal")
    result: repo.Directory | None = None
    homes: repo.HouseholdPage | None = None
    with request.app.state.database.session() as session:
        totals = repo.totals(session)
        if view == "foyers":
            homes = repo.household_page(session, query=query, page=page, size=size, visibility=visibility)
        else:
            result = repo.directory(
                session, view, page, service.today(), query=query, personal=personal, size=size, visibility=visibility
            )
    current = homes.page if homes is not None else result.page if result is not None else 1
    pages = homes.pages if homes is not None else result.pages if result is not None else 1
    context = {
        "view": view,
        "query": query,
        "result": result,
        "homes": homes,
        "totals": totals,
        "personal": personal,
        "chips": [
            {"code": code, "label": label, "current": code == view}
            for code, label in VIEWS.items()
            if code != "archives" or visibility.archived
        ],
        "page": current,
        "pages": pages,
        "window": page_numbers(current, pages),
        "size": size,
        "sizes": PAGE_SIZES if size in PAGE_SIZES else tuple(sorted({*PAGE_SIZES, size})),
    }
    response = _render(request, "patrons/pages/directory.html", context)
    if request.headers.get("HX-Request") == "true":
        # Une recherche remplace l'adresse sans empiler l'historique ; l'adresse ne contient jamais le texte cherché.
        header = "HX-Replace-Url" if query else "HX-Push-Url"
        response.headers[header] = directory_url(view, 1 if query else current, size)
    return response


@router.get("/lecteurs", include_in_schema=False)
def directory_page(request: Request, auth: CanView) -> HTMLResponse:
    """Consulter une vue. Un paramètre `q` dans l'adresse est ignoré : la recherche se fait en POST."""
    params = request.query_params
    size, page = rules.page_size(params.get("par_page")), rules.page_number(params.get("page"))
    return _directory(request, auth, params.get("vue", "tous"), "", page, size)


@router.post("/lecteurs/recherche", include_in_schema=False)
def directory_search(request: Request, auth: CanView, form: Form) -> HTMLResponse:
    """Chercher, filtrer, changer de page : le texte tapé peut être un nom ou une coordonnée, il reste dans le corps."""
    size, page = rules.page_size(_first(form, "par_page")), rules.page_number(_first(form, "page"))
    return _directory(request, auth, _first(form, "vue"), _first(form, "q").strip()[:SEARCH_LENGTH], page, size)


# --- Sélecteur de lecteur (préparation de L4) -------------------------------------------------------------------------
@router.post("/lecteurs/choix", include_in_schema=False)
def picker_options(
    request: Request, auth: CanView, form: Form, adultes: Annotated[str, Query(max_length=1)] = ""
) -> HTMLResponse:
    """Résultats du sélecteur : de quoi distinguer deux homonymes (D8), jamais une coordonnée.

    `adultes=1` ne propose que des adultes en service, pour choisir un responsable ou un référent."""
    typed = _typed(form)
    rows: list[repo.Row] = []
    if typed:
        with request.app.state.database.session() as session:
            rows = repo.search(
                session,
                typed,
                personal=auth.can("patrons.view_personal"),
                limit=PICKER_RESULTS,
                adults_only=adultes == "1",
                visibility=visibility_of(auth),
            )
    return _render(request, "patrons/fragments/picker_options.html", {"rows": rows, "query": typed})


# --- Palette ⌘K -------------------------------------------------------------------------------------------------------
PALETTE_QUERY_LENGTH = 100
PALETTE_LIMIT_KEY = "palette:{user}"


@router.post("/ui/recherche/lecteurs", include_in_schema=False)
def palette_readers(request: Request, auth: CanView, form: Form) -> HTMLResponse:
    """Lecteurs proposés par la palette. **POST** : le texte tapé, souvent un nom, ne passe jamais dans une adresse,
    donc ni dans un journal d'accès, ni chez un mandataire, ni dans un historique."""
    app = request.app
    query = _first(form, "q").strip()[:PALETTE_QUERY_LENGTH]
    if not query:
        return _render(request, "patrons/fragments/palette_results.html", {"result": repo.PaletteResult([])})
    key = PALETTE_LIMIT_KEY.format(user=auth.user_id)
    if app.state.search_limiter.blocked(key):
        if not app.state.search_alerts.blocked(key):
            app.state.search_alerts.record(key)
            service.record_search_limited(app.state.database, auth.user_id)
        return _render(request, "patrons/fragments/palette_results.html", {"result": None})
    app.state.search_limiter.record(key)
    with app.state.database.session() as session:
        result = repo.palette_search(session, query, visibility=visibility_of(auth))
    return _render(request, "patrons/fragments/palette_results.html", {"result": result})


@router.post("/foyers/choix", include_in_schema=False)
def household_options(request: Request, auth: CanView, form: Form) -> HTMLResponse:
    """Foyers proposés à l'inscription : le nom et les membres visibles par ce compte, jamais une coordonnée."""
    typed = _typed(form)
    cards: list[repo.HouseholdCard] = []
    if typed:
        with request.app.state.database.session() as session:
            found = repo.household_page(session, query=typed, visibility=visibility_of(auth))
        cards = found.cards[:PICKER_RESULTS]
    return _render(request, "patrons/fragments/household_options.html", {"cards": cards, "query": typed})


# --- Inscription : saisir, vérifier et corriger, enregistrer ----------------------------------------------------------
#: Tous les champs du formulaire. À l'étape de vérification, ils reviennent du navigateur : **rien n'est relu en base**.
REGISTRATION_FIELDS = (
    "categorie",
    "prenom",
    "nom",
    "naissance",
    "courriel",
    "telephone",
    "telephone_territoire",
    "adresse_ligne1",
    "adresse_ligne2",
    "code_postal",
    "ville",
    "observations",
    "adhesion",
    "carte",
    "foyer_mode",
    "foyer_id",
    "foyer_label",
    "foyer_nom",
    "foyer_type",
    "resp_id",
    "resp_label",
    "resp_prenom",
    "resp_nom",
    "resp_courriel",
    "resp_telephone",
    "resp_telephone_territoire",
    "resp_relation",
    "resp_emprunte",
    "resp_contact",
    "resp_seul",
)
MINOR_ONLY = tuple(name for name in REGISTRATION_FIELDS if name.startswith("resp_"))
ADULT_ONLY = (
    "courriel",
    "telephone",
    "telephone_territoire",
    "adresse_ligne1",
    "adresse_ligne2",
    "code_postal",
    "ville",
)


def _registration_values(form: FormData | None, auth: AuthContext) -> dict[str, str]:
    """Les valeurs retenues : ce que le rôle ne peut pas saisir, ou qui ne vaut pas pour cette catégorie, tombe."""
    values = {name: _first(form, name) if form is not None else "" for name in REGISTRATION_FIELDS}
    values["categorie"] = values["categorie"] if values["categorie"] in rules.CATEGORIES else "adult"
    values["foyer_mode"] = values["foyer_mode"] if values["foyer_mode"] in ("aucun", "existant", "nouveau") else "aucun"
    values["foyer_type"] = values["foyer_type"] if values["foyer_type"] in rules.HOUSEHOLD_KINDS else "family"
    values["resp_relation"] = values["resp_relation"] or "parent"
    for name, permission in (("observations", "patrons.view_personal"), ("adhesion", "patrons.edit")):
        if not auth.can(permission):
            values[name] = ""
    for name in ADULT_ONLY if values["categorie"] == "minor" else MINOR_ONLY:
        values[name] = "" if name != "resp_relation" else "parent"
    return values


def _guardian_number(request: Request, values: dict[str, str]) -> str:
    identifier = values["resp_id"]
    if not (identifier.isdigit() and len(identifier) < 12):
        return ""
    with request.app.state.database.session() as session:
        return session.scalar(select(Patron.number).where(Patron.id == int(identifier))) or ""


def _registration(request: Request, values: dict[str, str], *, confirmed: bool) -> Registration:
    identifier = values["foyer_id"]
    household = HouseholdChoice()
    if values["foyer_mode"] == "existant" and identifier.isdigit() and len(identifier) < 12:
        household = HouseholdChoice(household_id=int(identifier))
    elif values["foyer_mode"] == "nouveau":
        household = HouseholdChoice(new_name=values["foyer_nom"], kind=values["foyer_type"])
    return Registration(
        person=PersonInput(
            first_name=values["prenom"],
            last_name=values["nom"],
            category=values["categorie"],
            birth=values["naissance"],
            email=values["courriel"],
            phone=values["telephone"],
            phone_territory=values["telephone_territoire"],
            address=AddressInput(
                values["adresse_ligne1"], values["adresse_ligne2"], values["code_postal"], values["ville"]
            ),
            notes=values["observations"],
            membership_expires_on=values["adhesion"],
            card_number=values["carte"],
        ),
        household=household,
        guardian=GuardianInput(
            number=_guardian_number(request, values),
            first_name=values["resp_prenom"],
            last_name=values["resp_nom"],
            email=values["resp_courriel"],
            phone=values["resp_telephone"],
            phone_territory=values["resp_telephone_territoire"],
            relation=values["resp_relation"],
            can_borrow_for=values["resp_emprunte"] == "1",
            is_contact=values["resp_contact"] == "1",
            can_borrow_alone=values["resp_seul"] == "1",
        ),
        homonyms_confirmed=confirmed,
    )


def _registration_page(
    request: Request,
    values: dict[str, str],
    *,
    step: str = "saisir",
    review: Review | None = None,
    status: int = 200,
) -> HTMLResponse:
    review = review or Review()
    context = {
        "step": step,
        "values": values,
        "errors": review.errors,
        "homonyms": review.homonyms,
        "hidden_homonyms": review.hidden_homonyms,
        "phone_choices": review.phone_choices,
        "phones": review.phones,
        "guardian_name": review.guardian_name or values["resp_label"],
        "relation_options": list(rules.RELATIONS.items()),
        "kind_options": list(rules.HOUSEHOLD_KINDS.items()),
        "category_label": rules.CATEGORIES[values["categorie"]],
        "relation_label": rules.RELATIONS.get(values["resp_relation"], ""),
    }
    return _render(request, "patrons/pages/register.html", context, status)


@router.get("/lecteurs/nouveau", include_in_schema=False)
def new_patron(request: Request, auth: CanRegister) -> HTMLResponse:
    return _registration_page(request, _registration_values(None, auth))


@router.post("/lecteurs/nouveau", include_in_schema=False)
def register_patron(request: Request, auth: CanRegister, form: Form) -> Response:
    values = _registration_values(form, auth)
    step = _first(form, "etape")
    if step == "corriger":
        return _registration_page(request, values)
    database = request.app.state.database
    confirmed = _first(form, "homonymes_ok") == "1"
    data = _registration(request, values, confirmed=confirmed)
    if step != "enregistrer":
        review = service.check_registration(database, data, visibility_of(auth))
        if review.blocking:
            return _registration_page(request, values, review=review, status=400)
        return _registration_page(request, values, step="verifier", review=review)
    try:
        patron_id = service.register(database, data, auth.user_id, visibility_of(auth))
    except RegistrationRefused as refused:
        only_homonyms = set(refused.review.errors) == {"homonymes"}
        return _registration_page(
            request, values, step="verifier" if only_homonyms else "saisir", review=refused.review, status=400
        )
    return _go(request, f"/lecteurs/{patron_id}", "patron_registered")


# --- Fiche ------------------------------------------------------------------------------------------------------------
def _record(request: Request, patron_id: int, visibility: repo.Visibility = repo.EVERYTHING) -> repo.Sheet:
    with request.app.state.database.session() as session:
        record = repo.sheet(session, patron_id, service.today(), visibility)
    if record is None:
        raise HTTPException(status_code=404)
    return record


def _provided(request: Request, name: str, *arguments: Any) -> Any:
    """Ce qu'un autre module ajoute à la fiche (les prêts, en L4). Rien s'il n'est pas installé."""
    for module in request.app.state.modules:
        provider = module.extras.get(name)
        if callable(provider):
            return provider(*arguments)
    return None


def _sheet_context(record: repo.Sheet, auth: AuthContext, history: list[repo.HistoryLine]) -> dict[str, Any]:
    """Ce que la fiche affiche. **La date complète et les observations n'y entrent qu'avec `view_personal`.**"""
    personal = auth.can("patrons.view_personal")
    patron = record.patron
    today = service.today().isoformat()
    return {
        "record": record,
        "patron": patron,
        "personal": personal,
        "living": patron.status in ("active", "suspended"),
        "anonymized": patron.status == "anonymized",
        "name": rules.display_name(patron.first_name, patron.last_name, patron.status),
        "initials_text": "?" if patron.status == "anonymized" else rules.initials(patron.first_name, patron.last_name),
        "category_label": rules.CATEGORIES[patron.category],
        "status_label": rules.STATUSES[patron.status],
        "birth_text": rules.french_date(patron.birth) if personal else str(record.marks.birth_year or ""),
        "notes": patron.notes if personal else None,
        "registered_on": rules.french_date(patron.registered_on),
        "membership": rules.french_date(patron.membership_expires_on),
        "membership_expired": bool(patron.membership_expires_on and patron.membership_expires_on < today),
        "fr_date": rules.french_date,
        "relation_options": list(rules.RELATIONS.items()),
        "household_roles": rules.MEMBER_ROLES,
        "history": history,
        "card_reasons": [
            ("lost", rules.CARD_END_REASONS["lost"]),
            ("damaged", rules.CARD_END_REASONS["damaged"]),
            ("revoked", rules.CARD_END_REASONS["revoked"]),
        ],
    }


def _sheet_page(
    request: Request,
    auth: AuthContext,
    patron_id: int,
    *,
    errors: dict[str, str] | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    record = _record(request, patron_id, visibility_of(auth))
    with request.app.state.database.session() as session:
        history = repo.history(session, patron_id)
        loans_card = _provided(request, "patron_loans", session, auth, patron_id)
    context = _sheet_context(record, auth, history)
    context.update({"errors": errors or {}, "message": message, "loans_card": loans_card})
    return _render(request, "patrons/pages/patron.html", context, status)


@router.get("/lecteurs/{patron_id:int}", include_in_schema=False)
def patron_page(request: Request, patron_id: int, auth: CanView) -> HTMLResponse:
    return _sheet_page(request, auth, patron_id)


def _act(request: Request, auth: AuthContext, patron_id: int, call: Any, code: str) -> Response:
    """Une action de la fiche : le refus est dit sur la fiche, sans rien perdre."""
    try:
        call()
    except FormErrors as refused:
        return _sheet_page(request, auth, patron_id, errors=refused.fields, status=400)
    except ConflictError as refused:
        return _sheet_page(request, auth, patron_id, message=refused.message, status=409)
    except BusinessError as refused:
        return _sheet_page(request, auth, patron_id, message=refused.message, status=400)
    return _go(request, f"/lecteurs/{patron_id}", code)


@router.post("/lecteurs/{patron_id:int}/majorite", include_in_schema=False)
def confirm_majority(request: Request, patron_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    version = _version(form)
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.confirm_majority(database, patron_id, version, auth.user_id),
        "patron_majority",
    )


@router.post("/lecteurs/{patron_id:int}/renouveler", include_in_schema=False)
def renew(request: Request, patron_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    version, until = _version(form), _first(form, "adhesion")
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.renew_membership(database, patron_id, until, version, auth.user_id),
        "patron_renewed",
    )


@router.post("/lecteurs/{patron_id:int}/carte", include_in_schema=False)
def end_card(request: Request, patron_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    replace = _first(form, "action") != "retirer"
    reason, manual = _first(form, "motif") or "revoked", _first(form, "numero") if replace else ""
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.end_card(
            database, patron_id, reason, replace=replace, manual_number=manual, user_id=auth.user_id
        ),
        "card_replaced" if replace else "card_ended",
    )


# --- Coordonnées : masquées tant qu'on ne demande pas à les voir (D17) ------------------------------------------------
def _contacts_context(request: Request, patron_id: int) -> dict[str, Any]:
    with request.app.state.database.session() as session:
        patron = session.get(Patron, patron_id)
        if patron is None:
            raise HTTPException(status_code=404)
        minor = patron.category == "minor"
        return {
            "patron_id": patron_id,
            "minor": minor,
            "lines": repo.contacts_of(session, patron_id) if not minor else [],
            "guardians": repo.guardian_contacts(session, patron_id) if minor else [],
        }


@router.post("/lecteurs/{patron_id:int}/coordonnees/afficher", include_in_schema=False)
def show_contacts(request: Request, patron_id: int, auth: CanSeePersonal) -> Response:
    """Afficher les coordonnées est un acte journalisé : c'est donc un POST, protégé contre la falsification."""
    context = _contacts_context(request, patron_id)
    service.record_contacts_shown(request.app.state.database, patron_id, auth.user_id)
    return _render(request, "patrons/fragments/contacts.html", context)


@router.get("/lecteurs/{patron_id:int}/coordonnees/masquer", include_in_schema=False)
def hide_contacts(request: Request, patron_id: int, _auth: CanSeePersonal) -> HTMLResponse:
    return _render(request, "patrons/fragments/contacts_masked.html", {"patron_id": patron_id})


# --- Modification -----------------------------------------------------------------------------------------------------
def _identity_values(patron: Patron, personal: bool) -> dict[str, str]:
    return {
        "prenom": patron.first_name,
        "nom": patron.last_name,
        "naissance": (rules.french_date(patron.birth) if personal else ""),
        "adhesion": rules.french_date(patron.membership_expires_on),
        "observations": (patron.notes or "") if personal else "",
    }


def _edit_page(
    request: Request,
    auth: AuthContext,
    patron_id: int,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    record = _record(request, patron_id)
    if record.patron.status == "anonymized":
        raise HTTPException(status_code=404)
    personal = auth.can("patrons.view_personal")
    context = {
        "patron": record.patron,
        "name": rules.display_name(record.patron.first_name, record.patron.last_name, record.patron.status),
        "values": values or _identity_values(record.patron, personal),
        "errors": errors or {},
        "message": message,
        "personal": personal,
    }
    return _render(request, "patrons/pages/edit.html", context, status)


@router.get("/lecteurs/{patron_id:int}/modifier", include_in_schema=False)
def edit_patron(request: Request, patron_id: int, auth: CanEdit) -> HTMLResponse:
    return _edit_page(request, auth, patron_id)


@router.post("/lecteurs/{patron_id:int}/modifier", include_in_schema=False)
def update_patron(request: Request, patron_id: int, auth: CanEdit, form: Form) -> Response:
    record = _record(request, patron_id)
    personal = auth.can("patrons.view_personal")
    values = {key: _first(form, key) for key in ("prenom", "nom", "naissance", "adhesion", "observations")}
    # Sans `view_personal`, ces champs ne sont pas dans le formulaire : leur valeur en base ne doit pas être effacée.
    birth = values["naissance"] if personal else record.patron.birth or ""
    notes = values["observations"] if personal else record.patron.notes or ""
    data = IdentityEdit(values["prenom"], values["nom"], birth, notes, values["adhesion"])
    before = record.patron.version
    try:
        service.update_identity(request.app.state.database, patron_id, data, _version(form), auth.user_id)
    except FormErrors as refused:
        return _edit_page(request, auth, patron_id, values=values, errors=refused.fields, status=400)
    except ConflictError as refused:
        return _edit_page(request, auth, patron_id, values=values, message=refused.message, status=409)
    except BusinessError as refused:
        return _edit_page(request, auth, patron_id, values=values, message=refused.message, status=400)
    return _go(
        request,
        f"/lecteurs/{patron_id}",
        "patron_updated" if _record(request, patron_id).patron.version != before else "patron_unchanged",
    )


CONTACT_FIELDS = (
    "courriel",
    "telephone",
    "telephone_territoire",
    "adresse_ligne1",
    "adresse_ligne2",
    "code_postal",
    "ville",
)


def _contacts_edit_page(
    request: Request,
    patron_id: int,
    *,
    values: dict[str, str] | None = None,
    review: Review | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    record = _record(request, patron_id)
    if record.patron.status == "anonymized" or record.patron.category == "minor":
        raise HTTPException(status_code=404)
    review = review or Review()
    context = {
        "patron": record.patron,
        "name": rules.display_name(record.patron.first_name, record.patron.last_name, record.patron.status),
        "values": values or dict.fromkeys(CONTACT_FIELDS, ""),
        "errors": review.errors,
        "phone_choices": review.phone_choices,
        "message": message,
    }
    return _render(request, "patrons/pages/edit_contacts.html", context, status)


def _current_contact_values(request: Request, patron_id: int) -> dict[str, str]:
    values = dict.fromkeys(CONTACT_FIELDS, "")
    with request.app.state.database.session() as session:
        for line in repo.contacts_of(session, patron_id):
            if line.shared_by:
                continue
            if line.kind == "email":
                values["courriel"] = line.value or ""
            elif line.kind == "phone":
                values["telephone"] = line.value or ""
            elif line.kind == "address":
                values.update(
                    adresse_ligne1=line.line1 or "",
                    adresse_ligne2=line.line2 or "",
                    code_postal=line.postal_code or "",
                    ville=line.city or "",
                )
    return values


@router.get(
    "/lecteurs/{patron_id:int}/coordonnees",
    include_in_schema=False,
    dependencies=[require("patrons.edit")],
)
def edit_contacts(request: Request, patron_id: int, auth: CanSeePersonal) -> HTMLResponse:
    """Ouvrir ce formulaire, c'est afficher les coordonnées : l'affichage est journalisé comme les autres."""
    response = _contacts_edit_page(request, patron_id, values=_current_contact_values(request, patron_id))
    service.record_contacts_shown(request.app.state.database, patron_id, auth.user_id)
    return response


@router.post(
    "/lecteurs/{patron_id:int}/coordonnees",
    include_in_schema=False,
    dependencies=[require("patrons.edit")],
)
def update_contacts(request: Request, patron_id: int, auth: CanSeePersonal, form: Form) -> Response:
    values = {name: _first(form, name) for name in CONTACT_FIELDS}
    data = ContactsEdit(
        email=values["courriel"],
        phone=values["telephone"],
        phone_territory=values["telephone_territoire"],
        address=AddressInput(
            values["adresse_ligne1"], values["adresse_ligne2"], values["code_postal"], values["ville"]
        ),
    )
    try:
        service.update_contacts(request.app.state.database, patron_id, data, _version(form), auth.user_id)
    except RegistrationRefused as refused:
        return _contacts_edit_page(request, patron_id, values=values, review=refused.review, status=400)
    except ConflictError as refused:
        return _contacts_edit_page(request, patron_id, values=values, message=refused.message, status=409)
    except BusinessError as refused:
        return _contacts_edit_page(request, patron_id, values=values, message=refused.message, status=400)
    return _go(request, f"/lecteurs/{patron_id}", "patron_contacts")


# --- Liens de la fiche : foyers et responsables (`patrons.edit`) ------------------------------------------------------
def _int(text: str) -> int | None:
    return int(text) if text.isdigit() and len(text) < 12 else None


def _rights(form: FormData, prefix: str = "") -> service.Rights:
    rank = _int(_first(form, f"{prefix}rang")) or 1
    return service.Rights(
        _first(form, f"{prefix}relation"),
        _first(form, f"{prefix}emprunte") == "1",
        _first(form, f"{prefix}contact") == "1",
        rank,
        _first(form, f"{prefix}seul") == "1",
    )


def _visible_id(request: Request, auth: AuthContext, text: str) -> int | None:
    """Un identifiant reçu dans un formulaire : il ne désigne qu'une fiche que ce compte peut voir."""
    identifier = _int(text)
    if identifier is None:
        return None
    with request.app.state.database.session() as session:
        status = session.scalar(select(Patron.status).where(Patron.id == identifier))
    return identifier if status is not None and visibility_of(auth).allows(status) else None


def _no_choice() -> None:
    raise BusinessError(_("Choisissez d’abord dans la liste."))


@router.post("/lecteurs/{patron_id:int}/foyers", include_in_schema=False)
def join_household(request: Request, patron_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    household, role = _int(_first(form, "foyer_id")), _first(form, "role")
    if household is None:
        return _act(request, auth, patron_id, _no_choice, "member_added")
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.add_member(database, household, patron_id, role, auth.user_id),
        "member_added",
    )


@router.post("/lecteurs/{patron_id:int}/foyers/{household_id:int}/retirer", include_in_schema=False)
def leave_household(request: Request, patron_id: int, household_id: int, auth: CanEdit) -> Response:
    database = request.app.state.database
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.remove_member(database, household_id, patron_id, auth.user_id),
        "member_removed",
    )


@router.post("/lecteurs/{patron_id:int}/responsables", include_in_schema=False)
def add_guardian(request: Request, patron_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    guardian, rights = _visible_id(request, auth, _first(form, "resp_id")), _rights(form)
    if guardian is None:
        return _act(request, auth, patron_id, _no_choice, "guardian_added")
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.add_guardian(database, patron_id, guardian, rights, auth.user_id),
        "guardian_added",
    )


@router.post("/lecteurs/{patron_id:int}/responsables/{guardian_id:int}", include_in_schema=False)
def update_guardian(request: Request, patron_id: int, guardian_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    rights, version = _rights(form), _version(form)
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.update_guardian(database, patron_id, guardian_id, rights, version, auth.user_id),
        "guardian_updated",
    )


@router.post("/lecteurs/{patron_id:int}/responsables/{guardian_id:int}/retirer", include_in_schema=False)
def remove_guardian(request: Request, patron_id: int, guardian_id: int, auth: CanEdit) -> Response:
    database = request.app.state.database
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.remove_guardian(database, patron_id, guardian_id, auth.user_id),
        "guardian_removed",
    )


# --- Inscrire une famille : saisir, vérifier et corriger, enregistrer -------------------------------------------------
ADULT_ROWS = 2
CHILD_ROWS = 6
CHILD_ROWS_SHOWN = 2
ADULT_KEYS = ("prenom", "nom", "courriel", "telephone", "telephone_territoire", "relation", "emprunte", "contact")
CHILD_KEYS = ("prenom", "nom", "naissance")
SHARED_FIELDS = (
    "foyer_courriel",
    "foyer_telephone",
    "foyer_telephone_territoire",
    "adresse_ligne1",
    "adresse_ligne2",
    "code_postal",
    "ville",
)
FAMILY_FIELDS = (
    "nom_foyer",
    *SHARED_FIELDS,
    *(f"a{index}_{key}" for index in range(ADULT_ROWS) for key in ADULT_KEYS),
    *(f"e{index}_{key}" for index in range(CHILD_ROWS) for key in CHILD_KEYS),
)


def _family_values(form: FormData | None) -> dict[str, str]:
    values = {name: _first(form, name) if form is not None else "" for name in FAMILY_FIELDS}
    for index in range(ADULT_ROWS):
        values[f"a{index}_relation"] = values[f"a{index}_relation"] or "parent"
    return values


def _filled(values: dict[str, str], prefix: str, keys: tuple[str, ...]) -> bool:
    return any(values[f"{prefix}{key}"].strip() for key in keys)


def _row_touched(values: dict[str, str], review: Review, prefix: str, keys: tuple[str, ...]) -> bool:
    """Une rangée repliée s'ouvre si elle est remplie ou signale une erreur : rien ne reste caché à corriger."""
    return _filled(values, prefix, keys) or any(key.startswith(prefix) for key in review.errors)


def _family_data(values: dict[str, str], *, confirmed: bool) -> FamilyRegistration:
    """Une ligne vide ne compte pas : les rangées disponibles ne créent personne."""
    adults: list[FamilyAdult] = []
    for index in range(ADULT_ROWS):
        prefix = f"a{index}_"
        if not _filled(values, prefix, ("prenom", "nom", "courriel", "telephone")):
            continue
        adults.append(
            FamilyAdult(
                PersonInput(
                    first_name=values[f"{prefix}prenom"],
                    last_name=values[f"{prefix}nom"],
                    category="adult",
                    email=values[f"{prefix}courriel"],
                    phone=values[f"{prefix}telephone"],
                    phone_territory=values[f"{prefix}telephone_territoire"],
                ),
                values[f"{prefix}relation"],
                values[f"{prefix}emprunte"] == "1",
                values[f"{prefix}contact"] == "1",
            )
        )
    children = [
        PersonInput(
            first_name=values[f"e{index}_prenom"],
            last_name=values[f"e{index}_nom"],
            category="minor",
            birth=values[f"e{index}_naissance"],
        )
        for index in range(CHILD_ROWS)
        if _filled(values, f"e{index}_", CHILD_KEYS)
    ]
    return FamilyRegistration(
        name=values["nom_foyer"],
        adults=tuple(adults),
        children=tuple(children),
        shared_email=values["foyer_courriel"],
        shared_phone=values["foyer_telephone"],
        shared_phone_territory=values["foyer_telephone_territoire"],
        shared_address=AddressInput(
            values["adresse_ligne1"], values["adresse_ligne2"], values["code_postal"], values["ville"]
        ),
        homonyms_confirmed=confirmed,
    )


def _family_page(
    request: Request,
    values: dict[str, str],
    *,
    step: str = "saisir",
    review: Review | None = None,
    data: FamilyRegistration | None = None,
    status: int = 200,
) -> HTMLResponse:
    review = review or Review()
    context = {
        "step": step,
        "values": values,
        "errors": review.errors,
        "homonyms": review.homonyms,
        "hidden_homonyms": review.hidden_homonyms,
        "phone_choices": review.phone_choices,
        "phones": review.phones,
        "data": data,
        "first_children": range(CHILD_ROWS_SHOWN),
        "more_children": range(CHILD_ROWS_SHOWN, CHILD_ROWS),
        "second_adult_open": _row_touched(values, review, "a1_", ("prenom", "nom", "courriel", "telephone")),
        "more_children_open": any(
            _row_touched(values, review, f"e{index}_", CHILD_KEYS) for index in range(CHILD_ROWS_SHOWN, CHILD_ROWS)
        ),
        "relation_options": list(rules.RELATIONS.items()),
        "relation_labels": rules.RELATIONS,
    }
    return _render(request, "patrons/pages/family_new.html", context, status)


@router.get("/foyers/nouveau", include_in_schema=False)
def new_family(request: Request, _auth: CanRegister) -> HTMLResponse:
    return _family_page(request, _family_values(None))


@router.post("/foyers/nouveau", include_in_schema=False)
def register_family(request: Request, auth: CanRegister, form: Form) -> Response:
    values = _family_values(form)
    step = _first(form, "etape")
    if step == "corriger":
        return _family_page(request, values)
    database = request.app.state.database
    data = _family_data(values, confirmed=_first(form, "homonymes_ok") == "1")
    if step != "enregistrer":
        review = service.check_family(database, data, visibility_of(auth))
        if review.blocking:
            return _family_page(request, values, review=review, status=400)
        return _family_page(request, values, step="verifier", review=review, data=data)
    try:
        household_id = service.register_family(database, data, auth.user_id, visibility_of(auth))
    except RegistrationRefused as refused:
        only_homonyms = set(refused.review.errors) == {"homonymes"}
        return _family_page(
            request,
            values,
            step="verifier" if only_homonyms else "saisir",
            review=refused.review,
            data=data,
            status=400,
        )
    return _go(request, f"/foyers/{household_id}", "family_registered")


# --- Structures et fiche d'un foyer -----------------------------------------------------------------------------------
@router.get("/foyers/structure", include_in_schema=False)
def new_structure(request: Request, _auth: CanEdit) -> HTMLResponse:
    return _render(request, "patrons/pages/structure_new.html", {"values": {"nom": ""}, "errors": {}})


@router.post("/foyers/structure", include_in_schema=False)
def create_structure(request: Request, auth: CanEdit, form: Form) -> Response:
    name = _first(form, "nom")
    try:
        household_id = service.create_household(request.app.state.database, "structure", name, "", auth.user_id)
    except FormErrors as refused:
        context = {"values": {"nom": name}, "errors": refused.fields}
        return _render(request, "patrons/pages/structure_new.html", context, 400)
    return _go(request, f"/foyers/{household_id}", "structure_created")


def _household_page(
    request: Request,
    auth: AuthContext,
    household_id: int,
    *,
    errors: dict[str, str] | None = None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        record = repo.household_sheet(session, household_id, visibility_of(auth))
    if record is None:
        raise HTTPException(status_code=404)
    personal = auth.can("patrons.view_personal")
    household = record.household
    context = {
        "record": record,
        "household": household,
        "personal": personal,
        "notes": household.notes if personal else None,
        "archived": household.archived_at is not None,
        "role_options": list(rules.MEMBER_ROLES[household.kind].items()),
        "errors": errors or {},
        "message": message,
    }
    return _render(request, "patrons/pages/household.html", context, status)


def _household_act(request: Request, auth: AuthContext, household_id: int, call: Any, code: str) -> Response:
    try:
        call()
    except FormErrors as refused:
        return _household_page(request, auth, household_id, errors=refused.fields, status=400)
    except ConflictError as refused:
        return _household_page(request, auth, household_id, message=refused.message, status=409)
    except BusinessError as refused:
        return _household_page(request, auth, household_id, message=refused.message, status=400)
    return _go(request, f"/foyers/{household_id}", code)


@router.get("/foyers/{household_id:int}", include_in_schema=False)
def household_page(request: Request, household_id: int, auth: CanView) -> HTMLResponse:
    return _household_page(request, auth, household_id)


@router.post("/foyers/{household_id:int}/modifier", include_in_schema=False)
def update_household(request: Request, household_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    name, version = _first(form, "nom"), _version(form)
    notes = _first(form, "observations")
    if not auth.can("patrons.view_personal"):
        with database.session() as session:
            record = repo.household_sheet(session, household_id)
        notes = (record.household.notes or "") if record is not None else ""
    return _household_act(
        request,
        auth,
        household_id,
        lambda: service.update_household(database, household_id, name, notes, version, auth.user_id),
        "household_updated",
    )


@router.post("/foyers/{household_id:int}/membres", include_in_schema=False)
def add_member(request: Request, household_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    patron, role = _visible_id(request, auth, _first(form, "membre_id")), _first(form, "role")
    if patron is None:
        return _household_act(request, auth, household_id, _no_choice, "member_added")
    return _household_act(
        request,
        auth,
        household_id,
        lambda: service.add_member(database, household_id, patron, role, auth.user_id),
        "member_added",
    )


@router.post("/foyers/{household_id:int}/membres/{patron_id:int}/role", include_in_schema=False)
def change_member_role(request: Request, household_id: int, patron_id: int, auth: CanEdit, form: Form) -> Response:
    database = request.app.state.database
    role = _first(form, "role")
    return _household_act(
        request,
        auth,
        household_id,
        lambda: service.change_member_role(database, household_id, patron_id, role, auth.user_id),
        "member_updated",
    )


@router.post("/foyers/{household_id:int}/membres/{patron_id:int}/structure", include_in_schema=False)
def member_structure_right(request: Request, household_id: int, patron_id: int, auth: CanEdit, form: Form) -> Response:
    allowed = _first(form, "structure") == "1"
    database = request.app.state.database
    return _household_act(
        request,
        auth,
        household_id,
        lambda: service.set_structure_right(database, household_id, patron_id, allowed, auth.user_id),
        "household_updated",
    )


@router.post("/foyers/{household_id:int}/membres/{patron_id:int}/retirer", include_in_schema=False)
def remove_member(request: Request, household_id: int, patron_id: int, auth: CanEdit) -> Response:
    database = request.app.state.database
    return _household_act(
        request,
        auth,
        household_id,
        lambda: service.remove_member(database, household_id, patron_id, auth.user_id),
        "member_removed",
    )


@router.post("/foyers/{household_id:int}/archiver", include_in_schema=False)
def archive_household(request: Request, household_id: int, auth: CanArchive, form: Form) -> Response:
    database = request.app.state.database
    version = _version(form)
    return _household_act(
        request,
        auth,
        household_id,
        lambda: service.set_household_archived(database, household_id, True, version, auth.user_id),
        "household_archived",
    )


@router.post("/foyers/{household_id:int}/reactiver", include_in_schema=False)
def restore_household(request: Request, household_id: int, auth: CanArchive, form: Form) -> Response:
    database = request.app.state.database
    version = _version(form)
    return _household_act(
        request,
        auth,
        household_id,
        lambda: service.set_household_archived(database, household_id, False, version, auth.user_id),
        "household_restored",
    )


@router.post("/foyers/{household_id:int}/coordonnees/afficher", include_in_schema=False)
def show_household_contacts(request: Request, household_id: int, auth: CanSeePersonal) -> HTMLResponse:
    with request.app.state.database.session() as session:
        record = repo.household_sheet(session, household_id)
        if record is None:
            raise HTTPException(status_code=404)
        lines = repo.household_contacts(session, household_id)
    service.record_household_contacts_shown(request.app.state.database, household_id, auth.user_id)
    context = {"household_id": household_id, "lines": lines}
    return _render(request, "patrons/fragments/household_contacts.html", context)


@router.get("/foyers/{household_id:int}/coordonnees/masquer", include_in_schema=False)
def hide_household_contacts(request: Request, household_id: int, _auth: CanSeePersonal) -> HTMLResponse:
    return _render(request, "patrons/fragments/household_contacts_masked.html", {"household_id": household_id})


def _household_contact_values(request: Request, household_id: int) -> tuple[Any, dict[str, str]]:
    values = dict.fromkeys(SHARED_FIELDS, "")
    with request.app.state.database.session() as session:
        record = repo.household_sheet(session, household_id)
        if record is None or record.household.archived_at is not None:
            raise HTTPException(status_code=404)
        for line in repo.household_contacts(session, household_id):
            if line.kind == "email":
                values["foyer_courriel"] = line.value or ""
            elif line.kind == "phone":
                values["foyer_telephone"] = line.value or ""
            elif line.kind == "address":
                values.update(
                    adresse_ligne1=line.line1 or "",
                    adresse_ligne2=line.line2 or "",
                    code_postal=line.postal_code or "",
                    ville=line.city or "",
                )
    return record.household, values


@router.get("/foyers/{household_id:int}/coordonnees", include_in_schema=False, dependencies=[require("patrons.edit")])
def edit_household_contacts(request: Request, household_id: int, auth: CanSeePersonal) -> HTMLResponse:
    household, values = _household_contact_values(request, household_id)
    service.record_household_contacts_shown(request.app.state.database, household_id, auth.user_id)
    context = {"household": household, "values": values, "errors": {}, "phone_choices": {}, "message": ""}
    return _render(request, "patrons/pages/household_contacts_edit.html", context)


@router.post("/foyers/{household_id:int}/coordonnees", include_in_schema=False, dependencies=[require("patrons.edit")])
def update_household_contacts(request: Request, household_id: int, auth: CanSeePersonal, form: Form) -> Response:
    household, _current = _household_contact_values(request, household_id)
    values = {name: _first(form, name) for name in SHARED_FIELDS}
    data = ContactsEdit(
        email=values["foyer_courriel"],
        phone=values["foyer_telephone"],
        phone_territory=values["foyer_telephone_territoire"],
        address=AddressInput(
            values["adresse_ligne1"], values["adresse_ligne2"], values["code_postal"], values["ville"]
        ),
    )
    context: dict[str, Any] = {
        "household": household,
        "values": values,
        "errors": {},
        "phone_choices": {},
        "message": "",
    }
    try:
        service.set_household_contacts(request.app.state.database, household_id, data, _version(form), auth.user_id)
    except RegistrationRefused as refused:
        errors = {f"foyer_{key}": text for key, text in refused.review.errors.items()}
        choices = {f"foyer_{key}": found for key, found in refused.review.phone_choices.items()}
        context.update(errors=errors, phone_choices=choices)
        return _render(request, "patrons/pages/household_contacts_edit.html", context, 400)
    except ConflictError as refused:
        context["message"] = refused.message
        return _render(request, "patrons/pages/household_contacts_edit.html", context, 409)
    return _go(request, f"/foyers/{household_id}", "household_contacts")


# --- Statut, conservation, anonymisation, export, à traiter -----------------------------------------------------------
STATUS_MESSAGES = {"suspend": "patron_suspended", "archive": "patron_archived", "reactivate": "patron_reactivated"}


@router.post("/lecteurs/{patron_id:int}/statut", include_in_schema=False)
def change_status(request: Request, patron_id: int, auth: CanArchive, form: Form) -> Response:
    database = request.app.state.database
    action, version = _first(form, "action"), _version(form)
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.change_status(database, patron_id, action, version, auth.user_id),
        STATUS_MESSAGES.get(action, "patron_updated"),
    )


@router.post("/lecteurs/{patron_id:int}/conservation", include_in_schema=False)
def place_hold(request: Request, patron_id: int, auth: CanAnonymize, form: Form) -> Response:
    database = request.app.state.database
    reason, until = _first(form, "motif_conservation"), _first(form, "jusqu_au")
    return _act(
        request,
        auth,
        patron_id,
        lambda: service.place_hold(database, patron_id, reason, until, auth.user_id),
        "hold_placed",
    )


@router.post("/lecteurs/{patron_id:int}/conservation/{hold_id:int}/lever", include_in_schema=False)
def lift_hold(request: Request, patron_id: int, hold_id: int, auth: CanAnonymize) -> Response:
    database = request.app.state.database
    return _act(request, auth, patron_id, lambda: service.lift_hold(database, hold_id, auth.user_id), "hold_lifted")


def _anonymize_page(
    request: Request,
    patron_id: int,
    *,
    errors: dict[str, str] | None = None,
    message: str = "",
    values: dict[str, str] | None = None,
    status: int = 200,
) -> HTMLResponse:
    record = _record(request, patron_id)
    if record.patron.status == "anonymized":
        raise HTTPException(status_code=404)
    context = {
        "patron": record.patron,
        "name": rules.display_name(record.patron.first_name, record.patron.last_name, record.patron.status),
        "preview": service.anonymize_preview(request.app.state.database, patron_id),
        "fr_date": rules.french_date,
        "reason_options": [("", _("Choisir un motif…")), *rules.ANONYMIZE_REASONS.items()],
        "errors": errors or {},
        "message": message,
        "values": values or {"motif": "", "precision": ""},
    }
    return _render(request, "patrons/pages/anonymize.html", context, status)


@router.get("/lecteurs/{patron_id:int}/anonymiser", include_in_schema=False)
def anonymize_page(request: Request, patron_id: int, _auth: CanAnonymize) -> HTMLResponse:
    return _anonymize_page(request, patron_id)


@router.post("/lecteurs/{patron_id:int}/anonymiser", include_in_schema=False)
def anonymize(request: Request, patron_id: int, auth: CanAnonymize, form: Form) -> Response:
    data = service.AnonymizeInput(
        _first(form, "numero"), _first(form, "mot_de_passe"), _first(form, "motif"), _first(form, "precision")
    )
    values = {"motif": data.reason, "precision": data.reason_text}
    try:
        service.anonymize(request.app.state.database, patron_id, data, auth.user_id)
    except FormErrors as refused:
        return _anonymize_page(request, patron_id, errors=refused.fields, values=values, status=400)
    except BusinessError as refused:
        return _anonymize_page(request, patron_id, message=refused.message, values=values, status=400)
    return _go(request, f"/lecteurs/{patron_id}", "patron_anonymized")


def _exported(request: Request, auth: AuthContext, patron_id: int) -> dict[str, Any]:
    try:
        data = service.export(request.app.state.database, patron_id, auth.user_id, visibility_of(auth))
    except BusinessError as refused:
        raise HTTPException(status_code=404) from refused
    with request.app.state.database.session() as session:
        data.update(_provided(request, "patron_export", session, patron_id) or {})
    return data


@router.get("/lecteurs/{patron_id:int}/export", include_in_schema=False)
def export_page(request: Request, patron_id: int, auth: CanExport) -> HTMLResponse:
    """La page imprimable : ce qu'elle affiche est sorti de la base, l'export est donc journalisé."""
    return _render(
        request, "patrons/pages/export.html", {"data": _exported(request, auth, patron_id), "patron_id": patron_id}
    )


@router.get("/lecteurs/{patron_id:int}/export.json", include_in_schema=False)
def export_json(request: Request, patron_id: int, auth: CanExport) -> Response:
    """Le fichier porte le numéro de lecteur, jamais le nom, et n'est pas conservé sur le serveur."""
    data = _exported(request, auth, patron_id)
    headers = {
        "Content-Disposition": f'attachment; filename="{data["lecteur"]["numero"]}.json"',
        "Cache-Control": "no-store",
    }
    return JSONResponse(data, headers=headers)


@router.get("/lecteurs/a-traiter", include_in_schema=False)
def to_handle_page(request: Request, auth: CanEdit) -> HTMLResponse:
    with request.app.state.database.session() as session:
        data = repo.to_handle(session, service.today(), visibility_of(auth))
    context = {"data": data, "lines": repo.TO_HANDLE_LINES, "show_held": auth.can("patrons.anonymize")}
    return _render(request, "patrons/pages/to_handle.html", context)
