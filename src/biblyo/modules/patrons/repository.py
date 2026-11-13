# SPDX-License-Identifier: EUPL-1.2
"""Lectures des lecteurs et des foyers. **Les coordonnées ne se lisent que par `contacts_of`**, appelée seulement
quand la permission `patrons.view_personal` est établie : aucune autre vue ne les transporte."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import ColumnElement, Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from biblyo.core.models import AuditEntry, User
from biblyo.core.phones import format_phone
from biblyo.core.text import normalize
from biblyo.modules.patrons import rules
from biblyo.modules.patrons.models import (
    Contact,
    Guardianship,
    Household,
    HouseholdMember,
    Patron,
    PatronCard,
    PatronHold,
)
from biblyo.modules.patrons.rules import GuardianFacts, Marks

MAX_RESULTS = 20
LIVING = ("active", "suspended")
GONE = ("archived", "anonymized")


@dataclass(frozen=True)
class Visibility:
    """Les fiches retirées de l'annuaire qu'un compte peut voir, partout : listes, sélecteurs, liens, recherches."""

    archived: bool = False
    anonymized: bool = False

    @property
    def statuses(self) -> tuple[str, ...]:
        return (*LIVING, *(("archived",) if self.archived else ()), *(("anonymized",) if self.anonymized else ()))

    def allows(self, status: str) -> bool:
        return status in self.statuses

    @property
    def named(self) -> tuple[str, ...]:
        """Les statuts dont la fiche porte encore un nom."""
        return tuple(status for status in self.statuses if status != "anonymized")


EVERYTHING = Visibility(archived=True, anonymized=True)


# --- Repères de désambiguïsation (D8) ---------------------------------------------------------------------------------
def active_cards(session: Session, patron_ids: Sequence[int]) -> dict[int, str]:
    rows = session.execute(
        select(PatronCard.patron_id, PatronCard.number).where(
            PatronCard.patron_id.in_(list(patron_ids)), PatronCard.ended_on.is_(None)
        )
    )
    return {patron_id: number for patron_id, number in rows if patron_id is not None}


def _first_households(session: Session, patron_ids: Sequence[int]) -> dict[int, str]:
    rows = session.execute(
        select(HouseholdMember.patron_id, Household.name)
        .join(Household, Household.id == HouseholdMember.household_id)
        .where(HouseholdMember.patron_id.in_(list(patron_ids)), Household.archived_at.is_(None))
        .order_by(HouseholdMember.joined_on, Household.id)
    )
    found: dict[int, str] = {}
    for patron_id, name in rows:
        found.setdefault(patron_id, name)
    return found


def _cities(session: Session, patron_ids: Sequence[int]) -> dict[int, str]:
    """Commune de l'adresse propre, sinon de l'un des foyers. **Seulement la commune** : jamais la rue."""
    found: dict[int, str] = {}
    own = session.execute(
        select(Contact.patron_id, Contact.city)
        .where(Contact.patron_id.in_(list(patron_ids)), Contact.kind == "address")
        .order_by(Contact.is_primary.desc(), Contact.id)
    )
    for patron_id, city in own:
        if patron_id is not None and city:
            found.setdefault(patron_id, city)
    missing = [item for item in patron_ids if item not in found]
    if missing:
        shared = session.execute(
            select(HouseholdMember.patron_id, Contact.city)
            .join(Contact, Contact.household_id == HouseholdMember.household_id)
            .where(HouseholdMember.patron_id.in_(missing), Contact.kind == "address")
            .order_by(Contact.is_primary.desc(), Contact.id)
        )
        for patron_id, city in shared:
            if city:
                found.setdefault(patron_id, city)
    return found


def _first_guardians(session: Session, patron_ids: Sequence[int]) -> dict[int, str]:
    rows = session.execute(
        select(Guardianship.minor_id, Patron.first_name, Patron.last_name)
        .join(Patron, Patron.id == Guardianship.guardian_id)
        .where(Guardianship.minor_id.in_(list(patron_ids)), Patron.status.in_(LIVING))
        .order_by(Guardianship.contact_rank, Guardianship.created_at)
    )
    found: dict[int, str] = {}
    for minor_id, first, last in rows:
        found.setdefault(minor_id, f"{first} {last}")
    return found


def marks_of(session: Session, patrons: Sequence[Patron]) -> dict[int, Marks]:
    ids = [item.id for item in patrons]
    if not ids:
        return {}
    cards, households, cities = active_cards(session, ids), _first_households(session, ids), _cities(session, ids)
    guardians = _first_guardians(session, [item.id for item in patrons if item.category == "minor"])
    return {
        item.id: Marks(
            item.number,
            cards.get(item.id),
            rules.birth_year(item.birth),
            cities.get(item.id),
            households.get(item.id),
            guardians.get(item.id),
        )
        for item in patrons
    }


# --- Recherche et annuaire --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Row:
    """Un lecteur dans une liste : de quoi le reconnaître, **sans aucune coordonnée**."""

    id: int
    number: str
    first_name: str
    last_name: str
    category: str
    status: str
    marks: Marks
    homonym: bool
    needs_guardian: bool
    card_note: str | None = None  # « carte L-000001 remplacée » quand la recherche l'a trouvée par une ancienne carte

    @property
    def name(self) -> str:
        return rules.display_name(self.first_name, self.last_name, self.status)

    @property
    def marks_text(self) -> str:
        return rules.marks_text(self.marks)

    @property
    def initials(self) -> str:
        return "?" if self.status == "anonymized" else rules.initials(self.first_name, self.last_name)

    @property
    def category_label(self) -> str:
        return rules.CATEGORIES[self.category]

    @property
    def status_label(self) -> str:
        return rules.STATUSES[self.status]


FILTERS = {
    "tous": "",
    "adultes": "adult",
    "mineurs": "minor",
    "archives": "archived",
    "adhesion": "expired",
    "responsable": "needs_guardian",
}


def _needs_guardian(session: Session, minor_ids: Sequence[int]) -> set[int]:
    """Mineurs sans responsable habilité et joignable : « Responsable à renseigner ». Lu en lot, en trois requêtes."""
    facts = _facts(session, minor_ids)
    return {item for item in minor_ids if rules.needs_guardian(facts.get(item, []))}


def _rows(
    session: Session,
    patrons: Sequence[Patron],
    notes: dict[int, str] | None = None,
    visibility: Visibility = EVERYTHING,
) -> list[Row]:
    marks = marks_of(session, patrons)
    norms = {item.name_norm for item in patrons if item.name_norm}
    shared: set[str] = set()
    if norms:
        shared = set(
            session.scalars(
                select(Patron.name_norm)
                .where(Patron.name_norm.in_(norms), Patron.status.in_(visibility.named))
                .group_by(Patron.name_norm)
                .having(func.count() > 1)
            )
        )
    minors = [item.id for item in patrons if item.category == "minor" and item.status in ("active", "suspended")]
    needs = _needs_guardian(session, minors)
    return [
        Row(
            item.id,
            item.number,
            item.first_name,
            item.last_name,
            item.category,
            item.status,
            marks[item.id],
            item.name_norm in shared,
            item.id in needs,
            (notes or {}).get(item.id),
        )
        for item in patrons
    ]


def _name_condition(query: str) -> list[ColumnElement[bool]]:
    terms = normalize(query).split()
    return [or_(Patron.name_norm.like(f"{term}%"), Patron.name_norm.like(f"% {term}%")) for term in terms]


@dataclass(frozen=True)
class Match:
    """Ce que désigne un texte tapé : des fiches précises (numéro, carte, coordonnée), ou une condition sur le nom."""

    ids: list[int] | None
    condition: ColumnElement[bool] | None
    notes: dict[int, str]


def match(session: Session, query: str, *, personal: bool) -> Match | None:
    """Numéro de lecteur, carte (en service ou ancienne), puis coordonnées **seulement** avec
    `patrons.view_personal`, pour qu'une coordonnée ne se sonde pas par la recherche ; sinon le nom."""
    text = query.strip()[:200]
    if not text:
        return None
    notes: dict[int, str] = {}
    number = rules.parse_patron_number(text)
    ids: list[int] = []
    if number:
        ids = list(session.scalars(select(Patron.id).where(Patron.number == number)))
    card = rules.normalize_card(text)
    if not ids and card:
        for patron_id, ended in session.execute(
            select(PatronCard.patron_id, PatronCard.ended_on).where(PatronCard.number == card)
        ):
            if patron_id is not None:
                ids.append(patron_id)
                if ended:
                    notes[patron_id] = rules.CARD_ENDED_NOTE
    if not ids and personal:
        ids = _by_contact(session, text)
    if ids:
        return Match(ids, None, notes)
    conditions = _name_condition(text)
    if not conditions:
        return Match([], None, notes)
    return Match(None, and_(*conditions, Patron.status != "anonymized"), notes)


