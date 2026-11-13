# SPDX-License-Identifier: EUPL-1.2
"""Règles du catalogue sur une vraie base migrée : œuvres, éditions, exemplaires, séries, conflits."""

from __future__ import annotations

import json
import sqlite3

import pytest

from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.modules.catalogue import repository as repo
from biblyo.modules.catalogue import service
from biblyo.modules.catalogue.service import (
    ConflictError,
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


def _notice(database, work_id):
    with database.session() as session:
        return repo.load_notice(session, work_id)


def _type_id(database, code="bd"):
    with database.session() as session:
        return next(t.id for t in repo.document_types(session) if t.code == code)


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


# --- Cas exigés par l'utilisateur -------------------------------------------------------------------------------------
def test_two_different_editions_of_the_same_work(settings, database):
    work = make_notice(settings, "Le Lotus bleu", isbn="978-2-07-040850-4", publisher="Casterman", year="1946")
    second = service.add_edition(
        database, work, EditionInput(isbn="0-8044-2957-X", publisher="Gallimard", year="2019"), None
    )
    notice = _notice(database, work)
    assert len(notice.editions) == 2
    assert notice.primary.publisher.label == "Casterman"
    assert notice.primary.year == 1946
    other = next(edition for edition in notice.editions if edition.id == second)
    assert other.publisher.label == "Gallimard"
    assert other.year == 2019
    assert other.isbn13 == ("9780804429573",)
    assert not other.is_primary


def test_two_copies_of_the_same_edition(settings, database):
    work = make_notice(settings)
    edition = _notice(database, work).primary.id
    created = service.add_copies(database, edition, 2, "", None)
    notice = _notice(database, work)
    assert len(created) == 2
    assert len(notice.primary.copies) == 2
    assert [copy.barcode for copy in notice.primary.copies] == ["EX-000001", "EX-000002"]
    assert notice.primary.active_copies == 2


def test_a_book_without_isbn(settings, database):
    work = make_notice(settings, "Cahier sans identifiant")
    notice = _notice(database, work)
    assert notice.primary.identifiers == ()
    assert "ISBN" in notice.missing


def test_two_works_with_the_same_title_stay_distinct(settings, database):
    first = make_notice(settings, "Le Secret", authors=("Anne Martin",))
    second = make_notice(settings, "Le Secret", authors=("Paul Durand",))
    assert first != second
    assert _notice(database, first).authors_text == "Anne Martin"
    assert _notice(database, second).authors_text == "Paul Durand"


def test_concurrent_changes_do_not_overwrite_each_other(settings, database):
    work = make_notice(settings, "Titre d’origine")
    type_id = _type_id(database)
    seen_by_both = _notice(database, work).version
    service.update_identity(database, work, IdentityInput("Premier", type_id), seen_by_both, None)
    with pytest.raises(ConflictError, match="vient d’être modifiée"):
        service.update_identity(database, work, IdentityInput("Second", type_id), seen_by_both, None)
    assert _notice(database, work).title == "Premier"


def test_concurrent_changes_on_an_edition(settings, database):
    work = make_notice(settings, publisher="Casterman")
    edition = _notice(database, work).primary
    service.update_edition(database, edition.id, EditionInput(publisher="Gallimard"), edition.version, None)
    with pytest.raises(ConflictError):
        service.update_edition(database, edition.id, EditionInput(publisher="Dargaud"), edition.version, None)
    assert _notice(database, work).primary.publisher.label == "Gallimard"


def test_changing_an_edition_does_not_conflict_with_the_work(settings, database):
    """Œuvre et édition ont chacune leur version : deux personnes peuvent y travailler en parallèle."""
    work = make_notice(settings)
    notice = _notice(database, work)
    service.update_edition(database, notice.primary.id, EditionInput(year="2001"), notice.primary.version, None)
    service.update_identity(database, work, IdentityInput("Nouveau", _type_id(database)), notice.version, None)
    assert _notice(database, work).title == "Nouveau"


# --- ISBN -------------------------------------------------------------------------------------------------------------
def test_invalid_isbn_is_refused_with_a_french_message(settings, database):
    with pytest.raises(FormErrors) as caught:
        service.add_edition(database, make_notice(settings), EditionInput(isbn="978-2-07-040850-5"), None)
    assert "isbn" in caught.value.fields
    assert "clé de contrôle" in caught.value.fields["isbn"]


def test_isbn_is_stored_in_both_forms(settings, database):
    notice = _notice(database, make_notice(settings, isbn="2-07-040850-7"))
    assert notice.primary.isbn13 == ("9782070408504",)
    assert notice.primary.isbn10 == ("2070408507",)


def test_an_edition_can_carry_several_isbn(settings, database):
    notice = _notice(database, make_notice(settings, isbn="978-2-07-040850-4 ; 0-8044-2957-X"))
    assert notice.primary.isbn13 == ("9782070408504", "9780804429573")


def test_same_isbn_on_two_works_is_reported_and_never_refused(settings, database):
    first = make_notice(settings, "Premier", isbn="978-2-07-040850-4")
    second = make_notice(settings, "Second", isbn="2-07-040850-7")
    duplicates = _notice(database, second).duplicates
    assert [(d.work_id, d.title) for d in duplicates] == [(first, "Premier")]
    assert _notice(database, first).duplicates[0].work_id == second


# --- Contributeurs ----------------------------------------------------------------------------------------------------
def test_homonyms_stay_distinct(settings, database):
    type_id = _type_id(database)
    first = make_notice(settings, "A", authors=("Jean Dupont",))
    data = IdentityInput("B", type_id, contributors=(ContributorInput("Jean Dupont", "author", "new", "1950-2020"),))
    second = service.create_notice(database, data, SeriesInput(), ContentInput(), EditionInput(), None)
    one = _notice(database, first).contributors[0]
    two = _notice(database, second).contributors[0]
    assert one.agent_id != two.agent_id
    assert two.dates == "1950-2020"


def test_a_name_with_several_homonyms_requires_a_choice(settings, database):
    type_id = _type_id(database)
    first = make_notice(settings, "A", authors=("Jean Dupont",))
    data = IdentityInput("B", type_id, contributors=(ContributorInput("Jean Dupont", "author", "new"),))
    second = service.create_notice(database, data, SeriesInput(), ContentInput(), EditionInput(), None)

    ambiguous = IdentityInput("C", type_id, contributors=(ContributorInput("jean dupont"),))
    with pytest.raises(FormErrors) as caught:
        service.create_notice(database, ambiguous, SeriesInput(), ContentInput(), EditionInput(), None)
    assert "Plusieurs personnes" in caught.value.fields["contributeur_0"]

    chosen = _notice(database, second).contributors[0].agent_id
    data = IdentityInput("C", type_id, contributors=(ContributorInput("Jean Dupont", "author", str(chosen)),))
    third = service.create_notice(database, data, SeriesInput(), ContentInput(), EditionInput(), None)
    assert _notice(database, third).contributors[0].agent_id == chosen
    assert _notice(database, first).contributors[0].agent_id != chosen


def test_a_single_existing_name_is_linked_without_accents_or_case(settings, database):
    first = make_notice(settings, "A", authors=("René Goscinny",))
    second = make_notice(settings, "B", authors=("rene goscinny",))
    assert _notice(database, first).contributors[0].agent_id == _notice(database, second).contributors[0].agent_id


def test_contributors_keep_their_roles_and_order(settings, database):
    type_id = _type_id(database)
    rows = (ContributorInput("Morris", "artist"), ContributorInput("René Goscinny", "scenarist"))
    work = service.create_notice(
        database,
        IdentityInput("Lucky", type_id, contributors=rows),
        SeriesInput(),
        ContentInput(),
        EditionInput(),
        None,
    )
    people = _notice(database, work).contributors
    assert [(p.name, p.role, p.role_label) for p in people] == [
        ("Morris", "artist", "Dessinateur"),
        ("René Goscinny", "scenarist", "Scénariste"),
    ]


def test_one_person_can_hold_two_roles(settings, database):
    type_id = _type_id(database)
    rows = (ContributorInput("Hergé", "scenarist"), ContributorInput("Hergé", "artist"))
    work = service.create_notice(
        database, IdentityInput("T", type_id, contributors=rows), SeriesInput(), ContentInput(), EditionInput(), None
    )
    assert {p.role for p in _notice(database, work).contributors} == {"scenarist", "artist"}


def test_an_edition_has_its_own_contributors(settings, database):
    work = make_notice(settings, authors=("Jules Verne",))
    edition = service.add_edition(
        database, work, EditionInput(contributors=(ContributorInput("Anna Smith", "translator"),)), None
    )
    notice = _notice(database, work)
    other = next(e for e in notice.editions if e.id == edition)
    assert [c.name for c in other.contributors] == ["Anna Smith"]
    assert notice.primary.contributors == ()
    assert notice.authors_text == "Jules Verne"


def test_unknown_role_is_refused(settings, database):
    data = IdentityInput("T", _type_id(database), contributors=(ContributorInput("X", "chanteur"),))
    with pytest.raises(FormErrors):
        service.create_notice(database, data, SeriesInput(), ContentInput(), EditionInput(), None)


# --- Séries -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "number", "label", "shown"),
    [("1", 1.0, None, "1"), ("1,5", 1.5, None, "1,5"), ("HS 2", None, "HS 2", "HS 2")],
)
def test_volume_forms(settings, database, text, number, label, shown):
    series = _notice(database, make_notice(settings, series="Alix", volume=text)).series
    assert (series.volume_number, series.volume_label, series.volume_text) == (number, label, shown)


