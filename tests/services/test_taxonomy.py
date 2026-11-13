# SPDX-License-Identifier: EUPL-1.2
"""Listes du catalogue : fusions sans perte d'association, index à jour, journal, boucles de sections."""

from __future__ import annotations

import json
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.modules.catalogue import search, taxonomy
from biblyo.modules.catalogue.service import EditionInput, update_edition

from ..factories import make_notice

KINDS = taxonomy.KINDS


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _tag_id(settings, label, kind="category"):
    return _rows(settings, "SELECT id FROM tags WHERE label = ? AND kind = ?", label, kind)[0][0]


def _find(database, query):
    with database.session() as session:
        return search.match_ids(session, query, limit=50)


def _consistent(database):
    with database.session() as session:
        assert search.inconsistencies(session) == []


def _audit(settings, action):
    return _rows(settings, "SELECT entity_type, entity_id, details FROM audit_log WHERE action = ?", action)


def test_merging_tags_keeps_every_association(settings, database):
    first = make_notice(settings, "Un", categories="BD")
    second = make_notice(settings, "Deux", categories="Bande dessinée")
    source = _tag_id(settings, "BD")
    target = _tag_id(settings, "Bande dessinée")
    taxonomy.merge(database, KINDS["categories"], source, target, None)
    assert sorted(_rows(settings, "SELECT work_id FROM work_tags WHERE tag_id = ?", target)) == sorted(
        [(first,), (second,)]
    )
    assert _rows(settings, "SELECT COUNT(*) FROM tags WHERE id = ?", source) == [(0,)]


def test_merge_avoids_a_duplicate_association(settings, database):
    both = make_notice(settings, "Les deux", categories="BD, Bande dessinée")
    source = _tag_id(settings, "BD")
    target = _tag_id(settings, "Bande dessinée")
    taxonomy.merge(database, KINDS["categories"], source, target, None)
    assert _rows(settings, "SELECT work_id, tag_id FROM work_tags") == [(both, target)]


def test_merge_updates_the_search_index(settings, database):
    work = make_notice(settings, "Un", categories="Polar")
    make_notice(settings, "Deux", categories="Roman policier")
    source = _tag_id(settings, "Polar")
    target = _tag_id(settings, "Roman policier")
    assert _find(database, "polar") == [work]
    taxonomy.merge(database, KINDS["categories"], source, target, None)
    assert _find(database, "polar") == []
    assert work in _find(database, "policier")
    _consistent(database)


def test_merge_writes_the_journal(settings, database):
    make_notice(settings, "Un", categories="BD")
    make_notice(settings, "Deux", categories="Bande dessinée")
    source = _tag_id(settings, "BD")
    target = _tag_id(settings, "Bande dessinée")
    taxonomy.merge(database, KINDS["categories"], source, target, None)
    rows = _audit(settings, "taxonomy.merge")
    assert len(rows) == 1
    assert rows[0][0] == "categories" and rows[0][1] == target
    assert json.loads(rows[0][2]) == {"source": source, "associations": 1}


def test_merge_only_within_the_same_kind(settings, database):
    make_notice(settings, "Un", categories="Humour", genres="Humour")
    source = _tag_id(settings, "Humour", "genre")
    target = _tag_id(settings, "Humour", "category")
    with pytest.raises(BusinessError):
        taxonomy.merge(database, KINDS["genres"], source, target, None)


def test_merge_into_itself_is_refused(settings, database):
    make_notice(settings, "Un", categories="BD")
    tag = _tag_id(settings, "BD")
    with pytest.raises(BusinessError):
        taxonomy.merge(database, KINDS["categories"], tag, tag, None)


def _publisher_id(settings, name):
    return _rows(settings, "SELECT id FROM publishers WHERE name = ?", name)[0][0]


def _collection_ids(settings, name):
    return [row[0] for row in _rows(settings, "SELECT id FROM collections WHERE name = ?", name)]


