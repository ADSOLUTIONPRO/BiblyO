# SPDX-License-Identifier: EUPL-1.2
"""Révisions du catalogue : contraintes, amorçage, et surtout préservation des données d'une révision à l'autre."""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command

from biblyo.core.backup import list_backups, verify_backup
from biblyo.core.migrate import alembic_config, pending_revisions, upgrade
from biblyo.core.schema_state import SchemaStatus, read_state, script_directory

HEAD = script_directory().get_current_head()
MOMENT = "2026-01-01T00:00:00Z"


def _connect(settings) -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _insert_work(connection, public_id="w1", title="Essai", type_id=1) -> int:
    cursor = connection.execute(
        "INSERT INTO works (public_id, document_type_id, title, title_sort, title_norm, status, completeness, version,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', 0, 1, ?, ?)",
        (public_id, type_id, title, title.lower(), title.lower(), MOMENT, MOMENT),
    )
    return cursor.lastrowid


def _insert_edition(connection, work_id, public_id="e1", primary=1, year=None) -> int:
    cursor = connection.execute(
        "INSERT INTO editions (public_id, work_id, is_primary, publication_year, language, status, origin, version,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 'fre', 'active', 'manual', 1, ?, ?)",
        (public_id, work_id, primary, year, MOMENT, MOMENT),
    )
    return cursor.lastrowid


def _insert_copy(connection, edition_id, barcode="EX-000001") -> int:
    """Le numéro d'inventaire n'existe qu'à partir de la révision 0006 : il prend alors la valeur du code-barres."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(copies)")}
    extra = ", inventory_number" if "inventory_number" in columns else ""
    values = (f"c-{barcode}", edition_id, barcode, MOMENT, MOMENT, *((barcode,) if extra else ()))
    cursor = connection.execute(
        f"INSERT INTO copies (public_id, edition_id, barcode, status, created_at, updated_at{extra})"  # noqa: S608
        f" VALUES (?, ?, ?, 'in_processing', ?, ?{', ?' if extra else ''})",
        values,
    )
    return cursor.lastrowid


def test_document_types_and_sequence_are_seeded(settings):
    connection = _connect(settings)
    codes = {row[0] for row in connection.execute("SELECT code FROM document_types")}
    sequence = connection.execute(
        "SELECT prefix, padding, last_value FROM sequences WHERE name = 'copy_barcode'"
    ).fetchone()
    connection.close()
    assert codes == {"livre", "bd", "manga", "comics", "revue", "documentaire", "album"}
    assert sequence == ("EX-", 6, 0)


def test_no_library_data_is_seeded(settings):
    connection = _connect(settings)
    counts = connection.execute(
        "SELECT (SELECT COUNT(*) FROM works) + (SELECT COUNT(*) FROM editions)"
        " + (SELECT COUNT(*) FROM copies) + (SELECT COUNT(*) FROM tags)"
    ).fetchone()[0]
    connection.close()
    assert counts == 0


def test_a_work_can_have_two_editions_and_an_edition_two_copies(settings):
    connection = _connect(settings)
    work = _insert_work(connection)
    first = _insert_edition(connection, work, "e1", 1, 1961)
    second = _insert_edition(connection, work, "e2", 0, 2019)
    _insert_copy(connection, second, "EX-000001")
    _insert_copy(connection, second, "EX-000002")
    connection.commit()
    assert connection.execute("SELECT COUNT(*) FROM editions WHERE work_id = ?", (work,)).fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM copies WHERE edition_id = ?", (second,)).fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM copies WHERE edition_id = ?", (first,)).fetchone()[0] == 0
    connection.close()


def test_two_works_can_share_a_title(settings):
    connection = _connect(settings)
    _insert_work(connection, "w1", "Le Secret")
    _insert_work(connection, "w2", "Le Secret")
    connection.commit()
    assert connection.execute("SELECT COUNT(*) FROM works WHERE title = 'Le Secret'").fetchone()[0] == 2
    connection.close()


def test_a_work_has_at_most_one_primary_edition(settings):
    connection = _connect(settings)
    work = _insert_work(connection)
    _insert_edition(connection, work, "e1", 1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_edition(connection, work, "e2", 1)
    connection.close()


def test_a_work_has_at_most_one_primary_series(settings):
    connection = _connect(settings)
    work = _insert_work(connection)
    for public_id, title in (("s1", "a"), ("s2", "b")):
        connection.execute(
            "INSERT INTO series (public_id, title, title_sort, title_norm, next_call_sequence, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 1, 'unknown', ?, ?)",
            (public_id, title, title, title, MOMENT, MOMENT),
        )
    connection.execute("INSERT INTO work_series (work_id, series_id, is_primary) VALUES (?, 1, 1)", (work,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO work_series (work_id, series_id, is_primary) VALUES (?, 2, 1)", (work,))
    connection.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE works SET title = '   ' WHERE id = 1",
        "UPDATE works SET audience = 'martiens' WHERE id = 1",
        "UPDATE works SET completeness = 101 WHERE id = 1",
        "UPDATE works SET status = 'draft' WHERE id = 1",
        "UPDATE editions SET publication_year = 1200 WHERE id = 1",
        "UPDATE editions SET page_count = 0 WHERE id = 1",
        "UPDATE copies SET status = 'prete' WHERE id = 1",
        "INSERT INTO tags (kind, label, label_norm, created_at) VALUES ('theme', 'x', 'x', '2026')",
        "INSERT INTO work_contributors (work_id, agent_id, role) VALUES (1, 1, 'chanteur')",
        "UPDATE editions SET publication_year = 'mille' WHERE id = 1",  # STRICT : le type est contrôlé
    ],
)
def test_constraints(settings, statement):
    connection = _connect(settings)
    work = _insert_work(connection)
    edition = _insert_edition(connection, work)
    _insert_copy(connection, edition)
    connection.execute(
        "INSERT INTO agents (kind, display_name, sort_name, name_norm, created_at) VALUES ('person', 'A', 'a', 'a', ?)",
        (MOMENT,),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)
    connection.close()


def test_a_copy_prevents_deleting_its_edition(settings):
    connection = _connect(settings)
    work = _insert_work(connection)
    edition = _insert_edition(connection, work)
    _insert_copy(connection, edition)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM editions WHERE id = ?", (edition,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM works WHERE id = ?", (work,))
    connection.close()


def test_upgrade_from_an_initialized_l0_database_keeps_accounts_and_backs_up_first(bare_settings):
    """Base déjà initialisée, en révision 0001 avec des comptes : le cas réel de l'instance de développement."""
    command.upgrade(alembic_config(bare_settings), "0001")
    connection = sqlite3.connect(bare_settings.database_path)
    connection.execute(
        "INSERT INTO users (username, display_name, password_hash, is_active, must_change_password, failed_logins,"
        " created_at, updated_at) VALUES ('camille', 'Camille', 'x', 1, 0, 0, ?, ?)",
        (MOMENT, MOMENT),
    )
    connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('library.name', '\"Essai\"', ?)", (MOMENT,)
    )
    connection.commit()
    connection.close()
    assert read_state(bare_settings).status is SchemaStatus.BEHIND

    report = upgrade(bare_settings)

    assert report.previous == "0001"
    assert report.current == HEAD
    assert report.backup is not None
    assert verify_backup(list_backups(bare_settings)[0])["schema_revision"] == "0001"
    connection = sqlite3.connect(bare_settings.database_path)
    assert connection.execute("SELECT username FROM users").fetchall() == [("camille",)]
    assert connection.execute("SELECT value FROM settings WHERE key = 'library.name'").fetchone() == ('"Essai"',)
    connection.close()
    assert read_state(bare_settings).status is SchemaStatus.OK


