# SPDX-License-Identifier: EUPL-1.2
"""Constructeur de requête : accents, ISBN, critères par champ, et aucune saisie ne peut atteindre la syntaxe FTS5."""

from __future__ import annotations

import sqlite3

import pytest

from biblyo.modules.catalogue.search import COLUMNS, build_match


@pytest.mark.parametrize("empty", [None, "", "   ", "***", '""', "()", "-- ; '"])
def test_nothing_to_search_gives_none(empty):
    assert build_match(empty) is None


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("Cœur", '"coeur"*'),
        ("ÉLÉPHANT", '"elephant"*'),
        ("astérix gaulois", '"asterix"* AND "gaulois"*'),
        ("L’Agent", '"l"* AND "agent"*'),
    ],
)
def test_terms_are_normalized_and_prefixed(typed, expected):
    assert build_match(typed) == expected


@pytest.mark.parametrize(
    "typed",
    ["978-2-07-040850-4", "9782070408504", "978 2 07 040850 4", "2-07-040850-7", "2070408507"],
)
def test_an_isbn_in_any_form_targets_the_identifiers(typed):
    assert build_match(typed) == 'identifiers : "9782070408504"'


def test_a_numeric_code_with_a_wrong_check_digit_stays_a_prefix_search():
    assert build_match("2-07-040850-X") == '"207040850x"*'


def test_partial_numeric_code_with_dashes_is_compacted():
    assert build_match("978-2-07") == '"978207"*'


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("auteur:goscinny", 'contributors : "goscinny"*'),
        ("série:astérix", 'series : "asterix"*'),
        ("Editeur:dargaud annee:1961", 'publisher : "dargaud"* AND years : "1961"*'),
        ("titre:cœur", '{title subtitle} : "coeur"*'),
        ('titre:"le lotus bleu"', '{title subtitle} : "le lotus bleu"*'),
        ("isbn:978-2-07-040850-4", 'identifiers : "9782070408504"'),
        ("code:EX-000042", 'barcodes : "ex"* AND barcodes : "000042"*'),
    ],
)
def test_field_criteria(typed, expected):
    assert build_match(typed) == expected


def test_an_unknown_field_is_plain_text():
    assert build_match("foo:bar") == '"foo"* AND "bar"*'


HOSTILE = [
    '"',
    '"unbalanced',
    'a"b',
    "*",
    "a*",
    "a* b*",
    "NEAR(a b, 2)",
    "NEAR",
    "a OR b",
    "OR",
    "AND",
    "NOT",
    "a AND",
    "NOT a",
    "(",
    ")",
    "(a OR b",
    "title:",
    ":",
    ":a",
    "a:",
    "titre:",
    'titre:"',
    "{title}:a",
    "{title subtitle} : a",
    "^a",
    "a + b",
    "-a",
    "' OR 1=1 --",
    "works_search MATCH 'x'",
    "col1:a OR col2:b",
    "\x00",
    "a\x00b",
    "x" * 5000,
    " ".join(f"mot{i}" for i in range(200)),
]


@pytest.mark.parametrize("typed", HOSTILE)
def test_no_input_reaches_the_fts5_syntax(typed):
    connection = sqlite3.connect(":memory:")
    connection.execute(f"CREATE VIRTUAL TABLE works_search USING fts5({', '.join(COLUMNS)})")
    connection.execute("INSERT INTO works_search(title) VALUES ('le lotus bleu')")
    match = build_match(typed)
    if match is not None:
        connection.execute("SELECT rowid FROM works_search WHERE works_search MATCH ?", (match,)).fetchall()
    connection.close()


def test_operators_are_searched_as_words():
    assert build_match("a OR b") == '"a"* AND "or"* AND "b"*'
    assert build_match("NEAR(a b)") == '"near"* AND "a"* AND "b"*'


def test_the_number_of_terms_is_bounded():
    assert build_match(" ".join(f"mot{i}" for i in range(200))).count(" AND ") == 31
