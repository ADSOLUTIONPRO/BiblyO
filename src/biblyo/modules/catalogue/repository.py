# SPDX-License-Identifier: EUPL-1.2
"""Lecture du catalogue : des vues détachées de la session, que les gabarits peuvent lire sans requête cachée."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biblyo.core.text import normalize
from biblyo.modules.catalogue.models import (
    Agent,
    Collection,
    Copy,
    DocumentType,
    Edition,
    EditionContributor,
    EditionIdentifier,
    MediaFile,
    Publisher,
    SavedSearch,
    Section,
    Series,
    Tag,
    Work,
    WorkContributor,
    WorkSeries,
    WorkTag,
)
from biblyo.modules.catalogue.rules import (
    ACTIVE_COPY_EXCLUDED,
    AUDIENCES,
    CONTRIBUTOR_ROLES,
    COPY_STATUS_BADGES,
    COPY_STATUSES,
    LANGUAGES,
    code_of,
    completeness,
    cover_color,
    format_volume,
)
from biblyo.modules.holdings.models import Location
from biblyo.modules.holdings.rules import CopyState, WorkCounts, copy_state, count_states


@dataclass(frozen=True)
class NamedRef:
    id: int
    label: str


@dataclass(frozen=True)
class ContributorView:
    agent_id: int
    name: str
    dates: str | None
    role: str
    role_label: str
    position: int


@dataclass(frozen=True)
class CopyView:
    id: int
    public_id: str
    edition_id: int
    barcode: str
    status: str
    status_label: str
    notes: str | None
    created_at: str
    inventory_number: str = ""
    call_number: str | None = None
    location_id: int | None = None
    location_path: str | None = None
    position: int | None = None
    is_loanable: bool = True
    first_shelved_at: str | None = None
    version: int = 1
    due_on: str | None = None  # prêt en cours : l'échéance, jamais l'emprunteur

    @property
    def status_badge(self) -> str:
        return COPY_STATUS_BADGES.get(self.status, "neutral")

    @property
    def state(self) -> CopyState:
        """L'état de circulation, par la règle commune (holdings/rules.copy_state) : jamais l'emprunteur."""
        from biblyo.modules.holdings.availability import today

        return copy_state(self.status, self.is_loanable, self.due_on, today(), self.status_label)

    @property
    def availability_label(self) -> str:
        return self.state.label

    @property
    def is_loanable_now(self) -> bool:
        return self.state.is_loanable_now

    @property
    def inventory_differs(self) -> bool:
        return self.inventory_number != self.barcode

    @property
    def is_gone(self) -> bool:
        """Perdu ou retiré : l'exemplaire n'a plus de place, et ce n'est pas un oubli de rangement."""
        return self.status in ACTIVE_COPY_EXCLUDED

    @property
    def is_deletable(self) -> bool:
        """Créé par erreur et jamais rangé. Le service a le dernier mot : il consulte aussi le journal."""
        return self.status == "in_processing" and self.first_shelved_at is None and self.location_id is None


@dataclass(frozen=True)
class CoverView:
    sha256: str
    width: int | None
    height: int | None
    source: str | None = None  # fournisseur d'une couverture venue d'une source (révision 0009)
    retrieved_at: str | None = None
    origin_url: str | None = None  # notice de référence, ou adresse d'où l'image a été prise
    media_id: int | None = None
    origin: str = ""  # upload, url, bnf, openlibrary
    license_note: str = ""  # conditions de la source, ou droit d'utilisation déclaré

    @property
    def path(self) -> str:
        return f"/medias/{self.sha256}.webp"

    @property
    def thumbnail_path(self) -> str:
        return f"/medias/{self.sha256}-v.webp"


@dataclass(frozen=True)
class SeriesLink:
    series_id: int
    title: str
    volume_number: float | None
    volume_label: str | None
    call_sequence: int | None

    @property
    def volume_text(self) -> str:
        return format_volume(self.volume_number, self.volume_label)


