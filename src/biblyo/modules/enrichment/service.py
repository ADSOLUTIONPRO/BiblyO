# SPDX-License-Identifier: EUPL-1.2
"""Enrichissement d'une fiche : recherche à cinq niveaux, propositions, application champ par champ, annulation.

Le catalogue n'est écrit que par son propre service (`catalogue.service.apply_*`), en une transaction avec les
décisions, la provenance, les ISBN candidats et le journal. Rien n'est écrit sans une case cochée ; aucun champ
rempli n'est remplacé sans la sienne ; un ISBN ne devient confirmé que si l'édition est établie ou si la personne
dit l'avoir vérifié sur le livre. Jamais d'appel réseau dans une transaction.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.core.i18n import _
from biblyo.core.ids import new_public_id
from biblyo.core.media import InvalidImage, StoredImage, store_image
from biblyo.modules.catalogue import repository as catalogue_repo
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.catalogue.models import Edition, EditionIdentifier, MediaFile, Work
from biblyo.modules.catalogue.rules import CONTRIBUTOR_ROLES
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.enrichment import covers, diffing, matching
from biblyo.modules.enrichment import repository as repo
from biblyo.modules.enrichment.connectors import unimarc
from biblyo.modules.enrichment.connectors.base import Candidate, SearchQuery
from biblyo.modules.enrichment.diffing import FieldDiff
from biblyo.modules.enrichment.matching import Assessment, LocalFiche
from biblyo.modules.enrichment.models import (
    EditionIsbnCandidate,
    EnrichmentDecision,
    EnrichmentProposal,
    EnrichmentSearch,
    FieldProvenance,
)
from biblyo.modules.enrichment.normalize import isbn10_of, isbn13_of
from biblyo.modules.enrichment.sources import SourceHub, SourceState

MAX_LEVEL_CANDIDATES = 20
STALE_MESSAGE = _(
    "Cette fiche a été modifiée depuis la recherche. La proposition est périmée : relancez la recherche pour "
    "comparer à nouveau."
)
WORK_FIELDS = frozenset({"title", "subtitle", "contributors", "summary", "keywords", "genres"})
EDITION_FIELDS = frozenset(
    {"isbn", "publisher", "collection", "collection_number", "year", "statement", "language", "pages", "physical_note"}
)
LICENCES = {
    "bnf": "Bibliothèque nationale de France — Licence Ouverte de l'État 2.0, mention de la source et de la date",
    "sudoc": "Abes (Sudoc) — Licence Ouverte de l'État, mention de la source et de la date de récupération",
}


# --- Recherche --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Criteria:
    isbn: str = ""
    title: str = ""
    author: str = ""
    publisher: str = ""
    year: str = ""
    series: str = ""
    volume: str = ""

    @staticmethod
    def from_fiche(fiche: LocalFiche) -> Criteria:
        return Criteria(
            isbn=fiche.isbn13[0] if fiche.isbn13 else "",
            title=fiche.title,
            author=fiche.contributors[0] if fiche.contributors else "",
            publisher=fiche.publisher,
            year=str(fiche.year) if fiche.year else "",
            series=fiche.series,
            volume=str(int(fiche.volume)) if fiche.volume is not None and float(fiche.volume).is_integer() else "",
        )

    def query(self, level: int) -> SearchQuery | None:
        """La requête d'un niveau, ou rien si les critères ne le permettent pas."""
        isbn13 = isbn13_of(self.isbn) or ""
        year = int(self.year) if self.year.strip().isdigit() else None
        if level == 1:
            if not isbn13:
                return None
            return SearchQuery(level=1, isbn13=isbn13, isbn10=isbn10_of(isbn13) or "")
        if level == 2 and not (self.title and self.author):
            return None
        if level == 3 and not (self.title and self.publisher):
            return None
        if level == 4 and not self.series:
            return None
        if level == 5 and not (self.title or self.author):
            return None
        return SearchQuery(
            level=level,
            isbn13=isbn13,
            title=self.title,
            author=self.author,
            publisher=self.publisher,
            year=year,
            series=self.series,
            volume=self.volume,
        )


@dataclass(frozen=True)
class SearchOutcome:
    search_id: int
    outcome: str
    assessments: tuple[Assessment, ...]
    states: tuple[SourceState, ...]
    fingerprint: str
    levels: tuple[int, ...]
    proposals: dict[str, int] = field(default_factory=dict)  # (source:external_id) → identifiant de proposition

    @property
    def complete(self) -> bool:
        return all(s.settled for s in self.states)


