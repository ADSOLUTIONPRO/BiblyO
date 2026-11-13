# SPDX-License-Identifier: EUPL-1.2
"""Écrans des prêts. Les textes tapés voyagent en POST : un nom ou un numéro de carte n'entre dans aucune adresse."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from biblyo.core import flash
from biblyo.core.errors import BusinessError, FormErrors, PermissionDenied
from biblyo.core.i18n import _
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.catalogue.models import Copy
from biblyo.modules.circulation import counter, rules, service
from biblyo.modules.circulation import repository as repo
from biblyo.modules.circulation.counter import CounterState
from biblyo.modules.circulation.models import Loan
from biblyo.modules.circulation.rules import Candidate, Nature, Verdict
from biblyo.modules.circulation.service import Actor, LendResult, LoanRefused
from biblyo.modules.holdings import repository as holdings
from biblyo.modules.patrons import repository as patron_repo
from biblyo.modules.patrons import rules as patron_rules
from biblyo.modules.patrons import service as patron_service
from biblyo.modules.patrons.models import Patron
from biblyo.modules.patrons.repository import Visibility

router = APIRouter()


# --- Opérations distinctes, et jamais deux fois (correctif de L4, PC4) ----------------------------------------------
SCAN_IN_FLIGHT = Verdict(
    "D5",
    Nature.FORBIDDEN,
    _("Un scan était en cours : rien n’a été enregistré. Vérifiez la liste, puis recommencez."),
)
ALREADY_RECORDED = Verdict("D6", Nature.INFO, _("Ce prêt était déjà enregistré : rien n’a été ajouté."))
BEING_RECORDED = Verdict("D6", Nature.INFO, _("Ce prêt est déjà en cours d’enregistrement : rien n’a été ajouté."))


def _scanning(form: FormData) -> bool:
    """Une requête qui porte un code scanné est un scan : elle ne valide, n'annule et ne renouvelle jamais rien."""
    value = form.get("code")
    return isinstance(value, str) and bool(value.strip())


class Claims:
    """Une validation par jeton et par compte : double clic, renvoi du navigateur ou réseau qui répète n'enregistrent
    jamais deux fois. Gardé 15 minutes en mémoire ; au-delà, ou après un redémarrage, la base refuse de toute façon
    un exemplaire déjà sorti (R1)."""

    TTL = 900.0
    LIMIT = 5000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[int, str], tuple[float, LendResult | None]] = {}

    def _purge(self, moment: float) -> None:
        stale = [key for key, (at, _result) in self._items.items() if moment - at > self.TTL]
        for key in stale:
            del self._items[key]
        while len(self._items) >= self.LIMIT:
            del self._items[next(iter(self._items))]

    def claim(self, key: tuple[int, str]) -> tuple[bool, LendResult | None]:
        """Vrai si ce jeton est neuf ; sinon, le résultat déjà enregistré, ou None s'il est encore en cours."""
        moment = time.monotonic()
        with self._lock:
            self._purge(moment)
            if key in self._items:
                return False, self._items[key][1]
            self._items[key] = (moment, None)
            return True, None

    def settle(self, key: tuple[int, str], result: LendResult) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), result)

    def release(self, key: tuple[int, str]) -> None:
        with self._lock:
            self._items.pop(key, None)


_CLAIMS_LOCK = threading.Lock()


def _claims(request: Request) -> Claims:
    with _CLAIMS_LOCK:
        found = getattr(request.app.state, "counter_claims", None)
        if not isinstance(found, Claims):
            found = Claims()
            request.app.state.counter_claims = found
        return found
CIRCULATION_PERMISSIONS = (
    rules.LEND,
    rules.RENEW,
    rules.OVERRIDE,
    rules.OVERRIDE_LIMIT,
    rules.HISTORY,
    rules.SETTINGS,
)
PAGE = "circulation/pages/counter.html"
FRAGMENT = "circulation/fragments/counter_state.html"


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _counter_access(auth: Annotated[AuthContext, require("patrons.view")]) -> AuthContext:
    """D11 et P2 : identifier un lecteur (`patrons.view`), et pouvoir prêter ou renouveler."""
    if not (auth.can(rules.LEND) or auth.can(rules.RENEW)):
        raise PermissionDenied
    return auth


CanCounter = Annotated[AuthContext, Depends(_counter_access)]
CanRenew = Annotated[AuthContext, require(rules.RENEW)]


def actor_of(auth: AuthContext) -> Actor:
    return Actor(auth.user_id, frozenset(code for code in CIRCULATION_PERMISSIONS if auth.can(code)))


def visibility_of(auth: AuthContext) -> Visibility:
    """Les mêmes règles que l'annuaire : une fiche retirée n'est ni proposée ni empruntable (C10)."""
    return Visibility(archived=auth.can("patrons.archive"), anonymized=auth.can("patrons.anonymize"))


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.render(request, template, context, status_code=status)
    return response


def _reply(
    request: Request,
    auth: AuthContext,
    form: FormData | None,
    state: CounterState,
    *,
    notice: Verdict | None = None,
    result: LendResult | None = None,
    keep_entries: bool = True,
    lent_reader_id: int | None = None,
    proposal: bool = True,
    fresh: bool = False,
    confirm_return: int | None = None,
    renewed: int | None = None,
) -> HTMLResponse:
    entries = form is not None and keep_entries
    with request.app.state.database.session() as session:
        view = counter.build(
            session,
            state,
            actor_of(auth),
            visibility_of(auth),
            notice=notice,
            due=counter.due_dates(form, state) if form is not None and entries else None,
            loaned_on=str(form.get("date_pret") or "")[:10] if form is not None and entries else "",
            reason_code=str(form.get("motif") or "")[:32] if form is not None and entries else "",
            reason_text=str(form.get("motif_texte") or "")[:200] if form is not None and entries else "",
            result=result,
            browse=counter.Browse.read(form if entries else None, personal=auth.can("patrons.view_personal")),
            lent_reader_id=lent_reader_id,
            proposal=(
                counter.read_proposal(session, form, visibility_of(auth)) if form is not None and proposal else None
            ),
        )
    view.fresh = fresh
    if confirm_return is not None and any(loan.id == confirm_return for loan in view.loans):
        view.confirm_return = confirm_return
    view.renewed = renewed
    template = FRAGMENT if request.headers.get("HX-Request") else PAGE
    return _render(request, template, {"view": view})


