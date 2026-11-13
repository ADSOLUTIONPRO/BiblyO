# SPDX-License-Identifier: EUPL-1.2
"""Règles pures de L2 : hiérarchie, positions, capacité, cote, codes, statuts, dessin des dos."""

from __future__ import annotations

import random

import pytest

from biblyo.modules.holdings import rules
from biblyo.modules.holdings.rules import CallFacts, Fill, GridBox, LocationError, SortFacts


@pytest.mark.parametrize(
    ("kind", "parent"),
    [
        ("site", None),
        ("room", None),
        ("room", "site"),
        ("furniture", "room"),
        ("shelf", "furniture"),
        ("bin", "furniture"),
        ("virtual", None),
        ("virtual", "site"),
        ("virtual", "room"),
    ],
)
def test_allowed_parents(kind, parent):
    rules.check_parent(kind, parent)


@pytest.mark.parametrize(
    ("kind", "parent"),
    [
        ("site", "site"),
        ("room", "room"),
        ("room", "furniture"),
        ("furniture", None),
        ("furniture", "site"),
        ("furniture", "furniture"),
        ("shelf", "room"),
        ("shelf", "shelf"),
        ("shelf", None),
        ("bin", "shelf"),
        ("virtual", "furniture"),
        ("virtual", "shelf"),
        ("inconnu", None),
        ("room", "inconnu"),
    ],
)
def test_incoherent_parents_are_refused(kind, parent):
    with pytest.raises(LocationError):
        rules.check_parent(kind, parent)


def test_depth_is_bounded():
    assert [rules.depth_for(value) for value in (None, 0, 1, 2)] == [0, 1, 2, 3]
    with pytest.raises(LocationError):
        rules.depth_for(3)


def test_a_location_cannot_go_inside_itself():
    rules.check_no_cycle(5, [3, 1])
    for chain in ([5], [7, 5, 1]):
        with pytest.raises(LocationError):
            rules.check_no_cycle(5, chain)


@pytest.mark.parametrize(("value", "expected"), [("bd a", "BD-A"), (" bd-1 ", "BD-1"), ("", None), (None, None)])
def test_location_codes(value, expected):
    assert rules.clean_location_code(value) == expected


@pytest.mark.parametrize("value", ["é", "BD_A", "-A", "X" * 17])
def test_bad_location_codes(value):
    with pytest.raises(LocationError):
        rules.clean_location_code(value)


def test_capacity_is_optional_and_bounded():
    assert rules.clean_capacity("") is None
    assert rules.clean_capacity("0") == 0
    assert rules.clean_capacity(30) == 30
    for value in ("-1", "abc", "2001", "3.5"):
        with pytest.raises(LocationError):
            rules.clean_capacity(value)


@pytest.mark.parametrize(
    ("count", "capacity", "state", "excess", "free"),
    [
        (3, None, "none", 0, 0),
        (3, 10, "ok", 0, 7),
        (10, 10, "full", 0, 0),
        (12, 10, "over", 2, 0),
        (1, 0, "over", 1, 0),
    ],
)
def test_fill_states(count, capacity, state, excess, free):
    fill = Fill(count, capacity)
    assert (fill.state, fill.excess, fill.free) == (state, excess, free)


def test_grid_boxes():
    room = [GridBox(1, 1, 2, 1), GridBox(3, 1)]
    assert rules.grid_overlaps(GridBox(1, 2), room)
    assert not rules.grid_overlaps(GridBox(2, 2), room)
    rules.check_grid_box(GridBox(40, 40))
    for box in (GridBox(0, 1), GridBox(1, 40, 2, 1), GridBox(1, 1, 0, 1)):
        with pytest.raises(LocationError):
            rules.check_grid_box(box)


def test_insert_move_remove():
    assert rules.insert([1, 2, 3], [9], 1) == [9, 1, 2, 3]
    assert rules.insert([1, 2, 3], [9], 2) == [1, 9, 2, 3]
    assert rules.insert([1, 2, 3], [9]) == [1, 2, 3, 9]
    assert rules.insert([1, 2, 3], [9], 99) == [1, 2, 3, 9]
    assert rules.insert([1, 2, 3], [8, 9], 2) == [1, 8, 9, 2, 3]
    assert rules.move_to([1, 2, 3], 3, 1) == [3, 1, 2]
    assert rules.move_to([1, 2, 3], 1, 3) == [2, 3, 1]
    assert rules.remove([1, 2, 3], 2) == [1, 3]
    assert rules.neighbours([1, 2, 3], 2) == (1, 3)
    assert rules.neighbours([1, 2, 3], 1) == (None, 2)
    with pytest.raises(ValueError):
        rules.insert([1], [2, 2])