def search(
    database: Database,
    hub: SourceHub,
    *,
    edition_id: int,
    criteria: Criteria | None = None,
    sources: tuple[str, ...] | None = None,
    user_id: int | None,
    refresh: bool = False,
) -> SearchOutcome:
    """Interroge les sources niveau par niveau, note, classe, enregistre la recherche et ses propositions."""
    with database.session() as session:
        target = repo.load_target(session, edition_id)
    if target is None:
        raise BusinessError(_("Cette édition n’existe plus."))
    criteria = criteria or Criteria.from_fiche(target.fiche)
    return _run_search(
        database, hub, target.fiche, criteria, sources, edition_id, target.fingerprint, user_id=user_id, refresh=refresh
    )


def search_free(
    database: Database,
    hub: SourceHub,
    *,
    criteria: Criteria,
    sources: tuple[str, ...] | None = None,
    user_id: int | None,
    refresh: bool = False,
) -> SearchOutcome:
    """Recherche sans fiche (écran E3) : les critères tiennent lieu de fiche pour noter les candidats."""
    isbn = isbn13_of(criteria.isbn)
    fiche = LocalFiche(
        title=criteria.title,
        contributors=(criteria.author,) if criteria.author else (),
        isbn13=(isbn,) if isbn else (),
        publisher=criteria.publisher,
        year=int(criteria.year) if criteria.year.isdigit() else None,
        series=criteria.series,
        volume=float(criteria.volume) if criteria.volume.replace(",", ".").replace(".", "", 1).isdigit() else None,
        language="",
    )
    return _run_search(database, hub, fiche, criteria, sources, None, "", user_id=user_id, refresh=refresh)


def _run_search(
    database: Database,
    hub: SourceHub,
    fiche: LocalFiche,
    criteria: Criteria,
    sources: tuple[str, ...] | None,
    edition_id: int | None,
    fingerprint: str,
    *,
    user_id: int | None,
    refresh: bool,
) -> SearchOutcome:
    configs = hub.metadata_configs()
    # Une source désactivée est toujours dite « désactivée » ; une source active non cochée n'est pas montrée
    enabled = [c for c in configs if c.enabled and (sources is None or c.code in sources)]
    states: dict[str, SourceState] = {c.code: hub.disabled_state(c) for c in configs if not c.enabled}
    found: dict[tuple[str, str], tuple[Candidate, int]] = {}
    levels: list[int] = []

    for level in range(1, 6):
        query = criteria.query(level)
        if query is None or not enabled:
            continue
        levels.append(level)
        decided = False
        for index, config in enumerate(enabled):
            if level > 1 and index > 0 and decided:
                break  # D3 : la source suivante n'est appelée que si la première n'a pas tranché
            result, state = hub.search(config, query, limit=MAX_LEVEL_CANDIDATES, refresh=refresh)
            states[config.code] = _merge_state(states.get(config.code), state)
            for candidate in result.candidates:
                found.setdefault((candidate.source, candidate.external_id), (candidate, level))
            if _decides(fiche, [c for c, _lvl in found.values()]):
                decided = True
        if decided:
            break
    for config in enabled:  # jamais appelée parce que la source prioritaire a tranché (D3) : l'écran le dit
        states.setdefault(config.code, hub.not_needed_state(config))

    assessments = matching.classify(fiche, [matching.assess(fiche, c) for c, _lvl in found.values()])
    complete = all(s.settled for s in states.values())
    outcome = matching.outcome(assessments, complete=complete)
    with database.session(write=True) as session:
        record = EnrichmentSearch(
            edition_id=edition_id,
            criteria=json.dumps(asdict(criteria), ensure_ascii=False),
            sources=json.dumps([asdict(s) for s in states.values()], ensure_ascii=False),
            outcome=outcome,
            fingerprint=fingerprint,
            created_by=user_id,
            created_at=now_iso(),
        )
        session.add(record)
        session.flush()
        proposals: dict[str, int] = {}
        for assessment in assessments:
            candidate = assessment.candidate  # les candidats écartés sont conservés : l'écran dit pourquoi
            proposal = EnrichmentProposal(
                search_id=record.id,
                source_code=candidate.source,
                external_id=candidate.external_id,
                external_url=candidate.url,
                group_key=assessment.group,
                match_class=assessment.match_class,
                match_level=found[(candidate.source, candidate.external_id)][1],
                score=assessment.score,
                evidence=min(assessment.evidence, 100),
                explanation=json.dumps(assessment.explanation, ensure_ascii=False),
                payload=json.dumps(asdict(candidate), ensure_ascii=False),
                raw_record=json.dumps(unimarc.record_to_json(unimarc.record_of(candidate)), ensure_ascii=False),
                source_agency=candidate.source_agency,
                source_date=candidate.source_date or None,
                retrieved_at=now_iso(),
                status="pending" if assessment.match_class not in ("excluded", "none") else "rejected",
            )
            session.add(proposal)
            session.flush()
            proposals[f"{candidate.source}:{candidate.external_id}"] = proposal.id
        audit.record(
            session,
            "enrichment.search",
            "edition" if edition_id else "enrichment_search",
            edition_id or record.id,
            user_id=user_id,
            details={
                "recherche": record.id,
                "resultat": outcome,
                "niveaux": levels,
                "sources": [s.code for s in states.values()],
            },
        )
        return SearchOutcome(
            record.id, outcome, tuple(assessments), tuple(states.values()), fingerprint, tuple(levels), proposals
        )


