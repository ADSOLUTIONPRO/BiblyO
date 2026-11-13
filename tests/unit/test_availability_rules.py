# SPDX-License-Identifier: EUPL-1.2
"""Disponibilité de circulation : quatre états exclusifs, déduits, et des compteurs sans double compte."""

from __future__ import annotations

from datetime import date

import pytest

from biblyo.modules.holdings.rules import (
    STATE_AVAILABLE,
    STATE_GONE,
    STATE_ON_LOAN,
    STATE_OVERDUE,
    STATE_UNAVAILABLE,
    copy_state,
    count_states,
)

TODAY = date(2026, 11, 10)


@pytest.mark.parametrize(
    ("status", "loanable", "due", "state", "label"),
    [
        ("available", True, None, STATE_AVAILABLE, "Disponible"),
        ("available", True, "2026-12-01", STATE_ON_LOAN, "Emprunté · retour le 01/12/2026"),
        ("available", True, "2026-11-10", STATE_ON_LOAN, "Emprunté · retour le 10/11/2026"),  # le jour même : à l'heure
        ("available", True, "2026-11-09", STATE_OVERDUE, "En retard · 1 jour"),
        ("available", True, "2026-11-07", STATE_OVERDUE, "En retard · 3 jours"),
        ("in_repair", True, None, STATE_UNAVAILABLE, "Indisponible · en réparation"),
        ("in_processing", True, None, STATE_UNAVAILABLE, "Indisponible · en préparation"),
        ("missing", True, None, STATE_UNAVAILABLE, "Indisponible · introuvable"),
        ("available", False, None, STATE_UNAVAILABLE, "Consultation sur place"),
        ("withdrawn", True, None, STATE_GONE, "Retiré"),
        ("lost", True, None, STATE_GONE, "Perdu"),
    ],
)
def test_each_copy_has_one_state_and_a_plain_label(status, loanable, due, state, label):
    labels = {"in_repair": "En réparation", "in_processing": "En préparation", "missing": "Introuvable"}
    labels |= {"withdrawn": "Retiré", "lost": "Perdu", "available": "En place"}
    found = copy_state(status, loanable, due, TODAY, labels[status])
    assert found.state == state and found.label == label


def test_a_loan_wins_over_everything_and_never_names_anyone():
    found = copy_state("available", False, "2026-12-01", TODAY)
    assert found.state == STATE_ON_LOAN and found.is_out and not found.is_loanable_now
    assert set(vars(found)) == {"state", "reason", "due_on", "days_late"}  # aucun champ pour un emprunteur


def test_counts_are_exclusive_and_add_up_to_the_copies_in_the_collection():
    states = [
        copy_state("available", True, None, TODAY),
        copy_state("available", True, None, TODAY),
        copy_state("available", True, "2026-12-01", TODAY),
        copy_state("available", True, "2026-11-01", TODAY),
        copy_state("in_repair", True, None, TODAY, "en réparation"),
        copy_state("available", False, None, TODAY),
        copy_state("withdrawn", True, None, TODAY, "retiré"),
    ]
    counts = count_states(states)
    assert (counts.available, counts.on_loan, counts.overdue, counts.unavailable, counts.gone) == (2, 1, 1, 2, 1)
    assert counts.total == 6 == len(states) - counts.gone  # « en retard » n'est pas compté aussi dans « emprunté »
    assert counts.segments == [
        ("2 disponibles", "ok"),
        ("1 emprunté", "info"),
        ("1 en retard", "late"),
        ("2 indisponibles", "neutral"),
    ]
    assert counts.headline == ("2 disponibles", "ok") and counts.next_due == "2026-11-01"


@pytest.mark.parametrize(
    ("states", "headline"),
    [
        ([], ("Aucun exemplaire", "neutral")),
        ([("available", True, None)], ("1 disponible", "ok")),
        ([("available", True, "2026-12-01")], ("Emprunté · retour le 01/12", "info")),
        ([("available", True, "2026-12-01"), ("available", True, "2026-12-03")], ("Aucun disponible", "info")),
        ([("available", True, "2026-11-01")], ("En retard", "late")),
        ([("available", True, "2026-11-01"), ("available", True, "2026-11-02")], ("Tous sortis, en retard", "late")),
        ([("available", False, None)], ("Consultation sur place", "neutral")),
        ([("in_repair", True, None)], ("Indisponible", "neutral")),
        ([("withdrawn", True, None)], ("Aucun exemplaire", "neutral")),
    ],
)
def test_the_headline_of_a_card(states, headline):
    counts = count_states(copy_state(status, loanable, due, TODAY, status) for status, loanable, due in states)
    assert counts.headline == headline
