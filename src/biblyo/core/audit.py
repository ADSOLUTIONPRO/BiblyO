# SPDX-License-Identifier: EUPL-1.2
"""Journal d'audit, écrit dans la transaction de l'action qu'il décrit."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from biblyo.core.clock import now_iso
from biblyo.core.models import AuditEntry

#: Clés refusées dans le détail d'une entrée : une erreur de développement ne doit rien faire fuiter.
FORBIDDEN_KEYS = frozenset({"password", "mot_de_passe", "token", "jeton", "password_hash", "csrf", "secret"})
#: Données personnelles d'un lecteur : le journal dit quels champs ont changé, jamais leurs valeurs (L3, D10).
PERSONAL_KEYS = frozenset(
    {
        "email", "courriel", "phone", "telephone", "téléphone", "address", "adresse", "birth", "naissance",
        "first_name", "last_name", "prenom", "prénom", "nom", "name", "notes", "observations", "card", "carte",
        "number_card", "numero_carte", "city", "commune", "line1", "postal_code",
    }
)  # fmt: skip


def record(
    session: Session,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    *,
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if details and (FORBIDDEN_KEYS | PERSONAL_KEYS) & {key.casefold() for key in details}:
        raise ValueError("Donnée sensible ou personnelle refusée dans le journal")
    session.add(
        AuditEntry(
            at=now_iso(),
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details, ensure_ascii=False) if details else None,
        )
    )
