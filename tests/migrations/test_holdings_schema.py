# SPDX-License-Identifier: EUPL-1.2
"""Révision 0006 : emplacements et exemplaires complets, sur une base L1 déjà remplie, sans perte."""

from __future__ import annotations

import sqlite3

import pytest
import sqlalchemy as sa
from alembic import command

from biblyo.core import migrate
from biblyo.core.migrate import alembic_config, schema_differences
from biblyo.modules.catalogue.models import Copy

MOMENT = "2026-11-08T00:00:00Z"


def _connect(settings) -> sqlite3.Connection:
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _l1_catalogue(connection: sqlite3.Connection) -> None:
    """Une œuvre, une édition, deux exemplaires, tels que L1 les crée, avant la révision 0006."""
    connection.execute(
        "INSERT INTO works (id, public_id, document_type_id, title, title_sort, title_norm, status, completeness,"
        " version, created_at, updated_at)"
        " VALUES (1, 'w1', 1, 'Le Lotus bleu', 'lotus bleu', 'le lotus bleu', 'active', 0, 1, ?, ?)",
        (MOMENT, MOMENT),
    )
    connection.execute(
        "INSERT INTO editions (id, public_id, work_id, is_primary, language, status, origin, version, created_at,"
        " updated_at) VALUES (1, 'e1', 1, 1, 'fre', 'active', 'manual', 1, ?, ?)",
        (MOMENT, MOMENT),
    )
    for number in (1, 2):
        connection.execute(
            "INSERT INTO copies (public_id, edition_id, barcode, status, created_at, updated_at)"
            " VALUES (?, 1, ?, 'available', ?, ?)",
            (f"c{number}", f"EX-00000{number}", MOMENT, MOMENT),
        )
    connection.commit()


def _location(connection, name="Salle", kind="room", parent=None, depth=0) -> int:
    cursor = connection.execute(
        "INSERT INTO locations (public_id, parent_id, kind, name, path, depth, sort_order, grid_width, grid_height,"
        " is_active, version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, 1, 1, 1, 1, ?, ?)",
        (f"l-{name}", parent, kind, name, name, depth, MOMENT, MOMENT),
    )
    return cursor.lastrowid


@pytest.fixture
def at_0005(bare_settings):
    command.upgrade(alembic_config(bare_settings), "0005")
    connection = _connect(bare_settings)
    _l1_catalogue(connection)
    connection.close()
    return bare_settings


def test_existing_copies_are_kept_and_numbered(at_0005):
    command.upgrade(alembic_config(at_0005), "0006")
    connection = _connect(at_0005)
    rows = connection.execute(
        "SELECT public_id, barcode, inventory_number, status, location_id, shelf_position, is_loanable, version"
        " FROM copies ORDER BY id"
    ).fetchall()
    connection.close()
    assert rows == [
        ("c1", "EX-000001", "EX-000001", "available", None, None, 1, 1),
        ("c2", "EX-000002", "EX-000002", "available", None, None, 1, 1),
    ]
    command.upgrade(alembic_config(at_0005), "head")  # le modèle décrit la tête : on y monte avant de comparer
    assert schema_differences(at_0005) == []


def test_constraints_hold(settings):
    connection = _connect(settings)
    _l1_catalogue_head(connection)
    room = _location(connection)
    furniture = _location(connection, "Meuble", "furniture", room, 1)
    shelf = _location(connection, "Étagère 1", "shelf", furniture, 2)
    connection.execute("UPDATE copies SET location_id = ?, shelf_position = 1 WHERE public_id = 'c1'", (shelf,))
    failing = [
        "UPDATE copies SET shelf_position = 1 WHERE public_id = 'c2'",  # position sans emplacement
        f"UPDATE copies SET location_id = {shelf}, shelf_position = 1 WHERE public_id = 'c2'",  # noqa: S608 position prise
        f"UPDATE copies SET location_id = {shelf}, shelf_position = 0 WHERE public_id = 'c2'",  # noqa: S608
        "UPDATE copies SET location_id = 999 WHERE public_id = 'c2'",
        "UPDATE copies SET inventory_number = 'EX-000001' WHERE public_id = 'c2'",
        "UPDATE copies SET condition = 'mauvais' WHERE public_id = 'c2'",
        "UPDATE copies SET price_cents = -1 WHERE public_id = 'c2'",
        f"DELETE FROM locations WHERE id = {shelf}",  # noqa: S608 exemplaire rangé : suppression refusée
        f"DELETE FROM locations WHERE id = {room}",  # noqa: S608 enfants : suppression refusée
        "UPDATE locations SET kind = 'armoire' WHERE name = 'Salle'",
        "UPDATE locations SET depth = 4 WHERE name = 'Salle'",
        "UPDATE locations SET capacity = -1 WHERE name = 'Salle'",
        "UPDATE locations SET grid_row = 0, grid_col = 1 WHERE name = 'Meuble'",
    ]
    for statement in failing:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
    with pytest.raises(sqlite3.IntegrityError):
        _location(connection, "Étagère 1", "shelf", furniture, 2)  # même nom sous le même parent
    with pytest.raises(sqlite3.IntegrityError):
        _location(connection, "Salle")  # même nom à la racine
    connection.close()


