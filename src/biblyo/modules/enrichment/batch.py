# SPDX-License-Identifier: EUPL-1.2
"""Enrichissement de toute la bibliothèque (L7, T4) : une tâche **recherche**, la personne **valide**.

Une tâche vise des **éditions** (jamais des exemplaires : 929 exemplaires, 887 éditions, 887 recherches au plus). Pour
chacune, elle fait la recherche du service (BnF puis Sudoc, à la cadence « lot » de chaque source, avec ses pauses
en cas de quota ou de panne) et la classe en trois niveaux :

- **fiable** (`reliable`) : une édition de même ISBN ; ses informations manquantes peuvent être validées par petits
  lots, couverture comprise si l'on coche la case ;
- **à vérifier** (`plausible`) : édition probable, plusieurs éditions possibles, ou autre édition ; jamais validée en
  lot, toujours dans l'assistant, une à une ;
- **insuffisant** (`insufficient`) : rien d'assez proche ; rien n'est proposé, la recherche manuelle reste possible.

Aucune écriture dans les fiches ici, sauf `validate`, qui passe par `service.apply` (opérations traçables et
annulables). Open Library n'est jamais appelée en lot, et aucune image n'est téléchargée : la tâche note seulement si
la BnF **annonce** une couverture. Une édition déjà cherchée récemment, dont la fiche n'a pas changé, n'est pas
recherchée de nouveau : sa dernière recherche est reprise. Une tâche coupée par un redémarrage reprend l'état
« interrompue », et se relance d'un clic, là où elle s'était arrêtée.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.core.i18n import _
from biblyo.modules.catalogue.models import Edition, Work
from biblyo.modules.enrichment import assistant, service
from biblyo.modules.enrichment import repository as repo
from biblyo.modules.enrichment.diffing import NEVER_PRECHECKED
from biblyo.modules.enrichment.models import (
    EnrichmentJob,
    EnrichmentJobItem,
    EnrichmentProposal,
    EnrichmentSearch,
)
from biblyo.modules.enrichment.service import Acceptance, candidate_of
from biblyo.modules.enrichment.sources import SourceHub

log = logging.getLogger("biblyo.enrichment.batch")

SCOPES: dict[str, str] = {
    "all": _("Toutes les éditions"),
    "without_cover": _("Les éditions sans couverture"),
    "incomplete": _("Les fiches incomplètes"),
}
TIER_LABELS: dict[str, tuple[str, str]] = {
    "reliable": (_("Correspondance fiable"), "ok"),
    "plausible": (_("À vérifier"), "due"),
    "insufficient": (_("Aucune proposition satisfaisante"), "neutral"),
}
JOB_LABELS: dict[str, str] = {
    "running": _("En cours"),
    "paused": _("En pause"),
    "interrupted": _("Interrompue (redémarrage) : relancez-la"),
    "finished": _("Terminée"),
    "cancelled": _("Annulée"),
}
ACTIVE = ("running", "paused", "interrupted")
RECENT_DAYS = 30  # une recherche plus récente, sur une fiche inchangée, est reprise telle quelle
MAX_ATTEMPTS = 3
FAILURES_BEFORE_PAUSE = 5  # erreurs de suite : les sources sont sans doute indisponibles, la tâche se met en pause
VALIDATE_MAX = 25  # validation par petits lots


@dataclass(frozen=True)
class Counts:
    without_cover: int
    incomplete: int
    reliable: int
    plausible: int
    insufficient: int
    errors: int


@dataclass(frozen=True)
class BoardRow:
    item_id: int
    edition_id: int
    work_id: int
    title: str
    authors: str
    tier: str
    status: str
    cover_announced: bool
    has_cover: bool
    search_id: int | None
    error: str | None
    lead_title: str
    lead_edition: str


# --- Constitution d'une tâche ----------------------------------------------------------------------------------------
def _editions_in_scope(session: Session, scope: str) -> list[int]:
    query = (
        select(Edition.id)
        .join(Work, Work.id == Edition.work_id)
        .where(Work.status == "active", Edition.status == "active")
        .order_by(Work.title_sort, Edition.id)
    )
    if scope == "without_cover":
        query = query.where(Edition.cover_media_id.is_(None))
    elif scope == "incomplete":
        query = query.where(Work.completeness < 100)
    return list(session.scalars(query))


def active_job(database: Database) -> EnrichmentJob | None:
    with database.session() as session:
        return session.scalars(select(EnrichmentJob).where(EnrichmentJob.status.in_(ACTIVE))).first()


def start(database: Database, *, scope: str, user_id: int | None) -> int:
    """Crée la tâche et la liste de ses éditions (une seule fois chacune). Une seule tâche active à la fois."""
    if scope not in SCOPES:
        raise BusinessError(_("Choix de livres inconnu."))
    with database.session(write=True) as session:
        if session.scalars(select(EnrichmentJob).where(EnrichmentJob.status.in_(ACTIVE))).first():
            raise BusinessError(_("Une recherche de toute la bibliothèque est déjà en cours : reprenez-la."))
        editions = _editions_in_scope(session, scope)
        stamp = now_iso()
        job = EnrichmentJob(
            public_id=uuid.uuid4().hex,
            status="running",
            scope=json.dumps({"livres": scope}, ensure_ascii=False),
            total=len(editions),
            created_by=user_id,
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(job)
        session.flush()
        for position, edition_id in enumerate(editions, 1):
            session.add(EnrichmentJobItem(job_id=job.id, edition_id=edition_id, position=position, updated_at=stamp))
        audit.record(
            session,
            "enrichment.batch_started",
            "enrichment_job",
            job.id,
            user_id=user_id,
            details={"livres": scope, "editions": len(editions)},
        )
        return job.id


def _set_status(database: Database, job_id: int, status: str, *, user_id: int | None, allowed: tuple[str, ...]) -> None:
    with database.session(write=True) as session:
        job = session.get(EnrichmentJob, job_id)
        if job is None or job.status not in allowed:
            raise BusinessError(_("Cette recherche ne peut pas changer d’état maintenant."))
        job.status = status
        job.updated_at = now_iso()
        if status in ("finished", "cancelled"):
            job.finished_at = job.updated_at
        audit.record(session, f"enrichment.batch_{status}", "enrichment_job", job_id, user_id=user_id, details={})


def pause(database: Database, job_id: int, *, user_id: int | None) -> None:
    _set_status(database, job_id, "paused", user_id=user_id, allowed=("running",))


def resume(database: Database, job_id: int, *, user_id: int | None) -> None:
    _set_status(database, job_id, "running", user_id=user_id, allowed=("paused", "interrupted"))


def cancel(database: Database, job_id: int, *, user_id: int | None) -> None:
    _set_status(database, job_id, "cancelled", user_id=user_id, allowed=ACTIVE)


def recover_interrupted(database: Database) -> int:
    """Au démarrage : une tâche « en cours » a été coupée par l'arrêt du service ; elle devient « interrompue »."""
    with database.session(write=True) as session:
        jobs = session.scalars(select(EnrichmentJob).where(EnrichmentJob.status == "running")).all()
        for job in jobs:
            job.status = "interrupted"
            job.updated_at = now_iso()
        return len(jobs)


