# SPDX-License-Identifier: EUPL-1.2
"""Emplacements et codes retirés (révision 0006). Les colonnes de L2 sur `copies` sont déclarées avec `Copy`.

Hiérarchie typée (D8) : site facultatif → salle → meuble → niveau (étagère ou bac), plus des lieux virtuels.
Le service garantit les parents permis et l'absence de cycle ; la base garantit types, profondeur et unicités.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from biblyo.core.models_base import TABLE_OPTIONS, Base, IntBool


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_locations_public_id"),
        UniqueConstraint("code", name="uq_locations_code"),
        UniqueConstraint("parent_id", "name", name="uq_locations_sibling_name"),
        CheckConstraint("kind IN ('site','room','furniture','shelf','bin','virtual')", name="kind_values"),
        CheckConstraint("depth BETWEEN 0 AND 3", name="depth_range"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_empty"),
        CheckConstraint("capacity >= 0", name="capacity_positive"),
        CheckConstraint(
            "(grid_row IS NULL AND grid_col IS NULL) OR (grid_row >= 1 AND grid_col >= 1)", name="grid_position"
        ),
        CheckConstraint("grid_width >= 1 AND grid_height >= 1", name="grid_size"),
        CheckConstraint("is_active IN (0, 1)", name="is_active_bool"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ux_locations_root_name", "name", unique=True, sqlite_where=text("parent_id IS NULL")),
        Index("ix_locations_parent_order", "parent_id", "sort_order"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("locations.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(Text)  # code court imprimable (« BD-A »)
    path: Mapped[str] = mapped_column(Text, nullable=False)  # « Salle jeunesse / Meuble BD 1 / Étagère 2 »
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    capacity: Mapped[int | None] = mapped_column(Integer)  # places physiques (D1) ; jamais bloquante
    content_hint: Mapped[str | None] = mapped_column(Text)  # « 0–9 · A »
    grid_row: Mapped[int | None] = mapped_column(Integer)  # place d'un meuble sur la grille de sa salle (D5)
    grid_col: Mapped[int | None] = mapped_column(Integer)
    grid_width: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    grid_height: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(IntBool, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class RetiredCode(Base):
    """Code-barres ou numéro d'inventaire d'un exemplaire supprimé : jamais réattribué (D3)."""

    __tablename__ = "retired_codes"
    __table_args__ = (CheckConstraint("kind IN ('barcode','inventory')", name="kind_values"), TABLE_OPTIONS)

    value: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    copy_public_id: Mapped[str] = mapped_column(Text, nullable=False)
    retired_at: Mapped[str] = mapped_column(Text, nullable=False)