def _l1_catalogue_head(connection):
    connection.execute(
        "INSERT INTO works (id, public_id, document_type_id, title, title_sort, title_norm, status, completeness,"
        " version, created_at, updated_at)"
        " VALUES (1, 'w1', 1, 'Le Lotus bleu', 'lotus bleu', 'le lotus bleu', 'active', 0, 1, ?, ?)",
        (MOMENT, MOMENT),
    )
    connection.execute(
        "INSERT INTO editions (id, public_id, work_id, is_primary, language, status, origin, version, created_at,"
        " updated_at) VALUES (1, 'e1', 1, 1, 'fre', 'active', 'manual', 1, ?, ?)",
        (MOMENT, MOMENT),
    )
    for number in (1, 2):
        connection.execute(
            "INSERT INTO copies (public_id, edition_id, barcode, inventory_number, status, created_at, updated_at)"
            " VALUES (?, 1, ?, ?, 'available', ?, ?)",
            (f"c{number}", f"EX-00000{number}", f"EX-00000{number}", MOMENT, MOMENT),
        )


def test_downgrade_is_refused_once_l2_data_exists(at_0005):
    config = alembic_config(at_0005)
    command.upgrade(config, "0006")
    connection = _connect(at_0005)
    connection.execute("UPDATE copies SET call_number = 'BD LOT 1' WHERE public_id = 'c1'")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="données de L2"):
        command.downgrade(config, "0005")
    connection = _connect(at_0005)
    assert connection.execute("SELECT call_number FROM copies WHERE public_id = 'c1'").fetchone() == ("BD LOT 1",)
    connection.close()


def test_downgrade_is_refused_once_a_location_exists(at_0005):
    config = alembic_config(at_0005)
    command.upgrade(config, "0006")
    connection = _connect(at_0005)
    _location(connection)
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="1 emplacements"):
        command.downgrade(config, "0005")


def test_downgrade_without_l2_data_rebuilds_copies_with_their_rows(at_0005):
    config = alembic_config(at_0005)
    connection = _connect(at_0005)
    before = connection.execute("SELECT * FROM copies ORDER BY id").fetchall()
    connection.close()
    command.upgrade(config, "0006")
    command.downgrade(config, "0005")
    connection = _connect(at_0005)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "locations" not in tables and "retired_codes" not in tables
    assert connection.execute("SELECT * FROM copies ORDER BY id").fetchall() == before
    assert connection.execute("SELECT strict FROM pragma_table_list WHERE name = 'copies'").fetchone() == (1,)
    connection.close()
    command.upgrade(config, "head")
    assert schema_differences(at_0005) == []


@pytest.mark.parametrize(("action", "kept"), [("RESTRICT", 0), ("CASCADE", 2)])
def test_inline_foreign_keys_are_checked_not_ignored(action, kept):
    """La clé `location_id` n'est relue ni par son nom ni par son action : PRAGMA doit confirmer RESTRICT."""
    engine = sa.create_engine("sqlite://")
    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE locations (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE copies (id INTEGER PRIMARY KEY,"
            f" location_id INTEGER REFERENCES locations (id) ON DELETE {action})"
        )
        reflected = sa.MetaData()
        reflected.reflect(connection)
        found = next(iter(reflected.tables["copies"].foreign_keys)).constraint
        model = next(key.constraint for key in Copy.__table__.foreign_keys if key.parent.name == "location_id")
        differences = [("remove_fk", found), ("add_fk", model)]
        assert len(migrate._without_inline_foreign_keys(connection, differences)) == kept
