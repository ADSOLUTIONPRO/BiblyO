# SPDX-License-Identifier: EUPL-1.2
"""ISBN : clé de contrôle, formes normalisées, conversions."""

from __future__ import annotations

import pytest

from biblyo.core.isbn import clean, is_valid_isbn10, is_valid_isbn13, parse_isbn, split_list, to_isbn10, to_isbn13


@pytest.mark.parametrize(
    ("value", "isbn13", "isbn10"),
    [
        ("978-2-07-040850-4", "9782070408504", "2070408507"),
        ("9782070408504", "9782070408504", "2070408507"),
        ("2-07-040850-7", "9782070408504", "2070408507"),
        (" 2 07 040850 7 ", "9782070408504", "2070408507"),
        ("0-8044-2957-X", "9780804429573", "080442957X"),
        ("0-8044-2957-x", "9780804429573", "080442957X"),
        ("979-10-90636-07-1", "9791090636071", None),
    ],
)
def test_parse_valid(value, isbn13, isbn10):
    parsed = parse_isbn(value)
    assert parsed is not None
    assert (parsed.isbn13, parsed.isbn10) == (isbn13, isbn10)


@pytest.mark.parametrize(
    "value",
    ["", None, "abc", "978207040850", "9782070408505", "2070408506", "97820704085040", "1234567890123", "207040850X"],
)
def test_parse_invalid(value):
    assert parse_isbn(value) is None


def test_conversions_are_reversible():
    assert to_isbn13("2070408507") == "9782070408504"
    assert to_isbn10("9782070408504") == "2070408507"
    assert to_isbn10(to_isbn13("080442957X")) == "080442957X"
    assert to_isbn10("9791090636071") is None


def test_validity_requires_the_exact_form():
    assert is_valid_isbn13("9782070408504")
    assert not is_valid_isbn13("2070408507")
    assert is_valid_isbn10("2070408507")
    assert not is_valid_isbn10("9782070408504")


def test_clean_handles_typographic_hyphens():
    assert clean("978–2‑07 040850.4") == "9782070408504"


def test_split_list():
    assert split_list("978-2-07-040850-4 ; 2-07-040850-7,\n") == ["978-2-07-040850-4", "2-07-040850-7"]
    assert split_list(None) == []
