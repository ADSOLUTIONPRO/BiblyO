# SPDX-License-Identifier: EUPL-1.2
"""Exemplaires et emplacements sur une vraie base migrée : rangement, déplacements, statuts, codes, concurrence."""

from __future__ import annotations

import json
import random
import sqlite3
import threading
from collections import defaultdict
from types import SimpleNamespace

import pytest

from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.modules.catalogue import search
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.catalogue.models import Copy, Edition, Work
from biblyo.modules.catalogue.service import ConflictError, EditionInput
from biblyo.modules.holdings import repository as repo
from biblyo.modules.holdings import service as holdings
from biblyo.modules.holdings.models import Location
from biblyo.modules.holdings.rules import GridBox
from biblyo.modules.holdings.service import CopyInput, LocationInput
from biblyo.modules.patrons import service as patrons
from biblyo.modules.patrons.service import PersonInput, Registration

from ..factories import make_notice


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def _rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def _edition(settings, work_id):
    return _rows(settings, "SELECT id FROM editions WHERE work_id = ? AND is_primary = 1", work_id)[0][0]


def _new_copies(settings, database, title="Le Lotus bleu", count=1, **options):
    work = make_notice(settings, title, **options)
    return catalogue.add_copies(database, _edition(settings, work), count, "", None)


def _copy(database, copy_id) -> Copy:
    with database.session() as session:
        found = session.get(Copy, copy_id)
        assert found is not None
        return found


def _location(database, location_id) -> Location:
    with database.session() as session:
        found = session.get(Location, location_id)
        assert found is not None
        return found


def _order(database, location_id):
    with database.session() as session:
        return repo.shelf_order(session, location_id)


def _assert_positions_continuous(settings):
    """L'invariant du rangement : sur chaque niveau, les rangs vont de 1 à n, sans trou ni doublon."""
    by_level = defaultdict(list)
    for location_id, rank in _rows(
        settings,
        "SELECT location_id, shelf_position FROM copies WHERE shelf_position IS NOT NULL "
        "ORDER BY location_id, shelf_position",
    ):
        by_level[location_id].append(rank)
    for location_id, ranks in by_level.items():
        assert ranks == list(range(1, len(ranks) + 1)), (location_id, ranks)
    orphans = _rows(
        settings,
        "SELECT c.id FROM copies c JOIN locations l ON l.id = c.location_id "
        "WHERE (l.kind IN ('shelf','bin')) <> (c.shelf_position IS NOT NULL)",
    )
    assert orphans == []


def _journal(settings, action):
    return [
        (entity_id, json.loads(details) if details else None)
        for entity_id, details in _rows(
            settings, "SELECT entity_id, details FROM audit_log WHERE action = ? ORDER BY id", action
        )
    ]


@pytest.fixture
def place(database):
    """Le jeu fictif de D10, en petit : une salle, un meuble à trois étagères de 5 places, un lieu virtuel."""
    room = holdings.create_location(database, "room", None, LocationInput("Salle jeunesse", code="SJ"), None)
    furniture = holdings.create_location(database, "furniture", room, LocationInput("Meuble BD 1", code="BD1"), None)
    shelves = [
        holdings.create_location(database, "shelf", furniture, LocationInput(f"Étagère {n}", capacity="5"), None)
        for n in (1, 2, 3)
    ]
    virtual = holdings.create_location(database, "virtual", room, LocationInput("Exposition"), None)
    return SimpleNamespace(room=room, furniture=furniture, shelves=shelves, virtual=virtual)


# --- Cas exigés : œuvres, éditions, cotes -----------------------------------------------------------------------------
def test_two_copies_of_the_same_edition(settings, database, place):
    first, second = _new_copies(settings, database, "Le Lotus bleu", 2, series="Tintin", volume="5")
    assert _copy(database, first).call_number == _copy(database, second).call_number == "BD TIN 1"
    assert _copy(database, first).barcode != _copy(database, second).barcode
    holdings.shelve(database, [first], place.shelves[0], user_id=None)
    holdings.shelve(database, [second], place.shelves[1], user_id=None)
    with database.session() as session:
        one, two = repo.localise(session, first), repo.localise(session, second)
    assert (one.path, one.position) == ("Salle jeunesse / Meuble BD 1 / Étagère 1", 1)
    assert (two.path, two.position) == ("Salle jeunesse / Meuble BD 1 / Étagère 2", 1)


def test_two_editions_of_the_same_work_share_the_call_number(settings, database, place):
    work = make_notice(settings, "Le Lotus bleu", series="Tintin", volume="5", isbn="978-2-07-040850-4")
    second = catalogue.add_edition(database, work, EditionInput(isbn="0-8044-2957-X", publisher="Gallimard"), None)
    old = catalogue.add_copies(database, _edition(settings, work), 1, "", None)[0]
    new = catalogue.add_copies(database, second, 1, "", None)[0]
    assert _copy(database, old).call_number == _copy(database, new).call_number == "BD TIN 1"
    assert _copy(database, old).edition_id != _copy(database, new).edition_id
    holdings.shelve(database, [old, new], place.shelves[0], user_id=None)
    assert _order(database, place.shelves[0]) == [old, new]


def test_copy_without_isbn(settings, database, place):
    (copy_id,) = _new_copies(settings, database, "Les Misérables", document_type="livre", authors=("Victor Hugo",))
    copy = _copy(database, copy_id)
    assert copy.call_number == "R HUG"
    holdings.shelve(database, [copy_id], place.shelves[2], user_id=None)
    assert _copy(database, copy_id).shelf_position == 1


