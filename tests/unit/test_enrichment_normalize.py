# SPDX-License-Identifier: EUPL-1.2
"""Clés de comparaison bibliographiques : titres, personnes, éditeurs, années, pages, tomes, ISBN, termes."""

from __future__ import annotations

import pytest

from biblyo.modules.enrichment.normalize import (
    clean_text,
    is_open_date,
    isbn13_of,
    parse_pages,
    parse_volume,
    parse_year,
    publisher_key,
    publisher_relation,
    same_person,
    search_terms,
    similarity,
    title_key,
)


@pytest.mark.parametrize(
    ("left", "right", "floor"),
    [
        ("Le petit prince", "Le Petit Prince", 1.0),
        ("L'écume des jours", "L’ÉCUME DES JOURS", 1.0),
        ("Astérix le Gaulois", "Astérix le gaulois", 1.0),
        ("Le tour de Gaule d'Astérix", "Le Tour de Gaule", 0.70),
        ("Le petit prince", "Le petit prince voyage", 0.75),
    ],
)
def test_similar_titles(left, right, floor):
    assert similarity(left, right) >= floor


@pytest.mark.parametrize(
    ("left", "right"),
    [("Tintin au Congo", "Les cigares du pharaon"), ("Le Horla", "Boule de suif"), ("", "Le Horla"), ("", "")],
)
def test_different_titles(left, right):
    assert similarity(left, right) < 0.5


def test_title_key_drops_the_initial_article():
    assert title_key("L'Agent 212") == title_key("Agent 212")


@pytest.mark.parametrize(
    ("local", "surname", "forename"),
    [
        ("René Goscinny", "Goscinny", "René"),
        ("Goscinny, René", "Goscinny", "René"),
        ("R. Goscinny", "Goscinny", "René"),
        ("Antoine de Saint-Exupéry", "Saint-Exupéry", "Antoine de"),
        ("Hergé", "Hergé", ""),
        ("Goscinny", "Goscinny", "René"),
        ("Eiichiro Oda", "Oda", "Eiichirō"),
    ],
)
def test_same_person(local, surname, forename):
    assert same_person(local, surname, forename)


@pytest.mark.parametrize(
    ("local", "surname", "forename"),
    [
        ("Anne Goscinny", "Goscinny", "René"),
        ("Paul Brizzi", "Brizzi", "Gaëtan"),
        ("René Goscinny", "Uderzo", "Albert"),
        ("", "Goscinny", "René"),
        ("René", "Goscinny", "René"),
    ],
)
def test_other_person(local, surname, forename):
    assert not same_person(local, surname, forename)


@pytest.mark.parametrize(
    ("left", "right", "relation"),
    [
        ("Gallimard", "Gallimard", "same"),
        ("Les Éditions Albert-René", "Éditions Albert René", "same"),
        ("les Éditions Albert-René", "Albert René", "same"),
        ("Gallimard", "Gallimard jeunesse", "related"),
        ("Dargaud", "Hachette", "different"),
        ("", "Hachette", "unknown"),
        ("Éditions", "Hachette", "unknown"),
    ],
)
def test_publisher_relation(left, right, relation):
    assert publisher_relation(left, right) == relation


def test_publisher_key_ignores_noise_words():
    assert publisher_key("Librairie du petit jour") == "du petit jour"


@pytest.mark.parametrize(
    ("raw", "year"),
    [
        ("DL 2007", 2007),
        ("impr. 2014", 2014),
        ("cop. 2006-", 2006),
        ("[1980 (DL)]", 1980),
        ("1913", 1913),
        ("[s.d.?]", None),
        ("", None),
        ("DL. 1999", 1999),
        ("3999", None),
    ],
)
def test_parse_year(raw, year):
    assert parse_year(raw) == year


@pytest.mark.parametrize(
    ("raw", "open_"), [("1962-", True), ("cop. 2006-", True), ("2013-", True), ("DL 2007", False), ("1962-1970", False)]
)
def test_open_date(raw, open_):
    assert is_open_date(raw) is open_


@pytest.mark.parametrize(
    ("raw", "pages"),
    [
        ("1 vol. (113 p.)", 113),
        ("1 volume 48 p", 48),
        ("1 vol. (non paginé [ca 208] p.)", 208),
        ("Non paginé [8] p.", 8),
        ("1 vol. (297 p.-[2] p. dépl.)", 297),
        ("1 vol. (lxiii-1309 pages)", 1309),
        ("1 volume (113 pages)", 113),
        ("46 p., 48 p., 47 p., 48 p", None),
        ("4 vol.", None),
        ("1 boîte", None),
        ("", None),
    ],
)
def test_parse_pages(raw, pages):
    assert parse_pages(raw) == pages


@pytest.mark.parametrize(
    ("raw", "volume"),
    [
        ("5", 5.0),
        ("T. 5", 5.0),
        ("[1]", 1.0),
        ("Bd 6", 6.0),
        ("V", 5.0),
        ("12,5", 12.5),
        ("72", 72.0),
        ("HS 2", None),
        ("", None),
        ("Tome deuxième", None),
        ("M", None),
    ],
)
def test_parse_volume(raw, volume):
    assert parse_volume(raw) == volume


@pytest.mark.parametrize(
    ("raw", "isbn13"),
    [
        ("978-2-07-061275-8", "9782070612758"),
        ("2-07-061275-9", "9782070612758"),
        ("978-2-07-061275-9", None),
        ("2-84897-076-06", None),
        ("978-2-2151-338-5-8", "9782215133858"),
        ("", None),
    ],
)
def test_isbn13_of_recomputes_the_check_digit(raw, isbn13):
    assert isbn13_of(raw) == isbn13


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [("[Paris]", "Paris"), ("  Le  petit   prince ", "Le petit prince"), ("", ""), (None, "")],
)
def test_clean_text(raw, cleaned):
    assert clean_text(raw) == cleaned


@pytest.mark.parametrize(
    ("raw", "terms"),
    [
        ("L'écume des jours", "ecume des jours"),
        ('guillemet "non fermé and (', "guillemet non ferme"),
        ("Astérix le Gaulois", "asterix le gaulois"),
        ("One piece 72", "one piece 72"),
        ("", ""),
        ("un deux trois quatre cinq six sept huit neuf dix", "un deux trois quatre cinq six sept huit"),
    ],
)
def test_search_terms_are_safe(raw, terms):
    assert search_terms(raw) == terms
