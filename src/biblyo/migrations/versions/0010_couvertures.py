# SPDX-License-Identifier: EUPL-1.2
"""Historique des couvertures et source Open Library (correction des couvertures de L5, conception 17 § 6).

Révision 0010. Elle **crée** `cover_changes` : chaque ajout, remplacement, retrait ou rétablissement de la couverture
d'une édition, avec la couverture précédente, le type d'ajout, la source, la référence, le droit d'utilisation, la
personne et la date. Elle inscrit la source Open Library, **désactivée** : seules ses couvertures seraient utilisées,
et leurs droits restent ceux des éditeurs (conception 17 § 4). Aucune ligne existante n'est réécrite.

Descente : **refusée dès qu'un changement de couverture est consigné**, ou si Open Library a été activée. La voie de
retour est alors la sauvegarde prise avant la migration. Sinon, elle défait exactement ce qu'elle a créé.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

#: code, libellé, activée, priorité, intervalle interactif, intervalle en lot, délai, couvertures
OPEN_LIBRARY = ("openlibrary", "Open Library (Internet Archive)", 0, 3, 1500, 3000, 15000, 1)
KINDS = ("upload", "url", "source", "enrichment", "remove", "restore", "revert")
STATEMENTS = (
    f"""CREATE TABLE cover_changes (
        id INTEGER NOT NULL,
        edition_id INTEGER NOT NULL,
        previous_media_id INTEGER,
        media_id INTEGER,
        kind TEXT NOT NULL,
        source_code TEXT,
        reference TEXT,
        rights_note TEXT,
        user_id INTEGER,
        created_at TEXT NOT NULL,
        undone_at TEXT,
        undone_by INTEGER,
        CONSTRAINT pk_cover_changes PRIMARY KEY (id),
        CONSTRAINT fk_cover_changes_edition_id_editions
            FOREIGN KEY (edition_id) REFERENCES editions (id) ON DELETE CASCADE,
        CONSTRAINT fk_cover_changes_previous_media_id_media_files
            FOREIGN KEY (previous_media_id) REFERENCES media_files (id) ON DELETE SET NULL,
        CONSTRAINT fk_cover_changes_media_id_media_files
            FOREIGN KEY (media_id) REFERENCES media_files (id) ON DELETE SET NULL,
        CONSTRAINT fk_cover_changes_user_id_users FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT fk_cover_changes_undone_by_users FOREIGN KEY (undone_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT ck_cover_changes_kind_values CHECK (kind IN ({",".join(repr(k) for k in KINDS)}))
    ) STRICT""",
    "CREATE INDEX ix_cover_changes_edition_id_id ON cover_changes (edition_id, id)",
)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _journal(bind: sa.engine.Connection, action: str, details: dict[str, object]) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO audit_log (at, user_id, action, entity_type, entity_id, details)"
            " VALUES (:at, NULL, :action, 'metadata_source', NULL, :details)"
        ),
        {"at": _now(), "action": action, "details": json.dumps(details, ensure_ascii=False)},
    )


def upgrade() -> None:
    bind = op.get_bind()
    for statement in STATEMENTS:
        bind.exec_driver_sql(statement)
    code, label, enabled, priority, interval, batch_interval, timeout, covers = OPEN_LIBRARY
    taken = bind.exec_driver_sql("SELECT COALESCE(MAX(priority), 0) FROM metadata_sources").scalar() or 0
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
            "priority": max(priority, int(taken) + 1),
            "interval": interval,
            "batch": batch_interval,
            "timeout": timeout,
            "covers": covers,
            "at": _now(),
        },
    )
    _journal(bind, "migration.sources_created", {"revision": revision, "sources": [code], "activee": False})


def downgrade() -> None:
    bind = op.get_bind()
    changes = bind.exec_driver_sql("SELECT COUNT(*) FROM cover_changes").scalar() or 0
    enabled = bind.exec_driver_sql(
        "SELECT COUNT(*) FROM metadata_sources WHERE code = 'openlibrary' AND is_enabled = 1"
    ).scalar()
    if changes or enabled:
        raise RuntimeError(
            f"Des changements de couverture sont consignés ({changes}) ou Open Library est activée : ils ne sont"
            " jamais supprimés par un retour arrière. Restaurez la sauvegarde prise avant la migration."
        )
    bind.exec_driver_sql("DELETE FROM source_cache WHERE source_code = 'openlibrary'")
    bind.exec_driver_sql("DELETE FROM metadata_sources WHERE code = 'openlibrary'")
    bind.exec_driver_sql("DROP TABLE cover_changes")