def test_call_number_proposal_ignores_the_commercial_volume(settings, database):
    first = _new_copies(settings, database, "Tintin au Tibet", series="Tintin", volume="20")[0]
    second = _new_copies(settings, database, "Tintin au Congo", series="Tintin", volume="2")[0]
    assert _copy(database, first).call_number == "BD TIN 1"
    assert _copy(database, second).call_number == "BD TIN 2"


def test_call_number_proposal_skips_the_leading_article(settings, database):
    alone = _new_copies(settings, database, "La Cible")[0]
    in_series = _new_copies(settings, database, "Premier", series="Les Deux Rives", volume="1")[0]
    assert _copy(database, alone).call_number == "BD CIB"
    assert _copy(database, in_series).call_number == "BD DEU 1"


def test_several_copies_sharing_a_call_number(settings, database, place):
    copies = _new_copies(settings, database, "Astérix le Gaulois", 3, series="Astérix", volume="1")
    assert len({_copy(database, item).call_number for item in copies}) == 1
    holdings.shelve(database, copies[:2], place.shelves[0], user_id=None)
    holdings.shelve(database, [copies[2]], place.shelves[1], user_id=None)
    with database.session() as session:
        found = [repo.localise(session, item) for item in copies]
    assert [(item.location_id, item.position) for item in found] == [
        (place.shelves[0], 1),
        (place.shelves[0], 2),
        (place.shelves[1], 1),
    ]
    assert found[0].after is not None and found[0].after.id == copies[1]
    assert found[1].before is not None and found[1].before.id == copies[0]


def test_a_move_never_changes_the_call_number_nor_the_work(settings, database, place):
    (copy_id,) = _new_copies(settings, database, "Le Lotus bleu", series="Tintin", volume="5")
    with database.session() as session:
        work = session.get(Work, session.get(Edition, _copy(database, copy_id).edition_id).work_id)
        edition = session.get(Edition, _copy(database, copy_id).edition_id)
        before = (work.version, work.updated_at, edition.version, edition.updated_at)
    holdings.shelve(database, [copy_id], place.shelves[0], user_id=None)
    holdings.shelve(database, [copy_id], place.shelves[1], user_id=None)
    holdings.shelve(database, [copy_id], place.virtual, user_id=None)
    with database.session() as session:
        work = session.get(Work, work.id)
        edition = session.get(Edition, edition.id)
        assert (work.version, work.updated_at, edition.version, edition.updated_at) == before
    assert _copy(database, copy_id).call_number == "BD TIN 1"


# --- Cas exigés : capacité, concurrence, déplacements groupés ---------------------------------------------------------
def test_full_shelf_is_signalled_never_refused(settings, database, place):
    copies = _new_copies(settings, database, "Remplissage", 7)
    result = holdings.shelve(database, copies[:5], place.shelves[0], user_id=None)
    assert (result.fill.state, result.fill.free) == ("full", 0)
    result = holdings.shelve(database, copies[5:], place.shelves[0], user_id=None)
    assert (result.fill.state, result.fill.excess) == ("over", 2)
    assert _order(database, place.shelves[0]) == copies


def test_concurrent_move_of_the_same_copy_is_refused(settings, database, place):
    (copy_id,) = _new_copies(settings, database)
    shown = _copy(database, copy_id).version  # deux personnes affichent le même exemplaire
    holdings.shelve(database, [copy_id], place.shelves[0], expected_versions={copy_id: shown}, user_id=None)
    with pytest.raises(ConflictError):
        holdings.shelve(database, [copy_id], place.shelves[1], expected_versions={copy_id: shown}, user_id=None)
    assert _copy(database, copy_id).location_id == place.shelves[0]


def test_insertion_at_a_rank_is_refused_if_the_shelf_changed(settings, database, place):
    copies = _new_copies(settings, database, "Rangs", 3)
    holdings.shelve(database, copies[:2], place.shelves[0], user_id=None)
    shown = _location(database, place.shelves[0]).version
    holdings.shelve(database, [copies[0]], place.shelves[1], user_id=None)  # quelqu'un d'autre vide l'étagère
    with pytest.raises(ConflictError):
        holdings.shelve(
            database, [copies[2]], place.shelves[0], position=2, expected_location_version=shown, user_id=None
        )
    assert _order(database, place.shelves[0]) == [copies[1]]