@dataclass(frozen=True)
class EditionView:
    id: int
    public_id: str
    work_id: int
    is_primary: bool
    title_variant: str | None
    publisher: NamedRef | None
    collection: NamedRef | None
    collection_number: str | None
    year: int | None
    date: str | None
    statement: str | None
    language: str
    language_label: str
    pages: int | None
    physical_note: str | None
    status: str
    origin: str
    notes: str | None
    version: int
    identifiers: tuple[tuple[str, str], ...]
    contributors: tuple[ContributorView, ...]
    cover: CoverView | None
    copies: tuple[CopyView, ...]

    @property
    def isbn13(self) -> tuple[str, ...]:
        return tuple(value for scheme, value in self.identifiers if scheme == "isbn13")

    @property
    def isbn10(self) -> tuple[str, ...]:
        return tuple(value for scheme, value in self.identifiers if scheme == "isbn10")

    @property
    def isbn_text(self) -> str:
        """Saisie équivalente : un ISBN-13 par livre, l'ISBN-10 s'en déduit."""
        return ", ".join(self.isbn13)

    @property
    def active_copies(self) -> int:
        return sum(1 for copy in self.copies if copy.status not in ACTIVE_COPY_EXCLUDED)


@dataclass(frozen=True)
class DuplicateRef:
    work_id: int
    title: str
    value: str


@dataclass(frozen=True)
class NoticeView:
    id: int
    public_id: str
    title: str
    subtitle: str | None
    original_title: str | None
    document_type: NamedRef
    section: NamedRef | None
    audience: str | None
    audience_label: str
    summary: str | None
    notes: str | None
    status: str
    completeness: int
    missing: tuple[str, ...]
    version: int
    created_at: str
    updated_at: str
    contributors: tuple[ContributorView, ...]
    series: SeriesLink | None
    categories: tuple[str, ...]
    genres: tuple[str, ...]
    keywords: tuple[str, ...]
    editions: tuple[EditionView, ...]
    duplicates: tuple[DuplicateRef, ...] = field(default=())

    @property
    def primary(self) -> EditionView:
        return next((edition for edition in self.editions if edition.is_primary), self.editions[0])

    @property
    def copies(self) -> tuple[CopyView, ...]:
        return tuple(copy for edition in self.editions for copy in edition.copies)

    @property
    def has_copies_in_stock(self) -> bool:
        return any(not copy.is_gone for copy in self.copies)

    @property
    def availability(self) -> WorkCounts:
        """« 2 disponibles · 1 emprunté · 1 en retard » : la même règle que les cartes du catalogue."""
        return count_states(copy.state for copy in self.copies)

    @property
    def out_copies(self) -> tuple[CopyView, ...]:
        return tuple(copy for copy in self.copies if copy.state.is_out)

    @property
    def loanable_copies(self) -> tuple[CopyView, ...]:
        """Ceux qui peuvent sortir maintenant : en place, empruntables, pas déjà en prêt. « En place » ne suffit pas."""
        return tuple(copy for copy in self.copies if copy.is_loanable_now)

    @property
    def color(self) -> int:
        return cover_color(self.series.title if self.series else self.title)

    @property
    def authors_text(self) -> str:
        return ", ".join(contributor.name for contributor in self.contributors)

    @property
    def is_archived(self) -> bool:
        return self.status == "archived"


def _contributors_of(rows: Iterable[tuple[int, str, str | None, str, int]]) -> tuple[ContributorView, ...]:
    ordered = sorted(rows, key=lambda row: (row[4], row[1]))
    return tuple(
        ContributorView(agent_id, name, dates, role, CONTRIBUTOR_ROLES.get(role, role), position)
        for agent_id, name, dates, role, position in ordered
    )