# --- Traitement d'une édition ----------------------------------------------------------------------------------------
def classify_search(database: Database, search_id: int) -> tuple[str, bool]:
    """Niveau et couverture annoncée d'une recherche enregistrée, sans aucun appel réseau."""
    with database.session() as session:
        rows = session.scalars(select(EnrichmentProposal).where(EnrichmentProposal.search_id == search_id)).all()
        candidates = [(row, candidate_of(row), json.loads(row.explanation)) for row in rows]
    kept = [(r, c, e) for r, c, e in candidates if r.match_class in ("exact", "strong", "multiple", "uncertain")]
    announced = any(c.has_cover for r, c, _e in kept if r.status == "pending")
    if not kept:
        return "insufficient", False
    codes = {assistant.reliability(r.match_class, e) for r, _c, e in kept}
    return ("reliable" if "same_edition" in codes else "plausible"), announced


def _recent_search(session: Session, edition_id: int, fingerprint: str) -> int | None:
    since = (datetime.now(UTC) - timedelta(days=RECENT_DAYS)).isoformat()
    return session.scalar(
        select(EnrichmentSearch.id)
        .where(
            EnrichmentSearch.edition_id == edition_id,
            EnrichmentSearch.fingerprint == fingerprint,
            EnrichmentSearch.created_at >= since,
        )
        .order_by(EnrichmentSearch.id.desc())
        .limit(1)
    )


