# SPDX-License-Identifier: EUPL-1.2
"""Créer une notice à partir d'un résultat (parcours PA-2, D19).

Rien n'est créé ici : on prépare le formulaire de création du catalogue, que la personne relit et corrige, puis on
enregistre la provenance des champs qu'elle a gardés tels quels. Un résultat ambigu exige que l'édition ait été
confirmée sur le livre ; l'ISBN n'est prérempli que pour une correspondance exacte ou un ISBN vérifié sur le livre.
La série et le tome ne sont jamais préremplis : les saisir rattache l'œuvre à une série et lui donne un numéro de cote.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.i18n import _
from biblyo.core.ids import new_public_id
from biblyo.core.text import normalize
from biblyo.modules.catalogue import repository as catalogue_repo
from biblyo.modules.catalogue.models import Edition, EditionIdentifier, Work
from biblyo.modules.catalogue.rules import LANGUAGES
from biblyo.modules.enrichment import repository as repo
from biblyo.modules.enrichment.connectors.base import Candidate
from biblyo.modules.enrichment.diffing import propose_role
from biblyo.modules.enrichment.models import (
    EditionIsbnCandidate,
    EnrichmentDecision,
    EnrichmentProposal,
    EnrichmentSearch,
    FieldProvenance,
)
from biblyo.modules.enrichment.service import LICENCES, candidate_of

AMBIGUOUS = frozenset({"uncertain", "multiple"})
ISBN_HINT = _(
    "ISBN proposé par la source : {isbn}. Il n’est pas prérempli : saisissez-le après l’avoir vérifié sur le livre."
)
SERIES_HINT = _(
    "La source indique la série « {series} »{volume}. Elle n’est pas préremplie : la saisir rattache le livre à "
    "une série et lui attribue un numéro de cote."
)
SOURCE_LABELS = {"bnf": _("BnF"), "sudoc": _("Sudoc")}


@dataclass(frozen=True)
class Duplicate:
    work_id: int
    title: str
    reason: str


@dataclass(frozen=True)
class Plan:
    proposal_id: int
    candidate: Candidate
    match_class: str
    needs_confirmation: bool  # résultat ambigu : l'édition doit être confirmée sur le livre
    isbn_in_form: bool
    values: dict[str, str] = field(default_factory=dict)
    contributors: tuple[tuple[str, str, str], ...] = ()  # (nom, rôle, dates)
    hints: tuple[str, ...] = ()
    duplicates: tuple[Duplicate, ...] = ()

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.candidate.source, self.candidate.source)


def _proposal(session: Session, proposal_id: int) -> EnrichmentProposal | None:
    proposal = session.get(EnrichmentProposal, proposal_id)
    if proposal is None or proposal.status != "pending":
        return None
    return proposal


def duplicates(session: Session, candidate: Candidate) -> tuple[Duplicate, ...]:
    """Notices locales qui pourraient déjà décrire ce livre : même ISBN confirmé, ou même titre."""
    found: dict[int, Duplicate] = {}
    if candidate.edition.isbn13:
        rows = session.execute(
            select(Work.id, Work.title)
            .join(Edition, Edition.work_id == Work.id)
            .join(EditionIdentifier, EditionIdentifier.edition_id == Edition.id)
            .where(EditionIdentifier.scheme == "isbn13", EditionIdentifier.value.in_(candidate.edition.isbn13))
        ).all()
        for work_id, title in rows:
            found[work_id] = Duplicate(work_id, title, _("même ISBN"))
    key = normalize(candidate.work.title.value)
    if key:
        for work_id, title in session.execute(select(Work.id, Work.title).where(Work.title_norm == key)).all():
            found.setdefault(work_id, Duplicate(work_id, title, _("même titre")))
    return tuple(found.values())


def plan(
    database: Database, proposal_id: int, *, edition_verified: bool = False, isbn_verified: bool = False
) -> Plan | None:
    with database.session() as session:
        proposal = _proposal(session, proposal_id)
        if proposal is None or proposal.match_class in ("excluded", "none"):
            return None
        candidate = candidate_of(proposal)
        found = duplicates(session, candidate)
        match_class = proposal.match_class
    if candidate.record_kind == "set" or candidate.medium != "text":
        return None
    needs_confirmation = match_class in AMBIGUOUS and not edition_verified
    isbn = candidate.edition.isbn13[0] if candidate.edition.isbn13 else ""
    isbn_in_form = bool(isbn) and (match_class == "exact" or isbn_verified)
    edition, work = candidate.edition, candidate.work
    values: dict[str, str] = {
        "titre": work.title.value,
        "sous_titre": work.subtitle.value if work.subtitle else "",
        "isbn": isbn if isbn_in_form else "",
        "editeur": edition.publisher.value if edition.publisher else "",
        "collection": edition.collection.value if edition.collection else "",
        "numero_collection": edition.collection_number.value if edition.collection_number else "",
        "annee": str(edition.year.whole) if edition.year else "",
        "mention_edition": edition.statement.value if edition.statement else "",
        "langue": edition.language.value if edition.language and edition.language.value in LANGUAGES else "fre",
        "pages": str(edition.pages.whole) if edition.pages else "",
        "description_physique": edition.physical.value if edition.physical else "",
        "resume": work.summary.text if work.summary and work.summary.reuse == "established" else "",
    }
    people = tuple(
        (person.name, propose_role(person)[0], person.dates)
        for person in work.contributors
        if not (person.mention == "original" and person.function_code == "100")
    )
    hints: list[str] = []
    if isbn and not isbn_in_form:
        hints.append(ISBN_HINT.format(isbn=isbn))
    if work.series or work.volume:
        series = work.series.value if work.series else ""
        volume = work.volume.raw if work.volume else ""
        hints.append(SERIES_HINT.format(series=series, volume=_(", tome {n}").format(n=volume) if volume else ""))
    if work.summary and work.summary.reuse != "established":
        hints.append(
            _("Résumé fourni par {provider} : non repris, ses conditions de réutilisation sont à vérifier.").format(
                provider=work.summary.provider
            )
        )
    if any(propose_role(person)[2] for person in work.contributors):
        hints.append(_("Les rôles des contributeurs sont proposés d’après la notice : vérifiez-les."))
    hints.append(_("Le type de document et la section ne viennent pas de la source : choisissez-les."))
    return Plan(
        proposal_id, candidate, match_class, needs_confirmation, isbn_in_form, values, people, tuple(hints), found
    )


# --- Après la création ------------------------------------------------------------------------------------------------
FIELD_FORMS = {
    "title": ("work", "titre"),
    "subtitle": ("work", "sous_titre"),
    "summary": ("work", "resume"),
    "publisher": ("edition", "editeur"),
    "collection": ("edition", "collection"),
    "collection_number": ("edition", "numero_collection"),
    "year": ("edition", "annee"),
    "statement": ("edition", "mention_edition"),
    "language": ("edition", "langue"),
    "pages": ("edition", "pages"),
    "physical_note": ("edition", "description_physique"),
}


def record_creation(
    database: Database, *, proposal_id: int, work_id: int, isbn_verified: bool, user_id: int | None
) -> list[str]:
    """La notice vient d'être créée par le formulaire : provenance des champs gardés tels que proposés.

    Un champ corrigé par la personne n'a pas de provenance : c'est sa saisie. L'ISBN de la source n'est confirmé que
    s'il figure dans l'édition créée, pour une correspondance exacte ou un ISBN vérifié sur le livre.
    """
    created = plan(database, proposal_id, edition_verified=True, isbn_verified=isbn_verified)
    if created is None:
        return []
    with database.session(write=True) as session:
        proposal = _proposal(session, proposal_id)
        notice = catalogue_repo.load_notice(session, work_id)
        if proposal is None or notice is None:
            return []
        target = repo.load_target(session, notice.primary.id)
        if target is None:
            return []
        current = {
            "titre": notice.title,
            "sous_titre": notice.subtitle or "",
            "resume": notice.summary or "",
            "editeur": target.values.publisher,
            "collection": target.values.collection,
            "numero_collection": target.values.collection_number,
            "annee": target.values.year,
            "mention_edition": target.values.statement,
            "langue": target.values.language,
            "pages": target.values.pages,
            "description_physique": target.values.physical_note,
        }
        moment = now_iso()
        operation = new_public_id()
        kept: list[str] = []
        candidate = created.candidate
        for name, (level, form_name) in FIELD_FORMS.items():
            proposed = created.values.get(form_name, "")
            if not proposed or normalize(current[form_name]) != normalize(proposed):
                continue
            entity_id = work_id if level == "work" else notice.primary.id
            _remember(
                session, proposal, candidate, level, entity_id, name, current[form_name], operation, moment, user_id
            )
            kept.append(name)
        isbn = candidate.edition.isbn13[0] if candidate.edition.isbn13 else ""
        if isbn and isbn in notice.primary.isbn13 and (created.match_class == "exact" or isbn_verified):
            _remember(
                session, proposal, candidate, "edition", notice.primary.id, "isbn", [isbn], operation, moment, user_id
            )
            session.add(
                EditionIsbnCandidate(
                    edition_id=notice.primary.id,
                    isbn13=isbn,
                    status="confirmed",
                    source_code=candidate.source,
                    external_id=candidate.external_id,
                    external_url=candidate.url,
                    score=_score(proposal),
                    agreeing="[]",
                    conflicting="[]",
                    proposal_id=proposal.id,
                    created_at=moment,
                    decided_by=user_id,
                    decided_at=moment,
                )
            )
            kept.append("isbn")
        elif isbn and isbn not in notice.primary.isbn13:
            session.add(
                EditionIsbnCandidate(
                    edition_id=notice.primary.id,
                    isbn13=isbn,
                    status="proposed",
                    source_code=candidate.source,
                    external_id=candidate.external_id,
                    external_url=candidate.url,
                    score=_score(proposal),
                    agreeing="[]",
                    conflicting="[]",
                    proposal_id=proposal.id,
                    created_at=moment,
                )
            )
        proposal.status = "applied" if kept else "rejected"
        proposal.decided_by = user_id
        proposal.decided_at = moment
        search_row = session.get(EnrichmentSearch, proposal.search_id)
        if search_row is not None and search_row.edition_id is None:
            search_row.edition_id = notice.primary.id  # la recherche libre rejoint l'historique de l'édition créée
        audit.record(
            session,
            "enrichment.create_from_source",
            "work",
            work_id,
            user_id=user_id,
            details={"proposition": proposal.id, "source": candidate.source, "champs": kept},
        )
        return kept


def _score(proposal: EnrichmentProposal) -> int:
    return int(proposal.score)


def _remember(
    session: Session,
    proposal: EnrichmentProposal,
    candidate: Candidate,
    level: str,
    entity_id: int,
    name: str,
    value: Any,
    operation: str,
    moment: str,
    user_id: int | None,
) -> None:
    decision = EnrichmentDecision(
        proposal_id=proposal.id,
        operation_id=operation,
        entity_type=level,
        entity_id=entity_id,
        field=name,
        decision="accepted",
        action="fill",
        old_value=json.dumps("" if name != "isbn" else [], ensure_ascii=False),
        new_value=json.dumps(value, ensure_ascii=False),
        decided_by=user_id,
        decided_at=moment,
    )
    session.add(decision)
    session.flush()
    session.add(
        FieldProvenance(
            entity_type=level,
            entity_id=entity_id,
            field=name,
            source_code=candidate.source,
            external_id=candidate.external_id,
            external_url=candidate.url,
            sub_source=None,
            reuse_status="established",
            license_note=LICENCES.get(candidate.source, candidate.source),
            source_date=candidate.source_date or None,
            retrieved_at=proposal.retrieved_at,
            value_sha256=repo.value_hash(value),
            decision_id=decision.id,
        )
    )


# --- Points d'extension pour le catalogue -----------------------------------------------------------------------------
def notice_prefill(
    database: Database, proposal_id: int, edition_verified: bool, isbn_verified: bool
) -> dict[str, Any] | None:
    """Ce que le formulaire « Ajouter un livre » reprend d'un résultat, ou l'adresse de confirmation s'il est ambigu."""
    planned = plan(database, proposal_id, edition_verified=edition_verified, isbn_verified=isbn_verified)
    if planned is None:
        return None
    if planned.needs_confirmation:
        return {"confirm": f"/sources/propositions/{proposal_id}/creer"}
    return {
        "values": planned.values,
        "contributors": planned.contributors,
        "banner": {
            "source": planned.source_label,
            "external_id": planned.candidate.external_id,
            "url": planned.candidate.url,
            "hints": planned.hints,
            "duplicates": planned.duplicates,
            "isbn_in_form": planned.isbn_in_form,
        },
    }


def notice_created(
    database: Database, proposal_id: int, work_id: int, isbn_verified: bool, user_id: int | None
) -> None:
    record_creation(database, proposal_id=proposal_id, work_id=work_id, isbn_verified=isbn_verified, user_id=user_id)
