# SPDX-License-Identifier: EUPL-1.2
"""Téléphones : métropole, outre-mer, étranger. Forme seulement, jamais d'appel au réseau. Numéros fictifs."""

from __future__ import annotations

import pytest

from biblyo.core import phones
from biblyo.core.phones import Ambiguous, Phone, PhoneError


@pytest.mark.parametrize(
    ("text", "e164", "territory", "display"),
    [
        ("06 12 34 56 78", "+33612345678", "metropole", "06 12 34 56 78"),
        ("01.99.00.12.34", "+33199001234", "metropole", "01 99 00 12 34"),
        ("+33 7 12 34 56 78", "+33712345678", "metropole", "07 12 34 56 78"),
        ("0033612345678", "+33612345678", "metropole", "06 12 34 56 78"),
        ("0590 12 34 56", "+590590123456", "guadeloupe", "0590 12 34 56"),
        ("0690-12-34-56", "+590690123456", "guadeloupe", "0690 12 34 56"),
        ("0694 12 34 56", "+594694123456", "guyane", "0694 12 34 56"),
        ("0696 12 34 56", "+596696123456", "martinique", "0696 12 34 56"),
        ("0262 12 34 56", "+262262123456", "reunion", "0262 12 34 56"),
        ("0692 12 34 56", "+262692123456", "reunion", "0692 12 34 56"),
        ("0269 61 23 45", "+262269612345", "mayotte", "0269 61 23 45"),
        ("0639 12 34 56", "+262639123456", "mayotte", "0639 12 34 56"),
        ("+590 690 12 34 56", "+590690123456", "guadeloupe", "0690 12 34 56"),
        ("+262 639 12 34 56", "+262639123456", "mayotte", "0639 12 34 56"),
        ("+508 41 23 45", "+508412345", "saint_pierre", "+508 41 23 45"),
        ("+681 72 12 34", "+681721234", "wallis", "+681 72 12 34"),
        ("+687 28 12 34", "+687281234", "caledonie", "+687 28 12 34"),
        ("+689 40 12 34 56", "+68940123456", "polynesie", "+689 40 12 34 56"),
        ("+44 20 7946 0958", "+442079460958", None, "+44 20 79 46 09 58"),
        ("+1 202 555 0143", "+12025550143", None, "+1 20 25 55 01 43"),  # groupes de deux : forme générique
        ("+352 12 34 56", "+352123456", None, "+352 12 34 56"),
        ("00 12 34 56 78", "+12345678", None, "+1 2 34 56 78"),
    ],
)
def test_accepted_numbers(text, e164, territory, display):
    phone = phones.parse_phone(text)
    assert isinstance(phone, Phone)
    assert (phone.e164, phone.territory, phone.display) == (e164, territory, display)
    assert phones.territory_of(e164) == territory


def test_empty_is_none():
    assert phones.parse_phone("") is None and phones.parse_phone("  ") is None


@pytest.mark.parametrize(
    "text",
    ["12", "06 12 34 56", "012 345", "000 12 34 56 78", "+33 0 12 34 56 78", "+590 12 34 56 78 9", "+262 600 12 34 56",
     "06 12 34 56 7a", "+508 41 23", "+0123456789", "+12345", "123 456 789 012 345 678"],
)  # fmt: skip
def test_refused_numbers(text):
    with pytest.raises(PhoneError):
        phones.parse_phone(text)


def test_dom_number_with_the_metropolitan_code_must_be_confirmed():
    result = phones.parse_phone("+33 6 90 12 34 56")
    assert isinstance(result, Ambiguous)
    assert [item.e164 for item in result.candidates] == ["+590690123456"]
    assert "Guadeloupe" in result.reason


def test_six_digits_ask_which_collectivity():
    result = phones.parse_phone("41 23 45")
    assert isinstance(result, Ambiguous)
    assert [item.territory for item in result.candidates] == ["saint_pierre", "wallis", "caledonie"]


def test_nine_or_eight_digits_ask_for_confirmation():
    nine = phones.parse_phone("612345678")
    assert isinstance(nine, Ambiguous) and nine.candidates[0].e164 == "+33612345678"
    eight = phones.parse_phone("40123456")
    assert isinstance(eight, Ambiguous) and eight.candidates[0].territory == "polynesie"


def test_explicit_territory_resolves_the_ambiguity():
    assert phones.parse_phone("41 23 45", territory="caledonie") == Phone("+687412345", "caledonie")
    assert phones.parse_phone("612345678", territory="metropole") == Phone("+33612345678", "metropole")
    with pytest.raises(PhoneError, match="La Réunion"):
        phones.parse_phone("0692 12 34 56", territory="metropole")
    with pytest.raises(PhoneError):
        phones.parse_phone("41 23 45", territory="lune")


def test_every_territory_is_offered():
    codes = [code for code, _label in phones.TERRITORY_CHOICES]
    assert codes[0] == "metropole" and len(codes) == 10
