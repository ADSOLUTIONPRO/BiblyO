# SPDX-License-Identifier: EUPL-1.2
"""Sources, cache, recherches, propositions, décisions et provenance (révision 0009, plan de L5 § 11).

Rien ici ne modifie une table du catalogue : l'enrichissement écrit ses traces à côté, et ne touche aux œuvres et
aux éditions que par le service du catalogue. `edition_isbn_candidates` tient les ISBN proposés ou rejetés à l'écart
de `edition_identifiers`.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from biblyo.core.models_base import TABLE_OPTIONS, Base, IntBool

MATCH_CLASSES = ("exact", "strong", "uncertain", "multiple", "none", "excluded")
OUTCOMES = ("exact", "strong", "uncertain", "multiple", "none", "incomplete")
PROPOSAL_STATUSES = ("pending", "applied", "partial", "rejected", "stale", "reverted")


def _values(name: str, values: tuple[str, ...]) -> str:
    return f"{name} IN ({','.join(repr(v) for v in values)})"


class MetadataSource(Base):
    """Une source bibliographique et ses réglages : activée, priorité, cadences, délai, couvertures."""

    __tablename__ = "metadata_sources"
    __table_args__ = (
        UniqueConstraint("priority", name="uq_metadata_sources_priority"),
        CheckConstraint("is_enabled IN (0, 1)", name="is_enabled_bool"),
        CheckConstraint("priority >= 1", name="priority_positive"),
        CheckConstraint("interval_ms >= 250", name="interval_floor"),
        CheckConstraint("batch_interval_ms >= 1000", name="batch_interval_floor"),
        CheckConstraint("timeout_ms BETWEEN 1000 AND 60000", name="timeout_range"),
        CheckConstraint("covers_enabled IN (0, 1)", name="covers_enabled_bool"),
        TABLE_OPTIONS,
    )

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    covers_enabled: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    config: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class SourceCache(Base):
    """Réponse nettoyée d'une source, par requête normalisée. Les pannes ne sont jamais mises en cache."""

    __tablename__ = "source_cache"
    __table_args__ = (
        UniqueConstraint("source_code", "query_key", name="uq_source_cache_source_code_query_key"),
        CheckConstraint("status IN ('found','not_found')", name="status_values"),
        Index("ix_source_cache_expires_at", "expires_at"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_code: Mapped[str] = mapped_column(
        Text, ForeignKey("metadata_sources.code", ondelete="CASCADE"), nullable=False
    )
    query_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    body: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)


class EnrichmentSearch(Base):
    """Une recherche lancée : ses critères, l'état de chaque source, son résultat, l'empreinte de la fiche."""

    __tablename__ = "enrichment_searches"
    __table_args__ = (
        CheckConstraint(_values("outcome", OUTCOMES), name="outcome_values"),
        Index("ix_enrichment_searches_edition_id_created_at", "edition_id", "created_at"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("editions.id", ondelete="CASCADE"))
    criteria: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class EnrichmentProposal(Base):
    """Un candidat retenu pour une recherche : classe, score expliqué, candidat normalisé, notice d'origine."""

    __tablename__ = "enrichment_proposals"
    __table_args__ = (
        CheckConstraint(_values("match_class", MATCH_CLASSES), name="match_class_values"),
        CheckConstraint("match_level BETWEEN 1 AND 5", name="match_level_range"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        CheckConstraint("evidence BETWEEN 0 AND 100", name="evidence_range"),
        CheckConstraint(_values("status", PROPOSAL_STATUSES), name="status_values"),
        Index("ix_enrichment_proposals_search_id", "search_id"),
        Index("ix_enrichment_proposals_status_match_class_score", "status", "match_class", "score"),
        Index("ix_enrichment_proposals_source_code_external_id", "source_code", "external_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("enrichment_searches.id", ondelete="CASCADE"), nullable=False
    )
    source_code: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_url: Mapped[str] = mapped_column(Text, nullable=False)
    group_key: Mapped[str] = mapped_column(Text, nullable=False)
    match_class: Mapped[str] = mapped_column(Text, nullable=False)
    match_level: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence: Mapped[int] = mapped_column(Integer, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    raw_record: Mapped[str] = mapped_column(Text, nullable=False)
    source_agency: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_date: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    decided_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[str | None] = mapped_column(Text)


class EnrichmentDecision(Base):
    """Un champ examiné lors d'une confirmation : accepté, avec ses deux valeurs, ou refusé."""

    __tablename__ = "enrichment_decisions"
    __table_args__ = (
        CheckConstraint("entity_type IN ('work','edition')", name="entity_type_values"),
        CheckConstraint("decision IN ('accepted','rejected')", name="decision_values"),
        CheckConstraint("action IS NULL OR action IN ('fill','replace','add')", name="action_values"),
        CheckConstraint("(decision = 'accepted') = (action IS NOT NULL)", name="action_when_accepted"),
        Index("ix_enrichment_decisions_entity_type_entity_id_decided_at", "entity_type", "entity_id", "decided_at"),
        Index("ix_enrichment_decisions_operation_id", "operation_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("enrichment_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str | None] = mapped_column(Text)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[str] = mapped_column(Text, nullable=False)
    reverted_at: Mapped[str | None] = mapped_column(Text)
    reverted_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class EditionIsbnCandidate(Base):
    """ISBN proposé, rejeté ou confirmé par l'enrichissement. Jamais lu par la recherche ni par les doublons."""

    __tablename__ = "edition_isbn_candidates"
    __table_args__ = (
        UniqueConstraint("edition_id", "isbn13", name="uq_edition_isbn_candidates_edition_id_isbn13"),
        CheckConstraint("status IN ('proposed','rejected','confirmed')", name="status_values"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        Index("ix_edition_isbn_candidates_isbn13", "isbn13"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(Integer, ForeignKey("editions.id", ondelete="CASCADE"), nullable=False)
    isbn13: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source_code: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_url: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    agreeing: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    conflicting: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    proposal_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("enrichment_proposals.id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[str | None] = mapped_column(Text)


class FieldProvenance(Base):
    """Provenance courante d'un champ accepté : source, notice, fournisseur, conditions, dates, empreinte."""

    __tablename__ = "field_provenance"
    __table_args__ = (
        CheckConstraint("entity_type IN ('work','edition')", name="entity_type_values"),
        CheckConstraint("reuse_status IN ('established','third_party_allowed')", name="reuse_status_values"),
        TABLE_OPTIONS,
    )

    entity_type: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    source_code: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_url: Mapped[str] = mapped_column(Text, nullable=False)
    sub_source: Mapped[str | None] = mapped_column(Text)
    reuse_status: Mapped[str] = mapped_column(Text, nullable=False, default="established")
    license_note: Mapped[str] = mapped_column(Text, nullable=False)
    source_date: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[str] = mapped_column(Text, nullable=False)
    value_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    decision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("enrichment_decisions.id", ondelete="RESTRICT"), nullable=False
    )


JOB_STATUSES = ("running", "paused", "interrupted", "finished", "cancelled")
JOB_ITEM_STATUSES = ("pending", "done", "error")
JOB_TIERS = ("reliable", "plausible", "insufficient")


class EnrichmentJob(Base):
    """Une recherche par lot sur des éditions (révision 0013) : elle prépare des propositions, n'applique rien."""

    __tablename__ = "enrichment_jobs"
    __table_args__ = (
        UniqueConstraint("public_id"),
        CheckConstraint(_values("status", JOB_STATUSES), name="status_values"),
        CheckConstraint("total >= 0 AND processed >= 0 AND errors >= 0", name="counts"),
        Index("ix_enrichment_jobs_status", "status"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text)


class EnrichmentJobItem(Base):
    """Une édition dans une tâche : une seule fois par tâche, jamais un exemplaire."""

    __tablename__ = "enrichment_job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "edition_id"),
        CheckConstraint(_values("status", JOB_ITEM_STATUSES), name="status_values"),
        CheckConstraint(f"tier IS NULL OR {_values('tier', JOB_TIERS)}", name="tier_values"),
        CheckConstraint("cover_announced IN (0, 1)", name="cover_announced_bool"),
        Index("ix_enrichment_job_items_job_id_status", "job_id", "status", "position"),
        Index("ix_enrichment_job_items_edition_id", "edition_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(Integer, ForeignKey("enrichment_jobs.id", ondelete="CASCADE"), nullable=False)
    edition_id: Mapped[int] = mapped_column(Integer, ForeignKey("editions.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    search_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("enrichment_searches.id", ondelete="SET NULL"))
    tier: Mapped[str | None] = mapped_column(Text)
    cover_announced: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
