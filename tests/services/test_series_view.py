# SPDX-License-Identifier: EUPL-1.2
"""Séries : un trou n'est pas un manque, tomes décimaux et hors-séries, éditions et doublons physiques."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.modules.catalogue import series_view, service
from biblyo.modules.catalogue.models import Series
from biblyo.modules.catalogue.service import EditionInput

from ..factories import make_notice


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _series_id(database, title):
    with database.session() as session:
        return session.scalar(select(Series.id).where(Series.title == title))


def _detail(database, series_id):
    with database.session() as session:
        return series_view.detail(session, series_id)


def _states(card):
    return {item.label: item.state for item in card.strip}


def _edition(settings, work):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute("SELECT id FROM editions WHERE work_id = ?", (work,)).fetchone()[0]
    finally:
        connection.close()


def _make(settings, volumes, title="Astérix"):
    return {volume: make_notice(settings, f"Tome {volume}", series=title, volume=volume) for volume in volumes}


def test_a_gap_without_confirmation_is_not_counted_missing(settings, database):
    _make(settings, ("1", "2", "5"))
    card = _detail(database, _series_id(database, "Astérix")).card
    assert _states(card) == {"1": "present", "2": "present", "3": "unverified", "4": "unverified", "5": "present"}
    assert card.missing == 0
    assert card.unverified == 2
    assert not card.is_complete


def test_a_confirmed_absent_volume_is_missing(settings, database):
    _make(settings, ("1", "2", "5"))
    series = _series_id(database, "Astérix")
    service.confirm_volumes(database, series, "3", "", "Liste de l’éditeur", None)
    card = _detail(database, series).card
    assert _states(card)["3"] == "missing" and _states(card)["4"] == "unverified"
    assert card.missing == 1 and card.unverified == 1
    assert card.percent == 75  # 3 présents sur 4 attendus : le numéro à vérifier n'entre pas dans le calcul


def test_confirmation_range_beyond_the_known_volumes(settings, database):
    _make(settings, ("1",))
    series = _series_id(database, "Astérix")
    service.confirm_volumes(database, series, "2", "4", "", None)
    card = _detail(database, series).card
    assert _states(card) == {"1": "present", "2": "missing", "3": "missing", "4": "missing"}


def test_confirming_a_present_volume_changes_nothing(settings, database):
    _make(settings, ("1", "2"))
    series = _series_id(database, "Astérix")
    service.confirm_volumes(database, series, "1", "2", "", None)
    card = _detail(database, series).card
    assert card.missing == 0 and card.is_complete


def test_decimal_volumes_open_no_gap_and_special_issues_stay_labelled(settings, database):
    _make(settings, ("1", "1.5", "2", "HS 2"))
    card = _detail(database, _series_id(database, "Astérix")).card
    assert _states(card) == {"1": "present", "1,5": "present", "2": "present", "HS 2": "present"}
    assert card.unverified == 0 and card.present == 4


def test_two_editions_of_a_volume_and_two_copies_of_an_edition(settings, database):
    works = _make(settings, ("1",))
    edition = _edition(settings, works["1"])
    service.add_edition(database, works["1"], EditionInput(publisher="Hachette", year="2019"), None)
    service.add_copies(database, edition, 2, "", None)
    detail = _detail(database, _series_id(database, "Astérix"))
    assert detail.card.has_duplicates
    assert detail.card.strip[0].badge == "2"
    row = detail.rows[0]
    assert len(row.editions) == 2 and row.copies == 2
    assert sorted(line.copies for line in row.editions) == [0, 2]


def test_two_notices_for_the_same_volume_are_flagged(settings, database):
    first = make_notice(settings, "Une version", series="Astérix", volume="1")
    second = make_notice(settings, "Une autre", series="Astérix", volume="1")
    detail = _detail(database, _series_id(database, "Astérix"))
    assert detail.card.strip[0].badge == "2"
    assert detail.card.strip[0].href.endswith("#tome-1")
    assert {row.work_id for row in detail.rows} == {first, second}
    assert all(row.duplicate for row in detail.rows)
    assert detail.card.present == 1


def test_withdrawn_copies_are_not_counted(settings, database):
    works = _make(settings, ("1",))
    copy_ids = service.add_copies(database, _edition(settings, works["1"]), 2, "", None)
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE copies SET status = 'withdrawn' WHERE id = ?", (copy_ids[0],))
    connection.commit()
    connection.close()
    assert _detail(database, _series_id(database, "Astérix")).card.copies == 1


def test_archived_works_leave_the_strip(settings, database):
    works = _make(settings, ("1", "2"))
    service.archive_work(database, works["2"], None)
    assert _states(_detail(database, _series_id(database, "Astérix")).card) == {"1": "present"}


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("", {"Complète", "Trouée", "Attendue", "Doublon"}),
        ("incompletes", {"Attendue"}),
        ("a-verifier", {"Trouée"}),
        ("completes", {"Complète", "Doublon"}),
        ("doublons", {"Doublon"}),
    ],
)
def test_list_filters(settings, database, code, expected):
    _make(settings, ("1", "2"), "Complète")
    _make(settings, ("1", "3"), "Trouée")
    _make(settings, ("1",), "Attendue")
    service.confirm_volumes(database, _series_id(database, "Attendue"), "2", "", "", None)
    make_notice(settings, "A", series="Doublon", volume="1")
    make_notice(settings, "B", series="Doublon", volume="1")
    with database.session() as session:
        result = series_view.list_series(session, "", code)
    assert {card.title for card in result.cards} == expected


def test_list_search_ignores_accents(settings, database):
    _make(settings, ("1",), "Astérix")
    _make(settings, ("1",), "Tintin")
    with database.session() as session:
        result = series_view.list_series(session, "asterix")
    assert [card.title for card in result.cards] == ["Astérix"]


def test_incomplete_count_only_counts_confirmed_gaps(settings, database):
    _make(settings, ("1", "3"), "Avec trou")
    _make(settings, ("1",), "Confirmée")
    service.confirm_volumes(database, _series_id(database, "Confirmée"), "2", "", "", None)
    with database.session() as session:
        assert series_view.incomplete_count(session) == 1


def test_deleting_a_series_in_use_is_refused(settings, database):
    _make(settings, ("1",))
    series = _series_id(database, "Astérix")
    with pytest.raises(BusinessError, match="appartiennent"):
        service.delete_series(database, series, None)


@pytest.mark.parametrize(("first", "last"), [("", ""), ("3", "1"), ("abc", "")])
def test_bad_confirmations_are_refused(settings, database, first, last):
    _make(settings, ("1",))
    with pytest.raises(FormErrors):
        service.confirm_volumes(database, _series_id(database, "Astérix"), first, last, "", None)


def _card(database, title="Astérix"):
    return _detail(database, _series_id(database, title)).card


def test_a_complete_series_says_so_and_is_green(settings, database):
    _make(settings, ("1", "2", "3"))
    card = _card(database)
    assert card.percent == 100 and card.is_complete
    assert card.ring_module == "ok"
    assert card.percent_caption == "100 % des tomes attendus"


def test_an_incomplete_series_counts_its_confirmed_gaps(settings, database):
    _make(settings, ("1", "2", "3"))
    service.confirm_volumes(database, _series_id(database, "Astérix"), "4", "", "", None)
    card = _card(database)
    assert card.percent == 75 and not card.is_complete
    assert card.ring_module == "ser"
    assert card.percent_caption == "75 % des tomes attendus"


def test_uncertain_numbers_qualify_a_full_percentage(settings, database):
    _make(settings, ("1", "2", "5"))
    card = _card(database)
    assert card.percent == 100 and card.unverified == 2 and card.missing == 0
    assert not card.is_complete and card.state == "uncertain" and card.is_uncertain
    assert card.ring_module == ""  # jamais en vert tant que des numéros restent incertains : gris, en pointillés
    assert not card.matches("completes")
    assert card.percent_caption == "100 % des tomes confirmés · 2 numéros à vérifier, non comptés"


def test_one_uncertain_number_is_in_the_singular(settings, database):
    _make(settings, ("1", "3"))
    assert _card(database).percent_caption == "100 % des tomes confirmés · 1 numéro à vérifier, non compté"


def test_confirmed_gap_and_uncertain_numbers_together(settings, database):
    _make(settings, ("1", "5"))
    service.confirm_volumes(database, _series_id(database, "Astérix"), "2", "", "", None)
    card = _card(database)
    assert (card.present, card.missing, card.unverified) == (2, 1, 2)
    assert card.percent == 67 and card.state == "uncertain"
    assert card.percent_caption == "67 % des tomes confirmés · 2 numéros à vérifier, non comptés"


def test_a_series_with_nothing_numbered_makes_no_claim(settings, database):
    series = service.create_series(database, service.SeriesEdit("Vide"), None)
    card = _detail(database, series).card
    assert card.percent == 0 and card.percent_caption == "Aucun tome numéroté confirmé"
    assert not card.is_complete


def test_multiple_editions_and_copies_do_not_change_the_percentage(settings, database):
    works = _make(settings, ("1", "2"))
    edition = _edition(settings, works["1"])
    service.add_edition(database, works["1"], EditionInput(publisher="Hachette"), None)
    service.add_copies(database, edition, 3, "", None)
    card = _card(database)
    assert card.percent == 100 and card.is_complete and card.ring_module == "ok"
    assert card.has_duplicates and card.strip[0].badge == "3"
    assert card.present == 2 and card.copies == 3


def test_unfiltered_pagination_agrees_with_the_filtered_path(settings, database):
    """La liste sans filtre ne charge que sa page : elle doit dire la même chose que le calcul complet."""
    for index in range(45):
        make_notice(settings, f"Tome {index}", series=f"Série {index:02d}", volume="1")
    _make(settings, ("1",), "Zèbre attendu")
    service.confirm_volumes(database, _series_id(database, "Zèbre attendu"), "2", "", "", None)
    with database.session() as session:
        first = series_view.list_series(session, "", "", 1)
        second = series_view.list_series(session, "", "", 2)
        filtered = series_view.list_series(session, "", "incompletes", 1)
    assert first.total == 46 and first.pages == 2
    assert len(first.cards) == 40 and len(second.cards) == 6
    assert first.incomplete == 1 == filtered.incomplete
    assert second.cards[-1].title == "Zèbre attendu" and second.cards[-1].missing == 1
    assert {card.title for card in filtered.cards} == {"Zèbre attendu"}


def test_incomplete_count_follows_the_search_term(settings, database):
    _make(settings, ("1",), "Astérix")
    _make(settings, ("1",), "Tintin")
    for title in ("Astérix", "Tintin"):
        service.confirm_volumes(database, _series_id(database, title), "2", "", "", None)
    with database.session() as session:
        assert series_view.incomplete_count(session) == 2
        assert series_view.list_series(session, "asterix").incomplete == 1


def test_an_archived_volume_counts_as_absent_when_confirmed(settings, database):
    works = _make(settings, ("1", "2"))
    series = _series_id(database, "Astérix")
    service.confirm_volumes(database, series, "2", "", "", None)
    with database.session() as session:
        assert series_view.incomplete_count(session) == 0
    service.archive_work(database, works["2"], None)
    with database.session() as session:
        assert series_view.incomplete_count(session) == 1


def test_the_complete_label_names_the_last_volume(settings, database):
    _make(settings, ("1", "2", "3"))
    card = _card(database)
    assert card.state == "complete" and card.complete_label == "Complète jusqu’au t. 3"


def test_a_missing_only_series_is_blue_and_not_uncertain(settings, database):
    _make(settings, ("1", "2"))
    service.confirm_volumes(database, _series_id(database, "Astérix"), "3", "", "", None)
    card = _card(database)
    assert card.state == "missing" and card.ring_module == "ser" and not card.is_uncertain


def test_uncertain_labels(settings, database):
    _make(settings, ("1", "3"))
    assert _card(database).unverified_label == "1 numéro à vérifier"
    _make(settings, ("1", "4"), "Autre")
    assert _card(database, "Autre").unverified_label == "2 numéros à vérifier"


def _seed_varied(settings, database):
    """Séries de toutes les sortes : complète, trouée, attendue, doublons, éditions et exemplaires multiples."""
    _make(settings, ("1", "2", "3"), "Complète")
    _make(settings, ("1", "4"), "Trouée")
    _make(settings, ("1",), "Attendue")
    service.confirm_volumes(database, _series_id(database, "Attendue"), "2", "3", "", None)
    make_notice(settings, "A", series="Doublon", volume="1")
    make_notice(settings, "B", series="Doublon", volume="1")
    works = _make(settings, ("1", "2"), "Éditions")
    service.add_edition(database, works["1"], EditionInput(publisher="Autre"), None)
    works = _make(settings, ("1", "HS 1"), "Exemplaires")
    service.add_copies(database, _edition(settings, works["HS 1"]), 2, "", None)
    archived = _make(settings, ("1", "2", "3"), "Archivée")
    service.archive_work(database, archived["2"], None)
    _make(settings, ("1", "1.5", "2"), "Décimale")
    service.create_series(database, service.SeriesEdit("Vide"), None)


@pytest.mark.parametrize("code", ["", "incompletes", "a-verifier", "completes", "doublons"])
def test_fast_filtering_agrees_with_full_cards(settings, database, code):
    """Le classement rapide doit retenir exactement les séries que retiendraient leurs cartes complètes."""
    _seed_varied(settings, database)
    with database.session() as session:
        ids = series_view.ordered_series_ids(session)
        full = series_view._cards_of(session, ids)
        expected = [card.title for card in full if card.matches(code)]
        listed = series_view.list_series(session, "", code)
        counts = series_view.classify(ids, series_view.load_facts(session, with_duplicates=True))
    assert [card.title for card in listed.cards] == expected
    for card in full:
        assert counts[card.id] == card.counts, card.title


def test_filtered_list_states(settings, database):
    _seed_varied(settings, database)
    with database.session() as session:
        states = {card.title: card.state for card in series_view.list_series(session).cards}
    assert states == {
        "Archivée": "uncertain",
        "Attendue": "missing",
        "Complète": "complete",
        "Décimale": "complete",
        "Doublon": "complete",
        "Exemplaires": "complete",
        "Trouée": "uncertain",
        "Vide": "empty",
        "Éditions": "complete",
    }


def test_the_doublons_filter_needs_its_facts(settings, database):
    _make(settings, ("1",))
    with database.session() as session:
        ids = series_view.ordered_series_ids(session)
        counts = series_view.classify(ids, series_view.load_facts(session, with_duplicates=False))
    with pytest.raises(ValueError):
        counts[ids[0]].matches("doublons")