def _narrow(statement: Select[Patron], found: Match) -> Select[Patron]:
    if found.ids is not None:
        return statement.where(Patron.id.in_(found.ids))
    assert found.condition is not None
    return statement.where(found.condition)


def search(
    session: Session,
    query: str,
    *,
    personal: bool,
    limit: int = MAX_RESULTS,
    adults_only: bool = False,
    visibility: Visibility = EVERYTHING,
) -> list[Row]:
    """Recherche instantanée du sélecteur : toutes les fiches qui ne sont pas anonymisées, sauf par numéro.

    `adults_only` ne garde que les adultes en service, pour choisir un responsable ou un référent."""
    found = match(session, query, personal=personal)
    if found is None:
        return []
    statement = _narrow(select(Patron), found).where(Patron.status.in_(visibility.statuses))
    if adults_only:
        statement = statement.where(Patron.category == "adult", Patron.status == "active")
    statement = statement.order_by(Patron.sort_key, Patron.id).limit(limit)
    return _rows(session, list(session.scalars(statement)), found.notes, visibility)


def rows_for(session: Session, patrons: Sequence[Patron], visibility: Visibility) -> list[Row]:
    """Les lignes de ces fiches, avec les mêmes repères et le même masquage que l'annuaire."""
    return _rows(session, patrons, None, visibility)


