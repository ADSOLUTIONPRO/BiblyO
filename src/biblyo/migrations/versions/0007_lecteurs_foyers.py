# SPDX-License-Identifier: EUPL-1.2
"""Lecteurs, cartes, foyers, responsables et coordonnées (lot L3).

Révision 0007. Elle **crée** huit tables et deux numéroteurs ; elle ne réécrit aucune table existante.

Rôles fournis (plan de L3, C2 à C4 et C7) : un rôle n'est ajusté **que s'il est resté exactement tel que la révision
0001 l'a livré**. Bénévole : `+ patrons.register`. Bibliothécaire : `+ patrons.register`, `+ patrons.export`,
`− patrons.anonymize`. Un rôle modifié localement, ou créé localement, n'est jamais touché : la révision le signale
dans le journal de l'application et dans la sortie de la commande. Chaque ajustement fait est inscrit dans
`role_permission_changes`.

Descente : **refusée dès qu'une donnée de lecteur existe** (lecteur, carte, foyer, lien, coordonnée, mise en
conservation). Ces données ne sont jamais supprimées en silence : la voie de retour est la sauvegarde prise avant la
migration. Sans données, la descente défait exactement les ajustements de rôles inscrits, puis retire les tables.
"""

from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

log = logging.getLogger("biblyo.migrate")

# Contenu des rôles tels que livrés par la révision 0001, recopié ici pour ne dépendre d'aucun code courant.
LIBRARIAN_0001 = frozenset(
    {
        "catalogue.view", "catalogue.edit", "catalogue.archive", "holdings.edit", "holdings.withdraw",
        "locations.manage", "patrons.view", "patrons.view_personal", "patrons.edit", "patrons.archive",
        "patrons.anonymize", "circulation.lend", "circulation.renew", "circulation.override", "circulation.history",
        "enrichment.search", "enrichment.review", "enrichment.batch", "imports.simulate", "imports.apply",
        "inventory.run", "labels.print", "admin.settings", "admin.identity", "admin.sources", "admin.audit",
    }
)  # fmt: skip
VOLUNTEER_0001 = frozenset(
    {"catalogue.view", "patrons.view", "circulation.lend", "circulation.renew", "inventory.run", "holdings.edit"}
)
#: rôle → (contenu attendu, permissions ajoutées, permissions retirées)
ADJUSTMENTS: dict[str, tuple[frozenset[str], tuple[str, ...], tuple[str, ...]]] = {
    "librarian": (LIBRARIAN_0001, ("patrons.register", "patrons.export"), ("patrons.anonymize",)),
    "volunteer": (VOLUNTEER_0001, ("patrons.register",), ()),
}
PATRON_TABLES = (
    "patron_holds",
    "contacts",
    "guardianships",
    "household_members",
    "patron_cards",
    "households",
    "patrons",
)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _journal(bind: sa.engine.Connection, action: str, details: dict[str, object]) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO audit_log (at, user_id, action, entity_type, entity_id, details)"
            " VALUES (:at, NULL, :action, 'role', NULL, :details)"
        ),
        {"at": _now(), "action": action, "details": json.dumps(details, ensure_ascii=False)},
    )


def _adjust_roles(bind: sa.engine.Connection) -> None:
    moment = _now()
    for code, (shipped, added, removed) in ADJUSTMENTS.items():
        role_id = bind.execute(sa.text("SELECT id FROM roles WHERE code = :code"), {"code": code}).scalar()
        if role_id is None:
            log.warning("Rôle « %s » absent : aucun ajustement.", code)
            continue
        current = {
            row[0]
            for row in bind.execute(
                sa.text("SELECT permission FROM role_permissions WHERE role_id = :role"), {"role": role_id}
            )
        }
        if current != shipped:
            log.warning(
                "Rôle « %s » personnalisé : laissé tel quel. À revoir à la main : ajouter %s, retirer %s.",
                code,
                ", ".join(added) or "rien",
                ", ".join(removed) or "rien",
            )
            _journal(bind, "migration.role_kept", {"revision": revision, "role": code})
            continue
        for permission in added:
            bind.execute(
                sa.text("INSERT INTO role_permissions (role_id, permission) VALUES (:role, :permission)"),
                {"role": role_id, "permission": permission},
            )
        for permission in removed:
            bind.execute(
                sa.text("DELETE FROM role_permissions WHERE role_id = :role AND permission = :permission"),
                {"role": role_id, "permission": permission},
            )
        for permission, change in [*((item, "added") for item in added), *((item, "removed") for item in removed)]:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permission_changes (revision, role_id, permission, change, applied_at)"
                    " VALUES (:revision, :role, :permission, :change, :at)"
                ),
                {"revision": revision, "role": role_id, "permission": permission, "change": change, "at": moment},
            )
        _journal(
            bind,
            "migration.role_adjusted",
            {"revision": revision, "role": code, "ajouts": list(added), "retraits": list(removed)},
        )


