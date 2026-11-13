# SPDX-License-Identifier: EUPL-1.2
"""Onglet « Enrichissement » de la fiche : provenance, crédits, ISBN proposés, historique et annulations.

Fourni au catalogue par `Module.extras["enrichment_tab"]` : le catalogue n'importe rien de ce module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core.i18n import _
from biblyo.core.models import User
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.enrichment import repository as repo

FIELD_LABELS: dict[str, str] = {
    "title": _("Titre"),
    "subtitle": _("Sous-titre"),
    "contributors": _("Contributeurs"),
    "summary": _("Résumé"),
    "keywords": _("Mots-clés"),
    "genres": _("Genres"),
    "isbn": _("ISBN"),
    "publisher": _("Éditeur"),
    "collection": _("Collection"),
    "collection_number": _("Numéro dans la collection"),
    "year": _("Année"),
    "statement": _("Mention d’édition"),
    "language": _("Langue"),
    "pages": _("Pages"),
    "physical_note": _("Description matérielle"),
    "cover": _("Couverture"),
}
SOURCE_LABELS = {"bnf": _("BnF"), "sudoc": _("Sudoc")}
OUTCOME_LABELS = {
    "exact": _("Correspondance exacte"),
    "strong": _("Correspondance forte"),
    "uncertain": _("Correspondance incertaine"),
    "multiple": _("Plusieurs candidats"),
    "none": _("Aucun résultat"),
    "incomplete": _("Recherche incomplète"),
}
STATUS_LABELS = {
    "pending": _("en attente"),
    "applied": _("appliquée"),
    "partial": _("appliquée en partie"),
    "rejected": _("refusée"),
    "stale": _("périmée"),
    "reverted": _("annulée"),
}


@dataclass(frozen=True)
class Operation:
    """Une confirmation : ses champs acceptés, refusés, et ce qui en a été annulé."""

    id: str
    at: str
    by: str
    source: str
    external_id: str
    external_url: str
    accepted: tuple[str, ...]
    rejected: tuple[str, ...]
    reverted: tuple[str, ...]

    @property
    def revertible(self) -> bool:
        return any(name not in self.reverted for name in self.accepted)


def _names(session: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return dict(session.execute(select(User.id, User.display_name).where(User.id.in_(ids))).all())


def tab_context(session: Session, auth: AuthContext, work_id: int, edition_id: int) -> dict[str, Any]:
    target = repo.load_target(session, edition_id)
    if target is None or target.notice.id != work_id:
        return {"enrichment": None}
    provenance = repo.provenance(session, target.values, work_id, edition_id)
    searches = repo.history(session, edition_id)
    candidates = repo.isbn_candidates(session, edition_id)
    people = {s.created_by for s in searches if s.created_by}
    operations: dict[str, dict[str, Any]] = {}
    for search in searches:
        for proposal in search.proposals:
            for decision in proposal.decisions:
                if decision.decided_by:
                    people.add(decision.decided_by)
                entry = operations.setdefault(
                    decision.operation_id,
                    {
                        "at": decision.decided_at,
                        "by": decision.decided_by,
                        "source": proposal.source_code,
                        "external_id": proposal.external_id,
                        "external_url": proposal.external_url,
                        "accepted": [],
                        "rejected": [],
                        "reverted": [],
                    },
                )
                key = "accepted" if decision.decision == "accepted" else "rejected"
                entry[key].append(decision.field)
                if decision.reverted_at:
                    entry["reverted"].append(decision.field)
    names = _names(session, {p for p in people if p})
    can_see_who = auth.can("enrichment.search")
    ops = sorted(
        (
            Operation(
                op_id,
                data["at"],
                names.get(data["by"], "") if can_see_who else "",
                data["source"],
                data["external_id"],
                data["external_url"],
                tuple(data["accepted"]),
                tuple(data["rejected"]),
                tuple(data["reverted"]),
            )
            for op_id, data in operations.items()
        ),
        key=lambda op: op.at,
        reverse=True,
    )
    return {
        "enrichment": {
            "target": target,
            "provenance": provenance,
            "searches": searches,
            "operations": ops,
            "isbn_candidates": [
                {
                    "row": row,
                    "agreeing": json.loads(row.agreeing or "[]"),
                    "conflicting": json.loads(row.conflicting or "[]"),
                }
                for row in candidates
            ],
            "names": names if can_see_who else {},
            "field_labels": FIELD_LABELS,
            "source_labels": SOURCE_LABELS,
            "outcome_labels": OUTCOME_LABELS,
            "status_labels": STATUS_LABELS,
            "cover": target.edition.cover,
        }
    }