def _decides(fiche: LocalFiche, candidates: list[Candidate]) -> bool:
    if not candidates:
        return False
    classified = matching.classify(fiche, [matching.assess(fiche, c) for c in candidates])
    return matching.outcome(classified, complete=True) in ("exact", "strong")


def _merge_state(previous: SourceState | None, current: SourceState) -> SourceState:
    """Plusieurs niveaux pour une source : le nombre de résultats s'ajoute, le pire état l'emporte sur une réponse."""
    if previous is None or previous.status == "disabled":
        return current
    status = current.status if not current.answered or previous.answered else previous.status
    return replace(
        current,
        status=status,
        total=previous.total + current.total,
        elapsed_ms=previous.elapsed_ms + current.elapsed_ms,
        retried=previous.retried or current.retried,
        from_cache_at=current.from_cache_at or previous.from_cache_at,
    )


# --- Application ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Acceptance:
    """Ce que la personne a coché. Les contributeurs à ajouter sont nommés avec le rôle choisi."""

    fields: frozenset[str] = frozenset()
    contributors: tuple[tuple[str, str], ...] = ()  # (nom, rôle de biblyO)
    confirm_isbn: bool = False  # « J'ai vérifié cet ISBN sur le livre »
    replace_cover: bool = False  # « Remplacer la couverture actuelle » : confirmation dédiée


@dataclass(frozen=True)
class ApplyResult:
    operation_id: str
    applied: tuple[str, ...]
    rejected: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]  # (champ, motif)
    isbn_status: str = ""  # confirmed, proposed, rejected ou vide
    cover_status: str = ""  # état de la couverture demandée (covers.STATUS_LABELS), vide si non demandée


def _load_pending(session: Session, proposal_id: int) -> tuple[EnrichmentProposal, EnrichmentSearch]:
    proposal = session.get(EnrichmentProposal, proposal_id)
    if proposal is None:
        raise BusinessError(_("Cette proposition n’existe plus."))
    if proposal.status != "pending":
        raise BusinessError(_("Cette proposition a déjà été traitée."))
    search_row = session.get(EnrichmentSearch, proposal.search_id)
    if search_row is None or search_row.edition_id is None:
        raise BusinessError(_("Cette proposition n’est rattachée à aucune édition."))
    return proposal, search_row


def candidate_of(proposal: EnrichmentProposal) -> Candidate:
    """Le candidat relu depuis la notice conservée, par le lecteur courant, avec son adresse d'origine."""
    record = unimarc.record_from_json(json.loads(proposal.raw_record))
    candidate = unimarc.candidate(
        record, source=proposal.source_code, url=proposal.external_url, external_id=proposal.external_id
    )
    return candidate


def preview(database: Database, *, proposal_id: int) -> tuple[repo.Target, Candidate, Assessment, list[FieldDiff]]:
    """La fiche, le candidat, son évaluation et les différences : ce que l'écran de comparaison affiche."""
    with database.session() as session:
        proposal, search_row = _load_pending(session, proposal_id)
        target = repo.load_target(session, search_row.edition_id or 0)
    if target is None:
        raise BusinessError(_("Cette édition n’existe plus."))
    candidate = candidate_of(proposal)
    assessment = matching.assess(target.fiche, candidate)
    assessment = replace(assessment, match_class=proposal.match_class)  # type: ignore[arg-type]
    diffs = diffing.diff(
        target.values, candidate, match_class=proposal.match_class, edition_usable=assessment.edition_usable
    )
    return target, candidate, assessment, diffs


@dataclass(frozen=True)
class _CoverOutcome:
    status: str
    stored: StoredImage | None = None
    ark: str = ""
    url: str = ""
    reason: str = ""


def _prepare_cover(
    candidate: Candidate,
    diff: FieldDiff | None,
    acceptance: Acceptance,
    hub: SourceHub | None,
    media_dir: Path | None,
) -> _CoverOutcome | None:
    """Hors transaction : demande l'image d'origine, la contrôle et la range. Rien n'est rattaché ici."""
    if "cover" not in acceptance.fields or diff is None or not diff.applicable:
        return None
    if diff.action == "replace" and not acceptance.replace_cover:
        return _CoverOutcome("not_confirmed", reason=_("remplacement de la couverture non confirmé"))
    if hub is None or media_dir is None:
        return _CoverOutcome("unsupported", reason=covers.STATUS_LABELS["unsupported"])
    result = covers.fetch(hub, candidate, original=True)
    if result.status != "found":
        return _CoverOutcome(result.status, reason=covers.STATUS_LABELS.get(result.status, result.status))
    try:
        stored = store_image(result.data, media_dir)
    except InvalidImage:
        return _CoverOutcome("invalid_image", reason=covers.STATUS_LABELS["invalid_image"])
    return _CoverOutcome("found", stored, covers.ark_for(candidate), covers.public_url(candidate))


