# SPDX-License-Identifier: EUPL-1.2
"""Lectures des prêts. Rien ici n'écrit, et ce qui sort d'ici est déjà limité à ce que la personne peut voir."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from biblyo.core import isbn as isbn_codes
from biblyo.core.i18n import _
from biblyo.core.models import User
from biblyo.core.settings_store import get_setting
from biblyo.modules.catalogue.models import COPY_CODE, Copy, DocumentType, Edition, EditionIdentifier, Work
from biblyo.modules.catalogue.rules import code_of
from biblyo.modules.circulation import rules
from biblyo.modules.circulation.models import Loan, LoanEvent, LoanOperation, LoanReminder, LoanRule
from biblyo.modules.holdings import rules as holdings_rules
from biblyo.modules.holdings.models import Location
from biblyo.modules.patrons import repository as patrons
from biblyo.modules.patrons import rules as patron_rules
from biblyo.modules.patrons.models import Guardianship, Household, HouseholdMember, Patron, PatronCard, PatronHold
from biblyo.modules.patrons.repository import Row, Visibility

ACTIVE = (Loan.returned_at.is_(None), Loan.cancelled_at.is_(None))


def active_loan(session: Session, copy_id: int) -> Loan | None:
    return session.scalar(select(Loan).where(Loan.copy_id == copy_id, *ACTIVE))


def copy_facts(session: Session, copy: Copy) -> rules.CopyFacts:
    loan = active_loan(session, copy.id)
    return rules.CopyFacts(
        status=copy.status,
        is_loanable=copy.is_loanable,
        on_loan=loan is not None,
        due_on=loan.due_on if loan else None,
        condition=copy.condition,
        condition_note=copy.condition_note,
    )


def overdue_count(session: Session, patron_id: int, today: date) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Loan)
            .where(Loan.patron_id == patron_id, Loan.due_on < today.isoformat(), *ACTIVE)
        )
        or 0
    )


def borrower_facts(session: Session, patron: Patron, today: date) -> rules.BorrowerFacts:
    guardians = tuple(patrons.guardian_facts(session, patron.id)) if patron.category == "minor" else ()
    return rules.BorrowerFacts(
        category=patron.category,
        status=patron.status,
        birth=patron.birth,
        membership_expires_on=patron.membership_expires_on,
        overdue=overdue_count(session, patron.id, today),
        guardians=guardians,
    )


def with_responsible(
    session: Session, facts: rules.BorrowerFacts, responsible_id: int | None, today: date
) -> rules.BorrowerFacts:
    if responsible_id is None:
        return facts
    return replace(facts, responsible_overdue=overdue_count(session, responsible_id, today))


def structure_facts(session: Session, household_id: int | None, patron_id: int) -> rules.StructureFacts | None:
    if household_id is None:
        return None
    household = session.get(Household, household_id)
    if household is None:
        return None
    link = session.get(HouseholdMember, (household_id, patron_id))
    return rules.StructureFacts(
        household_id=household_id,
        is_structure=household.kind == "structure",
        archived=household.archived_at is not None,
        can_borrow_for_structure=bool(link and link.can_borrow_for_structure),
    )


def active_count(session: Session, patron_id: int, household_id: int | None) -> int:
    """Ce qui compte pour le plafond : les prêts de la structure, ou ceux de la personne pour elle-même (D2 bis, D3)."""
    scope = Loan.household_id == household_id if household_id is not None else Loan.patron_id == patron_id
    personal = () if household_id is not None else (Loan.borrow_mode != rules.MODE_STRUCTURE,)
    return int(session.scalar(select(func.count()).select_from(Loan).where(scope, *personal, *ACTIVE)) or 0)


def loan_rules(session: Session) -> list[rules.LoanRule]:
    return [
        rules.LoanRule(
            row.document_type_id,
            row.patron_category,
            row.loan_days,
            row.renewal_days,
            row.max_renewals,
            row.max_active_loans,
        )
        for row in session.scalars(select(LoanRule))
    ]


def document_type_of(session: Session, copy: Copy) -> int | None:
    return session.scalar(
        select(Work.document_type_id).join(Edition, Edition.work_id == Work.id).where(Edition.id == copy.edition_id)
    )


def limit_mode(session: Session) -> str:
    return str(get_setting(session, "circulation.limit_mode", rules.LIMIT_WARN))


def overdue_block(session: Session) -> int | None:
    value = get_setting(session, "circulation.overdue_block", None)
    return int(value) if value else None


def closed_weekday(session: Session) -> int | None:
    value = get_setting(session, "circulation.closed_weekday", None)
    return int(value) if value is not None else None


# --- Le comptoir : résoudre un code (D5), ne montrer que ce qui est permis (D11, P2) ----------------------------------
MAX_CANDIDATES = 12
_MIN_NAME_LETTERS = 2


def _copy_candidates(session: Session, code: str) -> list[rules.Candidate]:
    normal = holdings_rules.normalize_code(code) or code.strip().upper()
    found = session.scalars(
        select(Copy.id).where(or_(Copy.barcode == normal, Copy.inventory_number == normal)).limit(MAX_CANDIDATES)
    )
    return [rules.Candidate(rules.KIND_COPY, copy_id) for copy_id in found]


def _isbn_candidates(session: Session, code: str) -> list[rules.Candidate]:
    parsed = isbn_codes.parse_isbn(code)
    if parsed is None:
        return []
    found = session.scalars(
        select(Copy.id)
        .join(EditionIdentifier, EditionIdentifier.edition_id == Copy.edition_id)
        .where(EditionIdentifier.scheme.in_(("isbn13", "ean")), EditionIdentifier.value == parsed.isbn13)
        .limit(MAX_CANDIDATES)
    )
    return [rules.Candidate(rules.KIND_ISBN, copy_id) for copy_id in found]


def copy_candidates(session: Session, code: str) -> list[rules.Candidate]:
    """Pour un retour : seul un exemplaire se rend. Une carte n'a rien à faire ici."""
    return _copy_candidates(session, code) + _isbn_candidates(session, code)


