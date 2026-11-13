# SPDX-License-Identifier: EUPL-1.2
"""Connexions SQLite : PRAGMA à chaque connexion, transactions annulées, écritures sérialisées."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from biblyo.core.db import Database, readonly_uri
from biblyo.core.models import Setting


def test_foreign_keys_and_wal_on_every_connection(settings):
    database = Database(settings.database_path)
    for _ in range(2):
        with database.session() as session:
            assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert session.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
            assert session.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000
    database.dispose()


def test_foreign_key_violation_is_refused(settings):
    database = Database(settings.database_path)
    with pytest.raises(Exception, match="FOREIGN KEY"), database.session(write=True) as session:
        session.execute(text("INSERT INTO user_roles (user_id, role_id) VALUES (999, 999)"))
    database.dispose()


def test_exception_rolls_back_everything(settings):
    database = Database(settings.database_path)
    with pytest.raises(RuntimeError), database.session(write=True) as session:
        session.add(Setting(key="essai", value="1", updated_at="2026-01-01T00:00:00Z"))
        session.flush()
        raise RuntimeError("annulation")
    with database.session() as session:
        assert session.get(Setting, "essai") is None
    database.dispose()


def test_commit_on_success(settings):
    database = Database(settings.database_path)
    with database.session(write=True) as session:
        session.add(Setting(key="essai", value="1", updated_at="2026-01-01T00:00:00Z"))
    with database.session() as session:
        assert session.get(Setting, "essai").value == "1"
    database.dispose()


def test_write_unit_takes_the_write_lock_at_once(settings):
    """`BEGIN IMMEDIATE` : deux décisions « vérifier puis écrire » ne peuvent pas se croiser."""
    database = Database(settings.database_path)
    other = sqlite3.connect(settings.database_path, timeout=0, isolation_level=None)
    with database.session(write=True), pytest.raises(sqlite3.OperationalError, match="locked"):
        other.execute("BEGIN IMMEDIATE")
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")
    with database.session(write=False):
        other.execute("BEGIN IMMEDIATE")  # une lecture ne bloque pas un écrivain
        other.execute("ROLLBACK")
    other.close()
    database.dispose()


def test_strict_table_refuses_a_wrong_type(settings):
    connection = sqlite3.connect(settings.database_path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO audit_log (at, action, entity_type, entity_id) VALUES ('x', 'a', 'b', 'pas un nombre')"
        )
    connection.close()


def test_readonly_uri_survives_special_characters(tmp_path):
    folder = tmp_path / "dossier#1?essai%"
    folder.mkdir()
    target = folder / "biblyo.sqlite"
    connection = sqlite3.connect(target)
    connection.execute("CREATE TABLE t (id INTEGER)")
    connection.commit()
    connection.close()
    reader = sqlite3.connect(readonly_uri(target), uri=True)
    assert reader.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("INSERT INTO t VALUES (1)")
    reader.close()


def test_readonly_uri_never_creates_a_file(tmp_path):
    folder = tmp_path / "dossier#1"
    folder.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(readonly_uri(folder / "absente.sqlite"), uri=True).execute("SELECT 1")
    assert list(tmp_path.rglob("*absente*")) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dossier#1"]


def test_database_never_creates_a_missing_file(bare_settings):
    """Seule la migration crée la base : une session sur une base absente échoue sans laisser de fichier."""
    database = Database(bare_settings.database_path)
    with pytest.raises(OperationalError), database.session():
        pass
    database.dispose()
    assert not bare_settings.database_path.exists()
