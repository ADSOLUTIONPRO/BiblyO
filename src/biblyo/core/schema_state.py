# SPDX-License-Identifier: EUPL-1.2
"""État du schéma, lu sans jamais créer ni modifier la base."""

from __future__ import annotations

import enum
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from biblyo.config import Settings
from biblyo.core.db import APPLICATION_ID, MINIMUM_SQLITE, readonly_uri
from biblyo.core.templating import PACKAGE_DIR

MIGRATIONS_DIR = PACKAGE_DIR / "migrations"


class SchemaStatus(enum.StrEnum):
    OK = "ok"
    ABSENT = "absent"
    BEHIND = "behind"
    AHEAD = "ahead"
    INCOMPATIBLE = "incompatible"
    FAILED = "failed"


MESSAGES = {
    SchemaStatus.OK: "La base de données est à jour.",
    SchemaStatus.ABSENT: "Base de données non initialisée.",
    SchemaStatus.BEHIND: "Mise à jour de la base requise.",
    SchemaStatus.AHEAD: "Version de l’application trop ancienne pour cette base.",
    SchemaStatus.INCOMPATIBLE: "Ce fichier n’est pas une base biblyO utilisable.",
    SchemaStatus.FAILED: "La dernière mise à jour de la base a échoué.",
}


@dataclass(frozen=True)
class SchemaState:
    status: SchemaStatus
    current: str | None
    head: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is SchemaStatus.OK

    @property
    def message(self) -> str:
        return MESSAGES[self.status]


@lru_cache(maxsize=4)
def _load_scripts(location: str) -> ScriptDirectory:
    return ScriptDirectory(location)


def script_directory() -> ScriptDirectory:
    """Révisions du code, lues une seule fois : l'état du schéma est consulté très souvent."""
    return _load_scripts(str(MIGRATIONS_DIR))


def head_revision() -> str:
    head = script_directory().get_current_head()
    if head is None:
        raise RuntimeError("Aucune révision de migration")
    return head


def read_failure(settings: Settings) -> dict[str, str] | None:
    path = settings.migration_failure_path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"error": "Marqueur d’échec illisible"}
    return data if isinstance(data, dict) else {"error": "Marqueur d’échec illisible"}


def read_revision(path: Path) -> tuple[str | None, int]:
    """Révision et identifiant d'application, en lecture seule. Lève sqlite3.Error si le fichier est invalide."""
    connection = sqlite3.connect(readonly_uri(path), uri=True)
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
        ).fetchone()
        if not exists:
            return None, application_id
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        return (row[0] if row else None), application_id
    finally:
        connection.close()


def read_state(settings: Settings, *, ignore_failure: bool = False) -> SchemaState:
    head = head_revision()
    path = settings.database_path
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE:
        return SchemaState(SchemaStatus.INCOMPATIBLE, None, head, f"SQLite {sqlite3.sqlite_version} trop ancien")

    failure = None if ignore_failure else read_failure(settings)
    if not path.exists() or path.stat().st_size == 0:
        if failure:
            return SchemaState(SchemaStatus.FAILED, None, head, failure.get("error", ""))
        return SchemaState(SchemaStatus.ABSENT, None, head)
    try:
        current, application_id = read_revision(path)
    except sqlite3.Error as exc:
        return SchemaState(SchemaStatus.INCOMPATIBLE, None, head, str(exc))
    if failure:
        return SchemaState(SchemaStatus.FAILED, current, head, failure.get("error", ""))
    if current is None or application_id != APPLICATION_ID:
        return SchemaState(SchemaStatus.INCOMPATIBLE, current, head, "Identifiant d’application inattendu")
    if current == head:
        return SchemaState(SchemaStatus.OK, current, head)
    try:
        script_directory().get_revision(current)
    except CommandError:
        return SchemaState(SchemaStatus.AHEAD, current, head)
    return SchemaState(SchemaStatus.BEHIND, current, head)


class SchemaMonitor:
    """État courant pour le serveur web, relu au plus toutes les `ttl` secondes.

    L'application sort ainsi du mode maintenance sans redémarrage après un `biblyo db upgrade`.
    """

    def __init__(self, settings: Settings, *, ttl: float = 2.0) -> None:
        self._settings = settings
        self._ttl = ttl
        self._lock = threading.Lock()
        self._state: SchemaState | None = None
        self._read_at = 0.0

    def current(self, *, refresh: bool = False) -> SchemaState:
        with self._lock:
            age = time.monotonic() - self._read_at
            if refresh or self._state is None or age >= self._ttl:
                self._state = read_state(self._settings)
                self._read_at = time.monotonic()
            return self._state