def on_loan_among(session: Session, copy_ids: Sequence[int]) -> list[int]:
    ids = list(copy_ids)
    if not ids:
        return []
    return list(session.scalars(select(Loan.copy_id).where(Loan.copy_id.in_(ids), *ACTIVE)))


def _reader_candidates(session: Session, code: str, visibility: Visibility) -> list[rules.Candidate]:
    """Une fiche retirée n'est ni proposée ni citée : la carte d'un lecteur archivé est un code inconnu ici (C10)."""
    found: list[rules.Candidate] = []
    number = patron_rules.parse_patron_number(code)
    if number:
        people = session.scalars(
            select(Patron.id).where(Patron.number == number, Patron.status.in_(visibility.statuses))
        )
        found += [rules.Candidate(rules.KIND_PATRON, patron_id) for patron_id in people]
    card = patron_rules.normalize_card(code)
    if card:
        rows = session.execute(
            select(PatronCard.patron_id, PatronCard.ended_on)
            .join(Patron, Patron.id == PatronCard.patron_id)
            .where(PatronCard.number == card, Patron.status.in_(visibility.statuses))
        ).all()
        found += [
            rules.Candidate(rules.KIND_ENDED_CARD if ended else rules.KIND_CARD, patron_id)
            for patron_id, ended in rows
            if patron_id is not None
        ]
    return found


def candidates(session: Session, code: str, visibility: Visibility) -> list[rules.Candidate]:
    """Tout ce que ce code peut désigner, dans **tous** les types à la fois. Le nom sert si aucun code n'a parlé."""
    found = _reader_candidates(session, code, visibility)
    found += _copy_candidates(session, code)
    found += _isbn_candidates(session, code)
    if found or sum(char.isalpha() for char in code) < _MIN_NAME_LETTERS:
        return found
    rows = patrons.search(session, code, personal=False, limit=MAX_CANDIDATES, visibility=visibility)
    return [rules.Candidate(rules.KIND_NAME, row.id) for row in rows]


def reader_row(session: Session, patron_id: int, visibility: Visibility) -> Row | None:
    patron = session.get(Patron, patron_id)
    if patron is None or not visibility.allows(patron.status):
        return None
    return patrons.rows_for(session, [patron], visibility)[0]


@dataclass(frozen=True)
class Person:
    id: int
    name: str
    number: str


def people(session: Session, ids: Sequence[int]) -> dict[int, Person]:
    rows = session.scalars(select(Patron).where(Patron.id.in_(list(ids))))
    return {
        row.id: Person(row.id, patron_rules.display_name(row.first_name, row.last_name, row.status), row.number)
        for row in rows
    }