def _load_editions(session: Session, work_id: int) -> tuple[EditionView, ...]:
    statement = (
        select(Edition, Publisher, Collection, MediaFile)
        .outerjoin(Publisher, Publisher.id == Edition.publisher_id)
        .outerjoin(Collection, Collection.id == Edition.collection_id)
        .outerjoin(MediaFile, MediaFile.id == Edition.cover_media_id)
        .where(Edition.work_id == work_id)
        .order_by(Edition.is_primary.desc(), Edition.publication_year.desc(), Edition.id)
    )
    rows = session.execute(statement).all()
    ids = [row[0].id for row in rows]

    identifiers: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for edition_id, scheme, value in session.execute(
        select(EditionIdentifier.edition_id, EditionIdentifier.scheme, EditionIdentifier.value)
        .where(EditionIdentifier.edition_id.in_(ids))
        .order_by(EditionIdentifier.id)
    ):
        identifiers[edition_id].append((scheme, value))

    people: dict[int, list[tuple[int, str, str | None, str, int]]] = defaultdict(list)
    for edition_id, agent_id, name, dates, role, position in session.execute(
        select(
            EditionContributor.edition_id,
            Agent.id,
            Agent.display_name,
            Agent.dates,
            EditionContributor.role,
            EditionContributor.position,
        )
        .join(Agent, Agent.id == EditionContributor.agent_id)
        .where(EditionContributor.edition_id.in_(ids))
    ):
        people[edition_id].append((agent_id, name, dates, role, position))

    copies: dict[int, list[CopyView]] = defaultdict(list)
    copy_rows = session.execute(
        select(Copy, Location.path)
        .outerjoin(Location, Location.id == Copy.location_id)
        .where(Copy.edition_id.in_(ids))
        .order_by(Copy.id)
    ).all()
    from biblyo.modules.holdings.repository import on_loan

    out = on_loan(session, [copy.id for copy, _path in copy_rows])
    for copy, path in copy_rows:
        copies[copy.edition_id].append(
            CopyView(
                copy.id,
                copy.public_id,
                copy.edition_id,
                code_of(copy.barcode, copy.inventory_number),
                copy.status,
                COPY_STATUSES.get(copy.status, copy.status),
                copy.notes,
                copy.created_at,
                copy.inventory_number,
                copy.call_number,
                copy.location_id,
                path,
                copy.shelf_position,
                copy.is_loanable,
                copy.first_shelved_at,
                copy.version,
                out[copy.id].due_on if copy.id in out else None,
            )
        )

    return tuple(
        EditionView(
            id=edition.id,
            public_id=edition.public_id,
            work_id=edition.work_id,
            is_primary=edition.is_primary,
            title_variant=edition.title_variant,
            publisher=NamedRef(publisher.id, publisher.name) if publisher else None,
            collection=NamedRef(collection.id, collection.name) if collection else None,
            collection_number=edition.collection_number,
            year=edition.publication_year,
            date=edition.publication_date,
            statement=edition.edition_statement,
            language=edition.language,
            language_label=LANGUAGES.get(edition.language, edition.language),
            pages=edition.page_count,
            physical_note=edition.physical_note,
            status=edition.status,
            origin=edition.origin,
            notes=edition.notes,
            version=edition.version,
            identifiers=tuple(identifiers[edition.id]),
            contributors=_contributors_of(people[edition.id]),
            cover=(
                CoverView(
                    media.sha256,
                    media.width,
                    media.height,
                    media.source_code,
                    media.retrieved_at,
                    media.origin_url,
                    media.id,
                    media.origin,
                    media.license_note or "",
                )
                if media
                else None
            ),
            copies=tuple(copies[edition.id]),
        )
        for edition, publisher, collection, media in rows
    )


def completeness_facts(session: Session, work: Work) -> dict[str, bool]:
    """Ce qui compte pour la complétude : l'œuvre et son édition principale."""
    edition = session.scalar(select(Edition).where(Edition.work_id == work.id, Edition.is_primary == True))  # noqa: E712
    has_isbn = False
    if edition is not None:
        count = session.scalar(
            select(func.count())
            .select_from(EditionIdentifier)
            .where(EditionIdentifier.edition_id == edition.id, EditionIdentifier.scheme.in_(("isbn13", "isbn10")))
        )
        has_isbn = bool(count)
    categories = session.scalar(
        select(func.count())
        .select_from(WorkTag)
        .join(Tag, Tag.id == WorkTag.tag_id)
        .where(WorkTag.work_id == work.id, Tag.kind == "category")
    )
    contributors = session.scalar(
        select(func.count()).select_from(WorkContributor).where(WorkContributor.work_id == work.id)
    )
    return {
        "isbn": has_isbn,
        "publisher": bool(edition and edition.publisher_id),
        "year": bool(edition and edition.publication_year),
        "summary": bool(work.summary),
        "cover": bool(edition and edition.cover_media_id),
        "categories": bool(categories),
        "contributors": bool(contributors),
        "pages": bool(edition and edition.page_count),
        "collection": bool(edition and edition.collection_id),
    }


