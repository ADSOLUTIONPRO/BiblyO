# SPDX-License-Identifier: EUPL-1.2
"""Première installation : un jeton à usage unique empêche de s'approprier une installation neuve depuis le réseau."""

from __future__ import annotations

import hashlib
import logging
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biblyo.core.db import Database
from biblyo.core.models import User
from biblyo.core.settings_store import delete_setting, get_setting, set_setting

log = logging.getLogger("biblyo.install")

TOKEN_SETTING = "install.token_hash"  # noqa: S105


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_open(session: Session) -> bool:
    return session.scalar(select(func.count()).select_from(User)) == 0


def regenerate_token(database: Database) -> str | None:
    """Nouveau jeton, l'ancien cesse de valoir. Aucun si l'installation est déjà faite."""
    token = secrets.token_urlsafe(32)
    with database.session(write=True) as session:
        if not is_open(session):
            return None
        set_setting(session, TOKEN_SETTING, _digest(token))
    return token


def announce_token_if_needed(database: Database) -> None:
    """Sur une installation ouverte et sans jeton, en crée un et l'écrit dans le journal du conteneur."""
    with database.session() as session:
        needed = is_open(session) and get_setting(session, TOKEN_SETTING) is None
    if not needed:
        return
    token = regenerate_token(database)
    if token:
        log.warning("Installation à terminer sur /installation. Jeton à usage unique : %s", token)


def check_token(session: Session, token: str) -> bool:
    stored = get_setting(session, TOKEN_SETTING)
    return bool(stored) and bool(token) and secrets.compare_digest(str(stored), _digest(token))


def consume_token(session: Session) -> None:
    delete_setting(session, TOKEN_SETTING)
