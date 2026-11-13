# SPDX-License-Identifier: EUPL-1.2
"""Accès aux comptes et aux rôles."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biblyo.core.models import AuthSession, Role, RolePermission, User, UserRole

ADMIN_ROLE = "admin"


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def get_by_username(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(User.username == username))


def list_users(session: Session) -> Sequence[User]:
    return session.scalars(select(User).order_by(User.display_name, User.id)).all()


def list_roles(session: Session) -> Sequence[Role]:
    return session.scalars(select(Role).order_by(Role.id)).all()


def roles_by_codes(session: Session, codes: Iterable[str]) -> Sequence[Role]:
    return session.scalars(select(Role).where(Role.code.in_(list(codes))).order_by(Role.id)).all()


def count_active_admins(session: Session) -> int:
    """Comptes actifs détenant toutes les permissions : un rôle portant `*`, en pratique le rôle Administrateur."""
    statement = (
        select(func.count(func.distinct(User.id)))
        .join(UserRole, UserRole.user_id == User.id)
        .join(RolePermission, RolePermission.role_id == UserRole.role_id)
        .where(User.is_active == True, RolePermission.permission == "*")  # noqa: E712
    )
    return session.scalar(statement) or 0


def list_sessions(session: Session, user_id: int, moment: str) -> Sequence[AuthSession]:
    statement = (
        select(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.expires_at > moment)
        .order_by(AuthSession.last_seen_at.desc())
    )
    return session.scalars(statement).all()
