# SPDX-License-Identifier: EUPL-1.2
"""Révision 0012 (L6, T5) : code-barres facultatif, propositions élargies, identifiant d'opération ; lignes,
vue, déclencheur et prêts conservés ; descente refusée dès qu'un import ou un exemplaire sans code existe."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences, verify_schema

DEPENDENTS = (
    "SELECT name, sql FROM sqlite_master WHERE name IN ('copy_availability', 'trg_loans_copy_lendable') ORDER BY name"
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
def at_0011(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0011")
    _execute(
        bare_settings,
        "INSERT INTO works (id, public_id, document_type_id, title, title_sort, title_norm, status, completeness,"
        " version, created_at, updated_at) VALUES (1, 'w1', 1, 'Titre', 'titre', 'titre', 'active', 0, 1, '2026-11-13',"
        " '2026-11-13')",
        "INSERT INTO editions (id, public_id, work_id, is_primary, language, status, origin, version, created_at,"
        " updated_at) VALUES (1, 'e1', 1, 1, 'fre', 'active', 'manual', 1, '2026-11-13', '2026-11-13')",
        "INSERT INTO copies (id, public_id, edition_id, barcode, inventory_number, call_number, status, created_at,"
        " updated_at) VALUES (1, 'c1', 1, 'EX-000001', 'EX-000001', 'TIT/01', 'available', '2026-11-13', '2026-11-13')",
        "INSERT INTO copies (id, public_id, edition_id, barcode, inventory_number, status, created_at, updated_at)"
        " VALUES (2, 'c2', 1, 'EX-000002', '42', 'in_processing', '2026-11-13', '2026-11-13')",
    )
    return bare_settings


def test_upgrade_keeps_every_copy_and_the_objects_that_read_copies(at_0011):
    before = _all(at_0011, "SELECT * FROM copies ORDER BY id")
    dependents = _all(at_0011, DEPENDENTS)
    assert len(dependents) == 2
    objects = _all(at_0011, "SELECT type, name FROM sqlite_master WHERE tbl_name = 'copies' OR sql LIKE '%copies%'")
    command.upgrade(alembic_config(at_0011), "0012")
    assert _all(at_0011, "SELECT * FROM copies ORDER BY id") == before
    command.upgrade(alembic_config(at_0011), "head")  # 0013 n'ajoute que deux tables, rien ne change pour copies
    assert schema_differences(at_0011) == [] and verify_schema(at_0011) == []
    assert _all(at_0011, "SELECT * FROM copies ORDER BY id") == before
    after = _all(at_0011, "SELECT type, name FROM sqlite_master WHERE tbl_name = 'copies' OR sql LIKE '%copies%'")
    assert set(objects) <= set(after)
    assert _all(at_0011, "SELECT availability FROM copy_availability ORDER BY copy_id") == [
        ("available",),
        ("in_processing",),
    ]
    # Un exemplaire sans code-barres est admis ; deux aussi ; un code reste unique
    _execute(
        at_0011,
        "INSERT INTO copies (public_id, edition_id, barcode, inventory_number, status, created_at, updated_at)"
        " VALUES ('c3', 1, NULL, '7', 'in_processing', '2026-11-13', '2026-11-13')",
        "INSERT INTO copies (public_id, edition_id, barcode, inventory_number, status, created_at, updated_at)"
        " VALUES ('c4', 1, NULL, '8', 'in_processing', '2026-11-13', '2026-11-13')",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _execute(
            at_0011,
            "INSERT INTO copies (public_id, edition_id, barcode, inventory_number, status, created_at, updated_at)"
            " VALUES ('c5', 1, 'EX-000001', '9', 'in_processing', '2026-11-13', '2026-11-13')",
        )
    # La vue et le déclencheur des prêts sont recréés à l'identique, sur la table reconstruite
    assert dependents == _all(at_0011, DEPENDENTS)


def test_the_new_proposal_kinds_are_accepted_and_others_refused(at_0011):
    command.upgrade(alembic_config(at_0011), "0012")
    _execute(
        at_0011,
        "INSERT INTO import_proposals (edition_id, field, value, created_at) VALUES (1, 'isbn', '978', 'x')",
        "INSERT INTO import_proposals (edition_id, field, value, created_at) VALUES (1, 'name_form', 'A B', 'x')",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _execute(
            at_0011, "INSERT INTO import_proposals (edition_id, field, value, created_at) VALUES (1, 'x', 'v', 'x')"
        )


def test_downgrade_is_refused_once_a_copy_has_no_barcode_and_otherwise_restores_0011(at_0011):
    command.upgrade(alembic_config(at_0011), "0012")
    before = _all(at_0011, "SELECT * FROM copies ORDER BY id")
    command.downgrade(alembic_config(at_0011), "0011")
    assert _all(at_0011, "SELECT * FROM copies ORDER BY id") == before
    assert _all(at_0011, "SELECT \"notnull\" FROM pragma_table_info('copies') WHERE name = 'barcode'") == [(1,)]
    command.upgrade(alembic_config(at_0011), "0012")
    _execute(
        at_0011,
        "INSERT INTO copies (public_id, edition_id, barcode, inventory_number, status, created_at, updated_at)"
        " VALUES ('c3', 1, NULL, '7', 'in_processing', '2026-11-13', '2026-11-13')",
    )
    with pytest.raises(Exception, match="Descente refusée"):
        command.downgrade(alembic_config(at_0011), "0011")
    assert _all(at_0011, "SELECT COUNT(*) FROM copies") == [(3,)]