def duplicates_of(session: Session, work_id: int) -> tuple[DuplicateRef, ...]:
    """Autres œuvres portant un même ISBN : signalées, jamais refusées."""
    mine = (
        select(EditionIdentifier.scheme, EditionIdentifier.value)
        .join(Edition, Edition.id == EditionIdentifier.edition_id)
        .where(Edition.work_id == work_id, EditionIdentifier.scheme.in_(("isbn13", "isbn10")))
    )
    pairs = {(scheme, value) for scheme, value in session.execute(mine)}
    if not pairs:
        return ()
    values = {value for _scheme, value in pairs}
    rows = session.execute(
        select(Work.id, Work.title, EditionIdentifier.value)
        .join(Edition, Edition.work_id == Work.id)
        .join(EditionIdentifier, EditionIdentifier.edition_id == Edition.id)
        .where(Work.id != work_id, EditionIdentifier.value.in_(values))
        .order_by(Work.title_sort)
    ).all()
    seen: set[int] = set()
    found = []
    for other_id, title, value in rows:
        if other_id not in seen:
            seen.add(other_id)
            found.append(DuplicateRef(other_id, title, value))
    return tuple(found)


def load_notice(session: Session, work_id: int) -> NoticeView | None:
    work = session.get(Work, work_id)
    if work is None:
        return None
    document_type = session.get(DocumentType, work.document_type_id)
    assert document_type is not None  # clé étrangère obligatoire
    section = session.get(Section, work.section_id) if work.section_id else None
    people = session.execute(
        select(Agent.id, Agent.display_name, Agent.dates, WorkContributor.role, WorkContributor.position)
        .join(Agent, Agent.id == WorkContributor.agent_id)
        .where(WorkContributor.work_id == work_id)
    ).all()
    link = session.execute(
        select(WorkSeries, Series)
        .join(Series, Series.id == WorkSeries.series_id)
        .where(WorkSeries.work_id == work_id, WorkSeries.is_primary == True)  # noqa: E712
    ).first()
    tags: dict[str, list[str]] = defaultdict(list)
    for kind, label in session.execute(
        select(Tag.kind, Tag.label)
        .join(WorkTag, WorkTag.tag_id == Tag.id)
        .where(WorkTag.work_id == work_id)
        .order_by(Tag.label_norm)
    ):
        tags[kind].append(label)
    _score, missing = completeness(completeness_facts(session, work))
    return NoticeView(
        id=work.id,
        public_id=work.public_id,
        title=work.title,
        subtitle=work.subtitle,
        original_title=work.original_title,
        document_type=NamedRef(document_type.id, document_type.label),
        section=NamedRef(section.id, section.label) if section else None,
        audience=work.audience,
        audience_label=AUDIENCES.get(work.audience or "", ""),
        summary=work.summary,
        notes=work.notes,
        status=work.status,
        completeness=work.completeness,
        missing=tuple(missing),
        version=work.version,
        created_at=work.created_at,
        updated_at=work.updated_at,
        contributors=_contributors_of(
            (agent_id, name, dates, role, position) for agent_id, name, dates, role, position in people
        ),
        series=(
            SeriesLink(link[1].id, link[1].title, link[0].volume_number, link[0].volume_label, link[0].call_sequence)
            if link
            else None
        ),
        categories=tuple(tags["category"]),
        genres=tuple(tags["genre"]),
        keywords=tuple(tags["keyword"]),
        editions=_load_editions(session, work_id),
        duplicates=duplicates_of(session, work_id),
    )