def test_a_volume_without_a_series_is_refused(settings):
    with pytest.raises(FormErrors) as caught:
        make_notice(settings, volume="3")
    assert "serie" in caught.value.fields


def test_call_sequence_is_distinct_from_the_volume_and_never_reused(settings, database):
    first = make_notice(settings, "T7", series="Alix", volume="7")
    second = make_notice(settings, "T1", series="Alix", volume="1")
    assert _notice(database, first).series.call_sequence == 1  # ordre d'entrée, pas numéro de tome
    assert _notice(database, second).series.call_sequence == 2
    service.delete_work(database, second, None)
    third = make_notice(settings, "T2", series="Alix", volume="2")
    assert _notice(database, third).series.call_sequence == 3


def test_changing_volume_keeps_the_call_sequence(settings, database):
    work = make_notice(settings, series="Alix", volume="7")
    before = _notice(database, work)
    service.update_series(database, work, SeriesInput("Alix", "8"), before.version, None)
    after = _notice(database, work)
    assert after.series.volume_number == 8.0
    assert after.series.call_sequence == before.series.call_sequence


def test_two_works_can_carry_the_same_volume(settings, database):
    first = make_notice(settings, "Alix 3, version A", series="Alix", volume="3")
    second = make_notice(settings, "Alix 3, version B", series="Alix", volume="3")
    assert _notice(database, first).series.volume_number == _notice(database, second).series.volume_number


