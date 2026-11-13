# SPDX-License-Identifier: EUPL-1.2
"""Score explicable, conditions éliminatoires, déclassements et classes : une ligne de barème par test."""

from __future__ import annotations

from dataclasses import replace

import pytest

from biblyo.modules.enrichment.connectors.base import Candidate, EditionData, Number, Person, Text, WorkData
from biblyo.modules.enrichment.matching import LocalFiche, assess, classify, group_key, outcome
from tests.sources import candidate, candidates


def make(
    title: str = "Astérix le Gaulois",
    *,
    isbn: tuple[str, ...] = ("9782012101333",),
    publisher: str = "Hachette",
    year: int | None = 1999,
    pages: int | None = 48,
    collection: str = "",
    series: str = "",
    volume: float | None = None,
    volume_origin: str = "461$v",
    people: tuple[tuple[str, str], ...] = (("Goscinny", "René"), ("Uderzo", "Albert")),
    language: str = "fre",
    kind: str = "edition",
    status: str = "validated",
    medium: str = "text",
    source: str = "bnf",
    external_id: str = "ark:/12148/cb43404620t",
) -> Candidate:
    return Candidate(
        source=source,
        external_id=external_id,
        url="",
        work=WorkData(
            title=Text(title, title, "200$a"),
            contributors=tuple(Person(f"{f} {s}", s, f, function_code="070") for s, f in people),
            series=Text(series, series, "461$t") if series else None,
            volume=Number(volume, str(volume), volume_origin) if volume is not None else None,
        ),
        edition=EditionData(
            isbn13=isbn,
            publisher=Text(publisher, publisher, "210$c") if publisher else None,
            year=Number(year, str(year), "210$d") if year else None,
            pages=Number(pages, f"{pages} p.", "215$a") if pages else None,
            collection=Text(collection, collection, "225$a") if collection else None,
            language=Text(language, language, "101$a") if language else None,
        ),
        record_kind=kind,  # type: ignore[arg-type]
        record_status=status,  # type: ignore[arg-type]
        medium=medium,  # type: ignore[arg-type]
    )


LOCAL = LocalFiche(
    title="Astérix le Gaulois",
    contributors=("René Goscinny", "Albert Uderzo"),
    isbn13=("9782012101333",),
    publisher="Hachette",
    year=1999,
    pages=48,
)


def points(local: LocalFiche, item: Candidate, key: str) -> tuple[int, int]:
    criterion = next(c for c in assess(local, item).criteria if c.key == key)
    return criterion.points, criterion.possible


# --- Barème, une ligne par test ---------------------------------------------------------------------------------------
def test_identical_title_scores_thirty():
    assert points(LOCAL, make(), "title") == (30, 30)


def test_very_close_title_scores_twenty_two():
    assert points(LOCAL, make("Astérix le Gaulois 1"), "title") == (22, 30)


def test_close_title_scores_ten():
    assert points(replace(LOCAL, title="Le tour de Gaule d'Astérix"), make("Le Tour de Gaule"), "title") == (10, 30)


def test_all_authors_found_scores_twenty():
    assert points(LOCAL, make(), "authors") == (20, 20)


def test_one_author_found_scores_fourteen():
    assert points(LOCAL, make(people=(("Goscinny", "René"),)), "authors") == (14, 20)


def test_no_author_found_scores_zero():
    assert points(LOCAL, make(people=(("Hergé", ""),)), "authors") == (0, 20)


def test_same_publisher_scores_fifteen():
    assert points(LOCAL, make(publisher="Hachette"), "publisher") == (15, 15)


def test_related_publisher_scores_eight():
    assert points(LOCAL, make(publisher="Hachette jeunesse"), "publisher") == (8, 15)


def test_same_volume_scores_fifteen():
    local = replace(LOCAL, series="Une aventure d'Astérix", volume=1.0)
    assert points(local, make(series="Une aventure d'Astérix", volume=1.0), "volume") == (15, 15)


def test_same_year_scores_ten_and_neighbour_six():
    assert points(LOCAL, make(year=1999), "year") == (10, 10)
    assert points(LOCAL, make(year=2000), "year") == (6, 10)
    assert points(LOCAL, make(year=2019), "year") == (0, 10)


def test_pages_within_five_percent_score_five():
    assert points(LOCAL, make(pages=50), "pages") == (5, 5)
    assert points(LOCAL, make(pages=126), "pages") == (0, 5)