@pytest.mark.parametrize("seed", range(20))
def test_positions_stay_contiguous_after_any_sequence(seed):
    """Des milliers d'opérations tirées au sort : jamais de trou, jamais de doublon, rien de perdu."""
    rng = random.Random(seed)
    shelves: dict[str, list[int]] = {"a": [], "b": [], "c": []}
    unshelved = set(range(60))
    for _step in range(300):
        action = rng.random()
        if action < 0.4 and unshelved:
            item = rng.choice(sorted(unshelved))
            target = rng.choice(list(shelves))
            shelves[target] = rules.insert(shelves[target], [item], rng.randint(0, len(shelves[target]) + 3))
            unshelved.discard(item)
        elif action < 0.7:
            source = rng.choice(list(shelves))
            if shelves[source]:
                item = rng.choice(shelves[source])
                target = rng.choice(list(shelves))
                shelves[source] = rules.remove(shelves[source], item)
                shelves[target] = rules.insert(shelves[target], [item], rng.randint(1, len(shelves[target]) + 1))
        elif action < 0.9:
            source = rng.choice(list(shelves))
            if shelves[source]:
                item = rng.choice(shelves[source])
                shelves[source] = rules.move_to(shelves[source], item, rng.randint(1, len(shelves[source])))
        else:
            source = rng.choice(list(shelves))
            if shelves[source]:
                item = rng.choice(shelves[source])
                shelves[source] = rules.remove(shelves[source], item)
                unshelved.add(item)
        placed = [item for order in shelves.values() for item in order]
        assert len(placed) == len(set(placed))
        assert set(placed) | unshelved == set(range(60))
        assert not set(placed) & unshelved


def test_sorts_are_stable_and_complete():
    facts = [
        SortFacts(1, "zorro"),
        SortFacts(2, "asterix 3", "asterix", 3.0),
        SortFacts(3, "asterix 1", "asterix", 1.0),
        SortFacts(4, "asterix hs", "asterix", None, "HS 1"),
        SortFacts(5, "asterix 1 bis", "asterix", 1.0),
        SortFacts(6, "agent 212", None, None, None, "BD AGE 1"),
    ]
    assert rules.sorted_order(facts, "serie") == [3, 5, 2, 4, 6, 1]
    assert rules.sorted_order(facts, "titre") == [6, 3, 5, 2, 4, 1]
    assert rules.sorted_order(facts, "cote")[0] == 6
    assert rules.moved_count([1, 2, 3], [1, 3, 2]) == 2
    with pytest.raises(ValueError):
        rules.sorted_order(facts, "inconnu")


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (CallFacts("bd", "Astérix le Gaulois", "Astérix", None, 1), "BD AST 1"),
        (CallFacts("bd", "Tome 12", "Astérix", "ASX", 14), "BD ASX 14"),
        (CallFacts("manga", "Un", "Manga d’essai", None, None), "MAN MAN"),
        (CallFacts("livre", "Le Cœur", author_sort_name="martin jeanne"), "R MAR"),
        (CallFacts("documentaire", "Atlas sans auteur"), "DOC ATL"),
        (CallFacts("inconnu", "Éléphant"), "X ELE"),
    ],
)
def test_call_number_proposal(facts, expected):
    assert rules.propose_call_number(facts) == expected


def test_call_number_ignores_the_commercial_volume():
    """Le tome 12 rangé en 14e position de série : la cote porte le numéro interne, pas le tome."""
    assert rules.propose_call_number(CallFacts("bd", "Tome 12", "Lucky Luke", None, 14)).endswith(" 14")


def test_call_number_cleaning():
    assert rules.clean_call_number(" bd  ast 1 ") == "BD AST 1"
    assert rules.clean_call_number("") is None
    with pytest.raises(LocationError):
        rules.clean_call_number("X" * 41)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("EX-000042", "EX-000042"), ("ex-000042", "EX-000042"), (" csn 000123 ", "CSN000123"), ("A1B", "A1B")],
)
def test_codes_are_normalized(value, expected):
    assert rules.normalize_code(value) == expected


@pytest.mark.parametrize("value", ["", None, "AB", "-AB", "AB-", "É12", "A_B1", "X" * 33, "AB/12"])
def test_invalid_codes(value):
    assert rules.normalize_code(value) is None


@pytest.mark.parametrize(
    ("status", "first_time", "keep", "expected"),
    [
        ("in_processing", True, False, "available"),
        ("in_processing", True, True, "in_processing"),
        ("in_processing", False, False, "in_processing"),
        ("available", False, False, "available"),
        ("in_repair", True, False, "in_repair"),
    ],
)
def test_status_after_shelving(status, first_time, keep, expected):
    assert rules.status_after_shelving(status, first_time=first_time, keep_processing=keep) == expected