def apply(
    database: Database,
    *,
    proposal_id: int,
    acceptance: Acceptance,
    user_id: int | None,
    hub: SourceHub | None = None,
    media_dir: Path | None = None,
) -> ApplyResult:
    """Applique les champs cochés en une transaction, avec décisions, provenance, ISBN candidats et journal.

    La couverture, si elle est cochée, est demandée et contrôlée avant la transaction : aucun appel réseau n'a lieu
    pendant l'écriture, et son échec n'empêche jamais d'appliquer les autres champs.
    """
    with database.session() as session:
        proposal, search_row = _load_pending(session, proposal_id)
        target = repo.load_target(session, search_row.edition_id or 0)
        fingerprint = search_row.fingerprint
        early_candidate = candidate_of(proposal)
        match_class = proposal.match_class
    if target is None:
        raise BusinessError(_("Cette édition n’existe plus."))
    if target.fingerprint != fingerprint:
        with database.session(write=True) as session:
            stale_row = session.get(EnrichmentProposal, proposal_id)
            if stale_row is not None and stale_row.status == "pending":
                stale_row.status = "stale"
        raise ConflictError(STALE_MESSAGE)
    early_assessment = matching.assess(target.fiche, early_candidate)
    early_diffs = diffing.diff(
        target.values, early_candidate, match_class=match_class, edition_usable=early_assessment.edition_usable
    )
    cover = _prepare_cover(
        early_candidate, next((d for d in early_diffs if d.field == "cover"), None), acceptance, hub, media_dir
    )

    with database.session(write=True) as session:
        proposal, search_row = _load_pending(session, proposal_id)
        work = session.get(Work, target.notice.id)
        edition = session.get(Edition, target.edition.id)
        if work is None or edition is None:
            raise BusinessError(_("Cette édition n’existe plus."))
        if work.version != target.notice.version or edition.version != target.edition.version:
            raise ConflictError(catalogue.CONFLICT_MESSAGE)
        candidate = candidate_of(proposal)
        assessment = matching.assess(target.fiche, candidate)
        diffs = diffing.diff(
            target.values, candidate, match_class=proposal.match_class, edition_usable=assessment.edition_usable
        )
        operation = new_public_id()
        moment = now_iso()
        applied: list[str] = []
        rejected: list[str] = []
        skipped: list[tuple[str, str]] = []
        changes: dict[str, tuple[Any, Any, FieldDiff]] = {}  # champ → (ancienne, nouvelle, différence)

        by_field = {d.field: d for d in diffs}
        wanted = {f for f in acceptance.fields if f in by_field and by_field[f].applicable}
        for name in acceptance.fields - wanted:
            if name in by_field and not by_field[name].applicable:
                skipped.append((name, _("champ non applicable")))
        if cover is not None and cover.stored is None:
            skipped.append(("cover", cover.reason))
            wanted.discard("cover")

        # Œuvre
        identity_changed = False
        contributors = [
            catalogue.ContributorInput(name=c.name, role=c.role, choice=str(c.agent_id))
            for c in target.notice.contributors
        ]
        new_people: list[tuple[str, str]] = []
        if "contributors" in wanted:
            diff = by_field["contributors"]
            proposals_by_name = {p.name: p for p in diff.payload}
            for name, role in acceptance.contributors:
                proposal_item = proposals_by_name.get(name)
                if proposal_item is None or proposal_item.already_present:
                    skipped.append((f"contributeur:{name}", _("non proposé ou déjà présent")))
                    continue
                if role not in CONTRIBUTOR_ROLES:
                    skipped.append((f"contributeur:{name}", _("rôle inconnu")))
                    continue
                homonyms = catalogue_repo.agents_named(session, name)
                if len(homonyms) > 1:
                    skipped.append(
                        (f"contributeur:{name}", _("plusieurs personnes portent ce nom : à choisir à la main"))
                    )
                    continue
                contributors.append(catalogue.ContributorInput(name=name, role=role, dates=proposal_item.dates))
                new_people.append((name, role))
            if new_people:
                identity_changed = True
                changes["contributors"] = (
                    repo.current_value(target.values, "contributors"),
                    [list(pair) for pair in target.values.contributors] + [list(p) for p in new_people],
                    diff,
                )
            else:
                skipped.append(("contributors", _("aucun contributeur retenu")))
        title = target.notice.title
        subtitle = target.notice.subtitle or ""
        if "title" in wanted:
            title = by_field["title"].proposed
            changes["title"] = (target.values.title, title, by_field["title"])
            identity_changed = True
        if "subtitle" in wanted:
            subtitle = by_field["subtitle"].proposed
            changes["subtitle"] = (target.values.subtitle, subtitle, by_field["subtitle"])
            identity_changed = True
        if identity_changed:
            catalogue.apply_identity(
                session,
                work,
                catalogue.IdentityInput(
                    title=title,
                    document_type_id=target.notice.document_type.id,
                    subtitle=subtitle,
                    original_title=target.notice.original_title or "",
                    section_id=target.notice.section.id if target.notice.section else None,
                    audience=target.notice.audience or "",
                    contributors=tuple(contributors),
                ),
                user_id,
            )

        summary = target.notice.summary or ""
        keywords = list(target.notice.keywords)
        genres = list(target.notice.genres)
        content_changed = False
        if "summary" in wanted:
            summary = by_field["summary"].proposed
            changes["summary"] = (target.values.summary, summary, by_field["summary"])
            content_changed = True
        if "keywords" in wanted:
            added = [k.strip() for k in by_field["keywords"].proposed.split(", ") if k.strip()]
            keywords = keywords + [k for k in added if k not in keywords]
            changes["keywords"] = (list(target.notice.keywords), keywords, by_field["keywords"])
            content_changed = True
        if "genres" in wanted:
            added = [g.strip() for g in by_field["genres"].proposed.split(", ") if g.strip()]
            genres = genres + [g for g in added if g not in genres]
            changes["genres"] = (list(target.notice.genres), genres, by_field["genres"])
            content_changed = True
        if content_changed:
            catalogue.apply_content(
                session,
                work,
                catalogue.ContentInput(
                    summary=summary,
                    notes=target.notice.notes or "",
                    categories=", ".join(target.notice.categories),
                    genres=", ".join(genres),
                    keywords=", ".join(keywords),
                ),
                user_id,
            )

        # Édition
        isbn_status = ""
        edition_values = {
            "publisher": target.values.publisher,
            "collection": target.values.collection,
            "collection_number": target.values.collection_number,
            "year": target.values.year,
            "statement": target.values.statement,
            "language": target.values.language,
            "pages": target.values.pages,
            "physical_note": target.values.physical_note,
        }
        edition_changed = False
        for name in EDITION_FIELDS - {"isbn"}:
            if name in wanted:
                edition_values[name] = by_field[name].proposed
                changes[name] = (repo.current_value(target.values, name), by_field[name].proposed, by_field[name])
                edition_changed = True
        isbn_text = ", ".join(target.edition.isbn13)
        if "isbn" in by_field and by_field["isbn"].applicable:
            new_isbn = [v.strip() for v in by_field["isbn"].proposed.split(",") if v.strip()]
            established = proposal.match_class == "exact" or (
                proposal.match_class == "strong" and _same_publisher_and_year(assessment)
            )
            if "isbn" in wanted and (established or acceptance.confirm_isbn):
                isbn_status = "confirmed"
                isbn_text = ", ".join([*target.edition.isbn13, *new_isbn])
                changes["isbn"] = (list(target.edition.isbn13), [*target.edition.isbn13, *new_isbn], by_field["isbn"])
                edition_changed = True
            elif "isbn" in wanted:
                isbn_status = "proposed"
                skipped.append(("isbn", _("édition non établie : l'ISBN reste proposé, non confirmé")))
            else:
                isbn_status = "rejected"
            for value in new_isbn:
                _record_isbn_candidate(session, edition.id, value, isbn_status, proposal, assessment, user_id, moment)
            if isbn_status != "confirmed":
                wanted.discard("isbn")
        if edition_changed:
            catalogue.apply_edition_fields(
                session,
                edition,
                catalogue.EditionInput(
                    isbn=isbn_text,
                    publisher=edition_values["publisher"],
                    collection=edition_values["collection"],
                    collection_number=edition_values["collection_number"],
                    year=edition_values["year"],
                    date=target.edition.date or "",
                    statement=edition_values["statement"],
                    language=edition_values["language"],
                    pages=edition_values["pages"],
                    physical_note=edition_values["physical_note"],
                    title_variant=target.edition.title_variant or "",
                    notes=target.edition.notes or "",
                    contributors=tuple(
                        catalogue.ContributorInput(name=c.name, role=c.role, choice=str(c.agent_id))
                        for c in target.edition.contributors
                    ),
                ),
                user_id,
            )

        if cover is not None and cover.stored is not None and "cover" in wanted:
            retrieved = now_iso()
            catalogue.attach_cover(
                session,
                edition,
                cover.stored,
                user_id,
                provenance=catalogue.CoverOrigin(
                    origin="bnf",
                    origin_url=cover.url,
                    source_code="bnf",
                    external_id=cover.ark,
                    license_note=covers.licence_note(retrieved),
                    retrieved_at=retrieved,
                ),
                change=catalogue.CoverChangeInput("enrichment", source_code="bnf", reference=cover.ark),
            )
            changes["cover"] = (target.values.cover_sha256, cover.stored.sha256, by_field["cover"])

        # Décisions, provenance, état de la proposition
        licence = LICENCES.get(candidate.source, candidate.source)
        skipped_names = {name for name, _reason in skipped}
        for name, (old, new, diff) in changes.items():
            entity_type = "work" if name in WORK_FIELDS else "edition"
            entity_id = work.id if entity_type == "work" else edition.id
            decision = EnrichmentDecision(
                proposal_id=proposal.id,
                operation_id=operation,
                entity_type=entity_type,
                entity_id=entity_id,
                field=name,
                decision="accepted",
                action=diff.action,
                old_value=json.dumps(old, ensure_ascii=False),
                new_value=json.dumps(new, ensure_ascii=False),
                decided_by=user_id,
                decided_at=moment,
            )
            session.add(decision)
            session.flush()
            sub_source = candidate.work.summary.provider if name == "summary" and candidate.work.summary else None
            provenance = session.get(FieldProvenance, (entity_type, entity_id, name))
            if provenance is None:
                provenance = FieldProvenance(entity_type=entity_type, entity_id=entity_id, field=name)
                session.add(provenance)
            is_cover = name == "cover" and cover is not None
            provenance.source_code = "bnf" if is_cover else candidate.source
            provenance.external_id = cover.ark if is_cover and cover else candidate.external_id
            provenance.external_url = cover.url if is_cover and cover else candidate.url
            provenance.sub_source = sub_source or None
            provenance.reuse_status = "established"
            provenance.license_note = covers.licence_note(moment) if is_cover else licence
            provenance.source_date = candidate.source_date or None
            provenance.retrieved_at = moment if is_cover else proposal.retrieved_at
            provenance.value_sha256 = repo.value_hash(new)
            provenance.decision_id = decision.id
            applied.append(name)
        for diff in diffs:
            if diff.applicable and diff.field not in changes and diff.field not in skipped_names:
                session.add(
                    EnrichmentDecision(
                        proposal_id=proposal.id,
                        operation_id=operation,
                        entity_type="work" if diff.field in WORK_FIELDS else "edition",
                        entity_id=work.id if diff.field in WORK_FIELDS else edition.id,
                        field=diff.field,
                        decision="rejected",
                        decided_by=user_id,
                        decided_at=moment,
                    )
                )
                rejected.append(diff.field)
        proposal.status = "applied" if applied and not rejected else "partial" if applied else "rejected"
        proposal.decided_by = user_id
        proposal.decided_at = moment
        audit.record(
            session,
            "enrichment.apply",
            "edition",
            edition.id,
            user_id=user_id,
            details={
                "operation": operation,
                "proposition": proposal.id,
                "source": candidate.source,
                "champs": applied,
                "refuses": rejected,
            },
        )
        return ApplyResult(
            operation, tuple(applied), tuple(rejected), tuple(skipped), isbn_status, cover.status if cover else ""
        )


