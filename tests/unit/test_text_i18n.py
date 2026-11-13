# SPDX-License-Identifier: EUPL-1.2
from datetime import UTC, date, datetime

from biblyo.core import i18n, text


def test_normalize_ignores_accents_case_and_punctuation():
    assert text.normalize("L'Été à Noël") == "l ete a noel"
    assert text.normalize("Œuvres complètes") == "oeuvres completes"
    assert text.normalize(None) == ""


def test_initials():
    assert text.initials("Médiathèque Les Tilleuls") == "ML"
    assert text.initials("biblyO") == "B"


def test_french_dates():
    assert i18n.date_long(date(2026, 11, 6)) == "6 novembre 2026"
    assert i18n.date_long(None) == "—"
    assert i18n.date_with_weekday(date(2026, 11, 6)) == "Vendredi 6 novembre"
    moment = datetime(2026, 11, 6, 13, 3, tzinfo=UTC)
    assert i18n.date_long(moment) == "6 novembre 2026"
    assert i18n.datetime_short(moment) == "6 nov. 2026 à 14:03"


def test_french_typography_uses_non_breaking_spaces():
    expected = f"Vraiment{i18n.NNBSP}? Oui{i18n.NBSP}: «{i18n.NBSP}non{i18n.NBSP}»"
    assert i18n.typo("Vraiment ? Oui : « non »") == expected


def test_french_plural_treats_zero_as_singular():
    assert i18n.ngettext("livre", "livres", 0) == "livre"
    assert i18n.ngettext("livre", "livres", 2) == "livres"


def test_number_uses_french_grouping():
    assert i18n.number(12345).replace(" ", " ") == "12 345"