@dataclass(frozen=True)
class PaletteResult:
    rows: list[Row]
    more: bool = False  # d'autres fiches correspondent : on demande de préciser, sans en donner le nombre


PALETTE_LINES = 8
PALETTE_MIN_LETTERS = 3


def palette_search(
    session: Session, query: str, *, visibility: Visibility, limit: int = PALETTE_LINES
) -> PaletteResult:
    """La recherche de la palette ⌘K : nom, prénom, numéro permanent, carte **en service**. Jamais une coordonnée.

    Une fiche archivée ou anonymisée n'est proposée que si `visibility` le permet, et la seconde par son seul numéro.
    Une carte perdue ou révoquée ne mène plus à personne. Un texte trop court ne déclenche aucune recherche."""
    text = query.strip()
    if not text:
        return PaletteResult([])
    named = visibility.named
    number = rules.parse_patron_number(text)
    if number:
        found = session.scalars(
            select(Patron).where(Patron.number == number, Patron.status.in_(visibility.statuses))
        ).all()
        if found:
            return PaletteResult(_rows(session, list(found), None, visibility))
    card = rules.normalize_card(text)
    if card:
        holders = (
            session.scalars(
                select(Patron)
                .join(PatronCard, PatronCard.patron_id == Patron.id)
                .where(PatronCard.number == card, PatronCard.ended_on.is_(None), Patron.status.in_(named))
            )
            .unique()
            .all()
        )
        if holders:
            return PaletteResult(_rows(session, list(holders), None, visibility))
    if sum(len(term) for term in normalize(text).split()) < PALETTE_MIN_LETTERS:
        return PaletteResult([])
    statement = select(Patron).where(*_name_condition(text), Patron.status.in_(named))
    patrons = list(session.scalars(statement.order_by(Patron.sort_key, Patron.id).limit(limit + 1)))
    return PaletteResult(_rows(session, patrons[:limit], None, visibility), len(patrons) > limit)


def _by_contact(session: Session, text: str) -> list[int]:
    lowered = text.strip().lower()
    digits = "".join(char for char in text if char.isdigit())
    conditions = [and_(Contact.kind == "email", Contact.search_value == lowered)]
    if len(digits) >= 6:
        conditions.append(and_(Contact.kind == "phone", Contact.search_value.like(f"%{digits[-9:]}")))
    owners = session.execute(select(Contact.patron_id, Contact.household_id).where(or_(*conditions)).limit(50)).all()
    ids = {patron for patron, _household in owners if patron is not None}
    households = [household for _patron, household in owners if household is not None]
    if households:
        ids |= set(
            session.scalars(select(HouseholdMember.patron_id).where(HouseholdMember.household_id.in_(households)))
        )
    return sorted(ids)


@dataclass(frozen=True)
class Directory:
    rows: list[Row]
    total: int
    page: int
    pages: int
    elsewhere: int = 0  # fiches archivées qui répondent aussi à la recherche, hors de la vue courante


def _count(session: Session, statement: Select[Patron]) -> int:
    return session.scalar(select(func.count()).select_from(statement.subquery())) or 0