def _same_publisher_and_year(assessment: Assessment) -> bool:
    keys = {c.key: c for c in assessment.criteria}
    return all(key in keys and keys[key].agrees for key in ("publisher", "year"))


def _record_isbn_candidate(
    session: Session,
    edition_id: int,
    isbn13: str,
    status: str,
    proposal: EnrichmentProposal,
    assessment: Assessment,
    user_id: int | None,
    moment: str,
) -> None:
    row = session.scalar(
        select(EditionIsbnCandidate).where(
            EditionIsbnCandidate.edition_id == edition_id, EditionIsbnCandidate.isbn13 == isbn13
        )
    )
    if row is None:
        row = EditionIsbnCandidate(edition_id=edition_id, isbn13=isbn13, created_at=moment, status=status)
        session.add(row)
    elif row.status == "confirmed" and status != "confirmed":
        return  # un ISBN déjà confirmé ne redescend pas
    row.status = status
    row.source_code = proposal.source_code
    row.external_id = proposal.external_id
    row.external_url = proposal.external_url
    row.score = assessment.score
    row.agreeing = json.dumps(list(assessment.agreeing), ensure_ascii=False)
    row.conflicting = json.dumps(list(assessment.conflicting), ensure_ascii=False)
    row.proposal_id = proposal.id
    row.decided_by = user_id
    row.decided_at = moment


