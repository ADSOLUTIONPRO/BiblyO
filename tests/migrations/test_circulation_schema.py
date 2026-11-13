# SPDX-License-Identifier: EUPL-1.2
"""Révision 0008 : prêts et droits explicites, rôles ajustés sans écraser une personnalisation, retour arrière."""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest
from alembic import command

from biblyo.core.migrate import alembic_config, schema_differences, verify_schema

MOMENT = "2026-11-09T10:00:00Z"
NEW_PERMISSIONS = {"circulation.settings", "circulation.override_limit"}


def _connect(settings) -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _execute(settings, query: str, *params) -> None:
    connection = _connect(settings)
    try:
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


def _all(settings, query: str, *params):
    connection = _connect(settings)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _permissions(settings, code: str) -> set[str]:
    rows = _all(
        settings, "SELECT permission FROM role_permissions JOIN roles ON roles.id = role_id WHERE roles.code = ?", code
    )
    return {row[0] for row in rows}


def _shipped(settings):
    return {code: _permissions(settings, code) for code in ("admin", "librarian", "volunteer", "viewer")}


@pytest.fixture
def at_0007(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0007")
    return bare_settings


@pytest.fixture
def at_head(bare_settings):
    command.upgrade(alembic_config(bare_settings), "head")
    return bare_settings


def _seed_people(connection, *, category="minor") -> None:
    for number, (first, kind) in enumerate((("Léa", category), ("Paul", "adult")), 1):
        connection.execute(
            "INSERT INTO patrons (id, public_id, number, first_name, last_name, name_norm, sort_key, category, status,"
            " registered_on, version, created_at, updated_at) VALUES (?, ?, ?, ?, 'Martin', 'x', 'x', ?, 'active', 'x',"
            " 1, 'x', 'x')",
            (number, f"u{number}", f"P-{number:06d}", first, kind),
        )
    connection.execute(
        "INSERT INTO households (id, public_id, kind, name, name_norm, version, created_at, updated_at)"
        " VALUES (1, 'h1', 'structure', 'École', 'ecole', 1, 'x', 'x')"
    )


def _seed_copy(connection, copy_id=1, status="available", loanable=1) -> None:
    connection.commit()  # le pragma n'a aucun effet dans une transaction ouverte
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO copies (id, public_id, edition_id, barcode, status, is_loanable, inventory_number, created_at,"
        " updated_at) VALUES (?, ?, 1, ?, ?, ?, ?, 'x', 'x')",
        (copy_id, f"c{copy_id}", f"EX-{copy_id:06d}", status, loanable, f"EX-{copy_id:06d}"),
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")


LOAN = (
    "INSERT INTO loans (operation_id, copy_id, patron_id, responsible_id, household_id, borrow_mode, loaned_at, due_on,"
    " returned_at, cancelled_at) VALUES (1, ?, ?, ?, ?, ?, 'a', 'b', ?, ?)"
)


@pytest.fixture
def ready(at_head):
    connection = _connect(at_head)
    _seed_people(connection, category="adult")
    _seed_copy(connection, 1)
    _seed_copy(connection, 2)
    connection.execute("INSERT INTO loan_operations (id, kind, created_at) VALUES (1, 'lend', 'a')")
    yield connection
    connection.close()


# --- Montée --------------------------------------------------------------------------------------------------------
def test_upgrade_reaches_a_conformant_schema(at_0007):
    command.upgrade(alembic_config(at_0007), "0008")
    assert _all(at_0007, "SELECT version_num FROM alembic_version") == [("0008",)]
    command.upgrade(alembic_config(at_0007), "head")  # les modèles décrivent le schéma de la dernière révision
    assert schema_differences(at_0007) == [] and verify_schema(at_0007) == []


def test_no_right_is_granted_by_the_migration(at_0007):
    connection = _connect(at_0007)
    _seed_people(connection)
    connection.execute(
        "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank,"
        " version, created_at, updated_at) VALUES (1, 2, 'parent', 1, 1, 1, 1, 'x', 'x')"
    )
    connection.execute(
        "INSERT INTO household_members (household_id, patron_id, role, joined_on) VALUES (1, 2, 'referent', 'x')"
    )
    connection.commit()
    connection.close()
    command.upgrade(alembic_config(at_0007), "0008")
    assert _all(at_0007, "SELECT can_borrow_for, can_borrow_alone FROM guardianships") == [(1, 0)]
    assert _all(at_0007, "SELECT can_borrow_for_structure FROM household_members") == [(0,)]  # référent : aucun droit


def test_default_rules_and_settings(at_head):
    assert sorted(
        _all(at_head, "SELECT patron_category, loan_days, max_renewals, max_active_loans FROM loan_rules")
    ) == [("adult", 28, 1, 5), ("minor", 21, 1, 3), ("structure", 42, 1, 15)]
    stored = dict(_all(at_head, "SELECT key, value FROM settings WHERE key LIKE 'circulation.%'"))
    assert {key: json.loads(value) for key, value in stored.items()} == {
        "circulation.limit_mode": "avertir",
        "circulation.overdue_block": None,
        "circulation.closed_weekday": None,
        "circulation.retention_months": 12,
    }


# --- Rôles ------------------------------------------------------------------------------------------------------------
def test_shipped_librarian_receives_the_two_permissions(at_0007):
    before = _shipped(at_0007)
    command.upgrade(alembic_config(at_0007), "0008")
    after = _shipped(at_0007)
    assert after["librarian"] == before["librarian"] | NEW_PERMISSIONS
    assert after["admin"] == before["admin"] == {"*"}
    assert after["volunteer"] == before["volunteer"] and after["viewer"] == before["viewer"]
    assert not {"circulation.override", "circulation.history"} & after["volunteer"]
    changes = _all(at_0007, "SELECT permission, change FROM role_permission_changes WHERE revision = '0008'")
    assert sorted(changes) == [("circulation.override_limit", "added"), ("circulation.settings", "added")]


def test_a_customized_librarian_is_never_overwritten(at_0007):
    _execute(at_0007, "DELETE FROM role_permissions WHERE permission = 'admin.sources'")
    custom = _permissions(at_0007, "librarian")
    command.upgrade(alembic_config(at_0007), "0008")
    assert _permissions(at_0007, "librarian") == custom
    kept = _all(at_0007, "SELECT details FROM audit_log WHERE action = 'migration.role_kept'")
    assert len(kept) == 1 and '"0008"' in kept[0][0]


def test_a_local_role_is_never_touched(at_0007):
    _execute(
        at_0007,
        "INSERT INTO roles (id, code, label, is_system, created_at) VALUES (9, 'accueil_plus', 'Accueil+', 0, ?)",
        MOMENT,
    )
    _execute(at_0007, "INSERT INTO role_permissions (role_id, permission) VALUES (9, 'circulation.lend')")
    command.upgrade(alembic_config(at_0007), "0008")
    assert _permissions(at_0007, "accueil_plus") == {"circulation.lend"}


def test_missing_librarian_is_tolerated(at_0007):
    _execute(at_0007, "DELETE FROM user_roles WHERE role_id = 2")
    _execute(at_0007, "DELETE FROM roles WHERE code = 'librarian'")
    command.upgrade(alembic_config(at_0007), "0008")
    assert _all(at_0007, "SELECT COUNT(*) FROM role_permission_changes WHERE revision = '0008'") == [(0,)]


# --- Collisions de codes ----------------------------------------------------------------------------------------------
def test_collisions_are_reported_without_modification_nor_number(at_0007, caplog):
    connection = _connect(at_0007)
    _seed_people(connection, category="adult")
    _seed_copy(connection, 1)
    cards = [("EX-000001", 1, None, None), ("P-000123", 2, None, None)]
    connection.executemany(
        "INSERT INTO patron_cards (number, patron_id, issued_on, ended_on, end_reason, created_at)"
        " VALUES (?, ?, 'x', ?, ?, 'x')",
        cards,
    )
    connection.commit()
    connection.close()
    before = _all(at_0007, "SELECT number, patron_id FROM patron_cards ORDER BY number")
    with caplog.at_level(logging.WARNING, logger="biblyo.migrate"):
        command.upgrade(alembic_config(at_0007), "0008")
    assert _all(at_0007, "SELECT number, patron_id FROM patron_cards ORDER BY number") == before
    entries = _all(at_0007, "SELECT details FROM audit_log WHERE action = 'migration.code_collisions'")
    assert [json.loads(row[0]) for row in entries] == [{"exemplaires": 1, "numeros_lecteur": 1}]
    assert "EX-000001" not in entries[0][0] and "P-000123" not in entries[0][0]


def test_no_collision_says_nothing(at_0007):
    command.upgrade(alembic_config(at_0007), "0008")
    assert _all(at_0007, "SELECT COUNT(*) FROM audit_log WHERE action = 'migration.code_collisions'") == [(0,)]


# --- Descente ---------------------------------------------------------------------------------------------------------
def test_downgrade_restores_0007_exactly(at_0007):
    before = _shipped(at_0007)
    command.upgrade(alembic_config(at_0007), "0008")
    command.downgrade(alembic_config(at_0007), "0007")
    assert _shipped(at_0007) == before
    names = {row[0] for row in _all(at_0007, "SELECT name FROM sqlite_master")}
    assert not names & {"loans", "loan_rules", "loan_events", "loan_reminders", "loan_operations", "copy_availability"}
    columns = {row[1] for row in _all(at_0007, "PRAGMA table_info(guardianships)")}
    columns |= {row[1] for row in _all(at_0007, "PRAGMA table_info(household_members)")}
    assert not columns & {"can_borrow_alone", "can_borrow_for_structure"}
    assert _all(at_0007, "SELECT COUNT(*) FROM settings WHERE key LIKE 'circulation.%'") == [(0,)]
    assert _all(at_0007, "SELECT COUNT(*) FROM role_permission_changes WHERE revision = '0008'") == [(0,)]
    command.upgrade(alembic_config(at_0007), "head")
    assert verify_schema(at_0007) == []


def test_downgrade_keeps_a_later_local_choice(at_0007):
    command.upgrade(alembic_config(at_0007), "0008")
    _execute(at_0007, "INSERT INTO role_permissions (role_id, permission) VALUES (2, 'labels.special')")
    command.downgrade(alembic_config(at_0007), "0007")
    librarian = _permissions(at_0007, "librarian")
    assert "labels.special" in librarian and not NEW_PERMISSIONS & librarian


@pytest.mark.parametrize("what", ["operation", "loan", "reminder", "alone", "structure"])
def test_downgrade_is_refused_when_loan_data_exists(at_head, what):
    connection = _connect(at_head)
    _seed_people(connection)
    if what == "alone":
        connection.execute(
            "INSERT INTO guardianships (minor_id, guardian_id, relation, can_borrow_for, is_contact, contact_rank,"
            " can_borrow_alone, version, created_at, updated_at) VALUES (1, 2, 'parent', 0, 1, 1, 1, 1, 'x', 'x')"
        )
    elif what == "structure":
        connection.execute(
            "INSERT INTO household_members (household_id, patron_id, role, joined_on, can_borrow_for_structure)"
            " VALUES (1, 2, 'member', 'x', 1)"
        )
    else:
        connection.execute("INSERT INTO loan_operations (id, kind, created_at) VALUES (1, 'lend', 'a')")
    if what in ("loan", "reminder"):
        _seed_copy(connection)
        connection.execute(LOAN, (1, 2, None, None, "self", None, None))
    if what == "reminder":
        connection.execute(
            "INSERT INTO loan_reminders (loan_id, reminded_on, channel, result, created_at)"
            " VALUES (1, 'a', 'phone', 'reached', 'a')"
        )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="jamais supprimées"):
        command.downgrade(alembic_config(at_head), "0007")
    assert _all(at_head, "SELECT version_num FROM alembic_version") == [("0008",)]
    assert _all(at_head, "SELECT COUNT(*) FROM loan_operations") == [(0 if what in ("alone", "structure") else 1,)]


# --- Contraintes ------------------------------------------------------------------------------------------------------
def test_a_copy_cannot_be_on_two_active_loans(ready):
    ready.execute(LOAN, (1, 2, None, None, "self", None, None))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        ready.execute(LOAN, (1, 1, None, None, "self", None, None))


@pytest.mark.parametrize("ending", [("returned_at", "z"), ("cancelled_at", "z")])
def test_a_returned_or_cancelled_loan_frees_the_copy(ready, ending):
    ready.execute(LOAN, (1, 2, None, None, "self", None, None))
    ready.execute(f"UPDATE loans SET {ending[0]} = ?", (ending[1],))  # noqa: S608
    ready.execute(LOAN, (1, 1, None, None, "self", None, None))
    assert ready.execute("SELECT COUNT(*) FROM loans").fetchone() == (2,)


@pytest.mark.parametrize("status", ["in_processing", "in_repair", "missing", "lost", "withdrawn"])
def test_the_base_refuses_a_copy_that_cannot_leave(ready, status):
    _seed_copy(ready, 3, status=status)
    with pytest.raises(sqlite3.IntegrityError, match="non empruntable"):
        ready.execute(LOAN, (3, 2, None, None, "self", None, None))


def test_the_base_refuses_a_reference_only_copy(ready):
    _seed_copy(ready, 3, loanable=0)
    with pytest.raises(sqlite3.IntegrityError, match="non empruntable"):
        ready.execute(LOAN, (3, 2, None, None, "self", None, None))


@pytest.mark.parametrize(
    "values",
    [
        (1, 1, 1, None, "with_adult", None, None),  # responsable = emprunteur
        (1, 2, None, None, "with_adult", None, None),  # un mineur sans responsable
        (1, 2, None, None, "alone", None, None),
        (1, 2, 1, None, "self", None, None),  # pour soi, avec un responsable
        (1, 2, None, 1, "self", None, None),
        (1, 2, None, None, "structure", None, None),  # structure non désignée
        (1, 2, None, None, "famille", None, None),  # cadre inconnu
    ],
)
def test_a_loan_says_who_answers_for_it(ready, values):
    with pytest.raises(sqlite3.IntegrityError):
        ready.execute(LOAN, values)


def test_after_return_the_responsible_may_be_erased(ready):
    ready.execute(LOAN, (1, 1, 2, None, "with_adult", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        ready.execute("UPDATE loans SET responsible_id = NULL")
    ready.execute("UPDATE loans SET returned_at = 'z', responsible_id = NULL")
    assert ready.execute("SELECT responsible_id FROM loans").fetchone() == (None,)


def test_return_cannot_precede_the_loan(ready):
    ready.execute(LOAN, (1, 2, None, None, "self", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        ready.execute("UPDATE loans SET returned_at = '0'")


def test_a_person_or_copy_on_loan_cannot_be_deleted(ready):
    ready.execute(LOAN, (1, 2, None, None, "self", None, None))
    for query in ("DELETE FROM patrons WHERE id = 2", "DELETE FROM copies WHERE id = 1"):
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            ready.execute(query)


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO loan_reminders (loan_id, reminded_on, channel, result, created_at) VALUES (1,'a','sms','reached','a')",  # noqa: E501
        "INSERT INTO loan_reminders (loan_id, reminded_on, channel, result, created_at) VALUES (1,'a','phone','note','a')",  # noqa: E501
        "INSERT INTO loan_events (loan_id, kind, at) VALUES (1, 'note', 'a')",
        "INSERT INTO loan_operations (kind, created_at) VALUES ('purge', 'a')",
    ],
)
def test_closed_vocabularies(ready, query):
    ready.execute(LOAN, (1, 2, None, None, "self", None, None))
    with pytest.raises(sqlite3.IntegrityError):
        ready.execute(query)


def test_a_reminder_has_no_free_text_column(at_head):
    columns = {row[1] for row in _all(at_head, "PRAGMA table_info(loan_reminders)")}
    assert columns == {"id", "loan_id", "reminded_on", "channel", "result", "user_id", "created_at"}
    assert "notes" not in {row[1] for row in _all(at_head, "PRAGMA table_info(loans)")}


@pytest.mark.parametrize(
    "values",
    [
        (None, "adult", 7, 7, 1, 2),  # portée déjà livrée, malgré un type vide
        (None, "enfant", 7, 7, 1, 2),
        (None, None, 0, 7, 1, 2),
        (None, None, 366, 7, 1, 2),
        (None, None, 7, 7, -1, 2),
        (None, None, 7, 7, 1, 0),
    ],
)
def test_rule_constraints(at_head, values):
    connection = _connect(at_head)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO loan_rules (document_type_id, patron_category, loan_days, renewal_days, max_renewals,"
            " max_active_loans) VALUES (?, ?, ?, ?, ?, ?)",
            values,
        )
    connection.close()


def test_two_general_rules_cannot_coexist(at_head):
    connection = _connect(at_head)
    insert = "INSERT INTO loan_rules (loan_days, renewal_days) VALUES (7, 7)"
    connection.execute(insert)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        connection.execute(insert)
    connection.close()


def test_availability_view(ready):
    _seed_copy(ready, 3, loanable=0)
    _seed_copy(ready, 4, status="in_repair")
    ready.execute(
        "INSERT INTO loans (operation_id, copy_id, patron_id, borrow_mode, loaned_at, due_on)"
        " VALUES (1, 1, 2, 'self', 'a', '2026-12-01')"
    )
    rows = ready.execute("SELECT copy_id, availability, due_on FROM copy_availability ORDER BY copy_id").fetchall()
    assert rows == [
        (1, "on_loan", "2026-12-01"),
        (2, "available", None),
        (3, "reference_only", None),
        (4, "in_repair", None),
    ]
    ready.execute("UPDATE loans SET cancelled_at = 'z'")
    assert ready.execute("SELECT availability FROM copy_availability WHERE copy_id = 1").fetchone() == ("available",)
