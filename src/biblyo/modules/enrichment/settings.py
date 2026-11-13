# SPDX-License-Identifier: EUPL-1.2
"""Réglages des sources (écran E4) : activer, ordonner, cadencer, tester, vider le cache.

Les cadences ont un plancher (D16) : une installation ne peut pas interroger une source plus vite que ce que le
projet juge prudent, faute de limite publiée. La priorité est une suite sans trou, réécrite en entier à chaque
enregistrement pour respecter l'unicité.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select

from biblyo.core import audit
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.errors import FormErrors
from biblyo.core.i18n import _
from biblyo.core.settings_store import set_setting
from biblyo.modules.enrichment import cache
from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.base import Capability, SearchQuery
from biblyo.modules.enrichment.models import MetadataSource
from biblyo.modules.enrichment.sources import CONTACT_SETTING, SourceHub, contact_of

INTERVAL_FLOOR_MS = 500
BATCH_INTERVAL_FLOOR_MS = 2000
TIMEOUT_RANGE_MS = (1000, 60000)
#: ISBN public connu des deux sources : sert seulement à tester la connexion.
PROBE_ISBN13, PROBE_ISBN10 = "9782070612758", "2070612759"
_CONTACT = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


@dataclass(frozen=True)
class SourceInput:
    code: str
    enabled: bool
    interval_ms: str
    batch_interval_ms: str
    timeout_ms: str
    covers_enabled: bool


def _number(value: str, field: str, floor: int, ceiling: int, errors: dict[str, str]) -> int:
    text = value.strip()
    if not text.isdigit() or not floor <= int(text) <= ceiling:
        errors[field] = _("Indiquez un nombre de millisecondes entre {floor} et {ceiling}.").format(
            floor=floor, ceiling=ceiling
        )
        return floor
    return int(text)


def save(
    database: Database, rows: list[SourceInput], order: list[str], user_id: int | None, contact: str | None = None
) -> None:
    """Enregistre toutes les sources d'un coup : réglages, ordre de priorité et adresse de contact.

    Une source qui exige une adresse de contact (Open Library) ne s'active pas sans elle : l'adresse n'est jamais
    inventée, elle se renseigne ici."""
    with database.session(write=True) as session:
        sources = {s.code: s for s in session.scalars(select(MetadataSource))}
        errors: dict[str, str] = {}
        address = contact_of(session) if contact is None else contact.strip()
        if address and (len(address) > 120 or not _CONTACT.match(address)):
            errors["contact"] = _("Indiquez une adresse électronique valide, par exemple bibliotheque@exemple.fr.")
        for row in rows:
            if row.enabled and row.code in registry.CONTACT_REQUIRED and not address:
                errors[f"{row.code}_active"] = _(
                    "Renseignez d’abord l’adresse de contact : cette source demande qu’on puisse joindre la "
                    "bibliothèque. Sans elle, la source reste désactivée."
                )
        if sorted(order) != sorted(sources):
            errors["ordre"] = _("L’ordre doit citer chaque source une fois.")
        values: dict[str, tuple[bool, int, int, int, bool]] = {}
        for row in rows:
            if row.code not in sources:
                continue
            interval = _number(row.interval_ms, f"{row.code}_intervalle", INTERVAL_FLOOR_MS, 60000, errors)
            batch = _number(row.batch_interval_ms, f"{row.code}_lot", BATCH_INTERVAL_FLOOR_MS, 600000, errors)
            timeout = _number(row.timeout_ms, f"{row.code}_delai", *TIMEOUT_RANGE_MS, errors)
            values[row.code] = (row.enabled, interval, batch, timeout, row.covers_enabled)
        if errors:
            raise FormErrors(errors)
        moment = now_iso()
        # Unicité de la priorité : on passe d'abord par des valeurs hors de portée, puis on pose l'ordre voulu.
        for index, code in enumerate(order, start=1):
            sources[code].priority = 1000 + index
        session.flush()
        changes: dict[str, object] = {}
        for index, code in enumerate(order, start=1):
            source = sources[code]
            source.priority = index
            if code in values:
                enabled, interval, batch, timeout, covers_enabled = values[code]
                source.is_enabled = enabled
                source.interval_ms = interval
                source.batch_interval_ms = batch
                source.timeout_ms = timeout
                source.covers_enabled = covers_enabled and code in registry.COVER_SOURCES
            source.updated_at = moment
            source.updated_by = user_id
            changes[code] = {
                "active": source.is_enabled,
                "priorite": index,
                "intervalle_ms": source.interval_ms,
                "lot_ms": source.batch_interval_ms,
                "delai_ms": source.timeout_ms,
                "couvertures": source.covers_enabled,
            }
        if contact is not None:
            set_setting(session, CONTACT_SETTING, address, user_id)
        changes["contact_renseigne"] = bool(address)
        audit.record(session, "enrichment.sources_saved", "metadata_source", user_id=user_id, details=changes)


@dataclass(frozen=True)
class ProbeResult:
    code: str
    status: str
    elapsed_ms: int
    total: int
    detail: str


def probe(hub: SourceHub, code: str) -> ProbeResult:
    """Teste une source par un ISBN public connu, en ignorant le cache. Même désactivée, elle est essayée."""
    config = next((c for c in hub.configs() if c.code == code), None)
    if config is None:
        return ProbeResult(code, "refused", 0, 0, _("source inconnue"))
    connector = hub.connector(config)
    if connector is not None and Capability.SEARCH not in connector.capabilities:
        return _probe_covers(hub, config.code)
    query = SearchQuery(level=1, isbn13=PROBE_ISBN13, isbn10=PROBE_ISBN10)
    result, state = hub.search(config, query, limit=1, refresh=True)
    return ProbeResult(code, state.status, state.elapsed_ms, result.total, result.detail)


def _probe_covers(hub: SourceHub, code: str) -> ProbeResult:
    """Une source de couvertures seules : les images du même ISBN public, sans les garder."""
    import time

    started = time.monotonic()
    found, status = hub.find_covers(code, isbns=(PROBE_ISBN13,), title="", author="", force=True)
    elapsed = int((time.monotonic() - started) * 1000)
    return ProbeResult(code, status, elapsed, len(found), "")


def clear_cache(database: Database, code: str, user_id: int | None) -> int:
    with database.session(write=True) as session:
        count = cache.clear(session, code)
        audit.record(
            session,
            "enrichment.cache_cleared",
            "metadata_source",
            user_id=user_id,
            details={"source": code, "n": count},
        )
        return count


def cache_counts(database: Database) -> dict[str, int]:
    from sqlalchemy import func

    from biblyo.modules.enrichment.models import SourceCache

    with database.session() as session:
        return dict(
            session.execute(select(SourceCache.source_code, func.count()).group_by(SourceCache.source_code)).all()
        )
