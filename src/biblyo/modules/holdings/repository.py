# SPDX-License-Identifier: EUPL-1.2
"""Lecture des emplacements et des exemplaires : des vues détachées de la session."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from biblyo.core.i18n import _
from biblyo.core.models import AuditEntry
from biblyo.core.text import normalize
from biblyo.modules.catalogue.models import (
    Agent,
    Copy,
    DocumentType,
    Edition,
    Publisher,
    Series,
    Work,
    WorkContributor,
    WorkSeries,
)
from biblyo.modules.catalogue.rules import code_of
from biblyo.modules.holdings import rules
from biblyo.modules.holdings.models import Location
from biblyo.modules.holdings.rules import CallFacts, Fill, SortFacts


def ancestors(session: Session, location_id: int | None) -> list[Location]:
    """Le lieu et ses parents, du plus proche au plus lointain. S'arrête sur un cycle, qui ne devrait pas exister."""
    chain: list[Location] = []
    seen: set[int] = set()
    current = session.get(Location, location_id) if location_id else None
    while current is not None and current.id not in seen:
        chain.append(current)
        seen.add(current.id)
        current = session.get(Location, current.parent_id) if current.parent_id else None
    return chain


def descendant_ids(session: Session, location_id: int) -> list[int]:
    """Le lieu et tous ses descendants."""
    found = [location_id]
    frontier = [location_id]
    while frontier:
        children = list(session.scalars(select(Location.id).where(Location.parent_id.in_(frontier))))
        found += children
        frontier = children
    return found


def children(session: Session, location_id: int | None) -> list[Location]:
    statement = select(Location).order_by(Location.sort_order, Location.name, Location.id)
    statement = statement.where(
        Location.parent_id == location_id if location_id is not None else Location.parent_id.is_(None)
    )
    return list(session.scalars(statement))


def shelf_order(session: Session, location_id: int) -> list[int]:
    """Exemplaires d'un niveau, dans l'ordre de leurs positions."""
    statement = (
        select(Copy.id)
        .where(Copy.location_id == location_id, Copy.shelf_position.is_not(None))
        .order_by(Copy.shelf_position)
    )
    return list(session.scalars(statement))


def copies_in(session: Session, location_ids: Sequence[int]) -> int:
    statement = select(func.count()).select_from(Copy).where(Copy.location_id.in_(list(location_ids)))
    return session.scalar(statement) or 0


def fill_of(session: Session, location: Location) -> Fill:
    count = session.scalar(select(func.count()).select_from(Copy).where(Copy.location_id == location.id)) or 0
    return Fill(count, location.capacity)


@dataclass(frozen=True)
class OnLoan:
    """Un prêt en cours vu depuis l'exemplaire : de quoi dire « sorti » et l'échéance. Aucun lecteur ici."""

    loan_id: int
    due_on: str


_ON_LOAN = text(
    "SELECT copy_id, id, due_on FROM loans WHERE copy_id IN :ids AND returned_at IS NULL AND cancelled_at IS NULL"
).bindparams(bindparam("ids", expanding=True))


def on_loan(session: Session, copy_ids: Sequence[int]) -> dict[int, OnLoan]:
    """Les exemplaires sortis parmi ceux-ci. La table des prêts relève du module des prêts : lue, jamais écrite."""
    ids = list(copy_ids)
    if not ids:
        return {}
    return {copy_id: OnLoan(loan_id, due_on) for copy_id, loan_id, due_on in session.execute(_ON_LOAN, {"ids": ids})}


def find_copy_by_code(session: Session, value: str) -> Copy | None:
    """Exemplaire désigné par un scan ou une saisie : code-barres, sinon numéro d'inventaire (D3).

    Une saisie sans ses tirets (« EX 000001 », « ex000001 ») est reconnue si un seul exemplaire lui correspond : en cas
    de doute, rien n'est choisi à la place de la personne.
    """
    code = rules.normalize_code(value) or value.strip().upper()
    found = session.scalar(select(Copy).where(Copy.barcode == code))
    found = found or session.scalar(select(Copy).where(Copy.inventory_number == code))
    if found is not None:
        return found
    compact = code.replace("-", "")
    candidates = list(
        session.scalars(
            select(Copy)
            .where(
                (func.replace(Copy.barcode, "-", "") == compact)
                | (func.replace(Copy.inventory_number, "-", "") == compact)
            )
            .limit(2)
        )
    )
    return candidates[0] if len(candidates) == 1 else None


