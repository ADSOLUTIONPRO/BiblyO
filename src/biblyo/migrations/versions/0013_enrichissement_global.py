# SPDX-License-Identifier: EUPL-1.2
"""Enrichissement de toute la bibliothèque (lot L7, tâche T4 ; conception 22).

Révision 0013. Deux tables **nouvelles**, aucune table existante n'est modifiée, aucune donnée n'est écrite :

- `enrichment_jobs` : une tâche de recherche par lot (qui l'a lancée, quand, son état : en cours, en pause,
  interrompue par un redémarrage, terminée, annulée), ses compteurs et sa dernière erreur ;
- `enrichment_job_items` : une ligne par **édition** visée par une tâche (jamais par exemplaire : unicité
  `(job_id, edition_id)`), son état, la recherche enregistrée qu'elle a produite (`enrichment_searches`), le niveau
  de correspondance obtenu et si une couverture est annoncée.

La tâche ne fait que **rechercher** : elle n'applique rien. Les propositions qu'elle prépare sont validées ensuite à
l'écran, par les opérations d'enrichissement existantes (traçables et annulables).

Descente : **refusée** dès qu'une tâche existe (son historique serait perdu) ; la voie de retour est alors la
sauvegarde prise avant la migration. Sans tâche, les deux tables sont supprimées.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

JOB_STATUSES = ("running", "paused", "interrupted", "finished", "cancelled")
ITEM_STATUSES = ("pending", "done", "error")
TIERS = ("reliable", "plausible", "insufficient")


def _in(values: tuple[str, ...]) -> str:
    return ",".join(repr(value) for value in values)


def upgrade() -> None:
    op.execute(
        f"""CREATE TABLE enrichment_jobs (
        id INTEGER NOT NULL,
        public_id TEXT NOT NULL,
        status TEXT NOT NULL,
        scope TEXT NOT NULL,
        total INTEGER DEFAULT 0 NOT NULL,
        processed INTEGER DEFAULT 0 NOT NULL,
        errors INTEGER DEFAULT 0 NOT NULL,
        last_error TEXT,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        CONSTRAINT pk_enrichment_jobs PRIMARY KEY (id),
        CONSTRAINT uq_enrichment_jobs_public_id UNIQUE (public_id),
        CONSTRAINT ck_enrichment_jobs_status_values CHECK (status IN ({_in(JOB_STATUSES)})),
        CONSTRAINT ck_enrichment_jobs_counts CHECK (total >= 0 AND processed >= 0 AND errors >= 0),
        CONSTRAINT fk_enrichment_jobs_created_by_users FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL
    ) STRICT"""
    )
    op.execute("CREATE INDEX ix_enrichment_jobs_status ON enrichment_jobs (status)")
    op.execute(
        f"""CREATE TABLE enrichment_job_items (
        id INTEGER NOT NULL,
        job_id INTEGER NOT NULL,
        edition_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        status TEXT DEFAULT 'pending' NOT NULL,
        search_id INTEGER,
        tier TEXT,
        cover_announced INTEGER DEFAULT 0 NOT NULL,
        attempts INTEGER DEFAULT 0 NOT NULL,
        error TEXT,
        updated_at TEXT NOT NULL,
        CONSTRAINT pk_enrichment_job_items PRIMARY KEY (id),
        CONSTRAINT uq_enrichment_job_items_job_id_edition_id UNIQUE (job_id, edition_id),
        CONSTRAINT ck_enrichment_job_items_status_values CHECK (status IN ({_in(ITEM_STATUSES)})),
        CONSTRAINT ck_enrichment_job_items_tier_values CHECK (tier IS NULL OR tier IN ({_in(TIERS)})),
        CONSTRAINT ck_enrichment_job_items_cover_announced_bool CHECK (cover_announced IN (0, 1)),
        CONSTRAINT fk_enrichment_job_items_job_id_enrichment_jobs
            FOREIGN KEY (job_id) REFERENCES enrichment_jobs (id) ON DELETE CASCADE,
        CONSTRAINT fk_enrichment_job_items_edition_id_editions
            FOREIGN KEY (edition_id) REFERENCES editions (id) ON DELETE CASCADE,
        CONSTRAINT fk_enrichment_job_items_search_id_enrichment_searches
            FOREIGN KEY (search_id) REFERENCES enrichment_searches (id) ON DELETE SET NULL
    ) STRICT"""
    )
    op.execute("CREATE INDEX ix_enrichment_job_items_job_id_status ON enrichment_job_items (job_id, status, position)")
    op.execute("CREATE INDEX ix_enrichment_job_items_edition_id ON enrichment_job_items (edition_id)")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SELECT COUNT(*) FROM enrichment_jobs")).scalar():
        raise RuntimeError(
            "Descente refusée : des tâches d'enrichissement existent. Restaurez la sauvegarde prise avant la migration."
        )
    op.execute("DROP TABLE enrichment_job_items")
    op.execute("DROP TABLE enrichment_jobs")
