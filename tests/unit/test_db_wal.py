# SPDX-License-Identifier: EUPL-1.2
"""Passage en WAL d'une base restaurée : plusieurs connexions simultanées ne doivent pas échouer."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from biblyo.core.db import enable_wal, make_engine


class _Cursor:
    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        self.calls = 0

    def execute(self, _statement: str) -> None:
        self.calls += 1
        if self.failures:
            raise sqlite3.OperationalError(self.failures.pop(0))


def test_a_locked_database_is_retried():
    cursor = _Cursor(["database is locked", "database is locked"])
    enable_wal(cursor, pause=0)
    assert cursor.calls == 3


def test_it_gives_up_after_the_last_attempt():
    cursor = _Cursor(["database is locked"] * 3)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        enable_wal(cursor, attempts=3, pause=0)
    assert cursor.calls == 3


def test_another_error_is_not_retried():
    cursor = _Cursor(["disk I/O error", "database is locked"])
    with pytest.raises(sqlite3.OperationalError, match="disk"):
        enable_wal(cursor, pause=0)
    assert cursor.calls == 1


def _connect(path: Path, barrier: threading.Barrier, errors: list[Exception]) -> None:
    engine = make_engine(path)
    try:
        barrier.wait()
        with engine.connect():
            pass
    except Exception as error:
        errors.append(error)
    finally:
        engine.dispose()


def test_simultaneous_first_connections_to_a_rollback_journal_database(tmp_path: Path):
    for cycle in range(25):
        path = tmp_path / f"base-{cycle}.sqlite"
        creation = sqlite3.connect(path)
        creation.execute("CREATE TABLE essai (id INTEGER PRIMARY KEY) STRICT")
        creation.commit()
        creation.close()
        errors: list[Exception] = []
        barrier = threading.Barrier(6)
        threads = [threading.Thread(target=_connect, args=(path, barrier, errors)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == [], cycle
