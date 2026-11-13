# SPDX-License-Identifier: EUPL-1.2
"""Lecture : la fiche locale vue par l'enrichissement, son empreinte, l'historique et la provenance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.modules.catalogue import repository as catalogue
from biblyo.modules.catalogue.models import Edition, Tag
from biblyo.modules.catalogue.repository import EditionView, NoticeView
from biblyo.modules.enrichment.diffing import LocalValues
from biblyo.modules.enrichment.matching import LocalFiche
from biblyo.modules.enrichment.models import (
    EditionIsbnCandidate,
    EnrichmentDecision,
    EnrichmentProposal,
    EnrichmentSearch,
    FieldProvenance,
)


@dataclass(frozen=True)
class Target:
    """La fiche visée par un enrichissement : l'œuvre et l'une de ses éditions."""

    notice: NoticeView
    edition: EditionView
    fiche: LocalFiche
    values: LocalValues
    fingerprint: str


def known_genres(session: Session) -> tuple[str, ...]:
    return tuple(session.scalars(select(Tag.label).where(Tag.kind == "genre").order_by(Tag.label_norm)))


def load_target(session: Session, edition_id: int) -> Target | None:
    edition_row = session.get(Edition, edition_id)
    if edition_row is None:
        return None
    notice = catalogue.load_notice(session, edition_row.work_id)
    if notice is None:
        return None
    edition = next((e for e in notice.editions if e.id == edition_id), None)
    if edition is None:
        return None
    return Target(
        notice,
        edition,
        fiche_of(notice, edition),
        values_of(notice, edition, known_genres(session)),
        fingerprint_of(notice, edition),
    )


def fiche_of(notice: NoticeView, edition: EditionView) -> LocalFiche:
    return LocalFiche(
        title=notice.title,
        subtitle=notice.subtitle or "",
        contributors=tuple(c.name for c in notice.contributors),
        isbn13=edition.isbn13,
        publisher=edition.publisher.label if edition.publisher else "",
        year=edition.year,
        pages=edition.pages,
        collection=edition.collection.label if edition.collection else "",
        series=notice.series.title if notice.series else "",
        volume=notice.series.volume_number if notice.series else None,
        language=edition.language,
    )


def values_of(notice: NoticeView, edition: EditionView, genres: tuple[str, ...]) -> LocalValues:
    return LocalValues(
        title=notice.title,
        subtitle=notice.subtitle or "",
        contributors=tuple((c.name, c.role) for c in notice.contributors),
        series=notice.series.title if notice.series else "",
        volume=notice.series.volume_text if notice.series else "",
        summary=notice.summary or "",
        keywords=notice.keywords,
        genres=notice.genres,
        isbn13=edition.isbn13,
        publisher=edition.publisher.label if edition.publisher else "",
        collection=edition.collection.label if edition.collection else "",
        collection_number=edition.collection_number or "",
        year=str(edition.year) if edition.year else "",
        statement=edition.statement or "",
        language=edition.language,
        pages=str(edition.pages) if edition.pages else "",
        physical_note=edition.physical_note or "",
        has_cover=edition.cover is not None,
        cover_sha256=edition.cover.sha256 if edition.cover else "",
        known_genres=genres,
    )


def fingerprint_of(notice: NoticeView, edition: EditionView) -> str:
    """Empreinte de ce qu'un enrichissement peut lire ou changer : une modification entre-temps la change."""
    facts: dict[str, Any] = {
        "work": notice.version,
        "edition": edition.version,
        "title": notice.title,
        "subtitle": notice.subtitle,
        "contributors": [(c.agent_id, c.role) for c in notice.contributors],
        "summary": notice.summary,
        "tags": [notice.categories, notice.genres, notice.keywords],
        "series": (notice.series.series_id, notice.series.volume_text) if notice.series else None,
        "identifiers": list(edition.identifiers),
        "publisher": edition.publisher.id if edition.publisher else None,
        "collection": edition.collection.id if edition.collection else None,
        "fields": [
            edition.collection_number,
            edition.year,
            edition.statement,
            edition.language,
            edition.pages,
            edition.physical_note,
        ],
        "cover": edition.cover.sha256 if edition.cover else None,
    }
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def value_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def current_value(values: LocalValues, field: str) -> Any:
    """La valeur courante d'un champ, dans la forme conservée par les décisions."""
    if field == "contributors":
        return [list(pair) for pair in values.contributors]
    if field in ("keywords", "genres"):
        return list(getattr(values, field))
    if field == "isbn":
        return list(values.isbn13)
    if field == "cover":
        return values.cover_sha256
    return str(getattr(values, field, ""))


