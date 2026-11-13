# SPDX-License-Identifier: EUPL-1.2
"""Révision 0005 : les deux index du tableau de bord existent, servent, et se retirent sans perte."""

from __future__ import annotations

import sqlite3

from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences

INDEXES = {
    "ix_works_status_completeness": "CREATE INDEX ix_works_status_completeness ON works (status, completeness)",
    "ix_works_status_no_summary": "CREATE INDEX ix_works_status_no_summary ON works (status) WHERE summary IS NULL",
}
QUERIES = {
    "SELECT avg(completeness) FROM works WHERE status = 'active'": "ix_works_status_completeness",
    "SELECT count(*) FROM works WHERE status = 'active' AND completeness < 50": "ix_works_status_completeness",
    "SELECT count(*) FROM works WHERE status = 'active' AND summary IS NULL": "ix_works_status_no_summary",
}


def _connect(settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.database_path)


def _indexes(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'works'")
    return {name: sql for name, sql in rows if name in INDEXES}


def _fill(connection: sqlite3.Connection, count: int = 3000) -> None:
    connection.executemany(
        "INSERT INTO works (public_id, document_type_id, title, title_sort, title_norm, summary, status, completeness,"
        " version, created_at, updated_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?, 1, '2026-01-01', '2026-01-01')",
        [
            (f"w{i}", f"Titre {i}", f"titre {i}", f"titre {i}", None if i % 2 else "Résumé",
             "archived" if i % 50 == 0 else "active", i % 101)
            for i in range(count)
        ],
    )
    connection.commit()


def test_the_indexes_are_created_as_declared(settings):
    connection = _connect(settings)
    assert _indexes(connection) == INDEXES
    connection.close()
    assert schema_differences(settings) == []


def test_the_planner_uses_them_without_statistics(settings):
    """Une base en service n'a pas forcément de statistiques (ANALYZE) : les index doivent servir quand même."""
    connection = _connect(settings)
    _fill(connection)
    assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone() is None
    for query, index in QUERIES.items():
        plan = " | ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {query}"))
        assert index in plan, (query, plan)
    connection.close()


def test_downgrade_removes_only_the_indexes(settings):
    connection = _connect(settings)
    _fill(connection, 200)
    before = connection.execute("SELECT id, status, completeness, summary FROM works ORDER BY id").fetchall()
    connection.close()
    config = alembic_config(settings)
    command.downgrade(config, "0004")
    connection = _connect(settings)
    assert _indexes(connection) == {}
    assert connection.execute("SELECT id, status, completeness, summary FROM works ORDER BY id").fetchall() == before
    connection.close()
    command.upgrade(config, "head")
    connection = _connect(settings)
    assert _indexes(connection) == INDEXES
    connection.close()