# --- Annulation -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RevertResult:
    restored: tuple[str, ...]
    conflicts: tuple[tuple[str, Any, Any, Any], ...]  # (champ, valeur d'alors, valeur actuelle, valeur rétablie)
    nothing_to_do: bool = False


def revert(
    database: Database, *, operation_id: str, user_id: int | None, force: frozenset[str] = frozenset()
) -> RevertResult:
    """Rétablit les anciennes valeurs d'une opération. Un champ modifié depuis n'est pas touché, sauf s'il est forcé."""
    with database.session(write=True) as session:
        decisions = session.scalars(
            select(EnrichmentDecision)
            .where(
                EnrichmentDecision.operation_id == operation_id,
                EnrichmentDecision.decision == "accepted",
                EnrichmentDecision.reverted_at.is_(None),
            )
            .order_by(EnrichmentDecision.id)
        ).all()
        if not decisions:
            return RevertResult((), (), nothing_to_do=True)
        proposal = session.get(EnrichmentProposal, decisions[0].proposal_id)
        search_row = session.get(EnrichmentSearch, proposal.search_id) if proposal else None
        if proposal is None or search_row is None or search_row.edition_id is None:
            raise BusinessError(_("Cette opération n’est rattachée à aucune édition."))
        target = repo.load_target(session, search_row.edition_id)
        if target is None:
            raise BusinessError(_("Cette édition n’existe plus."))
        work = session.get(Work, target.notice.id)
        edition = session.get(Edition, target.edition.id)
        assert work is not None and edition is not None

        restorable: dict[str, tuple[Any, EnrichmentDecision]] = {}
        conflicts: list[tuple[str, Any, Any, Any]] = []
        for decision in decisions:
            written = json.loads(decision.new_value) if decision.new_value else None
            old = json.loads(decision.old_value) if decision.old_value else None
            current = repo.current_value(target.values, decision.field)
            if repo.value_hash(current) == repo.value_hash(written) or decision.field in force:
                restorable[decision.field] = (old, decision)
            else:
                conflicts.append((decision.field, written, current, old))
        if not restorable:
            return RevertResult((), tuple(conflicts))

        moment = now_iso()
        # Œuvre
        if restorable.keys() & {"title", "subtitle", "contributors"}:
            title = restorable["title"][0] if "title" in restorable else target.notice.title
            subtitle = restorable["subtitle"][0] if "subtitle" in restorable else (target.notice.subtitle or "")
            people = [(c.name, c.role, str(c.agent_id)) for c in target.notice.contributors]
            if "contributors" in restorable:
                kept = {(n, r) for n, r in restorable["contributors"][0] or []}
                people = [p for p in people if (p[0], p[1]) in kept]
            catalogue.apply_identity(
                session,
                work,
                catalogue.IdentityInput(
                    title=title,
                    document_type_id=target.notice.document_type.id,
                    subtitle=subtitle,
                    original_title=target.notice.original_title or "",
                    section_id=target.notice.section.id if target.notice.section else None,
                    audience=target.notice.audience or "",
                    contributors=tuple(catalogue.ContributorInput(name=n, role=r, choice=c) for n, r, c in people),
                ),
                user_id,
            )
        if restorable.keys() & {"summary", "keywords", "genres"}:
            catalogue.apply_content(
                session,
                work,
                catalogue.ContentInput(
                    summary=restorable["summary"][0] if "summary" in restorable else (target.notice.summary or ""),
                    notes=target.notice.notes or "",
                    categories=", ".join(target.notice.categories),
                    genres=", ".join(restorable["genres"][0] if "genres" in restorable else target.notice.genres),
                    keywords=", ".join(
                        restorable["keywords"][0] if "keywords" in restorable else target.notice.keywords
                    ),
                ),
                user_id,
            )
        # Édition
        edition_fields = restorable.keys() & EDITION_FIELDS
        if edition_fields:
            values = {
                "publisher": target.values.publisher,
                "collection": target.values.collection,
                "collection_number": target.values.collection_number,
                "year": target.values.year,
                "statement": target.values.statement,
                "language": target.values.language,
                "pages": target.values.pages,
                "physical_note": target.values.physical_note,
            }
            for name in edition_fields - {"isbn"}:
                values[name] = str(restorable[name][0] or "")
            isbn_list = list(target.edition.isbn13)
            if "isbn" in restorable:
                isbn_list = list(restorable["isbn"][0] or [])
                for removed in set(target.edition.isbn13) - set(isbn_list):
                    candidate_row = session.scalar(
                        select(EditionIsbnCandidate).where(
                            EditionIsbnCandidate.edition_id == edition.id, EditionIsbnCandidate.isbn13 == removed
                        )
                    )
                    if candidate_row is not None:
                        candidate_row.status = "proposed"
                        candidate_row.decided_by = user_id
                        candidate_row.decided_at = moment
            catalogue.apply_edition_fields(
                session,
                edition,
                catalogue.EditionInput(
                    isbn=", ".join(isbn_list),
                    publisher=values["publisher"],
                    collection=values["collection"],
                    collection_number=values["collection_number"],
                    year=values["year"],
                    date=target.edition.date or "",
                    statement=values["statement"],
                    language=values["language"],
                    pages=values["pages"],
                    physical_note=values["physical_note"],
                    title_variant=target.edition.title_variant or "",
                    notes=target.edition.notes or "",
                    contributors=tuple(
                        catalogue.ContributorInput(name=c.name, role=c.role, choice=str(c.agent_id))
                        for c in target.edition.contributors
                    ),
                ),
                user_id,
            )
        if "cover" in restorable:
            previous = str(restorable["cover"][0] or "")
            media = session.scalar(select(MediaFile).where(MediaFile.sha256 == previous)) if previous else None
            catalogue.set_cover_media(
                session,
                edition,
                media.id if media else None,
                user_id,
                catalogue.CoverChangeInput("revert", reference=f"proposition {proposal.id}"),
            )
        for name, (_old, decision) in restorable.items():
            decision.reverted_at = moment
            decision.reverted_by = user_id
            entity_type = "work" if name in WORK_FIELDS else "edition"
            entity_id = work.id if entity_type == "work" else edition.id
            provenance = session.get(FieldProvenance, (entity_type, entity_id, name))
            if provenance is not None and provenance.decision_id == decision.id:
                session.delete(provenance)
        remaining = session.scalar(
            select(EnrichmentDecision.id).where(
                EnrichmentDecision.proposal_id == proposal.id,
                EnrichmentDecision.decision == "accepted",
                EnrichmentDecision.reverted_at.is_(None),
            )
        )
        if remaining is None:
            proposal.status = "reverted"
        audit.record(
            session,
            "enrichment.revert",
            "edition",
            edition.id,
            user_id=user_id,
            details={"operation": operation_id, "champs": sorted(restorable), "conflits": [c[0] for c in conflicts]},
        )
        return RevertResult(tuple(sorted(restorable)), tuple(conflicts))