def step(database: Database, hub: SourceHub, job_id: int) -> bool:
    """Traite l'édition suivante. Rend faux quand il n'y a plus rien à faire (terminée, en pause, annulée)."""
    with database.session() as session:
        job = session.get(EnrichmentJob, job_id)
        if job is None or job.status != "running":
            return False
        item = session.scalars(
            select(EnrichmentJobItem)
            .where(EnrichmentJobItem.job_id == job_id, EnrichmentJobItem.status == "pending")
            .order_by(EnrichmentJobItem.position)
            .limit(1)
        ).first()
        if item is None:
            pass
        else:
            item_id, edition_id, attempts = item.id, item.edition_id, item.attempts
            target = repo.load_target(session, edition_id)
            reused = _recent_search(session, edition_id, target.fingerprint) if target else None
            user_id = job.created_by
    if item is None:
        _set_status(database, job_id, "finished", user_id=None, allowed=("running",))
        return False
    error = ""
    search_id = reused
    if target is None:
        error = _("Cette édition n’existe plus.")
    elif search_id is None:
        try:
            outcome = service.search(database, hub, edition_id=edition_id, user_id=user_id, batch=True)
            search_id = outcome.search_id
            if outcome.outcome == "incomplete" and not any(s.status == "found" for s in outcome.states):
                error = _("Sources indisponibles pour le moment.")
        except BusinessError as exc:
            error = exc.message
        except Exception as exc:  # une édition en erreur n’arrête jamais la tâche
            log.exception("enrichissement global : édition %s", edition_id)
            error = type(exc).__name__
    tier, announced = classify_search(database, search_id) if search_id and not error else (None, False)
    with database.session(write=True) as session:
        row = session.get(EnrichmentJobItem, item_id)
        job = session.get(EnrichmentJob, job_id)
        if row is None or job is None:
            return False
        row.attempts = attempts + 1
        row.updated_at = now_iso()
        row.search_id = search_id
        if error and row.attempts < MAX_ATTEMPTS and target is not None:
            row.error = error  # reste à traiter : réessayée au passage suivant
            row.position = job.total + row.position  # repoussée en fin de liste
        elif error:
            row.status, row.error = "error", error
            job.processed += 1
            job.errors += 1
        else:
            row.status, row.error, row.tier, row.cover_announced = "done", None, tier, announced
            job.processed += 1
        job.updated_at = row.updated_at
        if error:
            job.last_error = error
            recent = session.scalars(
                select(EnrichmentJobItem.error)
                .where(EnrichmentJobItem.job_id == job_id)
                .order_by(EnrichmentJobItem.updated_at.desc())
                .limit(FAILURES_BEFORE_PAUSE)
            ).all()
            if len(recent) == FAILURES_BEFORE_PAUSE and all(recent):
                job.status = "paused"  # les sources semblent indisponibles : la personne relance plus tard
    return True


class Runner:
    """Un fil d'exécution par application, qui fait avancer la tâche active. Il s'arrête de lui-même quand la tâche
    est en pause, annulée ou terminée ; un nouveau lancement ou une reprise le redémarre."""

    def __init__(self, database: Database, hub_factory: Callable[[], SourceHub]) -> None:
        self.database = database
        self.hub_factory = hub_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def ensure(self, job_id: int) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(job_id,), name="enrichissement-global", daemon=True)
            self._thread.start()

    def _run(self, job_id: int) -> None:
        hub = self.hub_factory()
        while not self._stop.is_set():
            try:
                if not step(self.database, hub, job_id):
                    return
            except Exception:  # la tâche s’arrête, sans emporter le service
                log.exception("enrichissement global : tâche %s", job_id)
                return

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


# --- Tableau de travail ----------------------------------------------------------------------------------------------
def counts(database: Database, job_id: int | None) -> Counts:
    with database.session() as session:
        base = (
            select(func.count(Edition.id))
            .join(Work, Work.id == Edition.work_id)
            .where(Work.status == "active", Edition.status == "active")
        )
        without_cover = session.scalar(base.where(Edition.cover_media_id.is_(None))) or 0
        incomplete = session.scalar(base.where(Work.completeness < 100)) or 0
        tiers: dict[str, int] = {}
        errors = 0
        if job_id is not None:
            for tier, number in session.execute(
                select(EnrichmentJobItem.tier, func.count())
                .where(EnrichmentJobItem.job_id == job_id, EnrichmentJobItem.status == "done")
                .group_by(EnrichmentJobItem.tier)
            ):
                tiers[str(tier)] = number
            errors = (
                session.scalar(
                    select(func.count()).where(EnrichmentJobItem.job_id == job_id, EnrichmentJobItem.status == "error")
                )
                or 0
            )
    return Counts(
        without_cover,
        incomplete,
        tiers.get("reliable", 0),
        tiers.get("plausible", 0),
        tiers.get("insufficient", 0),
        errors,
    )


FILTERS: dict[str, str] = {
    "reliable": _("Correspondances fiables"),
    "covers": _("Couvertures proposées"),
    "plausible": _("À vérifier"),
    "insufficient": _("Sans proposition satisfaisante"),
    "errors": _("En erreur"),
}


def _lead(session: Session, search_id: int | None) -> tuple[str, str]:
    if search_id is None:
        return "", ""
    rows = session.scalars(
        select(EnrichmentProposal)
        .where(
            EnrichmentProposal.search_id == search_id,
            EnrichmentProposal.status == "pending",
            EnrichmentProposal.match_class.in_(("exact", "strong", "multiple", "uncertain")),
        )
        .order_by(EnrichmentProposal.score.desc(), EnrichmentProposal.id)
        .limit(1)
    ).all()
    if not rows:
        return "", ""
    candidate = candidate_of(rows[0])
    return candidate.work.title.value, assistant._edition_text(candidate)


