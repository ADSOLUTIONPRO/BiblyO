# SPDX-License-Identifier: EUPL-1.2
"""Lots d'impression d'étiquettes (révision 0014). Un lot est une trace : il ne dit jamais seul qu'une étiquette est
collée ; seule la personne le confirme."""

from __future__ import annotations

from sqlalchemy import REAL, CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from biblyo.core.models_base import TABLE_OPTIONS, Base

BATCH_STATUSES = ("prepared", "confirmed", "abandoned")
BATCH_ORIGINS = ("selection", "copy", "resume", "reprint")


def _values(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(value) for value in values)})"


class LabelBatch(Base):
    __tablename__ = "label_batches"
    __table_args__ = (
        UniqueConstraint("public_id"),
        CheckConstraint(_values("status", BATCH_STATUSES), name="status_values"),
        CheckConstraint(_values("origin", BATCH_ORIGINS), name="origin_values"),
        CheckConstraint("start_position >= 1 AND total >= 1 AND sheets >= 1", name="counts"),
        Index("ix_label_batches_status", "status"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="prepared", server_default="prepared")
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("label_batches.id", ondelete="SET NULL"))
    sheet: Mapped[str] = mapped_column(Text, nullable=False)  # géométrie employée (JSON)
    model: Mapped[str] = mapped_column(Text, nullable=False)  # éléments imprimés (JSON)
    start_position: Mapped[int] = mapped_column(Integer, nullable=False)
    occupied: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    offset_x: Mapped[float] = mapped_column(REAL, nullable=False, default=0, server_default="0")
    offset_y: Mapped[float] = mapped_column(REAL, nullable=False, default=0, server_default="0")
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    sheets: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[str | None] = mapped_column(Text)


class LabelBatchItem(Base):
    __tablename__ = "label_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "rank"),
        UniqueConstraint("batch_id", "sheet_number", "position"),
        CheckConstraint("rank >= 1 AND sheet_number >= 1 AND position >= 1", name="places"),
        Index("ix_label_batch_items_copy_id", "copy_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(Integer, ForeignKey("label_batches.id", ondelete="CASCADE"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    copy_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("copies.id", ondelete="SET NULL"))
    barcode: Mapped[str] = mapped_column(Text, nullable=False)  # le code imprimé, gardé tel quel
    sheet_number: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