def test_same_collection_scores_five():
    local = replace(LOCAL, collection="Une aventure d'Astérix")
    assert points(local, make(collection="Une aventure d'Astérix"), "collection") == (5, 5)


def test_absent_local_fields_do_not_count_in_the_evidence():
    sparse_local = LocalFiche(title="Astérix le Gaulois")
    result = assess(sparse_local, make())
    assert (result.evidence, result.score) == (30, 100)
    full = assess(LOCAL, make())
    assert (full.evidence, full.score) == (80, 100)


def test_explanation_is_readable():
    result = assess(replace(LOCAL, publisher="Dargaud"), make())
    assert "Éditeur différent : Dargaud contre Hachette" in result.conflicting
    assert "Titre identique" in result.agreeing
    assert result.explanation["score"] == result.score == round(100 * 65 / 80)


# --- Exclusions et déclassements --------------------------------------------------------------------------------------
def test_a_set_record_is_excluded():
    result = assess(LOCAL, make(kind="set"))
    assert result.excluded.startswith("Notice d'ensemble")


def test_another_medium_is_excluded():
    assert assess(LOCAL, make(medium="other")).excluded.startswith("Autre type")


def test_another_language_is_excluded():
    assert assess(LOCAL, make(language="ger")).excluded.startswith("Langue différente")


def test_a_plainly_different_title_is_excluded_even_with_the_same_isbn():
    result = assess(LOCAL, make("Les cigares du pharaon"))
    assert result.isbn_equal and result.excluded == "Titre manifestement différent"


def test_a_different_volume_is_excluded():
    local = replace(LOCAL, series="Une aventure d'Astérix", volume=1.0)
    assert assess(local, make(series="Une aventure d'Astérix", volume=5.0)).excluded == "Numéro de tome différent"


def test_a_collection_number_is_not_a_volume():
    """« Folio junior, 100 » : le 225$v n'est un tome que si la série locale porte ce nom."""
    local = LocalFiche(title="Le petit prince", series="Le Petit Prince", volume=1.0)
    item = make("Le petit prince", series="Folio junior", volume=100.0, volume_origin="225$v")
    result = assess(local, item)
    assert result.excluded == ""
    assert next(c for c in result.criteria if c.key == "volume").note == "Tome non renseigné par la source"


def test_a_contradictory_series_demotes_and_blocks_the_edition():
    local = replace(LOCAL, series="Les aventures de Tintin")
    result = assess(local, make(series="Une aventure d'Astérix"))
    assert any(reason.startswith("Série contradictoire") for reason in result.demoted)
    assert not result.edition_usable


def test_another_isbn_blocks_the_edition_but_not_the_work():
    result = assess(LOCAL, make(isbn=("9782014001150",), year=2019))
    assert "Édition incompatible : un autre ISBN" in result.demoted
    assert not result.edition_usable and not result.excluded


def test_different_publisher_and_year_block_the_edition():
    result = assess(replace(LOCAL, isbn13=()), make(isbn=(), publisher="Dargaud", year=1961))
    assert "Édition incompatible : éditeur et année différents" in result.demoted
    assert not result.edition_usable


def test_an_insufficient_sparse_record_is_demoted():
    result = assess(LOCAL, make(status="sparse", publisher=""))
    assert "Notice élémentaire insuffisante" in result.demoted
    assert classify(LOCAL, [result])[0].match_class != "exact"


def test_a_sufficient_sparse_record_can_still_be_exact():
    result = assess(LOCAL, make(status="sparse"))
    assert result.demoted == ()
    assert classify(LOCAL, [result])[0].match_class == "exact"


# --- Classes ----------------------------------------------------------------------------------------------------------
def test_exact_needs_an_equal_isbn():
    [exact] = classify(LOCAL, [assess(LOCAL, make())])
    assert exact.match_class == "exact" and exact.isbn_equal


def test_a_perfect_score_without_isbn_is_strong_never_exact():
    local = replace(LOCAL, isbn13=())
    [strong] = classify(local, [assess(local, make(isbn=()))])
    assert (strong.score, strong.match_class) == (100, "strong")


def test_strong_needs_enough_evidence():
    local = LocalFiche(title="Astérix le Gaulois", contributors=("René Goscinny",))
    [result] = classify(local, [assess(local, make(isbn=()))])
    assert (result.score, result.evidence, result.match_class) == (100, 50, "uncertain")


