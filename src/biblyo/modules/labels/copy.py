# SPDX-License-Identifier: EUPL-1.2
"""Ce que le module des étiquettes ajoute à la fiche d'un exemplaire (« Générer pour ce livre ») : son code, lisible
à l'écran par une douchette ou une caméra, son étiquette, et les lots où elle a été préparée. Lecture seule."""

from __future__ import annotations

import json
from dataclasses import dataclass

from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core.barcodes import BarcodeError
from biblyo.core.i18n import _
from biblyo.core.models import AuditEntry
from biblyo.modules.catalogue.models import Copy
from biblyo.modules.labels.models import LabelBatch, LabelBatchItem
from biblyo.modules.labels.render import barcode_svg


@dataclass(frozen=True)
class PrintedIn:
    batch_id: int
    at: str
    status: str
    code: str


@dataclass(frozen=True)
class CopyLabels:
    copy_id: int
    version: int
    code: str | None
    svg: Markup | None
    batches: tuple[PrintedIn, ...]
    code_events: tuple[tuple[str, str], ...]  # date, ce qui s'est passé pour le code (journal)


def copy_labels(session: Session, copy_id: int) -> CopyLabels | None:
    copy = session.get(Copy, copy_id)
    if copy is None:
        return None
    try:
        svg = barcode_svg(copy.barcode) if copy.barcode else None
    except BarcodeError:  # un ancien code saisi à la main hors du jeu imprimable : montré sans dessin
        svg = None
    batches = session.execute(
        select(LabelBatch.id, LabelBatch.created_at, LabelBatch.status, LabelBatchItem.barcode)
        .join(LabelBatchItem, LabelBatchItem.batch_id == LabelBatch.id)
        .where(LabelBatchItem.copy_id == copy_id)
        .order_by(LabelBatch.id.desc())
        .limit(10)
    ).all()
    events = session.execute(
        select(AuditEntry.at, AuditEntry.details)
        .where(AuditEntry.entity_type == "copy", AuditEntry.entity_id == copy_id, AuditEntry.action == "copy.update")
        .order_by(AuditEntry.id.desc())
        .limit(20)
    ).all()
    code_events: list[tuple[str, str]] = []
    for at, details in events:
        try:
            data = json.loads(details or "{}")
        except ValueError:
            continue
        change = data.get("code_barres")
        if isinstance(change, list) and len(change) == 2:
            before, after = change
            text = (
                _("Code attribué : {code}").format(code=after)
                if not before
                else _("Code changé : {old} → {new} (l’ancien est retiré pour toujours)").format(old=before, new=after)
            )
            code_events.append((at, text))
    return CopyLabels(
        copy.id, copy.version, copy.barcode, svg, tuple(PrintedIn(*row) for row in batches), tuple(code_events[:5])
    )
