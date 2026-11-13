# SPDX-License-Identifier: EUPL-1.2
"""Le comptoir, sans état côté serveur : la liste voyage dans le formulaire, et tout est revérifié à chaque échange.

Un code passe par une seule fonction, `scan` : douchette, clavier, et plus tard caméra (L7). Une ambiguïté fige la
suite : les codes suivants attendent dans `attente`, rien n'est ajouté tant que le choix n'est pas fait (D5).
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from biblyo.core.errors import BusinessError
from biblyo.core.i18n import _
from biblyo.modules.catalogue.models import Copy, Work
from biblyo.modules.catalogue.repository import CardView
from biblyo.modules.circulation import repository as repo
from biblyo.modules.circulation import rules, service
from biblyo.modules.circulation.rules import Candidate, Verdict
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest, LendResult, LoanRefused
from biblyo.modules.holdings import repository as holdings
from biblyo.modules.patrons.models import Patron
from biblyo.modules.patrons.repository import Row, Visibility

MAX_LINES = 50
MAX_WAITING = 50
_READER_KINDS = {rules.KIND_CARD, rules.KIND_PATRON, rules.KIND_NAME}
_COPY_KINDS = {rules.KIND_COPY, rules.KIND_ISBN}


def _integer(text: object) -> int | None:
    value = text if isinstance(text, str) else ""
    return int(value) if value.isdecimal() and len(value) < 10 else None


def _text(form: FormData, name: str, limit: int = 200) -> str:
    value = form.get(name)
    return value.strip()[:limit] if isinstance(value, str) else ""


@dataclass
class CounterState:
    """Ce qui est en cours. Rien de tout cela n'est cru sur parole : chaque valeur est réévaluée au serveur."""

    patron_id: int | None = None
    mode: str | None = None
    guardian_id: int | None = None
    household_id: int | None = None
    copy_ids: list[int] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    doubt: str | None = None
    book_id: int | None = None  # le livre d'où l'on vient : pour y revenir, jamais pour valider

    @classmethod
    def read(cls, form: FormData) -> CounterState:
        state = cls(
            patron_id=_integer(form.get("lecteur")),
            guardian_id=_integer(form.get("responsable")),
            book_id=_integer(form.get("livre")),
            waiting=[item[:64] for item in form.getlist("attente") if isinstance(item, str) and item][:MAX_WAITING],
            doubt=_text(form, "doute", 64) or None,
        )
        ids = [_integer(item) for item in form.getlist("ex")]
        state.copy_ids = list(dict.fromkeys(item for item in ids if item is not None))[:MAX_LINES]
        frame = _text(form, "cadre", 32)
        if frame.startswith("structure:"):
            state.mode, state.household_id = rules.MODE_STRUCTURE, _integer(frame.split(":", 1)[1])
        elif frame in rules.BORROW_MODES:
            state.mode = frame
        return state


@dataclass(frozen=True)
class Choice:
    value: str
    kind_label: str
    title: str
    detail: str


@dataclass(frozen=True)
class Line:
    copy_id: int
    title: str
    barcode: str
    call_number: str | None
    due_on: str | None
    notes: tuple[Verdict, ...]
    chosen_due: str = ""
    computed_due: str = ""  # celle de la règle : la recevoir telle quelle n'est pas un choix
    card: object | None = None  # catalogue.repository.CardView : couverture, auteurs, série et tome
    edition: str = ""


