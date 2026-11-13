# SPDX-License-Identifier: EUPL-1.2
"""Accès SQLite : PRAGMA à chaque connexion et unité de travail.

Le pilote sqlite3 gère mal les transactions par défaut ; on reprend la main (recette de la documentation
SQLAlchemy) pour que `BEGIN IMMEDIATE` sérialise les décisions « vérifier puis écrire ».
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import URL, Connection, Engine, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

#: Empreinte d'une base biblyO, écrite par la première révision (`PRAGMA application_id`, « bibl »).
APPLICATION_ID = int.from_bytes(b"bibl", "big")
MINIMUM_SQLITE = (3, 37, 0)  # tables STRICT
_WRITE_OPTION = "biblyo_write"
WAL_ATTEMPTS = 100
WAL_PAUSE = 0.05  # soit 5 s au plus, comme `busy_timeout`


def readonly_uri(path: Path) -> str:
    """URI SQLite en lecture seule. Le chemin est échappé : un « # » ferait perdre `mode=ro`, et créerait un fichier."""
    return f"file:{quote(str(path))}?mode=ro"


def enable_wal(cursor: Any, *, attempts: int = WAL_ATTEMPTS, pause: float = WAL_PAUSE) -> None:
    """Passe la base en WAL. Une base encore en journal classique (sauvegarde restaurée, copie) doit changer de mode :
    SQLite répond alors « verrouillée » sans attendre si d'autres connexions arrivent en même temps. On réessaie."""
    for attempt in range(attempts):
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or attempt == attempts - 1:
                raise
            time.sleep(pause)


def make_engine(path: Path, *, foreign_keys: bool = True, create: bool = False) -> Engine:
    """Moteur sans pool : aucune poignée de fichier ne reste ouverte entre deux requêtes.

    Sans `create`, une base absente provoque une erreur : seule la migration peut créer le fichier.
    """
    url = URL.create(
        "sqlite+pysqlite",
        database=f"file:{quote(str(path))}",
        query={"mode": "rwc" if create else "rw", "uri": "true"},
    )
    # `hide_parameters` : une erreur SQL ne recopie pas dans les traces ce que la requête cherchait ou écrivait.
    engine = create_engine(url, poolclass=NullPool, connect_args={"check_same_thread": False}, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cursor.execute("PRAGMA busy_timeout=5000")
        enable_wal(cursor)
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(connection: Connection) -> None:
        write = connection.get_execution_options().get(_WRITE_OPTION, False)
        connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")

    return engine


class Database:
    """La base de l'installation. Le moteur n'est créé qu'au premier usage : une base absente le reste."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._engine: Engine | None = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = make_engine(self.path)
        return self._engine

    @contextmanager
    def session(self, *, write: bool = False) -> Iterator[Session]:
        """Unité de travail : tout est validé à la sortie, tout est annulé sur une exception."""
        with self.engine.connect() as connection:
            connection = connection.execution_options(**{_WRITE_OPTION: write})
            with Session(bind=connection, expire_on_commit=False) as session, session.begin():
                session.connection()  # la session est paresseuse : le verrou se prend ici, pas à la première requête
                yield session

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