def confirmed_isbns(session: Session, edition_id: int) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(EditionIdentifier.value).where(
                EditionIdentifier.edition_id == edition_id, EditionIdentifier.scheme == "isbn13"
            )
        )
    )


# --- ISBN proposés ----------------------------------------------------------------------------------------------------
def decide_isbn(database: Database, *, candidate_id: int, confirm: bool, user_id: int | None) -> str:
    """Confirme un ISBN proposé (vérifié sur le livre) ou le rejette. Rend l'état obtenu.

    Confirmer l'ajoute aux identifiants de l'édition par le service du catalogue : il entre alors dans la recherche
    et le contrôle des doublons. Rejeter le garde à l'écart pour qu'il ne soit pas reproposé.
    """
    with database.session(write=True) as session:
        row = session.get(EditionIsbnCandidate, candidate_id)
        if row is None or row.status != "proposed":
            raise BusinessError(_("Cet ISBN n’est plus en attente de décision."))
        moment = now_iso()
        row.decided_by = user_id
        row.decided_at = moment
        if not confirm:
            row.status = "rejected"
            audit.record(
                session, "enrichment.isbn_rejected", "edition", row.edition_id, user_id=user_id,
                details={"isbn": row.isbn13, "source": row.source_code},
            )  # fmt: skip
            return "rejected"
        target = repo.load_target(session, row.edition_id)
        edition = session.get(Edition, row.edition_id)
        if target is None or edition is None:
            raise BusinessError(_("Cette édition n’existe plus."))
        if row.isbn13 not in target.edition.isbn13:
            catalogue.apply_edition_fields(
                session,
                edition,
                catalogue.EditionInput(
                    isbn=", ".join([*target.edition.isbn13, row.isbn13]),
                    publisher=target.values.publisher,
                    collection=target.values.collection,
                    collection_number=target.values.collection_number,
                    year=target.values.year,
                    date=target.edition.date or "",
                    statement=target.values.statement,
                    language=target.values.language,
                    pages=target.values.pages,
                    physical_note=target.values.physical_note,
                    title_variant=target.edition.title_variant or "",
                    notes=target.edition.notes or "",
                    contributors=tuple(
                        catalogue.ContributorInput(name=c.name, role=c.role, choice=str(c.agent_id))
                        for c in target.edition.contributors
                    ),
                ),
                user_id,
            )
        row.status = "confirmed"
        audit.record(
            session, "enrichment.isbn_confirmed", "edition", row.edition_id, user_id=user_id,
            details={"isbn": row.isbn13, "source": row.source_code, "verifie_sur_le_livre": True},
        )  # fmt: skip
        return "confirmed"


def operation_edition(database: Database, operation_id: str) -> int | None:
    """L'édition visée par une opération d'enrichissement, pour vérifier qu'elle appartient bien à la fiche."""
    with database.session() as session:
        decision = session.scalar(
            select(EnrichmentDecision).where(EnrichmentDecision.operation_id == operation_id).limit(1)
        )
        if decision is None:
            return None
        proposal = session.get(EnrichmentProposal, decision.proposal_id)
        search_row = session.get(EnrichmentSearch, proposal.search_id) if proposal else None
        return search_row.edition_id if search_row else None