@dataclass
class View:
    state: CounterState
    can_lend: bool
    can_renew: bool
    can_override: bool
    reader: Row | None = None
    adults: tuple[repo.Person, ...] = ()
    preselected: int | None = None
    alone_by: repo.Person | None = None
    structures: tuple[tuple[int, str], ...] = ()
    lines: tuple[Line, ...] = ()
    choices: tuple[Choice, ...] = ()
    notice: Verdict | None = None
    general: tuple[Verdict, ...] = ()
    to_justify: tuple[Verdict, ...] = ()
    loans: tuple[repo.CounterLoan, ...] = ()
    active_count: int = 0
    limit: int | None = None
    overdue: int = 0
    result: LendResult | None = None
    reasons: dict[str, str] = field(default_factory=dict)
    loaned_on: str = ""
    reason_code: str = ""
    reason_text: str = ""
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))  # une validation, une seule fois
    book: tuple[int, str] | None = None  # retour au livre d'où l'on vient
    waiting_reason: str = ""  # pourquoi « Confirmer le prêt » attend encore
    readers: ReaderPage | None = None  # sans lecteur choisi : la liste, d'emblée
    books: BookPage | None = None  # panier vide, ou « Ajouter un livre » : le catalogue, d'emblée
    browsing: bool = False
    lent: tuple[LentLine, ...] = ()
    lent_reader: Row | None = None
    proposal: Proposal | None = None
    fresh: bool = False  # « Nouveau prêt » : la préparation gardée dans l'onglet est oubliée
    confirm_return: int | None = None  # « Retour » cliqué sur un prêt en cours : confirmer, ou annuler
    renewed: int | None = None  # le prêt tout juste renouvelé, confirmé par le serveur (U15)
    membership_expired: bool = False  # avertissement, jamais un blocage (R19, U17)

    @property
    def summary(self) -> str:
        """« 2 livres · Paul Martin » : ce que la validation enregistrera, juste à côté du bouton, avec ses mots."""
        if not self.reader or not self.lines:
            return ""
        count = len(self.lines)
        label = _("livre") if count == 1 else _("livres")
        return f"{count} {label} · {self.reader.name}"

    @property
    def confirm_label(self) -> str:
        """« Confirmer le prêt de 2 livres » : le bouton dit ce qu'il enregistre."""
        count = len(self.lines)
        if count == 1:
            return _("Confirmer le prêt du livre")
        if count > 1:
            return _("Confirmer le prêt de {count} livres").format(count=count)
        return _("Confirmer le prêt")

    @property
    def responsible_text(self) -> str:
        """Qui répond du prêt, dit en clair : le bénéficiaire, l'adulte responsable et la structure ne se confondent
        jamais."""
        state = self.state
        if state.mode == rules.MODE_WITH_ADULT and state.guardian_id is not None:
            adult = next((item for item in self.adults if item.id == state.guardian_id), None)
            return _("Répond du prêt : {name}").format(name=adult.name) if adult else ""
        if state.mode == rules.MODE_ALONE and self.alone_by is not None:
            return _("Emprunte seul, autorisé par {name}").format(name=self.alone_by.name)
        if state.mode == rules.MODE_STRUCTURE:
            name = next((label for key, label in self.structures if key == state.household_id), "")
            return _("Emprunte pour la structure {name}").format(name=name) if name else ""
        return ""

    @property
    def ready(self) -> bool:
        if not (self.can_lend and self.reader and self.lines and self.state.mode):
            return False
        blocked = [item for item in self.general if item.nature is rules.Nature.FORBIDDEN]
        blocked += [i for line in self.lines for i in line.notes if i.nature is rules.Nature.FORBIDDEN]
        return not blocked

    @property
    def is_minor(self) -> bool:
        return self.reader is not None and self.reader.category == "minor"


