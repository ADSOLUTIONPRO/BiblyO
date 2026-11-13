# SPDX-License-Identifier: EUPL-1.2
"""Recherche sur une vraie base : accents, ISBN, éditions, codes-barres, index à jour et reconstructible."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.modules.catalogue import search, service
from biblyo.modules.catalogue.service import (
    ContentInput,
    ContributorInput,
    EditionInput,
    IdentityInput,
    SeriesEdit,
    SeriesInput,
)

from ..factories import make_notice


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _find(database, query):
    with database.session() as session:
        return search.match_ids(session, query, limit=100)


def _edition_id(settings, work_id):
    connection = sqlite3.connect(settings.database_path)
    try:
        query = "SELECT id FROM editions WHERE work_id = ? AND is_primary = 1"
        return connection.execute(query, (work_id,)).fetchone()[0]
    finally:
        connection.close()


@pytest.mark.parametrize("query", ["coeur", "cœur", "CŒUR", "Coeur des", "oeuvres", "ŒUVRES"])
def test_ligatures_case_and_accents(settings, database, query):
    work = make_notice(settings, "Le Cœur des œuvres", authors=("Jeanne Martin",))
    assert _find(database, query) == [work]


def test_accents_both_ways(settings, database):
    work = make_notice(settings, "Astérix et Cléopâtre")
    plain = make_notice(settings, "Noel a Paris")
    assert _find(database, "asterix cleopatre") == [work]
    assert _find(database, "Noël") == [plain]


def test_prefix_search(settings, database):
    work = make_notice(settings, "Les Bijoux de la Castafiore")
    assert _find(database, "casta") == [work]


def test_every_indexed_field(settings, database):
    work = make_notice(
        settings,
        "Le Lotus bleu",
        authors=("Hergé",),
        series="Tintin",
        volume="5",
        publisher="Casterman",
        collection="Grande Collection",
        year="1936",
        categories="Aventures",
        genres="Espionnage",
        keywords="Chine",
    )
    queries = ("herge", "tintin", "casterman", "grande coll", "1936", "aventures", "espionnage", "chine", "tintin 5")
    for query in queries:
        assert _find(database, query) == [work], query


@pytest.mark.parametrize(
    "query",
    ["978-2-07-040850-4", "9782070408504", "2-07-040850-7", "2070408507", "978 2 07 040850 4", "isbn:9782070408504"],
)
def test_an_isbn10_finds_an_isbn13_and_back(settings, database, query):
    work = make_notice(settings, "Avec ISBN", isbn="978-2-07-040850-4")
    make_notice(settings, "Sans ISBN")
    assert _find(database, query) == [work]


def test_isbn_with_x_check_digit(settings, database):
    work = make_notice(settings, "Clé X", isbn="0-8044-2957-X")
    assert _find(database, "0-8044-2957-X") == [work]
    assert _find(database, "080442957x") == [work]


def test_isbn_of_the_second_edition(settings, database):
    work = make_notice(settings, "Deux éditions", isbn="978-2-07-040850-4")
    service.add_edition(database, work, EditionInput(isbn="979-10-90636-07-1", publisher="Hachette", year="2019"), None)
    assert _find(database, "979-10-90636-07-1") == [work]
    assert _find(database, "hachette") == [work]
    assert _find(database, "2019") == [work]


def test_barcode_of_any_copy(settings, database):
    work = make_notice(settings, "Avec exemplaires")
    edition = _edition_id(settings, work)
    service.add_copies(database, edition, 2, "", None)
    for query in ("EX-000001", "ex-000002", "ex000002", "code:EX-000001"):
        assert _find(database, query) == [work], query


def test_two_books_with_the_same_title_are_both_found(settings, database):
    first = make_notice(settings, "Le Secret", authors=("Anne Martin",))
    second = make_notice(settings, "Le Secret", authors=("Paul Durand",))
    assert set(_find(database, "secret")) == {first, second}
    assert _find(database, "secret durand") == [second]


def test_field_criteria_narrow_the_search(settings, database):
    by_author = make_notice(settings, "Un roman", authors=("Rousseau",))
    in_title = make_notice(settings, "Rousseau, une vie", authors=("Autre Personne",))
    assert set(_find(database, "rousseau")) == {by_author, in_title}
    assert _find(database, "auteur:rousseau") == [by_author]
    assert _find(database, "titre:rousseau") == [in_title]


def test_title_matches_rank_before_other_fields(settings, database):
    other = make_notice(settings, "Autre chose", keywords="Lotus")
    title = make_notice(settings, "Lotus")
    assert _find(database, "lotus") == [title, other]


def test_index_follows_identity_update(settings, database):
    work = make_notice(settings, "Ancien titre", document_type="livre")
    with database.session() as session:
        type_id = session.scalar(text("SELECT id FROM document_types WHERE code = 'livre'"))
    identity = IdentityInput("Nouveau titre", type_id, contributors=(ContributorInput("Léa Roy"),))
    service.update_identity(database, work, identity, None, None)
    assert _find(database, "ancien") == []
    assert _find(database, "nouveau") == [work]
    assert _find(database, "lea roy") == [work]


def test_index_follows_every_other_write(settings, database):
    work = make_notice(settings, "Une notice")
    edition = _edition_id(settings, work)
    service.update_series(database, work, SeriesInput("Les Deux Rives", "3"), None, None)
    assert _find(database, "rives") == [work]
    service.update_content(database, work, ContentInput(categories="Roman", genres="", keywords="Mer"), None, None)
    assert _find(database, "mer") == [work]
    service.update_edition(database, edition, EditionInput(publisher="Seuil", isbn="9782070408504"), None, None)
    assert _find(database, "seuil") == [work]
    assert _find(database, "9782070408504") == [work]
    service.update_edition(database, edition, EditionInput(publisher="Autre"), None, None)
    assert _find(database, "seuil") == []
    assert _find(database, "9782070408504") == []


def test_series_rename_reaches_the_index(settings, database):
    work = make_notice(settings, "Tome un", series="Vieux nom", volume="1")
    with database.session() as session:
        series_id = session.scalar(text("SELECT id FROM series"))
    service.update_series_record(database, series_id, SeriesEdit("Nom neuf"), None)
    assert _find(database, "vieux") == []
    assert _find(database, "neuf") == [work]


def test_archived_work_stays_indexed_and_deleted_work_leaves(settings, database):
    work = make_notice(settings, "À archiver")
    service.archive_work(database, work, None)
    assert _find(database, "archiver") == [work]  # le filtre d'état s'applique à la liste, pas à l'index
    service.delete_work(database, work, None)
    assert _find(database, "archiver") == []
    with database.session() as session:
        assert search.inconsistencies(session) == []


def test_refused_write_leaves_the_index_unchanged(settings, database):
    work = make_notice(settings, "Avec exemplaire")
    service.add_copies(database, _edition_id(settings, work), 1, "", None)
    with pytest.raises(BusinessError):
        service.delete_work(database, work, None)
    assert _find(database, "exemplaire") == [work]


def test_reindex_gives_the_same_results(settings, database):
    works = [make_notice(settings, f"Livre {i}", authors=("Jeanne Martin",)) for i in range(5)]
    before = _find(database, "martin")
    with database.session(write=True) as session:
        session.execute(text("DELETE FROM works_search"))
    assert _find(database, "martin") == []
    with database.session(write=True) as session:
        assert search.reindex_all(session) == len(works)
    assert _find(database, "martin") == before
    with database.session() as session:
        assert search.inconsistencies(session) == []


def test_inconsistencies_are_named(settings, database):
    work = make_notice(settings, "Présent")
    other = make_notice(settings, "Modifié")
    with database.session() as session:
        assert search.inconsistencies(session) == []
    with database.session(write=True) as session:
        session.execute(text("DELETE FROM works_search WHERE rowid = :id"), {"id": work})
        session.execute(text("UPDATE works_search SET title = 'autre' WHERE rowid = :id"), {"id": other})
        session.execute(text("INSERT INTO works_search(rowid, title) VALUES (9999, 'orphelin')"))
    with database.session() as session:
        problems = search.inconsistencies(session)
    assert len(problems) == 3
    assert any("absente" in p and str(work) in p for p in problems)
    assert any("orpheline" in p and "9999" in p for p in problems)
    assert any("réindexer" in p and str(other) in p for p in problems)


def test_db_check_reports_a_drifted_index_and_reindex_repairs_it(settings, database, run_cli):
    make_notice(settings, "Dérive")
    with database.session(write=True) as session:
        session.execute(text("DELETE FROM works_search"))
    result = run_cli(settings, "biblyo db check")
    assert result.exit_code == 1
    assert "index de recherche" in result.output
    result = run_cli(settings, "biblyo search reindex")
    assert result.exit_code == 0, result.output
    assert "1 œuvre" in result.output
    assert run_cli(settings, "biblyo db check").exit_code == 0