@pytest.mark.parametrize("status", ["missing", "lost", "withdrawn"])
def test_absent_copies_cannot_be_shelved(status):
    with pytest.raises(LocationError, match="retrouvé"):
        rules.status_after_shelving(status, first_time=True)


def test_availability_is_deduced():
    assert rules.availability("available", True) == "available"
    assert rules.availability("available", False) == "on_site"
    for status in ("in_processing", "in_repair", "missing", "lost", "withdrawn"):
        assert rules.availability(status, True) == "unavailable"


def test_spines_are_bounded_illustrations():
    assert rules.spine_width(None, "manga") == 3
    assert rules.spine_width(None, "inconnu") == 3
    assert [rules.spine_width(pages, "livre") for pages in (10, 48, 49, 300, 5000)] == [2, 2, 2, 5, 8]
    assert [rules.spine_width(pages, "revue") for pages in (10, 60)] == [1, 2]
    assert rules.spine_width(48, "bd") == 4  # un album cartonné n'est jamais aussi fin qu'une revue
    assert rules.spine_width(700, "bd") == 7
    assert rules.spine_height("bd") == "tall" and rules.spine_height("manga") == "short"
    assert rules.spine_height("inconnu") == "mid"


# --- Changements de statut (D7) ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "action", "target"),
    [
        ("available", "repair", "in_repair"),
        ("in_repair", "repaired", "available"),
        ("available", "missing", "missing"),
        ("missing", "lost", "lost"),
        ("missing", "found", "available"),
        ("lost", "found", "available"),
        ("lost", "withdraw", "withdrawn"),
        ("withdrawn", "reinstate", "in_processing"),
    ],
)
def test_status_actions(status, action, target):
    assert rules.status_after_action(status, action) == target


@pytest.mark.parametrize(
    ("status", "action"),
    [("withdrawn", "withdraw"), ("available", "found"), ("in_processing", "repaired"), ("lost", "repair")],
)
def test_impossible_status_actions(status, action):
    with pytest.raises(LocationError, match="Impossible depuis"):
        rules.status_after_action(status, action)


def test_no_status_becomes_available_by_itself():
    """Seules deux déclarations explicites rendent un exemplaire « En place » : retrouvé et réparé."""
    reaching = {action for action, (_allowed, target) in rules.STATUS_ACTIONS.items() if target == "available"}
    assert reaching == {"found", "repaired"}
    assert sorted(rules.UNSHELVING_ACTIONS) == ["lost", "withdraw"]
    assert rules.actions_for("withdrawn") == ["reinstate"]
    assert set(rules.STATUS_ACTION_LABELS) == set(rules.STATUS_ACTIONS)
    with pytest.raises(LocationError):
        rules.status_after_action("available", "teleport")


# --- Acquisition ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("2026", "2026"),
        ("2026-3", "2026-03"),
        ("2026-03-14", "2026-03-14"),
        ("14/03/2026", "2026-03-14"),
        ("3/2026", "2026-03"),
        ("29.02.2024", "2024-02-29"),
    ],
)
def test_partial_dates(text, expected):
    assert rules.clean_partial_date(text) == expected


@pytest.mark.parametrize("text", ["hier", "inf. 2026", "1799", "2026-13", "31/02/2026", "2026-02-30", "26"])
def test_refused_dates(text):
    with pytest.raises(LocationError):
        rules.clean_partial_date(text)


@pytest.mark.parametrize(
    ("text", "cents"),
    [("", None), ("12", 1200), ("12,5", 1250), ("12.50 €", 1250), ("1 234,05", 123405), ("0", 0)],
)
def test_prices(text, cents):
    assert rules.parse_price(text) == cents


@pytest.mark.parametrize("text", ["douze", "-3", "12,505", "1234567", "12,5,0"])
def test_refused_prices(text):
    with pytest.raises(LocationError):
        rules.parse_price(text)


def test_price_format():
    values = (None, 0, 5, 1250, 123405)
    assert [rules.format_price(value) for value in values] == ["", "0,00", "0,05", "12,50", "1234,05"]


def test_status_permissions():
    permissions = rules.STATUS_PERMISSIONS
    restricted = {action for action, permission in permissions.items() if permission == "holdings.withdraw"}
    assert restricted == {"withdraw", "lost", "reinstate"}
    assert set(rules.STATUS_PERMISSIONS) == set(rules.STATUS_ACTIONS)
