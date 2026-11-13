# SPDX-License-Identifier: EUPL-1.2
"""Catalogue à trois niveaux : œuvre, édition, exemplaire (03-MODELE-DONNEES.md § 3.3, ADR 0003).

La recherche plein texte n'apparaît pas ici : elle est créée par sa révision, et sa présence est exigée par
`core.migrate.REQUIRED_OBJECTS`.
"""

from __future__ import annotations

from sqlalchemy import (
    REAL,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
    type_coerce,
)
from sqlalchemy.orm import Mapped, mapped_column

from biblyo.core.models_base import TABLE_OPTIONS, Base, IntBool

CONTRIBUTOR_ROLE_CHECK = (
    "role IN ('author','scenarist','artist','colorist','illustrator','translator','editor','adapter','other')"
)


class DocumentType(Base):
    __tablename__ = "document_types"
    __table_args__ = (
        UniqueConstraint("code", name="uq_document_types_code"),
        CheckConstraint("is_active IN (0, 1)", name="is_active_bool"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(IntBool, nullable=False, default=True)


class Section(Base):
    __tablename__ = "sections"
    __table_args__ = (
        UniqueConstraint("code", name="uq_sections_code"),
        CheckConstraint("is_active IN (0, 1)", name="is_active_bool"),
        Index("ix_sections_parent_id", "parent_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sections.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    call_prefix: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(IntBool, nullable=False, default=True)


class Tag(Base):
    """Catégorie, genre ou mot-clé : renommable et fusionnable sans perdre ses associations."""

    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("kind", "label_norm", name="uq_tags_kind_label"),
        CheckConstraint("kind IN ('category','genre','keyword')", name="kind_values"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    label_norm: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Publisher(Base):
    __tablename__ = "publishers"
    __table_args__ = (UniqueConstraint("name_norm", name="uq_publishers_name_norm"), TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint("publisher_id", "name_norm", name="uq_collections_publisher_name"),
        Index("ux_collections_no_publisher", "name_norm", unique=True, sqlite_where=text("publisher_id IS NULL")),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publisher_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("publishers.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    issn: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class MediaFile(Base):
    """Image dans /data/media, nommée par son empreinte : jamais d'après une saisie."""

    __tablename__ = "media_files"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_media_files_sha256"),
        CheckConstraint("mime IN ('image/jpeg','image/png','image/webp')", name="mime_values"),
        CheckConstraint("bytes > 0", name="bytes_positive"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    origin_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    source_code: Mapped[str | None] = mapped_column(Text)  # révision 0009 : fournisseur d'une couverture de source
    external_id: Mapped[str | None] = mapped_column(Text)  # identifiant de la notice chez le fournisseur
    license_note: Mapped[str | None] = mapped_column(Text)  # conditions de réutilisation appliquées, telles que lues
    retrieved_at: Mapped[str | None] = mapped_column(Text)


COVER_CHANGE_KINDS = ("upload", "url", "source", "enrichment", "remove", "restore", "revert")


class CoverChange(Base):
    """Un changement de la couverture d'une édition (révision 0010) : d'où vient l'image, qui l'a posée, laquelle elle
    remplace. Un retrait n'a pas de nouvelle image ; un rétablissement remet la précédente."""

    __tablename__ = "cover_changes"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ",".join(repr(k) for k in COVER_CHANGE_KINDS) + ")",
            name="kind_values",
        ),
        Index("ix_cover_changes_edition_id_id", "edition_id", "id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(Integer, ForeignKey("editions.id", ondelete="CASCADE"), nullable=False)
    previous_media_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("media_files.id", ondelete="SET NULL"))
    media_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("media_files.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_code: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    rights_note: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    undone_at: Mapped[str | None] = mapped_column(Text)
    undone_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class Agent(Base):
    """Personne ou collectivité. Aucune unicité sur le nom : deux homonymes restent deux personnes."""

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("ark_bnf", name="uq_agents_ark_bnf"),
        CheckConstraint("kind IN ('person','organization')", name="kind_values"),
        Index("ix_agents_name_norm", "name_norm"),
        Index("ix_agents_sort_name", "sort_name"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="person")
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    ark_bnf: Mapped[str | None] = mapped_column(Text)
    dates: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Series(Base):
    __tablename__ = "series"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_series_public_id"),
        UniqueConstraint("title_norm", name="uq_series_title_norm"),
        UniqueConstraint("call_prefix", name="uq_series_call_prefix"),
        CheckConstraint("next_call_sequence >= 1", name="next_call_sequence_positive"),
        CheckConstraint("status IN ('ongoing','complete','stopped','unknown')", name="status_values"),
        Index("ix_series_title_sort", "title_sort"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_sort: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("series.id", ondelete="SET NULL"))
    call_prefix: Mapped[str | None] = mapped_column(Text)
    next_call_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class SeriesExpectedVolume(Base):
    """Tome dont la parution est confirmée. Un trou dans la numérotation, sans confirmation, n'est pas un manque."""

    __tablename__ = "series_expected_volumes"
    __table_args__ = (
        UniqueConstraint("series_id", "volume_number", name="uq_series_expected_volumes"),
        CheckConstraint("volume_number >= 0", name="volume_number_positive"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(Integer, ForeignKey("series.id", ondelete="CASCADE"), nullable=False)
    volume_number: Mapped[float] = mapped_column(REAL, nullable=False)
    volume_label: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    source_note: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[str] = mapped_column(Text, nullable=False)


class Work(Base):
    """Œuvre ou tome : ce que décrit la notice, quelle qu'en soit la parution."""

    __tablename__ = "works"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_works_public_id"),
        CheckConstraint("length(trim(title)) > 0", name="title_not_empty"),
        CheckConstraint("audience IN ('children','teen','adult','all')", name="audience_values"),
        CheckConstraint("status IN ('active','archived')", name="status_values"),
        CheckConstraint("completeness BETWEEN 0 AND 100", name="completeness_range"),
        Index("ix_works_status_title_sort", "status", "title_sort"),
        Index("ix_works_status_created_at", "status", "created_at"),
        Index("ix_works_document_type_id", "document_type_id"),
        Index("ix_works_section_id", "section_id"),
        Index("ix_works_title_norm", "title_norm"),
        Index("ix_works_completeness", "completeness"),
        Index("ix_works_status_completeness", "status", "completeness"),  # révision 0005, A12
        Index("ix_works_status_no_summary", "status", sqlite_where=text("summary IS NULL")),  # révision 0005, A12
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_type_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("document_types.id", ondelete="RESTRICT"), nullable=False
    )
    section_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sections.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(Text)
    title_sort: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(Text, nullable=False)
    original_title: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    completeness: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class WorkContributor(Base):
    __tablename__ = "work_contributors"
    __table_args__ = (
        CheckConstraint(CONTRIBUTOR_ROLE_CHECK, name="role_values"),
        Index("ix_work_contributors_agent_id", "agent_id"),
        TABLE_OPTIONS,
    )

    work_id: Mapped[int] = mapped_column(Integer, ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="RESTRICT"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WorkSeries(Base):
    __tablename__ = "work_series"
    __table_args__ = (
        UniqueConstraint("series_id", "call_sequence", name="uq_work_series_call"),
        CheckConstraint("volume_number >= 0", name="volume_number_positive"),
        CheckConstraint("call_sequence >= 1", name="call_sequence_positive"),
        CheckConstraint("is_primary IN (0, 1)", name="is_primary_bool"),
        Index("ix_work_series_series_volume", "series_id", "volume_number"),
        Index("ux_work_series_primary", "work_id", unique=True, sqlite_where=text("is_primary = 1")),
        TABLE_OPTIONS,
    )

    work_id: Mapped[int] = mapped_column(Integer, ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    series_id: Mapped[int] = mapped_column(Integer, ForeignKey("series.id", ondelete="RESTRICT"), primary_key=True)
    volume_number: Mapped[float | None] = mapped_column(REAL)
    volume_label: Mapped[str | None] = mapped_column(Text)
    call_sequence: Mapped[int | None] = mapped_column(Integer)
    is_primary: Mapped[bool] = mapped_column(IntBool, nullable=False, default=True)


class WorkTag(Base):
    __tablename__ = "work_tags"
    __table_args__ = (Index("ix_work_tags_tag_id", "tag_id"), TABLE_OPTIONS)

    work_id: Mapped[int] = mapped_column(Integer, ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


class Edition(Base):
    """Édition bibliographique d'une œuvre : une parution, avec ses identifiants, son éditeur et son année."""

    __tablename__ = "editions"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_editions_public_id"),
        CheckConstraint("is_primary IN (0, 1)", name="is_primary_bool"),
        CheckConstraint("publication_year BETWEEN 1400 AND 2200", name="publication_year_range"),
        CheckConstraint("page_count > 0", name="page_count_positive"),
        CheckConstraint("status IN ('active','archived')", name="status_values"),
        Index("ix_editions_work_id", "work_id"),
        Index("ux_editions_primary", "work_id", unique=True, sqlite_where=text("is_primary = 1")),
        Index("ix_editions_publisher_id", "publisher_id"),
        Index("ix_editions_collection_id", "collection_id"),
        Index("ix_editions_publication_year", "publication_year"),
        Index("ix_editions_language", "language"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    work_id: Mapped[int] = mapped_column(Integer, ForeignKey("works.id", ondelete="CASCADE"), nullable=False)
    is_primary: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    title_variant: Mapped[str | None] = mapped_column(Text)
    publisher_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("publishers.id", ondelete="SET NULL"))
    collection_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("collections.id", ondelete="SET NULL"))
    collection_number: Mapped[str | None] = mapped_column(Text)
    publication_year: Mapped[int | None] = mapped_column(Integer)
    publication_date: Mapped[str | None] = mapped_column(Text)
    edition_statement: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False, default="fre")
    page_count: Mapped[int | None] = mapped_column(Integer)
    physical_note: Mapped[str | None] = mapped_column(Text)
    cover_media_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("media_files.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class EditionContributor(Base):
    __tablename__ = "edition_contributors"
    __table_args__ = (
        CheckConstraint(CONTRIBUTOR_ROLE_CHECK, name="role_values"),
        Index("ix_edition_contributors_agent_id", "agent_id"),
        TABLE_OPTIONS,
    )

    edition_id: Mapped[int] = mapped_column(Integer, ForeignKey("editions.id", ondelete="CASCADE"), primary_key=True)
    agent_id: Mapped[int] = mapped_column(Integer, ForeignKey("agents.id", ondelete="RESTRICT"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EditionIdentifier(Base):
    """Volontairement sans unicité entre éditions : un même ISBN sur deux éditions est signalé, jamais refusé."""

    __tablename__ = "edition_identifiers"
    __table_args__ = (
        UniqueConstraint("edition_id", "scheme", "value", name="uq_edition_identifiers"),
        CheckConstraint(
            "scheme IN ('isbn13','isbn10','ean','issn','ark_bnf','ppn_sudoc','other')", name="scheme_values"
        ),
        Index("ix_edition_identifiers_lookup", "scheme", "value"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    edition_id: Mapped[int] = mapped_column(Integer, ForeignKey("editions.id", ondelete="CASCADE"), nullable=False)
    scheme: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class NumberSequence(Base):
    """Numéroteur sans trou ni réemploi : code-barres d'exemplaire aujourd'hui, carte de lecteur au lot L3."""

    __tablename__ = "sequences"
    __table_args__ = (
        CheckConstraint("padding BETWEEN 1 AND 12", name="padding_range"),
        CheckConstraint("last_value >= 0", name="last_value_positive"),
        TABLE_OPTIONS,
    )

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    prefix: Mapped[str] = mapped_column(Text, nullable=False, default="")
    padding: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Copy(Base):
    """Exemplaire physique : son identité propre, sa cote, sa place. Noyau de L1, compléments de L2 (révision 0006).

    Numéro d'inventaire et code-barres sont distincts en base, même quand ils sont égaux (D3). La cote ne dépend ni du
    tome ni de l'emplacement (D2). La position n'existe que sur un niveau, et le service la garde continue.
    Le code-barres peut manquer (révision 0012, D-L6-7) : un exemplaire importé garde son inventaire d'origine et
    attend une étiquette ; `code_of` donne alors l'inventaire, qui suffit à le retrouver au comptoir.
    """

    __tablename__ = "copies"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_copies_public_id"),
        UniqueConstraint("barcode", name="uq_copies_barcode"),
        CheckConstraint(
            "status IN ('in_processing','available','in_repair','missing','lost','withdrawn')", name="status_values"
        ),
        CheckConstraint(
            "shelf_position IS NULL OR (shelf_position >= 1 AND location_id IS NOT NULL)", name="shelf_position_valid"
        ),
        CheckConstraint("is_loanable IN (0, 1)", name="is_loanable_bool"),
        CheckConstraint("condition IN ('new','good','fair','worn','damaged')", name="condition_values"),
        CheckConstraint(
            "acquisition_mode IN ('purchase','donation','exchange','legacy','unknown')", name="acquisition_mode_values"
        ),
        CheckConstraint("price_cents >= 0", name="price_cents_positive"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_copies_edition_id", "edition_id"),
        Index("ix_copies_location_id", "location_id"),
        Index("ix_copies_call_number", "call_number"),
        Index("ux_copies_inventory_number", "inventory_number", unique=True),
        Index(
            "ux_copies_location_position",
            "location_id",
            "shelf_position",
            unique=True,
            sqlite_where=text("shelf_position IS NOT NULL"),
        ),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    edition_id: Mapped[int] = mapped_column(Integer, ForeignKey("editions.id", ondelete="RESTRICT"), nullable=False)
    barcode: Mapped[str | None] = mapped_column(Text)  # facultatif depuis 0012 : à attribuer (D-L6-7)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="in_processing")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    # --- Lot L2 (révision 0006)
    inventory_number: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    call_number: Mapped[str | None] = mapped_column(Text)
    location_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("locations.id", ondelete="RESTRICT"))
    shelf_position: Mapped[int | None] = mapped_column(Integer)
    is_loanable: Mapped[bool] = mapped_column(IntBool, nullable=False, default=True, server_default=text("1"))
    condition: Mapped[str | None] = mapped_column(Text)
    condition_note: Mapped[str | None] = mapped_column(Text)  # état d'origine conservé tel quel
    acquisition_mode: Mapped[str | None] = mapped_column(Text)
    acquired_on: Mapped[str | None] = mapped_column(Text)  # date calendaire, si elle est connue
    acquisition_note: Mapped[str | None] = mapped_column(Text)  # « inf. 2026 », conservé tel quel
    price_cents: Mapped[int | None] = mapped_column(Integer)
    provenance: Mapped[str | None] = mapped_column(Text)
    missing_on: Mapped[str | None] = mapped_column(Text)
    withdrawn_on: Mapped[str | None] = mapped_column(Text)
    withdrawal_reason: Mapped[str | None] = mapped_column(Text)
    first_shelved_at: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


#: Code d'un exemplaire dans une requête : le code-barres, sinon l'inventaire (révision 0012 ; `rules.code_of`).
COPY_CODE = type_coerce(func.coalesce(Copy.barcode, Copy.inventory_number), Text).label("copy_code")


class SavedSearch(Base):
    """Recherche enregistrée : des critères du catalogue, propres à un compte."""

    __tablename__ = "saved_searches"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_saved_searches_user_name"), TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    criteria: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
