# SPDX-License-Identifier: EUPL-1.2
"""Révision 0010 : historique des couvertures, Open Library désactivée, retour arrière refusé après une trace."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences, verify_schema


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
def at_0009(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0009")
    return bare_settings


def test_upgrade_creates_the_history_and_a_disabled_open_library(at_0009):
    before = _all(at_0009, "SELECT code, is_enabled, priority FROM metadata_sources ORDER BY priority")
    command.upgrade(alembic_config(at_0009), "head")  # les modèles décrivent le schéma de la dernière révision
    assert schema_differences(at_0009) == [] and verify_schema(at_0009) == []
    assert _all(at_0009, "SELECT strict FROM pragma_table_list WHERE name = 'cover_changes'") == [(1,)]
    after = _all(at_0009, "SELECT code, is_enabled, priority, covers_enabled FROM metadata_sources ORDER BY priority")
    assert [row[:3] for row in after[:-1]] == before  # rien de réécrit
    assert after[-1] == ("openlibrary", 0, 3, 1)
    assert _all(at_0009, "SELECT action FROM audit_log WHERE action = 'migration.sources_created'")[-1] == (
        "migration.sources_created",
    )


def test_an_unknown_kind_is_refused(at_0009):
    command.upgrade(alembic_config(at_0009), "0010")
    with pytest.raises(sqlite3.IntegrityError):
        _execute(at_0009, "INSERT INTO cover_changes (edition_id, kind, created_at) VALUES (1, 'magie', 'x')")


def test_downgrade_undoes_exactly_when_nothing_was_recorded(at_0009):
    command.upgrade(alembic_config(at_0009), "0010")
    command.downgrade(alembic_config(at_0009), "0009")
    assert _all(at_0009, "SELECT name FROM sqlite_master WHERE name = 'cover_changes'") == []
    assert _all(at_0009, "SELECT code FROM metadata_sources WHERE code = 'openlibrary'") == []


def test_downgrade_is_refused_once_open_library_is_enabled(at_0009):
    command.upgrade(alembic_config(at_0009), "0010")
    _execute(at_0009, "UPDATE metadata_sources SET is_enabled = 1 WHERE code = 'openlibrary'")
    with pytest.raises(RuntimeError, match="Restaurez la sauvegarde"):
        command.downgrade(alembic_config(at_0009), "0009")
