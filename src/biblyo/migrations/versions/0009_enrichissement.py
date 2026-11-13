# SPDX-License-Identifier: EUPL-1.2
"""Sources bibliographiques, recherches, propositions, décisions et provenance (lot L5, plan § 11).

Révision 0009. Elle **crée** sept tables, ajoute quatre colonnes facultatives de provenance aux couvertures
(`media_files`) et inscrit les deux sources fournies : la BnF (priorité 1, couvertures activées) et le Sudoc
(priorité 2). Aucune ligne existante n'est réécrite. Les permissions `enrichment.*` et `admin.sources` existent
depuis la révision 0001 : aucun rôle n'est touché.

Un ISBN proposé ou rejeté vit dans `edition_isbn_candidates`, jamais dans `edition_identifiers` : seul un ISBN
confirmé entre dans la recherche, le contrôle des doublons et les exports.

Descente : **refusée dès qu'une décision d'enrichissement, une provenance ou une couverture de source existe**. La
voie de retour est alors la sauvegarde prise avant la migration. Sinon, elle défait exactement ce qu'elle a créé ;
les recherches et propositions non appliquées sont perdues, ce qui est dit au journal.
"""

from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

log = logging.getLogger("biblyo.migrate")

#: code, libellé, activée, priorité, intervalle interactif, intervalle en lot, délai, couvertures (D3, D4, D13, D16)
SOURCES = (
    ("bnf", "BnF, catalogue général", 1, 1, 1000, 5000, 10000, 1),
    ("sudoc", "Sudoc (Abes)", 1, 2, 1000, 2000, 15000, 0),
)
MEDIA_COLUMNS = ("source_code", "external_id", "license_note", "retrieved_at")
STATEMENTS = (
    """CREATE TABLE metadata_sources (
        code TEXT NOT NULL,
        label TEXT NOT NULL,
        is_enabled INTEGER NOT NULL DEFAULT 0,
        priority INTEGER NOT NULL,
        interval_ms INTEGER NOT NULL,
        batch_interval_ms INTEGER NOT NULL,
        timeout_ms INTEGER NOT NULL,
        covers_enabled INTEGER NOT NULL DEFAULT 0,
        config TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        updated_by INTEGER,
        CONSTRAINT pk_metadata_sources PRIMARY KEY (code),
        CONSTRAINT uq_metadata_sources_priority UNIQUE (priority),
        CONSTRAINT fk_metadata_sources_updated_by_users FOREIGN KEY (updated_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_metadata_sources_is_enabled_bool CHECK (is_enabled IN (0, 1)),
        CONSTRAINT ck_metadata_sources_priority_positive CHECK (priority >= 1),
        CONSTRAINT ck_metadata_sources_interval_floor CHECK (interval_ms >= 250),
        CONSTRAINT ck_metadata_sources_batch_interval_floor CHECK (batch_interval_ms >= 1000),
        CONSTRAINT ck_metadata_sources_timeout_range CHECK (timeout_ms BETWEEN 1000 AND 60000),
        CONSTRAINT ck_metadata_sources_covers_enabled_bool CHECK (covers_enabled IN (0, 1))
    ) STRICT""",
    """CREATE TABLE source_cache (
        id INTEGER NOT NULL,
        source_code TEXT NOT NULL,
        query_key TEXT NOT NULL,
        status TEXT NOT NULL,
        total INTEGER NOT NULL DEFAULT 0,
        body TEXT,
        fetched_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        CONSTRAINT pk_source_cache PRIMARY KEY (id),
        CONSTRAINT uq_source_cache_source_code_query_key UNIQUE (source_code, query_key),
        CONSTRAINT fk_source_cache_source_code_metadata_sources
            FOREIGN KEY (source_code) REFERENCES metadata_sources (code) ON DELETE CASCADE,
        CONSTRAINT ck_source_cache_status_values CHECK (status IN ('found','not_found'))
    ) STRICT""",
    "CREATE INDEX ix_source_cache_expires_at ON source_cache (expires_at)",
    """CREATE TABLE enrichment_searches (
        id INTEGER NOT NULL,
        edition_id INTEGER,
        criteria TEXT NOT NULL,
        sources TEXT NOT NULL,
        outcome TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        CONSTRAINT pk_enrichment_searches PRIMARY KEY (id),
        CONSTRAINT fk_enrichment_searches_edition_id_editions
            FOREIGN KEY (edition_id) REFERENCES editions (id) ON DELETE CASCADE,
        CONSTRAINT fk_enrichment_searches_created_by_users FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_enrichment_searches_outcome_values
            CHECK (outcome IN ('exact','strong','uncertain','multiple','none','incomplete'))
    ) STRICT""",
    "CREATE INDEX ix_enrichment_searches_edition_id_created_at ON enrichment_searches (edition_id, created_at)",
    """CREATE TABLE enrichment_proposals (
        id INTEGER NOT NULL,
        search_id INTEGER NOT NULL,
        source_code TEXT NOT NULL,
        external_id TEXT NOT NULL,
        external_url TEXT NOT NULL,
        group_key TEXT NOT NULL,
        match_class TEXT NOT NULL,
        match_level INTEGER NOT NULL,
        score INTEGER NOT NULL,
        evidence INTEGER NOT NULL,
        explanation TEXT NOT NULL,
        payload TEXT NOT NULL,
        raw_record TEXT NOT NULL,
        source_agency TEXT NOT NULL DEFAULT '',
        source_date TEXT,
        retrieved_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        decided_by INTEGER,
        decided_at TEXT,
        CONSTRAINT pk_enrichment_proposals PRIMARY KEY (id),
        CONSTRAINT fk_enrichment_proposals_search_id_enrichment_searches
            FOREIGN KEY (search_id) REFERENCES enrichment_searches (id) ON DELETE CASCADE,
        CONSTRAINT fk_enrichment_proposals_decided_by_users FOREIGN KEY (decided_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_enrichment_proposals_match_class_values
            CHECK (match_class IN ('exact','strong','uncertain','multiple','none','excluded')),
        CONSTRAINT ck_enrichment_proposals_match_level_range CHECK (match_level BETWEEN 1 AND 5),
        CONSTRAINT ck_enrichment_proposals_score_range CHECK (score BETWEEN 0 AND 100),
        CONSTRAINT ck_enrichment_proposals_evidence_range CHECK (evidence BETWEEN 0 AND 100),
        CONSTRAINT ck_enrichment_proposals_status_values
            CHECK (status IN ('pending','applied','partial','rejected','stale','reverted'))
    ) STRICT""",
    "CREATE INDEX ix_enrichment_proposals_search_id ON enrichment_proposals (search_id)",
    "CREATE INDEX ix_enrichment_proposals_status_match_class_score ON enrichment_proposals (status, match_class, score)",
    "CREATE INDEX ix_enrichment_proposals_source_code_external_id ON enrichment_proposals (source_code, external_id)",
    """CREATE TABLE enrichment_decisions (
        id INTEGER NOT NULL,
        proposal_id INTEGER NOT NULL,
        operation_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        field TEXT NOT NULL,
        decision TEXT NOT NULL,
        action TEXT,
        old_value TEXT,
        new_value TEXT,
        decided_by INTEGER,
        decided_at TEXT NOT NULL,
        reverted_at TEXT,
        reverted_by INTEGER,
        CONSTRAINT pk_enrichment_decisions PRIMARY KEY (id),
        CONSTRAINT fk_enrichment_decisions_proposal_id_enrichment_proposals
            FOREIGN KEY (proposal_id) REFERENCES enrichment_proposals (id) ON DELETE RESTRICT,
        CONSTRAINT fk_enrichment_decisions_decided_by_users FOREIGN KEY (decided_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT fk_enrichment_decisions_reverted_by_users FOREIGN KEY (reverted_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_enrichment_decisions_entity_type_values CHECK (entity_type IN ('work','edition')),
        CONSTRAINT ck_enrichment_decisions_decision_values CHECK (decision IN ('accepted','rejected')),
        CONSTRAINT ck_enrichment_decisions_action_values CHECK (action IS NULL OR action IN ('fill','replace','add')),
        CONSTRAINT ck_enrichment_decisions_action_when_accepted CHECK ((decision = 'accepted') = (action IS NOT NULL))
    ) STRICT""",
    "CREATE INDEX ix_enrichment_decisions_entity_type_entity_id_decided_at"
    " ON enrichment_decisions (entity_type, entity_id, decided_at)",
    "CREATE INDEX ix_enrichment_decisions_operation_id ON enrichment_decisions (operation_id)",
    """CREATE TABLE edition_isbn_candidates (
        id INTEGER NOT NULL,
        edition_id INTEGER NOT NULL,
        isbn13 TEXT NOT NULL,
        status TEXT NOT NULL,
        source_code TEXT NOT NULL,
        external_id TEXT NOT NULL,
        external_url TEXT NOT NULL,
        score INTEGER NOT NULL,
        agreeing TEXT NOT NULL DEFAULT '[]',
        conflicting TEXT NOT NULL DEFAULT '[]',
        proposal_id INTEGER,
        created_at TEXT NOT NULL,
        decided_by INTEGER,
        decided_at TEXT,
        CONSTRAINT pk_edition_isbn_candidates PRIMARY KEY (id),
        CONSTRAINT uq_edition_isbn_candidates_edition_id_isbn13 UNIQUE (edition_id, isbn13),
        CONSTRAINT fk_edition_isbn_candidates_edition_id_editions
            FOREIGN KEY (edition_id) REFERENCES editions (id) ON DELETE CASCADE,
        CONSTRAINT fk_edition_isbn_candidates_proposal_id_enrichment_proposals
            FOREIGN KEY (proposal_id) REFERENCES enrichment_proposals (id) ON DELETE SET NULL,
        CONSTRAINT fk_edition_isbn_candidates_decided_by_users
            FOREIGN KEY (decided_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_edition_isbn_candidates_status_values CHECK (status IN ('proposed','rejected','confirmed')),
        CONSTRAINT ck_edition_isbn_candidates_score_range CHECK (score BETWEEN 0 AND 100)
    ) STRICT""",
    "CREATE INDEX ix_edition_isbn_candidates_isbn13 ON edition_isbn_candidates (isbn13)",
    """CREATE TABLE field_provenance (
        entity_type TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        field TEXT NOT NULL,
        source_code TEXT NOT NULL,
        external_id TEXT NOT NULL,
        external_url TEXT NOT NULL,
        sub_source TEXT,
        reuse_status TEXT NOT NULL DEFAULT 'established',
        license_note TEXT NOT NULL,
        source_date TEXT,
        retrieved_at TEXT NOT NULL,
        value_sha256 TEXT NOT NULL,
        decision_id INTEGER NOT NULL,
        CONSTRAINT pk_field_provenance PRIMARY KEY (entity_type, entity_id, field),
        CONSTRAINT fk_field_provenance_decision_id_enrichment_decisions
            FOREIGN KEY (decision_id) REFERENCES enrichment_decisions (id) ON DELETE RESTRICT,
        CONSTRAINT ck_field_provenance_entity_type_values CHECK (entity_type IN ('work','edition')),
        CONSTRAINT ck_field_provenance_reuse_status_values
            CHECK (reuse_status IN ('established','third_party_allowed'))
    ) STRICT""",
    "ALTER TABLE media_files ADD COLUMN source_code TEXT",
    "ALTER TABLE media_files ADD COLUMN external_id TEXT",
    "ALTER TABLE media_files ADD COLUMN license_note TEXT",
    "ALTER TABLE media_files ADD COLUMN retrieved_at TEXT",
)
TABLES = (
    "field_provenance",
    "edition_isbn_candidates",
    "enrichment_decisions",
    "enrichment_proposals",
    "enrichment_searches",
    "source_cache",
    "metadata_sources",
)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _journal(bind: sa.engine.Connection, action: str, entity: str, details: dict[str, object]) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO audit_log (at, user_id, action, entity_type, entity_id, details)"
            " VALUES (:at, NULL, :action, :entity, NULL, :details)"
        ),
        {"at": _now(), "action": action, "entity": entity, "details": json.dumps(details, ensure_ascii=False)},
    )