# --- Un code entre (D5) -----------------------------------------------------------------------------------------------
@dataclass
class Scanner:
    session: Session
    visibility: Visibility
    state: CounterState
    notice: Verdict | None = None

    def _take_reader(self, patron_id: int) -> None:
        if self.state.patron_id != patron_id:
            self.state.patron_id, self.state.mode, self.state.guardian_id, self.state.household_id = (
                patron_id,
                None,
                None,
                None,
            )

    def _take_copy(self, copy_id: int) -> None:
        if copy_id in self.state.copy_ids:
            self.notice = rules.Verdict("D6", rules.Nature.INFO, _("Cet exemplaire est déjà dans la liste."))
            return
        copy = self.session.get(Copy, copy_id)
        if copy is None:
            self.notice = rules.verdict("R4")
            return
        stopped = rules.forbidden(rules.copy_verdicts(repo.copy_facts(self.session, copy)))
        if stopped:
            self.notice = stopped[0]  # signalé à l'ajout, et n'entre pas dans la liste (D6)
            return
        if len(self.state.copy_ids) >= MAX_LINES:
            self.notice = rules.Verdict("D6", rules.Nature.INFO, _("La liste est pleine : validez d’abord ce prêt."))
            return
        self.state.copy_ids.append(copy_id)

    def apply(self, candidate: Candidate) -> None:
        if candidate.kind == rules.KIND_ENDED_CARD:
            self.notice = rules.verdict("R11")
        elif candidate.kind in _READER_KINDS:
            self._take_reader(candidate.target_id)
        elif candidate.kind in _COPY_KINDS:
            self._take_copy(candidate.target_id)

    def scan(self, code: str) -> None:
        """Le point d'entrée unique d'un code. Pendant un choix en attente, le code est mis de côté, dans l'ordre."""
        if self.state.doubt is not None:
            if len(self.state.waiting) < MAX_WAITING:
                self.state.waiting.append(code)
            return
        outcome = rules.resolve(repo.candidates(self.session, code, self.visibility))
        if outcome.picked is not None:
            self.apply(outcome.picked)
        elif outcome.verdict is not None and outcome.verdict.code == "R10":
            self.state.doubt = code
            self.notice = outcome.verdict
        else:
            self.notice = outcome.verdict

    def choose(self, value: str) -> None:
        """Le choix doit être l'un de ceux que ce code permet : le formulaire ne désigne jamais autre chose."""
        code, self.state.doubt = self.state.doubt, None
        if code is None or value == "aucun":
            return
        allowed = rules.resolve(repo.candidates(self.session, code, self.visibility)).choices
        chosen = next((item for item in allowed if f"{item.kind}:{item.target_id}" == value), None)
        if chosen is not None:
            self.apply(chosen)

    def drain(self) -> None:
        while self.state.waiting and self.state.doubt is None:
            self.scan(self.state.waiting.pop(0))


# --- Ce que voit la personne au comptoir (D11, P2) --------------------------------------------------------------------
def _choices(session: Session, code: str, visibility: Visibility) -> tuple[Choice, ...]:
    found = rules.resolve(repo.candidates(session, code, visibility)).choices
    copies = holdings.briefs(session, [item.target_id for item in found if item.kind in _COPY_KINDS])
    out: list[Choice] = []
    for item in found:
        label, detail = "", ""
        if item.kind in _COPY_KINDS and item.target_id in copies:
            brief = copies[item.target_id]
            label, detail = brief.title, brief.barcode
        elif item.kind == rules.KIND_ENDED_CARD:
            label = _("Carte hors service")  # sans titulaire : elle ne mène plus à personne (R11)
        elif item.kind in _READER_KINDS and (row := repo.reader_row(session, item.target_id, visibility)):
            label, detail = (
                row.name,
                " · ".join(part for part in (row.number, row.marks.card or "", _year(row)) if part),
            )
        out.append(Choice(f"{item.kind}:{item.target_id}", rules.CANDIDATE_KINDS[item.kind], label, detail))
    return tuple(out)


def _year(row: Row) -> str:
    return str(row.marks.birth_year) if row.marks.birth_year else ""


def _load_reader(session: Session, view: View, visibility: Visibility) -> None:
    state = view.state
    if state.patron_id is None:
        return
    row = repo.reader_row(session, state.patron_id, visibility)
    if row is None:  # fiche retirée entre-temps, ou identifiant inventé : personne n'est sélectionné
        state.patron_id, state.mode, state.guardian_id, state.household_id = None, None, None, None
        return
    view.reader = row
    patron = session.get(Patron, row.id)
    assert patron is not None
    if patron.category == "minor":
        options = rules.minor_options(repo.borrower_facts(session, patron, service.today()).guardians)
        persons = repo.people(session, [*options.adults, *([options.alone_by] if options.alone_by else [])])
        view.adults = tuple(persons[item] for item in options.adults if item in persons)
        view.preselected = options.preselected
        view.alone_by = persons.get(options.alone_by) if options.alone_by else None
        if state.mode not in (rules.MODE_WITH_ADULT, rules.MODE_ALONE):
            state.mode = None  # jamais déduit : la personne au comptoir dit « avec un adulte » ou « seul » (D1)
        if state.mode == rules.MODE_WITH_ADULT and state.guardian_id is None:
            state.guardian_id = options.preselected  # seul adulte possible : affiché, et confirmé par la validation
    else:
        view.structures = tuple(repo.allowed_structures(session, patron.id))
        if state.mode not in (rules.MODE_SELF, rules.MODE_STRUCTURE):
            state.mode = rules.MODE_SELF
    view.loans = tuple(repo.counter_loans(session, patron.id, service.today()))
    expires = patron.membership_expires_on
    view.membership_expired = bool(expires and expires < service.today().isoformat())
    household = state.household_id if state.mode == rules.MODE_STRUCTURE else None
    view.active_count = repo.active_count(session, patron.id, household)
    view.overdue = repo.overdue_count(session, patron.id, service.today())
    category = rules.rule_category(patron.category, state.mode or rules.MODE_SELF)
    view.limit = rules.pick_rule(repo.loan_rules(session), None, category).max_active_loans


