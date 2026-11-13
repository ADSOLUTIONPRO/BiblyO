# SPDX-License-Identifier: EUPL-1.2
"""Le compteur rapide d'une série dit exactement ce que montre sa frise, sur des milliers de cas tirés au sort."""

from __future__ import annotations

import random

import pytest

from biblyo.modules.catalogue.rules import series_strip
from biblyo.modules.catalogue.series_view import Tally, tally

NUMBERS = [0.0, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 7.5, 10.0, 12.0]


def _expected(links, confirmed):
    numbers = {number for _work, number, _label in links if number is not None}
    labels = {label for _work, number, label in links if number is None and label}
    strip = series_strip(numbers, set(confirmed))
    return (
        sum(1 for item in strip if item.state == "present") + len(labels),
        sum(1 for item in strip if item.state == "missing"),
        sum(1 for item in strip if item.state == "unverified"),
    )


@pytest.mark.parametrize("seed", range(40))
def test_tally_matches_the_strip(seed):
    rng = random.Random(seed)
    for _case in range(100):
        links = []
        for work in range(rng.randint(0, 8)):
            kind = rng.random()
            number = rng.choice(NUMBERS) if kind < 0.8 else None
            label = rng.choice(["HS 1", "HS 2", "Intégrale"]) if number is None and kind < 0.95 else None
            links.append((work, number, label))
        confirmed = set(rng.sample(NUMBERS, rng.randint(0, 4)))
        counts = tally(links, confirmed, None)
        assert (counts.present, counts.missing, counts.unverified) == _expected(links, confirmed), (links, confirmed)
        assert counts.has_duplicates is None


def test_duplicates_come_from_several_works_or_a_multiple_work():
    assert tally([(1, 1.0, None), (2, 1.0, None)], (), lambda _w: False).has_duplicates
    assert tally([(1, None, "HS 1"), (2, None, "HS 1")], (), lambda _w: False).has_duplicates
    assert tally([(1, 1.0, None)], (), lambda work: work == 1).has_duplicates
    assert not tally([(1, 1.0, None), (2, 2.0, None)], (), lambda _w: False).has_duplicates


@pytest.mark.parametrize(
    ("counts", "state"),
    [
        (Tally(3, 0, 0, 3), "complete"),
        (Tally(2, 0, 1, 3), "uncertain"),
        (Tally(2, 1, 1, 4), "uncertain"),
        (Tally(2, 1, 0, 3), "missing"),
        (Tally(0, 0, 0, 0), "empty"),
    ],
)
def test_states(counts, state):
    assert counts.state == state
    assert counts.is_complete is (state == "complete")