def test_series_title_is_unique_without_accents(settings, database):
    make_notice(settings, series="Astérix", volume="1")
    with pytest.raises(FormErrors) as caught:
        service.create_series(database, SeriesEdit("ASTERIX"), None)
    assert "2e série" in caught.value.fields["titre"]


def test_a_series_in_use_cannot_be_deleted(settings, database):
    work = make_notice(settings, series="Alix", volume="1")
    series = _notice(database, work).series.series_id
    with pytest.raises(BusinessError):
        service.delete_series(database, series, None)
    service.delete_work(database, work, None)
    service.delete_series(database, series, None)
    assert _rows(settings, "SELECT COUNT(*) FROM series") == [(0,)]


def test_confirming_volumes_adds_only_what_is_new(settings, database):
    series = service.create_series(database, SeriesEdit("Alix"), None)
    assert service.confirm_volumes(database, series, "1", "3", "Liste de l’éditeur", None) == 3
    assert service.confirm_volumes(database, series, "2", "4", "", None) == 1
    assert _rows(settings, "SELECT COUNT(*) FROM series_expected_volumes") == [(4,)]
    service.unconfirm_volume(database, series, 4.0, None)
    assert _rows(settings, "SELECT COUNT(*) FROM series_expected_volumes") == [(3,)]


