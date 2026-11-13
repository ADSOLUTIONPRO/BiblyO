# SPDX-License-Identifier: EUPL-1.2
"""Règles pures des prêts : une vérification par ligne du § 3 du plan de L4, à sa nature validée."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from biblyo.modules.circulation import rules
from biblyo.modules.circulation.rules import (
    BorrowerFacts,
    CancelFacts,
    Candidate,
    CirculationError,
    CopyFacts,
    LoanRule,
    Nature,
    StructureFacts,
)
from biblyo.modules.patrons.rules import GuardianFacts

TODAY = date(2026, 11, 9)
ALL = {rules.LEND, rules.RENEW, rules.OVERRIDE, rules.OVERRIDE_LIMIT, rules.HISTORY}
VOLUNTEER = {rules.LEND, rules.RENEW}


def codes(verdicts):
    return [item.code for item in verdicts]


def adult(**changes):
    return BorrowerFacts(**({"category": "adult", "status": "active"} | changes))


def minor(*guardians, **changes):
    return BorrowerFacts(**({"category": "minor", "status": "active", "guardians": guardians} | changes))


def guardian(guardian_id=1, **changes):
    values = {"can_borrow_for": True, "is_contact": True, "reachable": True, "active": True, "can_borrow_alone": False}
    return GuardianFacts(guardian_id, **(values | changes))


def loanable(**changes):
    return CopyFacts(**({"status": "available", "is_loanable": True} | changes))


# --- La nature de chaque règle est celle du § 3 -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "nature"),
    [
        ("R1", Nature.FORBIDDEN),
        ("R2", Nature.FORBIDDEN),
        ("R3", Nature.FORBIDDEN),
        ("R4", Nature.FORBIDDEN),
        ("R5", Nature.FORBIDDEN),
        ("R6", Nature.FORBIDDEN),
        ("R7", Nature.FORBIDDEN),
        ("R8", Nature.FORBIDDEN),
        ("R9", Nature.WARNING),
        ("R10", Nature.FORBIDDEN),
        ("R11", Nature.FORBIDDEN),
        ("R12", Nature.FORBIDDEN),
        ("R13", Nature.FORBIDDEN),
        ("R14", Nature.WARNING),
        ("R15", Nature.OVERRIDABLE),
        ("R16", Nature.OVERRIDABLE),
        ("R17", Nature.OVERRIDABLE),
        ("R18", Nature.WARNING),
        ("R19", Nature.WARNING),
        ("R20", Nature.WARNING),
        ("R21", Nature.INFO),
        ("R22", Nature.WARNING),
        ("R23", Nature.WARNING),
        ("R24", Nature.WARNING),
        ("R25", Nature.INFO),
        ("R26", Nature.FORBIDDEN),
        ("R27", Nature.FORBIDDEN),
        ("R28", Nature.FORBIDDEN),
    ],
)
def test_each_rule_has_its_validated_nature(code, nature):
    assert rules.RULES[code].nature is nature


def test_no_forbidden_rule_carries_a_permission():
    assert not [code for code, rule in rules.RULES.items() if rule.nature is Nature.FORBIDDEN and rule.permission]


def test_overridable_rules_name_their_permission():
    assert all(rule.permission for rule in rules.RULES.values() if rule.nature is Nature.OVERRIDABLE)


def test_a_forbidden_rule_stops_even_with_every_permission_and_a_reason():
    stopped = rules.refusals([rules.verdict("R1", due="")], ALL | {"*"}, has_reason=True)
    assert codes(stopped) == ["R1"]


def test_an_overridable_rule_needs_the_permission_and_a_reason():
    item = [rules.verdict("R15", maximum=1)]
    assert codes(rules.refusals(item, VOLUNTEER, has_reason=True)) == ["R15"]
    assert codes(rules.refusals(item, ALL, has_reason=False)) == ["R15"]
    assert rules.refusals(item, ALL, has_reason=True) == []


def test_a_warning_never_stops():
    assert rules.refusals([rules.verdict("R23"), rules.verdict("R19")], set(), has_reason=False) == []


# --- R1 à R3, R22 : l'exemplaire --------------------------------------------------------------------------------------
def test_r1_a_copy_on_loan_gives_its_due_date_and_no_holder():
    found = rules.copy_verdicts(loanable(on_loan=True, due_on="2026-12-01"))
    assert codes(found) == ["R1"] and "01/12/2026" in found[0].message


def test_r2_reference_only():
    assert codes(rules.copy_verdicts(loanable(is_loanable=False))) == ["R2"]


@pytest.mark.parametrize("status", ["in_processing", "in_repair", "missing", "lost", "withdrawn"])
def test_r3_copy_status(status):
    found = rules.copy_verdicts(loanable(status=status))
    assert codes(found) == ["R3"] and found[0].nature is Nature.FORBIDDEN


@pytest.mark.parametrize("facts", [{"condition": "damaged"}, {"condition_note": "page 12 déchirée"}])
def test_r22_a_condition_warns_and_does_not_stop(facts):
    found = rules.copy_verdicts(loanable(**facts))
    assert codes(found) == ["R22"] and rules.refusals(found, set(), has_reason=False) == []


def test_a_copy_in_good_condition_says_nothing():
    assert rules.copy_verdicts(loanable(condition="good")) == []


# --- R4, R10, R11 : résolution d'un code ------------------------------------------------------------------------------
def test_r4_unknown_code():
    outcome = rules.resolve([])
    assert outcome.picked is None and outcome.verdict.code == "R4"


def test_one_result_in_one_type_is_taken():
    only = Candidate(rules.KIND_COPY, 7)
    assert rules.resolve([only]).picked == only


def test_the_same_copy_found_twice_is_one_result():
    assert rules.resolve([Candidate(rules.KIND_COPY, 7), Candidate(rules.KIND_COPY, 7)]).picked is not None


@pytest.mark.parametrize(
    "found",
    [
        [Candidate(rules.KIND_CARD, 1), Candidate(rules.KIND_COPY, 7)],  # un code, deux types
        [Candidate(rules.KIND_CARD, 1), Candidate(rules.KIND_PATRON, 1)],  # deux types, la même fiche
        [Candidate(rules.KIND_COPY, 7), Candidate(rules.KIND_COPY, 8)],  # un ISBN, plusieurs exemplaires
        [Candidate(rules.KIND_NAME, 1), Candidate(rules.KIND_NAME, 2)],  # homonymes
    ],
)
def test_r10_every_ambiguity_requires_a_selection(found):
    outcome = rules.resolve(found)
    assert outcome.picked is None and outcome.verdict.code == "R10" and len(outcome.choices) == len(found)


def test_r11_an_ended_card_is_refused_as_such():
    outcome = rules.resolve([Candidate(rules.KIND_ENDED_CARD, 1)])
    assert outcome.picked is None and outcome.verdict.code == "R11" and outcome.choices == ()


# --- R5, R6, R9, R18, R19 : le lecteur --------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["archived", "anonymized"])
def test_r5_withdrawn_reader(status):
    assert codes(rules.borrower_verdicts(adult(status=status), TODAY)) == ["R5"]


def test_r6_suspended_reader():
    assert codes(rules.borrower_verdicts(adult(status="suspended"), TODAY)) == ["R6"]


def test_r9_majority_to_confirm_warns_and_stays_a_minor():
    facts = minor(birth="2008")
    assert codes(rules.borrower_verdicts(facts, TODAY)) == ["R9"]
    assert codes(rules.decide_borrowing(facts, rules.MODE_WITH_ADULT)[1]) == ["R7"]  # R7 s'applique


def test_r18_overdues_warn_without_titles():
    found = rules.borrower_verdicts(adult(overdue=2, responsible_overdue=1), TODAY)
    assert codes(found) == ["R18"] and found[0].nature is Nature.WARNING
    assert "2" in found[0].message and "1" in found[0].message


def test_r18_a_threshold_makes_it_overridable_never_forbidden():
    found = rules.borrower_verdicts(adult(overdue=2), TODAY, overdue_block=2)
    assert found[0].nature is Nature.OVERRIDABLE and found[0].permission == rules.OVERRIDE
    assert rules.borrower_verdicts(adult(overdue=1), TODAY, overdue_block=2)[0].nature is Nature.WARNING


def test_r19_expired_membership_warns():
    assert codes(rules.borrower_verdicts(adult(membership_expires_on="2026-11-08"), TODAY)) == ["R19"]
    assert rules.borrower_verdicts(adult(membership_expires_on="2026-11-09"), TODAY) == []


# --- R7, R8, R21 : les mineurs (D1, P1) -------------------------------------------------------------------------------
def test_r7_minor_without_any_guardian():
    assert codes(rules.decide_borrowing(minor(), rules.MODE_WITH_ADULT, chosen_guardian=1)[1]) == ["R7"]
    assert codes(rules.decide_borrowing(minor(), rules.MODE_ALONE)[1]) == ["R7"]


def test_r7_a_minor_cannot_borrow_for_himself():
    assert codes(rules.decide_borrowing(minor(guardian()), rules.MODE_SELF)[1]) == ["R7"]


@pytest.mark.parametrize("lacking", ["can_borrow_for", "reachable", "active"])
def test_r7_no_usable_adult(lacking):
    assert codes(
        rules.decide_borrowing(minor(guardian(**{lacking: False})), rules.MODE_WITH_ADULT, chosen_guardian=1)[1]
    ) == ["R7"]


def test_r8_chosen_adult_is_not_authorized_on_this_link():
    facts = minor(guardian(1), guardian(2, can_borrow_for=False))
    assert codes(rules.decide_borrowing(facts, rules.MODE_WITH_ADULT, chosen_guardian=2)[1]) == ["R8"]
    assert codes(rules.decide_borrowing(facts, rules.MODE_WITH_ADULT, chosen_guardian=99)[1]) == ["R8"]


def test_an_adult_must_still_be_confirmed_even_when_alone_on_the_list():
    facts = minor(guardian(1))
    assert rules.minor_options(facts.guardians).preselected == 1
    assert codes(rules.decide_borrowing(facts, rules.MODE_WITH_ADULT)[1]) == ["R8"]


def test_two_guardians_with_different_rights():
    facts = minor(guardian(1), guardian(2, can_borrow_for=False, can_borrow_alone=True))
    assert rules.minor_options(facts.guardians).adults == (1,)
    assert rules.minor_options(facts.guardians).alone_by == 2
    assert rules.minor_options(facts.guardians).preselected == 1


def test_a_usable_adult_answers_for_the_loan():
    borrowing, found = rules.decide_borrowing(minor(guardian(5)), rules.MODE_WITH_ADULT, chosen_guardian=5)
    assert borrowing == rules.Borrowing(rules.MODE_WITH_ADULT, responsible_id=5) and found == []


def test_r21_alone_with_the_four_conditions():
    facts = minor(guardian(9, can_borrow_for=False, can_borrow_alone=True))
    borrowing, found = rules.decide_borrowing(facts, rules.MODE_ALONE)
    assert borrowing == rules.Borrowing(rules.MODE_ALONE, responsible_id=9) and codes(found) == ["R21"]


@pytest.mark.parametrize("lacking", ["can_borrow_alone", "is_contact", "reachable", "active"])
def test_r21_refused_without_one_of_the_four_conditions(lacking):
    facts = minor(guardian(can_borrow_for=False, **({"can_borrow_alone": True} | {lacking: False})))
    assert codes(rules.decide_borrowing(facts, rules.MODE_ALONE)[1]) == ["R7"]


def test_the_two_rights_never_imply_each_other():
    assert codes(rules.decide_borrowing(minor(guardian()), rules.MODE_ALONE)[1]) == ["R7"]
    only_alone = minor(guardian(can_borrow_for=False, can_borrow_alone=True))
    assert codes(rules.decide_borrowing(only_alone, rules.MODE_WITH_ADULT, chosen_guardian=1)[1]) == ["R7"]


def test_an_adult_borrows_for_himself_only_in_that_frame():
    assert rules.decide_borrowing(adult(), rules.MODE_SELF) == (rules.Borrowing(rules.MODE_SELF), [])
    assert codes(rules.decide_borrowing(adult(), rules.MODE_WITH_ADULT)[1]) == ["R8"]


# --- R13 : les structures (D2) ----------------------------------------------------------------------------------------
def structure(**changes):
    values = {"household_id": 4, "is_structure": True, "archived": False, "can_borrow_for_structure": True}
    return StructureFacts(**(values | changes))


def test_an_authorized_adult_borrows_for_the_structure():
    borrowing, found = rules.decide_borrowing(adult(), rules.MODE_STRUCTURE, structure=structure())
    assert borrowing == rules.Borrowing(rules.MODE_STRUCTURE, household_id=4) and found == []


@pytest.mark.parametrize("changes", [{"can_borrow_for_structure": False}, {"is_structure": False}, {"archived": True}])
def test_r13_structure_refused(changes):
    assert codes(rules.decide_borrowing(adult(), rules.MODE_STRUCTURE, structure=structure(**changes))[1]) == ["R13"]


def test_r13_without_a_structure_or_for_a_minor():
    assert codes(rules.decide_borrowing(adult(), rules.MODE_STRUCTURE)[1]) == ["R13"]
    assert codes(rules.decide_borrowing(minor(guardian()), rules.MODE_STRUCTURE, structure=structure())[1]) == ["R13"]


# --- R14, R20 : les plafonds (D3) -------------------------------------------------------------------------------------
def test_r14_warns_by_default_and_lets_the_loan_go():
    found = rules.limit_verdicts(5, 1, 5, rules.LIMIT_WARN)
    assert codes(found) == ["R14"] and found[0].nature is Nature.WARNING
    assert rules.refusals(found, set(), has_reason=False) == []


def test_r14_blocking_mode_is_overridable_with_its_own_permission():
    found = rules.limit_verdicts(5, 1, 5, rules.LIMIT_BLOCK)
    assert found[0].nature is Nature.OVERRIDABLE and found[0].permission == rules.OVERRIDE_LIMIT
    assert codes(rules.refusals(found, {rules.OVERRIDE}, has_reason=True)) == ["R14"]
    assert rules.refusals(found, {rules.OVERRIDE_LIMIT}, has_reason=True) == []


def test_r14_counts_the_whole_batch():
    assert codes(rules.limit_verdicts(3, 3, 5, rules.LIMIT_WARN)) == ["R14"]


@pytest.mark.parametrize(("active", "adding"), [(3, 1), (4, 1)])
def test_r20_nearly_reached(active, adding):
    found = rules.limit_verdicts(active, adding, 5, rules.LIMIT_BLOCK)
    assert codes(found) == ["R20"] and found[0].nature is Nature.WARNING


def test_no_limit_says_nothing():
    assert rules.limit_verdicts(1, 1, 5, rules.LIMIT_WARN) == []
    assert rules.limit_verdicts(100, 1, None, rules.LIMIT_BLOCK) == []


# --- R15, R23 : renouvellements (D7) ----------------------------------------------------------------------------------
def test_r15_renewal_limit_is_overridable():
    found = rules.renewal_verdicts(1, 1, TODAY + timedelta(days=4), TODAY)
    assert codes(found) == ["R15"] and found[0].permission == rules.OVERRIDE


def test_r23_renewing_an_overdue_loan_warns():
    found = rules.renewal_verdicts(0, 1, TODAY - timedelta(days=1), TODAY)
    assert codes(found) == ["R23"] and rules.refusals(found, set(), has_reason=False) == []


def test_a_renewal_counts_from_the_due_date_or_from_today_when_late():
    assert rules.renewed_due(date(2026, 11, 20), TODAY, 21) == date(2026, 12, 11)
    assert rules.renewed_due(date(2026, 11, 1), TODAY, 21) == date(2026, 11, 30)
    assert rules.renewed_due(TODAY, TODAY, 21) == date(2026, 11, 30)


# --- R16, R17, R24 : les dates (D8, D12) ------------------------------------------------------------------------------
def test_due_date():
    assert rules.due_date(TODAY, 28) == date(2026, 12, 7)


def test_r16_backdating_is_overridable():
    found = rules.date_verdicts(
        TODAY - timedelta(days=1), TODAY + timedelta(days=5), TODAY + timedelta(days=27), TODAY, None
    )
    assert codes(found) == ["R16"] and found[0].permission == rules.OVERRIDE


def test_r17_a_longer_loan_is_overridable_a_shorter_one_is_free():
    rule_due = TODAY + timedelta(days=28)
    assert codes(rules.date_verdicts(TODAY, rule_due + timedelta(days=1), rule_due, TODAY, None)) == ["R17"]
    assert rules.date_verdicts(TODAY, rule_due - timedelta(days=10), rule_due, TODAY, None) == []
    assert rules.date_verdicts(TODAY, rule_due, rule_due, TODAY, None) == []


def test_r24_closing_day_warns():
    due = date(2026, 11, 16)  # un lundi
    found = rules.date_verdicts(TODAY, due, due, TODAY, 0)
    assert codes(found) == ["R24"] and found[0].nature is Nature.WARNING
    assert rules.date_verdicts(TODAY, due, due, TODAY, 1) == []


def test_a_due_date_must_follow_the_loan():
    with pytest.raises(CirculationError):
        rules.check_dates(TODAY, TODAY)
    rules.check_dates(TODAY, TODAY + timedelta(days=1))


# --- R25 : retour, et retards (D4, PA-7) ------------------------------------------------------------------------------
def test_r25_nothing_to_return_is_not_an_error():
    found = rules.nothing_to_return()
    assert found.code == "R25" and found.nature is Nature.INFO


def test_overdue_the_day_after_the_due_date():
    assert (
        rules.is_overdue(TODAY - timedelta(days=1), TODAY) and rules.days_overdue(TODAY - timedelta(days=3), TODAY) == 3
    )
    assert not rules.is_overdue(TODAY, TODAY) and rules.days_overdue(TODAY, TODAY) == 0
    assert not rules.is_overdue(TODAY + timedelta(days=1), TODAY)


def test_due_soon():
    assert rules.is_due_soon(TODAY, TODAY) and rules.is_due_soon(TODAY + timedelta(days=3), TODAY)
    assert not rules.is_due_soon(TODAY + timedelta(days=4), TODAY)
    assert not rules.is_due_soon(TODAY - timedelta(days=1), TODAY)


# --- R26 et D9 : annulation -------------------------------------------------------------------------------------------
def cancel(minutes=5, **changes):
    return CancelFacts(**({"elapsed": timedelta(minutes=minutes), "own": True} | changes))


def test_the_author_cancels_within_fifteen_minutes():
    decision = rules.cancel_decision(cancel(14), VOLUNTEER)
    assert decision.allowed and not decision.needs_reason


@pytest.mark.parametrize("facts", [cancel(16), cancel(15), cancel(5, own=False)])
def test_a_volunteer_is_refused_late_or_on_someone_elses_operation(facts):
    decision = rules.cancel_decision(facts, VOLUNTEER)
    assert not decision.allowed and decision.verdict.code == "D9"


@pytest.mark.parametrize("facts", [cancel(16), cancel(5, own=False)])
def test_a_librarian_cancels_with_a_reason(facts):
    decision = rules.cancel_decision(facts, ALL)
    assert decision.allowed and decision.needs_reason


@pytest.mark.parametrize("dependent", ["relent", "returned", "renewed", "reminded"])
def test_r26_a_dependent_operation_forbids_it_for_everyone(dependent):
    decision = rules.cancel_decision(cancel(1, **{dependent: True}), ALL)
    assert not decision.allowed and decision.verdict.code == "R26"


# --- Motifs et rappels ------------------------------------------------------------------------------------------------
def test_a_listed_reason_needs_no_text_and_drops_any():
    assert rules.clean_reason("late_entry", "ignoré", rules.OVERRIDE_REASONS) == ("late_entry", None)


def test_other_needs_a_short_text():
    assert rules.clean_reason("other", "  livre  rare ", rules.OVERRIDE_REASONS) == ("other", "livre rare")
    for code, text in (("other", ""), ("other", "x" * 121), ("nope", "texte"), (None, "texte")):
        with pytest.raises(CirculationError):
            rules.clean_reason(code, text, rules.OVERRIDE_REASONS)


@pytest.mark.parametrize("text", ["pour Durand", "contact a@example.org", "appeler 06123456"])
def test_a_reason_refuses_names_emails_and_numbers(text):
    with pytest.raises(CirculationError, match="journal"):
        rules.clean_reason("other", text, rules.CANCEL_REASONS, names=["Anne Durand"])


def test_reminders_are_structured_values_only():
    rules.check_reminder("phone", "reached")
    for channel, result in (("sms", "reached"), ("phone", "note libre"), (None, None)):
        with pytest.raises(CirculationError):
            rules.check_reminder(channel, result)


# --- R12 : verdict, R27, R28 : ce qui se voit -------------------------------------------------------------------------
def test_r12_concurrent_change_is_forbidden():
    assert rules.verdict("R12").nature is Nature.FORBIDDEN


def test_r27_the_borrower_requires_history():
    assert not rules.can_see_borrower(VOLUNTEER) and rules.can_see_borrower({rules.HISTORY})


def test_r28_counter_loans_require_lend_or_renew():
    assert rules.can_view_counter_loans({rules.LEND}) and rules.can_view_counter_loans({rules.RENEW})
    assert not rules.can_view_counter_loans({rules.HISTORY})


# --- Choix de la règle (D2 bis) ---------------------------------------------------------------------------------------
DEFAULTS = [
    LoanRule(None, "adult", 28, 28, 1, 5),
    LoanRule(None, "minor", 21, 21, 1, 3),
    LoanRule(None, "structure", 42, 42, 1, 15),
]


def test_each_category_has_its_rule():
    assert rules.pick_rule(DEFAULTS, 2, "adult").loan_days == 28
    assert rules.pick_rule(DEFAULTS, None, "minor").max_active_loans == 3


def test_the_most_precise_rule_wins():
    both = LoanRule(2, "adult", 7, 7, 0, 1)
    by_type = LoanRule(2, None, 14, 14, 0, 2)
    assert rules.pick_rule([*DEFAULTS, by_type, both], 2, "adult") is both
    assert rules.pick_rule([*DEFAULTS, by_type], 2, "adult") is by_type
    assert rules.pick_rule([*DEFAULTS, by_type], 3, "adult").loan_days == 28


def test_a_structure_has_its_own_duration_and_limit():
    assert rules.rule_category("adult", rules.MODE_STRUCTURE) == "structure"
    assert rules.rule_category("minor", rules.MODE_ALONE) == "minor"


def test_a_missing_rule_is_reported():
    with pytest.raises(CirculationError, match="règle"):
        rules.pick_rule([], None, "adult")


def test_a_recorded_reason_reads_as_chosen():
    assert rules.reason_label("wrong_due") == "Échéance erronée"
    assert rules.reason_label("librarian_decision") == "Décision du bibliothécaire"
    assert rules.reason_label(None) is None
