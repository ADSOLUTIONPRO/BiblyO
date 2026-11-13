# SPDX-License-Identifier: EUPL-1.2
"""Application d'un import (lot L6, tâche T5 ; conceptions 20 et 21, décision D-L6-7).

Révision 0012. Trois changements, aucun ne touche une donnée existante :

- `copies.barcode` devient **facultatif**. Décision D-L6-7 : un exemplaire importé garde son numéro d'inventaire
  d'origine et n'a pas encore de code-barres ; l'attribution d'un code scannable vient plus tard, sans modifier
  l'inventaire, et ne remplit qu'un code vide. L'unicité reste (SQLite admet plusieurs valeurs vides). SQLite ne
  sait pas retirer un `NOT NULL` : la table est reconstruite à l'identique, sa vue et son déclencheur recréés depuis
  leur texte exact, les lignes recopiées telles quelles ;
- `import_proposals.field` admet les sortes de propositions de la simulation (ISBN, tome, forme d'un nom, séparation
  de noms, organisation, forme de recherche, rôle, état) : la table, vide tant qu'aucun import n'a été appliqué, est
  reconstruite avec sa nouvelle liste ;
- `import_batches` reçoit l'identifiant stable de l'opération (`public_id`), le fichier complémentaire lu et la
  sauvegarde prise avant l'écriture.

Descente : **refusée** dès qu'un exemplaire est sans code-barres ou qu'un lot ou une proposition d'import existe ;
la voie de retour est alors la sauvegarde prise avant la migration.
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

FIELDS_0011 = ("publication_year", "contributor", "publisher", "other")
FIELDS_0012 = (
    "isbn",
    "publication_year",
    "contributor",
    "role",
    "publisher",
    "volume",
    "name_form",
    "name_split",
    "organization",
    "search_form",
    "condition",
    "other",
)
PROPOSAL_STATUSES = ("proposed", "confirmed", "rejected")
BARCODE_REQUIRED = "barcode TEXT NOT NULL"
BARCODE_OPTIONAL = "barcode TEXT"


def _in(values: tuple[str, ...]) -> str:
    return ",".join(repr(value) for value in values)


def _proposals(fields: tuple[str, ...]) -> str:
    return f"""CREATE TABLE import_proposals (
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
        CONSTRAINT ck_import_proposals_field_values CHECK (field IN ({_in(fields)})),
        CONSTRAINT ck_import_proposals_status_values CHECK (status IN ({_in(PROPOSAL_STATUSES)})),
        CONSTRAINT fk_import_proposals_edition_id_editions
            FOREIGN KEY (edition_id) REFERENCES editions (id) ON DELETE CASCADE,
        CONSTRAINT fk_import_proposals_row_id_import_rows
            FOREIGN KEY (row_id) REFERENCES import_rows (id) ON DELETE SET NULL,
        CONSTRAINT fk_import_proposals_decided_by_users FOREIGN KEY (decided_by) REFERENCES users (id) ON DELETE SET NULL
    ) STRICT"""


BATCH_COLUMNS = (
    ("public_id", "TEXT"),
    ("complement_filename", "TEXT"),
    ("complement_sha256", "TEXT"),
    ("backup_name", "TEXT"),
)


def _scalar(bind, query: str) -> int:
    return int(bind.exec_driver_sql(query).scalar() or 0)


def _rebuild_copies(bind, old: str, new: str) -> None:
    """Reconstruit `copies` en ne changeant que la déclaration de `barcode`. Les clés étrangères sont coupées par
    l'environnement de migration ; la vue et le déclencheur qui lisent `copies` sont retirés puis recréés depuis leur
    texte exact, les index aussi."""
    table_sql = bind.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'copies'").scalar()
    if table_sql is None or old not in table_sql:
        raise RuntimeError(f"Table copies inattendue : « {old} » introuvable.")
    dependents = bind.exec_driver_sql(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('view', 'trigger') AND sql LIKE '%copies%'"
        " ORDER BY type DESC, name"
    ).fetchall()
    indexes = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'copies' AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    before = _scalar(bind, "SELECT COUNT(*) FROM copies")
    known = set(bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall())
    for kind, name, _sql in dependents:
        bind.exec_driver_sql(f'DROP {kind.upper()} "{name}"')
    _head, rest = table_sql.split("(", 1)
    bind.exec_driver_sql("CREATE TABLE copies_0012 (" + rest.replace(old, new, 1))
    bind.exec_driver_sql("INSERT INTO copies_0012 SELECT * FROM copies")
    bind.exec_driver_sql("DROP TABLE copies")
    bind.exec_driver_sql("ALTER TABLE copies_0012 RENAME TO copies")
    for (sql,) in indexes:
        bind.exec_driver_sql(sql)
    for _kind, _name, sql in reversed(dependents):
        bind.exec_driver_sql(sql)
    after = _scalar(bind, "SELECT COUNT(*) FROM copies")
    problems = set(bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()) - known  # écarts nouveaux seulement
    if after != before or problems:
        raise RuntimeError(
            f"Reconstruction de copies incohérente : {before} → {after} lignes, {len(problems)} écart(s)."
        )


def _rebuild_proposals(bind, fields: tuple[str, ...]) -> None:
    bind.exec_driver_sql("ALTER TABLE import_proposals RENAME TO import_proposals_old")
    bind.exec_driver_sql("DROP INDEX ix_import_proposals_edition_id")
    bind.exec_driver_sql(_proposals(fields))
    bind.exec_driver_sql("INSERT INTO import_proposals SELECT * FROM import_proposals_old")
    bind.exec_driver_sql("DROP TABLE import_proposals_old")
    bind.exec_driver_sql("CREATE INDEX ix_import_proposals_edition_id ON import_proposals (edition_id)")


def upgrade() -> None:
    bind = op.get_bind()
    _rebuild_copies(bind, BARCODE_REQUIRED, BARCODE_OPTIONAL)
    _rebuild_proposals(bind, FIELDS_0012)
    for name, kind in BATCH_COLUMNS:
        bind.exec_driver_sql(f"ALTER TABLE import_batches ADD COLUMN {name} {kind}")
    bind.exec_driver_sql(
        "CREATE UNIQUE INDEX ux_import_batches_public_id ON import_batches (public_id) WHERE public_id IS NOT NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    without = _scalar(bind, "SELECT COUNT(*) FROM copies WHERE barcode IS NULL")
    batches = _scalar(bind, "SELECT COUNT(*) FROM import_batches")
    proposals = _scalar(bind, "SELECT COUNT(*) FROM import_proposals")
    if without or batches or proposals:
        raise RuntimeError(
            f"Descente refusée : {without} exemplaire(s) sans code-barres, {batches} lot(s) et {proposals} "
            "proposition(s) d'import existent. Restaurez la sauvegarde prise avant la migration 0012."
        )
    bind.exec_driver_sql("DROP INDEX ux_import_batches_public_id")
    for name, _kind in reversed(BATCH_COLUMNS):
        bind.exec_driver_sql(f"ALTER TABLE import_batches DROP COLUMN {name}")
    _rebuild_proposals(bind, FIELDS_0011)
    _rebuild_copies(bind, BARCODE_OPTIONAL + ",", BARCODE_REQUIRED + ",")