def test_strong_needs_a_lead_over_the_next_edition():
    local = replace(LOCAL, isbn13=(), year=None, pages=None)
    first = make(isbn=(), year=1999, external_id="a")
    second = make(isbn=(), year=2019, external_id="b")
    results = classify(local, [assess(local, first), assess(local, second)])
    assert {r.match_class for r in results} == {"multiple"}


def test_uncertain_between_sixty_and_eighty_four():
    local = replace(LOCAL, isbn13=(), publisher="Dargaud", year=1961)
    [result] = classify(local, [assess(local, make(isbn=(), publisher="Hachette", year=1999))])
    assert 60 <= result.score < 85 and result.match_class == "uncertain"


def test_below_forty_is_discarded():
    local = LocalFiche(title="Astérix le Gaulois", contributors=("Hergé",), publisher="Casterman", year=1960, pages=62)
    [result] = classify(local, [assess(local, make(isbn=()))])
    assert result.score < 40 and result.match_class == "none"


def test_one_isbn_two_titles_gives_several_candidates():
    """Essai 11 : au Sudoc, 2-203-00101-1 porte *Tintin au Congo* et *Les cigares du pharaon*."""
    local = LocalFiche(title="Tintin au Congo", contributors=("Hergé",), isbn13=("9782203001015",))
    results = classify(local, [assess(local, c) for c in candidates("sudoc/sru-isbn-quatre-notices-deux-titres")])
    classes = {r.candidate.work.title.value: r.match_class for r in results if r.match_class != "excluded"}
    assert classes["Tintin au Congo"] == "multiple"
    assert all(r.match_class != "exact" for r in results)
    assert outcome(results, complete=True) == "multiple"


def test_several_records_of_one_title_for_one_isbn_count_as_one_exact():
    local = LocalFiche(title="Astérix le Gaulois", contributors=("René Goscinny",), isbn13=("9782012101333",))
    results = classify(local, [assess(local, c) for c in candidates("sudoc/sru-isbn-deux-notices")])
    assert [r.match_class for r in results] == ["exact", "exact"]
    assert outcome(results, complete=True) == "exact"


def test_set_and_sparse_records_of_a_long_series():
    """Essai 8 : notices d'ensemble écartées, notice élémentaire classée après la notice validée."""
    local = LocalFiche(
        title="One piece", contributors=("Eiichiro Oda",), series="One piece", volume=72.0, publisher="Glénat"
    )
    results = classify(local, [assess(local, c) for c in candidates("bnf/manga-serie-notices-d-ensemble")])
    sets = [r for r in results if r.candidate.record_kind == "set"]
    assert sets and all(r.match_class == "excluded" for r in sets)
    best = results[0]
    assert best.candidate.external_id == "ark:/12148/cb44207688z" and best.match_class == "strong"


def test_old_edition_without_isbn():
    """Essai 2 : l'édition de 1913 se distingue des rééditions par l'éditeur et l'année."""
    local = LocalFiche(
        title="Le Grand Meaulnes", contributors=("Alain-Fournier",), publisher="Émile-Paul frères", year=1913
    )
    results = classify(local, [assess(local, c) for c in candidates("bnf/ancien-sans-isbn")])
    assert results[0].candidate.edition.year is not None and results[0].candidate.edition.year.whole == 1913
    assert results[0].match_class in ("strong", "multiple")
    modern = [r for r in results if r.candidate.edition.year and r.candidate.edition.year.whole == 2023]
    assert all(not r.edition_usable for r in modern)


def test_exact_match_from_the_recorded_petit_prince():
    local = LocalFiche(title="Le Petit Prince", contributors=("Antoine de Saint-Exupéry",), isbn13=("9782070612758",))
    bnf = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    sudoc = candidate("sudoc/sru-isbn-petit-prince", "117074047")
    results = classify(local, [assess(local, bnf), assess(local, sudoc)])
    assert [r.match_class for r in results] == ["exact", "exact"]
    assert len({r.group for r in results}) == 1  # la même édition, vue par deux sources, grâce à l'ARK cité
    assert outcome(results, complete=True) == "exact"


def test_group_key_falls_back_on_isbn_publisher_year():
    item = make(source="sudoc", external_id="165964073")
    assert group_key(item) == "isbn:9782012101333:hachette:1999"


def test_outcome_without_all_sources_is_incomplete():
    assert outcome([], complete=False) == "incomplete"
    assert outcome([], complete=True) == "none"


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5])
def test_levels_have_a_label(level):
    from biblyo.modules.enrichment.matching import LEVELS

    assert LEVELS[level]


