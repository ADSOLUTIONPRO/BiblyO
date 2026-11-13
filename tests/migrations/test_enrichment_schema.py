# SPDX-License-Identifier: EUPL-1.2
"""Révision 0009 : tables de l'enrichissement, sources fournies, provenance des couvertures, retour arrière."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences, verify_schema

from .conftest import fingerprint

NEW_TABLES = {
    "metadata_sources",
    "source_cache",
    "enrichment_searches",
    "enrichment_proposals",
    "enrichment_decisions",
    "edition_isbn_candidates",
    "field_provenance",
}


def _connect(settings) -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _all(settings, query: str, *params):
    connection = _connect(settings)
    try:
        return connection.execute(query, params).fetchall()
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


def _permissions(settings) -> dict[str, set[str]]:
    rows = _all(settings, "SELECT roles.code, permission FROM role_permissions JOIN roles ON roles.id = role_id")
    found: dict[str, set[str]] = {}
    for code, permission in rows:
        found.setdefault(code, set()).add(permission)
    return found


@pytest.fixture
def at_0008(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0008")
    return bare_settings


def test_upgrade_reaches_a_conformant_schema(at_0008):
    command.upgrade(alembic_config(at_0008), "0009")
    command.upgrade(alembic_config(at_0008), "head")  # les modèles décrivent le schéma de la dernière révision
    assert schema_differences(at_0008) == [] and verify_schema(at_0008) == []
    tables = {r[0] for r in _all(at_0008, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables >= NEW_TABLES
    strict = {
        name: flag for name, flag in _all(at_0008, "SELECT name, strict FROM pragma_table_list") if name in NEW_TABLES
    }
    assert strict and all(strict.values())


def test_sources_are_created_bnf_first(at_0008):
    command.upgrade(alembic_config(at_0008), "0009")
    rows = _all(
        at_0008,
        "SELECT code, is_enabled, priority, interval_ms, batch_interval_ms, timeout_ms, covers_enabled, config"
        " FROM metadata_sources ORDER BY priority",
    )
    assert rows == [
        ("bnf", 1, 1, 1000, 5000, 10000, 1, "{}"),
        ("sudoc", 1, 2, 1000, 2000, 15000, 0, "{}"),
    ]
    assert _all(at_0008, "SELECT action FROM audit_log WHERE action = 'migration.sources_created'") == [
        ("migration.sources_created",)
    ]


def test_no_role_and_no_existing_row_is_touched(at_0008):
    before = _permissions(at_0008)
    _execute(
        at_0008,
        "INSERT INTO media_files (sha256, mime, bytes, width, height, origin, origin_url, created_at)"
        " VALUES ('a' , 'image/webp', 10, 32, 32, 'upload', NULL, '2026-11-11T00:00:00Z')",
    )
    command.upgrade(alembic_config(at_0008), "0009")
    assert _permissions(at_0008) == before
    assert _all(at_0008, "SELECT origin, source_code, external_id, license_note, retrieved_at FROM media_files") == [
        ("upload", None, None, None, None)
    ]


def test_cover_provenance_columns_accept_a_source(at_0008):
    command.upgrade(alembic_config(at_0008), "0009")
    _execute(
        at_0008,
        "INSERT INTO media_files (sha256, mime, bytes, origin, origin_url, created_at, source_code, external_id,"
        " license_note, retrieved_at) VALUES ('b', 'image/webp', 10, 'bnf', 'https://catalogue.bnf.fr/ark:/12148/cb0',"
        " 'x', 'bnf', 'ark:/12148/cb0', 'Licence Ouverte', 'x')",
    )
    assert _all(at_0008, "SELECT source_code FROM media_files WHERE sha256 = 'b'") == [("bnf",)]


def test_isbn_candidates_live_apart_from_identifiers(at_0008):
    command.upgrade(alembic_config(at_0008), "0009")
    columns = {r[1] for r in _all(at_0008, "PRAGMA table_info(edition_isbn_candidates)")}
    assert {"isbn13", "status", "score", "agreeing", "conflicting"} <= columns
    with pytest.raises(sqlite3.IntegrityError):
        _execute(
            at_0008,
            "INSERT INTO edition_isbn_candidates (edition_id, isbn13, status, source_code, external_id, external_url,"
            " score, created_at) VALUES (1, '9782070612758', 'maybe', 'bnf', 'x', 'x', 50, 'x')",
        )


def test_a_decision_must_carry_an_action_only_when_accepted(at_0008):
    command.upgrade(alembic_config(at_0008), "0009")
    _execute(
        at_0008,
        "INSERT INTO enrichment_searches (id, criteria, sources, outcome, fingerprint, created_at)"
        " VALUES (1, '{}', '{}', 'exact', 'f', 'x')",
        "INSERT INTO enrichment_proposals (id, search_id, source_code, external_id, external_url, group_key,"
        " match_class, match_level, score, evidence, explanation, payload, raw_record, retrieved_at)"
        " VALUES (1, 1, 'bnf', 'ark', 'u', 'g', 'exact', 1, 100, 80, '{}', '{}', '[]', 'x')",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _execute(
            at_0008,
            "INSERT INTO enrichment_decisions (proposal_id, operation_id, entity_type, entity_id, field, decision,"
            " action, decided_at) VALUES (1, 'op', 'work', 1, 'title', 'rejected', 'fill', 'x')",
        )
    with pytest.raises(sqlite3.IntegrityError):
        _execute(
            at_0008,
            "INSERT INTO enrichment_decisions (proposal_id, operation_id, entity_type, entity_id, field, decision,"
            " action, decided_at) VALUES (1, 'op', 'work', 1, 'title', 'accepted', NULL, 'x')",
        )


def test_downgrade_restores_0008_exactly(at_0008):
    """Tout ce que la révision a créé disparaît ; seul le journal garde la trace de son passage."""
    before = fingerprint(at_0008.database_path)
    config = alembic_config(at_0008)
    command.upgrade(config, "0009")
    command.downgrade(config, "0008")
    after = fingerprint(at_0008.database_path)
    journal = [line for line in after.splitlines() if "migration.sources_created" in line]
    assert len(journal) == 1
    assert [line for line in after.splitlines() if line not in journal] == before.splitlines()
    names = {r[0] for r in _all(at_0008, "SELECT name FROM sqlite_master")}
    assert not names & NEW_TABLES
    columns = {r[1] for r in _all(at_0008, "PRAGMA table_info(media_files)")}
    assert not columns & {"source_code", "external_id", "license_note", "retrieved_at"}
    assert _all(at_0008, "SELECT version_num FROM alembic_version") == [("0008",)]


def test_downgrade_drops_unapplied_searches_with_a_warning(at_0008, caplog):
    config = alembic_config(at_0008)
    command.upgrade(config, "0009")
    _execute(
        at_0008,
        "INSERT INTO enrichment_searches (id, criteria, sources, outcome, fingerprint, created_at)"
        " VALUES (1, '{}', '{}', 'none', 'f', 'x')",
    )
    with caplog.at_level("WARNING", logger="biblyo.migrate"):
        command.downgrade(config, "0008")
    assert "1 recherche(s)" in caplog.text
    assert _all(at_0008, "SELECT action FROM audit_log WHERE action = 'migration.searches_dropped'")


def test_downgrade_is_refused_once_an_enrichment_was_applied(at_0008):
    config = alembic_config(at_0008)
    command.upgrade(config, "0009")
    _execute(
        at_0008,
        "INSERT INTO enrichment_searches (id, criteria, sources, outcome, fingerprint, created_at)"
        " VALUES (1, '{}', '{}', 'exact', 'f', 'x')",
        "INSERT INTO enrichment_proposals (id, search_id, source_code, external_id, external_url, group_key,"
        " match_class, match_level, score, evidence, explanation, payload, raw_record, retrieved_at)"
        " VALUES (1, 1, 'bnf', 'ark', 'u', 'g', 'exact', 1, 100, 80, '{}', '{}', '[]', 'x')",
        "INSERT INTO enrichment_decisions (proposal_id, operation_id, entity_type, entity_id, field, decision,"
        " action, new_value, decided_at) VALUES (1, 'op', 'work', 1, 'title', 'accepted', 'fill', '\"T\"', 'x')",
    )
    with pytest.raises(RuntimeError, match="décisions d'enrichissement : 1"):
        command.downgrade(config, "0008")
    assert {r[0] for r in _all(at_0008, "SELECT name FROM sqlite_master WHERE type = 'table'")} >= NEW_TABLES


def test_downgrade_is_refused_once_a_cover_came_from_a_source(at_0008):
    config = alembic_config(at_0008)
    command.upgrade(config, "0009")
    _execute(
        at_0008,
        "INSERT INTO media_files (sha256, mime, bytes, origin, created_at, source_code, external_id, license_note,"
        " retrieved_at) VALUES ('c', 'image/webp', 10, 'bnf', 'x', 'bnf', 'ark', 'LO', 'x')",
    )
    with pytest.raises(RuntimeError, match="couvertures venues d'une source : 1"):
        command.downgrade(config, "0008")