def _preview(session: Session, view: View, due: dict[int, str]) -> tuple[service.Preview | None, Verdict | None]:
    state = view.state
    if view.reader is None or not state.mode or not state.copy_ids:
        return None, None
    if state.mode == rules.MODE_WITH_ADULT and state.guardian_id is None:
        return None, None
    request = LendRequest(
        view.reader.id,
        tuple(LendItem(item, due.get(item)) for item in state.copy_ids),
        state.mode,
        state.guardian_id,
        state.household_id,
        view.loaned_on or None,
    )
    try:
        return service.preview(session, request), None
    except LoanRefused as refused:
        return None, refused.general[0] if refused.general else None
    except BusinessError as refused:
        return None, rules.Verdict("D8", rules.Nature.FORBIDDEN, refused.message)


@dataclass(frozen=True)
class Browse:
    """Ce que la personne regarde dans les sélecteurs : textes cherchés et pages, lus dans le formulaire (POST)."""

    reader_query: str = ""
    reader_page: int = 1
    book_query: str = ""
    book_page: int = 1
    browsing: bool = False
    personal: bool = False

    @classmethod
    def read(cls, form: FormData | None, *, personal: bool) -> Browse:
        if form is None:
            return cls(personal=personal)
        return cls(
            _text(form, "lecteur_q", SEARCH_LENGTH),
            max(1, min(_integer(form.get("lecteur_page")) or 1, 10_000)),
            _text(form, "livre_q", SEARCH_LENGTH),
            max(1, min(_integer(form.get("livre_page")) or 1, 10_000)),
            form.get("parcourir") == "1" and not form.get("fermer_catalogue"),
            personal,
        )


def build(
    session: Session,
    state: CounterState,
    actor: Actor,
    visibility: Visibility,
    *,
    notice: Verdict | None = None,
    due: dict[int, str] | None = None,
    loaned_on: str = "",
    reason_code: str = "",
    reason_text: str = "",
    result: LendResult | None = None,
    browse: Browse | None = None,
    lent_reader_id: int | None = None,
    proposal: Proposal | None = None,
) -> View:
    """Tout ce que le gabarit affichera, calculé ici : ni coordonnée, ni historique, ni détenteur n'y entrent."""
    can_override = bool(actor.permissions & {rules.OVERRIDE, rules.OVERRIDE_LIMIT})
    view = View(
        state,
        can_lend=rules.LEND in actor.permissions,
        can_renew=rules.RENEW in actor.permissions,
        can_override=can_override,
        notice=notice,
        result=result,
        reasons=rules.OVERRIDE_REASONS,
        loaned_on=loaned_on if rules.OVERRIDE in actor.permissions else "",
        reason_code=reason_code,
        reason_text=reason_text,
    )
    chosen = due or {}
    _load_reader(session, view, visibility)
    if state.doubt:
        view.choices = _choices(session, state.doubt, visibility)
    computed, problem = _preview(session, view, chosen)
    if problem is not None:
        view.general = (problem,)
    briefs = holdings.briefs(session, state.copy_ids)
    cards = _cards(session, sorted({brief.work_id for brief in briefs.values()}))
    editions = _editions(session, state.copy_ids)
    lines: list[Line] = []
    for copy_id in state.copy_ids:
        brief = briefs.get(copy_id)
        if brief is None:
            continue
        copy = session.get(Copy, copy_id)
        assert copy is not None
        found = computed.by_copy.get(copy_id, []) if computed else rules.copy_verdicts(repo.copy_facts(session, copy))
        due_on = computed.due.get(copy_id) if computed else None
        rule_due = computed.rule_due.get(copy_id, "") if computed else ""
        line = Line(copy_id, brief.title, brief.barcode, brief.call_number, due_on, tuple(found))
        lines.append(
            replace(
                line,
                chosen_due=chosen.get(copy_id, ""),
                computed_due=rule_due,
                card=cards.get(brief.work_id),
                edition=editions.get(copy_id, ""),
            )
        )
    view.lines = tuple(lines)
    if computed is not None:
        view.general = computed.general
        every = list(computed.general) + [item for line in lines for item in line.notes]
        view.to_justify = tuple(rules.overridable(every))
    if state.book_id is not None and (work := session.get(Work, state.book_id)) is not None:
        view.book = (work.id, work.title)
    view.waiting_reason = _waiting_reason(view)
    browse = browse or Browse()
    if result is not None:
        view.lent = lent_lines(session, result)
        if lent_reader_id is not None:
            view.lent_reader = repo.reader_row(session, lent_reader_id, visibility)
    view.proposal = proposal if view.reader is None else None
    if result is None and view.reader is None and view.can_lend:
        view.readers = reader_page(
            session, browse.reader_query, browse.reader_page, personal=browse.personal, visibility=visibility
        )
    view.browsing = browse.browsing or (not state.copy_ids and result is None)
    if view.browsing and view.can_lend and result is None:
        view.books = book_page(session, browse.book_query, browse.book_page, state.copy_ids)
    return view


