# SPDX-License-Identifier: EUPL-1.2
"""Chiffres réels du tableau de bord : le catalogue et sa qualité, rien de simulé."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biblyo.core.security.sessions import AuthContext
from biblyo.modules.catalogue import repository as repo
from biblyo.modules.catalogue import series_view
from biblyo.modules.catalogue.listing import LOW_COMPLETENESS, Criteria, filtered_statement
from biblyo.modules.catalogue.models import Agent, Copy, Series, Work
from biblyo.modules.catalogue.rules import ACTIVE_COPY_EXCLUDED

RECENT = 8


@dataclass(frozen=True)
class QualityLine:
    label: str
    count: int
    href: str
    kind: str


@dataclass(frozen=True)
class CatalogueStats:
    works: int
    copies: int
    series: int
    contributors: int
    average: int
    no_isbn: int
    low: int
    no_cover: int
    no_summary: int
    incomplete_series: int
    recent: list[repo.CardView]


def _count(session: Session, criteria: Criteria) -> int:
    return session.scalar(filtered_statement(criteria).with_only_columns(func.count(), maintain_column_froms=True)) or 0


def compute(session: Session) -> CatalogueStats:
    works = repo.active_work_count(session)
    average = session.scalar(select(func.avg(Work.completeness)).where(Work.status == "active")) or 0
    copies = session.scalar(
        select(func.count()).select_from(Copy).where(Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
    )
    series = session.scalar(select(func.count()).select_from(Series))
    contributors = session.scalar(select(func.count()).select_from(Agent))
    recent_ids = list(
        session.scalars(
            select(Work.id)
            .where(Work.status == "active")
            .order_by(Work.created_at.desc(), Work.id.desc())
            .limit(RECENT)
        )
    )
    return CatalogueStats(
        works=works,
        copies=copies or 0,
        series=series or 0,
        contributors=contributors or 0,
        average=round(average),
        no_isbn=_count(session, Criteria(meta="sans-isbn")),
        low=_count(session, Criteria(meta="incomplet")),
        no_cover=_count(session, Criteria(meta="sans-couverture")),
        no_summary=_count(session, Criteria(meta="sans-resume")),
        incomplete_series=series_view.incomplete_count(session),
        recent=repo.load_cards(session, recent_ids),
    )


def dashboard_context(session: Session, auth: AuthContext) -> dict[str, Any]:
    """Section du catalogue sur l'accueil. Rien pour un compte sans droit de consultation."""
    if not auth.can("catalogue.view"):
        return {}
    return {"catalogue_stats": compute(session), "low_threshold": LOW_COMPLETENESS}
