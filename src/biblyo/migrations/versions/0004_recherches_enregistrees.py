# SPDX-License-Identifier: EUPL-1.2
"""Recherches enregistrées, propres à chaque compte. Révision 0004 : une table ajoutée, rien de réécrit."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("criteria", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_saved_searches"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_saved_searches_user_id_users", ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "name", name="uq_saved_searches_user_name"),
        sqlite_strict=True,
    )


def downgrade() -> None:
    op.drop_table("saved_searches")