def _cards(session: Session, work_ids: list[int]) -> dict[int, object]:
    from biblyo.modules.catalogue.repository import load_cards

    return {card.id: card for card in load_cards(session, work_ids)}


def _editions(session: Session, copy_ids: list[int]) -> dict[int, str]:
    """« Casterman, 1976 » : de quoi distinguer deux éditions d'un même livre dans la sélection."""
    from sqlalchemy import select

    from biblyo.modules.catalogue.models import Edition, Publisher

    if not copy_ids:
        return {}
    rows = session.execute(
        select(Copy.id, Publisher.name, Edition.publication_year)
        .join(Edition, Edition.id == Copy.edition_id)
        .outerjoin(Publisher, Publisher.id == Edition.publisher_id)
        .where(Copy.id.in_(copy_ids))
    )
    return {copy_id: ", ".join(str(part) for part in (name, year) if part) for copy_id, name, year in rows}


def _waiting_reason(view: View) -> str:
    """Ce qui manque avant de pouvoir valider, dit en clair à côté du bouton (au lieu d'un bouton grisé muet)."""
    state = view.state
    if not view.can_lend or (view.result is not None and not view.lines):
        return ""
    if view.reader is None:
        return _("Choisissez d’abord le lecteur dans la liste, ou scannez sa carte.")
    if view.is_minor and state.mode is None:
        return _("Indiquez comment l’enfant emprunte : avec un adulte, ou seul.")
    if state.mode == rules.MODE_WITH_ADULT and state.guardian_id is None:
        return _("Choisissez l’adulte qui répond de ce prêt.")
    if not view.lines:
        return _("Ajoutez au moins un livre : choisissez-le dans le catalogue, ou scannez-le.")
    if not view.ready:
        return _("Un blocage empêche ce prêt : il est indiqué plus haut.")
    if view.to_justify and not view.can_override:
        return _("Ce prêt demande une dérogation : faites intervenir un bibliothécaire.")
    return ""


def due_dates(form: FormData, state: CounterState) -> dict[int, str]:
    """Les échéances **changées par la personne**. Une date laissée telle que le comptoir l'a calculée ne compte pas :
    si la date de prêt change ensuite, l'échéance se recalcule, au lieu de devenir un choix que personne n'a fait."""
    found: dict[int, str] = {}
    for copy_id in state.copy_ids:
        value = _text(form, f"retour_{copy_id}", 10)
        if value and value != _text(form, f"calcul_{copy_id}", 10):
            found[copy_id] = value
    return found


def request_from(form: FormData, state: CounterState) -> LendRequest:
    if state.patron_id is None or state.mode is None:
        raise BusinessError(_("Indiquez d’abord le lecteur et le cadre de l’emprunt."))
    chosen = due_dates(form, state)
    return LendRequest(
        state.patron_id,
        tuple(LendItem(item, chosen.get(item)) for item in state.copy_ids),
        state.mode,
        state.guardian_id if state.mode == rules.MODE_WITH_ADULT else None,
        state.household_id if state.mode == rules.MODE_STRUCTURE else None,
        _text(form, "date_pret", 10) or None,
        _text(form, "motif", 32) or None,
        _text(form, "motif_texte", 200) or None,
    )