def upgrade() -> None:
    bind = op.get_bind()
    for statement in STATEMENTS:
        bind.exec_driver_sql(statement)
    moment = _now()
    for code, label, enabled, priority, interval, batch_interval, timeout, covers in SOURCES:
        bind.execute(
            sa.text(
                "INSERT INTO metadata_sources (code, label, is_enabled, priority, interval_ms, batch_interval_ms,"
                " timeout_ms, covers_enabled, config, updated_at)"
                " VALUES (:code, :label, :enabled, :priority, :interval, :batch, :timeout, :covers, '{}', :at)"
            ),
            {
                "code": code,
                "label": label,
                "enabled": enabled,
                "priority": priority,
                "interval": interval,
                "batch": batch_interval,
                "timeout": timeout,
                "covers": covers,
                "at": moment,
            },
        )
    _journal(
        bind, "migration.sources_created", "metadata_source", {"revision": revision, "sources": [s[0] for s in SOURCES]}
    )


def downgrade() -> None:
    bind = op.get_bind()
    counts = {
        "décisions d'enrichissement": "SELECT COUNT(*) FROM enrichment_decisions",
        "provenances de champ": "SELECT COUNT(*) FROM field_provenance",
        "ISBN confirmés par l'enrichissement": "SELECT COUNT(*) FROM edition_isbn_candidates WHERE status = 'confirmed'",
        "couvertures venues d'une source": "SELECT COUNT(*) FROM media_files WHERE source_code IS NOT NULL",
    }
    filled = {name: count for name, query in counts.items() if (count := bind.exec_driver_sql(query).scalar())}
    if filled:
        detail = ", ".join(f"{name} : {count}" for name, count in filled.items())
        raise RuntimeError(
            f"Des données d'enrichissement existent ({detail}). Elles ne sont jamais supprimées par un retour "
            "arrière : restaurez la sauvegarde prise avant la migration."
        )
    searches = bind.exec_driver_sql("SELECT COUNT(*) FROM enrichment_searches").scalar() or 0
    if searches:
        log.warning("Retour arrière : %d recherche(s) et leurs propositions non appliquées sont perdues.", searches)
        _journal(
            bind, "migration.searches_dropped", "enrichment_search", {"revision": revision, "recherches": searches}
        )
    for table in TABLES:
        bind.exec_driver_sql(f"DROP TABLE {table}")
    for column in reversed(MEDIA_COLUMNS):
        bind.exec_driver_sql(f"ALTER TABLE media_files DROP COLUMN {column}")
