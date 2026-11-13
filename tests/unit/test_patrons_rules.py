# SPDX-License-Identifier: EUPL-1.2
"""Règles des lecteurs : noms, naissance, majorité, numéros, cartes, courriels, adresses, liens et droits."""

from __future__ import annotations

from datetime import date

import pytest

from biblyo.modules.patrons import rules
from biblyo.modules.patrons.rules import GuardianFacts, Marks, PatronError

TODAY = date(2026, 11, 8)


def test_names_are_cleaned_and_keys_are_accent_free():
    assert rules.clean_name("  Léa   ", "prénom") == "Léa"
    assert rules.name_norm("Léa", "Martin") == "lea martin"
    assert rules.sort_key("Léa", "Martin") == "martin lea"
    assert rules.initials("Léa", "Martin") == "LM"
    with pytest.raises(PatronError, match="nom"):
        rules.clean_name(" ", "nom")
    with pytest.raises(PatronError):
        rules.clean_name("x" * 81, "nom")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("2014", "2014"),
        ("2014-05-03", "2014-05-03"),
        ("3/5/2014", "2014-05-03"),
        ("29.02.2012", "2012-02-29"),
    ],
)
def test_birth_is_kept_as_precise_as_typed(text, expected):
    assert rules.parse_birth(text, TODAY) == expected


@pytest.mark.parametrize("text", ["1899", "2027", "2026-11-09", "31/02/2014", "mai 2014", "14"])
def test_implausible_births_are_refused(text):
    with pytest.raises(PatronError):
        rules.parse_birth(text, TODAY)


def test_age_and_majority():
    assert rules.age_range("2008-11-08", TODAY) == (18, 18)
    assert rules.age_range("2008-11-09", TODAY) == (17, 17)
    assert rules.age_range("2008", TODAY) == (17, 18)
    assert rules.majority_to_confirm("minor", "2008-11-08", TODAY) is True
    assert rules.majority_to_confirm("minor", "2008-11-09", TODAY) is False
    assert rules.majority_to_confirm("minor", "2008", TODAY) is True  # l'année des 18 ans : à vérifier
    assert rules.majority_to_confirm("minor", None, TODAY) is False
    assert rules.majority_to_confirm("adult", "2000", TODAY) is False


def test_incoherent_category_is_signalled_not_corrected():
    assert rules.category_issue("adult", "2015", TODAY) is not None
    assert rules.category_issue("adult", "2008", TODAY) is None  # peut-être 18 ans : pas une incohérence certaine
    assert rules.category_issue("minor", "1980", TODAY) is None  # relève de la majorité à confirmer


def test_patron_numbers_and_cards():
    assert rules.format_patron_number(123) == "P-000123"
    assert [rules.parse_patron_number(text) for text in ("p-123", "P000123", " P - 000123 ")] == ["P-000123"] * 3
    assert rules.parse_patron_number("L-000123") is None
    assert rules.normalize_card(" l-000045 ") == "L-000045"
    assert rules.normalize_card("csn 0042") == "CSN0042"
    assert rules.normalize_card("é") is None and rules.normalize_card("") is None


@pytest.mark.parametrize("value", ["P-123", "p-000123", "P000123", " P 12 "])
def test_a_new_card_cannot_read_as_a_patron_number(value):
    with pytest.raises(PatronError, match="numéro de lecteur"):
        rules.check_new_card(value)


def test_a_new_card_is_normalized_or_refused():
    assert rules.check_new_card(" l-000045 ") == "L-000045"
    assert rules.check_new_card("PA-12") == "PA-12"  # seule la forme exacte d'un numéro de lecteur est refusée
    for value in ("é", "", None):
        with pytest.raises(PatronError, match="3 à 32"):
            rules.check_new_card(value)


def test_emails_are_checked_on_their_form_only():
    assert rules.clean_email(" Lea.Martin@EXAMPLE.org ") == "Lea.Martin@example.org"
    assert rules.clean_email("") is None
    for bad in ("lea", "lea@", "lea@example", "lea martin@example.org", "@example.org"):
        with pytest.raises(PatronError):
            rules.clean_email(bad)


def test_addresses_are_all_or_nothing():
    assert rules.clean_address("", "", "", "") is None
    address = rules.clean_address(" 1 rue des Lilas ", "", "69001", "Lyon")
    assert address == rules.Address("1 rue des Lilas", None, "69001", "Lyon", "FR")
    with pytest.raises(PatronError, match="rue et la commune"):
        rules.clean_address("", "", "69001", "")
    with pytest.raises(PatronError, match="cinq chiffres"):
        rules.clean_address("1 rue", "", "6900", "Lyon")
    assert rules.clean_address("10 Downing Street", "", "SW1A 2AA", "London", "gb").country == "GB"