# --- Sélecteurs visuels (UX-Circulation 2.0) ----------------------------------------------------------------------
#: Une page de lecteurs, puis « Lecteurs suivants » : jamais l'annuaire entier dans le navigateur.
READERS_PAGE = 20
BOOKS_PAGE = 12
SEARCH_LENGTH = 100


@dataclass(frozen=True)
class ReaderOption:
    """Un lecteur proposé au comptoir : de quoi le reconnaître et savoir s'il peut emprunter, sans coordonnée."""

    row: Row
    active_loans: int
    membership_expired: bool


@dataclass(frozen=True)
class ReaderPage:
    options: tuple[ReaderOption, ...]
    total: int
    page: int
    pages: int
    query: str

    @property
    def has_more(self) -> bool:
        return self.page < self.pages


def reader_page(
    session: Session, query: str, page: int, *, personal: bool, visibility: Visibility, size: int = READERS_PAGE
) -> ReaderPage:
    """Les lecteurs en service, dans l'ordre de l'annuaire, affinés par le texte tapé (nom, numéro, carte). La
    recherche tolère accents et casse ; une coordonnée n'est cherchée qu'avec `patrons.view_personal`, comme à
    l'annuaire. Rien n'est choisi d'office, même s'il n'y a qu'un résultat : la personne clique."""
    from sqlalchemy import func, select

    from biblyo.modules.circulation.models import Loan
    from biblyo.modules.patrons import repository as patrons

    found = patrons.directory(
        session,
        "tous",
        page,
        service.today(),
        query=query[:SEARCH_LENGTH],
        personal=personal,
        size=size,
        visibility=visibility,
    )
    ids = [row.id for row in found.rows]
    loans: dict[int, int] = {}
    expired: set[int] = set()
    if ids:
        loans = dict(
            session.execute(
                select(Loan.patron_id, func.count())
                .where(Loan.patron_id.in_(ids), *repo.ACTIVE)
                .group_by(Loan.patron_id)
            ).all()
        )
        today = service.today().isoformat()
        expired = set(
            session.scalars(
                select(Patron.id).where(
                    Patron.id.in_(ids),
                    Patron.membership_expires_on.is_not(None),
                    Patron.membership_expires_on < today,
                )
            )
        )
    options = tuple(ReaderOption(row, loans.get(row.id, 0), row.id in expired) for row in found.rows)
    return ReaderPage(options, found.total, found.page, found.pages, query)


@dataclass(frozen=True)
class BookOption:
    """Un livre proposé au comptoir : sa carte du catalogue, ses compteurs, et l'exemplaire à ajouter s'il est seul."""

    card: object  # catalogue.repository.CardView
    loanable: tuple[int, ...]  # exemplaires qui peuvent sortir maintenant, hors ceux déjà dans la sélection
    in_cart: int  # exemplaires de ce livre déjà dans la sélection


@dataclass(frozen=True)
class BookPage:
    options: tuple[BookOption, ...]
    total: int
    page: int
    pages: int
    query: str

    @property
    def has_more(self) -> bool:
        return self.page < self.pages


def loanable_copies(session: Session, work_ids: list[int]) -> dict[int, list[int]]:
    """Par livre, les exemplaires qui peuvent sortir maintenant : en place, empruntables, sans prêt en cours."""
    from sqlalchemy import select

    from biblyo.modules.catalogue.models import Edition
    from biblyo.modules.circulation.models import Loan

    found: dict[int, list[int]] = {work_id: [] for work_id in work_ids}
    if not work_ids:
        return found
    busy = select(Loan.copy_id).where(*repo.ACTIVE)
    rows = session.execute(
        select(Edition.work_id, Copy.id)
        .join(Edition, Edition.id == Copy.edition_id)
        .where(
            Edition.work_id.in_(work_ids),
            Copy.status == "available",
            Copy.is_loanable.is_(True),
            Copy.id.not_in(busy),
        )
        .order_by(Copy.id)
    )
    for work_id, copy_id in rows:
        found[work_id].append(copy_id)
    return found