def allowed_structures(session: Session, patron_id: int) -> list[tuple[int, str]]:
    rows = session.execute(
        select(Household.id, Household.name)
        .join(HouseholdMember, HouseholdMember.household_id == Household.id)
        .where(
            HouseholdMember.patron_id == patron_id,
            HouseholdMember.can_borrow_for_structure.is_(True),
            Household.kind == "structure",
            Household.archived_at.is_(None),
        )
        .order_by(Household.name)
    )
    return [(row[0], row[1]) for row in rows]


@dataclass(frozen=True)
class CounterLoan:
    """P2 : ouvrage, exemplaire, échéance, statut. **Aucun autre champ n'existe ici** : rien d'autre ne peut fuiter."""

    id: int
    title: str
    barcode: str
    due_on: str
    late: bool
    due_soon: bool
    version: int

    @property
    def due_label(self) -> str:
        return patron_rules.french_date(self.due_on)


def counter_loans(session: Session, patron_id: int, today: date) -> list[CounterLoan]:
    """Dans l’ordre des prêts : un renouvellement ne déplace jamais une ligne sous le pointeur (U16)."""
    rows = session.execute(
        select(Loan.id, Work.title, COPY_CODE, Loan.due_on, Loan.version)
        .join(Copy, Copy.id == Loan.copy_id)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .where(Loan.patron_id == patron_id, *ACTIVE)
        .order_by(Loan.id)
    ).all()
    return [
        CounterLoan(
            loan_id,
            title,
            barcode,
            due_on,
            rules.is_overdue(date.fromisoformat(due_on), today),
            rules.is_due_soon(date.fromisoformat(due_on), today),
            version,
        )
        for loan_id, title, barcode, due_on, version in rows
    ]


# --- Listes (`circulation.history`) : en cours, retards, échéances proches, historique (CIR-08) ---------------------
LIST_VIEWS: dict[str, str] = {
    "cours": _("En cours"),
    "retards": _("En retard"),
    "proches": _("Échéance proche"),
    "historique": _("Historique"),
}


@dataclass(frozen=True)
class ListRow:
    id: int
    title: str
    barcode: str
    borrower: Person | None  # None : fiche retirée, fermée à ce compte
    responsible: Person | None
    mode: str
    due_on: str
    returned_at: str | None
    cancelled_at: str | None
    late: int
    work_id: int = 0
    version: int = 1
    copy_id: int = 0

    @property
    def active(self) -> bool:
        return self.returned_at is None and self.cancelled_at is None

    @property
    def due_label(self) -> str:
        return patron_rules.french_date(self.due_on)

    @property
    def ended_label(self) -> str:
        return patron_rules.french_date((self.returned_at or self.cancelled_at or "")[:10])

    @property
    def mode_label(self) -> str:
        return rules.BORROW_MODES[self.mode]


@dataclass(frozen=True)
class LoanList:
    rows: list[ListRow]
    total: int


def _view_filter(view: str, today: date) -> tuple[Any, ...]:
    day = today.isoformat()
    if view == "retards":
        return (*ACTIVE, Loan.due_on < day)
    if view == "proches":
        return (*ACTIVE, Loan.due_on >= day, Loan.due_on <= (today + timedelta(days=rules.DUE_SOON_DAYS)).isoformat())
    if view == "historique":
        return ((Loan.returned_at.is_not(None)) | (Loan.cancelled_at.is_not(None)),)
    if view == "rappel":  # en retard ou à échéance proche (D15)
        return (*ACTIVE, Loan.due_on <= (today + timedelta(days=rules.DUE_SOON_DAYS)).isoformat())
    return ACTIVE


def visible_people(session: Session, ids: Sequence[int | None], visibility: Visibility) -> dict[int, Person]:
    """Les personnes que ce compte peut voir. Une fiche retirée sans la permission n'y figure pas (C10)."""
    wanted = sorted({item for item in ids if item})
    rows = session.scalars(select(Patron).where(Patron.id.in_(wanted), Patron.status.in_(visibility.statuses)))
    return {
        row.id: Person(row.id, patron_rules.display_name(row.first_name, row.last_name, row.status), row.number)
        for row in rows
    }