def _primary_contributor(session: Session, work_id: int) -> str | None:
    return session.scalar(
        select(Agent.sort_name)
        .join(WorkContributor, WorkContributor.agent_id == Agent.id)
        .where(WorkContributor.work_id == work_id)
        .order_by(WorkContributor.position)
        .limit(1)
    )


def call_facts(session: Session, edition_id: int) -> CallFacts:
    """De quoi proposer une cote (D2) : jamais le tome commercial, jamais l'emplacement."""
    row = session.execute(
        select(Work, DocumentType.code)
        .join(Edition, Edition.work_id == Work.id)
        .join(DocumentType, DocumentType.id == Work.document_type_id)
        .where(Edition.id == edition_id)
    ).one()
    work, type_code = row
    link = session.execute(
        select(WorkSeries.call_sequence, Series.title_sort, Series.call_prefix)
        .join(Series, Series.id == WorkSeries.series_id)
        .where(WorkSeries.work_id == work.id, WorkSeries.is_primary.is_(True))
    ).first()
    return CallFacts(
        type_code=type_code,
        title=work.title_sort,  # sans article : « La Cible » se range à C
        series_title=link[1] if link else None,
        series_prefix=link[2] if link else None,
        call_sequence=link[0] if link else None,
        author_sort_name=_primary_contributor(session, work.id),
    )


def sort_facts(session: Session, copy_ids: Sequence[int]) -> list[SortFacts]:
    rows = session.execute(
        select(Copy.id, Copy.call_number, Work.title_sort)
        .add_columns(Series.title_sort, WorkSeries.volume_number, WorkSeries.volume_label)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .outerjoin(WorkSeries, (WorkSeries.work_id == Work.id) & WorkSeries.is_primary.is_(True))
        .outerjoin(Series, Series.id == WorkSeries.series_id)
        .where(Copy.id.in_(list(copy_ids)))
    ).all()
    return [SortFacts(row[0], row[2], row[3], row[4], row[5], row[1]) for row in rows]


@dataclass(frozen=True)
class CopyBrief:
    """Un exemplaire tel qu'on le désigne à quelqu'un : titre, cote, code."""

    id: int
    work_id: int
    title: str
    call_number: str | None
    barcode: str
    inventory_number: str
    status: str


def briefs(session: Session, copy_ids: Sequence[int]) -> dict[int, CopyBrief]:
    rows = session.execute(
        select(Copy, Work.id, Work.title)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .where(Copy.id.in_(list(copy_ids)))
    ).all()
    return {
        copy.id: CopyBrief(
            copy.id, work_id, title, copy.call_number, code_of(copy.barcode, copy.inventory_number),
            copy.inventory_number, copy.status
        )
        for copy, work_id, title in rows
    }


@dataclass(frozen=True)
class Localisation:
    copy: CopyBrief
    location_id: int | None
    path: str
    position: int | None
    before: CopyBrief | None
    after: CopyBrief | None


def localise(session: Session, copy_id: int) -> Localisation | None:
    """Où se trouve un exemplaire, et entre quels voisins."""
    copy = session.get(Copy, copy_id)
    if copy is None:
        return None
    brief = briefs(session, [copy_id])[copy_id]
    if copy.location_id is None:
        return Localisation(brief, None, "", None, None, None)
    location = session.get(Location, copy.location_id)
    assert location is not None
    before = after = None
    if copy.shelf_position is not None:
        order = shelf_order(session, location.id)
        previous_id, next_id = rules.neighbours(order, copy.id)
        found = briefs(session, [item for item in (previous_id, next_id) if item is not None])
        before = found.get(previous_id) if previous_id else None
        after = found.get(next_id) if next_id else None
    return Localisation(brief, location.id, location.path, copy.shelf_position, before, after)