def test_without_a_local_title_the_title_is_not_judged():
    """Recherche libre par ISBN seul : le titre n'est ni compté ni éliminatoire."""
    local = LocalFiche(title="", isbn13=("9782012101333",))
    result = assess(local, make())
    assert result.excluded == "" and all(c.key != "title" for c in result.criteria)
    assert classify(local, [result])[0].match_class == "exact"


# --- Titre de la série : recette sur DEV, One Piece tome 72 ----------------------------------------------------------
ONE_PIECE = LocalFiche(title="One Piece", contributors=("Eiichirō Oda",), isbn13=("9782344004319",), series="One Piece",
                       volume=72.0)


def _one_piece(title: str, *, volume: float = 72.0, isbn: tuple[str, ...] = ("9782344004319",), source: str = "sudoc",
               origin: str = "200$h") -> Candidate:
    return make(title, isbn=isbn, publisher="Glénat", year=2014, pages=None, series="One piece", volume=volume,
                volume_origin=origin, people=(("Oda", "Eiichirō"),), source=source, external_id=f"{source}:{title}")


def test_the_series_title_with_the_same_volume_is_the_title():
    """Le Sudoc : 200$a « One piece », $h 72, $i « Les oubliés de Dressrosa ». La fiche porte le titre de la série."""
    result = assess(ONE_PIECE, _one_piece("Les oubliés de Dressrosa"))
    assert not result.excluded
    title = next(c for c in result.criteria if c.key == "title")
    assert (title.points, title.note) == (30, "Titre de la série, même livre : « One piece »")


def test_the_series_title_alone_never_matches_another_volume():
    local = replace(ONE_PIECE, isbn13=())
    result = assess(local, _one_piece("Les oubliés de Dressrosa", volume=73.0, isbn=("9782344005859",)))
    assert result.excluded  # tome différent, et le titre de la série ne vaut que pour le même livre


def test_the_series_title_needs_the_same_book():
    local = replace(ONE_PIECE, isbn13=(), volume=None)
    result = assess(local, _one_piece("Les oubliés de Dressrosa", isbn=("9782344005859",)))
    assert result.excluded == "Titre manifestement différent"


def test_one_isbn_with_series_title_and_volume_title_stays_exact():
    bnf = _one_piece("One piece", source="bnf", origin="461$v")
    sudoc = _one_piece("Les oubliés de Dressrosa")
    results = classify(ONE_PIECE, [assess(ONE_PIECE, c) for c in (bnf, sudoc)])
    assert [r.match_class for r in results] == ["exact", "exact"]
    assert outcome(results, complete=True) == "exact"


def test_one_isbn_two_volumes_of_one_series_stays_several_candidates():
    bnf = _one_piece("One piece", source="bnf", origin="461$v")
    other = _one_piece("Les oubliés de Dressrosa", volume=73.0)
    results = classify(ONE_PIECE, [assess(ONE_PIECE, c) for c in (bnf, other)])
    assert all(r.match_class != "exact" for r in results)


def test_another_publisher_alone_blocks_the_edition():
    """Recette sur DEV : fiche « L'écume des jours », Gallimard, sans année ; candidat Futuropolis 2020."""
    local = LocalFiche(title="L'écume des jours", contributors=("Boris Vian",), publisher="Gallimard")
    result = assess(local, make("L'écume des jours", isbn=("9782754829106",), publisher="Futuropolis", year=2020,
                                people=(("Vian", "Boris"),)))
    assert "Édition incompatible : autre éditeur" in result.demoted
    assert not result.edition_usable and not result.excluded


def test_a_candidate_without_publisher_keeps_its_edition():
    local = LocalFiche(title="L'écume des jours", contributors=("Boris Vian",), publisher="Gallimard")
    result = assess(local, make("L'écume des jours", isbn=(), publisher="", year=1963, people=(("Vian", "Boris"),)))
    assert result.edition_usable


def test_the_recorded_volume_title_record_of_the_sudoc_is_exact():
    """Réponse réelle du Sudoc, relevée le 12 novembre 2026 : 200$a « One piece », $h « Tome [72] », $i le titre."""
    results = classify(ONE_PIECE, [assess(ONE_PIECE, c) for c in candidates("sudoc/sru-isbn-tome-titre-propre")])
    assert [(r.candidate.work.title.value, r.match_class) for r in results] == [("Les oubliés de Dressrosa", "exact")]