def loan_list(
    session: Session,
    view: str,
    today: date,
    visibility: Visibility,
    *,
    page: int,
    size: int,
    patron_id: int | None = None,
    copies: Any = None,
    query: str = "",
    by_reader: bool = False,
) -> LoanList:
    """Une page de prêts. L'historique se lit du plus récent au plus ancien, par l'ordre d'enregistrement.

    `query` : un titre, un code d'exemplaire, et, seulement avec `by_reader`, un nom de lecteur."""
    conditions = list(_view_filter(view, today))
    if query.strip():
        conditions.append(_matching(query, visibility, by_reader))
    if patron_id is not None:
        conditions.append((Loan.patron_id == patron_id) | (Loan.responsible_id == patron_id))
    if copies is not None:
        conditions.append(Loan.copy_id.in_(copies.scalar_subquery()))
    total = int(session.scalar(select(func.count()).select_from(Loan).where(*conditions)) or 0)
    order = (Loan.id.desc(),) if view == "historique" else (Loan.due_on, Loan.id)
    # D'abord la page, dans la seule table des prêts ; ensuite les titres de ces lignes. Joindre avant de paginer
    # faisait parcourir tous les exemplaires puis trier tout l'historique (350 ms à 210 000 prêts).
    window = select(Loan.id).where(*conditions).order_by(*order).limit(size).offset((page - 1) * size)
    page_ids = list(session.scalars(window))
    rank = {loan_id: index for index, loan_id in enumerate(page_ids)}
    found = sorted(
        session.execute(
            select(Loan, Work.title, COPY_CODE, Work.id)
            .join(Copy, Copy.id == Loan.copy_id)
            .join(Edition, Edition.id == Copy.edition_id)
            .join(Work, Work.id == Edition.work_id)
            .where(Loan.id.in_(page_ids))
        ).all(),
        key=lambda row: rank[row[0].id],
    )
    persons = visible_people(
        session, [i for loan, *_rest in found for i in (loan.patron_id, loan.responsible_id)], visibility
    )
    rows = [
        ListRow(
            loan.id,
            title,
            barcode,
            persons.get(loan.patron_id),
            persons.get(loan.responsible_id) if loan.responsible_id else None,
            loan.borrow_mode,
            loan.due_on,
            loan.returned_at,
            loan.cancelled_at,
            rules.days_overdue(date.fromisoformat(loan.due_on), today)
            if loan.returned_at is None and loan.cancelled_at is None
            else 0,
            work_id,
            loan.version,
            loan.copy_id,
        )
        for loan, title, barcode, work_id in found
    ]
    return LoanList(rows, total)


def _matching(query: str, visibility: Visibility, by_reader: bool) -> Any:
    """Les prêts dont l'ouvrage, le code d'exemplaire ou (avec l'historique) le lecteur répond au texte tapé."""
    from biblyo.core.text import normalize

    text_value = query.strip()[:100]
    words = normalize(text_value).split()
    code = holdings_rules.normalize_code(text_value) or text_value.upper()
    titled = select(Copy.id).join(Edition, Edition.id == Copy.edition_id).join(Work, Work.id == Edition.work_id)
    titled = titled.where(*(Work.title_norm.contains(word, autoescape=True) for word in words)) if words else titled
    coded = select(Copy.id).where((Copy.barcode == code) | (Copy.inventory_number == code))
    found = Loan.copy_id.in_(titled) | Loan.copy_id.in_(coded)
    if by_reader and words:
        named = select(Patron.id).where(
            Patron.status.in_(visibility.statuses),
            *(or_(Patron.name_norm.like(f"{word}%"), Patron.name_norm.like(f"% {word}%")) for word in words),
        )
        found = found | Loan.patron_id.in_(named)
    return found


# --- Carte « Prêts » de la fiche d'un lecteur (P2, D11 bis) ----------------------------------------------------------
@dataclass(frozen=True)
class PatronLoans:
    """Le nombre pour tous ; le détail seulement avec `circulation.history`, et alors `detailed` est vrai."""

    active: int
    overdue: int
    answering: int
    detailed: bool
    current: tuple[ListRow, ...] = ()
    recent: tuple[ListRow, ...] = ()
    patron_id: int = 0
    next_due: str | None = None  # la plus proche échéance de ses prêts : une date, ni titre ni code

    @property
    def next_due_label(self) -> str:
        return patron_rules.french_date(self.next_due)


