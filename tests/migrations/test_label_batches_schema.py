# SPDX-License-Identifier: EUPL-1.2
"""Révision 0014 (L8, T4) : deux tables nouvelles, rien de modifié ni écrit ; une étiquette par case et par rang dans
un lot ; un lot garde le code imprimé même si l'exemplaire disparaît ; descente refusée dès qu'un lot existe."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences, verify_schema

COUNTED = ("works", "editions", "copies", "loans", "patrons", "sequences", "retired_codes", "audit_log", "settings")


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
def at_0013(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0013")
    _execute(
        bare_settings,
        "INSERT INTO works (id, public_id, document_type_id, title, title_sort, title_norm, status, completeness,"
        " version, created_at, updated_at) VALUES (1, 'w1', (SELECT id FROM document_types WHERE code = 'bd'),"
        " 'Le Lotus bleu', 'lotus bleu', 'le lotus bleu', 'active', 40, 1, '2026-11-18', '2026-11-18')",
        "INSERT INTO editions (id, public_id, work_id, is_primary, language, status, origin, version, created_at,"
        " updated_at) VALUES (1, 'e1', 1, 1, 'fre', 'active', 'manual', 1, '2026-11-18', '2026-11-18')",
        "INSERT INTO copies (id, public_id, edition_id, barcode, status, created_at, updated_at, inventory_number,"
        " version) VALUES (1, 'c1', 1, NULL, 'in_processing', '2026-11-18', '2026-11-18', '354', 1)",
    )
    return bare_settings


def _counts(settings) -> dict[str, int]:
    return {table: _all(settings, f"SELECT COUNT(*) FROM {table}")[0][0] for table in COUNTED}  # noqa: S608


def _batch(settings) -> None:
    _execute(
        settings,
        "INSERT INTO label_batches (id, public_id, origin, sheet, model, start_position, total, sheets, created_at,"
        " updated_at) VALUES (1, 'b1', 'selection', '{}', '{}', 8, 1, 1, '2026-11-18', '2026-11-18')",
    )


def test_upgrade_adds_two_tables_and_changes_nothing_else(at_0013):
    before_rows = _counts(at_0013)
    before = set(_all(at_0013, "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    copies = _all(at_0013, "SELECT * FROM copies")
    command.upgrade(alembic_config(at_0013), "0014")
    assert schema_differences(at_0013) == [] and verify_schema(at_0013) == []
    assert _counts(at_0013) == before_rows
    assert _all(at_0013, "SELECT * FROM copies") == copies
    after = set(_all(at_0013, "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    assert before - after == set()
    assert {o[1] for o in after - before if o[0] == "table"} == {"label_batches", "label_batch_items"}
    strict = _all(at_0013, "SELECT name, strict FROM pragma_table_list WHERE name LIKE 'label_%'")
    assert sorted(strict) == [("label_batch_items", 1), ("label_batches", 1)]


def test_one_label_per_rank_and_per_slot(at_0013):
    command.upgrade(alembic_config(at_0013), "0014")
    _batch(at_0013)
    _execute(
        at_0013,
        "INSERT INTO label_batch_items (batch_id, rank, copy_id, barcode, sheet_number, position)"
        " VALUES (1, 1, 1, 'EX-000001', 1, 8)",
    )
    insert = "INSERT INTO label_batch_items (batch_id, rank, copy_id, barcode, sheet_number, position) VALUES "
    for values in ("(1, 1, 1, 'EX-000002', 1, 9)", "(1, 2, 1, 'EX-000002', 1, 8)", "(1, 3, 1, 'EX-000003', 0, 1)"):
        with pytest.raises(sqlite3.IntegrityError):
            _execute(at_0013, insert + values)
    with pytest.raises(sqlite3.IntegrityError):
        _execute(at_0013, "UPDATE label_batches SET status = 'printed' WHERE id = 1")  # jamais « imprimé » de lui-même


def test_the_printed_code_survives_the_copy(at_0013):
    command.upgrade(alembic_config(at_0013), "0014")
    _batch(at_0013)
    _execute(
        at_0013,
        "INSERT INTO label_batch_items (batch_id, rank, copy_id, barcode, sheet_number, position)"
        " VALUES (1, 1, 1, 'EX-000001', 1, 8)",
        "DELETE FROM copies WHERE id = 1",
    )
    assert _all(at_0013, "SELECT copy_id, barcode FROM label_batch_items") == [(None, "EX-000001")]


def test_downgrade_is_refused_once_a_batch_exists(at_0013):
    command.upgrade(alembic_config(at_0013), "0014")
    _batch(at_0013)
    with pytest.raises(RuntimeError, match="Descente refusée"):
        command.downgrade(alembic_config(at_0013), "0013")
    assert _all(at_0013, "SELECT version_num FROM alembic_version") == [("0014",)]


def test_downgrade_without_batch_removes_the_two_tables(at_0013):
    command.upgrade(alembic_config(at_0013), "0014")
    command.downgrade(alembic_config(at_0013), "0013")
    tables = {row[0] for row in _all(at_0013, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"label_batches", "label_batch_items"} & tables


def test_printer_role_is_added_alone_and_protected(at_0013):
    roles_before = _all(at_0013, "SELECT code, label FROM roles ORDER BY code")
    grants_before = _all(at_0013, "SELECT role_id, permission FROM role_permissions ORDER BY 1, 2")
    command.upgrade(alembic_config(at_0013), "0014")
    added = set(_all(at_0013, "SELECT code, label FROM roles")) - set(roles_before)
    assert added == {("labels_printer", "Impression des étiquettes")}
    grants = set(_all(at_0013, "SELECT role_id, permission FROM role_permissions")) - set(grants_before)
    assert [permission for _role, permission in grants] == ["labels.print"]  # aucun autre rôle n'est élargi
    assert _all(at_0013, "SELECT COUNT(*) FROM user_roles ur JOIN roles r ON r.id = ur.role_id"
                         " WHERE r.code = 'labels_printer'") == [(0,)]  # fmt: skip
    _execute(
        at_0013,
        "INSERT INTO users (id, username, display_name, password_hash, is_active, must_change_password, failed_logins,"
        " created_at, updated_at) VALUES (9, 'benevole', 'Bénévole', 'x', 1, 0, 0, '2026-11-18', '2026-11-18')",
        "INSERT INTO user_roles (user_id, role_id) SELECT 9, id FROM roles WHERE code = 'labels_printer'",
    )
    with pytest.raises(RuntimeError, match="Impression des étiquettes"):
        command.downgrade(alembic_config(at_0013), "0013")
    _execute(at_0013, "DELETE FROM user_roles WHERE user_id = 9")
    command.downgrade(alembic_config(at_0013), "0013")
    assert _all(at_0013, "SELECT code, label FROM roles ORDER BY code") == roles_before
    assert _all(at_0013, "SELECT role_id, permission FROM role_permissions ORDER BY 1, 2") == grants_before
