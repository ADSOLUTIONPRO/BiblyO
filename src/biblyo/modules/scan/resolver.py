# SPDX-License-Identifier: EUPL-1.2
"""Résolution centrale d'un code lu (lot L8, scanner universel). **Lecture seule** : identifier n'écrit jamais.

Un même texte est cherché, dans cet ordre et sans rien deviner :

1. code-barres interne ou numéro d'inventaire d'un exemplaire (`holdings.repository.find_copy_by_code`, la recherche
   du rangement par scan et de « Localiser ») ;
2. ISBN-10, ISBN-13 ou EAN d'une **édition** : il mène aux éditions du catalogue puis à leurs exemplaires, à choisir
   explicitement, jamais à un exemplaire déduit (un ISBN n'identifie pas un livre physique) ;
3. carte ou numéro de lecteur, seulement pour un compte qui voit les lecteurs, selon les mêmes règles qu'au comptoir.

Deux réponses de natures différentes forment une ambiguïté : la personne choisit. Rien ne part sur Internet ici ; une
recherche à la BnF ou au Sudoc n'est que **proposée** pour un ISBN absent du catalogue. Un code mal tapé par une
douchette réglée en QWERTY est relu, mais seulement montré comme suggestion, jamais appliqué en silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core.identifiers import Shape, shape
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.catalogue.models import Copy, Edition, EditionIdentifier, Publisher
from biblyo.modules.catalogue.repository import CardView, load_cards
from biblyo.modules.catalogue.rules import ACTIVE_COPY_EXCLUDED, COPY_STATUSES
from biblyo.modules.circulation import repository as circulation
from biblyo.modules.circulation import rules as circulation_rules
from biblyo.modules.holdings import availability
from biblyo.modules.holdings import repository as holdings
from biblyo.modules.holdings.models import Location
from biblyo.modules.holdings.rules import CopyState
from biblyo.modules.patrons.repository import Visibility

KIND_COPY = "copy"
KIND_ISBN = "isbn"
KIND_READER = "reader"
KIND_AMBIGUOUS = "ambiguous"
KIND_UNKNOWN = "unknown"
MAX_EDITIONS = 12
MAX_COPIES_PER_EDITION = 30


@dataclass(frozen=True)
class CopyView:
    """Tout ce qu'il faut voir d'un exemplaire après un scan, sans emprunteur (D11 bis)."""

    id: int
    work_id: int
    edition_id: int
    card: CardView | None
    edition: str
    barcode: str | None
    inventory: str
    call_number: str | None
    status: str
    status_label: str
    state: CopyState | None
    loan_id: int | None
    due_on: str | None
    location_id: int | None
    path: str
    position: int | None
    version: int = 1
    found_by: str = ""  # « barcode », « inventory » ou « choice »

    @property
    def title(self) -> str:
        return self.card.title if self.card else ""

    @property
    def withdrawn(self) -> bool:
        return self.status in ACTIVE_COPY_EXCLUDED


@dataclass(frozen=True)
class EditionMatch:
    edition_id: int
    work_id: int
    card: CardView | None
    edition: str
    copies: tuple[CopyView, ...]


@dataclass(frozen=True)
class ReaderMatch:
    patron_id: int
    name: str
    detail: str
    ended: bool = False


@dataclass
class Identification:
    raw: str
    shape: Shape
    kind: str = KIND_UNKNOWN
    copy: CopyView | None = None
    editions: list[EditionMatch] = field(default_factory=list)
    readers: list[ReaderMatch] = field(default_factory=list)
    keyboard: str | None = None  # même code relu « en AZERTY » quand la douchette écrit en QWERTY
    keyboard_found: bool = False  # ce code relu désigne bien quelque chose

    @property
    def text(self) -> str:
        return self.shape.text

    @property
    def isbn13(self) -> str | None:
        return self.shape.isbn.isbn13 if self.shape.isbn else self.shape.ean


def _edition_labels(session: Session, edition_ids: list[int]) -> dict[int, str]:
    if not edition_ids:
        return {}
    rows = session.execute(
        select(Edition.id, Publisher.name, Edition.publication_year)
        .outerjoin(Publisher, Publisher.id == Edition.publisher_id)
        .where(Edition.id.in_(edition_ids))
    ).all()
    isbns: dict[int, str] = {}
    for edition_id, value in session.execute(
        select(EditionIdentifier.edition_id, EditionIdentifier.value).where(
            EditionIdentifier.edition_id.in_(edition_ids), EditionIdentifier.scheme == "isbn13"
        )
    ):
        isbns.setdefault(edition_id, value)
    out = {}
    for edition_id, publisher, year in rows:
        parts = [part for part in (publisher, str(year) if year else "") if part]
        if edition_id in isbns:
            parts.append(f"ISBN {isbns[edition_id]}")
        out[edition_id] = " · ".join(parts)
    return out


def copy_views(session: Session, copy_ids: list[int], found_by: str = "") -> list[CopyView]:
    """Les exemplaires demandés, prêts à afficher, dans l’ordre demandé, en quelques requêtes."""
    if not copy_ids:
        return []
    rows = session.execute(
        select(Copy, Edition.work_id, Location.path)
        .join(Edition, Edition.id == Copy.edition_id)
        .outerjoin(Location, Location.id == Copy.location_id)
        .where(Copy.id.in_(copy_ids))
    ).all()
    found = {copy.id: (copy, work_id, path) for copy, work_id, path in rows}
    cards = {card.id: card for card in load_cards(session, sorted({work for _c, work, _p in found.values()}))}
    states = availability.for_copies(session, list(found))
    loans = holdings.on_loan(session, list(found))
    editions = _edition_labels(session, sorted({copy.edition_id for copy, _w, _p in found.values()}))
    out = []
    for copy_id in copy_ids:
        if copy_id not in found:
            continue
        copy, work_id, path = found[copy_id]
        loan = loans.get(copy_id)
        out.append(
            CopyView(
                copy.id,
                work_id,
                copy.edition_id,
                cards.get(work_id),
                editions.get(copy.edition_id, ""),
                copy.barcode,
                copy.inventory_number,
                copy.call_number,
                copy.status,
                COPY_STATUSES.get(copy.status, copy.status),
                states.get(copy_id),
                loan.loan_id if loan else None,
                loan.due_on if loan else None,
                copy.location_id,
                path or "",
                copy.shelf_position,
                copy.version,
                found_by,
            )
        )
    return out


def _isbn_editions(session: Session, found: Shape) -> list[EditionMatch]:
    """Les éditions qui portent cet ISBN ou cet EAN, et leurs exemplaires : jamais un exemplaire choisi pour la
    personne. Les éditions de toutes les notices sont cherchées, archivées comprises (montrées comme telles)."""
    values: set[str] = set()
    if found.isbn is not None:
        values.add(found.isbn.isbn13)
        if found.isbn.isbn10:
            values.add(found.isbn.isbn10)
    if found.ean:
        values.add(found.ean)
    if not values:
        return []
    edition_ids = list(
        dict.fromkeys(
            session.scalars(
                select(EditionIdentifier.edition_id)
                .where(EditionIdentifier.scheme.in_(("isbn13", "isbn10", "ean")), EditionIdentifier.value.in_(values))
                .order_by(EditionIdentifier.edition_id)
                .limit(MAX_EDITIONS)
            )
        )
    )
    if not edition_ids:
        return []
    rows = session.execute(select(Edition.id, Edition.work_id).where(Edition.id.in_(edition_ids))).all()
    works = dict(rows)
    cards = {card.id: card for card in load_cards(session, sorted(set(works.values())))}
    labels = _edition_labels(session, edition_ids)
    copies_by_edition: dict[int, list[int]] = {}
    for edition_id, copy_id in session.execute(
        select(Copy.edition_id, Copy.id).where(Copy.edition_id.in_(edition_ids)).order_by(Copy.id)
    ):
        copies_by_edition.setdefault(edition_id, []).append(copy_id)
    all_copies = [item for ids in copies_by_edition.values() for item in ids[:MAX_COPIES_PER_EDITION]]
    views = {view.id: view for view in copy_views(session, all_copies, "choice")}
    return [
        EditionMatch(
            edition_id,
            works[edition_id],
            cards.get(works[edition_id]),
            labels.get(edition_id, ""),
            tuple(
                views[item] for item in copies_by_edition.get(edition_id, [])[:MAX_COPIES_PER_EDITION] if item in views
            ),
        )
        for edition_id in edition_ids
        if edition_id in works
    ]


def _readers(session: Session, code: str, auth: AuthContext) -> list[ReaderMatch]:
    """Une carte ou un numéro de lecteur, avec les règles du comptoir : sans `patrons.view`, rien du tout."""
    if not auth.can("patrons.view"):
        return []
    visibility = Visibility(archived=auth.can("patrons.archive"), anonymized=auth.can("patrons.anonymize"))
    out: list[ReaderMatch] = []
    for candidate in circulation._reader_candidates(session, code, visibility):
        ended = candidate.kind == circulation_rules.KIND_ENDED_CARD
        row = circulation.reader_row(session, candidate.target_id, visibility)
        if row is None:
            continue
        out.append(ReaderMatch(row.id, "" if ended else row.name, "" if ended else row.number, ended))
    unique = {item.patron_id: item for item in out}
    return list(unique.values())


def _copy_by_code(session: Session, text: str) -> tuple[Copy | None, str]:
    found = holdings.find_copy_by_code(session, text)
    if found is None:
        return None, ""
    by = "barcode" if found.barcode and found.barcode.replace("-", "") == text.replace("-", "") else "inventory"
    return found, by


def identify(session: Session, raw: str, auth: AuthContext) -> Identification:
    """Ce que ce code désigne, sans rien écrire ni rien demander à Internet."""
    found = shape(raw)
    result = Identification(raw=raw, shape=found)
    if not found.text:
        return result
    copy, by = _copy_by_code(session, found.text) if found.internal else (None, "")
    editions = _isbn_editions(session, found) if found.is_book_number else []
    readers = _readers(session, found.text, auth) if found.internal else []
    if copy is not None:
        (result.copy,) = copy_views(session, [copy.id], by)
    result.editions, result.readers = editions, readers
    natures = sum(1 for present in (copy is not None, bool(editions), bool(readers)) if present)
    if natures > 1:
        result.kind = KIND_AMBIGUOUS
    elif copy is not None:
        result.kind = KIND_COPY
    elif found.is_book_number:
        result.kind = KIND_ISBN  # éditions trouvées, ou ISBN absent du catalogue : recherche proposée
    elif readers:
        result.kind = KIND_READER
    if result.kind == KIND_UNKNOWN and found.keyboard and found.keyboard != found.text:
        result.keyboard = found.keyboard
        again = shape(found.keyboard)
        result.keyboard_found = bool(
            (again.internal and holdings.find_copy_by_code(session, again.text) is not None)
            or _isbn_editions(session, again)
        )
    return result


def by_copy(session: Session, copy_id: int, auth: AuthContext) -> Identification:
    """Un exemplaire choisi dans une liste (après un ISBN ou une ambiguïté) : présenté comme un scan de son code."""
    views = copy_views(session, [copy_id], "choice")
    if not views:
        return Identification(raw="", shape=shape(""))
    view = views[0]
    result = Identification(raw=view.barcode or view.inventory, shape=shape(view.barcode or view.inventory))
    result.kind, result.copy = KIND_COPY, view
    return result