def work_ids_of_copies(session: Session, copy_ids: Sequence[int]) -> list[int]:
    statement = select(Edition.work_id).join(Copy, Copy.edition_id == Edition.id).where(Copy.id.in_(list(copy_ids)))
    return sorted(set(session.scalars(statement)))


def counts_by_location(session: Session) -> dict[int, int]:
    rows = session.execute(
        select(Copy.location_id, func.count()).where(Copy.location_id.is_not(None)).group_by(Copy.location_id)
    )
    return {location_id: count for location_id, count in rows if location_id is not None}


def unshelved_count(session: Session) -> int:
    statement = select(func.count()).select_from(Copy).where(
        Copy.location_id.is_(None), Copy.status.not_in(("withdrawn", "lost"))
    )
    return session.scalar(statement) or 0


def subtree_counts(locations: Sequence[Location], direct: dict[int, int]) -> dict[int, int]:
    """Exemplaires de chaque lieu, descendants compris."""
    by_parent: dict[int | None, list[Location]] = defaultdict(list)
    for item in locations:
        by_parent[item.parent_id].append(item)
    totals: dict[int, int] = {}

    def total(location: Location) -> int:
        value = direct.get(location.id, 0) + sum(total(child) for child in by_parent.get(location.id, []))
        totals[location.id] = value
        return value

    for root in by_parent.get(None, []):
        total(root)
    return totals


def sibling_name_taken(session: Session, parent_id: int | None, name: str, exclude: int | None = None) -> bool:
    statement = select(Location.id, Location.name).where(
        Location.parent_id == parent_id if parent_id is not None else Location.parent_id.is_(None)
    )
    return any(normalize(other) == normalize(name) and item != exclude for item, other in session.execute(statement))


# --- Fiche d'exemplaire -----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class HistoryLine:
    at: str
    text: str


HISTORY_ACTIONS = (
    "copy.shelve",
    "copy.shelve_scan",
    "copy.shelve_undo",
    "copy.unshelve",
    "copy.status",
    "copy.update",
)
MAX_HISTORY = 30


def _history_text(action: str, details: dict[str, object]) -> str:
    origin, target, rank = details.get("de"), details.get("vers"), details.get("rang")
    if action == "copy.shelve_undo":
        return _("Rangement annulé")
    if action == "copy.unshelve":
        return _("A quitté sa place : {origin}").format(origin=origin)
    if action == "copy.status":
        labels = {**rules.STATUS_ACTION_LABELS, "verify": _("Présence vérifiée : en place")}  # lot L6
        label = labels.get(str(details.get("action")), str(details.get("action")))
        motive = details.get("motif")
        return f"{label} — {motive}" if motive else str(label)
    if action == "copy.update":
        return _("Fiche modifiée")
    place = f"{target}, " + _("position {n}").format(n=rank) if rank else str(target)
    return _("Déplacé de {origin} vers {place}").format(origin=origin, place=place) if origin else _(
        "Rangé : {place}"
    ).format(place=place)


def history(session: Session, copy_id: int) -> list[HistoryLine]:
    """Ce qui est arrivé à cet exemplaire, du plus récent au plus ancien : d'où, vers où, pourquoi."""
    rows = session.execute(
        select(AuditEntry.at, AuditEntry.action, AuditEntry.details)
        .where(AuditEntry.entity_type == "copy", AuditEntry.entity_id == copy_id)
        .where(AuditEntry.action.in_(HISTORY_ACTIONS))
        .order_by(AuditEntry.id.desc())
        .limit(MAX_HISTORY)
    )
    return [
        HistoryLine(at, _history_text(action, json.loads(details) if details else {})) for at, action, details in rows
    ]


@dataclass(frozen=True)
class CopySheet:
    copy: Copy
    work_id: int
    work_title: str
    edition_label: str
    where: Localisation
    history: list[HistoryLine]


