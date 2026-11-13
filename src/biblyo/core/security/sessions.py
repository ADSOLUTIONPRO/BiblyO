# SPDX-License-Identifier: EUPL-1.2
"""Sessions serveur : seule l'empreinte du jeton est en base (04-SECURITE-ROLES.md, § 1)."""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from biblyo.config import Settings
from biblyo.core.clock import from_iso, now, now_iso, to_iso
from biblyo.core.db import Database
from biblyo.core.models import AuthSession

log = logging.getLogger("biblyo.sessions")

COOKIE_NAME = "biblyo_session"
TOUCH_INTERVAL = timedelta(minutes=5)  # l'expiration glissante n'écrit pas à chaque requête
USER_AGENT_LENGTH = 200


@dataclass(frozen=True)
class AuthContext:
    """L'utilisateur de la requête : des valeurs simples, sans lien avec la session SQLAlchemy."""

    user_id: int
    username: str
    display_name: str
    role_codes: tuple[str, ...]
    role_labels: tuple[str, ...]
    permissions: frozenset[str]
    session_id: int
    csrf_token: str
    must_change_password: bool

    def can(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def open_session(session: Session, user_id: int, settings: Settings, ip: str | None, user_agent: str | None) -> str:
    """Crée une session dans la transaction de l'appelant et rend le jeton, qui n'existe qu'ici et dans le cookie."""
    raw = secrets.token_urlsafe(32)
    moment = now()
    session.execute(delete(AuthSession).where(AuthSession.user_id == user_id, AuthSession.expires_at <= to_iso(moment)))
    session.add(
        AuthSession(
            token_hash=hash_token(raw),
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=to_iso(moment),
            last_seen_at=to_iso(moment),
            expires_at=to_iso(moment + timedelta(days=settings.session_days)),
            ip=ip,
            user_agent=(user_agent or "")[:USER_AGENT_LENGTH] or None,
        )
    )
    return raw


def revoke_all(session: Session, user_id: int) -> None:
    session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


def resolve(database: Database, raw: str, settings: Settings) -> tuple[AuthContext | None, bool]:
    """Retrouve la session d'un jeton. Le second résultat indique si l'expiration vient d'être repoussée."""
    digest = hash_token(raw)
    with database.session() as session:
        row = session.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
        if row is None or row.expires_at <= now_iso() or not row.user.is_active:
            return None, False
        user = row.user
        context = AuthContext(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role_codes=tuple(r.code for r in user.roles),
            role_labels=tuple(r.label for r in user.roles),
            permissions=frozenset(p.permission for r in user.roles for p in r.permissions),
            session_id=row.id,
            csrf_token=row.csrf_token,
            must_change_password=user.must_change_password,
        )
        stale = now() - from_iso(row.last_seen_at) >= TOUCH_INTERVAL
    if not stale:
        return context, False
    moment = now()
    try:
        with database.session(write=True) as session:
            session.execute(
                update(AuthSession)
                .where(AuthSession.id == context.session_id)
                .values(
                    last_seen_at=to_iso(moment),
                    expires_at=to_iso(moment + timedelta(days=settings.session_days)),
                )
            )
    except SQLAlchemyError:
        log.warning("Prolongation de session impossible", exc_info=True)  # non bloquant : la session reste valide
        return context, False
    return context, True


def set_cookie(response: Response, raw: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        raw,
        max_age=settings.session_days * 86400,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure)


def get_auth(request: Request) -> AuthContext | None:
    """Utilisateur de la requête, résolu une seule fois. Aucune lecture tant que la base n'est pas prête."""
    if hasattr(request.state, "auth"):
        return request.state.auth  # type: ignore[no-any-return]
    auth: AuthContext | None = None
    raw = request.cookies.get(COOKIE_NAME)
    app = request.app
    if raw and app.state.schema.current().ok:
        try:
            auth, refreshed = resolve(app.state.database, raw, app.state.settings)
        except SQLAlchemyError:
            log.exception("Lecture de la session impossible")
        else:
            if refreshed:
                request.state.refresh_session_cookie = raw
    request.state.auth = auth
    return auth