MAX_FROM_BOOK = 20


@router.get("/comptoir", include_in_schema=False)
def counter_page(
    request: Request,
    auth: CanCounter,
    exemplaire: Annotated[list[str] | None, Query(max_length=9)] = None,
    livre: Annotated[str, Query(max_length=9)] = "",
    lecteur: Annotated[str, Query(max_length=9)] = "",
    nouveau: Annotated[str, Query(max_length=1)] = "",
) -> HTMLResponse:
    """Depuis la fiche d'un livre : le comptoir s'ouvre avec ces exemplaires dans la liste, contrôlés comme un scan.
    Depuis la fiche d'un lecteur : avec ce lecteur choisi, revérifié comme un scan de sa carte.

    Rien n'est validé ici. Seuls des identifiants d'exemplaire, de notice et de fiche passent dans l'adresse."""
    state = CounterState(book_id=int(livre) if livre.isdecimal() and len(livre) < 10 else None)
    if lecteur.isdecimal() and len(lecteur) < 10:
        state.patron_id = int(lecteur)  # une fiche retirée ou inventée n'est pas reprise : `build` la refuse
    notice: Verdict | None = None
    wanted = [int(item) for item in (exemplaire or [])[:MAX_FROM_BOOK] if item.isdecimal()]
    if wanted:
        with request.app.state.database.session() as session:
            scanner = counter.Scanner(session, visibility_of(auth), state)
            for copy_id in wanted:
                scanner.apply(Candidate(rules.KIND_COPY, copy_id))
            notice = scanner.notice
    response = _reply(request, auth, None, state, notice=notice, fresh=nouveau == "1")
    if wanted:
        response.headers["X-Comptoir-Ajout"] = "1"
    return response


@router.post("/comptoir/scan", include_in_schema=False)
def counter_scan(request: Request, auth: CanCounter, form: Form) -> HTMLResponse:
    state = CounterState.read(form)
    code = str(form.get("code") or form.get("saisie") or "").strip()[:64]
    with request.app.state.database.session() as session:
        scanner = counter.Scanner(session, visibility_of(auth), state)
        if code:
            scanner.scan(code)
    return _reply(request, auth, form, state, notice=scanner.notice)


@router.post("/comptoir/choix", include_in_schema=False)
def counter_choice(request: Request, auth: CanCounter, form: Form) -> HTMLResponse:
    """Une ambiguïté levée, puis les codes qui attendaient, dans l'ordre où ils sont arrivés."""
    state = CounterState.read(form)
    with request.app.state.database.session() as session:
        scanner = counter.Scanner(session, visibility_of(auth), state)
        scanner.choose(str(form.get("choix") or "")[:64])
        scanner.drain()
    return _reply(request, auth, form, state, notice=scanner.notice)


@router.post("/comptoir/etat", include_in_schema=False)
def counter_state(request: Request, auth: CanCounter, form: Form) -> HTMLResponse:
    """Cadre d'emprunt changé, ligne retirée ou ajoutée, lecteur choisi ou à changer : rien n'est écrit.

    Un lecteur choisi dans la liste, ou un livre ajouté depuis le catalogue du comptoir, passe les mêmes contrôles
    qu'un scan : le formulaire ne désigne jamais qu'une fiche visible ou un exemplaire qui peut sortir."""
    state = CounterState.read(form)
    notice: Verdict | None = None
    removed = str(form.get("retirer") or "")
    if removed.isdecimal() and len(removed) < 10:
        state.copy_ids = [item for item in state.copy_ids if item != int(removed)]
    if form.get("changer"):
        state.patron_id, state.mode, state.guardian_id, state.household_id = None, None, None, None
    picked = counter._integer(form.get("choisir_lecteur"))
    added = counter._integer(form.get("ajouter"))
    keep_proposal = True
    with request.app.state.database.session() as session:
        scanner = counter.Scanner(session, visibility_of(auth), state)
        if picked is not None:
            if repo.reader_row(session, picked, visibility_of(auth)) is None:
                notice = rules.verdict("R4")
            else:
                scanner.apply(Candidate(rules.KIND_PATRON, picked))
        if added is not None:
            scanner.apply(Candidate(rules.KIND_COPY, added))
            notice = scanner.notice or notice
        if form.get("reprendre"):
            proposal = counter.read_proposal(session, form, visibility_of(auth))
            if proposal is not None:
                scanner.apply(Candidate(rules.KIND_PATRON, proposal.reader.id))
                if proposal.mode in rules.BORROW_MODES and proposal.mode != rules.MODE_STRUCTURE:
                    state.mode, state.guardian_id = proposal.mode, proposal.guardian_id
                kept = list(state.copy_ids)
                state.copy_ids = []
                for copy_id in dict.fromkeys([*proposal.copy_ids, *kept]):
                    scanner.apply(Candidate(rules.KIND_COPY, copy_id))
                notice = scanner.notice
            keep_proposal = False
        if form.get("ignorer"):
            keep_proposal = False
    asked = counter._integer(form.get("rendre")) if auth.can(rules.LEND) else None
    return _reply(request, auth, form, state, notice=notice, proposal=keep_proposal, confirm_return=asked)


READERS_FRAGMENT = "circulation/fragments/counter_readers.html"
BOOKS_FRAGMENT = "circulation/fragments/counter_books.html"
COPIES_FRAGMENT = "circulation/fragments/counter_copies.html"


@router.post("/comptoir/lecteurs", include_in_schema=False)
def counter_readers(request: Request, auth: CanCounter, form: Form) -> HTMLResponse:
    """La liste des lecteurs, filtrée pendant la frappe. **POST** : un nom cherché n'entre dans aucune adresse."""
    browse = counter.Browse.read(form, personal=auth.can("patrons.view_personal"))
    with request.app.state.database.session() as session:
        readers = counter.reader_page(
            session, browse.reader_query, browse.reader_page, personal=browse.personal, visibility=visibility_of(auth)
        )
    return _render(request, READERS_FRAGMENT, {"readers": readers, "show_all": form.get("lecteurs_tous") == "1"})