def board(database: Database, job_id: int, *, view: str, page: int = 1, size: int = VALIDATE_MAX) -> list[BoardRow]:
    with database.session() as session:
        query = (
            select(EnrichmentJobItem, Edition, Work)
            .join(Edition, Edition.id == EnrichmentJobItem.edition_id)
            .join(Work, Work.id == Edition.work_id)
            .where(EnrichmentJobItem.job_id == job_id)
            .order_by(EnrichmentJobItem.position)
        )
        if view == "errors":
            query = query.where(EnrichmentJobItem.status == "error")
        elif view == "covers":
            query = query.where(
                EnrichmentJobItem.status == "done",
                EnrichmentJobItem.cover_announced.is_(True),
                Edition.cover_media_id.is_(None),
            )
        else:
            query = query.where(EnrichmentJobItem.status == "done", EnrichmentJobItem.tier == view)
        found = session.execute(query.offset((max(page, 1) - 1) * size).limit(size)).all()
        rows = []
        for item, edition, work in found:
            title, edition_text = _lead(session, item.search_id)
            target = repo.load_target(session, edition.id)
            rows.append(
                BoardRow(
                    item_id=item.id,
                    edition_id=edition.id,
                    work_id=work.id,
                    title=work.title,
                    authors=target.notice.authors_text if target else "",
                    tier=item.tier or "",
                    status=item.status,
                    cover_announced=item.cover_announced,
                    has_cover=edition.cover_media_id is not None,
                    search_id=item.search_id,
                    error=item.error,
                    lead_title=title,
                    lead_edition=edition_text,
                )
            )
        return rows


def latest_job(database: Database) -> EnrichmentJob | None:
    with database.session() as session:
        return session.scalars(select(EnrichmentJob).order_by(EnrichmentJob.id.desc()).limit(1)).first()


def history(database: Database, limit: int = 10) -> list[EnrichmentJob]:
    with database.session() as session:
        return list(session.scalars(select(EnrichmentJob).order_by(EnrichmentJob.id.desc()).limit(limit)))


# --- Validation par petits lots --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Validation:
    applied: int
    skipped: int


def _reliable_proposal(session: Session, search_id: int) -> int | None:
    for row in session.scalars(
        select(EnrichmentProposal)
        .where(EnrichmentProposal.search_id == search_id, EnrichmentProposal.status == "pending")
        .order_by(EnrichmentProposal.score.desc(), EnrichmentProposal.id)
    ):
        if assistant.reliability(row.match_class, json.loads(row.explanation)) == "same_edition":
            return int(row.id)
    return None


def validate(
    database: Database,
    hub: SourceHub,
    media_dir: Path,
    *,
    item_ids: list[int],
    with_cover: bool,
    user_id: int | None,
) -> Validation:
    """Ajoute les informations manquantes des éditions **fiables** choisies (même ISBN), et leur couverture si la
    case est cochée et que l'édition n'en a pas. Jamais l'ISBN, les auteurs ni le résumé ; jamais un remplacement.
    Les éditions « à vérifier » sont refusées ici : elles se valident une à une dans l'assistant."""
    if len(item_ids) > VALIDATE_MAX:
        raise BusinessError(_("Validez au plus {n} livres à la fois.").format(n=VALIDATE_MAX))
    applied = skipped = 0
    for item_id in item_ids:
        with database.session() as session:
            item = session.get(EnrichmentJobItem, item_id)
            if item is None or item.tier != "reliable" or item.search_id is None:
                skipped += 1
                continue
            proposal_id = _reliable_proposal(session, item.search_id)
        if proposal_id is None:
            skipped += 1
            continue
        try:
            _target, _candidate, _assessment, diffs = service.preview(database, proposal_id=proposal_id)
        except BusinessError:
            skipped += 1
            continue
        fields = {d.field for d in diffs if d.action == "fill" and d.field not in NEVER_PRECHECKED}
        if with_cover and any(d.field == "cover" and d.action == "fill" for d in diffs):
            fields.add("cover")
        if not fields:
            skipped += 1
            continue
        try:
            result = service.apply(
                database,
                proposal_id=proposal_id,
                acceptance=Acceptance(fields=frozenset(fields)),
                user_id=user_id,
                hub=hub,
                media_dir=media_dir,
            )
        except BusinessError:
            skipped += 1
            continue
        if result.applied:
            applied += 1
        else:
            skipped += 1
    return Validation(applied, skipped)
