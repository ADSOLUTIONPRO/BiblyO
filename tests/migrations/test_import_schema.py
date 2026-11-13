# SPDX-License-Identifier: EUPL-1.2
"""Révision 0011 (L6, T1) : tables de l'import, un fichier appliqué une seule fois, retour arrière refusé après une
trace."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences, verify_schema

TABLES = ("import_profiles", "import_batches", "import_rows", "import_links", "import_proposals")
BATCH = (
    "INSERT INTO import_batches"
    " (target, filename, file_sha256, file_bytes, mapping, schema_version, status, created_at)"
    " VALUES ('catalogue', 'catalogue.xlsx', '{sha}', 10, '{{}}', '0011', '{status}', 'x')"
)


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
def at_0010(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0010")
    return bare_settings


def test_upgrade_creates_strict_tables_and_touches_nothing_else(at_0010):
    before = _all(at_0010, "SELECT name, sql FROM sqlite_master WHERE tbl_name NOT LIKE 'import%' ORDER BY name")
    command.upgrade(alembic_config(at_0010), "0011")
    strict = _all(at_0010, "SELECT name, strict FROM pragma_table_list WHERE name LIKE 'import%' ORDER BY name")
    assert sorted(strict) == sorted((table, 1) for table in TABLES)
    after = _all(at_0010, "SELECT name, sql FROM sqlite_master WHERE tbl_name NOT LIKE 'import%' ORDER BY name")
    assert after == before
    command.upgrade(alembic_config(at_0010), "head")  # les modèles décrivent la tête (0012 et suivantes)
    assert schema_differences(at_0010) == [] and verify_schema(at_0010) == []


def test_a_file_is_applied_only_once_but_may_be_simulated_again(at_0010):
    command.upgrade(alembic_config(at_0010), "0011")
    sha = "a" * 64
    _execute(at_0010, BATCH.format(sha=sha, status="simulated"), BATCH.format(sha=sha, status="simulated"))
    _execute(at_0010, BATCH.format(sha=sha, status="applied"))
    with pytest.raises(sqlite3.IntegrityError):
        _execute(at_0010, BATCH.format(sha=sha, status="applying"))
    _execute(at_0010, BATCH.format(sha=sha, status="cancelled"))
    assert _all(at_0010, "SELECT status, COUNT(*) FROM import_batches GROUP BY status ORDER BY status") == [
        ("applied", 1),
        ("cancelled", 1),
        ("simulated", 2),
    ]


def test_values_outside_the_lists_are_refused(at_0010):
    command.upgrade(alembic_config(at_0010), "0011")
    with pytest.raises(sqlite3.IntegrityError):
        _execute(at_0010, BATCH.format(sha="b" * 64, status="peut-être"))
    _execute(at_0010, BATCH.format(sha="b" * 64, status="simulated"))
    for statement in (
        "INSERT INTO import_rows (batch_id, row_number, raw, action) VALUES (1, 0, '{}', 'create')",
        "INSERT INTO import_rows (batch_id, row_number, raw, action) VALUES (1, 2, '{}', 'effacer')",
        "INSERT INTO import_rows (batch_id, row_number, raw, action, severity) VALUES (1, 2, '{}', 'create', 'grave')",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _execute(at_0010, statement)
    _execute(at_0010, "INSERT INTO import_rows (batch_id, row_number, raw, action) VALUES (1, 2, '{}', 'create')")
    with pytest.raises(sqlite3.IntegrityError):  # une ligne du fichier une seule fois par lot
        _execute(at_0010, "INSERT INTO import_rows (batch_id, row_number, raw, action) VALUES (1, 2, '{}', 'create')")
    with pytest.raises(sqlite3.IntegrityError):
        _execute(
            at_0010, "INSERT INTO import_links (row_id, entity_type, entity_id, created) VALUES (1, 'lecteur', 1, 1)"
        )


def test_rows_go_with_their_batch(at_0010):
    command.upgrade(alembic_config(at_0010), "0011")
    _execute(
        at_0010,
        BATCH.format(sha="c" * 64, status="simulated"),
        "INSERT INTO import_rows (batch_id, row_number, raw, action) VALUES (1, 2, '{}', 'create')",
        "INSERT INTO import_links (row_id, entity_type, entity_id, created) VALUES (1, 'work', 7, 1)",
        "DELETE FROM import_batches WHERE id = 1",
    )
    assert _all(at_0010, "SELECT COUNT(*) FROM import_rows") == [(0,)]
    assert _all(at_0010, "SELECT COUNT(*) FROM import_links") == [(0,)]


def test_downgrade_undoes_exactly_when_nothing_was_recorded(at_0010):
    before = _all(at_0010, "SELECT name, sql FROM sqlite_master ORDER BY name")
    command.upgrade(alembic_config(at_0010), "0011")
    command.downgrade(alembic_config(at_0010), "0010")
    assert _all(at_0010, "SELECT name, sql FROM sqlite_master ORDER BY name") == before


def test_downgrade_is_refused_once_a_batch_exists(at_0010):
    command.upgrade(alembic_config(at_0010), "0011")
    _execute(at_0010, BATCH.format(sha="d" * 64, status="simulated"))
    with pytest.raises(RuntimeError, match="Restaurez la sauvegarde"):
        command.downgrade(alembic_config(at_0010), "0010")
