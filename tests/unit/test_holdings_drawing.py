# SPDX-License-Identifier: EUPL-1.2
"""Ce que dessinent les étagères : dos, niveaux, meubles, arbre, plan de salle. Aucune base."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from biblyo.modules.catalogue.rules import cover_color
from biblyo.modules.holdings import drawing
from biblyo.modules.holdings.drawing import LevelView, RoomPlan, RoomTile, SpineFacts, UnitView
from biblyo.modules.holdings.rules import Fill, GridBox


def _fact(copy_id, title="Titre", *, series=None, volume=None, status="available", position=None, **extra):
    return SpineFacts(
        copy_id=copy_id,
        position=position if position is not None else copy_id,
        title=title,
        status=status,
        type_code=extra.pop("type_code", "bd"),
        href=f"/x/{copy_id}",
        series=series,
        volume=volume,
        **extra,
    )


def test_spine_label_says_position_title_volume_call_and_status():
    fact = _fact(3, "Astérix le Gaulois", series="Astérix", volume="1", call_number="BD AST 3")
    assert drawing.spine_label(fact) == "Position 3 : Astérix le Gaulois, tome 1, cote BD AST 3, en place"
    loose = SpineFacts(9, None, "Atlas", "in_repair", "documentaire", "/x/9")
    assert drawing.spine_label(loose) == "Atlas, en réparation"


def test_neighbours_of_a_series_share_a_band():
    facts = [
        _fact(1, series="A"),
        _fact(2, series="A"),
        _fact(3, series="B"),
        _fact(4),
        _fact(5, series="A"),
    ]
    assert [item.group_edge for item in drawing.spines(facts)] == ["start", "end", "solo", None, "solo"]
    assert [item.group_edge for item in drawing.spines(facts, group_series=False)] == [None] * 5
    three = drawing.spines([_fact(1, series="A"), _fact(2, series="A"), _fact(3, series="A")])
    assert [item.group_edge for item in three] == ["start", "mid", "end"]


def test_spine_classes_come_from_the_server():
    built = drawing.spines(
        [
            _fact(1, "Le Lotus bleu", series="Tintin", pages=62),
            _fact(2, "Kumo", type_code="manga", status="in_processing"),
            _fact(3, "Revue", type_code="revue", status="missing", pages=30),
        ],
        hit=3,
    )
    assert built[0].classes == f"spn w4 h-tall cv-{cover_color('Tintin')}"
    assert built[1].classes == f"spn w3 h-short cv-{cover_color('Kumo')} st-prep"
    assert built[1].state_icon == "clock"
    assert built[2].classes == f"spn w1 h-tall cv-{cover_color('Revue')} st-missing hit"
    assert built[2].state_icon == "alert"
    assert [item.hit for item in built] == [False, False, True]


def test_every_state_but_in_place_has_a_pictogram():
    states = ["in_processing", "available", "in_repair", "missing"]
    built = drawing.spines([_fact(index, status=status) for index, status in enumerate(states, start=1)])
    assert [item.state_icon for item in built] == ["clock", None, "gear", "alert"]


@pytest.mark.parametrize(
    ("fill", "slots", "more", "caption", "text"),
    [
        (Fill(6, 12), 6, 0, "Étagère 1 · 6/12", "6 / 12 places"),
        (Fill(2, 30), 6, 22, "Étagère 1 · 2/30", "2 / 30 places"),
        (Fill(4, 4), 0, 0, "Étagère 1 · 4/4", "4 / 4 places, pleine"),
        (Fill(6, 4), 0, 0, "Étagère 1 · 6/4", "6 / 4 places, dépassée de 2"),
        (Fill(5, None), 0, 0, "Étagère 1 · 5", "5 exemplaires"),
        (Fill(1, None), 0, 0, "Étagère 1 · 1", "1 exemplaire"),
    ],
)
def test_level_free_slots_caption_and_fill(fill, slots, more, caption, text):
    level = LevelView(1, "Étagère 1", "/e/1", [], fill)
    assert (level.free_slots, level.more_free, level.caption, level.fill_text) == (slots, more, caption, text)


def test_unit_counts_its_levels():
    levels = [LevelView(1, "A", "/", [], Fill(3, 5)), LevelView(2, "B", "/", [], Fill(1, 5))]
    unit = UnitView(1, "Meuble", "/m/1", levels)
    assert (unit.count, unit.count_text) == (4, "4 exemplaires")
    assert UnitView(2, "Vide", "/m/2", [LevelView(3, "A", "/", [], Fill(1, None))]).count_text == "1 exemplaire"


@dataclass(frozen=True)
class Place:
    id: int
    parent_id: int | None
    kind: str
    name: str
    sort_order: int = 0
    capacity: int | None = None
    is_active: bool = True


def test_tree_counts_descendants_and_opens_the_current_branch():
    places = [
        Place(1, None, "room", "Salle"),
        Place(2, 1, "furniture", "Meuble B", 2),
        Place(3, 1, "furniture", "Meuble A", 1),
        Place(4, 3, "shelf", "Haut", 1, 10),
        Place(5, 3, "shelf", "Bas", 2, 3),
        Place(6, 1, "virtual", "Exposition", 3),
        Place(7, None, "room", "Réserve", 1, None, False),
    ]
    roots = drawing.tree(places, {4: 4, 5: 5, 6: 1}, current=5)
    assert [node.name for node in roots] == ["Salle", "Réserve"]  # ordre manuel, puis nom
    room = roots[0]
    assert room.count == 10
    assert [child.name for child in room.children] == ["Meuble A", "Meuble B", "Exposition"]
    furniture = room.children[0]
    assert (furniture.count, furniture.open, furniture.fill) == (9, True, None)
    assert [(shelf.fill.state, shelf.current) for shelf in furniture.children] == [("ok", False), ("over", True)]
    assert room.children[1].open is False  # hors de la branche courante, un meuble reste fermé
    assert roots[1].active is False and roots[1].href == "/emplacements/7"
    assert furniture.icon == "shelf" and furniture.kind_label == "Meuble"


def test_room_plan_size_and_classes():
    tiles = [
        RoomTile(1, "A", "/a", 3, [50], GridBox(1, 1, 2, 1), "BD", True),
        RoomTile(2, "B", "/b", 1, [], GridBox(4, 10, 3, 2)),
        RoomTile(3, "C", "/c", 0, [], None),
    ]
    plan = RoomPlan(1, "Salle", tiles)
    assert (plan.columns, plan.rows) == (12, 5)
    assert plan.grid_classes == "is-plan [--room-cols:12]"
    assert [tile.name for tile in plan.unplaced] == ["C"]
    assert tiles[1].grid_classes == "md:row-start-4 md:col-start-10 md:row-span-2 md:col-span-3"
    assert tiles[2].grid_classes == ""
    assert (tiles[0].summary, tiles[1].summary) == ("BD · 3 ex.", "1 ex.")
    assert RoomPlan(2, "Vide", []).columns == 8