@dataclass(frozen=True)
class CardView:
    """Une œuvre dans une liste : la grille et le tableau lisent la même chose."""

    id: int
    title: str
    status: str
    type_label: str
    section_label: str
    series_id: int | None
    series_title: str
    volume_text: str
    authors_text: str
    color: int
    thumbnail: str
    edition_count: int
    copy_count: int
    completeness: int
    publisher: str
    year: int | None
    isbn: str
    created_at: str
    call_number: str = ""
    place: str = ""
    availability: WorkCounts | None = None  # compteurs de circulation, sans emprunteur (holdings/availability.py)

    @property
    def is_archived(self) -> bool:
        return self.status == "archived"


def load_cards(session: Session, work_ids: Sequence[int]) -> list[CardView]:
    """Cartes des œuvres demandées, dans l'ordre demandé, en quelques requêtes quel que soit leur nombre."""
    if not work_ids:
        return []
    ids = list(work_ids)
    works = session.execute(
        select(Work, DocumentType.label, Section.label)
        .join(DocumentType, DocumentType.id == Work.document_type_id)
        .outerjoin(Section, Section.id == Work.section_id)
        .where(Work.id.in_(ids))
    ).all()
    primary = session.execute(
        select(Edition.work_id, Edition.id, Edition.publication_year, Publisher.name, MediaFile.sha256)
        .outerjoin(Publisher, Publisher.id == Edition.publisher_id)
        .outerjoin(MediaFile, MediaFile.id == Edition.cover_media_id)
        .where(Edition.work_id.in_(ids), Edition.is_primary.is_(True))
    ).all()
    by_primary = {row[0]: row for row in primary}
    isbn: dict[int, str] = {}
    edition_to_work = {row[1]: row[0] for row in primary}
    for edition_id, value in session.execute(
        select(EditionIdentifier.edition_id, EditionIdentifier.value)
        .where(EditionIdentifier.edition_id.in_(list(edition_to_work)), EditionIdentifier.scheme == "isbn13")
        .order_by(EditionIdentifier.id.desc())
    ):
        isbn[edition_to_work[edition_id]] = value
    series = {
        row[0]: row
        for row in session.execute(
            select(WorkSeries.work_id, Series.id, Series.title, WorkSeries.volume_number, WorkSeries.volume_label)
            .join(Series, Series.id == WorkSeries.series_id)
            .where(WorkSeries.work_id.in_(ids), WorkSeries.is_primary.is_(True))
        )
    }
    authors: dict[int, list[str]] = defaultdict(list)
    for work_id, name in session.execute(
        select(WorkContributor.work_id, Agent.display_name)
        .join(Agent, Agent.id == WorkContributor.agent_id)
        .where(WorkContributor.work_id.in_(ids))
        .order_by(WorkContributor.position)
    ):
        if name not in authors[work_id]:
            authors[work_id].append(name)
    editions = dict(
        session.execute(
            select(Edition.work_id, func.count()).where(Edition.work_id.in_(ids)).group_by(Edition.work_id)
        ).all()
    )
    copies = dict(
        session.execute(
            select(Edition.work_id, func.count())
            .join(Copy, Copy.edition_id == Edition.id)
            .where(Edition.work_id.in_(ids))
            .group_by(Edition.work_id)
        ).all()
    )
    calls: dict[int, str] = {}
    places: dict[int, list[str]] = defaultdict(list)
    for work_id, call_number, path in session.execute(
        select(Edition.work_id, Copy.call_number, Location.path)
        .join(Copy, Copy.edition_id == Edition.id)
        .outerjoin(Location, Location.id == Copy.location_id)
        .where(Edition.work_id.in_(ids), Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
        .order_by(Copy.id)
    ):
        if call_number and work_id not in calls:
            calls[work_id] = call_number
        if path and path not in places[work_id]:
            places[work_id].append(path)
    from biblyo.modules.holdings.availability import for_works

    counts = for_works(session, ids)
    cards: dict[int, CardView] = {}
    for work, type_label, section_label in works:
        link = series.get(work.id)
        edition = by_primary.get(work.id)
        cards[work.id] = CardView(
            id=work.id,
            title=work.title,
            status=work.status,
            type_label=type_label,
            section_label=section_label or "",
            series_id=link[1] if link else None,
            series_title=link[2] if link else "",
            volume_text=format_volume(link[3], link[4]) if link else "",
            authors_text=", ".join(authors[work.id]),
            color=cover_color(link[2] if link else work.title),
            thumbnail=f"/medias/{edition[4]}-v.webp" if edition and edition[4] else "",
            edition_count=editions.get(work.id, 0),
            copy_count=copies.get(work.id, 0),
            completeness=work.completeness,
            publisher=(edition[3] or "") if edition else "",
            year=edition[2] if edition else None,
            isbn=isbn.get(work.id, ""),
            created_at=work.created_at,
            call_number=calls.get(work.id, ""),
            place=_place_text(places.get(work.id, [])),
            availability=counts.get(work.id),
        )
    return [cards[work_id] for work_id in ids if work_id in cards]


def _place_text(paths: Sequence[str]) -> str:
    """Le premier emplacement, et combien d'autres : la liste reste lisible, la fiche donne le détail."""
    if not paths:
        return ""
    return paths[0] if len(paths) == 1 else f"{paths[0]} (+{len(paths) - 1})"


def active_work_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Work).where(Work.status == "active")) or 0