@router.post("/comptoir/livres", include_in_schema=False)
def counter_books(request: Request, auth: CanCounter, form: Form) -> HTMLResponse:
    """Le catalogue au comptoir, filtré pendant la frappe : ajouter un livre sans quitter la préparation du prêt."""
    browse = counter.Browse.read(form, personal=False)
    state = CounterState.read(form)
    with request.app.state.database.session() as session:
        books = counter.book_page(session, browse.book_query, browse.book_page, state.copy_ids)
    return _render(request, BOOKS_FRAGMENT, {"books": books})


@router.post("/comptoir/livres/{work_id:int}/exemplaires", include_in_schema=False)
def counter_copies(request: Request, work_id: int, auth: CanCounter, form: Form) -> HTMLResponse:
    """Les exemplaires d'un livre, pour en choisir un précis : édition, place, état. Jamais l'emprunteur."""
    state = CounterState.read(form)
    with request.app.state.database.session() as session:
        found = repo.book_copies(session, work_id, show_loans=False)
    if found is None:
        raise HTTPException(status_code=404)
    title, copies = found
    context = {"work_id": work_id, "title": title, "copies": copies, "cart": set(state.copy_ids)}
    return _render(request, COPIES_FRAGMENT, context)


@router.post("/comptoir/rendre/{loan_id:int}", include_in_schema=False)
def counter_return(request: Request, loan_id: int, auth: CanLend, form: Form) -> HTMLResponse:
    """P2 : rendre un prêt **du lecteur choisi au comptoir**, ce prêt-là seulement, sans quitter le comptoir."""
    if not auth.can("patrons.view"):
        raise PermissionDenied
    state = CounterState.read(form)
    if _scanning(form):
        return _reply(request, auth, form, state, notice=SCAN_IN_FLIGHT)
    database = request.app.state.database
    with database.session() as session:
        loan = session.get(Loan, loan_id)
        row = repo.reader_row(session, state.patron_id, visibility_of(auth)) if state.patron_id else None
        if loan is None or row is None or loan.patron_id != row.id:
            raise HTTPException(status_code=403)
        copy_id = loan.copy_id
    notice: Verdict
    try:
        result = service.return_copy(database, copy_id, actor_of(auth), expected_loan=loan_id)
    except LoanRefused as refused:
        notice = refused.general[0] if refused.general else rules.verdict("R12")
    else:
        if result.loan_id is None:
            notice = result.verdict or rules.nothing_to_return()
        else:
            with database.session() as session:
                brief = holdings.briefs(session, [copy_id]).get(copy_id)
            where = result.location_path or _("sans place attribuée")
            if result.location_path and result.shelf_position:
                where = f"{where} · {_('place')} {result.shelf_position}"
            message = _("Retour enregistré : « {title} ». À ranger : {where}.").format(
                title=brief.title if brief else "", where=where
            )
            notice = Verdict("D10", Nature.INFO, message)
    return _reply(request, auth, form, state, notice=notice)


@router.post("/comptoir/valider", include_in_schema=False)
def counter_validate(request: Request, auth: CanCounter, form: Form) -> Response:
    """Seule voie qui enregistre un prêt. Un scan n'y aboutit jamais, et un même jeton n'enregistre qu'une fois."""
    state = CounterState.read(form)
    if _scanning(form):
        return _reply(request, auth, form, state, notice=SCAN_IN_FLIGHT)
    token = str(form.get("jeton") or "").strip()[:64]
    key = (auth.user_id, token) if token else None
    claims = _claims(request)
    if key is not None:
        fresh, previous = claims.claim(key)
        if not fresh:
            if previous is not None:
                return _reply(
                    request, auth, None, CounterState(book_id=state.book_id), result=previous, notice=ALREADY_RECORDED
                )
            return _reply(request, auth, form, state, notice=BEING_RECORDED)
    try:
        result = service.lend(request.app.state.database, counter.request_from(form, state), actor_of(auth))
    except LoanRefused as refused:
        if key is not None:
            claims.release(key)
        first = refused.general or [item for found in refused.by_copy.values() for item in found]
        return _reply(request, auth, form, state, notice=first[0] if first else None)
    except BusinessError as error:
        if key is not None:
            claims.release(key)
        return _reply(request, auth, form, state, notice=Verdict("D6", Nature.FORBIDDEN, error.message))
    if key is not None:
        claims.settle(key, result)
    return _reply(
        request, auth, None, CounterState(book_id=state.book_id), result=result, lent_reader_id=state.patron_id
    )


@router.post("/comptoir/renouveler/{loan_id}", include_in_schema=False)
def counter_renew(request: Request, loan_id: int, auth: CanRenew, form: Form) -> Response:
    """P2 : seulement un prêt du lecteur sélectionné, que cette personne a le droit de voir."""
    if not auth.can("patrons.view"):
        raise PermissionDenied
    state = CounterState.read(form)
    if _scanning(form):
        return _reply(request, auth, form, state, notice=SCAN_IN_FLIGHT)
    database = request.app.state.database
    with database.session() as session:
        loan = session.get(Loan, loan_id)
        row = repo.reader_row(session, state.patron_id, visibility_of(auth)) if state.patron_id else None
        if loan is None or row is None or loan.patron_id != row.id:
            raise HTTPException(status_code=403)
        before = loan.due_on
        brief = holdings.briefs(session, [loan.copy_id]).get(loan.copy_id)
    if form.get("vient_renouvele") == str(loan_id):
        # U16 : ce prêt vient d'être renouvelé depuis cet écran ; un clic répété (double clic, file htmx, Entrée) ne
        # le renouvelle pas une seconde fois. Un nouveau renouvellement voulu passe par un autre geste d'abord.
        title = brief.title if brief else ""
        text_value = _("« {title} » vient d’être renouvelé : rien n’a été refait.").format(title=title)
        repeat = Verdict("D7-repete", Nature.INFO, text_value)
        return _reply(request, auth, form, state, notice=repeat, renewed=loan_id)
    notice: Verdict | None
    renewed_id: int | None = None
    try:
        renewed = service.renew(database, loan_id, actor_of(auth), expected_version=_version(form, loan_id))
    except LoanRefused as refused:
        notice = refused.general[0] if refused.general else None
    except BusinessError as error:
        notice = Verdict("D6", Nature.FORBIDDEN, error.message)
    else:
        # U15 : annoncé seulement maintenant, la base ayant confirmé ; le titre, l'ancienne et la nouvelle échéance
        message = _("« {title} » renouvelé : retour le {before}, désormais le {after}.").format(
            title=brief.title if brief else "",
            before=patron_rules.french_date(before),
            after=patron_rules.french_date(renewed.due_on),
        )
        if renewed.warnings:
            message = f"{message} {renewed.warnings[0].message}"
        notice = Verdict("D7", Nature.INFO, message)
        renewed_id = loan_id
    return _reply(request, auth, form, state, notice=notice, renewed=renewed_id)