RECENT_ON_SHEET = 5


def patron_loans(session: Session, patron_id: int, today: date, visibility: Visibility, detailed: bool) -> PatronLoans:
    active = int(
        session.scalar(select(func.count()).select_from(Loan).where(Loan.patron_id == patron_id, *ACTIVE)) or 0
    )
    answering = int(
        session.scalar(select(func.count()).select_from(Loan).where(Loan.responsible_id == patron_id, *ACTIVE)) or 0
    )
    next_due = session.scalar(select(func.min(Loan.due_on)).where(Loan.patron_id == patron_id, *ACTIVE))
    card = PatronLoans(
        active, overdue_count(session, patron_id, today), answering, detailed, patron_id=patron_id, next_due=next_due
    )
    if not detailed:
        return card
    current = loan_list(session, "cours", today, visibility, page=1, size=50, patron_id=patron_id).rows
    recent = loan_list(session, "historique", today, visibility, page=1, size=RECENT_ON_SHEET, patron_id=patron_id).rows
    return replace(card, current=tuple(current), recent=tuple(recent))


# --- Tableau de bord (CIR-08) : des nombres pour tous, les lecteurs avec `circulation.history` --------------------
@dataclass(frozen=True)
class LoanStats:
    active: int
    overdue: int
    due_soon: int
    late: tuple[ListRow, ...] = ()


LATE_ON_DASHBOARD = 5


def loan_stats(session: Session, today: date, visibility: Visibility | None) -> LoanStats:
    def count(view: str) -> int:
        return int(session.scalar(select(func.count()).select_from(Loan).where(*_view_filter(view, today))) or 0)

    late: tuple[ListRow, ...] = ()
    if visibility is not None:
        late = tuple(loan_list(session, "retards", today, visibility, page=1, size=LATE_ON_DASHBOARD).rows)
    return LoanStats(count("cours"), count("retards"), count("proches"), late)


# --- À rappeler (D15) : qui joindre, sans coordonnée ; des rappels en valeurs structurées --------------------------
@dataclass(frozen=True)
class ReminderLine:
    day: str
    channel: str
    result: str
    who: str | None

    @property
    def day_label(self) -> str:
        return patron_rules.french_date(self.day)

    @property
    def channel_label(self) -> str:
        return rules.REMINDER_CHANNELS[self.channel]

    @property
    def result_label(self) -> str:
        return rules.REMINDER_RESULTS[self.result]


def reminders_of(session: Session, loan_ids: Sequence[int]) -> dict[int, list[ReminderLine]]:
    """Les rappels notés, du plus récent au plus ancien."""
    found: dict[int, list[ReminderLine]] = {}
    ids = list(loan_ids)
    if not ids:
        return found
    rows = session.execute(
        select(LoanReminder, User.display_name)
        .join(User, User.id == LoanReminder.user_id, isouter=True)
        .where(LoanReminder.loan_id.in_(ids))
        .order_by(LoanReminder.reminded_on.desc(), LoanReminder.id.desc())
    )
    for reminder, who in rows:
        line = ReminderLine(reminder.reminded_on, reminder.channel, reminder.result, who)
        found.setdefault(reminder.loan_id, []).append(line)
    return found


@dataclass(frozen=True)
class RemindRow:
    loan: ListRow
    minor: bool
    callees: tuple[Person, ...]
    last: ReminderLine | None
    count: int


LIVING = ("active", "suspended")


def _callees(
    session: Session, rows: Sequence[ListRow], visibility: Visibility
) -> tuple[set[int], dict[int, tuple[Person, ...]]]:
    """Qui joindre : l'emprunteur adulte ; un mineur, par ses responsables **autorisés à être contactés**."""
    ids = [row.borrower.id for row in rows if row.borrower]
    minors = set(session.scalars(select(Patron.id).where(Patron.id.in_(ids), Patron.category == "minor")))
    guardians: dict[int, list[Person]] = {}
    statuses = [status for status in LIVING if visibility.allows(status)]
    found = session.execute(
        select(Guardianship.minor_id, Patron)
        .join(Patron, Patron.id == Guardianship.guardian_id)
        .where(
            Guardianship.minor_id.in_(sorted(minors)), Guardianship.is_contact.is_(True), Patron.status.in_(statuses)
        )
        .order_by(Guardianship.contact_rank, Guardianship.created_at)
    )
    for minor_id, other in found:
        name = patron_rules.display_name(other.first_name, other.last_name, other.status)
        guardians.setdefault(minor_id, []).append(Person(other.id, name, other.number))
    callees = {
        row.id: tuple(guardians.get(row.borrower.id, [])) if row.borrower.id in minors else (row.borrower,)
        for row in rows
        if row.borrower
    }
    return minors, callees