@pytest.mark.parametrize(("first", "last"), [("", ""), ("HS", ""), ("3", "1"), ("1", "999")])
def test_confirming_volumes_refuses_bad_ranges(settings, database, first, last):
    series = service.create_series(database, SeriesEdit("Alix"), None)
    with pytest.raises(FormErrors):
        service.confirm_volumes(database, series, first, last, "", None)


# --- Taxonomies -------------------------------------------------------------------------------------------------------
def test_categories_genres_and_keywords_are_multiple_and_separate(settings, database):
    work = make_notice(settings, categories="BD, Jeunesse", genres="Aventure, Humour", keywords="Tibet, Chine")
    notice = _notice(database, work)
    assert notice.categories == ("BD", "Jeunesse")
    assert notice.genres == ("Aventure", "Humour")
    assert notice.keywords == ("Chine", "Tibet")


def test_same_label_in_two_kinds_gives_two_tags(settings, database):
    make_notice(settings, categories="Aventure", genres="Aventure")
    assert _rows(settings, "SELECT kind FROM tags ORDER BY kind") == [("category",), ("genre",)]


def test_tag_written_differently_is_reused(settings, database):
    make_notice(settings, "A", genres="Récit historique")
    make_notice(settings, "B", genres="RECIT HISTORIQUE")
    assert _rows(settings, "SELECT COUNT(*) FROM tags") == [(1,)]


def test_publisher_and_collection_are_reused_by_normalized_name(settings, database):
    make_notice(settings, "A", publisher="Éditions Dupuis", collection="Repérages")
    make_notice(settings, "B", publisher="EDITIONS DUPUIS", collection="reperages")
    assert _rows(settings, "SELECT COUNT(*) FROM publishers") == [(1,)]
    assert _rows(settings, "SELECT COUNT(*) FROM collections") == [(1,)]


# --- Éditions ---------------------------------------------------------------------------------------------------------
def test_primary_edition_can_change(settings, database):
    work = make_notice(settings, year="1946")
    second = service.add_edition(database, work, EditionInput(year="2019"), None)
    service.set_primary_edition(database, second, None)
    notice = _notice(database, work)
    assert notice.primary.id == second
    assert sum(1 for e in notice.editions if e.is_primary) == 1


def test_primary_edition_cannot_be_removed(settings, database):
    work = make_notice(settings)
    primary = _notice(database, work).primary.id
    with pytest.raises(BusinessError):
        service.delete_edition(database, primary, None)
    with pytest.raises(BusinessError):
        service.archive_edition(database, primary, None)


def test_edition_with_copies_cannot_be_deleted(settings, database):
    work = make_notice(settings)
    second = service.add_edition(database, work, EditionInput(year="2019"), None)
    service.add_copies(database, second, 1, "", None)
    with pytest.raises(BusinessError):
        service.delete_edition(database, second, None)
    with pytest.raises(BusinessError):
        service.archive_edition(database, second, None)


def test_year_and_pages_are_validated(settings, database):
    work = make_notice(settings)
    with pytest.raises(FormErrors) as caught:
        service.add_edition(database, work, EditionInput(year="12", pages="abc", language="xx"), None)
    assert set(caught.value.fields) == {"annee", "pages", "langue"}


# --- Exemplaires ------------------------------------------------------------------------------------------------------
def test_barcode_is_never_reused(settings, database):
    work = make_notice(settings)
    edition = _notice(database, work).primary.id
    first = service.add_copies(database, edition, 1, "", None)[0]
    service.delete_copy(database, first, None)
    service.add_copies(database, edition, 1, "", None)
    assert [c.barcode for c in _notice(database, work).primary.copies] == ["EX-000002"]


