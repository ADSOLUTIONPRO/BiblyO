# SPDX-License-Identifier: EUPL-1.2
"""Tables de l'import (lot L6, tâche T1 ; conceptions 03 § 3.8, 19 et 21).

Révision 0011. Elle **crée** `import_profiles`, `import_batches`, `import_rows`, `import_links` et `import_proposals`.
Aucune table existante n'est touchée, aucune ligne n'est écrite. Un même fichier ne peut être appliqué qu'une fois :
l'index unique partiel `ux_import_batches_applied_file` le garantit dans la base elle-même.

Descente : **refusée dès qu'un lot ou une proposition d'import existe** ; la voie de retour est alors la sauvegarde
prise avant la migration. Sinon, elle défait exactement ce qu'elle a créé.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TARGETS = ("catalogue", "copies", "locations", "patrons", "mixed")
BATCH_STATUSES = ("uploaded", "simulated", "applying", "applied", "failed", "cancelled")
ROW_ACTIONS = ("create", "update", "link", "skip", "error")
SEVERITIES = ("ok", "warning", "error")
LINK_TYPES = ("work", "edition", "copy", "series", "agent", "publisher", "collection", "isbn_candidate", "proposal")
PROPOSAL_FIELDS = ("publication_year", "contributor", "publisher", "other")
PROPOSAL_STATUSES = ("proposed", "confirmed", "rejected")
TABLES = ("import_proposals", "import_links", "import_rows", "import_batches", "import_profiles")


def _in(values: tuple[str, ...]) -> str:
    return ",".join(repr(value) for value in values)


STATEMENTS = (
    f"""CREATE TABLE import_profiles (
        id INTEGER NOT NULL,
        name TEXT NOT NULL,
        target TEXT NOT NULL,
        mapping TEXT NOT NULL,
        options TEXT DEFAULT '{{}}' NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        created_by INTEGER,
        CONSTRAINT pk_import_profiles PRIMARY KEY (id),
        CONSTRAINT uq_import_profiles_name UNIQUE (name),
        CONSTRAINT ck_import_profiles_target_values CHECK (target IN ({_in(TARGETS)})),
        CONSTRAINT fk_import_profiles_created_by_users FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL
    ) STRICT""",
    f"""CREATE TABLE import_batches (
        id INTEGER NOT NULL,
        profile_id INTEGER,
        target TEXT NOT NULL,
        filename TEXT NOT NULL,
        file_sha256 TEXT NOT NULL,
        file_bytes INTEGER NOT NULL,
        sheet TEXT DEFAULT '' NOT NULL,
        stored_path TEXT,
        mapping TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        plan_sha256 TEXT,
        status TEXT NOT NULL,
        summary TEXT,
        created_by INTEGER,
        applied_by INTEGER,
        created_at TEXT NOT NULL,
        simulated_at TEXT,
        applied_at TEXT,
        CONSTRAINT pk_import_batches PRIMARY KEY (id),
        CONSTRAINT ck_import_batches_status_values CHECK (status IN ({_in(BATCH_STATUSES)})),
        CONSTRAINT ck_import_batches_target_values CHECK (target IN ({_in(TARGETS)})),
        CONSTRAINT ck_import_batches_file_bytes_positive CHECK (file_bytes >= 0),
        CONSTRAINT fk_import_batches_profile_id_import_profiles
            FOREIGN KEY (profile_id) REFERENCES import_profiles (id) ON DELETE SET NULL,
        CONSTRAINT fk_import_batches_created_by_users FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT fk_import_batches_applied_by_users FOREIGN KEY (applied_by) REFERENCES users (id) ON DELETE SET NULL
    ) STRICT""",
    "CREATE UNIQUE INDEX ux_import_batches_applied_file ON import_batches (file_sha256)"
    " WHERE status IN ('applying','applied')",
    "CREATE INDEX ix_import_batches_status ON import_batches (status)",
    f"""CREATE TABLE import_rows (
        id INTEGER NOT NULL,
        batch_id INTEGER NOT NULL,
        sheet TEXT DEFAULT '' NOT NULL,
        row_number INTEGER NOT NULL,
        raw TEXT NOT NULL,
        normalized TEXT,
        action TEXT NOT NULL,
        severity TEXT DEFAULT 'ok' NOT NULL,
        messages TEXT DEFAULT '[]' NOT NULL,
        CONSTRAINT pk_import_rows PRIMARY KEY (id),
        CONSTRAINT uq_import_rows_batch_id_sheet_row_number UNIQUE (batch_id, sheet, row_number),
        CONSTRAINT ck_import_rows_action_values CHECK (action IN ({_in(ROW_ACTIONS)})),
        CONSTRAINT ck_import_rows_severity_values CHECK (severity IN ({_in(SEVERITIES)})),
        CONSTRAINT ck_import_rows_row_number_positive CHECK (row_number >= 1),
        CONSTRAINT fk_import_rows_batch_id_import_batches
            FOREIGN KEY (batch_id) REFERENCES import_batches (id) ON DELETE CASCADE
    ) STRICT""",
    f"""CREATE TABLE import_links (
        id INTEGER NOT NULL,
        row_id INTEGER NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        created INTEGER NOT NULL,
        CONSTRAINT pk_import_links PRIMARY KEY (id),
        CONSTRAINT uq_import_links_row_id_entity_type_entity_id UNIQUE (row_id, entity_type, entity_id),
        CONSTRAINT ck_import_links_entity_type_values CHECK (entity_type IN ({_in(LINK_TYPES)})),
        CONSTRAINT ck_import_links_created_bool CHECK (created IN (0, 1)),
        CONSTRAINT fk_import_links_row_id_import_rows FOREIGN KEY (row_id) REFERENCES import_rows (id) ON DELETE CASCADE
    ) STRICT""",
    "CREATE INDEX ix_import_links_entity ON import_links (entity_type, entity_id)",
    f"""CREATE TABLE import_proposals (
        id INTEGER NOT NULL,
        edition_id INTEGER NOT NULL,
        row_id INTEGER,
        field TEXT NOT NULL,
        value TEXT NOT NULL,
        source_url TEXT,
        note TEXT,
        status TEXT DEFAULT 'proposed' NOT NULL,
        created_at TEXT NOT NULL,
        decided_by INTEGER,
        decided_at TEXT,
        CONSTRAINT pk_import_proposals PRIMARY KEY (id),
        CONSTRAINT ck_import_proposals_field_values CHECK (field IN ({_in(PROPOSAL_FIELDS)})),
        CONSTRAINT ck_import_proposals_status_values CHECK (status IN ({_in(PROPOSAL_STATUSES)})),
        CONSTRAINT fk_import_proposals_edition_id_editions
            FOREIGN KEY (edition_id) REFERENCES editions (id) ON DELETE CASCADE,
        CONSTRAINT fk_import_proposals_row_id_import_rows
            FOREIGN KEY (row_id) REFERENCES import_rows (id) ON DELETE SET NULL,
        CONSTRAINT fk_import_proposals_decided_by_users FOREIGN KEY (decided_by) REFERENCES users (id) ON DELETE SET NULL
    ) STRICT""",
    "CREATE INDEX ix_import_proposals_edition_id ON import_proposals (edition_id)",
)


def upgrade() -> None:
    bind = op.get_bind()
    for statement in STATEMENTS:
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    batches = bind.exec_driver_sql("SELECT COUNT(*) FROM import_batches").scalar() or 0
    proposals = bind.exec_driver_sql("SELECT COUNT(*) FROM import_proposals").scalar() or 0
    if batches or proposals:
        raise RuntimeError(
            f"Descente refusée : {batches} lot(s) et {proposals} proposition(s) d'import existent. "
            "Restaurez la sauvegarde prise avant la migration 0011."
        )
    for table in TABLES:
        bind.exec_driver_sql(f"DROP TABLE {table}")