def test_merging_publishers_moves_editions_and_merges_twin_collections(settings, database):
    first = make_notice(settings, "Un", publisher="Dargaud SA", collection="Pilote")
    second = make_notice(settings, "Deux", publisher="Dargaud", collection="Pilote")
    third = make_notice(settings, "Trois", publisher="Dargaud SA", collection="Autre")
    source = _publisher_id(settings, "Dargaud SA")
    target = _publisher_id(settings, "Dargaud")
    taxonomy.merge(database, KINDS["editeurs"], source, target, None)
    assert _rows(settings, "SELECT COUNT(*) FROM editions WHERE publisher_id = ?", target) == [(3,)]
    assert _rows(settings, "SELECT COUNT(*) FROM publishers") == [(1,)]
    pilote = _collection_ids(settings, "Pilote")
    assert len(pilote) == 1
    assert _rows(settings, "SELECT COUNT(*) FROM editions WHERE collection_id = ?", pilote[0]) == [(2,)]
    assert _rows(settings, "SELECT publisher_id FROM collections WHERE name = 'Autre'") == [(target,)]
    assert {first, second, third} <= set(_find(database, "dargaud"))
    _consistent(database)


def test_merging_collections(settings, database):
    work = make_notice(settings, "Un", publisher="Gallimard", collection="Folio Poche")
    make_notice(settings, "Deux", publisher="Gallimard", collection="Folio")
    source = _collection_ids(settings, "Folio Poche")[0]
    target = _collection_ids(settings, "Folio")[0]
    taxonomy.merge(database, KINDS["collections"], source, target, None)
    assert _find(database, "poche") == []
    assert work in _find(database, "folio")
    _consistent(database)


def _section(database, label, parent=None):
    return taxonomy.create(database, KINDS["sections"], label, None, parent)


def test_create_and_rename_section(settings, database):
    section = _section(database, "Jeunesse")
    assert _rows(settings, "SELECT code, label FROM sections WHERE id = ?", section) == [("jeunesse", "Jeunesse")]
    taxonomy.rename(database, KINDS["sections"], section, "Enfance", None)
    assert _rows(settings, "SELECT code, label FROM sections WHERE id = ?", section) == [("jeunesse", "Enfance")]


def test_merging_sections_moves_works_and_children(settings, database):
    source = _section(database, "Ados")
    target = _section(database, "Jeunesse")
    child = _section(database, "Mangas ados", source)
    work = make_notice(settings, "Un", section_id=source)
    taxonomy.merge(database, KINDS["sections"], source, target, None)
    assert _rows(settings, "SELECT section_id FROM works WHERE id = ?", work) == [(target,)]
    assert _rows(settings, "SELECT parent_id FROM sections WHERE id = ?", child) == [(target,)]


def test_section_loops_are_refused(settings, database):
    top = _section(database, "A")
    middle = _section(database, "B", top)
    bottom = _section(database, "C", middle)
    with pytest.raises(BusinessError, match="sous-sections"):
        taxonomy.set_parent(database, top, bottom, None)
    with pytest.raises(BusinessError, match="sous-sections"):
        taxonomy.set_parent(database, top, top, None)
    with pytest.raises(BusinessError, match="sous-sections"):
        taxonomy.merge(database, KINDS["sections"], top, bottom, None)
    taxonomy.set_parent(database, bottom, None, None)
    assert _rows(settings, "SELECT parent_id FROM sections WHERE id = ?", bottom) == [(None,)]


def test_merging_types_moves_works(settings, database):
    work = make_notice(settings, "Un", document_type="comics")
    source = _rows(settings, "SELECT id FROM document_types WHERE code = 'comics'")[0][0]
    target = _rows(settings, "SELECT id FROM document_types WHERE code = 'bd'")[0][0]
    taxonomy.merge(database, KINDS["types"], source, target, None)
    assert _rows(settings, "SELECT document_type_id FROM works WHERE id = ?", work) == [(target,)]
    assert _rows(settings, "SELECT COUNT(*) FROM document_types WHERE code = 'comics'") == [(0,)]


