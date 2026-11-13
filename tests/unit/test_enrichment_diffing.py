# SPDX-License-Identifier: EUPL-1.2
"""Différences champ par champ : cases précochées, remplacements jamais silencieux, résumés tiers non copiés."""

from __future__ import annotations

from biblyo.modules.enrichment.connectors.base import Person
from biblyo.modules.enrichment.diffing import LocalValues, diff, propose_role
from tests.sources import candidate

EMPTY = LocalValues(title="Le petit prince")


def by_field(items, field):
    return next(item for item in items if item.field == field)


def test_empty_fields_are_prechecked_on_an_exact_match():
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    diffs = diff(EMPTY, item, match_class="exact", edition_usable=True)
    assert by_field(diffs, "publisher").action == "fill" and by_field(diffs, "publisher").prechecked
    assert by_field(diffs, "year").proposed == "2007" and by_field(diffs, "year").prechecked
    assert by_field(diffs, "pages").proposed == "113"
    assert by_field(diffs, "title").action == "identical"


def test_never_prechecked_fields():
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    diffs = diff(EMPTY, item, match_class="exact", edition_usable=True)
    for field in ("isbn", "cover", "contributors"):
        assert not by_field(diffs, field).prechecked, field


def test_nothing_is_prechecked_on_an_uncertain_match():
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    diffs = diff(EMPTY, item, match_class="uncertain", edition_usable=True)
    assert not any(d.prechecked for d in diffs)


def test_a_filled_field_is_a_replacement_never_prechecked():
    local = LocalValues(title="Le petit prince", publisher="Galimard", year="2007")
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    diffs = diff(local, item, match_class="exact", edition_usable=True)
    publisher = by_field(diffs, "publisher")
    assert (publisher.action, publisher.prechecked, publisher.local, publisher.proposed) == (
        "replace",
        False,
        "Galimard",
        "Gallimard",
    )
    assert by_field(diffs, "year").action == "identical"


def test_third_party_summary_is_not_copyable():
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    summary = by_field(diff(EMPTY, item, match_class="exact", edition_usable=True), "summary")
    assert summary.action == "not_copyable" and not summary.applicable and "Electre" in summary.note


def test_established_summary_is_copyable():
    item = candidate("sudoc/sru-isbn-quatre-notices-deux-titres", "013211765")
    summary = by_field(
        diff(LocalValues(title="Tintin au Congo"), item, match_class="exact", edition_usable=True), "summary"
    )
    assert summary.action == "fill" and summary.applicable and not summary.prechecked


def test_edition_fields_are_withheld_when_the_edition_is_not_usable():
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    diffs = diff(EMPTY, item, match_class="uncertain", edition_usable=False)
    assert all(d.level != "edition" for d in diffs)
    assert by_field(diffs, "contributors").level == "work"


def test_series_and_volume_are_shown_aside_never_applied():
    item = candidate("bnf/bd-asterix-unimarc", "ark:/12148/cb43404620t")
    aside = by_field(
        diff(LocalValues(title="Astérix le Gaulois"), item, match_class="exact", edition_usable=True), "series"
    )
    assert aside.level == "aside" and aside.action == "info" and not aside.applicable
    assert aside.proposed == "Une aventure d'Astérix · tome 1"


def test_contributors_already_present_keep_their_role():
    local = LocalValues(title="Astérix le Gaulois", contributors=(("René Goscinny", "author"),))
    item = candidate("bnf/bd-asterix-unimarc", "ark:/12148/cb43404620t")
    proposals = by_field(diff(local, item, match_class="exact", edition_usable=True), "contributors").payload
    goscinny = next(p for p in proposals if p.name == "René Goscinny")
    uderzo = next(p for p in proposals if p.name == "Albert Uderzo")
    assert goscinny.already_present and goscinny.present_role == "author"
    assert (goscinny.role, uderzo.role) == ("scenarist", "artist")
    assert goscinny.uncertain and uderzo.uncertain  # le code « auteur » et la mention se contredisent


def test_roles_from_function_codes_and_mentions():
    assert propose_role(Person("Albert Uderzo", "Uderzo", "Albert", function_code="440"))[0] == "illustrator"
    assert (
        propose_role(Person("Albert Uderzo", "Uderzo", "Albert", function_code="070", mention="drawing"))[0] == "artist"
    )
    assert propose_role(Person("René Goscinny", "Goscinny", "René", function_code="070"))[0] == "author"
    assert propose_role(Person("Gudrun Penndorf", "Penndorf", "Gudrun", function_code="730"))[0] == "translator"
    role, _origin, uncertain = propose_role(Person("Djamel Rabahi", "Rabahi", "Djamel", function_text="Traducteur"))
    assert (role, uncertain) == ("translator", True)
    role, _origin, uncertain = propose_role(Person("X Y", "Y", "X"))
    assert (role, uncertain) == ("author", True)


def test_keywords_are_proposed_and_genres_only_from_the_taxonomy():
    item = candidate("sudoc/sru-isbn-petit-prince", "117074047")
    local = LocalValues(title="Le Petit Prince", keywords=("Amitié",), genres=(), known_genres=("Contes", "Roman"))
    diffs = diff(local, item, match_class="exact", edition_usable=True)
    keywords = by_field(diffs, "keywords")
    assert "Amitié" not in keywords.proposed.split(", ") and "Écoles" in keywords.proposed
    assert not keywords.prechecked
    genres = by_field(diffs, "genres")
    assert genres.proposed == "Contes"  # « Roman pour la jeunesse » n'est pas « Roman » : aucune catégorie inventée


def test_cover_replacement_is_a_dedicated_case():
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    cover = by_field(
        diff(LocalValues(title="Le petit prince", has_cover=True), item, match_class="exact", edition_usable=True),
        "cover",
    )
    assert cover.action == "replace" and not cover.prechecked


def test_a_collection_number_is_not_shown_as_a_volume():
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    diffs = diff(EMPTY, item, match_class="exact", edition_usable=True)
    assert all(d.field != "series" for d in diffs)  # « Folio junior, 100 » est une collection, pas un tome
    assert by_field(diffs, "collection_number").proposed == "100"