@pytest.mark.parametrize("quantity", [0, -1, 51])
def test_copy_quantity_is_bounded(settings, database, quantity):
    edition = _notice(database, make_notice(settings)).primary.id
    with pytest.raises(FormErrors):
        service.add_copies(database, edition, quantity, "", None)


# --- Archivage --------------------------------------------------------------------------------------------------------
def test_archive_and_restore(settings, database):
    work = make_notice(settings)
    service.archive_work(database, work, None)
    assert _notice(database, work).is_archived
    service.unarchive_work(database, work, None)
    assert not _notice(database, work).is_archived


def test_work_with_active_copies_cannot_be_archived(settings, database):
    work = make_notice(settings)
    service.add_copies(database, _notice(database, work).primary.id, 1, "", None)
    with pytest.raises(BusinessError, match="exemplaires au fonds"):
        service.archive_work(database, work, None)
    assert not _notice(database, work).is_archived


def test_work_with_copies_cannot_be_deleted(settings, database):
    work = make_notice(settings)
    service.add_copies(database, _notice(database, work).primary.id, 1, "", None)
    with pytest.raises(BusinessError, match="seulement archivée"):
        service.delete_work(database, work, None)


def test_work_without_copies_can_be_deleted_with_its_editions(settings, database):
    work = make_notice(settings, isbn="978-2-07-040850-4", authors=("Hergé",), series="Tintin", volume="5")
    service.add_edition(database, work, EditionInput(year="2019"), None)
    service.delete_work(database, work, None)
    assert _rows(settings, "SELECT COUNT(*) FROM works") == [(0,)]
    assert _rows(settings, "SELECT COUNT(*) FROM editions") == [(0,)]
    assert _rows(settings, "SELECT COUNT(*) FROM edition_identifiers") == [(0,)]
    assert _rows(settings, "SELECT COUNT(*) FROM agents") == [(1,)]  # la personne reste


# --- Validation et journal --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("title", ["", "   "])
def test_a_title_is_required(settings, database, title):
    with pytest.raises(FormErrors) as caught:
        service.create_notice(
            database, IdentityInput(title, _type_id(database)), SeriesInput(), ContentInput(), EditionInput(), None
        )
    assert "titre" in caught.value.fields


def test_all_errors_are_reported_together(settings, database):
    with pytest.raises(FormErrors) as caught:
        service.create_notice(
            database,
            IdentityInput("", None),
            SeriesInput("", "3"),
            ContentInput(),
            EditionInput(isbn="123", year="x"),
            None,
        )
    assert {"titre", "type", "serie", "isbn", "annee"} <= set(caught.value.fields)


def test_nothing_is_written_when_validation_fails(settings, database):
    with pytest.raises(FormErrors):
        service.create_notice(
            database,
            IdentityInput("Essai", _type_id(database)),
            SeriesInput(),
            ContentInput(),
            EditionInput(isbn="1"),
            None,
        )
    assert _rows(settings, "SELECT COUNT(*) FROM works") == [(0,)]


def test_completeness_follows_the_primary_edition(settings, database):
    work = make_notice(settings, authors=("Hergé",), isbn="978-2-07-040850-4", publisher="Casterman", year="1946")
    assert _notice(database, work).completeness == 52  # auteurs 10, ISBN 20, éditeur 12, année 10
    second = service.add_edition(database, work, EditionInput(), None)
    assert _notice(database, work).completeness == 52  # une édition vide ne change rien
    service.set_primary_edition(database, second, None)
    assert _notice(database, work).completeness == 10


def test_actions_are_journaled_without_content(settings, database):
    work = make_notice(settings, "Titre confidentiel", summary="Résumé confidentiel")
    version = _notice(database, work).version
    service.update_content(database, work, ContentInput(summary="Autre"), version, None)
    rows = _rows(settings, "SELECT action, details FROM audit_log WHERE entity_type = 'work' ORDER BY id")
    assert [row[0] for row in rows] == ["work.create", "work.update"]
    assert json.loads(rows[1][1]) == {"section": "contenu"}
    assert "confidentiel" not in json.dumps(rows)
