# SPDX-License-Identifier: EUPL-1.2
"""Cache des réponses des sources, en base : par source et par requête normalisée (D15).

Trouvé : 30 jours. Aucun résultat : 7 jours. Pannes et réponses invalides : jamais. Le corps conservé est la
réponse nettoyée, relue par le lecteur courant à chaque usage : une évolution du lecteur n'exige aucune purge.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from biblyo.core.clock import from_iso, now, to_iso
from biblyo.modules.enrichment.models import SourceCache

FOUND_DAYS = 30
NOT_FOUND_DAYS = 7
MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class CacheEntry:
    status: str
    total: int
    body: str
    fetched_at: str


def key_for(kind: str, *parts: str) -> str:
    """« isbn:9782070612758 », « search:2:<empreinte> », « fetch:ark:/… » : jamais le texte saisi en clair."""
    if kind in ("isbn", "fetch"):
        return f"{kind}:{':'.join(parts)}"
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{kind}:{digest}"


def get(session: Session, source: str, key: str) -> CacheEntry | None:
    row = session.scalar(select(SourceCache).where(SourceCache.source_code == source, SourceCache.query_key == key))
    if row is None:
        return None
    if from_iso(row.expires_at) <= now():
        session.delete(row)
        return None
    return CacheEntry(row.status, row.total, row.body or "", row.fetched_at)


def put(session: Session, source: str, key: str, *, status: str, total: int, body: str) -> None:
    if status not in ("found", "not_found") or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        return
    moment = now()
    days = FOUND_DAYS if status == "found" else NOT_FOUND_DAYS
    row = session.scalar(select(SourceCache).where(SourceCache.source_code == source, SourceCache.query_key == key))
    if row is None:
        row = SourceCache(source_code=source, query_key=key)
        session.add(row)
    row.status = status
    row.total = total
    row.body = body
    row.fetched_at = to_iso(moment)
    row.expires_at = to_iso(moment + timedelta(days=days))


def forget(session: Session, source: str, key: str) -> None:
    session.execute(delete(SourceCache).where(SourceCache.source_code == source, SourceCache.query_key == key))


def purge_expired(session: Session) -> int:
    stale = SourceCache.expires_at <= to_iso(now())
    count = session.scalar(select(func.count()).select_from(SourceCache).where(stale)) or 0
    session.execute(delete(SourceCache).where(stale))
    return int(count)


def clear(session: Session, source: str | None = None) -> int:
    statement = delete(SourceCache)
    counting = select(func.count()).select_from(SourceCache)
    if source:
        statement = statement.where(SourceCache.source_code == source)
        counting = counting.where(SourceCache.source_code == source)
    count = session.scalar(counting) or 0
    session.execute(statement)
    return int(count)
