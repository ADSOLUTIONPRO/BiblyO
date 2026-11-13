# SPDX-License-Identifier: EUPL-1.2
"""Concentrateur des sources : réglages, connecteurs, cache, espacement, réessais, mise de côté.

Aucun appel réseau n'a lieu dans une transaction : le cache est lu et écrit dans des transactions courtes, de part
et d'autre de l'appel. Un seul concentrateur par application, car le régulateur et la mise de côté sont partagés par
toutes les recherches.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.settings_store import get_setting
from biblyo.modules.enrichment import cache
from biblyo.modules.enrichment.connectors import registry, unimarc
from biblyo.modules.enrichment.connectors.base import (
    Capability,
    Connector,
    CoverRequest,
    CoverResult,
    SearchQuery,
    SourceResult,
)
from biblyo.modules.enrichment.connectors.transport import HttpTransport
from biblyo.modules.enrichment.models import MetadataSource
from biblyo.modules.enrichment.throttle import Sidelined, Throttle

RETRY_PAUSE_S = 1.0
BATCH_RETRY_PAUSES_S = (2.0, 8.0)


@dataclass(frozen=True)
class SourceConfig:
    code: str
    label: str
    enabled: bool
    priority: int
    interval_s: float
    batch_interval_s: float
    timeout_s: float
    covers_enabled: bool
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceState:
    """Ce que l'écran dit d'une source après une recherche."""

    code: str
    label: str
    status: str  # found, not_found, unavailable, timeout, throttled, refused, invalid_response, disabled, sidelined,
    # not_needed (active, mais la source prioritaire a tranché : jamais appelée)
    total: int = 0
    elapsed_ms: int = 0
    from_cache_at: str | None = None
    detail: str = ""
    sidelined_s: float = 0.0
    retried: bool = False

    @property
    def answered(self) -> bool:
        return self.status in ("found", "not_found")

    @property
    def settled(self) -> bool:
        """Rien ne manque de cette source : elle a répondu, est désactivée, ou n'avait pas à être appelée."""
        return self.answered or self.status in ("disabled", "not_needed")


