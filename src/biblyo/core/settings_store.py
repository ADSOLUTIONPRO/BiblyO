# SPDX-License-Identifier: EUPL-1.2
"""Réglages en base : valeurs JSON dans la table `settings`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from biblyo.core.clock import now_iso
from biblyo.core.models import Setting


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(Setting, key)
    return default if row is None else json.loads(row.value)


def set_setting(session: Session, key: str, value: Any, user_id: int | None = None) -> None:
    session.merge(
        Setting(key=key, value=json.dumps(value, ensure_ascii=False), updated_at=now_iso(), updated_by=user_id)
    )


def delete_setting(session: Session, key: str) -> None:
    row = session.get(Setting, key)
    if row is not None:
        session.delete(row)
