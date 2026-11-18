# SPDX-License-Identifier: EUPL-1.2
"""Lots d'impression d'étiquettes (lot L8, tâche T4 ; conception 23).

Révision 0014. Deux tables **nouvelles**, aucune table existante n'est modifiée, aucune donnée n'est écrite :

- `label_batches` : un PDF d'étiquettes préparé (qui, quand, quelle planche et quelle mise en page, à partir de quelle
  case, avec quelle calibration) et son état : « préparé » (le PDF a été produit), « confirmé » (la personne dit que
  les étiquettes sont imprimées et collées), « abandonné ». Un lot n'est jamais tenu pour imprimé de lui-même ;
- `label_batch_items` : une ligne par étiquette du lot, dans l'ordre, avec la feuille et la case où elle tombe et le
  **code imprimé**, gardé tel quel : il prouve, même plus tard, ce qui a été imprimé pour quel exemplaire.

Un lot peut en reprendre un autre (impression interrompue, réimpression) : `parent_id`. Les codes ne sont jamais
régénérés : une réimpression relit le code enregistré de l'exemplaire.

Un rôle d'appoint, **« Impression des étiquettes »** (`labels_printer`), est ajouté s'il n'existe pas : il ne porte que
`labels.print` et se coche **en plus** du rôle d'un compte choisi (un bénévole qui imprime), sans élargir le rôle
« Bénévole » lui-même (arbitrage du 18 novembre 2026). Aucun compte ne le reçoit à la migration.

Descente : **refusée** dès qu'un lot existe (son historique serait perdu) ou qu'un compte porte le rôle d'appoint ; la
voie de retour est alors la sauvegarde prise avant la migration. Sinon, les deux tables et le rôle sont supprimés.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alembic import op
from sqlalchemy import text

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

BATCH_STATUSES = ("prepared", "confirmed", "abandoned")
BATCH_ORIGINS = ("selection", "copy", "resume", "reprint")
PRINTER_ROLE = "labels_printer"
PRINTER_LABEL = "Impression des étiquettes"


def _in(values: tuple[str, ...]) -> str:
    return ",".join(repr(value) for value in values)


def upgrade() -> None:
    op.execute(
        f"""CREATE TABLE label_batches (
        id INTEGER NOT NULL,
        public_id TEXT NOT NULL,
        status TEXT DEFAULT 'prepared' NOT NULL,
        origin TEXT NOT NULL,
        parent_id INTEGER,
        sheet TEXT NOT NULL,
        model TEXT NOT NULL,
        start_position INTEGER NOT NULL,
        occupied TEXT DEFAULT '[]' NOT NULL,
        offset_x REAL DEFAULT 0 NOT NULL,
        offset_y REAL DEFAULT 0 NOT NULL,
        total INTEGER NOT NULL,
        sheets INTEGER NOT NULL,
        created_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        confirmed_by INTEGER,
        confirmed_at TEXT,
        CONSTRAINT pk_label_batches PRIMARY KEY (id),
        CONSTRAINT uq_label_batches_public_id UNIQUE (public_id),
        CONSTRAINT ck_label_batches_status_values CHECK (status IN ({_in(BATCH_STATUSES)})),
        CONSTRAINT ck_label_batches_origin_values CHECK (origin IN ({_in(BATCH_ORIGINS)})),
        CONSTRAINT ck_label_batches_counts CHECK (start_position >= 1 AND total >= 1 AND sheets >= 1),
        CONSTRAINT fk_label_batches_parent_id_label_batches
            FOREIGN KEY (parent_id) REFERENCES label_batches (id) ON DELETE SET NULL,
        CONSTRAINT fk_label_batches_created_by_users FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL,
        CONSTRAINT fk_label_batches_confirmed_by_users
            FOREIGN KEY (confirmed_by) REFERENCES users (id) ON DELETE SET NULL
    ) STRICT"""
    )
    op.execute("CREATE INDEX ix_label_batches_status ON label_batches (status)")
    op.execute(
        """CREATE TABLE label_batch_items (
        id INTEGER NOT NULL,
        batch_id INTEGER NOT NULL,
        rank INTEGER NOT NULL,
        copy_id INTEGER,
        barcode TEXT NOT NULL,
        sheet_number INTEGER NOT NULL,
        position INTEGER NOT NULL,
        CONSTRAINT pk_label_batch_items PRIMARY KEY (id),
        CONSTRAINT uq_label_batch_items_batch_id_rank UNIQUE (batch_id, rank),
        CONSTRAINT uq_label_batch_items_batch_id_sheet_number_position UNIQUE (batch_id, sheet_number, position),
        CONSTRAINT ck_label_batch_items_places CHECK (rank >= 1 AND sheet_number >= 1 AND position >= 1),
        CONSTRAINT fk_label_batch_items_batch_id_label_batches
            FOREIGN KEY (batch_id) REFERENCES label_batches (id) ON DELETE CASCADE,
        CONSTRAINT fk_label_batch_items_copy_id_copies FOREIGN KEY (copy_id) REFERENCES copies (id) ON DELETE SET NULL
    ) STRICT"""
    )
    op.execute("CREATE INDEX ix_label_batch_items_copy_id ON label_batch_items (copy_id)")
    connection = op.get_bind()
    if connection.execute(text("SELECT 1 FROM roles WHERE code = :code"), {"code": PRINTER_ROLE}).first() is None:
        moment = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        connection.execute(
            text("INSERT INTO roles (code, label, is_system, created_at) VALUES (:code, :label, 0, :at)"),
            {"code": PRINTER_ROLE, "label": PRINTER_LABEL, "at": moment},
        )
        connection.execute(
            text(
                "INSERT INTO role_permissions (role_id, permission)"
                " SELECT id, 'labels.print' FROM roles WHERE code = :code"
            ),
            {"code": PRINTER_ROLE},
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(text("SELECT COUNT(*) FROM label_batches")).scalar():
        raise RuntimeError(
            "Descente refusée : des lots d'étiquettes existent. Restaurez la sauvegarde prise avant la migration."
        )
    holders = connection.execute(
        text("SELECT COUNT(*) FROM user_roles JOIN roles ON roles.id = user_roles.role_id WHERE roles.code = :code"),
        {"code": PRINTER_ROLE},
    ).scalar()
    if holders:
        raise RuntimeError(
            f"Descente refusée : {holders} compte(s) ont le rôle « {PRINTER_LABEL} ». Restaurez la sauvegarde prise "
            "avant la migration."
        )
    # Les clés étrangères ne sont pas actives pendant une migration : les droits du rôle sont retirés explicitement.
    connection.execute(
        text("DELETE FROM role_permissions WHERE role_id IN (SELECT id FROM roles WHERE code = :code)"),
        {"code": PRINTER_ROLE},
    )
    connection.execute(text("DELETE FROM roles WHERE code = :code"), {"code": PRINTER_ROLE})
    op.execute("DROP TABLE label_batch_items")
    op.execute("DROP TABLE label_batches")