def to_remind(
    session: Session, today: date, visibility: Visibility, *, page: int, size: int
) -> tuple[list[RemindRow], int]:
    found = loan_list(session, "rappel", today, visibility, page=page, size=size)
    minors, callees = _callees(session, found.rows, visibility)
    noted = reminders_of(session, [row.id for row in found.rows])
    rows = [
        RemindRow(
            row,
            bool(row.borrower and row.borrower.id in minors),
            callees.get(row.id, ()),
            noted[row.id][0] if row.id in noted else None,
            len(noted.get(row.id, [])),
        )
        for row in found.rows
    ]
    return rows, found.total


# --- Fiche d'un prêt (`circulation.history`) --------------------------------------------------------------------------
@dataclass(frozen=True)
class EventLine:
    kind: str
    at: str
    who: str | None
    reason: str | None

    @property
    def day(self) -> str:
        return patron_rules.french_date(self.at[:10])

    @property
    def reason_label(self) -> str | None:
        return rules.reason_label(self.reason)


@dataclass(frozen=True)
class LoanSheet:
    loan: Loan
    title: str
    barcode: str
    borrower: Person
    responsible: Person | None
    household: str | None
    operation_kind: str
    events: tuple[EventLine, ...]
    reminders: tuple[ReminderLine, ...] = ()

    @property
    def mode_label(self) -> str:
        return rules.BORROW_MODES[self.loan.borrow_mode]

    @property
    def active(self) -> bool:
        return self.loan.returned_at is None and self.loan.cancelled_at is None

    @property
    def status_label(self) -> str:
        if self.loan.cancelled_at:
            return rules.LOAN_STATES["cancelled"]
        return rules.LOAN_STATES["returned" if self.loan.returned_at else "active"]

    @property
    def due_label(self) -> str:
        return patron_rules.french_date(self.loan.due_on)


def loan_sheet(session: Session, loan_id: int) -> LoanSheet | None:
    loan = session.get(Loan, loan_id)
    if loan is None:
        return None
    title, barcode = session.execute(
        select(Work.title, COPY_CODE)
        .join(Edition, Edition.work_id == Work.id)
        .join(Copy, Copy.edition_id == Edition.id)
        .where(Copy.id == loan.copy_id)
    ).one()
    found = people(session, [item for item in (loan.patron_id, loan.responsible_id) if item])
    household = (
        session.scalar(select(Household.name).where(Household.id == loan.household_id)) if loan.household_id else None
    )
    operation = session.get(LoanOperation, loan.operation_id)
    rows = session.execute(
        select(LoanEvent, User.display_name)
        .join(User, User.id == LoanEvent.user_id, isouter=True)
        .where(LoanEvent.loan_id == loan.id)
        .order_by(LoanEvent.id)
    ).all()
    events = tuple(EventLine(event.kind, event.at, who, event.reason_code) for event, who in rows)
    return LoanSheet(
        loan,
        title,
        barcode,
        found[loan.patron_id],
        found.get(loan.responsible_id) if loan.responsible_id else None,
        household,
        operation.kind if operation else "lend",
        events,
        tuple(reminders_of(session, [loan.id]).get(loan.id, [])),
    )


# --- Conservation (D13) : une simulation, sans aucune écriture ----------------------------------------------------
@dataclass(frozen=True)
class RetentionPreview:
    months: int
    cutoff: str
    concerned: int
    kept_lost_copy: int
    kept_hold: int

    @property
    def cutoff_label(self) -> str:
        return patron_rules.french_date(self.cutoff)