def document_types(session: Session, *, include_id: int | None = None) -> Sequence[DocumentType]:
    statement = select(DocumentType).order_by(DocumentType.sort_order, DocumentType.label)
    rows = session.scalars(statement).all()
    return [row for row in rows if row.is_active or row.id == include_id]


def sections(session: Session, *, include_id: int | None = None) -> Sequence[Section]:
    rows = session.scalars(select(Section).order_by(Section.sort_order, Section.label)).all()
    return [row for row in rows if row.is_active or row.id == include_id]


def homonym_ids(session: Session, agents: Sequence[Agent]) -> set[int]:
    """Parmi ces personnes, celles dont le nom est porté par une autre fiche."""
    names = {agent.name_norm for agent in agents}
    if not names:
        return set()
    shared = set(
        session.scalars(
            select(Agent.name_norm).where(Agent.name_norm.in_(names)).group_by(Agent.name_norm).having(func.count() > 1)
        )
    )
    return {agent.id for agent in agents if agent.name_norm in shared}


def agents_named(session: Session, name: str) -> Sequence[Agent]:
    """Personnes portant exactement ce nom, sans tenir compte des accents ni de la casse : les homonymes."""
    return session.scalars(select(Agent).where(Agent.name_norm == normalize(name)).order_by(Agent.id)).all()


def works_of_agent(session: Session, agent_id: int) -> list[int]:
    """Œuvres d'une personne, sur l'œuvre ou sur une édition, dans l'ordre du catalogue. Les archivées sont exclues."""
    on_edition = (
        select(Edition.work_id)
        .join(EditionContributor, EditionContributor.edition_id == Edition.id)
        .where(EditionContributor.agent_id == agent_id)
    )
    on_work = select(WorkContributor.work_id).where(WorkContributor.agent_id == agent_id)
    statement = (
        select(Work.id)
        .where(Work.status == "active", Work.id.in_(on_work.union(on_edition)))
        .order_by(Work.title_sort, Work.id)
    )
    return list(session.scalars(statement))


def agent_work_counts(session: Session, agent_ids: Iterable[int]) -> dict[int, int]:
    ids = list(agent_ids)
    rows = session.execute(
        select(WorkContributor.agent_id, func.count(func.distinct(WorkContributor.work_id)))
        .where(WorkContributor.agent_id.in_(ids))
        .group_by(WorkContributor.agent_id)
    )
    return dict(rows.all())


@dataclass(frozen=True)
class SavedSearchView:
    id: int
    name: str
    criteria: dict[str, str]


def saved_searches(session: Session, user_id: int) -> list[SavedSearchView]:
    rows = session.scalars(select(SavedSearch).where(SavedSearch.user_id == user_id).order_by(SavedSearch.name))
    return [SavedSearchView(row.id, row.name, json.loads(row.criteria)) for row in rows]
