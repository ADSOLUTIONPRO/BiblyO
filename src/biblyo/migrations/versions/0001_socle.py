# SPDX-License-Identifier: EUPL-1.2
"""Socle : réglages, journal, comptes, rôles, sessions.

Révision : 0001
Précédente : aucune
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from alembic import op

from biblyo.core.clock import now_iso
from biblyo.core.db import APPLICATION_ID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

LIBRARIAN_PERMISSIONS = (
    "catalogue.view",
    "catalogue.edit",
    "catalogue.archive",
    "holdings.edit",
    "holdings.withdraw",
    "locations.manage",
    "patrons.view",
    "patrons.view_personal",
    "patrons.edit",
    "patrons.archive",
    "patrons.anonymize",
    "circulation.lend",
    "circulation.renew",
    "circulation.override",
    "circulation.history",
    "enrichment.search",
    "enrichment.review",
    "enrichment.batch",
    "imports.simulate",
    "imports.apply",
    "inventory.run",
    "labels.print",
    "admin.settings",
    "admin.identity",
    "admin.sources",
    "admin.audit",
)
VOLUNTEER_PERMISSIONS = (
    "catalogue.view",
    "patrons.view",
    "circulation.lend",
    "circulation.renew",
    "inventory.run",
    "holdings.edit",
)
VIEWER_PERMISSIONS = ("catalogue.view",)


def upgrade() -> None:
    op.execute(f"PRAGMA application_id = {APPLICATION_ID}")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.Text(collation="NOCASE"), nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("is_active", sa.Integer, nullable=False),
        sa.Column("must_change_password", sa.Integer, nullable=False),
        sa.Column("failed_logins", sa.Integer, nullable=False),
        sa.Column("locked_until", sa.Text),
        sa.Column("last_login_at", sa.Text),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint("is_active IN (0, 1)", name="ck_users_is_active_bool"),
        sa.CheckConstraint("must_change_password IN (0, 1)", name="ck_users_must_change_password_bool"),
        sqlite_strict=True,
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("updated_by", sa.Integer),
        sa.PrimaryKeyConstraint("key", name="pk_settings"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="fk_settings_updated_by_users", ondelete="SET NULL"),
        sqlite_strict=True,
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("at", sa.Text, nullable=False),
        sa.Column("user_id", sa.Integer),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("entity_id", sa.Integer),
        sa.Column("details", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_audit_log_user_id_users", ondelete="SET NULL"),
        sqlite_strict=True,
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("is_system", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("code", name="uq_roles_code"),
        sa.CheckConstraint("is_system IN (0, 1)", name="ck_roles_is_system_bool"),
        sqlite_strict=True,
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Integer, nullable=False),
        sa.Column("permission", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission", name="pk_role_permissions"),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_role_permissions_role_id_roles", ondelete="CASCADE"
        ),
        sqlite_strict=True,
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("role_id", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_roles_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_user_roles_role_id_roles", ondelete="RESTRICT"),
        sqlite_strict=True,
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("csrf_token", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("last_seen_at", sa.Text, nullable=False),
        sa.Column("expires_at", sa.Text, nullable=False),
        sa.Column("ip", sa.Text),
        sa.Column("user_agent", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"),
        sqlite_strict=True,
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id"])

    # Amorçage : rôles fournis et identifiant de l'installation. Aucune donnée de bibliothèque.
    moment = now_iso()
    roles = sa.table(
        "roles",
        sa.column("id", sa.Integer),
        sa.column("code", sa.Text),
        sa.column("label", sa.Text),
        sa.column("is_system", sa.Integer),
        sa.column("created_at", sa.Text),
    )
    op.bulk_insert(
        roles,
        [
            {"id": 1, "code": "admin", "label": "Administrateur", "is_system": 1, "created_at": moment},
            {"id": 2, "code": "librarian", "label": "Bibliothécaire", "is_system": 0, "created_at": moment},
            {
                "id": 3,
                "code": "volunteer",
                "label": "Accueil / bénévole",
                "is_system": 0,
                "created_at": moment,
            },
            {"id": 4, "code": "viewer", "label": "Consultation", "is_system": 0, "created_at": moment},
        ],
    )
    # Contenu des rôles fournis (04-SECURITE-ROLES.md, § 3), écrit ici pour ne dépendre d'aucun code courant.
    # Une permission qu'aucun module ne déclare encore reste sans effet.
    role_permissions = sa.table("role_permissions", sa.column("role_id", sa.Integer), sa.column("permission", sa.Text))
    op.bulk_insert(
        role_permissions,
        [
            {"role_id": 1, "permission": "*"},
            *({"role_id": 2, "permission": permission} for permission in LIBRARIAN_PERMISSIONS),
            *({"role_id": 3, "permission": permission} for permission in VOLUNTEER_PERMISSIONS),
            *({"role_id": 4, "permission": permission} for permission in VIEWER_PERMISSIONS),
        ],
    )
    settings = sa.table(
        "settings", sa.column("key", sa.Text), sa.column("value", sa.Text), sa.column("updated_at", sa.Text)
    )
    op.bulk_insert(
        settings, [{"key": "installation.uuid", "value": json.dumps(str(uuid.uuid4())), "updated_at": moment}]
    )


def downgrade() -> None:
    for table in ("sessions", "user_roles", "role_permissions", "roles", "audit_log", "settings", "users"):
        op.drop_table(table)
