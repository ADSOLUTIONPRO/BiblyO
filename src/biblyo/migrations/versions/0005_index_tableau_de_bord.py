# SPDX-License-Identifier: EUPL-1.2
"""Deux index pour le tableau de bord (anomalie A12, optimisation O1).

Révision 0005. Elle n'ajoute que des index et ne réécrit aucune donnée.

- `ix_works_status_completeness` couvre la complétude moyenne, le filtre « Complétude < 50 % » et le tri par complétude.
- `ix_works_status_no_summary`, index partiel, compte les notices actives sans résumé sans parcourir la table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_works_status_completeness", "works", ["status", "completeness"])
    op.create_index("ix_works_status_no_summary", "works", ["status"], sqlite_where=sa.text("summary IS NULL"))


def downgrade() -> None:
    """Retirer un index ne perd aucune donnée."""
    op.drop_index("ix_works_status_no_summary", table_name="works")
    op.drop_index("ix_works_status_completeness", table_name="works")