def _version(form: FormData, loan_id: int) -> int | None:
    value = form.get(f"version_{loan_id}")
    return int(value) if isinstance(value, str) and value.isdecimal() and len(value) < 10 else None


# --- Retour rapide (D10, R25, R27) --------------------------------------------------------------------------------
CanLend = Annotated[AuthContext, require(rules.LEND)]
CanHistory = Annotated[AuthContext, require(rules.HISTORY)]
RETURN_PAGE = "circulation/pages/returns.html"
RETURN_LINE = "circulation/fragments/return_line.html"


@dataclass(frozen=True)
class ReturnLine:
    """Ce qui s'affiche après un scan. Le nom de l'emprunteur n'y entre qu'avec `circulation.history` (D11 bis)."""

    result: service.ReturnResult | None
    title: str = ""
    barcode: str = ""
    borrower: str | None = None
    message: Verdict | None = None
    choices: tuple[tuple[str, str], ...] = ()


def _return_line(
    session: Any, auth: AuthContext, result: service.ReturnResult, message: Verdict | None = None
) -> ReturnLine:
    brief = holdings.briefs(session, [result.copy_id]).get(result.copy_id)
    borrower = None
    if result.loan_id is not None and auth.can(rules.HISTORY):
        loan = session.get(Loan, result.loan_id)
        if loan is not None and (row := repo.reader_row(session, loan.patron_id, visibility_of(auth))):
            borrower = row.name
    return ReturnLine(
        result, brief.title if brief else "", brief.barcode if brief else "", borrower, message or result.verdict
    )


@router.get("/comptoir/retours", include_in_schema=False)
def returns_page(request: Request, auth: CanLend) -> HTMLResponse:
    return _render(request, RETURN_PAGE, {"lines": []})


def _reply_return(request: Request, line: ReturnLine) -> HTMLResponse:
    template = RETURN_LINE if request.headers.get("HX-Request") else RETURN_PAGE
    return _render(request, template, {"line": line, "lines": [line]})


@router.post("/comptoir/retours/scan", include_in_schema=False)
def returns_scan(request: Request, auth: CanLend, form: Form) -> HTMLResponse:
    """Chaque scan rend aussitôt. Un ISBN à plusieurs exemplaires sortis demande un choix (D5)."""
    code = str(form.get("choix_code") or form.get("code") or form.get("saisie") or "").strip()[:64]
    database = request.app.state.database
    with database.session() as session:
        found = list(dict.fromkeys(item.target_id for item in repo.copy_candidates(session, code))) if code else []
        out = repo.on_loan_among(session, found) if len(found) > 1 else found
        if len(out) > 1:
            briefs = holdings.briefs(session, out)
            choices = tuple((briefs[item].barcode, briefs[item].title) for item in out if item in briefs)
            return _reply_return(request, ReturnLine(None, message=rules.verdict("R10"), choices=choices))
        if not out:
            verdict = rules.nothing_to_return() if found else rules.verdict("R4")
            return _reply_return(request, ReturnLine(None, message=verdict))
    try:
        result = service.return_copy(database, out[0], actor_of(auth))
    except LoanRefused as refused:
        return _reply_return(request, ReturnLine(None, message=refused.general[0] if refused.general else None))
    with database.session() as session:
        return _reply_return(request, _return_line(session, auth, result))


RETURN_SEARCH = "circulation/fragments/return_search.html"


@router.post("/comptoir/retours/recherche", include_in_schema=False)
def returns_search(request: Request, auth: CanLend, form: Form) -> HTMLResponse:
    """Retrouver un livre sorti sans son code : par titre ou code ; par nom de lecteur seulement avec l'historique,
    sinon chercher un nom révélerait qui a quel livre (D11 bis). Aucun emprunteur n'est montré sans ce droit."""
    query = str(form.get("q_retour") or "").strip()[:100]
    history = auth.can(rules.HISTORY)
    rows: list[repo.ListRow] = []
    if query:
        with request.app.state.database.session() as session:
            rows = repo.loan_list(
                session, "cours", service.today(), visibility_of(auth), page=1, size=20, query=query, by_reader=history
            ).rows
    if not history:
        rows = [replace(row, borrower=None, responsible=None) for row in rows]
    return _render(request, RETURN_SEARCH, {"rows": rows, "query": query, "history": history})


@router.post("/comptoir/retours/pret/{loan_id:int}", include_in_schema=False)
def returns_pick(request: Request, loan_id: int, auth: CanLend, form: Form) -> HTMLResponse:
    """Le prêt choisi dans la recherche : celui-là, rendu aussitôt, comme un scan, et annulable de la même façon."""
    if _scanning(form):
        return _reply_return(request, ReturnLine(None, message=SCAN_IN_FLIGHT))
    database = request.app.state.database
    with database.session() as session:
        loan = session.get(Loan, loan_id)
        copy_id = loan.copy_id if loan is not None else None
    if copy_id is None:
        raise HTTPException(status_code=404)
    try:
        result = service.return_copy(database, copy_id, actor_of(auth), expected_loan=loan_id)
    except LoanRefused as refused:
        return _reply_return(request, ReturnLine(None, message=refused.general[0] if refused.general else None))
    if result.loan_id is None:
        return _reply_return(request, ReturnLine(None, message=result.verdict))
    with database.session() as session:
        return _reply_return(request, _return_line(session, auth, result))