def book_page(session: Session, query: str, page: int, cart: list[int], *, size: int = BOOKS_PAGE) -> BookPage:
    """Le catalogue au comptoir : titre, auteur, série, tome, ISBN, cote, numéro d'inventaire ou code-barres. Seuls
    les livres qui ont un exemplaire sont proposés. Un code n'est jamais nécessaire : la liste s'affiche d'emblée."""
    from biblyo.modules.catalogue import listing

    if not query.strip():
        return _first_books(session, page, cart, size)
    criteria = listing.Criteria.from_params({"q": query[:SEARCH_LENGTH], "exemplaires": "avec", "page": str(page)})
    found = listing.run(session, criteria, page_size=size)
    return _book_options(session, found.cards, cart, found.total, found.page, found.pages, query)


def _first_books(session: Session, page: int, cart: list[int], size: int) -> BookPage:
    """Sans saisie : les livres dans l'ordre du titre, par l'index du titre, en s'arrêtant à la page voulue. Compter
    d'abord les 100 000 notices pour savoir lesquelles ont un exemplaire coûtait 200 ms (mesure de UX5) : on ne
    compte pas, on regarde seulement s'il existe une page suivante."""
    from sqlalchemy import exists, select

    from biblyo.modules.catalogue.models import Edition
    from biblyo.modules.catalogue.repository import load_cards

    has_copy = exists(select(Copy.id).join(Edition, Edition.id == Copy.edition_id).where(Edition.work_id == Work.id))
    ids = list(
        session.scalars(
            select(Work.id)
            .where(Work.status == "active", has_copy)
            .order_by(Work.title_sort, Work.id)
            .limit(size + 1)
            .offset((page - 1) * size)
        )
    )
    more = len(ids) > size
    cards = load_cards(session, ids[:size])
    return _book_options(session, cards, cart, -1, page, page + 1 if more else page, "")


def _book_options(
    session: Session, cards: Sequence[CardView], cart: list[int], total: int, page: int, pages: int, query: str
) -> BookPage:
    found_cards = cards
    ids = [card.id for card in found_cards]
    ready = loanable_copies(session, ids)
    taken = set(cart)
    in_cart: dict[int, int] = {}
    if taken:
        for brief in holdings.briefs(session, list(taken)).values():
            in_cart[brief.work_id] = in_cart.get(brief.work_id, 0) + 1
    options = tuple(
        BookOption(card, tuple(item for item in ready.get(card.id, []) if item not in taken), in_cart.get(card.id, 0))
        for card in found_cards
    )
    return BookPage(options, total, page, pages, query)


@dataclass(frozen=True)
class LentLine:
    """Un exemplaire tout juste prêté : ce que l'écran de confirmation montre, et où mène chaque lien."""

    copy_id: int
    work_id: int
    title: str
    barcode: str
    due_on: str

    @property
    def due_label(self) -> str:
        return f"{self.due_on[8:10]}/{self.due_on[5:7]}/{self.due_on[:4]}"


def lent_lines(session: Session, result: LendResult) -> tuple[LentLine, ...]:
    briefs = holdings.briefs(session, list(result.due_dates))
    return tuple(
        LentLine(copy_id, briefs[copy_id].work_id, briefs[copy_id].title, briefs[copy_id].barcode, due)
        for copy_id, due in result.due_dates.items()
        if copy_id in briefs
    )


@dataclass(frozen=True)
class Proposal:
    """Une préparation laissée en cours dans cet onglet : elle est **proposée**, jamais reprise d'office."""

    reader: Row
    titles: tuple[str, ...]
    copy_ids: tuple[int, ...]
    mode: str
    guardian_id: int | None


def read_proposal(session: Session, form: FormData, visibility: Visibility) -> Proposal | None:
    patron_id = _integer(form.get("propose_lecteur"))
    if patron_id is None:
        return None
    row = repo.reader_row(session, patron_id, visibility)
    if row is None:
        return None
    ids = list(dict.fromkeys(item for item in (_integer(v) for v in form.getlist("propose_ex")) if item))[:MAX_LINES]
    briefs = holdings.briefs(session, ids)
    return Proposal(
        row,
        tuple(briefs[item].title for item in ids if item in briefs),
        tuple(item for item in ids if item in briefs),
        _text(form, "propose_cadre", 32),
        _integer(form.get("propose_responsable")),
    )