def upgrade() -> None:
    op.create_table(
        "role_permission_changes",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("revision", sa.Text, nullable=False),
        sa.Column("role_id", sa.Integer, nullable=False),
        sa.Column("permission", sa.Text, nullable=False),
        sa.Column("change", sa.Text, nullable=False),
        sa.Column("applied_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_role_permission_changes"),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_role_permission_changes_role_id_roles", ondelete="CASCADE"
        ),
        sa.CheckConstraint("change IN ('added','removed')", name="ck_role_permission_changes_change_values"),
        sqlite_strict=True,
    )
    op.create_index("ix_role_permission_changes_revision", "role_permission_changes", ["revision"])
    op.create_table(
        "patrons",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("number", sa.Text, nullable=False),
        sa.Column("first_name", sa.Text, nullable=False),
        sa.Column("last_name", sa.Text, nullable=False),
        sa.Column("name_norm", sa.Text, nullable=False),
        sa.Column("sort_key", sa.Text, nullable=False),
        sa.Column("birth", sa.Text),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("registered_on", sa.Text, nullable=False),
        sa.Column("membership_expires_on", sa.Text),
        sa.Column("notes", sa.Text),
        sa.Column("archived_at", sa.Text),
        sa.Column("anonymized_at", sa.Text),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("created_by", sa.Integer),
        sa.Column("updated_by", sa.Integer),
        sa.PrimaryKeyConstraint("id", name="pk_patrons"),
        sa.UniqueConstraint("public_id", name="uq_patrons_public_id"),
        sa.UniqueConstraint("number", name="uq_patrons_number"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="fk_patrons_created_by_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_patrons_updated_by_users", ondelete="SET NULL"),
        sa.CheckConstraint("category IN ('adult','minor')", name="ck_patrons_category_values"),
        sa.CheckConstraint(
            "status IN ('active','suspended','archived','anonymized')", name="ck_patrons_status_values"
        ),
        sa.CheckConstraint("status = 'anonymized' OR length(trim(last_name)) > 0", name="ck_patrons_name_present"),
        sa.CheckConstraint("version >= 1", name="ck_patrons_version_positive"),
        sqlite_strict=True,
    )
    op.create_index("ix_patrons_name_norm", "patrons", ["name_norm"])
    op.create_index("ix_patrons_sort_key", "patrons", ["sort_key"])
    op.create_index("ix_patrons_status", "patrons", ["status"])
    op.create_table(
        "patron_cards",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("number", sa.Text, nullable=False),
        sa.Column("patron_id", sa.Integer),
        sa.Column("issued_on", sa.Text, nullable=False),
        sa.Column("ended_on", sa.Text),
        sa.Column("end_reason", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_patron_cards"),
        sa.UniqueConstraint("number", name="uq_patron_cards_number"),
        sa.ForeignKeyConstraint(
            ["patron_id"], ["patrons.id"], name="fk_patron_cards_patron_id_patrons", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('lost','damaged','replaced','revoked','anonymized')",
            name="ck_patron_cards_end_reason_values",
        ),
        sa.CheckConstraint("(ended_on IS NULL) = (end_reason IS NULL)", name="ck_patron_cards_end_consistent"),
        sa.CheckConstraint(
            "patron_id IS NOT NULL OR ended_on IS NOT NULL", name="ck_patron_cards_detached_only_when_ended"
        ),
        sqlite_strict=True,
    )
    op.create_index(
        "ux_patron_cards_active", "patron_cards", ["patron_id"], unique=True, sqlite_where=sa.text("ended_on IS NULL")
    )
    op.create_index("ix_patron_cards_patron", "patron_cards", ["patron_id"])
    op.create_table(
        "households",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_norm", sa.Text, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("archived_at", sa.Text),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_households"),
        sa.UniqueConstraint("public_id", name="uq_households_public_id"),
        sa.CheckConstraint("kind IN ('family','structure')", name="ck_households_kind_values"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_households_name_not_empty"),
        sa.CheckConstraint("version >= 1", name="ck_households_version_positive"),
        sqlite_strict=True,
    )
    op.create_index("ix_households_name_norm", "households", ["name_norm"])
    op.create_table(
        "household_members",
        sa.Column("household_id", sa.Integer, nullable=False),
        sa.Column("patron_id", sa.Integer, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("joined_on", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("household_id", "patron_id", name="pk_household_members"),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name="fk_household_members_household_id_households",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["patron_id"], ["patrons.id"], name="fk_household_members_patron_id_patrons", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "role IN ('responsible','member','child','referent')", name="ck_household_members_role_values"
        ),
        sqlite_strict=True,
    )
    op.create_index("ix_household_members_patron", "household_members", ["patron_id"])
    op.create_table(
        "guardianships",
        sa.Column("minor_id", sa.Integer, nullable=False),
        sa.Column("guardian_id", sa.Integer, nullable=False),
        sa.Column("relation", sa.Text, nullable=False),
        sa.Column("can_borrow_for", sa.Integer, nullable=False),
        sa.Column("is_contact", sa.Integer, nullable=False),
        sa.Column("contact_rank", sa.Integer, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("minor_id", "guardian_id", name="pk_guardianships"),
        sa.ForeignKeyConstraint(
            ["minor_id"], ["patrons.id"], name="fk_guardianships_minor_id_patrons", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["guardian_id"], ["patrons.id"], name="fk_guardianships_guardian_id_patrons", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("minor_id <> guardian_id", name="ck_guardianships_distinct_people"),
        sa.CheckConstraint(
            "relation IN ('parent','legal_guardian','teacher','other')", name="ck_guardianships_relation_values"
        ),
        sa.CheckConstraint("can_borrow_for IN (0, 1)", name="ck_guardianships_can_borrow_for_bool"),
        sa.CheckConstraint("is_contact IN (0, 1)", name="ck_guardianships_is_contact_bool"),
        sa.CheckConstraint("contact_rank >= 1", name="ck_guardianships_contact_rank_positive"),
        sa.CheckConstraint("version >= 1", name="ck_guardianships_version_positive"),
        sqlite_strict=True,
    )
    op.create_index("ix_guardianships_guardian", "guardianships", ["guardian_id"])
    op.create_table(
        "contacts",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("patron_id", sa.Integer),
        sa.Column("household_id", sa.Integer),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("label", sa.Text),
        sa.Column("value", sa.Text),
        sa.Column("search_value", sa.Text),
        sa.Column("line1", sa.Text),
        sa.Column("line2", sa.Text),
        sa.Column("postal_code", sa.Text),
        sa.Column("city", sa.Text),
        sa.Column("country", sa.Text),
        sa.Column("is_primary", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_contacts"),
        sa.ForeignKeyConstraint(["patron_id"], ["patrons.id"], name="fk_contacts_patron_id_patrons", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], name="fk_contacts_household_id_households", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("(patron_id IS NULL) <> (household_id IS NULL)", name="ck_contacts_one_owner"),
        sa.CheckConstraint("kind IN ('email','phone','address')", name="ck_contacts_kind_values"),
        sa.CheckConstraint(
            "label IS NULL OR label IN ('home','mobile','work','other')", name="ck_contacts_label_values"
        ),
        sa.CheckConstraint("is_primary IN (0, 1)", name="ck_contacts_is_primary_bool"),
        sa.CheckConstraint(
            "(kind = 'address') = (line1 IS NOT NULL AND city IS NOT NULL AND value IS NULL)",
            name="ck_contacts_address_shape",
        ),
        sqlite_strict=True,
    )
    op.create_index("ix_contacts_patron", "contacts", ["patron_id"])
    op.create_index("ix_contacts_household", "contacts", ["household_id"])
    op.create_index("ix_contacts_search", "contacts", ["kind", "search_value"])
    op.create_table(
        "patron_holds",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column("patron_id", sa.Integer, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("until", sa.Text),
        sa.Column("placed_at", sa.Text, nullable=False),
        sa.Column("placed_by", sa.Integer),
        sa.Column("lifted_at", sa.Text),
        sa.Column("lifted_by", sa.Integer),
        sa.PrimaryKeyConstraint("id", name="pk_patron_holds"),
        sa.ForeignKeyConstraint(
            ["patron_id"], ["patrons.id"], name="fk_patron_holds_patron_id_patrons", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["placed_by"], ["users.id"], name="fk_patron_holds_placed_by_users", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["lifted_by"], ["users.id"], name="fk_patron_holds_lifted_by_users", ondelete="SET NULL"
        ),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_patron_holds_reason_not_empty"),
        sqlite_strict=True,
    )
    op.create_index("ix_patron_holds_patron", "patron_holds", ["patron_id"])
    sequences = sa.table(
        "sequences",
        sa.column("name", sa.Text),
        sa.column("prefix", sa.Text),
        sa.column("padding", sa.Integer),
        sa.column("last_value", sa.Integer),
    )
    op.bulk_insert(
        sequences,
        [
            {"name": "patron_number", "prefix": "P-", "padding": 6, "last_value": 0},
            {"name": "patron_card", "prefix": "L-", "padding": 6, "last_value": 0},
        ],
    )
    _adjust_roles(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    counts = {table: bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar() for table in PATRON_TABLES}  # noqa: S608
    filled = {table: count for table, count in counts.items() if count}
    if filled:
        detail = ", ".join(f"{table} : {count}" for table, count in filled.items())
        raise RuntimeError(
            f"Des données de lecteurs existent ({detail}). Elles ne sont jamais supprimées par un retour arrière : "
            "restaurez la sauvegarde prise avant la migration."
        )
    for role_id, permission, change in bind.execute(
        sa.text(
            "SELECT role_id, permission, change FROM role_permission_changes WHERE revision = :revision"
            " ORDER BY id DESC"
        ),
        {"revision": revision},
    ).all():
        if change == "added":
            bind.execute(
                sa.text("DELETE FROM role_permissions WHERE role_id = :role AND permission = :permission"),
                {"role": role_id, "permission": permission},
            )
        else:
            bind.execute(
                sa.text("INSERT OR IGNORE INTO role_permissions (role_id, permission) VALUES (:role, :permission)"),
                {"role": role_id, "permission": permission},
            )
    bind.exec_driver_sql("DELETE FROM sequences WHERE name IN ('patron_number', 'patron_card')")
    for table in PATRON_TABLES:
        op.drop_table(table)
    op.drop_table("role_permission_changes")