@router.post("/comptoir/retours/{loan_id:int}/annuler", include_in_schema=False)
def returns_cancel(request: Request, loan_id: int, auth: CanLend, form: Form) -> HTMLResponse:
    if _scanning(form):
        return _reply_return(request, ReturnLine(None, message=SCAN_IN_FLIGHT))
    database = request.app.state.database
    notice: Verdict
    try:
        service.cancel_return(database, loan_id, actor_of(auth))
        notice = Verdict("D9", Nature.INFO, _("Retour annulé : le prêt court à nouveau."))
    except LoanRefused as refused:
        notice = refused.general[0] if refused.general else rules.verdict("R12")
    except BusinessError as error:
        notice = Verdict("D9", Nature.FORBIDDEN, error.message)
    return _reply_return(request, ReturnLine(None, message=notice))


@router.post("/comptoir/annuler/{operation_id:int}", include_in_schema=False)
def counter_cancel(request: Request, operation_id: int, auth: CanCounter, form: Form) -> HTMLResponse:
    """Le prêt tout juste validé. Au-delà de 15 minutes, la fiche du prêt demande un motif (D9)."""
    if _scanning(form):
        return _reply(request, auth, form, CounterState.read(form), notice=SCAN_IN_FLIGHT)
    notice: Verdict
    try:
        service.cancel_lend(request.app.state.database, operation_id, actor_of(auth))
        notice = Verdict("D9", Nature.INFO, _("Le prêt est annulé. Sa trace reste dans l’historique."))
    except LoanRefused as refused:
        notice = refused.general[0] if refused.general else rules.verdict("R12")
    except BusinessError as error:
        notice = Verdict("D9", Nature.FORBIDDEN, error.message)
    return _reply(request, auth, None, CounterState(), notice=notice)


# --- Fiche d'un prêt : emprunteur, historique, corrections (D8, D9) -----------------------------------------------
SHEET_PAGE = "circulation/pages/loan.html"


def _sheet_or_403(session: Any, auth: AuthContext, loan_id: int) -> repo.LoanSheet:
    sheet = repo.loan_sheet(session, loan_id)
    if sheet is None:
        raise HTTPException(status_code=404)
    if repo.reader_row(session, sheet.loan.patron_id, visibility_of(auth)) is None:
        raise HTTPException(status_code=403)  # les fiches retirées restent fermées, y compris par ce chemin
    return sheet


def _sheet_page(
    request: Request, auth: AuthContext, loan_id: int, message: str = "", status: int = 200
) -> HTMLResponse:
    with request.app.state.database.session() as session:
        sheet = _sheet_or_403(session, auth, loan_id)
    context = {
        "record": sheet,
        "message": message,
        "event_labels": rules.EVENT_KINDS,
        "correction_reasons": rules.CORRECTION_REASONS,
        "cancel_reasons": rules.CANCEL_REASONS,
        "can_override": auth.can(rules.OVERRIDE),
        "can_lend": auth.can(rules.LEND),
        "channels": rules.REMINDER_CHANNELS,
        "results": rules.REMINDER_RESULTS,
        "today": service.today().isoformat(),
    }
    return _render(request, SHEET_PAGE, context, status)


@router.get("/prets/{loan_id:int}", include_in_schema=False)
def loan_page(request: Request, loan_id: int, auth: CanHistory) -> HTMLResponse:
    return _sheet_page(request, auth, loan_id)


def _loan_act(
    request: Request,
    auth: AuthContext,
    loan_id: int,
    call: Callable[[], object],
    code: str,
    target: str | None = None,
) -> Response:
    with request.app.state.database.session() as session:
        _sheet_or_403(session, auth, loan_id)
    try:
        call()
    except BusinessError as refused:
        return _sheet_page(request, auth, loan_id, refused.message, 400)
    response = RedirectResponse(target or f"/prets/{loan_id}", status_code=303)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def _reason_of(form: FormData) -> tuple[str | None, str | None]:
    return str(form.get("motif") or "")[:32] or None, str(form.get("motif_texte") or "")[:200] or None


@router.post("/prets/{loan_id:int}/echeance", include_in_schema=False)
def loan_due(request: Request, loan_id: int, auth: CanHistory, form: Form) -> Response:
    new_due = str(form.get("echeance") or "")[:10]
    database = request.app.state.database
    code, text_value = _reason_of(form)

    def call() -> object:
        return service.change_due(database, loan_id, new_due, actor_of(auth), reason_code=code, reason_text=text_value)

    return _loan_act(request, auth, loan_id, call, "loan_due_changed")


@router.post("/prets/{loan_id:int}/date-retour", include_in_schema=False)
def loan_return_date(request: Request, loan_id: int, auth: CanHistory, form: Form) -> Response:
    day = str(form.get("date_retour") or "")[:10]
    database = request.app.state.database
    code, text_value = _reason_of(form)

    def call() -> object:
        service.correct_return_date(database, loan_id, day, actor_of(auth), reason_code=code, reason_text=text_value)
        return None

    return _loan_act(request, auth, loan_id, call, "loan_corrected")


@router.post("/prets/{loan_id:int}/annuler", include_in_schema=False)
def loan_cancel(request: Request, loan_id: int, auth: CanHistory, form: Form) -> Response:
    """Annule la dernière opération de ce prêt : le prêt lui-même, son retour, ou son renouvellement."""
    what = str(form.get("quoi") or "")
    database = request.app.state.database
    actor = actor_of(auth)
    code, text_value = _reason_of(form)

    def call() -> object:
        if what == "retour":
            service.cancel_return(database, loan_id, actor, reason_code=code, reason_text=text_value)
        elif what == "renouvellement":
            service.cancel_renewal(database, loan_id, actor, reason_code=code, reason_text=text_value)
        else:
            with database.session() as session:
                operation_id = session.get(Loan, loan_id).operation_id
            service.cancel_lend(database, operation_id, actor, reason_code=code, reason_text=text_value)
        return None

    return _loan_act(request, auth, loan_id, call, "loan_cancelled")