def test_each_revision_preserves_the_data_of_the_previous_ones(bare_settings):
    """Toutes les révisions du catalogue, appliquées une à une sur une base déjà remplie."""
    command.upgrade(alembic_config(bare_settings), "0002")
    connection = _connect(bare_settings)
    work = _insert_work(connection, "w1", "Le Lotus bleu")
    edition = _insert_edition(connection, work, "e1", 1, 1936)
    _insert_copy(connection, edition, "EX-000042")
    connection.commit()

    def snapshot():
        return (
            connection.execute("SELECT public_id, title FROM works").fetchall(),
            connection.execute("SELECT public_id, publication_year FROM editions").fetchall(),
            connection.execute("SELECT barcode, status FROM copies").fetchall(),
        )

    before = snapshot()
    connection.close()
    for revision in pending_revisions("0002"):
        command.upgrade(alembic_config(bare_settings), revision)
        connection = _connect(bare_settings)
        assert snapshot() == before, revision
        connection.close()


def test_downgrade_is_refused_once_works_exist(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0002")
    connection = _connect(bare_settings)
    _insert_work(connection)
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="œuvres existent"):
        command.downgrade(alembic_config(bare_settings), "0001")
    connection = _connect(bare_settings)
    assert connection.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1
    connection.close()


def test_downgrade_without_works_returns_to_the_socle(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0002")
    command.downgrade(alembic_config(bare_settings), "0001")
    connection = sqlite3.connect(bare_settings.database_path)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    connection.close()
    assert "works" not in tables and "users" in tables
