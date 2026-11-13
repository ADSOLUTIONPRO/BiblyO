# SPDX-License-Identifier: EUPL-1.2
"""Liste du catalogue : filtres cumulés, tri stable, pagination aux bornes, critères relus depuis l'adresse."""

from __future__ import annotations

import sqlite3
from urllib.parse import parse_qsl

import pytest

from biblyo.core.db import Database
from biblyo.modules.catalogue import listing, service
from biblyo.modules.catalogue.listing import Criteria
from biblyo.modules.catalogue.service import EditionInput

from ..factories import make_notice


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _run(database, **values):
    with database.session() as session:
        return listing.run(session, Criteria.from_params(values))


def _ids(database, **values):
    return [card.id for card in _run(database, **values).cards]


def _edition(settings, work):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute("SELECT id FROM editions WHERE work_id = ?", (work,)).fetchone()[0]
    finally:
        connection.close()


def _section(settings, code="jeunesse", label="Jeunesse"):
    connection = sqlite3.connect(settings.database_path)
    try:
        cursor = connection.execute(
            "INSERT INTO sections (code, label, sort_order, is_active) VALUES (?, ?, 0, 1)", (code, label)
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def test_default_list_is_active_works_by_sort_title(settings, database):
    second = make_notice(settings, "Zorro")
    first = make_notice(settings, "L'Agent 212")
    archived = make_notice(settings, "Archivée")
    service.archive_work(database, archived, None)
    assert _ids(database) == [first, second]


def test_status_filter(settings, database):
    active = make_notice(settings, "Active")
    archived = make_notice(settings, "Archivée")
    service.archive_work(database, archived, None)
    assert _ids(database, statut="archivees") == [archived]
    assert set(_ids(database, statut="toutes")) == {active, archived}


def test_filters_add_up(settings, database):
    section = _section(settings)
    match = make_notice(
        settings,
        "Bonne",
        document_type="bd",
        section_id=section,
        categories="Aventure",
        genres="Humour",
        keywords="Gaule",
        audience="children",
        publisher="Dargaud",
        collection="Pilote",
        series="Astérix",
        volume="1",
        authors=("René Goscinny",),
        year="1961",
        language="fre",
    )
    make_notice(settings, "Autre type", document_type="livre", categories="Aventure", publisher="Dargaud")
    make_notice(settings, "Autre catégorie", document_type="bd", categories="Roman", publisher="Dargaud")
    make_notice(settings, "Autre éditeur", document_type="bd", categories="Aventure", publisher="Seuil")
    assert _ids(database, type="bd", cat="Aventure", editeur="dargaud") == [match]
    for values in (
        {"type": "bd"},
        {"section": "jeunesse"},
        {"cat": "aventure"},
        {"genre": "HUMOUR"},
        {"mot": "gaule"},
        {"public": "children"},
        {"editeur": "darg"},
        {"collection": "pilote"},
        {"serie": "asterix"},
        {"contributeur": "goscinny"},
        {"annee_min": "1960", "annee_max": "1962"},
        {"langue": "fre", "cat": "aventure", "type": "bd", "editeur": "dargaud"},
    ):
        assert match in _ids(database, **values), values
    assert _ids(database, q="bonne", type="livre") == []
    assert _ids(database, q="bonne", type="bd") == [match]


def test_language_and_years_look_at_every_edition(settings, database):
    work = make_notice(settings, "Deux parutions", language="fre", year="1960")
    service.add_edition(database, work, EditionInput(language="eng", year="2005"), None)
    make_notice(settings, "Une seule", language="fre", year="1960")
    assert _ids(database, langue="eng") == [work]
    assert _ids(database, annee_min="2000") == [work]


def test_both_year_bounds_apply_to_the_same_edition(settings, database):
    work = make_notice(settings, "Deux parutions", year="1950")
    service.add_edition(database, work, EditionInput(year="2010"), None)
    assert _ids(database, annee_min="1960", annee_max="2000") == []


def test_contributor_on_edition_only_is_found(settings, database):
    work = make_notice(settings, "Traduit")
    service.add_edition(
        database, work, EditionInput(contributors=(service.ContributorInput("Lena Traductrice", "translator"),)), None
    )
    assert _ids(database, contributeur="lena") == [work]


def test_homonyms_are_separated_by_identifier(settings, database):
    first = make_notice(settings, "Premier", authors=("Jean Dupont",))
    connection = sqlite3.connect(settings.database_path)
    agent = connection.execute("SELECT id FROM agents").fetchone()[0]
    connection.close()
    identity = service.IdentityInput(
        "Second",
        1,
        contributors=(service.ContributorInput("Jean Dupont", choice="new"),),
    )
    second = service.create_notice(
        database, identity, service.SeriesInput(), service.ContentInput(), EditionInput(), None
    )
    assert set(_ids(database, contributeur="jean dupont")) == {first, second}
    assert _ids(database, contributeur=str(agent)) == [first]


def test_copies_and_metadata_filters(settings, database):
    with_all = make_notice(settings, "Complète", isbn="9782070408504", summary="Un résumé", authors=("A B",))
    bare = make_notice(settings, "Nue")
    service.add_copies(database, _edition(settings, with_all), 1, "", None)
    assert _ids(database, exemplaires="avec") == [with_all]
    assert _ids(database, exemplaires="sans") == [bare]
    assert _ids(database, meta="sans-isbn") == [bare]
    assert _ids(database, meta="sans-resume") == [bare]
    assert _ids(database, meta="sans-auteur") == [bare]
    assert set(_ids(database, meta="sans-couverture")) == {with_all, bare}
    assert bare in _ids(database, meta="incomplet")


def test_sorts_are_total_and_stable(settings, database):
    same = [make_notice(settings, "Même titre") for _ in range(3)]
    assert _ids(database) == sorted(same)
    assert _ids(database, tri="recents") == sorted(same, reverse=True)


def test_series_sort_orders_volumes_and_keeps_others_last(settings, database):
    alone = make_notice(settings, "Sans série")
    third = make_notice(settings, "T3", series="Astérix", volume="3")
    half = make_notice(settings, "T1,5", series="Astérix", volume="1,5")
    first = make_notice(settings, "T1", series="Astérix", volume="1")
    assert _ids(database, tri="serie") == [first, half, third, alone]


def test_year_sort(settings, database):
    old = make_notice(settings, "Ancien", year="1950")
    recent = make_notice(settings, "Récent", year="2020")
    none = make_notice(settings, "Sans année")
    assert _ids(database, tri="annee") == [recent, old, none]


def test_a_search_is_sorted_by_relevance_unless_asked(settings, database):
    keyword = make_notice(settings, "Autre", keywords="Lotus")
    title = make_notice(settings, "Lotus")
    assert _ids(database, q="lotus") == [title, keyword]
    assert _run(database, q="lotus").sort == "pertinence"
    assert _ids(database, q="lotus", tri="titre") == [keyword, title]


def test_pagination_bounds(settings, database):
    ids = [make_notice(settings, f"Titre {i:03d}") for i in range(5)]
    with database.session() as session:
        first = listing.run(session, Criteria(), page_size=2)
        last = listing.run(session, Criteria(page=3), page_size=2)
        beyond = listing.run(session, Criteria(page=99), page_size=2)
    assert first.total == 5 and first.pages == 3
    assert [c.id for c in first.cards] == ids[:2]
    assert [c.id for c in last.cards] == ids[4:]
    assert beyond.page == 3 and [c.id for c in beyond.cards] == ids[4:]


def test_pages_neither_repeat_nor_skip(settings, database):
    for index in range(7):
        make_notice(settings, f"Livre {index % 2}")
    seen = []
    with database.session() as session:
        for page in (1, 2, 3, 4):
            seen += [card.id for card in listing.run(session, Criteria(page=page), page_size=2).cards]
    assert len(seen) == 7 and len(set(seen)) == 7


def test_total_and_copies_follow_the_filters(settings, database):
    work = make_notice(settings, "Avec", categories="Roman")
    make_notice(settings, "Sans", categories="Essai")
    service.add_copies(database, _edition(settings, work), 3, "", None)
    result = _run(database, cat="roman")
    assert (result.total, result.copies) == (1, 3)


@pytest.mark.parametrize(
    "bad",
    [
        {"view": "cube"},
        {"tri": "'; DROP TABLE works"},
        {"tri": "pertinence"},
        {"page": "abc"},
        {"page": "-3"},
        {"page": "99999999999"},
        {"annee_min": "abcd"},
        {"meta": "nope"},
        {"exemplaires": "nope"},
        {"statut": "nope"},
        {"langue": "klingon"},
        {"public": "nope"},
        {"q": '"'},
        {"q": "NEAR(("},
        {"type": "' OR 1=1"},
    ],
)
def test_malformed_criteria_are_ignored_without_error(settings, database, bad):
    work = make_notice(settings, "Une notice")
    result = _run(database, **bad)
    if "type" in bad or ("q" in bad and bad["q"] == "NEAR(("):
        assert result.total == 0
    else:
        assert [card.id for card in result.cards] == [work]


def test_address_round_trip():
    criteria = Criteria.from_params(
        {
            "q": "Cœur",
            "view": "list",
            "tri": "recents",
            "page": "3",
            "type": "bd",
            "cat": "Bande dessinée",
            "annee_min": "1990",
        }
    )
    assert Criteria.from_params(dict(parse_qsl(criteria.query_string()))) == criteria


def test_default_values_are_left_out_of_the_address():
    assert Criteria().url() == "/catalogue"
    assert Criteria(view="list").url() == "/catalogue?view=list"
    assert Criteria(page=1, q="a").url() == "/catalogue?q=a"


def test_saved_criteria_never_store_the_page():
    assert "page" not in Criteria(page=4, q="a").as_dict()


def test_like_wildcards_are_literal(settings, database):
    make_notice(settings, "Un livre", publisher="Dargaud")
    assert _ids(database, editeur="%") == []
    assert _ids(database, editeur="d_rgaud") == []