@router.post("/prets/{loan_id:int}/lecteur", include_in_schema=False)
def loan_reader(request: Request, loan_id: int, auth: CanHistory, form: Form) -> Response:
    """Mauvais lecteur : un prêt lié remplace le prêt fautif, qui reste dans l'historique (D9)."""
    database = request.app.state.database
    actor = actor_of(auth)
    visibility = visibility_of(auth)
    new_id = form.get("nouveau")
    guardian = form.get("nouveau_responsable")
    mode = str(form.get("cadre") or "")

    def call() -> object:
        reader = int(new_id) if isinstance(new_id, str) and new_id.isdecimal() and len(new_id) < 10 else None
        adult = int(guardian) if isinstance(guardian, str) and guardian.isdecimal() and len(guardian) < 10 else None
        with database.session() as session:
            known = reader is not None and repo.reader_row(session, reader, visibility) is not None
        if not known or reader is None:
            raise BusinessError(_("Choisissez le bon lecteur."))
        if mode not in rules.BORROW_MODES or mode == rules.MODE_STRUCTURE:
            raise BusinessError(_("Choisissez le cadre de l’emprunt."))
        code, text_value = _reason_of(form)
        request_ = service.LendRequest(reader, (), mode, adult, None, None, code, text_value)
        return service.correct_borrower(database, loan_id, request_, actor)

    return _loan_act(request, auth, loan_id, call, "loan_corrected")


# --- Retour et renouvellement directs, depuis une liste ou une fiche (UX-Circulation 2.0) ----------------------------
def _back_to(form: FormData, loan_id: int) -> str:
    """Revenir là d'où l'on vient : une liste des prêts ou une fiche de lecteur, jamais une adresse extérieure."""
    target = str(form.get("retour") or "")[:200]
    if target.startswith(("/prets", "/lecteurs/")) and "//" not in target and "\\" not in target:
        return target
    return f"/prets/{loan_id}"


@router.post("/prets/{loan_id:int}/retour", include_in_schema=False)
def loan_return(request: Request, loan_id: int, auth: CanHistory, form: Form) -> Response:
    """Ce prêt-là, et seulement s'il court encore : un prêt plus récent du même exemplaire n'est jamais rendu (R12)."""
    if not auth.can(rules.LEND):
        raise PermissionDenied
    database = request.app.state.database

    def call() -> object:
        with database.session() as session:
            loan = session.get(Loan, loan_id)
            copy_id = loan.copy_id if loan is not None else None
        if copy_id is None:
            raise BusinessError(_("Ce prêt n’existe plus."))
        result = service.return_copy(database, copy_id, actor_of(auth), expected_loan=loan_id)
        if result.loan_id is None:
            raise BusinessError(rules.nothing_to_return().message)
        return result

    return _loan_act(request, auth, loan_id, call, "loan_returned", _back_to(form, loan_id))


@router.post("/prets/{loan_id:int}/renouveler", include_in_schema=False)
def loan_renew(request: Request, loan_id: int, auth: CanHistory, form: Form) -> Response:
    if not auth.can(rules.RENEW):
        raise PermissionDenied
    database = request.app.state.database

    def call() -> object:
        return service.renew(database, loan_id, actor_of(auth), expected_version=_version(form, loan_id))

    return _loan_act(request, auth, loan_id, call, "loan_renewed", _back_to(form, loan_id))


# --- Listes des prêts (CIR-08, D11 bis) -----------------------------------------------------------------------------
LIST_PAGE = "circulation/pages/loans.html"
SEARCH_RESULTS = 50


