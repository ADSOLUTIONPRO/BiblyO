# SPDX-License-Identifier: EUPL-1.2
"""Disponibilité de circulation, lue en lot : la seule source des pastilles « Disponible », « Emprunté », « En retard »,
« Indisponible » du catalogue, des fiches, des séries, des étagères et du comptoir.

Tout se déduit du statut physique de l'exemplaire et de son prêt en cours. La table des prêts relève du module des
prêts : elle est lue ici, jamais écrite. Aucun emprunteur n'en sort (D11 bis du plan de L4).

Un affichage « Disponible » ne garantit rien : l'ajout au panier et la validation relisent la base.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from biblyo.core.clock import now
from biblyo.core.i18n import TIMEZONE
from biblyo.modules.catalogue.rules import COPY_STATUSES
from biblyo.modules.holdings.rules import CopyState, WorkCounts, copy_state, count_states


def today() -> date:
    """La date à la bibliothèque, comme au comptoir : une échéance est un jour calendaire."""
    return now().astimezone(TIMEZONE).date()


_COPIES = text(
    "SELECT c.id, c.status, c.is_loanable, l.due_on FROM copies c"
    " LEFT JOIN loans l ON l.copy_id = c.id AND l.returned_at IS NULL AND l.cancelled_at IS NULL"
    " WHERE c.id IN :ids"
).bindparams(bindparam("ids", expanding=True))

_WORKS = text(
    "SELECT e.work_id, c.status, c.is_loanable, l.due_on FROM copies c"
    " JOIN editions e ON e.id = c.edition_id"
    " LEFT JOIN loans l ON l.copy_id = c.id AND l.returned_at IS NULL AND l.cancelled_at IS NULL"
    " WHERE e.work_id IN :ids"
).bindparams(bindparam("ids", expanding=True))

#: Au-delà, la requête est découpée : SQLite limite le nombre de paramètres d'une instruction.
_CHUNK = 900


def _state(status: str, is_loanable: object, due_on: str | None, day: date) -> CopyState:
    return copy_state(status, bool(is_loanable), due_on, day, COPY_STATUSES.get(status, status))


def for_copies(session: Session, copy_ids: Sequence[int], day: date | None = None) -> dict[int, CopyState]:
    """L'état de chaque exemplaire demandé, en une requête par tranche de 900."""
    day = day or today()
    ids = list(dict.fromkeys(copy_ids))
    found: dict[int, CopyState] = {}
    for start in range(0, len(ids), _CHUNK):
        rows = session.execute(_COPIES, {"ids": ids[start : start + _CHUNK]})
        for copy_id, status, loanable, due_on in rows:
            found[copy_id] = _state(status, loanable, due_on, day)
    return found


def for_works(session: Session, work_ids: Sequence[int], day: date | None = None) -> dict[int, WorkCounts]:
    """Les compteurs de chaque œuvre demandée ; une œuvre sans exemplaire a des compteurs nuls."""
    day = day or today()
    ids = list(dict.fromkeys(work_ids))
    states: dict[int, list[CopyState]] = defaultdict(list)
    for start in range(0, len(ids), _CHUNK):
        rows = session.execute(_WORKS, {"ids": ids[start : start + _CHUNK]})
        for work_id, status, loanable, due_on in rows:
            states[work_id].append(_state(status, loanable, due_on, day))
    return {work_id: count_states(states.get(work_id, [])) for work_id in ids}


def for_work(session: Session, work_id: int, day: date | None = None) -> WorkCounts:
    return for_works(session, [work_id], day)[work_id]
