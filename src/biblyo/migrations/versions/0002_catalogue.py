# SPDX-License-Identifier: EUPL-1.2
"""Catalogue à trois niveaux : œuvre, édition, exemplaire (ADR 0003).

Révision 0002. Elle ajoute des tables et ne réécrit aucune donnée existante. Les permissions du catalogue sont
déjà semées par la révision 0001. Seules des valeurs génériques sont amorcées : aucune donnée de bibliothèque.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

DOCUMENT_TYPES = (
    ("livre", "Livre", "book", 10),
    ("bd", "Bande dessinée", "book", 20),
    ("manga", "Manga", "book", 30),
    ("comics", "Comics", "book", 40),
    ("revue", "Revue", "book", 50),
    ("documentaire", "Documentaire", "book", 60),
    ("album", "Album jeunesse", "book", 70),
)
TABLES = (
    "copies",
    "sequences",
    "edition_identifiers",
    "edition_contributors",
    "editions",
    "work_tags",
    "work_series",
    "work_contributors",
    "works",
    "series_expected_volumes",
    "series",
    "agents",
    "media_files",
    "collections",
    "publishers",
    "tags",
    "sections",
    "document_types",
)


def upgrade() -> None:
    op.create_table(
        "document_types",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("icon", sa.Text),
        sa.Column("sort_order", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document_types"),
        sa.UniqueConstraint("code", name="uq_document_types_code"),
        sa.CheckConstraint("is_active IN (0, 1)", name="ck_document_types_is_active_bool"),
        sqlite_strict=True,
    )
    op.create_table(
        "sections",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("parent_id", sa.Integer),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("call_prefix", sa.Text),
        sa.Column("sort_order", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["sections.id"], name="fk_sections_parent_id_sections", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("code", name="uq_sections_code"),
        sa.CheckConstraint("is_active IN (0, 1)", name="ck_sections_is_active_bool"),
        sa.PrimaryKeyConstraint("id", name="pk_sections"),
        sqlite_strict=True,
    )
    op.create_index("ix_sections_parent_id", "sections", ["parent_id"])
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("label_norm", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.CheckConstraint("kind IN ('category','genre','keyword')", name="ck_tags_kind_values"),
        sa.PrimaryKeyConstraint("id", name="pk_tags"),
        sa.UniqueConstraint("kind", "label_norm", name="uq_tags_kind_label"),
        sqlite_strict=True,
    )
    op.create_table(
        "publishers",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_norm", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint("name_norm", name="uq_publishers_name_norm"),
        sa.PrimaryKeyConstraint("id", name="pk_publishers"),
        sqlite_strict=True,
    )
    op.create_table(
        "collections",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("publisher_id", sa.Integer),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_norm", sa.Text, nullable=False),
        sa.Column("issn", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint("publisher_id", "name_norm", name="uq_collections_publisher_name"),
        sa.PrimaryKeyConstraint("id", name="pk_collections"),
        sa.ForeignKeyConstraint(
            ["publisher_id"], ["publishers.id"], name="fk_collections_publisher_id_publishers", ondelete="SET NULL"
        ),
        sqlite_strict=True,
    )
    op.create_table(
        "media_files",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("mime", sa.Text, nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column("origin_url", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint("sha256", name="uq_media_files_sha256"),
        sa.CheckConstraint("mime IN ('image/jpeg','image/png','image/webp')", name="ck_media_files_mime_values"),
        sa.PrimaryKeyConstraint("id", name="pk_media_files"),
        sa.CheckConstraint("bytes > 0", name="ck_media_files_bytes_positive"),
        sqlite_strict=True,
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("sort_name", sa.Text, nullable=False),
        sa.Column("name_norm", sa.Text, nullable=False),
        sa.Column("ark_bnf", sa.Text),
        sa.Column("dates", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.UniqueConstraint("ark_bnf", name="uq_agents_ark_bnf"),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.CheckConstraint("kind IN ('person','organization')", name="ck_agents_kind_values"),
        sqlite_strict=True,
    )
    op.create_index("ix_agents_name_norm", "agents", ["name_norm"])
    op.create_index("ix_agents_sort_name", "agents", ["sort_name"])
    op.create_table(
        "series",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("title_sort", sa.Text, nullable=False),
        sa.Column("title_norm", sa.Text, nullable=False),
        sa.Column("parent_id", sa.Integer),
        sa.Column("call_prefix", sa.Text),
        sa.Column("next_call_sequence", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.CheckConstraint("next_call_sequence >= 1", name="ck_series_next_call_sequence_positive"),
        sa.UniqueConstraint("public_id", name="uq_series_public_id"),
        sa.PrimaryKeyConstraint("id", name="pk_series"),
        sa.UniqueConstraint("title_norm", name="uq_series_title_norm"),
        sa.CheckConstraint("status IN ('ongoing','complete','stopped','unknown')", name="ck_series_status_values"),
        sa.ForeignKeyConstraint(["parent_id"], ["series.id"], name="fk_series_parent_id_series", ondelete="SET NULL"),
        sa.UniqueConstraint("call_prefix", name="uq_series_call_prefix"),
        sqlite_strict=True,
    )
    op.create_index("ix_series_title_sort", "series", ["title_sort"])
    op.create_table(
        "series_expected_volumes",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("series_id", sa.Integer, nullable=False),
        sa.Column("volume_number", sa.REAL, nullable=False),
        sa.Column("volume_label", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("source_note", sa.Text),
        sa.Column("confirmed_by", sa.Integer),
        sa.Column("confirmed_at", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name="fk_series_expected_volumes_series_id_series", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_series_expected_volumes"),
        sa.UniqueConstraint("series_id", "volume_number", name="uq_series_expected_volumes"),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], ["users.id"], name="fk_series_expected_volumes_confirmed_by_users", ondelete="SET NULL"
        ),
        sa.CheckConstraint("volume_number >= 0", name="ck_series_expected_volumes_volume_number_positive"),
        sqlite_strict=True,
    )
    op.create_table(
        "works",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("document_type_id", sa.Integer, nullable=False),
        sa.Column("section_id", sa.Integer),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("subtitle", sa.Text),
        sa.Column("title_sort", sa.Text, nullable=False),
        sa.Column("title_norm", sa.Text, nullable=False),
        sa.Column("original_title", sa.Text),
        sa.Column("audience", sa.Text),
        sa.Column("summary", sa.Text),
        sa.Column("notes", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("completeness", sa.Integer, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_works_updated_by_users", ondelete="SET NULL"),
        sa.CheckConstraint("audience IN ('children','teen','adult','all')", name="ck_works_audience_values"),
        sa.CheckConstraint("length(trim(title)) > 0", name="ck_works_title_not_empty"),
        sa.UniqueConstraint("public_id", name="uq_works_public_id"),
        sa.PrimaryKeyConstraint("id", name="pk_works"),
        sa.CheckConstraint("completeness BETWEEN 0 AND 100", name="ck_works_completeness_range"),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_works_status_values"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_works_created_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["document_type_id"],
            ["document_types.id"],
            name="fk_works_document_type_id_document_types",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"], ["sections.id"], name="fk_works_section_id_sections", ondelete="SET NULL"
        ),
        sqlite_strict=True,
    )
    op.create_index("ix_works_completeness", "works", ["completeness"])
    op.create_index("ix_works_document_type_id", "works", ["document_type_id"])
    op.create_index("ix_works_section_id", "works", ["section_id"])
    op.create_index("ix_works_status_created_at", "works", ["status", "created_at"])
    op.create_index("ix_works_status_title_sort", "works", ["status", "title_sort"])
    op.create_index("ix_works_title_norm", "works", ["title_norm"])
    op.create_table(
        "work_contributors",
        sa.Column("work_id", sa.Integer, nullable=False),
        sa.Column("agent_id", sa.Integer, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(
            ["work_id"], ["works.id"], name="fk_work_contributors_work_id_works", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "role IN ('author','scenarist','artist','colorist','illustrator','translator','editor','adapter','other')",
            name="ck_work_contributors_role_values",
        ),
        sa.PrimaryKeyConstraint("work_id", "agent_id", "role", name="pk_work_contributors"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_work_contributors_agent_id_agents", ondelete="RESTRICT"
        ),
        sqlite_strict=True,
    )
    op.create_index("ix_work_contributors_agent_id", "work_contributors", ["agent_id"])
    op.create_table(
        "work_series",
        sa.Column("work_id", sa.Integer, nullable=False),
        sa.Column("series_id", sa.Integer, nullable=False),
        sa.Column("volume_number", sa.REAL),
        sa.Column("volume_label", sa.Text),
        sa.Column("call_sequence", sa.Integer),
        sa.Column("is_primary", sa.Integer, nullable=False),
        sa.CheckConstraint("is_primary IN (0, 1)", name="ck_work_series_is_primary_bool"),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name="fk_work_series_series_id_series", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], name="fk_work_series_work_id_works", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("work_id", "series_id", name="pk_work_series"),
        sa.CheckConstraint("volume_number >= 0", name="ck_work_series_volume_number_positive"),
        sa.UniqueConstraint("series_id", "call_sequence", name="uq_work_series_call"),
        sa.CheckConstraint("call_sequence >= 1", name="ck_work_series_call_sequence_positive"),
        sqlite_strict=True,
    )
    op.create_index("ix_work_series_series_volume", "work_series", ["series_id", "volume_number"])
    op.create_table(
        "work_tags",
        sa.Column("work_id", sa.Integer, nullable=False),
        sa.Column("tag_id", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], name="fk_work_tags_tag_id_tags", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], name="fk_work_tags_work_id_works", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("work_id", "tag_id", name="pk_work_tags"),
        sqlite_strict=True,
    )
    op.create_index("ix_work_tags_tag_id", "work_tags", ["tag_id"])
    op.create_table(
        "editions",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("work_id", sa.Integer, nullable=False),
        sa.Column("is_primary", sa.Integer, nullable=False),
        sa.Column("title_variant", sa.Text),
        sa.Column("publisher_id", sa.Integer),
        sa.Column("collection_id", sa.Integer),
        sa.Column("collection_number", sa.Text),
        sa.Column("publication_year", sa.Integer),
        sa.Column("publication_date", sa.Text),
        sa.Column("edition_statement", sa.Text),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("page_count", sa.Integer),
        sa.Column("physical_note", sa.Text),
        sa.Column("cover_media_id", sa.Integer),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_editions_created_by_users", ondelete="SET NULL"),
        sa.CheckConstraint("is_primary IN (0, 1)", name="ck_editions_is_primary_bool"),
        sa.CheckConstraint("publication_year BETWEEN 1400 AND 2200", name="ck_editions_publication_year_range"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], name="fk_editions_work_id_works", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["collection_id"], ["collections.id"], name="fk_editions_collection_id_collections", ondelete="SET NULL"
        ),
        sa.UniqueConstraint("public_id", name="uq_editions_public_id"),
        sa.CheckConstraint("page_count > 0", name="ck_editions_page_count_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_editions"),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_editions_status_values"),
        sa.ForeignKeyConstraint(
            ["publisher_id"], ["publishers.id"], name="fk_editions_publisher_id_publishers", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["cover_media_id"], ["media_files.id"], name="fk_editions_cover_media_id_media_files", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_editions_updated_by_users", ondelete="SET NULL"),
        sqlite_strict=True,
    )
    op.create_index("ix_editions_collection_id", "editions", ["collection_id"])
    op.create_index("ix_editions_language", "editions", ["language"])
    op.create_index("ix_editions_publication_year", "editions", ["publication_year"])
    op.create_index("ix_editions_publisher_id", "editions", ["publisher_id"])
    op.create_index("ix_editions_work_id", "editions", ["work_id"])
    op.create_table(
        "edition_contributors",
        sa.Column("edition_id", sa.Integer, nullable=False),
        sa.Column("agent_id", sa.Integer, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name="fk_edition_contributors_edition_id_editions", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "role IN ('author','scenarist','artist','colorist','illustrator','translator','editor','adapter','other')",
            name="ck_edition_contributors_role_values",
        ),
        sa.PrimaryKeyConstraint("edition_id", "agent_id", "role", name="pk_edition_contributors"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_edition_contributors_agent_id_agents", ondelete="RESTRICT"
        ),
        sqlite_strict=True,
    )
    op.create_index("ix_edition_contributors_agent_id", "edition_contributors", ["agent_id"])
    op.create_table(
        "edition_identifiers",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("edition_id", sa.Integer, nullable=False),
        sa.Column("scheme", sa.Text, nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_edition_identifiers"),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["editions.id"], name="fk_edition_identifiers_edition_id_editions", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("edition_id", "scheme", "value", name="uq_edition_identifiers"),
        sa.CheckConstraint(
            "scheme IN ('isbn13','isbn10','ean','issn','ark_bnf','ppn_sudoc','other')",
            name="ck_edition_identifiers_scheme_values",
        ),
        sqlite_strict=True,
    )
    op.create_index("ix_edition_identifiers_lookup", "edition_identifiers", ["scheme", "value"])
    op.create_table(
        "sequences",
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("prefix", sa.Text, nullable=False),
        sa.Column("padding", sa.Integer, nullable=False),
        sa.Column("last_value", sa.Integer, nullable=False),
        sa.CheckConstraint("last_value >= 0", name="ck_sequences_last_value_positive"),
        sa.CheckConstraint("padding BETWEEN 1 AND 12", name="ck_sequences_padding_range"),
        sa.PrimaryKeyConstraint("name", name="pk_sequences"),
        sqlite_strict=True,
    )
    op.create_table(
        "copies",
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
    op.create_index("ix_copies_edition_id", "copies", ["edition_id"])

    # Index partiels : au plus une édition principale et une série principale par œuvre.
    op.create_index(
        "ux_collections_no_publisher",
        "collections",
        ["name_norm"],
        unique=True,
        sqlite_where=sa.text("publisher_id IS NULL"),
    )
    op.create_index(
        "ux_work_series_primary", "work_series", ["work_id"], unique=True, sqlite_where=sa.text("is_primary = 1")
    )
    op.create_index("ux_editions_primary", "editions", ["work_id"], unique=True, sqlite_where=sa.text("is_primary = 1"))

    types = sa.table(
        "document_types",
        sa.column("code", sa.Text),
        sa.column("label", sa.Text),
        sa.column("icon", sa.Text),
        sa.column("sort_order", sa.Integer),
        sa.column("is_active", sa.Integer),
    )
    op.bulk_insert(
        types,
        [{"code": c, "label": la, "icon": i, "sort_order": s, "is_active": 1} for c, la, i, s in DOCUMENT_TYPES],
    )
    sequences = sa.table(
        "sequences",
        sa.column("name", sa.Text),
        sa.column("prefix", sa.Text),
        sa.column("padding", sa.Integer),
        sa.column("last_value", sa.Integer),
    )
    op.bulk_insert(sequences, [{"name": "copy_barcode", "prefix": "EX-", "padding": 6, "last_value": 0}])


def downgrade() -> None:
    """Refusée dès qu'une œuvre existe : la voie de retour est alors la sauvegarde prise avant la migration."""
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM works")).scalar()
    if count:
        raise RuntimeError(f"{count} œuvres existent. Restaurez la sauvegarde prise avant la migration.")
    for name in TABLES:
        op.drop_table(name)