@router.get("/prets", include_in_schema=False)
def loans_page(
    request: Request,
    auth: CanHistory,
    vue: Annotated[str, Query(max_length=20)] = "cours",
    page: Annotated[str, Query(max_length=9)] = "1",
    par_page: Annotated[str, Query(max_length=9)] = "",
    lecteur: Annotated[str, Query(max_length=9)] = "",
) -> HTMLResponse:
    """Seuls la vue, la page et un identifiant de fiche passent dans l'adresse : jamais un nom ni un numéro de carte."""
    view = vue if vue in repo.LIST_VIEWS else "cours"
    size = patron_rules.page_size(par_page)
    number = patron_rules.page_number(page)
    visibility = visibility_of(auth)
    patron_id = int(lecteur) if lecteur.isdecimal() else None
    with request.app.state.database.session() as session:
        patron = repo.reader_row(session, patron_id, visibility) if patron_id is not None else None
        if patron_id is not None and patron is None:
            raise HTTPException(status_code=403)
        found = repo.loan_list(session, view, service.today(), visibility, page=number, size=size, patron_id=patron_id)
        pages = max(1, -(-found.total // size))
        if number > pages:
            number = pages
            found = repo.loan_list(
                session, view, service.today(), visibility, page=number, size=size, patron_id=patron_id
            )
    return _list_page(request, view, found, number, pages, size, patron, patron_id)


def _list_page(
    request: Request,
    view: str,
    found: repo.LoanList,
    number: int,
    pages: int,
    size: int,
    patron: Any,
    patron_id: int | None,
    query: str = "",
) -> HTMLResponse:
    from biblyo.modules.catalogue.repository import load_cards

    with request.app.state.database.session() as session:
        cards = {card.id: card for card in load_cards(session, sorted({row.work_id for row in found.rows}))}
    context = {
        "view": view,
        "views": repo.LIST_VIEWS,
        "found": found,
        "page": number,
        "pages": pages,
        "size": size,
        "patron": patron,
        "query_base": f"&lecteur={patron_id}" if patron_id is not None else "",
        "query": query,
        "cards": cards,
        "here": f"/prets?vue={view}" + (f"&lecteur={patron_id}" if patron_id is not None else ""),
    }
    return _render(request, LIST_PAGE, context)


@router.post("/prets/recherche", include_in_schema=False)
def loans_search(request: Request, auth: CanHistory, form: Form) -> HTMLResponse:
    """Chercher un prêt par titre, code ou nom de lecteur. **POST** : un nom n’entre dans aucune adresse."""
    view = str(form.get("vue") or "cours")
    view = view if view in repo.LIST_VIEWS else "cours"
    query = str(form.get("q") or "").strip()[:100]
    visibility = visibility_of(auth)
    with request.app.state.database.session() as session:
        found = repo.loan_list(
            session, view, service.today(), visibility, page=1, size=SEARCH_RESULTS, query=query, by_reader=True
        )
    return _list_page(request, view, found, 1, 1, SEARCH_RESULTS, None, None, query)


# --- Fournis aux autres modules : fiche lecteur et tableau de bord ---------------------------------------------------
def patron_loans_card(session: Any, auth: AuthContext, patron_id: int) -> repo.PatronLoans:
    """La carte « Prêts » d'une fiche : le nombre pour tous, le détail avec `circulation.history` (P2)."""
    return repo.patron_loans(session, patron_id, service.today(), visibility_of(auth), auth.can(rules.HISTORY))


def book_loans_tab(session: Any, auth: AuthContext | None, work_id: int) -> repo.BookLoans | None:
    """L'onglet « Prêts » d'une fiche : rien sans droit sur les prêts, l'emprunteur seulement avec l'historique."""
    if auth is None or not (auth.can(rules.LEND) or auth.can(rules.RENEW) or auth.can(rules.HISTORY)):
        return None
    return repo.book_loans(session, work_id, service.today(), visibility_of(auth), auth.can(rules.HISTORY))


def dashboard_context(session: Any, auth: AuthContext) -> dict[str, Any]:
    if not (auth.can(rules.LEND) or auth.can(rules.RENEW) or auth.can(rules.HISTORY)):
        return {}
    visibility = visibility_of(auth) if auth.can(rules.HISTORY) else None
    return {"loan_stats": repo.loan_stats(session, service.today(), visibility)}


# --- À rappeler et rappels notés (D15) ------------------------------------------------------------------------------
REMIND_PAGE = "circulation/pages/remind.html"


@router.get("/prets/a-rappeler", include_in_schema=False)
def remind_page(request: Request, auth: CanHistory, page: Annotated[str, Query(max_length=9)] = "1") -> HTMLResponse:
    size = patron_rules.DEFAULT_PAGE_SIZE
    number = patron_rules.page_number(page)
    with request.app.state.database.session() as session:
        rows, total = repo.to_remind(session, service.today(), visibility_of(auth), page=number, size=size)
    context = {
        "rows": rows,
        "total": total,
        "page": number,
        "pages": max(1, -(-total // size)),
        "channels": rules.REMINDER_CHANNELS,
        "results": rules.REMINDER_RESULTS,
        "today": service.today().isoformat(),
    }
    return _render(request, REMIND_PAGE, context)


@router.post("/prets/{loan_id:int}/rappel", include_in_schema=False)
def remind_note(request: Request, loan_id: int, auth: CanHistory, form: Form) -> Response:
    """Seuls la date, le canal et le résultat sont lus : aucun autre champ n'est enregistré, quoi qu'on envoie."""
    day = str(form.get("date") or "")[:10] or None
    channel = str(form.get("canal") or "")[:20]
    result = str(form.get("resultat") or "")[:20]
    database = request.app.state.database

    def call() -> object:
        return service.note_reminder(database, loan_id, channel, result, actor_of(auth), reminded_on=day)

    target = "/prets/a-rappeler" if form.get("retour") == "liste" else None
    return _loan_act(request, auth, loan_id, call, "reminder_noted", target)


@router.post("/prets/{loan_id:int}/coordonnees", include_in_schema=False)
def remind_contacts(request: Request, loan_id: int, auth: CanHistory) -> HTMLResponse:
    """Les coordonnées de qui joindre, à la demande, journalisées comme sur la fiche (D15, L3)."""
    if not auth.can("patrons.view_personal"):
        raise PermissionDenied
    with request.app.state.database.session() as session:
        sheet = _sheet_or_403(session, auth, loan_id)
        patron_id = sheet.loan.patron_id
        patron = session.get(Patron, patron_id)
        minor = patron is not None and patron.category == "minor"
        context = {
            "minor": minor,
            "lines": [] if minor else patron_repo.contacts_of(session, patron_id),
            "guardians": patron_repo.guardian_contacts(session, patron_id) if minor else [],
        }
    patron_service.record_contacts_shown(request.app.state.database, patron_id, auth.user_id)
    return _render(request, "circulation/fragments/remind_contacts.html", context)


# --- Règles de prêt (`circulation.settings`) et simulation de conservation (D13) -----------------------------------
CanSettings = Annotated[AuthContext, require(rules.SETTINGS)]
SETTINGS_PAGE = "circulation/pages/settings.html"


def _settings_page(
    request: Request, errors: dict[str, str] | None = None, message: str = "", typed: dict[str, str] | None = None
) -> HTMLResponse:
    from biblyo.modules.catalogue.repository import document_types

    with request.app.state.database.session() as session:
        context = {
            "rules": repo.rule_views(session),
            "values": repo.settings_values(session),
            "preview": repo.retention_preview(session, service.today()),
            "document_types": document_types(session),
        }
    context.update(
        {
            "limit_modes": rules.LIMIT_MODES,
            "weekdays": rules.WEEKDAYS,
            "categories": rules.RULE_CATEGORIES,
            "errors": errors or {},
            "message": message,
            "typed": typed or {},
        }
    )
    return _render(request, SETTINGS_PAGE, context, 400 if errors or message else 200)


def _go_settings(request: Request, code: str) -> Response:
    response = RedirectResponse("/prets/regles", status_code=303)
    flash.set_flash(response, code, request.app.state.settings)
    return response


def _text(form: FormData, name: str, limit: int = 12) -> str:
    value = form.get(name)
    return value.strip()[:limit] if isinstance(value, str) else ""


@router.get("/prets/regles", include_in_schema=False)
def settings_page(request: Request, auth: CanSettings) -> HTMLResponse:
    return _settings_page(request)


@router.post("/prets/regles", include_in_schema=False)
def settings_save(request: Request, auth: CanSettings, form: Form) -> Response:
    with request.app.state.database.session() as session:
        ids = [rule.id for rule in repo.rule_views(session)]
    data = service.SettingsInput(
        rules={
            rule_id: service.RuleValues(
                _text(form, f"regle_{rule_id}_duree"),
                _text(form, f"regle_{rule_id}_prolongation"),
                _text(form, f"regle_{rule_id}_renouvellements"),
                _text(form, f"regle_{rule_id}_plafond"),
            )
            for rule_id in ids
        },
        limit_mode=_text(form, "mode_plafond"),
        overdue_block=_text(form, "seuil_retards"),
        closed_weekday=_text(form, "fermeture"),
        retention_months=_text(form, "conservation_mois"),
    )
    try:
        service.update_settings(request.app.state.database, data, actor_of(auth))
    except FormErrors as refused:
        typed = {key: value for key, value in form.items() if isinstance(value, str) and key != "_csrf"}
        return _settings_page(request, refused.fields, typed=typed)
    return _go_settings(request, "loan_rules_saved")


@router.post("/prets/regles/ajouter", include_in_schema=False)
def settings_add_rule(request: Request, auth: CanSettings, form: Form) -> Response:
    values = service.RuleValues(
        _text(form, "duree"), _text(form, "prolongation"), _text(form, "renouvellements"), _text(form, "plafond")
    )
    try:
        service.add_rule(
            request.app.state.database, _text(form, "type"), _text(form, "categorie", 20), values, actor_of(auth)
        )
    except FormErrors as refused:
        return _settings_page(request, message=refused.message)
    return _go_settings(request, "loan_rules_saved")


@router.post("/prets/regles/supprimer", include_in_schema=False)
def settings_delete_rule(request: Request, auth: CanSettings, form: Form) -> Response:
    rule = _text(form, "supprimer")
    try:
        if not rule.isdecimal():
            raise BusinessError(_("Cette règle n’existe plus."))
        service.delete_rule(request.app.state.database, int(rule), actor_of(auth))
    except BusinessError as refused:
        return _settings_page(request, message=refused.message)
    return _go_settings(request, "loan_rules_saved")


def patron_export(session: Any, patron_id: int) -> dict[str, Any]:
    return repo.patron_export(session, patron_id)


# --- Depuis la fiche d'un livre : quels exemplaires peuvent sortir, et pourquoi pas les autres -----------------------
BOOK_PAGE = "circulation/pages/book.html"


@dataclass(frozen=True)
class Returned:
    """Ce que dit un retour enregistré : le livre, la date, l'état final, où le ranger. Jamais l'emprunteur."""

    title: str
    barcode: str
    day: str
    days_overdue: int
    location_id: int | None
    location_path: str | None
    position: int | None
    state: Any = None


def _book_context(session: Any, auth: AuthContext, work_id: int) -> dict[str, Any]:
    from biblyo.modules.catalogue.repository import load_cards

    found = repo.book_copies(session, work_id, show_loans=auth.can(rules.HISTORY))
    if found is None:
        raise HTTPException(status_code=404)
    title, copies = found
    cards = load_cards(session, [work_id])
    return {
        "work_id": work_id,
        "title": title,
        "card": cards[0] if cards else None,
        "copies": copies,
        "loanable": [item for item in copies if item.reason is None],
        "out": [item for item in copies if item.active_loan is not None],
        "returned": None,
        "message": None,
    }


@router.get("/comptoir/livre/{work_id:int}", include_in_schema=False)
def book_page(request: Request, work_id: int, auth: CanCounter) -> HTMLResponse:
    """Aucun détenteur n'y figure : l'indisponibilité et l'échéance, et le lien vers le prêt avec l'historique (R27)."""
    with request.app.state.database.session() as session:
        context = _book_context(session, auth, work_id)
    return _render(request, BOOK_PAGE, context)


@router.get("/comptoir/livre/{work_id:int}/preter", include_in_schema=False)
def book_lend(request: Request, work_id: int, auth: CanCounter) -> Response:
    """« Prêter » depuis une carte du catalogue : un seul exemplaire peut sortir, le comptoir s'ouvre avec lui ;
    sinon, le choix des exemplaires. Rien n'est enregistré ici."""
    with request.app.state.database.session() as session:
        found = repo.book_copies(session, work_id, show_loans=False)
    if found is None:
        raise HTTPException(status_code=404)
    ready = [item for item in found[1] if item.reason is None]
    if len(ready) == 1:
        return RedirectResponse(f"/comptoir?exemplaire={ready[0].id}&livre={work_id}", status_code=303)
    return RedirectResponse(f"/comptoir/livre/{work_id}", status_code=303)


@router.post("/comptoir/livre/{work_id:int}/retour", include_in_schema=False)
def book_return(request: Request, work_id: int, auth: CanLend, form: Form) -> HTMLResponse:
    """Enregistrer le retour d'un exemplaire de ce livre, après confirmation : celui-ci, et ce prêt-là seulement."""
    if not auth.can("patrons.view"):
        raise PermissionDenied
    copy_id = _number(form.get("exemplaire_rendu"))
    loan_id = _number(form.get("pret"))
    database = request.app.state.database
    with database.session() as session:
        found = repo.book_copies(session, work_id, show_loans=False)
    if found is None:
        raise HTTPException(status_code=404)
    if copy_id is None or copy_id not in {item.id for item in found[1]}:
        raise HTTPException(status_code=400)
    message: Verdict | None = None
    returned: Returned | None = None
    try:
        result = service.return_copy(database, copy_id, actor_of(auth), expected_loan=loan_id)
    except LoanRefused as refused:
        message = refused.general[0] if refused.general else rules.verdict("R12")
    else:
        if result.loan_id is None:
            message = result.verdict
        else:
            with database.session() as session:
                from biblyo.modules.holdings import availability

                brief = holdings.briefs(session, [copy_id]).get(copy_id)
                copy = session.get(Copy, copy_id)
                returned = Returned(
                    brief.title if brief else "",
                    brief.barcode if brief else "",
                    patron_rules.french_date(service.today().isoformat()),
                    result.days_overdue,
                    copy.location_id if copy else None,
                    result.location_path,
                    result.shelf_position,
                    availability.for_copies(session, [copy_id]).get(copy_id),
                )
    with database.session() as session:
        context = _book_context(session, auth, work_id)
    context.update({"returned": returned, "message": message})
    return _render(request, BOOK_PAGE, context, 200 if message is None or returned else 409)


def _number(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.isdecimal() and len(value) < 10 else None