# --- Historique et provenance -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProvenanceView:
    entity_type: str
    field: str
    source_code: str
    external_id: str
    external_url: str
    sub_source: str | None
    license_note: str
    source_date: str | None
    retrieved_at: str
    changed_since: bool


def provenance(session: Session, values: LocalValues, work_id: int, edition_id: int) -> list[ProvenanceView]:
    rows = session.scalars(
        select(FieldProvenance).where(
            ((FieldProvenance.entity_type == "work") & (FieldProvenance.entity_id == work_id))
            | ((FieldProvenance.entity_type == "edition") & (FieldProvenance.entity_id == edition_id))
        )
    )
    return [
        ProvenanceView(
            row.entity_type,
            row.field,
            row.source_code,
            row.external_id,
            row.external_url,
            row.sub_source,
            row.license_note,
            row.source_date,
            row.retrieved_at,
            changed_since=value_hash(current_value(values, row.field)) != row.value_sha256,
        )
        for row in rows
    ]


@dataclass(frozen=True)
class DecisionView:
    id: int
    operation_id: str
    field: str
    entity_type: str
    decision: str
    action: str | None
    old_value: Any
    new_value: Any
    decided_at: str
    decided_by: int | None
    reverted_at: str | None


@dataclass(frozen=True)
class SearchView:
    id: int
    criteria: dict[str, Any]
    sources: list[dict[str, Any]]
    outcome: str
    created_at: str
    created_by: int | None
    proposals: tuple[ProposalView, ...]


@dataclass(frozen=True)
class ProposalView:
    id: int
    source_code: str
    external_id: str
    external_url: str
    group_key: str
    match_class: str
    match_level: int
    score: int
    evidence: int
    explanation: dict[str, Any]
    payload: dict[str, Any]
    status: str
    retrieved_at: str
    source_agency: str
    source_date: str | None
    decisions: tuple[DecisionView, ...]


def history(session: Session, edition_id: int | None, *, search_id: int | None = None) -> list[SearchView]:
    """Recherches d'une édition ; `None` : recherches libres, sans fiche (écran E3)."""
    if edition_id is None:
        statement = select(EnrichmentSearch).where(EnrichmentSearch.edition_id.is_(None))
    else:
        statement = select(EnrichmentSearch).where(EnrichmentSearch.edition_id == edition_id)
    if search_id is not None:
        statement = statement.where(EnrichmentSearch.id == search_id)
    searches = session.scalars(statement.order_by(EnrichmentSearch.created_at.desc(), EnrichmentSearch.id.desc())).all()
    ids = [s.id for s in searches]
    proposals = session.scalars(
        select(EnrichmentProposal).where(EnrichmentProposal.search_id.in_(ids)).order_by(EnrichmentProposal.id)
    ).all()
    decisions = session.scalars(
        select(EnrichmentDecision)
        .where(EnrichmentDecision.proposal_id.in_([p.id for p in proposals]))
        .order_by(EnrichmentDecision.id)
    ).all()
    by_proposal: dict[int, list[DecisionView]] = {}
    for d in decisions:
        by_proposal.setdefault(d.proposal_id, []).append(
            DecisionView(
                d.id,
                d.operation_id,
                d.field,
                d.entity_type,
                d.decision,
                d.action,
                json.loads(d.old_value) if d.old_value else None,
                json.loads(d.new_value) if d.new_value else None,
                d.decided_at,
                d.decided_by,
                d.reverted_at,
            )
        )
    by_search: dict[int, list[ProposalView]] = {}
    for p in proposals:
        by_search.setdefault(p.search_id, []).append(
            ProposalView(
                p.id,
                p.source_code,
                p.external_id,
                p.external_url,
                p.group_key,
                p.match_class,
                p.match_level,
                p.score,
                p.evidence,
                json.loads(p.explanation),
                json.loads(p.payload),
                p.status,
                p.retrieved_at,
                p.source_agency,
                p.source_date,
                tuple(by_proposal.get(p.id, [])),
            )
        )
    return [
        SearchView(
            s.id,
            json.loads(s.criteria),
            json.loads(s.sources),
            s.outcome,
            s.created_at,
            s.created_by,
            tuple(by_search.get(s.id, [])),
        )
        for s in searches
    ]


def isbn_candidates(session: Session, edition_id: int) -> list[EditionIsbnCandidate]:
    return list(
        session.scalars(
            select(EditionIsbnCandidate)
            .where(EditionIsbnCandidate.edition_id == edition_id)
            .order_by(EditionIsbnCandidate.status, EditionIsbnCandidate.id)
        )
    )