def test_simultaneous_shelvings_keep_ranks_continuous(settings, place):
    database = Database(settings.database_path)
    copies = _new_copies(settings, database, "Concurrence", 24)
    database.dispose()
    errors: list[BaseException] = []

    def worker(batch):
        own = Database(settings.database_path)
        try:
            for copy_id in batch:
                holdings.shelve(own, [copy_id], place.shelves[0], position=1, user_id=None)
        except BaseException as error:  # remonté au fil principal
            errors.append(error)
        finally:
            own.dispose()

    threads = [threading.Thread(target=worker, args=(copies[index::4],)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    final = Database(settings.database_path)
    assert sorted(_order(final, place.shelves[0])) == sorted(copies)
    final.dispose()
    _assert_positions_continuous(settings)


def test_multi_copy_move(settings, database, place):
    source = _new_copies(settings, database, "Source", 5)
    target = _new_copies(settings, database, "Cible", 3)
    holdings.shelve(database, source, place.shelves[0], user_id=None)
    holdings.shelve(database, target, place.shelves[1], user_id=None)
    moved = [source[3], source[1]]  # l'ordre de la sélection est l'ordre d'arrivée
    plan = holdings.plan_shelving(database, moved, place.shelves[1], position=2)
    assert (plan.fill_after.count, plan.shifted, plan.refusals) == (5, 2, [])
    assert [item.from_position for item in plan.copies] == [4, 2]
    result = holdings.shelve(
        database,
        moved,
        place.shelves[1],
        position=2,
        expected_versions=plan.expected_versions,
        expected_location_version=plan.location_version,
        user_id=None,
    )
    assert result.shifted == 2
    assert _order(database, place.shelves[1]) == [target[0], source[3], source[1], target[1], target[2]]
    assert _order(database, place.shelves[0]) == [source[0], source[2], source[4]]
    _assert_positions_continuous(settings)
    journal = _journal(settings, "copy.shelve")[-2:]
    assert [entry[1]["de_rang"] for entry in journal] == [4, 2]
    assert {entry[1]["vers"] for entry in journal} == {"Salle jeunesse / Meuble BD 1 / Étagère 2"}


def test_a_copy_is_never_in_two_places(settings, database, place):
    copies = _new_copies(settings, database, "Unique", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.shelve(database, [copies[0]], place.shelves[1], user_id=None)
    holdings.shelve(database, [copies[0]], place.virtual, user_id=None)
    assert _order(database, place.shelves[0]) == copies[1:]
    assert _order(database, place.shelves[1]) == []
    copy = _copy(database, copies[0])
    assert (copy.location_id, copy.shelf_position) == (place.virtual, None)
    _assert_positions_continuous(settings)


def test_inserting_never_overwrites_a_rank(settings, database, place):
    copies = _new_copies(settings, database, "Insertion", 4)
    holdings.shelve(database, copies[:3], place.shelves[0], user_id=None)
    holdings.shelve(database, [copies[3]], place.shelves[0], position=2, user_id=None)
    assert _order(database, place.shelves[0]) == [copies[0], copies[3], copies[1], copies[2]]
    holdings.shelve(database, [copies[0]], place.shelves[0], position=99, user_id=None)  # au-delà : à la fin
    assert _order(database, place.shelves[0]) == [copies[3], copies[1], copies[2], copies[0]]


def test_random_operations_keep_every_shelf_continuous(settings, database, place):
    copies = _new_copies(settings, database, "Hasard", 12)
    targets = [*place.shelves, place.virtual]
    chance = random.Random(20261108)
    for _step in range(120):
        roll = chance.random()
        if roll < 0.6:
            chosen = chance.sample(copies, chance.randint(1, 3))
            target = chance.choice(targets)
            position = chance.choice([None, 1, 2, 5]) if target != place.virtual else None
            holdings.shelve(database, chosen, target, position=position, user_id=None)
        elif roll < 0.8:
            shelf = chance.choice(place.shelves)
            order = _order(database, shelf)
            chance.shuffle(order)
            holdings.reorder(database, shelf, order, None, None)
        else:
            holdings.unshelve(database, [chance.choice(copies)], user_id=None)
        _assert_positions_continuous(settings)


# --- Cas exigés : emplacements ----------------------------------------------------------------------------------------
def test_deleting_non_empty_furniture(settings, database, place):
    (copy_id,) = _new_copies(settings, database)
    holdings.shelve(database, [copy_id], place.shelves[1], user_id=None)
    with pytest.raises(BusinessError, match="contient encore 1 exemplaire"):
        holdings.delete_location(database, place.furniture, None, None, with_children=True)
    holdings.unshelve(database, [copy_id], user_id=None)
    with pytest.raises(BusinessError, match="contient encore 3 emplacements"):
        holdings.delete_location(database, place.furniture, None, None)
    assert len(_rows(settings, "SELECT id FROM locations")) == 6
    assert holdings.delete_location(database, place.furniture, None, None, with_children=True) == place.room
    assert len(_rows(settings, "SELECT id FROM locations")) == 2
    assert _journal(settings, "location.delete")[-1][1]["sous_emplacements"] == 3


def test_location_cycle_is_refused(settings, database, place):
    for parent in (place.room, place.furniture, place.shelves[0]):
        with pytest.raises(BusinessError, match="à l’intérieur de lui-même"):
            holdings.move_location(database, place.room, parent, None, None)
    assert _location(database, place.room).parent_id is None
    assert _location(database, place.shelves[0]).path == "Salle jeunesse / Meuble BD 1 / Étagère 1"


@pytest.mark.parametrize(
    ("kind", "parent"),
    [("furniture", None), ("shelf", "room"), ("room", "furniture"), ("site", "room"), ("shelf", "shelf")],
)
def test_incoherent_parent_is_refused(database, place, kind, parent):
    parents = {None: None, "room": place.room, "furniture": place.furniture, "shelf": place.shelves[0]}
    with pytest.raises(BusinessError, match="se place sous"):
        holdings.create_location(database, kind, parents[parent], LocationInput("Essai"), None)


def test_hierarchy_with_a_site_and_its_depth(settings, database, place):
    site = holdings.create_location(database, "site", None, LocationInput("Club"), None)
    holdings.move_location(database, place.room, site, None, None)
    shelf = _location(database, place.shelves[2])
    assert (shelf.depth, shelf.path) == (3, "Club / Salle jeunesse / Meuble BD 1 / Étagère 3")
    with pytest.raises(BusinessError, match="se place sous"):
        holdings.create_location(database, "shelf", place.shelves[2], LocationInput("Trop bas"), None)


def test_location_names_codes_and_capacity_are_checked(database, place):
    with pytest.raises(FormErrors) as caught:
        holdings.create_location(
            database, "shelf", place.furniture, LocationInput(" étagère 1 ", code="bd1", capacity="-3"), None
        )
    assert set(caught.value.fields) == {"nom", "code", "capacite"}
    assert "Meuble BD 1" in caught.value.fields["code"]
    with pytest.raises(FormErrors) as caught:
        holdings.create_location(database, "furniture", place.room, LocationInput("Bac", capacity="10"), None)
    assert set(caught.value.fields) == {"capacite"}


def test_rename_updates_the_paths_below(database, place):
    holdings.update_location(database, place.room, LocationInput("Salle BD", code="SJ"), 1, None)
    assert _location(database, place.shelves[1]).path == "Salle BD / Meuble BD 1 / Étagère 2"
    with pytest.raises(ConflictError):
        holdings.update_location(database, place.room, LocationInput("Autre nom"), 1, None)


def test_capacity_below_the_count_is_allowed_and_signalled(settings, database, place):
    copies = _new_copies(settings, database, "Capacité", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.update_location(database, place.shelves[0], LocationInput("Étagère 1", capacity="2"), None, None)
    with database.session() as session:
        fill = repo.fill_of(session, session.get(Location, place.shelves[0]))
    assert (fill.state, fill.excess) == ("over", 1)


def test_furniture_is_placed_on_the_room_plan_without_overlap(database, place):
    first = _location(database, place.furniture)
    assert (first.grid_row, first.grid_col, first.grid_width, first.grid_height) == (1, 1, 2, 1)
    second = holdings.create_location(database, "furniture", place.room, LocationInput("Meuble BD 2"), None)
    assert (_location(database, second).grid_row, _location(database, second).grid_col) == (1, 3)
    with pytest.raises(BusinessError, match="occupée par « Meuble BD 1 »"):
        holdings.place_furniture(database, second, GridBox(1, 2, 2, 1), None, None)
    holdings.place_furniture(database, second, GridBox(3, 1, 1, 2), None, None)
    moved = _location(database, second)
    assert (moved.grid_row, moved.grid_col, moved.grid_width, moved.grid_height) == (3, 1, 1, 2)
    with pytest.raises(BusinessError, match="sort de la grille"):
        holdings.place_furniture(database, second, GridBox(40, 40, 2, 1), None, None)


def test_moving_furniture_keeps_its_copies_and_their_ranks(settings, database, place):
    copies = _new_copies(settings, database, "Meuble", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    other = holdings.create_location(database, "room", None, LocationInput("Salle adultes"), None)
    holdings.move_location(database, place.furniture, other, None, None)
    assert _location(database, place.shelves[0]).path == "Salle adultes / Meuble BD 1 / Étagère 1"
    assert _order(database, place.shelves[0]) == copies


def test_levels_can_be_reordered(database, place):
    reversed_ids = list(reversed(place.shelves))
    holdings.reorder_children(database, place.furniture, reversed_ids, None, None)
    with database.session() as session:
        assert [item.id for item in repo.children(session, place.furniture)] == reversed_ids
    with pytest.raises(ConflictError):
        holdings.reorder_children(database, place.furniture, reversed_ids[:2], None, None)


def test_deactivation_needs_an_empty_place(settings, database, place):
    (copy_id,) = _new_copies(settings, database)
    holdings.shelve(database, [copy_id], place.shelves[0], user_id=None)
    with pytest.raises(BusinessError, match="encore 1 exemplaire"):
        holdings.set_location_active(database, place.furniture, False, None, None)
    holdings.set_location_active(database, place.shelves[1], False, None, None)
    with pytest.raises(BusinessError, match="désactivé"):
        holdings.shelve(database, [copy_id], place.shelves[1], user_id=None)
    holdings.set_location_active(database, place.shelves[1], True, None, None)
    holdings.shelve(database, [copy_id], place.shelves[1], user_id=None)


def test_copies_go_only_on_levels_and_virtual_places(settings, database, place):
    (copy_id,) = _new_copies(settings, database)
    for target in (place.room, place.furniture):
        with pytest.raises(BusinessError, match="se range sur une étagère"):
            holdings.shelve(database, [copy_id], target, user_id=None)
    with pytest.raises(BusinessError, match="pas de rang"):
        holdings.shelve(database, [copy_id], place.virtual, position=1, user_id=None)


# --- Cas exigés : codes -----------------------------------------------------------------------------------------------
def _form(copy: Copy, **changes) -> CopyInput:
    values = {"barcode": copy.barcode, "inventory_number": copy.inventory_number, "call_number": copy.call_number or ""}
    return CopyInput(**(values | changes))


def test_barcode_already_assigned_is_refused(settings, database):
    first, second = _new_copies(settings, database, "Codes", 2)
    one = _copy(database, first)
    with pytest.raises(FormErrors) as caught:
        holdings.update_copy(database, second, _form(_copy(database, second), barcode=one.barcode), None, None)
    assert "« Codes » (EX-000001)" in caught.value.fields["code_barres"]
    with pytest.raises(FormErrors) as caught:
        holdings.update_copy(database, second, _form(_copy(database, second), inventory_number=one.barcode), None, None)
    assert set(caught.value.fields) == {"inventaire"}
    assert _copy(database, second).barcode == "EX-000002"


def test_codes_are_normalized_and_distinct_from_each_other(settings, database):
    (copy_id,) = _new_copies(settings, database)
    copy = _copy(database, copy_id)
    holdings.update_copy(database, copy_id, _form(copy, barcode=" cl 2026 0042 ", inventory_number=""), 1, None)
    copy = _copy(database, copy_id)
    assert (copy.barcode, copy.inventory_number, copy.version) == ("CL20260042", "CL20260042", 2)
    holdings.update_copy(database, copy_id, _form(copy, inventory_number="INV-1987-12"), 2, None)
    assert _copy(database, copy_id).inventory_number == "INV-1987-12"
    with pytest.raises(FormErrors) as caught:
        holdings.update_copy(database, copy_id, _form(_copy(database, copy_id), barcode="é!"), None, None)
    assert set(caught.value.fields) == {"code_barres"}


def test_an_abandoned_code_is_never_given_to_another_copy(settings, database):
    first, second = _new_copies(settings, database, "Abandon", 2)
    renamed = _form(_copy(database, first), barcode="CL-1", inventory_number="CL-1")
    holdings.update_copy(database, first, renamed, 1, None)
    with pytest.raises(FormErrors) as caught:
        holdings.update_copy(database, second, _form(_copy(database, second), barcode="EX-000001"), None, None)
    assert "jamais réattribué" in caught.value.fields["code_barres"]
    # Son ancien exemplaire peut le reprendre : une faute de frappe se corrige.
    holdings.update_copy(
        database, first, _form(_copy(database, first), barcode="EX-000001", inventory_number=""), None, None
    )
    assert _copy(database, first).barcode == "EX-000001"
    assert _rows(settings, "SELECT value FROM retired_codes ORDER BY value") == [("CL-1",)]


def test_generated_codes_skip_manual_and_retired_ones(settings, database):
    (copy_id,) = _new_copies(settings, database)
    holdings.update_copy(database, copy_id, _form(_copy(database, copy_id), barcode="EX-000002"), None, None)
    created = _new_copies(settings, database, "Suivant")[0]
    assert _copy(database, created).barcode == "EX-000003"


def _reader_with_card(database, number) -> int:
    person = PersonInput(first_name="Anne", last_name="Durand", category="adult", card_number=number)
    return patrons.register(database, Registration(person=person), None)


def test_a_copy_code_cannot_be_a_card_number(settings, database):
    (copy_id,) = _new_copies(settings, database)
    _reader_with_card(database, "CARTE-77")
    for field, name in (("barcode", "code_barres"), ("inventory_number", "inventaire")):
        with pytest.raises(FormErrors) as caught:
            holdings.update_copy(database, copy_id, _form(_copy(database, copy_id), **{field: "carte-77"}), None, None)
        assert "carte de lecteur" in caught.value.fields[name]
        assert "Durand" not in caught.value.fields[name] and "Anne" not in caught.value.fields[name]
    assert _copy(database, copy_id).barcode == "EX-000001"


def test_generated_codes_skip_card_numbers(settings, database):
    _reader_with_card(database, "EX-000001")
    (copy_id,) = _new_copies(settings, database)
    assert _copy(database, copy_id).barcode == "EX-000002"


def test_copy_fields_are_validated(settings, database):
    (copy_id,) = _new_copies(settings, database)
    bad = _form(
        _copy(database, copy_id),
        call_number="X" * 41,
        condition="superbe",
        acquisition_mode="vol",
        acquired_on="hier",
        price="douze",
    )
    with pytest.raises(FormErrors) as caught:
        holdings.update_copy(database, copy_id, bad, None, None)
    assert set(caught.value.fields) == {"cote", "etat", "mode_acquisition", "date_acquisition", "prix"}
    good = _form(
        _copy(database, copy_id),
        call_number="bd  tin 1",
        condition="worn",
        condition_note="Dos fendu",
        acquisition_mode="donation",
        acquired_on="03/2019",
        acquisition_note="Don d'un adhérent",
        price="12,5 €",
        provenance="Fonds du Club",
        is_loanable=False,
    )
    holdings.update_copy(database, copy_id, good, None, None)
    copy = _copy(database, copy_id)
    assert (copy.call_number, copy.condition, copy.acquired_on, copy.price_cents, copy.is_loanable) == (
        "BD TIN 1",
        "worn",
        "2019-03",
        1250,
        False,
    )


def test_call_numbers_and_inventory_numbers_are_searchable(settings, database):
    (copy_id,) = _new_copies(settings, database, "Cherchable")
    work = _rows(settings, "SELECT work_id FROM editions WHERE id = ?", _copy(database, copy_id).edition_id)[0][0]
    form = _form(_copy(database, copy_id), call_number="BD CHE 7", inventory_number="INV-1987-12")
    holdings.update_copy(database, copy_id, form, None, None)
    with database.session() as session:
        for query in ("cote:BD CHE 7", "bdche7", "INV-1987-12", "code:inv198712", "EX-000001"):
            assert search.match_ids(session, query, limit=10) == [work], query


# --- Cas exigés : statuts ---------------------------------------------------------------------------------------------
def test_withdrawing_a_copy_with_history(settings, database, place):
    copies = _new_copies(settings, database, "Retrait", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    with pytest.raises(BusinessError, match="historique"):
        catalogue.delete_copy(database, copies[1], None)
    with pytest.raises(FormErrors) as caught:
        holdings.change_status(database, copies[1], "withdraw", user_id=None)
    assert set(caught.value.fields) == {"motif"}
    holdings.change_status(database, copies[1], "withdraw", reason="Trop abîmé", user_id=None)
    copy = _copy(database, copies[1])
    assert (copy.status, copy.location_id, copy.withdrawal_reason) == ("withdrawn", None, "Trop abîmé")
    assert copy.withdrawn_on is not None
    assert _order(database, place.shelves[0]) == [copies[0], copies[2]]
    _assert_positions_continuous(settings)
    entry = _journal(settings, "copy.status")[-1]
    assert entry[0] == copies[1]
    assert entry[1] | {} == {
        "action": "withdraw",
        "de": "available",
        "vers": "withdrawn",
        "motif": "Trop abîmé",
        "emplacement": "Salle jeunesse / Meuble BD 1 / Étagère 1",
        "rang": 2,
    }
    assert len(_journal(settings, "copy.shelve")) == 3  # l'historique reste
    with pytest.raises(BusinessError, match="déclarez-le d’abord retrouvé"):
        holdings.shelve(database, [copies[1]], place.shelves[0], user_id=None)
    holdings.change_status(database, copies[1], "reinstate", user_id=None)
    assert _copy(database, copies[1]).status == "in_processing"


def test_shelving_a_lost_copy_is_refused(settings, database, place):
    copies = _new_copies(settings, database, "Perdu", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.change_status(database, copies[0], "lost", user_id=None)
    assert _copy(database, copies[0]).location_id is None
    assert _order(database, place.shelves[0]) == [copies[1]]
    with pytest.raises(BusinessError, match=r"« Perdu » \(EX-000001\) : .*perdu.*retrouvé"):
        holdings.shelve(database, [copies[1], copies[0]], place.shelves[1], user_id=None)
    assert _copy(database, copies[1]).location_id == place.shelves[0]  # tout ou rien
    plan = holdings.plan_shelving(database, [copies[1], copies[0]], place.shelves[1])
    assert [item.refusal is not None for item in plan.copies] == [False, True]
    holdings.change_status(database, copies[0], "found", user_id=None)
    holdings.shelve(database, [copies[0]], place.shelves[1], user_id=None)
    assert _copy(database, copies[0]).status == "available"


def test_shelving_an_in_repair_copy_keeps_its_status(settings, database, place):
    (copy_id,) = _new_copies(settings, database)
    holdings.change_status(database, copy_id, "repair", user_id=None)
    holdings.shelve(database, [copy_id], place.shelves[0], user_id=None)
    holdings.shelve(database, [copy_id], place.shelves[1], user_id=None)
    assert _copy(database, copy_id).status == "in_repair"
    holdings.change_status(database, copy_id, "repaired", user_id=None)
    assert _copy(database, copy_id).status == "available"


def test_first_shelving_makes_a_new_copy_available_only_once(settings, database, place):
    first, second = _new_copies(settings, database, "Premier", 2)
    result = holdings.shelve(database, [first], place.shelves[0], user_id=None)
    assert result.statuses == {first: ("in_processing", "available")}
    holdings.shelve(database, [second], place.shelves[0], keep_processing=True, user_id=None)
    assert _copy(database, second).status == "in_processing"
    holdings.shelve(database, [second], place.shelves[1], user_id=None)  # plus un premier rangement
    assert _copy(database, second).status == "in_processing"


def test_missing_copy_keeps_its_place(settings, database, place):
    copies = _new_copies(settings, database, "Introuvable", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.change_status(database, copies[0], "missing", user_id=None)
    copy = _copy(database, copies[0])
    assert (copy.status, copy.shelf_position) == ("missing", 1)
    assert copy.missing_on is not None
    holdings.change_status(database, copies[0], "found", user_id=None)
    assert (_copy(database, copies[0]).status, _copy(database, copies[0]).missing_on) == ("available", None)


def test_impossible_status_change_is_refused(settings, database):
    (copy_id,) = _new_copies(settings, database)
    with pytest.raises(BusinessError, match="Impossible depuis le statut « en préparation »"):
        holdings.change_status(database, copy_id, "found", user_id=None)


def test_a_copy_without_history_can_be_deleted_and_its_codes_retired(settings, database):
    (copy_id,) = _new_copies(settings, database)
    holdings.update_copy(database, copy_id, _form(_copy(database, copy_id), inventory_number="INV-9"), None, None)
    catalogue.delete_copy(database, copy_id, None)
    assert _rows(settings, "SELECT value, kind FROM retired_codes ORDER BY value") == [
        ("EX-000001", "barcode"),
        ("INV-9", "inventory"),
    ]
    (other,) = _new_copies(settings, database, "Autre")
    with pytest.raises(FormErrors):
        holdings.update_copy(database, other, _form(_copy(database, other), inventory_number="INV-9"), None, None)


# --- Tri, réordonnancement, retrait de place --------------------------------------------------------------------------
def test_sort_preview_then_apply(settings, database, place):
    third = _new_copies(settings, database, "Tome trois", series="Lanfeust", volume="3")[0]
    first = _new_copies(settings, database, "Tome un", series="Lanfeust", volume="1")[0]
    alone = _new_copies(settings, database, "Album seul")[0]
    holdings.shelve(database, [alone, third, first], place.shelves[0], user_id=None)
    preview = holdings.sort_preview(database, place.shelves[0], "serie")
    assert preview.after == [first, third, alone]
    assert preview.moved == 2  # le tome 3 garde son rang
    assert _order(database, place.shelves[0]) == [alone, third, first]  # l'aperçu ne change rien
    assert holdings.apply_sort(database, place.shelves[0], "serie", preview.location_version, None) == 2
    assert _order(database, place.shelves[0]) == [first, third, alone]
    with pytest.raises(ConflictError):
        holdings.apply_sort(database, place.shelves[0], "titre", preview.location_version, None)


def test_reorder_needs_exactly_the_shelf_contents(settings, database, place):
    copies = _new_copies(settings, database, "Ordre", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    with pytest.raises(ConflictError):
        holdings.reorder(database, place.shelves[0], copies[:2], None, None)
    with pytest.raises(ConflictError):
        holdings.reorder(database, place.shelves[0], [copies[0], copies[0], copies[1]], None, None)
    assert holdings.reorder(database, place.shelves[0], list(reversed(copies)), None, None) == 2
    assert _order(database, place.shelves[0]) == list(reversed(copies))


def test_unshelving_closes_the_gap(settings, database, place):
    copies = _new_copies(settings, database, "Trou", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.unshelve(database, [copies[0]], user_id=None)
    assert _order(database, place.shelves[0]) == copies[1:]
    copy = _copy(database, copies[0])
    assert (copy.location_id, copy.status) == (None, "available")
    _assert_positions_continuous(settings)


# --- Ranger en scannant -----------------------------------------------------------------------------------------------
def test_successive_scans_reproduce_the_physical_order(settings, database, place):
    copies = _new_copies(settings, database, "Scan", 3)
    for code in ("ex-000003", "EX 000001", "EX-000002"):
        holdings.shelve_by_code(database, code, place.shelves[0], user_id=None)
    assert _order(database, place.shelves[0]) == [copies[2], copies[0], copies[1]]
    again = holdings.shelve_by_code(database, "EX-000003", place.shelves[0], user_id=None)
    assert again.already_there and again.positions == {copies[2]: 1}
    assert _order(database, place.shelves[0]) == [copies[2], copies[0], copies[1]]
    with pytest.raises(BusinessError, match="Aucun exemplaire ne porte le code « EX-999 »"):
        holdings.shelve_by_code(database, "EX-999", place.shelves[0], user_id=None)
    lookalike = _form(_copy(database, copies[1]), inventory_number="EX0-00001")
    holdings.update_copy(database, copies[1], lookalike, None, None)
    with pytest.raises(BusinessError, match="Aucun exemplaire"):  # deux exemplaires répondent : aucun n'est choisi
        holdings.shelve_by_code(database, "ex000001", place.shelves[1], user_id=None)
    assert len(_journal(settings, "copy.shelve_scan")) == 3


def test_scan_by_inventory_number_and_undo(settings, database, place):
    copies = _new_copies(settings, database, "Annuler", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.update_copy(database, copies[0], _form(_copy(database, copies[0]), inventory_number="INV-7"), None, None)
    before = _copy(database, copies[0])
    result = holdings.shelve_by_code(database, "inv-7", place.shelves[1], user_id=None)
    assert result.previous == {copies[0]: (place.shelves[0], 1)}
    holdings.undo_shelving(
        database,
        copies[0],
        previous_location_id=place.shelves[0],
        previous_position=1,
        previous_status=before.status,
        expected_version=_copy(database, copies[0]).version,
        user_id=None,
    )
    assert _order(database, place.shelves[0]) == copies
    assert _order(database, place.shelves[1]) == []


def test_undo_of_a_first_shelving_restores_the_new_copy(settings, database, place):
    (copy_id,) = _new_copies(settings, database)
    holdings.shelve_by_code(database, "EX-000001", place.shelves[0], user_id=None)
    shelved = _copy(database, copy_id)
    with pytest.raises(ConflictError):
        holdings.undo_shelving(
            database,
            copy_id,
            previous_location_id=None,
            previous_position=None,
            previous_status="in_processing",
            expected_version=shelved.version - 1,
            user_id=None,
        )
    holdings.undo_shelving(
        database,
        copy_id,
        previous_location_id=None,
        previous_position=None,
        previous_status="in_processing",
        expected_version=shelved.version,
        user_id=None,
    )
    copy = _copy(database, copy_id)
    assert (copy.location_id, copy.status, copy.first_shelved_at) == (None, "in_processing", None)


# --- Lecture ----------------------------------------------------------------------------------------------------------
def test_tree_counts_and_unshelved(settings, database, place):
    copies = _new_copies(settings, database, "Compter", 4)
    holdings.shelve(database, copies[:2], place.shelves[0], user_id=None)
    holdings.shelve(database, [copies[2]], place.virtual, user_id=None)
    with database.session() as session:
        locations = list(session.query(Location))
        totals = repo.subtree_counts(locations, repo.counts_by_location(session))
        assert repo.unshelved_count(session) == 1
    assert totals[place.room] == 3
    assert totals[place.furniture] == 2
    assert totals[place.shelves[1]] == 0


# --- Placer avant un dos ----------------------------------------------------------------------------------------------
def test_placing_before_a_spine_on_the_same_shelf(settings, database, place):
    copies = _new_copies(settings, database, "Avant", 4)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.shelve(database, [copies[0]], place.shelves[0], before=3, user_id=None)  # avant le dos affiché au rang 3
    assert _order(database, place.shelves[0]) == [copies[1], copies[0], copies[2], copies[3]]
    holdings.shelve(database, [copies[3]], place.shelves[0], before=1, user_id=None)
    assert _order(database, place.shelves[0]) == [copies[3], copies[1], copies[0], copies[2]]
    _assert_positions_continuous(settings)


def test_placing_before_a_spine_of_another_shelf(settings, database, place):
    copies = _new_copies(settings, database, "Ailleurs", 3)
    holdings.shelve(database, copies[:2], place.shelves[0], user_id=None)
    holdings.shelve(database, [copies[2]], place.shelves[1], user_id=None)
    holdings.shelve(database, [copies[2]], place.shelves[0], before=2, user_id=None)
    assert _order(database, place.shelves[0]) == [copies[0], copies[2], copies[1]]


def test_journal_keeps_what_an_undo_needs(settings, database, place):
    _new_copies(settings, database)
    holdings.shelve_by_code(database, "EX-000001", place.shelves[0], user_id=None)
    details = _journal(settings, "copy.shelve_scan")[-1][1]
    assert (details["de_id"], details["statut_avant"], details["version"]) == (None, "in_processing", 2)


# --- A14 : ranger un exemplaire à sa place actuelle n'écrit rien ------------------------------------------------------
def _footprint(settings, place):
    """Tout ce qu'un déplacement modifie : rangs, versions et dates des exemplaires et des lieux, taille du journal."""
    return (
        _rows(settings, "SELECT id, location_id, shelf_position, version, updated_at, status FROM copies ORDER BY id"),
        _rows(settings, "SELECT id, version, updated_at FROM locations ORDER BY id"),
        _rows(settings, "SELECT count(*) FROM audit_log")[0][0],
    )


@pytest.mark.parametrize(
    "request_kind",
    ["même rang", "avant le voisin de droite", "fin, déjà dernier", "sans rang, déjà dernier", "avant soi-même"],
)
def test_moving_to_the_current_place_writes_nothing(settings, database, place, request_kind):
    copies = _new_copies(settings, database, "Sans effet", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    before = _footprint(settings, place)
    options = {
        "même rang": ([copies[1]], {"position": 2}),
        "avant le voisin de droite": ([copies[1]], {"before": 3}),
        "fin, déjà dernier": ([copies[2]], {"before": 4}),
        "sans rang, déjà dernier": ([copies[2]], {}),
        "avant soi-même": ([copies[0]], {"before": 1}),
    }[request_kind]
    result = holdings.shelve(database, options[0], place.shelves[0], user_id=None, **options[1])
    assert result.changed is False and result.already_there is True
    assert _footprint(settings, place) == before


def test_group_already_in_place_and_in_order_writes_nothing(settings, database, place):
    copies = _new_copies(settings, database, "Groupe immobile", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    before = _footprint(settings, place)
    assert holdings.plan_shelving(database, copies[1:], place.shelves[0], position=2).unchanged is True
    assert holdings.shelve(database, copies[1:], place.shelves[0], position=2, user_id=None).changed is False
    assert _footprint(settings, place) == before
    assert holdings.plan_shelving(database, list(reversed(copies[1:])), place.shelves[0], position=2).unchanged is False


def test_virtual_place_again_writes_nothing(settings, database, place):
    (copy_id,) = _new_copies(settings, database)
    holdings.shelve(database, [copy_id], place.virtual, user_id=None)
    before = _footprint(settings, place)
    assert holdings.shelve(database, [copy_id], place.virtual, user_id=None).changed is False
    assert _footprint(settings, place) == before


def test_a_real_move_is_still_written(settings, database, place):
    copies = _new_copies(settings, database, "Vraiment", 3)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    version = _copy(database, copies[2]).version
    result = holdings.shelve(database, [copies[2]], place.shelves[0], position=1, user_id=None)
    assert result.changed is True
    assert _order(database, place.shelves[0]) == [copies[2], copies[0], copies[1]]
    assert _copy(database, copies[2]).version == version + 1
    mixed = holdings.shelve(database, copies[:2], place.shelves[0], position=1, user_id=None)  # l'un bouge, l'autre non
    assert mixed.changed is True


def test_a_missing_copy_left_in_place_is_not_refused(settings, database, place):
    """Introuvable, il garde sa place : l'y « remettre » ne change rien, et ne doit pas être refusé."""
    copies = _new_copies(settings, database, "Introuvable", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    holdings.change_status(database, copies[0], "missing", user_id=None)
    before = _footprint(settings, place)
    assert holdings.shelve(database, [copies[0]], place.shelves[0], before=2, user_id=None).changed is False
    assert _footprint(settings, place) == before
    with pytest.raises(BusinessError, match="retrouvé"):  # un vrai déplacement reste refusé
        holdings.shelve(database, [copies[0]], place.shelves[1], user_id=None)


def test_outdated_view_is_still_refused_even_without_effect(settings, database, place):
    copies = _new_copies(settings, database, "Périmé", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    with pytest.raises(ConflictError):
        outdated = {copies[0]: 99}
        holdings.shelve(database, [copies[0]], place.shelves[0], position=1, expected_versions=outdated, user_id=None)


def test_undo_with_nothing_to_restore_writes_nothing(settings, database, place):
    (copy_id,) = _new_copies(settings, database)
    holdings.shelve(database, [copy_id], place.shelves[0], user_id=None)
    copy = _copy(database, copy_id)
    before = _footprint(settings, place)
    holdings.undo_shelving(
        database,
        copy_id,
        previous_location_id=place.shelves[0],
        previous_position=1,
        previous_status=copy.status,
        expected_version=copy.version,
        user_id=None,
    )
    assert _footprint(settings, place) == before


def test_scanning_a_copy_already_there_writes_nothing(settings, database, place):
    copies = _new_copies(settings, database, "Rescanné", 2)
    holdings.shelve(database, copies, place.shelves[0], user_id=None)
    before = _footprint(settings, place)
    result = holdings.shelve_by_code(database, "EX-000001", place.shelves[0], user_id=None)
    assert result.already_there and _footprint(settings, place) == before