def directory(
    session: Session,
    view: str,
    page: int,
    today: date,
    *,
    query: str = "",
    personal: bool = False,
    size: int = rules.DEFAULT_PAGE_SIZE,
    visibility: Visibility = EVERYTHING,
) -> Directory:
    """L'annuaire : une vue (tous, adultes, mineurs, archivés, adhésion échue, responsable à renseigner), affinée
    par la recherche. Les fiches archivées qui répondent à la recherche sont **comptées**, jamais mêlées."""
    gone = tuple(status for status in GONE if visibility.allows(status))
    statement = select(Patron).where(Patron.status.in_(gone if view == "archives" else LIVING))
    if view in ("adultes", "mineurs"):
        statement = statement.where(Patron.category == FILTERS[view])
    if view == "adhesion":
        statement = statement.where(
            Patron.membership_expires_on.is_not(None), Patron.membership_expires_on < today.isoformat()
        )
    if view == "responsable":
        minors = list(session.scalars(select(Patron.id).where(Patron.category == "minor", Patron.status.in_(LIVING))))
        statement = statement.where(Patron.id.in_(sorted(_needs_guardian(session, minors))))
    found = match(session, query, personal=personal)
    notes: dict[int, str] = {}
    elsewhere = 0
    if found is not None:
        notes = found.notes
        statement = _narrow(statement, found)
        if view != "archives" and visibility.archived:
            elsewhere = _count(session, _narrow(select(Patron).where(Patron.status == "archived"), found))
    total = _count(session, statement)
    size = min(max(size, 1), rules.MAX_PAGE_SIZE)
    pages = max(1, -(-total // size))
    page = min(max(page, 1), pages)
    patrons = list(
        session.scalars(statement.order_by(Patron.sort_key, Patron.id).limit(size).offset((page - 1) * size))
    )
    return Directory(_rows(session, patrons, notes, visibility), total, page, pages, elsewhere)


@dataclass(frozen=True)
class Totals:
    patrons: int
    households: int


def totals(session: Session) -> Totals:
    """Les chiffres de l'en-tête : lecteurs en service, foyers et structures non archivés."""
    patrons = session.scalar(select(func.count()).select_from(Patron).where(Patron.status.in_(LIVING))) or 0
    count = session.scalar(select(func.count()).select_from(Household).where(Household.archived_at.is_(None))) or 0
    return Totals(patrons, count)


def homonyms(
    session: Session, first: str, last: str, exclude: int | None = None, visibility: Visibility = EVERYTHING
) -> list[Row]:
    """Personnes de même nom, avec leurs repères. Rien n'est rapproché : on signale, une personne décide."""
    key = rules.name_norm(first, last)
    statement = select(Patron).where(Patron.name_norm == key, Patron.status.in_(visibility.named))
    if exclude is not None:
        statement = statement.where(Patron.id != exclude)
    return _rows(session, list(session.scalars(statement.order_by(Patron.id))), None, visibility)


def hidden_homonyms(session: Session, first: str, last: str, visibility: Visibility) -> int:
    """Fiches archivées de même nom, que ce compte ne peut pas ouvrir : on signale leur nombre, jamais leurs repères."""
    hidden = [status for status in EVERYTHING.named if status not in visibility.named]
    if not hidden:
        return 0
    key = rules.name_norm(first, last)
    statement = select(func.count()).select_from(Patron).where(Patron.name_norm == key, Patron.status.in_(hidden))
    return session.scalar(statement) or 0


# --- Responsables : préparation de L4 ---------------------------------------------------------------------------------
def _reachable(session: Session, guardian_ids: Sequence[int]) -> set[int]:
    """Un responsable est joignable s'il a un téléphone ou un courriel, à lui ou à l'un de ses foyers."""
    own = set(
        session.scalars(
            select(Contact.patron_id).where(
                Contact.patron_id.in_(list(guardian_ids)), Contact.kind.in_(("phone", "email"))
            )
        )
    )
    shared = set(
        session.scalars(
            select(HouseholdMember.patron_id)
            .join(Contact, Contact.household_id == HouseholdMember.household_id)
            .where(HouseholdMember.patron_id.in_(list(guardian_ids)), Contact.kind.in_(("phone", "email")))
        )
    )
    return {item for item in own | shared if item is not None}


def _facts(session: Session, minor_ids: Sequence[int]) -> dict[int, list[GuardianFacts]]:
    """Les responsables de plusieurs mineurs et ce qu'ils peuvent faire, lus en lot."""
    found: dict[int, list[GuardianFacts]] = defaultdict(list)
    ids = sorted(set(minor_ids))
    if not ids:
        return found
    rows = session.execute(
        select(Guardianship, Patron.status)
        .join(Patron, Patron.id == Guardianship.guardian_id)
        .where(Guardianship.minor_id.in_(ids))
        .order_by(Guardianship.minor_id, Guardianship.contact_rank, Guardianship.created_at)
    ).all()
    reachable = _reachable(session, sorted({link.guardian_id for link, _status in rows}))
    for link, status in rows:
        found[link.minor_id].append(
            GuardianFacts(
                link.guardian_id,
                bool(link.can_borrow_for),
                bool(link.is_contact),
                link.guardian_id in reachable,
                status == "active",
                bool(link.can_borrow_alone),
            )
        )
    return found


def guardian_facts(session: Session, minor_id: int) -> list[GuardianFacts]:
    return list(_facts(session, [minor_id]).get(minor_id, []))


def eligible_guardians(session: Session, minor_id: int) -> list[int]:
    """Responsables qui pourront emprunter pour ce mineur en L4 : habilités **et** joignables, rien d'implicite."""
    return rules.eligible_guardians(guardian_facts(session, minor_id))


def can_borrow_for(session: Session, guardian_id: int, minor_id: int) -> bool:
    return guardian_id in eligible_guardians(session, minor_id)


# --- Fiche ------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CardLine:
    id: int
    number: str
    issued_on: str
    ended_on: str | None
    end_reason: str | None

    @property
    def end_label(self) -> str:
        return rules.CARD_END_REASONS.get(self.end_reason or "", "")


@dataclass(frozen=True)
class Membership:
    household_id: int
    name: str
    kind: str
    role: str
    joined_on: str
    archived: bool

    @property
    def role_label(self) -> str:
        return rules.MEMBER_ROLES.get(self.kind, {}).get(self.role, self.role)

    @property
    def kind_label(self) -> str:
        return rules.HOUSEHOLD_KINDS[self.kind]


@dataclass(frozen=True)
class Link:
    """Un lien de responsabilité vu d'un côté ou de l'autre, avec ses autorisations explicites."""

    other_id: int
    other_number: str
    other_name: str
    relation: str
    can_borrow_for: bool
    is_contact: bool
    contact_rank: int
    version: int
    reachable: bool = False
    can_borrow_alone: bool = False

    @property
    def relation_label(self) -> str:
        return rules.RELATIONS[self.relation]


@dataclass(frozen=True)
class HoldLine:
    id: int
    reason: str
    until: str | None
    placed_at: str
    lifted_at: str | None
    active: bool


@dataclass(frozen=True)
class Sheet:
    patron: Patron
    marks: Marks
    cards: list[CardLine]
    households: list[Membership]
    guardians: list[Link]
    wards: list[Link]
    holds: list[HoldLine]
    homonyms: list[Row]
    issues: list[str] = field(default_factory=list)
    majority_to_confirm: bool = False
    needs_guardian: bool = False

    @property
    def active_card(self) -> CardLine | None:
        return next((card for card in self.cards if card.ended_on is None), None)

    @property
    def active_hold(self) -> HoldLine | None:
        return next((hold for hold in self.holds if hold.active), None)


def hold_is_active(hold: PatronHold, today: date) -> bool:
    return hold.lifted_at is None and (hold.until is None or hold.until >= today.isoformat())


def sheet(session: Session, patron_id: int, today: date, visibility: Visibility = EVERYTHING) -> Sheet | None:
    patron = session.get(Patron, patron_id)
    if patron is None:
        return None
    cards = [
        CardLine(card.id, card.number, card.issued_on, card.ended_on, card.end_reason)
        for card in session.scalars(
            select(PatronCard)
            .where(PatronCard.patron_id == patron.id)
            .order_by(PatronCard.issued_on.desc(), PatronCard.id.desc())
        )
    ]
    memberships = [
        Membership(
            household.id,
            household.name,
            household.kind,
            member.role,
            member.joined_on,
            household.archived_at is not None,
        )
        for member, household in session.execute(
            select(HouseholdMember, Household)
            .join(Household, Household.id == HouseholdMember.household_id)
            .where(HouseholdMember.patron_id == patron.id)
            .order_by(Household.name)
        )
    ]
    guardian_rows = session.execute(
        select(Guardianship, Patron)
        .join(Patron, Patron.id == Guardianship.guardian_id)
        .where(Guardianship.minor_id == patron.id)
        .order_by(Guardianship.contact_rank, Guardianship.created_at)
    ).all()
    reachable = _reachable(session, [link.guardian_id for link, _other in guardian_rows])
    guardians = [
        Link(
            other.id,
            other.number,
            rules.display_name(other.first_name, other.last_name, other.status),
            link.relation,
            bool(link.can_borrow_for),
            bool(link.is_contact),
            link.contact_rank,
            link.version,
            other.id in reachable,
            bool(link.can_borrow_alone),
        )
        for link, other in guardian_rows
        if visibility.allows(other.status)
    ]
    wards = [
        Link(
            other.id,
            other.number,
            rules.display_name(other.first_name, other.last_name, other.status),
            link.relation,
            bool(link.can_borrow_for),
            bool(link.is_contact),
            link.contact_rank,
            link.version,
        )
        for link, other in session.execute(
            select(Guardianship, Patron)
            .join(Patron, Patron.id == Guardianship.minor_id)
            .where(Guardianship.guardian_id == patron.id, Patron.status.in_(visibility.statuses))
            .order_by(Patron.sort_key)
        )
    ]
    holds = [
        HoldLine(hold.id, hold.reason, hold.until, hold.placed_at, hold.lifted_at, hold_is_active(hold, today))
        for hold in session.scalars(
            select(PatronHold).where(PatronHold.patron_id == patron.id).order_by(PatronHold.placed_at.desc())
        )
    ]
    issue = rules.category_issue(patron.category, patron.birth, today)
    living = patron.status in ("active", "suspended")
    return Sheet(
        patron=patron,
        marks=marks_of(session, [patron])[patron.id],
        cards=cards,
        households=memberships,
        guardians=guardians,
        wards=wards,
        holds=holds,
        homonyms=homonyms(session, patron.first_name, patron.last_name, patron.id, visibility)
        if patron.status != "anonymized"
        else [],
        issues=[issue] if issue else [],
        majority_to_confirm=living and rules.majority_to_confirm(patron.category, patron.birth, today),
        needs_guardian=living
        and patron.category == "minor"
        and rules.needs_guardian(guardian_facts(session, patron.id)),
    )


# --- Coordonnées : seulement avec `patrons.view_personal` ------------------------------------------------------------
@dataclass(frozen=True)
class ContactLine:
    id: int
    kind: str
    label: str | None
    value: str | None
    line1: str | None
    line2: str | None
    postal_code: str | None
    city: str | None
    country: str | None
    shared_by: str | None = None  # nom du foyer quand la coordonnée est partagée

    @property
    def display(self) -> str:
        if self.kind == "phone" and self.value:
            return format_phone(self.value)
        if self.kind == "address":
            parts = [self.line1, self.line2, " ".join(part for part in (self.postal_code, self.city) if part)]
            if self.country and self.country != "FR":
                parts.append(self.country)
            return "\n".join(part for part in parts if part)
        return self.value or ""

    @property
    def label_text(self) -> str:
        return rules.CONTACT_LABELS.get(self.label or "", "")


def _contact_line(contact: Contact, shared_by: str | None = None) -> ContactLine:
    return ContactLine(
        contact.id,
        contact.kind,
        contact.label,
        contact.value,
        contact.line1,
        contact.line2,
        contact.postal_code,
        contact.city,
        contact.country,
        shared_by,
    )


def contacts_of(session: Session, patron_id: int) -> list[ContactLine]:
    """Coordonnées propres, puis celles des foyers du lecteur. **N'appeler qu'avec `patrons.view_personal`.**"""
    own = [
        _contact_line(contact)
        for contact in session.scalars(
            select(Contact).where(Contact.patron_id == patron_id).order_by(Contact.kind, Contact.is_primary.desc())
        )
    ]
    shared = [
        _contact_line(contact, name)
        for contact, name in session.execute(
            select(Contact, Household.name)
            .join(HouseholdMember, HouseholdMember.household_id == Contact.household_id)
            .join(Household, Household.id == Contact.household_id)
            .where(HouseholdMember.patron_id == patron_id)
            .order_by(Household.name, Contact.kind)
        )
    ]
    return own + shared


@dataclass(frozen=True)
class GuardianContacts:
    guardian_id: int
    number: str
    name: str
    lines: list[ContactLine]


def guardian_contacts(session: Session, minor_id: int) -> list[GuardianContacts]:
    """Comment joindre un mineur : ses responsables **autorisés à être contactés**, eux seuls (D6).

    **N'appeler qu'avec `patrons.view_personal`.**"""
    rows = session.execute(
        select(Guardianship, Patron)
        .join(Patron, Patron.id == Guardianship.guardian_id)
        .where(Guardianship.minor_id == minor_id, Guardianship.is_contact.is_(True), Patron.status.in_(LIVING))
        .order_by(Guardianship.contact_rank, Guardianship.created_at)
    ).all()
    return [
        GuardianContacts(
            other.id,
            other.number,
            rules.display_name(other.first_name, other.last_name, other.status),
            contacts_of(session, other.id),
        )
        for _link, other in rows
    ]


def household_contacts(session: Session, household_id: int) -> list[ContactLine]:
    """Coordonnées d'un foyer. **N'appeler qu'avec `patrons.view_personal`.**"""
    return [
        _contact_line(contact)
        for contact in session.scalars(
            select(Contact).where(Contact.household_id == household_id).order_by(Contact.kind)
        )
    ]


# --- Foyers -----------------------------------------------------------------------------------------------------------
#: Les adultes qui répondent du foyer d'abord, comme sur la maquette.
_ROLE_ORDER = case(
    (HouseholdMember.role.in_(("responsible", "referent")), 0), (HouseholdMember.role == "member", 1), else_=2
)


@dataclass(frozen=True)
class MemberLine:
    patron_id: int
    number: str
    name: str
    category: str
    role: str
    kind: str
    status: str
    can_borrow_for_structure: bool = False

    @property
    def role_label(self) -> str:
        return rules.MEMBER_ROLES[self.kind].get(self.role, self.role)


@dataclass(frozen=True)
class HouseholdSheet:
    household: Household
    members: list[MemberLine]

    @property
    def kind_label(self) -> str:
        return rules.HOUSEHOLD_KINDS[self.household.kind]


def household_sheet(session: Session, household_id: int, visibility: Visibility = EVERYTHING) -> HouseholdSheet | None:
    household = session.get(Household, household_id)
    if household is None:
        return None
    members = [
        MemberLine(
            patron.id,
            patron.number,
            rules.display_name(patron.first_name, patron.last_name, patron.status),
            patron.category,
            member.role,
            household.kind,
            patron.status,
            bool(member.can_borrow_for_structure),
        )
        for member, patron in session.execute(
            select(HouseholdMember, Patron)
            .join(Patron, Patron.id == HouseholdMember.patron_id)
            .where(HouseholdMember.household_id == household.id, Patron.status.in_(visibility.statuses))
            .order_by(_ROLE_ORDER, Patron.sort_key)
        )
    ]
    return HouseholdSheet(household, members)


@dataclass(frozen=True)
class HouseholdCard:
    id: int
    name: str
    kind: str
    members: list[MemberLine]
    archived: bool

    @property
    def kind_label(self) -> str:
        return rules.HOUSEHOLD_KINDS[self.kind]


@dataclass(frozen=True)
class HouseholdPage:
    cards: list[HouseholdCard]
    total: int
    page: int
    pages: int


def _household_statement(archived: bool, query: str, visibility: Visibility = EVERYTHING) -> Select[Household]:
    statement = select(Household).where(
        Household.archived_at.is_not(None) if archived else Household.archived_at.is_(None)
    )
    for term in normalize(query[:200]).split():
        member = (
            select(HouseholdMember.household_id)
            .join(Patron, Patron.id == HouseholdMember.patron_id)
            .where(Patron.name_norm.like(f"%{term}%"), Patron.status.in_(visibility.named))
        )
        statement = statement.where(or_(Household.name_norm.like(f"%{term}%"), Household.id.in_(member)))
    return statement


def household_page(
    session: Session,
    *,
    query: str = "",
    page: int = 1,
    archived: bool = False,
    size: int = rules.DEFAULT_PAGE_SIZE,
    visibility: Visibility = EVERYTHING,
) -> HouseholdPage:
    """Les foyers et structures, par nom du foyer ou de l'un de ses membres, page par page."""
    statement = _household_statement(archived, query, visibility)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    size = min(max(size, 1), rules.MAX_PAGE_SIZE)
    pages = max(1, -(-total // size))
    page = min(max(page, 1), pages)
    ordered = statement.order_by(Household.kind, Household.name_norm, Household.id)
    found = list(session.scalars(ordered.limit(size).offset((page - 1) * size)))
    return HouseholdPage(_household_cards(session, found, visibility), total, page, pages)


def _household_cards(
    session: Session, found: list[Household], visibility: Visibility = EVERYTHING
) -> list[HouseholdCard]:
    members: dict[int, list[MemberLine]] = defaultdict(list)
    if found:
        kinds = {item.id: item.kind for item in found}
        for member, patron in session.execute(
            select(HouseholdMember, Patron)
            .join(Patron, Patron.id == HouseholdMember.patron_id)
            .where(HouseholdMember.household_id.in_(list(kinds)), Patron.status.in_(visibility.statuses))
            .order_by(_ROLE_ORDER, Patron.sort_key)
        ):
            members[member.household_id].append(
                MemberLine(
                    patron.id,
                    patron.number,
                    rules.display_name(patron.first_name, patron.last_name, patron.status),
                    patron.category,
                    member.role,
                    kinds[member.household_id],
                    patron.status,
                )
            )
    return [
        HouseholdCard(item.id, item.name, item.kind, members[item.id], item.archived_at is not None) for item in found
    ]


# --- À traiter (D2, D5, D12, D14) -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ToHandle:
    majorities: list[Row]
    incoherent: list[Row]
    without_guardian: list[Row]
    expired: list[Row]
    held: list[Row]
    empty_households: list[HouseholdCard]
    counts: dict[str, int] = field(default_factory=dict)  # les totaux : chaque liste s'arrête à `TO_HANDLE_LINES`


TO_HANDLE_LINES = 50


def to_handle(session: Session, today: date, visibility: Visibility = EVERYTHING) -> ToHandle:
    living = list(session.scalars(select(Patron).where(Patron.status.in_(("active", "suspended")))))
    majorities = [item for item in living if rules.majority_to_confirm(item.category, item.birth, today)]
    incoherent = [item for item in living if rules.category_issue(item.category, item.birth, today)]
    minors = [item for item in living if item.category == "minor"]
    lacking = _needs_guardian(session, [item.id for item in minors])
    expired = [item for item in living if item.membership_expires_on and item.membership_expires_on < today.isoformat()]
    held_ids = {hold.patron_id for hold in session.scalars(select(PatronHold)) if hold_is_active(hold, today)}
    held_query = select(Patron).where(Patron.id.in_(held_ids), Patron.status.in_(visibility.statuses))
    held = list(session.scalars(held_query)) if held_ids else []
    lonely = select(Household).where(
        Household.archived_at.is_(None),
        ~select(HouseholdMember.patron_id).where(HouseholdMember.household_id == Household.id).exists(),
    )
    empty = _household_cards(session, list(session.scalars(lonely.order_by(Household.name_norm).limit(200))))
    without = [item for item in minors if item.id in lacking]
    groups = {
        "majorities": majorities,
        "incoherent": incoherent,
        "without_guardian": without,
        "expired": expired,
        "held": held,
    }
    return ToHandle(
        _rows(session, majorities[:TO_HANDLE_LINES]),
        _rows(session, incoherent[:TO_HANDLE_LINES]),
        _rows(session, without[:TO_HANDLE_LINES]),
        _rows(session, expired[:TO_HANDLE_LINES]),
        _rows(session, held[:TO_HANDLE_LINES]),
        empty,
        {key: len(value) for key, value in groups.items()},
    )


# --- Historique : le journal, sans valeurs (D10) ----------------------------------------------------------------------
@dataclass(frozen=True)
class HistoryLine:
    at: str
    action: str
    fields: list[str]
    who: str = ""

    @property
    def label(self) -> str:
        return rules.HISTORY_ACTIONS.get(self.action, self.action)

    @property
    def field_labels(self) -> list[str]:
        return [rules.HISTORY_FIELDS.get(item, item) for item in self.fields]


def history(session: Session, patron_id: int, limit: int = 50) -> list[HistoryLine]:
    rows = session.execute(
        select(AuditEntry.at, AuditEntry.action, AuditEntry.details, User.display_name)
        .outerjoin(User, User.id == AuditEntry.user_id)
        .where(AuditEntry.entity_type == "patron", AuditEntry.entity_id == patron_id)
        .order_by(AuditEntry.id.desc())
        .limit(limit)
    )
    lines = []
    for at, action, details, who in rows:
        data = json.loads(details) if details else {}
        lines.append(HistoryLine(at, action, [str(item) for item in data.get("champs", [])], who or ""))
    return lines