def test_merging_into_a_disabled_type_is_refused(settings, database):
    source = _rows(settings, "SELECT id FROM document_types WHERE code = 'comics'")[0][0]
    target = _rows(settings, "SELECT id FROM document_types WHERE code = 'bd'")[0][0]
    taxonomy.set_active(database, KINDS["types"], target, False, None)
    with pytest.raises(BusinessError, match="actif"):
        taxonomy.merge(database, KINDS["types"], source, target, None)


def test_the_last_active_type_cannot_be_disabled(settings, database):
    ids = [row[0] for row in _rows(settings, "SELECT id FROM document_types")]
    for type_id in ids[:-1]:
        taxonomy.set_active(database, KINDS["types"], type_id, False, None)
    with pytest.raises(BusinessError, match="au moins un"):
        taxonomy.set_active(database, KINDS["types"], ids[-1], False, None)


def test_only_types_and_sections_can_be_disabled(settings, database):
    make_notice(settings, "Un", categories="BD")
    with pytest.raises(BusinessError):
        taxonomy.set_active(database, KINDS["categories"], _tag_id(settings, "BD"), False, None)


def test_tags_cannot_be_created_here(settings, database):
    with pytest.raises(BusinessError):
        taxonomy.create(database, KINDS["categories"], "Nouvelle", None)


def test_renaming_updates_the_index(settings, database):
    work = make_notice(settings, "Un", categories="Roman", publisher="Dargaud", collection="Pilote")
    taxonomy.rename(database, KINDS["categories"], _tag_id(settings, "Roman"), "Fiction", None)
    assert _find(database, "fiction") == [work] and _find(database, "roman") == []
    taxonomy.rename(database, KINDS["editeurs"], _publisher_id(settings, "Dargaud"), "Darg Éditions", None)
    assert _find(database, "darg editions") == [work]
    taxonomy.rename(database, KINDS["collections"], _collection_ids(settings, "Pilote")[0], "Copilote", None)
    assert _find(database, "copilote") == [work]
    _consistent(database)


def test_renaming_to_an_existing_label_asks_for_a_merge(settings, database):
    make_notice(settings, "Un", categories="BD, Roman")
    with pytest.raises(BusinessError, match="fusionnez"):
        taxonomy.rename(database, KINDS["categories"], _tag_id(settings, "BD"), "roman", None)


@pytest.mark.parametrize("label", ["", "   ", "***", "x" * 300])
def test_invalid_labels_are_refused(settings, database, label):
    make_notice(settings, "Un", categories="BD")
    with pytest.raises(BusinessError):
        taxonomy.rename(database, KINDS["categories"], _tag_id(settings, "BD"), label, None)


def test_a_failed_merge_changes_nothing(settings, database):
    work = make_notice(settings, "Un", categories="BD")
    tag = _tag_id(settings, "BD")
    with pytest.raises(BusinessError):
        taxonomy.merge(database, KINDS["categories"], tag, 99999, None)
    assert _rows(settings, "SELECT work_id FROM work_tags WHERE tag_id = ?", tag) == [(work,)]


def test_row_counts(settings, database):
    work = make_notice(settings, "Un", publisher="Dargaud", categories="BD")
    edition = _rows(settings, "SELECT id FROM editions WHERE work_id = ?", work)[0][0]
    update_edition(database, edition, EditionInput(publisher="Dargaud"), None, None)
    with database.session() as session:
        publishers = taxonomy.rows(session, KINDS["editeurs"])
        categories = taxonomy.rows(session, KINDS["categories"])
    assert [(row.label, row.count) for row in publishers] == [("Dargaud", 1)]
    assert [(row.label, row.count) for row in categories] == [("BD", 1)]
