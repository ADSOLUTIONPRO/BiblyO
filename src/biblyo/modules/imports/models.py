# SPDX-License-Identifier: EUPL-1.2
"""Profils, lots, lignes, liens et propositions d'import (révision 0011, conceptions 03 § 3.8 et 21, T1).

Rien ici ne touche au catalogue : un import écrit ses traces à côté, garde chaque ligne lue telle qu'elle était, et
relie chaque notice, édition, exemplaire ou proposition créée à sa ligne d'origine (incompatibilité I4). Une valeur
« à vérifier » qui n'est pas un ISBN devient une proposition (I3) ; un ISBN proposé va dans
`edition_isbn_candidates`, comme en L5.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from biblyo.core.models_base import TABLE_OPTIONS, Base

TARGETS = ("catalogue", "copies", "locations", "patrons", "mixed")
BATCH_STATUSES = ("uploaded", "simulated", "applying", "applied", "failed", "cancelled")
ROW_ACTIONS = ("create", "update", "link", "skip", "error")
SEVERITIES = ("ok", "warning", "error")
LINK_TYPES = ("work", "edition", "copy", "series", "agent", "publisher", "collection", "isbn_candidate", "proposal")
#: Sortes de propositions (révision 0012) : jamais des faits, chacune à confirmer ou rejeter.
PROPOSAL_FIELDS = (
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


def _values(name: str, values: tuple[str, ...]) -> str:
    return f"{name} IN ({','.join(repr(v) for v in values)})"


class ImportProfile(Base):
    """Correspondance d'un fichier : colonne lue → champ, règles de transformation, tables de valeurs (JSON)."""

    __tablename__ = "import_profiles"
    __table_args__ = (
        UniqueConstraint("name", name="uq_import_profiles_name"),
        CheckConstraint(_values("target", TARGETS), name="target_values"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    mapping: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class ImportBatch(Base):
    """Un fichier lu, simulé, puis appliqué ou abandonné. Un même fichier ne s'applique qu'une fois."""

    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(_values("status", BATCH_STATUSES), name="status_values"),
        CheckConstraint(_values("target", TARGETS), name="target_values"),
        CheckConstraint("file_bytes >= 0", name="file_bytes_positive"),
        Index(
            "ux_import_batches_applied_file",
            "file_sha256",
            unique=True,
            sqlite_where=text("status IN ('applying','applied')"),
        ),
        Index("ix_import_batches_status", "status"),
        Index("ux_import_batches_public_id", "public_id", unique=True, sqlite_where=text("public_id IS NOT NULL")),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("import_profiles.id", ondelete="SET NULL"))
    target: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    file_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sheet: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    stored_path: Mapped[str | None] = mapped_column(Text)  # /data/imports/<empreinte>, purgé après conservation
    mapping: Mapped[str] = mapped_column(Text, nullable=False)  # figé à la simulation
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    plan_sha256: Mapped[str | None] = mapped_column(Text)  # l'application exige ce plan exact
    status: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)  # JSON : créations, liens, rejets, avertissements
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    applied_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    simulated_at: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[str | None] = mapped_column(Text)
    # --- Révision 0012 (T5)
    public_id: Mapped[str | None] = mapped_column(Text)  # identifiant stable de l'opération
    complement_filename: Mapped[str | None] = mapped_column(Text)
    complement_sha256: Mapped[str | None] = mapped_column(Text)
    backup_name: Mapped[str | None] = mapped_column(Text)  # sauvegarde vérifiée prise avant l'écriture


class ImportRow(Base):
    """Une ligne du fichier, telle que lue (valeurs brutes) et telle que comprise (valeurs normalisées)."""

    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "sheet", "row_number", name="uq_import_rows_batch_id_sheet_row_number"),
        CheckConstraint(_values("action", ROW_ACTIONS), name="action_values"),
        CheckConstraint(_values("severity", SEVERITIES), name="severity_values"),
        CheckConstraint("row_number >= 1", name="row_number_positive"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(Integer, ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False)
    sheet: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)  # numéro de ligne dans le fichier, en-têtes = 1
    raw: Mapped[str] = mapped_column(Text, nullable=False)  # JSON : en-tête exact → valeur lue
    normalized: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="ok", server_default=text("'ok'"))
    messages: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default=text("'[]'"))


class ImportLink(Base):
    """Ce qu'une ligne a créé ou rejoint : la traçabilité d'une notice, d'un exemplaire ou d'une proposition."""

    __tablename__ = "import_links"
    __table_args__ = (
        UniqueConstraint("row_id", "entity_type", "entity_id", name="uq_import_links_row_id_entity_type_entity_id"),
        CheckConstraint(_values("entity_type", LINK_TYPES), name="entity_type_values"),
        CheckConstraint("created IN (0, 1)", name="created_bool"),
        Index("ix_import_links_entity", "entity_type", "entity_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    row_id: Mapped[int] = mapped_column(Integer, ForeignKey("import_rows.id", ondelete="CASCADE"), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 : créé par la ligne ; 0 : rejoint, existant


class ImportProposal(Base):
    """Valeur « à vérifier » d'un fichier qui n'est pas un ISBN : année, contributeur, éditeur. Jamais un fait."""

    __tablename__ = "import_proposals"
    __table_args__ = (
        CheckConstraint(_values("field", PROPOSAL_FIELDS), name="field_values"),
        CheckConstraint(_values("status", PROPOSAL_STATUSES), name="status_values"),
        Index("ix_import_proposals_edition_id", "edition_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(Integer, ForeignKey("editions.id", ondelete="CASCADE"), nullable=False)
    row_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("import_rows.id", ondelete="SET NULL"))
    field: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed", server_default=text("'proposed'"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[str | None] = mapped_column(Text)
