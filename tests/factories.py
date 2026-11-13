# SPDX-License-Identifier: EUPL-1.2
"""Données de test, toutes synthétiques."""

from __future__ import annotations

from biblyo.config import Settings
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.models import User
from biblyo.core.security import passwords, sessions
from biblyo.modules.admin import repository as repo

PASSWORD = "Lecture-Partagee-42"


def make_user(
    settings: Settings,
    username: str = "camille",
    role: str = "volunteer",
    *,
    password: str = PASSWORD,
    display_name: str | None = None,
    active: bool = True,
    must_change: bool = False,
) -> int:
    database = Database(settings.database_path)
    moment = now_iso()
    with database.session(write=True) as session:
        account = User(
            username=username,
            display_name=display_name or username.capitalize(),
            password_hash=passwords.hash_password(password),
            is_active=active,
            must_change_password=must_change,
            created_at=moment,
            updated_at=moment,
        )
        account.roles = list(repo.roles_by_codes(session, [role]))
        session.add(account)
        session.flush()
        user_id = account.id
    database.dispose()
    return user_id


def open_session_for(settings: Settings, user_id: int) -> tuple[str, str]:
    """Ouvre une session et rend son jeton et son jeton CSRF."""
    database = Database(settings.database_path)
    with database.session(write=True) as session:
        raw = sessions.open_session(session, user_id, settings, "192.0.2.10", "Navigateur de test")
    context, _ = sessions.resolve(database, raw, settings)
    database.dispose()
    assert context is not None
    return raw, context.csrf_token


def make_notice(
    settings: Settings,
    title: str = "Le Lotus bleu",
    *,
    document_type: str = "bd",
    authors: tuple[str, ...] = (),
    series: str = "",
    volume: str = "",
    isbn: str = "",
    publisher: str = "",
    year: str = "",
    summary: str = "",
    categories: str = "",
    genres: str = "",
    keywords: str = "",
    section_id: int | None = None,
    audience: str = "",
    language: str = "fre",
    collection: str = "",
    pages: str = "",
    user_id: int | None = None,
) -> int:
    """Crée une notice par le service, comme l'interface : l'œuvre et sa première édition."""
    from sqlalchemy import select

    from biblyo.modules.catalogue import service
    from biblyo.modules.catalogue.models import DocumentType

    database = Database(settings.database_path)
    with database.session() as session:
        type_id = session.scalar(select(DocumentType.id).where(DocumentType.code == document_type))
    work_id = service.create_notice(
        database,
        service.IdentityInput(
            title=title,
            document_type_id=type_id,
            section_id=section_id,
            audience=audience,
            contributors=tuple(service.ContributorInput(name=name) for name in authors),
        ),
        service.SeriesInput(title=series, volume=volume),
        service.ContentInput(summary=summary, categories=categories, genres=genres, keywords=keywords),
        service.EditionInput(
            isbn=isbn, publisher=publisher, year=year, language=language, collection=collection, pages=pages
        ),
        user_id,
    )
    database.dispose()
    return work_id