def _config(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def load_configs(session: Session) -> list[SourceConfig]:
    rows = session.scalars(select(MetadataSource).order_by(MetadataSource.priority))
    return [
        SourceConfig(
            code=row.code,
            label=row.label,
            enabled=bool(row.is_enabled),
            priority=row.priority,
            interval_s=row.interval_ms / 1000,
            batch_interval_s=row.batch_interval_ms / 1000,
            timeout_s=row.timeout_ms / 1000,
            covers_enabled=bool(row.covers_enabled),
            config=_config(row.config),
        )
        for row in rows
    ]


CONTACT_SETTING = "enrichment.contact"


def contact_of(session: Session) -> str:
    """Adresse de contact donnée aux sources (en-tête User-Agent) ; vide tant que personne ne l'a renseignée."""
    value = get_setting(session, CONTACT_SETTING, "")
    return value.strip() if isinstance(value, str) else ""


def query_key(query: SearchQuery) -> str:
    if query.level == 1:
        return cache.key_for("isbn", query.isbn13 or query.isbn10)
    parts = (
        str(query.level),
        query.title,
        query.author,
        query.publisher,
        str(query.year or ""),
        query.series,
        query.volume,
    )
    return cache.key_for("search", *parts)


class SourceHub:
    def __init__(
        self,
        database: Database,
        *,
        transport: HttpTransport | None = None,
        throttle: Throttle | None = None,
        sleep: Callable[[float], None] = time.sleep,
        factory: Callable[[SourceConfig, HttpTransport], Connector | None] | None = None,
    ) -> None:
        self._database = database
        self._transport = transport or HttpTransport(registry.HOSTS)
        self.throttle = throttle or Throttle()
        self._sleep = sleep
        self._factory = factory or (
            lambda config, transport: registry.build(
                config.code, transport, timeout_s=config.timeout_s, config=config.config
            )
        )

    def configs(self) -> list[SourceConfig]:
        """Les sources par priorité. Une source qui exige une adresse de contact reste inactive tant qu'il n'y en a
        pas, quel que soit son réglage : on n'invente jamais cette adresse."""
        with self._database.session() as session:
            configs = load_configs(session)
            contact = contact_of(session)
        set_contact = getattr(self._transport, "set_contact", None)
        if callable(set_contact):
            set_contact(contact)
        if contact:
            return configs
        return [replace(c, enabled=False) if c.code in registry.CONTACT_REQUIRED else c for c in configs]

    def metadata_configs(self) -> list[SourceConfig]:
        """Les sources de notices. Une source qui ne fournit que des couvertures n'y figure jamais."""
        return [c for c in self.configs() if self._can(c, Capability.SEARCH)]

    def cover_configs(self) -> list[SourceConfig]:
        """Les sources capables de fournir une couverture, activées ou non, par priorité."""
        return [c for c in self.configs() if self._can(c, Capability.COVER)]

    def _can(self, config: SourceConfig, capability: Capability) -> bool:
        connector = self.connector(config)
        return connector is not None and capability in connector.capabilities

    def enabled(self, codes: tuple[str, ...] | None = None) -> list[SourceConfig]:
        return [c for c in self.configs() if c.enabled and (codes is None or c.code in codes)]

    def connector(self, config: SourceConfig) -> Connector | None:
        return self._factory(config, self._transport)

    # --- Cache ----------------------------------------------------------------------------------------------------
    def _cached(self, config: SourceConfig, key: str, connector: Connector) -> SourceResult | None:
        with self._database.session(write=True) as session:  # une entrée périmée est retirée à la lecture
            entry = cache.get(session, config.code, key)
        if entry is None:
            return None
        if entry.status == "not_found":
            return SourceResult("not_found", total=entry.total, from_cache_at=entry.fetched_at)
        try:
            data = json.loads(entry.body)
            records = tuple(unimarc.record_from_json(item) for item in data.get("records", []))
        except (ValueError, TypeError, AttributeError):
            return None
        return connector.from_records(records, total=int(data.get("total", 0)), cached_at=entry.fetched_at)

    def _store(self, config: SourceConfig, key: str, result: SourceResult) -> None:
        if not result.answered:
            return
        body = ""
        if result.status == "found":
            records = [unimarc.record_to_json(unimarc.record_of(c)) for c in result.candidates]
            body = json.dumps({"total": result.total, "records": records}, ensure_ascii=False)
        with self._database.session(write=True) as session:
            cache.put(session, config.code, key, status=result.status, total=result.total, body=body)

    # --- Appels ---------------------------------------------------------------------------------------------------
    def _call(
        self, config: SourceConfig, call: Callable[[], SourceResult], *, batch: bool
    ) -> tuple[SourceResult, bool]:
        """Un appel espacé, réessayé une fois (deux en lot) s'il le mérite ; rend le résultat et s'il a été réessayé."""
        interval = config.batch_interval_s if batch else config.interval_s
        pauses = BATCH_RETRY_PAUSES_S if batch else (RETRY_PAUSE_S,)
        retried = False
        attempts = 0
        while True:
            try:
                self.throttle.acquire(config.code, interval)
            except Sidelined as exc:
                return SourceResult("unavailable", detail=f"source mise de côté encore {exc.remaining:.0f} s"), retried
            result = call()
            if result.answered:
                self.throttle.succeeded(config.code)
                return result, retried
            if result.status in ("refused", "invalid_response"):
                return result, retried  # une erreur de requête ou de réponse ne vaut ni réessai ni mise de côté
            self.throttle.failed(config.code, retry_after=result.retry_after)
            if not result.retryable or attempts >= len(pauses) or result.retry_after:
                return result, retried
            self._sleep(pauses[attempts])
            attempts += 1
            retried = True

    def search(
        self, config: SourceConfig, query: SearchQuery, *, limit: int = 20, refresh: bool = False, batch: bool = False
    ) -> tuple[SourceResult, SourceState]:
        connector = self.connector(config)
        if connector is None:
            return SourceResult("unavailable", detail="connecteur absent"), SourceState(
                config.code, config.label, "unavailable"
            )
        key = query_key(query)
        if not refresh:
            cached = self._cached(config, key, connector)
            if cached is not None:
                return cached, self._state(config, cached, retried=False)
        result, retried = self._call(config, lambda: connector.search(query, limit=limit), batch=batch)
        self._store(config, key, result)
        return result, self._state(config, result, retried=retried)

    def fetch(
        self, config: SourceConfig, external_id: str, *, refresh: bool = False
    ) -> tuple[SourceResult, SourceState]:
        connector = self.connector(config)
        if connector is None:
            return SourceResult("unavailable", detail="connecteur absent"), SourceState(
                config.code, config.label, "unavailable"
            )
        key = cache.key_for("fetch", external_id)
        if not refresh:
            cached = self._cached(config, key, connector)
            if cached is not None:
                return cached, self._state(config, cached, retried=False)
        result, retried = self._call(config, lambda: connector.fetch(external_id), batch=False)
        self._store(config, key, result)
        return result, self._state(config, result, retried=retried)

    def cover(self, config: SourceConfig, request: CoverRequest) -> CoverResult:
        """Les couvertures ne passent pas par le cache : seule l'image retenue est conservée, par le service."""
        connector = self.connector(config)
        if connector is None or not config.covers_enabled:
            return CoverResult("unsupported", detail="couvertures désactivées pour cette source")
        code = f"{config.code}:covers"  # état propre au service de couvertures : sa panne ne touche pas les notices
        try:
            self.throttle.acquire(code, config.interval_s)
        except Sidelined as exc:
            return CoverResult("unavailable", detail=f"service de couvertures mis de côté encore {exc.remaining:.0f} s")
        result = connector.cover(request)
        if result.status in ("unavailable", "invalid_image"):
            self.throttle.failed(code)
        else:
            self.throttle.succeeded(code)
        return result

    def find_covers(
        self, code: str, *, isbns: tuple[str, ...], title: str, author: str, force: bool = False
    ) -> tuple[list[Any], str]:
        """Les images qu'une source de couvertures signale (Open Library). Activée et couvertures cochées, sauf
        `force` (test de connexion) ; espacée comme ses autres appels."""
        config = next((c for c in self.configs() if c.code == code), None)
        if config is None or not (force or (config.enabled and config.covers_enabled)):
            return [], "disabled"
        connector = self.connector(config)
        finder = getattr(connector, "find_covers", None)
        if not callable(finder):
            return [], "unsupported"
        try:
            self.throttle.acquire(config.code, config.interval_s)
        except Sidelined:
            return [], "sidelined"
        found, status = finder(isbns=isbns, title=title, author=author)
        if status in ("unavailable", "timeout"):
            self.throttle.failed(config.code)
        else:
            self.throttle.succeeded(config.code)
        return list(found), str(status)

    def _state(self, config: SourceConfig, result: SourceResult, *, retried: bool) -> SourceState:
        return SourceState(
            code=config.code,
            label=config.label,
            status=result.status,
            total=result.total,
            elapsed_ms=result.elapsed_ms,
            from_cache_at=result.from_cache_at,
            detail=result.detail,
            sidelined_s=self.throttle.sidelined_for(config.code),
            retried=retried,
        )

    def disabled_state(self, config: SourceConfig) -> SourceState:
        return SourceState(config.code, config.label, "disabled")

    def not_needed_state(self, config: SourceConfig) -> SourceState:
        return SourceState(config.code, config.label, "not_needed")

    def stamp(self) -> str:
        return now_iso()

    def close(self) -> None:
        self._transport.close()


def with_timeout(config: SourceConfig, timeout_s: float) -> SourceConfig:
    return replace(config, timeout_s=timeout_s)
