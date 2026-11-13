# SPDX-License-Identifier: EUPL-1.2
"""Tables du socle : réglages, journal, comptes, rôles, sessions (03-MODELE-DONNEES.md, § 3.1 et 3.2)."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from biblyo.core.models_base import TABLE_OPTIONS, Base, IntBool


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (TABLE_OPTIONS,)

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))


class AuditEntry(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_at", "at"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[str | None] = mapped_column(Text)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint("is_system IN (0, 1)", name="is_system_bool"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_system: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    permissions: Mapped[list[RolePermission]] = relationship(cascade="all, delete-orphan")


class RolePermissionChange(Base):
    """Ajustement d'un rôle fourni fait par une révision : la descente défait exactement ce qui est écrit ici.

    Une révision n'ajuste un rôle que s'il est resté tel que livré ; un rôle personnalisé n'est jamais touché.
    """

    __tablename__ = "role_permission_changes"
    __table_args__ = (
        CheckConstraint("change IN ('added','removed')", name="change_values"),
        Index("ix_role_permission_changes_revision", "revision"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[str] = mapped_column(Text, nullable=False)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    permission: Mapped[str] = mapped_column(Text, nullable=False)
    change: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[str] = mapped_column(Text, nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (TABLE_OPTIONS,)

    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission: Mapped[str] = mapped_column(Text, primary_key=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("is_active IN (0, 1)", name="is_active_bool"),
        CheckConstraint("must_change_password IN (0, 1)", name="must_change_password_bool"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text(collation="NOCASE"), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(IntBool, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(IntBool, nullable=False, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[str | None] = mapped_column(Text)
    last_login_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    roles: Mapped[list[Role]] = relationship(secondary="user_roles", lazy="selectin", order_by="Role.id")


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (TABLE_OPTIONS,)

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True)


class AuthSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user", "user_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(lazy="joined")
