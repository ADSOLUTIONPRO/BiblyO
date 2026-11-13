# SPDX-License-Identifier: EUPL-1.2
"""UX-Circulation 2.0 : aucune requête par ligne. Le nombre de requêtes d'une page ne dépend pas de sa longueur."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import event

from biblyo.core.db import Database
from biblyo.modules.catalogue import listing
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.catalogue.listing import Criteria
from biblyo.modules.circulation import counter, rules, service
from biblyo.modules.circulation import repository as repo
from biblyo.modules.circulation.service import Actor, LendItem, LendRequest
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.repository import EVERYTHING
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice

RIGHTS = frozenset({rules.LEND, rules.RENEW})


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _fill(settings, database, books: int, readers: int) -> None:
    connection = sqlite3.connect(settings.database_path)
    people = []
    for index in range(readers):
        person = PersonInput(first_name=f"P{index:02d}", last_name="Compte", category="adult", email=f"c{index}@ex.org")
        people.append(patrons.register(database, Registration(person=person, homonyms_confirmed=True), None))
    for index in range(books):
        work = make_notice(settings, f"Livre {index:02d}")
        edition = connection.execute("SELECT id FROM editions WHERE work_id = ?", (work,)).fetchone()[0]
        ids = catalogue.add_copies(database, edition, 2, "", None)
        connection.executemany("UPDATE copies SET status = 'available' WHERE id = ?", [(item,) for item in ids])
        connection.commit()
        request = LendRequest(people[index % len(people)], (LendItem(ids[0]),), rules.MODE_SELF)
        service.lend(database, request, Actor(None, RIGHTS))
    connection.close()


def _count(database, operation) -> int:
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    with database.session() as session:
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            operation(session)
        finally:
            event.remove(engine, "before_cursor_execute", record)
    return len(statements)


OPERATIONS = {
    "grille du catalogue": lambda s: listing.run(s, Criteria()),
    "lecteurs du comptoir": lambda s: counter.reader_page(s, "", 1, personal=False, visibility=EVERYTHING),
    "livres du comptoir": lambda s: counter.book_page(s, "", 1, []),
    "livres du comptoir, recherche": lambda s: counter.book_page(s, "Livre", 1, []),
    "liste des prêts": lambda s: repo.loan_list(s, "cours", service.today(), EVERYTHING, page=1, size=50),
    "liste des prêts, recherche": lambda s: repo.loan_list(
        s, "cours", service.today(), EVERYTHING, page=1, size=50, query="Livre", by_reader=True
    ),
}


@pytest.mark.parametrize("name", list(OPERATIONS))
def test_the_number_of_queries_does_not_grow_with_the_rows(tmp_path_factory, template_database, name):
    from biblyo.config import Settings

    counts = []
    for books, readers in ((2, 2), (12, 12)):
        root = tmp_path_factory.mktemp(f"n{books}")
        (root / "data").mkdir()
        (root / "backups").mkdir()
        settings = Settings(env="test", data_dir=root / "data", backup_dir=root / "backups")
        settings.database_path.write_bytes(template_database.read_bytes())
        database = Database(settings.database_path)
        try:
            _fill(settings, database, books, readers)
            counts.append(_count(database, OPERATIONS[name]))
        finally:
            database.dispose()
    assert counts[0] == counts[1], f"{name} : {counts[0]} requêtes pour 2 lignes, {counts[1]} pour 12"
