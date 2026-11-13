# SPDX-License-Identifier: EUPL-1.2
"""Règles pures du catalogue : normalisation française, tomes, complétude, couleurs, frise des séries."""

from __future__ import annotations

import re
import uuid

import pytest

from biblyo.core.ids import new_public_id
from biblyo.core.text import normalize, sort_name, split_terms, title_sort
from biblyo.modules.catalogue.rules import (
    COMPLETENESS_FIELDS,
    COVER_PALETTE_SIZE,
    InvalidVolume,
    Volume,
    completeness,
    cover_color,
    format_volume,
    parse_volume,
    series_strip,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("cœur", "coeur"),
        ("CŒUR", "coeur"),
        ("Lætitia", "laetitia"),
        ("Astérix", "asterix"),
        ("Éléphant", "elephant"),
        ("À l’ombre", "a l ombre"),
        ("L'Été à Noël", "l ete a noel"),
        ("straße", "strasse"),
    ],
)
def test_french_normalization(left, right):
    assert normalize(left) == right


@pytest.mark.parametrize(
    ("title", "key"),
    [
        ("L'Agent 212", "agent 212"),
        ("L’Agent 212", "agent 212"),
        ("Le Lotus bleu", "lotus bleu"),
        ("La Marque jaune", "marque jaune"),
        ("Les 4 as", "4 as"),
        ("Une aventure", "aventure"),
        ("Un", "un"),
        ("Des", "des"),
        ("Lesotho", "lesotho"),
        ("À l'ombre des derricks", "a l ombre des derricks"),
    ],
)
def test_title_sort_ignores_leading_article(title, key):
    assert title_sort(title) == key


def test_sort_name_puts_the_last_word_first():
    assert sort_name("René Goscinny") == "goscinny rene"
    assert sort_name("Greg") == "greg"
    assert sort_name("") == ""


def test_split_terms_removes_empty_and_duplicates():
    assert split_terms(" Aventure, aventure ; Humour,, ") == ["Aventure", "Humour"]
    assert split_terms(None) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12", Volume(12.0)),
        ("12,5", Volume(12.5)),
        ("12.5", Volume(12.5)),
        ("T. 12", Volume(12.0)),
        ("tome 3", Volume(3.0)),
        ("0", Volume(0.0)),
        ("HS 2", Volume(label="HS 2")),
        ("Intégrale 1", Volume(label="Intégrale 1")),
        ("", Volume()),
        ("   ", Volume()),
        (None, Volume()),
    ],
)
def test_parse_volume(text, expected):
    assert parse_volume(text) == expected


@pytest.mark.parametrize("text", ["-1", "-2,5", "x" * 61])
def test_parse_volume_refuses(text):
    with pytest.raises(InvalidVolume):
        parse_volume(text)


def test_format_volume():
    assert format_volume(12.0, None) == "12"
    assert format_volume(12.5, None) == "12,5"
    assert format_volume(None, "HS 2") == "HS 2"
    assert format_volume(3.0, "Intégrale 3") == "Intégrale 3"
    assert format_volume(None, None) == ""


def test_completeness_weights_add_up_to_100():
    assert sum(weight for _key, _label, weight in COMPLETENESS_FIELDS) == 100


def test_completeness_reports_what_is_missing():
    assert completeness({})[0] == 0
    score, missing = completeness({"isbn": True, "contributors": True})
    assert score == 30
    assert "ISBN" not in missing and "Résumé" in missing
    assert completeness({key: True for key, _label, _weight in COMPLETENESS_FIELDS}) == (100, [])


def test_cover_color_is_stable_and_ignores_accents():
    assert cover_color("Astérix") == cover_color("ASTERIX")
    assert all(0 <= cover_color(str(i)) < COVER_PALETTE_SIZE for i in range(50))
    assert len({cover_color(str(i)) for i in range(200)}) == COVER_PALETTE_SIZE


def _states(strip):
    return {item.number: item.state for item in strip}


def test_a_gap_in_the_numbering_is_not_a_missing_volume():
    """Décision D2 : rien ne dit que le tome 2 a paru."""
    assert _states(series_strip({1.0, 3.0}, set())) == {1.0: "present", 2.0: "unverified", 3.0: "present"}


def test_a_confirmed_volume_absent_from_the_catalogue_is_missing():
    assert _states(series_strip({1.0}, {1.0, 2.0})) == {1.0: "present", 2.0: "missing"}
    strip = _states(series_strip({1.0, 4.0}, {2.0}))
    assert strip == {1.0: "present", 2.0: "missing", 3.0: "unverified", 4.0: "present"}


def test_decimal_volumes_open_no_gap():
    assert _states(series_strip({1.0, 1.5, 2.0}, set())) == {1.0: "present", 1.5: "present", 2.0: "present"}
    assert _states(series_strip({1.5}, set())) == {1.5: "present"}


def test_confirmed_volumes_beyond_the_catalogue_extend_the_strip():
    assert _states(series_strip(set(), {3.0})) == {1.0: "unverified", 2.0: "unverified", 3.0: "missing"}


def test_empty_series_has_an_empty_strip():
    assert series_strip(set(), set()) == []


def test_public_id_is_a_uuid7_sorted_by_time(frozen_clock):
    first = new_public_id()
    frozen_clock.advance(seconds=1)
    second = new_public_id()
    assert uuid.UUID(first).version == 7
    assert first < second
    assert re.fullmatch(r"[0-9a-f-]{36}", first)
