# SPDX-License-Identifier: EUPL-1.2
"""Révision 0003 : l'index se remplit sur une base déjà garnie, sans toucher au catalogue, et se retire sans perte."""

from __future__ import annotations

import sqlite3

from alembic import command
from sqlalchemy import text

from biblyo.core.db import Database
from biblyo.core.migrate import alembic_config, schema_differences
from biblyo.modules.catalogue import search, service
from biblyo.modules.catalogue.service import EditionInput

from ..factories import make_notice


def _index(settings):
    connection = sqlite3.connect(settings.database_path)
    try:
        columns = ", ".join(search.COLUMNS)
        return connection.execute(f"SELECT rowid, {columns} FROM works_search ORDER BY rowid").fetchall()  # noqa: S608
    finally:
        connection.close()


def _catalogue(settings):
    connection = sqlite3.connect(settings.database_path)
    try:
        return (
            connection.execute("SELECT id, title, version, completeness FROM works").fetchall(),
            connection.execute("SELECT id, work_id, publication_year FROM editions").fetchall(),
            connection.execute("SELECT barcode, status FROM copies").fetchall(),
        )
    finally:
        connection.close()


def _populate(settings):
    database = Database(settings.database_path)
    first = make_notice(
        settings,
        "Le Cœur des œuvres",
        authors=("René Goscinny", "Albert Uderzo"),
        series="Astérix",
        volume="12,5",
        isbn="978-2-07-040850-4",
        publisher="Dargaud",
        collection="Pilote",
        year="1961",
        categories="Bande dessinée",
        genres="Aventure",
        keywords="Gaule",
    )
    make_notice(settings, "Sans rien du tout")
    make_notice(settings, "Hors-série", series="Astérix", volume="HS 2")
    second = service.add_edition(database, first, EditionInput(isbn="979-10-90636-07-1", publisher="Hachette"), None)
    service.add_copies(database, second, 2, "", None)
    # Une base de L1 : ses exemplaires n'avaient pas de cote. La révision 0006 refuse de redescendre sinon.
    with database.session(write=True) as session:
        session.execute(text("UPDATE copies SET call_number = NULL"))
        search.reindex_all(session)
    database.dispose()


def test_the_revision_builds_exactly_what_the_service_builds(settings):
    """La révision porte sa propre copie du calcul : elle doit donner à la lettre le résultat du service."""
    _populate(settings)
    expected = _index(settings)
    catalogue = _catalogue(settings)
    assert len(expected) == 3

    config = alembic_config(settings)
    command.downgrade(config, "0002")
    connection = sqlite3.connect(settings.database_path)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    connection.close()
    assert not any(name.startswith("works_search") for name in tables)
    assert _catalogue(settings) == catalogue

    command.upgrade(config, "head")
    assert _index(settings) == expected
    assert _catalogue(settings) == catalogue
    assert schema_differences(settings) == []
    database = Database(settings.database_path)
    with database.session() as session:
        assert search.inconsistencies(session) == []
        assert set(search.match_ids(session, "coeur")) == {expected[0][0]}
    database.dispose()