def copy_sheet(session: Session, copy_id: int) -> CopySheet | None:
    copy = session.get(Copy, copy_id)
    where = localise(session, copy_id)
    if copy is None or where is None:
        return None
    row = session.execute(
        select(Work.id, Work.title, Publisher.name, Edition.publication_year)
        .join(Edition, Edition.work_id == Work.id)
        .outerjoin(Publisher, Publisher.id == Edition.publisher_id)
        .where(Edition.id == copy.edition_id)
    ).one()
    label = " · ".join(str(part) for part in (row[2], row[3]) if part)
    return CopySheet(copy, row[0], row[1], label, where, history(session, copy_id))


@dataclass(frozen=True)
class PlaceChoice:
    """Un lieu où ranger : étagère, bac ou lieu virtuel actif, avec son remplissage."""

    id: int
    path: str
    fill: Fill
    is_level: bool

    @property
    def label(self) -> str:
        if self.fill.capacity is None:
            return f"{self.path} ({self.fill.count})"
        return f"{self.path} ({self.fill.count}/{self.fill.capacity})"


def place_choices(session: Session) -> list[PlaceChoice]:
    """Les lieux qui acceptent des exemplaires, sous des ancêtres tous actifs."""
    locations = list(session.scalars(select(Location).order_by(Location.path)))
    by_id = {item.id: item for item in locations}
    counts = counts_by_location(session)

    def active(item: Location) -> bool:
        seen: set[int] = set()
        current: Location | None = item
        while current is not None and current.id not in seen:
            if not current.is_active:
                return False
            seen.add(current.id)
            current = by_id.get(current.parent_id) if current.parent_id else None
        return True

    return [
        PlaceChoice(item.id, item.path, Fill(counts.get(item.id, 0), item.capacity), rules.is_level(item.kind))
        for item in locations
        if rules.accepts_copies(item.kind) and active(item)
    ]


# --- Ranger en scannant : le journal vient du journal d'audit, seule source qui ne ment pas ---------------------------
@dataclass(frozen=True)
class ScanLine:
    entry_id: int
    at: str
    copy: CopyBrief
    origin: str | None
    target: str
    rank: int | None
    undoable: bool


SCAN_ACTION = "copy.shelve_scan"
MAX_SCAN_LINES = 20


def scan_log(session: Session, user_id: int) -> list[ScanLine]:
    """Les derniers scans de ce compte, du plus récent au plus ancien. Annulables si l'exemplaire n'a pas rebougé."""
    rows = session.execute(
        select(AuditEntry.id, AuditEntry.at, AuditEntry.entity_id, AuditEntry.details)
        .where(AuditEntry.action == SCAN_ACTION, AuditEntry.user_id == user_id)
        .order_by(AuditEntry.id.desc())
        .limit(MAX_SCAN_LINES)
    ).all()
    copy_ids = [row[2] for row in rows if row[2] is not None]
    found = briefs(session, copy_ids) if copy_ids else {}
    versions = dict(session.execute(select(Copy.id, Copy.version).where(Copy.id.in_(copy_ids))).all())
    lines = []
    for entry_id, at, copy_id, details in rows:
        data = json.loads(details) if details else {}
        if copy_id not in found:
            continue
        lines.append(
            ScanLine(
                entry_id,
                at,
                found[copy_id],
                data.get("de"),
                data.get("vers", ""),
                data.get("rang"),
                versions.get(copy_id) == data.get("version"),
            )
        )
    return lines


def scan_entry(session: Session, entry_id: int, user_id: int) -> tuple[int, dict[str, object]] | None:
    """Une ligne de scan de ce compte : on n'annule que ce qu'on a soi-même rangé."""
    row = session.execute(
        select(AuditEntry.entity_id, AuditEntry.details).where(
            AuditEntry.id == entry_id, AuditEntry.action == SCAN_ACTION, AuditEntry.user_id == user_id
        )
    ).first()
    if row is None or row[0] is None:
        return None
    return row[0], json.loads(row[1]) if row[1] else {}