def _months_before(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 - months
    year, month = index // 12, index % 12 + 1
    return date(year, month, min(day.day, 28))


def retention_preview(session: Session, today: date) -> RetentionPreview:
    """Ce que toucherait une conservation de N mois. Simple hypothèse : **rien n'est effacé, ni ici ni ailleurs**."""
    months = int(get_setting(session, "circulation.retention_months", 12))
    cutoff = _months_before(today, months).isoformat()
    ended = func.coalesce(Loan.returned_at, Loan.cancelled_at)
    over = (ended.is_not(None), ended < cutoff)
    lost = exists().where(Copy.id == Loan.copy_id, Copy.status == "lost")
    hold = exists().where(
        PatronHold.patron_id == Loan.patron_id,
        PatronHold.lifted_at.is_(None),
        (PatronHold.until.is_(None)) | (PatronHold.until >= today.isoformat()),
    )
    count = func.count()
    concerned = session.scalar(select(count).select_from(Loan).where(*over, ~lost, ~hold)) or 0
    kept_lost = session.scalar(select(count).select_from(Loan).where(*over, lost)) or 0
    kept_hold = session.scalar(select(count).select_from(Loan).where(*over, ~lost, hold)) or 0
    return RetentionPreview(months, cutoff, int(concerned), int(kept_lost), int(kept_hold))


# --- Réglages (`circulation.settings`) ------------------------------------------------------------------------------
BASE_SCOPES = ("adult", "minor", "structure")


@dataclass(frozen=True)
class RuleView:
    id: int
    document_type_id: int | None
    type_label: str | None
    patron_category: str | None
    loan_days: int
    renewal_days: int
    max_renewals: int
    max_active_loans: int | None

    @property
    def scope_label(self) -> str:
        category = rules.RULE_CATEGORIES.get(self.patron_category or "", _("toutes catégories"))
        return f"{self.type_label} · {category}" if self.type_label else category

    @property
    def removable(self) -> bool:
        """Les trois règles de départ restent : sans elles, aucune durée ne s'appliquerait."""
        return not (self.document_type_id is None and self.patron_category in BASE_SCOPES)


def rule_views(session: Session) -> list[RuleView]:
    rows = session.execute(
        select(LoanRule, DocumentType.label)
        .join(DocumentType, DocumentType.id == LoanRule.document_type_id, isouter=True)
        .order_by(LoanRule.document_type_id.is_not(None), DocumentType.label, LoanRule.patron_category, LoanRule.id)
    ).all()
    return [
        RuleView(
            rule.id,
            rule.document_type_id,
            label,
            rule.patron_category,
            rule.loan_days,
            rule.renewal_days,
            rule.max_renewals,
            rule.max_active_loans,
        )
        for rule, label in rows
    ]


@dataclass(frozen=True)
class SettingsValues:
    limit_mode: str
    overdue_block: int | None
    closed_weekday: str
    retention_months: int


def settings_values(session: Session) -> SettingsValues:
    weekday = closed_weekday(session)
    return SettingsValues(
        limit_mode(session),
        overdue_block(session),
        "" if weekday is None else str(weekday),
        int(get_setting(session, "circulation.retention_months", 12)),
    )


# --- Export d'un lecteur (D13 de L3) : ses prêts, et ceux dont il répond ---------------------------------------
def patron_export(session: Session, patron_id: int) -> dict[str, Any]:
    def rows(condition: Any) -> list[tuple[Loan, str, str]]:
        return [
            (loan, title, barcode)
            for loan, title, barcode in session.execute(
                select(Loan, Work.title, COPY_CODE)
                .join(Copy, Copy.id == Loan.copy_id)
                .join(Edition, Edition.id == Copy.edition_id)
                .join(Work, Work.id == Edition.work_id)
                .where(condition)
                .order_by(Loan.id)
            )
        ]

    own = rows(Loan.patron_id == patron_id)
    answering = rows(Loan.responsible_id == patron_id)
    persons = people(session, [loan.responsible_id for loan, _title, _code in own if loan.responsible_id])

    def common(loan: Loan, title: str, barcode: str) -> dict[str, Any]:
        return {
            "ouvrage": title,
            "exemplaire": barcode,
            "prete_le": loan.loaned_at[:10],
            "echeance": loan.due_on,
            "rendu_le": loan.returned_at[:10] if loan.returned_at else None,
            "annule": loan.cancelled_at is not None,
        }

    return {
        "prets": [
            {
                **common(loan, title, barcode),
                "cadre": rules.BORROW_MODES[loan.borrow_mode],
                "responsable": (
                    {"numero": persons[loan.responsible_id].number, "nom": persons[loan.responsible_id].name}
                    if loan.responsible_id in persons
                    else None
                ),
            }
            for loan, title, barcode in own
        ],
        # Ceux dont la personne a répondu : l'ouvrage et les dates, sans l'identité de l'enfant
        "prets_dont_il_repond": [common(loan, title, barcode) for loan, title, barcode in answering],
    }


# --- Exemplaires d'un livre, vus depuis sa fiche --------------------------------------------------------------------
@dataclass(frozen=True)
class BookCopy:
    id: int
    barcode: str
    call_number: str | None
    location: str | None
    position: int | None
    reason: rules.Verdict | None  # None : peut sortir maintenant
    caution: rules.Verdict | None  # mention d'état, à vérifier avant la sortie (R22)
    on_loan: bool
    loan_id: int | None  # seulement avec `circulation.history` : le lien vers la fiche du prêt
    state: holdings_rules.CopyState | None = None
    edition: str = ""
    location_id: int | None = None
    active_loan: int | None = None  # le prêt à rendre : vise ce prêt-là, jamais un prêt arrivé entre-temps


def book_copies(session: Session, work_id: int, *, show_loans: bool) -> tuple[str, list[BookCopy]] | None:
    """Les exemplaires d'une notice et ce que dit pour chacun la règle du comptoir, sans jamais nommer un emprunteur.

    Trois requêtes quel que soit le nombre d'exemplaires : exemplaires, prêts en cours, états."""
    from biblyo.modules.catalogue.models import Publisher
    from biblyo.modules.holdings import availability
    from biblyo.modules.holdings import repository as holdings_repo

    work = session.get(Work, work_id)
    if work is None:
        return None
    rows = session.execute(
        select(Copy, Location.path, Publisher.name, Edition.publication_year)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Location, Location.id == Copy.location_id, isouter=True)
        .join(Publisher, Publisher.id == Edition.publisher_id, isouter=True)
        .where(Edition.work_id == work_id)
        .order_by(Copy.id)
    ).all()
    ids = [row[0].id for row in rows]
    out = holdings_repo.on_loan(session, ids)
    states = availability.for_copies(session, ids)
    found: list[BookCopy] = []
    for copy, path, publisher, year in rows:
        loan = out.get(copy.id)
        facts = rules.CopyFacts(
            copy.status,
            copy.is_loanable,
            loan is not None,
            loan.due_on if loan else None,
            copy.condition,
            copy.condition_note,
        )
        verdicts = rules.copy_verdicts(facts)
        blocking = rules.forbidden(verdicts)
        cautions = rules.warnings(verdicts)
        found.append(
            BookCopy(
                copy.id,
                code_of(copy.barcode, copy.inventory_number),
                copy.call_number,
                path,
                copy.shelf_position,
                blocking[0] if blocking else None,
                cautions[0] if cautions else None,
                loan is not None,
                loan.loan_id if loan is not None and show_loans else None,
                states.get(copy.id),
                ", ".join(str(part) for part in (publisher, year) if part),
                copy.location_id,
                loan.loan_id if loan is not None else None,
            )
        )
    return work.title, found


# --- Onglet « Prêts » de la fiche d'un livre (D11 bis) -------------------------------------------------------------
@dataclass(frozen=True)
class BookLoans:
    """Exemplaires sortis et échéances pour tous ; emprunteurs et historique avec `circulation.history`."""

    work_id: int
    current: tuple[ListRow, ...]
    finished: tuple[ListRow, ...]
    total_finished: int
    detailed: bool


RECENT_ON_BOOK = 10


def book_loans(session: Session, work_id: int, today: date, visibility: Visibility, detailed: bool) -> BookLoans:
    copies = select(Copy.id).join(Edition, Edition.id == Copy.edition_id).where(Edition.work_id == work_id)
    current = loan_list(session, "cours", today, visibility, page=1, size=100, copies=copies).rows
    if not detailed:
        current = [replace(row, borrower=None, responsible=None) for row in current]
        return BookLoans(work_id, tuple(current), (), 0, False)
    finished = loan_list(session, "historique", today, visibility, page=1, size=RECENT_ON_BOOK, copies=copies)
    return BookLoans(work_id, tuple(current), tuple(finished.rows), finished.total, True)
