# SPDX-License-Identifier: EUPL-1.2
"""Origine d'un exemplaire importé, montrée sur sa page (lot L6 ; I4, I5, I3).

« Ligne 356 du classeur Catalogue.xlsx, feuille Feuil1, importé le … » ; puis chaque colonne d'origine, en-tête
exact et valeur telle que lue, dans l'ordre du fichier ; puis les propositions de la ligne, avec leur statut. Aucune
colonne n'est cachée : une colonne sans champ dans biblyO est montrée comme les autres.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core.i18n import _
from biblyo.modules.imports.models import ImportBatch, ImportLink, ImportProposal, ImportRow
from biblyo.modules.imports.profile import FIELD_BY_CODE, KEEP, display

FIELD_LABELS = {
    "isbn": _("ISBN à vérifier"),
    "publication_year": _("Année à vérifier"),
    "contributor": _("Contributeur"),
    "role": _("Rôle"),
    "publisher": _("Éditeur"),
    "volume": _("Tome"),
    "name_form": _("Forme du nom"),
    "name_split": _("Séparation des noms"),
    "organization": _("Organisation"),
    "search_form": _("Forme de recherche"),
    "condition": _("État à vérifier"),
    "other": _("Autre valeur"),
}
STATUS_LABELS = {"proposed": _("à vérifier"), "confirmed": _("confirmée"), "rejected": _("rejetée")}


@dataclass(frozen=True)
class OriginCell:
    letter: str
    header: str
    value: str
    destination: str


@dataclass(frozen=True)
class OriginProposal:
    field: str
    value: str
    status: str
    source_url: str
    note: str


@dataclass
class CopyOrigin:
    batch_id: int
    filename: str
    sheet: str
    row_number: int
    source_id: str
    applied_at: str
    complement: str
    cells: list[OriginCell] = field(default_factory=list)
    proposals: list[OriginProposal] = field(default_factory=list)


def copy_origin(session: Session, copy_id: int) -> CopyOrigin | None:
    found = session.execute(
        select(ImportRow, ImportBatch)
        .join(ImportLink, ImportLink.row_id == ImportRow.id)
        .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
        .where(ImportLink.entity_type == "copy", ImportLink.entity_id == copy_id, ImportBatch.status == "applied")
    ).first()
    if found is None:
        return None
    row, batch = found
    summary = json.loads(batch.summary or "{}")
    raw = json.loads(row.raw)
    stored = json.loads(row.normalized or "{}")
    fields: dict[str, str] = summary.get("fields", {})
    origin = CopyOrigin(
        batch.id,
        batch.filename,
        row.sheet,
        row.row_number,
        str(stored.get("source_id", "")),
        batch.applied_at or "",
        batch.complement_filename or "",
    )
    for _index, letter, header, label in summary.get("columns", []):
        spec = FIELD_BY_CODE.get(fields.get(label, KEEP), FIELD_BY_CODE[KEEP])
        origin.cells.append(OriginCell(letter, header or label, display(raw.get(label)), spec.label))
    for item in session.scalars(
        select(ImportProposal).where(ImportProposal.row_id == row.id).order_by(ImportProposal.id)
    ):
        origin.proposals.append(
            OriginProposal(
                FIELD_LABELS.get(item.field, item.field),
                item.value,
                STATUS_LABELS.get(item.status, item.status),
                item.source_url or "",
                item.note or "",
            )
        )
    return origin
