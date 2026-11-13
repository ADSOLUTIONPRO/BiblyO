# SPDX-License-Identifier: EUPL-1.2
"""Emplacements et exemplaires complets (lot L2).

Révision 0006. Elle crée `locations` et `retired_codes`, et **complète** `copies` par `ALTER TABLE ADD COLUMN` :
aucune ligne n'est réécrite, sinon pour donner à chaque exemplaire existant un numéro d'inventaire égal à son
code-barres (D3). Les exemplaires de L1 deviennent « non rangés ».

Descente : refusée dès qu'un emplacement, un code retiré ou une donnée propre à L2 existe ; la voie de retour est
alors la sauvegarde prise avant la migration. Sinon, `copies` est reconstruite telle que la révision 0002 l'a créée,
lignes comprises.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

COPY_COLUMNS = (
    "inventory_number TEXT NOT NULL DEFAULT ''",
    "call_number TEXT",
    "location_id INTEGER CONSTRAINT fk_copies_location_id_locations REFERENCES locations (id) ON DELETE RESTRICT",
    "shelf_position INTEGER CONSTRAINT ck_copies_shelf_position_valid"
    " CHECK (shelf_position IS NULL OR (shelf_position >= 1 AND location_id IS NOT NULL))",
    "is_loanable INTEGER NOT NULL DEFAULT 1 CONSTRAINT ck_copies_is_loanable_bool CHECK (is_loanable IN (0, 1))",
    "condition TEXT CONSTRAINT ck_copies_condition_values CHECK (condition IN ('new','good','fair','worn','damaged'))",
    "condition_note TEXT",
    "acquisition_mode TEXT CONSTRAINT ck_copies_acquisition_mode_values"
    " CHECK (acquisition_mode IN ('purchase','donation','exchange','legacy','unknown'))",
    "acquired_on TEXT",
    "acquisition_note TEXT",
    "price_cents INTEGER CONSTRAINT ck_copies_price_cents_positive CHECK (price_cents >= 0)",
    "provenance TEXT",
    "missing_on TEXT",
    "withdrawn_on TEXT",
    "withdrawal_reason TEXT",
    "first_shelved_at TEXT",
    "version INTEGER NOT NULL DEFAULT 1 CONSTRAINT ck_copies_version_positive CHECK (version >= 1)",
)
#: Une donnée propre à L2 interdit la descente : elle serait perdue.
L2_DATA = (
    "inventory_number <> barcode OR call_number IS NOT NULL OR location_id IS NOT NULL OR shelf_position IS NOT NULL"
    " OR is_loanable <> 1 OR condition IS NOT NULL OR condition_note IS NOT NULL OR acquisition_mode IS NOT NULL"
    " OR acquired_on IS NOT NULL OR acquisition_note IS NOT NULL OR price_cents IS NOT NULL OR provenance IS NOT NULL"
    " OR missing_on IS NOT NULL OR withdrawn_on IS NOT NULL OR withdrawal_reason IS NOT NULL"
    " OR first_shelved_at IS NOT NULL OR version <> 1"
)
L1_COPY_COLUMNS = "id, public_id, edition_id, barcode, status, notes, created_at, updated_at, created_by, updated_by"


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("parent_id", sa.Integer),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("code", sa.Text),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("depth", sa.Integer, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False),
        sa.Column("capacity", sa.Integer),
        sa.Column("content_hint", sa.Text),
        sa.Column("grid_row", sa.Integer),
        sa.Column("grid_col", sa.Integer),
        sa.Column("grid_width", sa.Integer, nullable=False),
        sa.Column("grid_height", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Integer, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_locations"),
        sa.UniqueConstraint("public_id", name="uq_locations_public_id"),
        sa.UniqueConstraint("code", name="uq_locations_code"),
        sa.UniqueConstraint("parent_id", "name", name="uq_locations_sibling_name"),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["locations.id"], name="fk_locations_parent_id_locations", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("kind IN ('site','room','furniture','shelf','bin','virtual')", name="ck_locations_kind_values"),
        sa.CheckConstraint("depth BETWEEN 0 AND 3", name="ck_locations_depth_range"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_locations_name_not_empty"),
        sa.CheckConstraint("capacity >= 0", name="ck_locations_capacity_positive"),
        sa.CheckConstraint(
            "(grid_row IS NULL AND grid_col IS NULL) OR (grid_row >= 1 AND grid_col >= 1)",
            name="ck_locations_grid_position",
        ),
        sa.CheckConstraint("grid_width >= 1 AND grid_height >= 1", name="ck_locations_grid_size"),
        sa.CheckConstraint("is_active IN (0, 1)", name="ck_locations_is_active_bool"),
        sa.CheckConstraint("version >= 1", name="ck_locations_version_positive"),
        sqlite_strict=True,
    )
    op.create_index("ux_locations_root_name", "locations", ["name"], unique=True, sqlite_where=sa.text("parent_id IS NULL"))
    op.create_index("ix_locations_parent_order", "locations", ["parent_id", "sort_order"])
    op.create_table(
        "retired_codes",
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("copy_public_id", sa.Text, nullable=False),
        sa.Column("retired_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("value", name="pk_retired_codes"),
        sa.CheckConstraint("kind IN ('barcode','inventory')", name="ck_retired_codes_kind_values"),
        sqlite_strict=True,
    )
    bind = op.get_bind()
    for column in COPY_COLUMNS:
        bind.exec_driver_sql(f"ALTER TABLE copies ADD COLUMN {column}")
    bind.exec_driver_sql("UPDATE copies SET inventory_number = barcode")
    op.create_index("ux_copies_inventory_number", "copies", ["inventory_number"], unique=True)
    op.create_index(
        "ux_copies_location_position",
        "copies",
        ["location_id", "shelf_position"],
        unique=True,
        sqlite_where=sa.text("shelf_position IS NOT NULL"),
    )
    op.create_index("ix_copies_location_id", "copies", ["location_id"])
    op.create_index("ix_copies_call_number", "copies", ["call_number"])


def downgrade() -> None:
    bind = op.get_bind()
    locations = bind.exec_driver_sql("SELECT COUNT(*) FROM locations").scalar()
    retired = bind.exec_driver_sql("SELECT COUNT(*) FROM retired_codes").scalar()
    filled = bind.exec_driver_sql(f"SELECT COUNT(*) FROM copies WHERE {L2_DATA}").scalar()  # noqa: S608
    if locations or retired or filled:
        raise RuntimeError(
            f"Des données de L2 existent ({locations} emplacements, {retired} codes retirés, {filled} exemplaires "
            "complétés). Restaurez la sauvegarde prise avant la migration."
        )
    # Reconstruction de `copies` telle que la révision 0002 l'a créée, lignes comprises.
    op.create_table(
        "copies_l1",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("edition_id", sa.Integer, nullable=False),
        sa.Column("barcode", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.CheckConstraint(
            "status IN ('in_processing','available','in_repair','missing','lost','withdrawn')",
            name="ck_copies_status_values",
        ),
        sa.UniqueConstraint("public_id", name="uq_copies_public_id"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_copies_updated_by_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_copies"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_copies_created_by_users", ondelete="SET NULL"),
        sa.UniqueConstraint("barcode", name="uq_copies_barcode"),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name="fk_copies_edition_id_editions", ondelete="RESTRICT"
        ),
        sqlite_strict=True,
    )
    bind.exec_driver_sql(f"INSERT INTO copies_l1 ({L1_COPY_COLUMNS}) SELECT {L1_COPY_COLUMNS} FROM copies")  # noqa: S608
    op.drop_table("copies")
    op.rename_table("copies_l1", "copies")
    op.create_index("ix_copies_edition_id", "copies", ["edition_id"])
    op.drop_table("retired_codes")
    op.drop_index("ix_locations_parent_order", table_name="locations")
    op.drop_index("ux_locations_root_name", table_name="locations")
    op.drop_table("locations")
