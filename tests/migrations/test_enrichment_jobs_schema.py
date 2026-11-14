# SPDX-License-Identifier: EUPL-1.2
"""Révision 0013 (L7, T4) : deux tables nouvelles, rien de modifié ; une édition une seule fois par tâche ; descente
refusée dès qu'une tâche existe."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences, verify_schema

COUNTED = ("works", "editions", "copies", "loans", "patrons", "enrichment_searches", "import_batches", "audit_log")


def _connect(settings) -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _all(settings, query: str):
    connection = _connect(settings)
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


def _execute(settings, *statements: str) -> None:
    connection = _connect(settings)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def at_0012(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0012")
    _execute(
        bare_settings,
        "INSERT INTO works (id, public_id, document_type_id, title, title_sort, title_norm, status, completeness,"
        " version, created_at, updated_at) VALUES (1, 'w1', (SELECT id FROM document_types WHERE code = 'bd'),"
        " 'Cerveau-choc', 'cerveau-choc', 'cerveau-choc', 'active', 40, 1, '2026-11-14', '2026-11-14')",
        "INSERT INTO editions (id, public_id, work_id, is_primary, language, status, origin, version, created_at,"
        " updated_at) VALUES (1, 'e1', 1, 1, 'fre', 'active', 'manual', 1, '2026-11-14', '2026-11-14')",
    )
    return bare_settings


def _counts(settings) -> dict[str, int]:
    return {table: _all(settings, f"SELECT COUNT(*) FROM {table}")[0][0] for table in COUNTED}  # noqa: S608


def test_upgrade_adds_two_tables_and_changes_nothing_else(at_0012):
    before_rows = _counts(at_0012)
    before_objects = set(_all(at_0012, "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    command.upgrade(alembic_config(at_0012), "0013")
    assert schema_differences(at_0012) == [] and verify_schema(at_0012) == []
    assert _counts(at_0012) == before_rows
    after_objects = set(_all(at_0012, "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    assert before_objects - after_objects == {o for o in before_objects if o[1] == "alembic_version"} - after_objects
    added = {o[1] for o in after_objects - before_objects if o[0] == "table"}
    assert added == {"enrichment_jobs", "enrichment_job_items"}


def test_an_edition_appears_once_per_job(at_0012):
    command.upgrade(alembic_config(at_0012), "0013")
    _execute(
        at_0012,
        "INSERT INTO enrichment_jobs (id, public_id, status, scope, created_at, updated_at)"
        " VALUES (1, 'j1', 'running', '{}', '2026-11-14', '2026-11-14')",
        "INSERT INTO enrichment_job_items (job_id, edition_id, position, updated_at) VALUES (1, 1, 1, '2026-11-14')",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _execute(
            at_0012,
            "INSERT INTO enrichment_job_items (job_id, edition_id, position, updated_at)"
            " VALUES (1, 1, 2, '2026-11-14')",
        )


def test_downgrade_is_refused_once_a_job_exists(at_0012):
    command.upgrade(alembic_config(at_0012), "0013")
    _execute(
        at_0012,
        "INSERT INTO enrichment_jobs (id, public_id, status, scope, created_at, updated_at)"
        " VALUES (1, 'j1', 'finished', '{}', '2026-11-14', '2026-11-14')",
    )
    with pytest.raises(RuntimeError, match="Descente refusée"):
        command.downgrade(alembic_config(at_0012), "0012")
    assert _all(at_0012, "SELECT version_num FROM alembic_version") == [("0013",)]


def test_downgrade_without_job_removes_the_two_tables(at_0012):
    command.upgrade(alembic_config(at_0012), "0013")
    command.downgrade(alembic_config(at_0012), "0012")
    tables = {name for (name,) in _all(at_0012, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not tables & {"enrichment_jobs", "enrichment_job_items"}
    assert _all(at_0012, "SELECT title FROM works") == [("Cerveau-choc",)]