def test_member_roles_depend_on_the_kind_of_household():
    rules.check_member_role("family", "child")
    rules.check_member_role("structure", "referent")
    with pytest.raises(PatronError):
        rules.check_member_role("structure", "child")
    with pytest.raises(PatronError):
        rules.check_member_role("famille", "member")


@pytest.mark.parametrize(
    ("minor", "guardian", "same", "status", "message"),
    [
        ("minor", "adult", True, "active", "propre responsable"),
        ("adult", "adult", False, "active", "Seul un mineur"),
        ("minor", "minor", False, "active", "un adulte"),
        ("minor", "adult", False, "anonymized", "anonymisé"),
    ],
)
def test_impossible_guardianships(minor, guardian, same, status, message):
    with pytest.raises(PatronError, match=message):
        rules.check_guardianship(minor, guardian, same, status)


def test_only_an_explicitly_authorized_and_reachable_guardian_counts():
    facts = [
        GuardianFacts(1, can_borrow_for=True, is_contact=True, reachable=True, active=True),
        GuardianFacts(2, can_borrow_for=False, is_contact=True, reachable=True, active=True),
        GuardianFacts(3, can_borrow_for=True, is_contact=False, reachable=False, active=True),
        GuardianFacts(4, can_borrow_for=True, is_contact=True, reachable=True, active=False),
    ]
    assert rules.eligible_guardians(facts) == [1]
    assert rules.eligible_guardians([]) == []


def _alone(guardian_id=1, **changes) -> GuardianFacts:
    values = {"can_borrow_for": False, "is_contact": True, "reachable": True, "active": True, "can_borrow_alone": True}
    return GuardianFacts(guardian_id, **(values | changes))


def test_borrowing_alone_requires_the_four_conditions_on_the_same_link():
    assert rules.guardians_allowing_alone([_alone()]) == [1]
    for lacking in ("can_borrow_alone", "is_contact", "reachable", "active"):
        assert rules.guardians_allowing_alone([_alone(**{lacking: False})]) == [], lacking
    # Quatre conditions réparties sur deux responsables ne font pas une autorisation
    split = [_alone(1, is_contact=False), _alone(2, can_borrow_alone=False)]
    assert rules.guardians_allowing_alone(split) == []
    assert rules.guardians_allowing_alone([]) == []


def test_the_two_rights_never_imply_each_other():
    adult_only = GuardianFacts(1, can_borrow_for=True, is_contact=True, reachable=True, active=True)
    assert rules.eligible_guardians([adult_only]) == [1] and rules.guardians_allowing_alone([adult_only]) == []
    assert rules.guardians_allowing_alone([_alone()]) == [1] and rules.eligible_guardians([_alone()]) == []


def test_a_guardian_is_needed_only_when_neither_right_is_usable():
    # Personne n'a le droit d'emprunter seul : le comportement de L3 est inchangé
    usable = GuardianFacts(1, can_borrow_for=True, is_contact=False, reachable=True, active=True)
    assert rules.needs_guardian([usable]) is False
    assert rules.needs_guardian([GuardianFacts(1, False, True, True, True)]) is True
    assert rules.needs_guardian([]) is True
    assert rules.needs_guardian([_alone()]) is False
    assert rules.needs_guardian([_alone(is_contact=False)]) is True


def test_disambiguation_marks():
    marks = Marks("P-000123", "L-000045", 2014, "Lyon", "Famille Martin", "Paul Martin")
    expected = "P-000123 · carte L-000045 · naissance 2014 · Lyon · Famille Martin · resp. Paul Martin"
    assert rules.marks_text(marks) == expected
    assert rules.marks_text(Marks("P-000001", None, None, None, None, None)) == "P-000001"
    assert rules.display_name("Léa", "Martin", "anonymized") == "Lecteur anonymisé"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, 25), ("", 25), ("abc", 25), ("-1", 25), ("0", 25), ("1.5", 25), ("1e3", 25), (" 50 ", 50), ("1", 1),
        ("25", 25), ("100", 100), ("101", 100), ("100000", 100), ("9" * 40, 25),
    ],
)  # fmt: skip
def test_page_size_is_decided_by_the_server(text, expected):
    assert rules.page_size(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [(None, 1), ("", 1), ("abc", 1), ("-3", 1), ("0", 1), ("2", 2), ("999999999", 999999999), ("9" * 40, 1)],
)
def test_page_number_is_never_invalid(text, expected):
    assert rules.page_number(text) == expected
