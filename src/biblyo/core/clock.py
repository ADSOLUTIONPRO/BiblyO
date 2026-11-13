# SPDX-License-Identifier: EUPL-1.2
"""Horloge unique de l'application : les tests la remplacent, le code ne lit jamais l'heure ailleurs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime


def _system_now() -> datetime:
    return datetime.now(UTC)


_now: Callable[[], datetime] = _system_now


def now() -> datetime:
    return _now()


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Remplace l'horloge (tests). `None` rétablit l'horloge réelle."""
    global _now
    _now = fn if fn is not None else _system_now


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return to_iso(now())


def from_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
