# SPDX-License-Identifier: EUPL-1.2
"""Lecture UNIMARC des réponses enregistrées : chaque fichier est lu, aucune donnée d'exemplaire n'en sort."""

from __future__ import annotations

from pathlib import Path

import pytest
from defusedxml import ElementTree as SafeElementTree

from biblyo.modules.enrichment.connectors import unimarc
from tests.sources import FIXTURES, candidate, candidates, load

ALL = sorted(p.relative_to(FIXTURES).with_suffix("").as_posix() for p in FIXTURES.glob("*/*.xml"))


@pytest.mark.parametrize("name", ALL)
def test_every_recorded_response_is_readable(name):
    found = candidates(name)
    for item in found:
        assert item.external_id, name
        assert item.work.title.value or item.record_kind == "set", name
        assert all(not tag.startswith("9") for tag, _ind, _subs in item.raw)
        assert all(all(code != "5" for code, _v in subs) for _t, _i, subs in item.raw)


def test_intermarc_and_dublin_core_are_not_read():
    assert candidates("bnf/bd-asterix-intermarc") == []
    assert candidates("bnf/bd-asterix-dublincore") == []
    assert candidates("sudoc/sru-dublincore") == []


def test_petit_prince_from_bnf():
    item = candidate("bnf/isbn-exact-petit-prince", "ark:/12148/cb41023439w")
    assert item.work.title.value == "Le petit prince"
    assert item.edition.isbn13 == ("9782070612758",)
    assert item.edition.ean == ("9782070612758",)
    assert item.edition.publisher is not None and item.edition.publisher.value == "Gallimard"
    assert item.edition.place is not None and item.edition.place.value == "Paris"  # « [Paris] » sans crochets
    assert item.edition.year is not None and (item.edition.year.whole, item.edition.year.raw) == (2007, "DL 2007")
    assert item.edition.pages is not None and item.edition.pages.whole == 113
    assert item.edition.collection is not None and item.edition.collection.value == "Folio junior"
    assert item.edition.collection_number is not None and item.edition.collection_number.value == "100"
    assert item.edition.language is not None and item.edition.language.value == "fre"
    assert item.work.volume is not None and item.work.volume.origin == "225$v"  # un numéro de collection, pas un tome
    assert item.work.summary is not None and item.work.summary.provider == "Electre"
    assert item.work.summary.reuse == "third_party"
    assert item.cover_sides == (1,)
    assert (item.source_agency, item.source_date) == ("FR-751131015", "20070503")
    assert item.record_status == "validated" and item.record_kind == "edition" and item.medium == "text"
    [person] = item.work.contributors
    assert (person.surname, person.forename, person.function_code, person.authority_id) == (
        "Saint-Exupéry",
        "Antoine de",
        "070",
        "11923342",
    )
    assert item.url == "https://catalogue.bnf.fr/ark:/12148/cb41023439w"


def test_petit_prince_from_sudoc_links_to_the_bnf_record():
    item = candidate("sudoc/sru-isbn-petit-prince", "117074047")
    assert item.linked_ids[0] == "ark:/12148/cb41023439w"
    assert (item.source_agency, item.source_date) == ("Abes", "20260701")
    assert item.edition.publisher is not None and item.edition.publisher.origin == "214$c"
    assert item.work.summary is not None and item.work.summary.provider == "quatrième de couverture"
    assert len(item.work.subjects) == 12 and item.work.subjects[0].value == "Écoles"
    assert item.cover_sides == ()


def test_the_same_record_by_ppn_reads_alike():
    by_sru = candidate("sudoc/sru-isbn-petit-prince", "117074047")
    by_ppn = candidate("sudoc/notice-par-ppn", "117074047")
    assert by_ppn.work.title == by_sru.work.title
    assert by_ppn.edition.isbn13 == by_sru.edition.isbn13
    assert by_ppn.work.summary == by_sru.work.summary


def test_holdings_of_the_sudoc_never_reach_the_candidate(tmp_path: Path):
    """Les zones d'exemplaire sont retirées même quand elles sont présentes : ce test les ajoute lui-même."""
    text = (FIXTURES / "sudoc" / "sru-isbn-petit-prince.xml").read_text(encoding="utf-8")
    planted = text.replace(
        '<datafield tag="010" ind1=" " ind2=" ">',
        '<datafield tag="930" ind1=" " ind2=" "><subfield code="5">123456789:987654321</subfield>'
        '<subfield code="a">843 SAI</subfield></datafield>'
        '<datafield tag="316" ind1=" " ind2=" "><subfield code="5">123456789:987654321</subfield>'
        '<subfield code="a">Don de Jean Dupont (2020)</subfield></datafield>'
        '<datafield tag="702" ind1=" " ind2=" "><subfield code="5">123456789:987654321</subfield>'
        '<subfield code="a">Dupont</subfield></datafield>'
        '<datafield tag="010" ind1=" " ind2=" ">',
        1,
    )
    assert planted != text
    [element] = list(unimarc.iter_records(SafeElementTree.fromstring(planted.encode("utf-8"))))
    record = unimarc.read(element)
    item = unimarc.candidate(record, source="sudoc", url="", external_id="117074047")
    flat = repr(item)
    assert "Dupont" not in flat and "843 SAI" not in flat and "987654321" not in flat
    assert record.get("930") == [] and record.get("316") == []


def test_asterix_reconverted_record():
    item = candidate("bnf/bd-asterix-unimarc", "ark:/12148/cb43404620t")
    assert item.work.title.value == "Astérix le Gaulois"
    assert item.work.series is not None and item.work.series.value == "Une aventure d'Astérix"
    assert item.work.volume is not None and (item.work.volume.value, item.work.volume.origin) == (1.0, "461$v")
    assert item.edition.statement is not None and item.edition.statement.value == "Edition 20"
    uderzo, goscinny = item.work.contributors
    assert (uderzo.function_code, uderzo.mention) == ("070", "drawing")
    assert (goscinny.function_code, goscinny.mention) == ("070", "text")
    assert item.record_status == "validated"
    assert item.work.summary is None and item.cover_sides == ()


def test_set_records_are_recognized():
    series = candidate("bnf/serie-plusieurs-editions", "ark:/12148/cb371381846")
    assert series.record_kind == "set"
    ongoing = candidate("bnf/manga-serie-notices-d-ensemble", "ark:/12148/cb409245134")
    assert ongoing.record_kind == "set"
    volume = candidate("bnf/manga-serie-notices-d-ensemble", "ark:/12148/cb44207688z")
    assert volume.record_kind == "edition"
    assert volume.work.volume is not None and (volume.work.volume.value, volume.work.volume.origin) == (72.0, "461$v")


def test_sparse_records_are_recognized():
    first, second = candidates("bnf/isbn-deux-notices-elementaires")
    assert {first.record_status, second.record_status} == {"sparse"}
    assert first.work.volume is not None and first.work.volume.origin == "200$h"
    assert any(p.function_text == "Traducteur" for p in second.work.contributors)
    assert all(p.function_code == "" for p in second.work.contributors)


def test_non_text_documents_are_marked():
    items = {item.external_id: item for item in candidates("bnf/accents-apostrophes")}
    assert items["ark:/12148/cb379853326"].medium == "other"  # cassette audio
    assert items["ark:/12148/cb465628739"].medium == "other"  # DVD
    assert items["ark:/12148/cb46532085g"].medium == "text"
    assert items["ark:/12148/cb379853326"].edition.publisher is None  # « [s.n.] » n'est pas un éditeur


def test_sudoc_volume_in_the_title():
    item = candidate("sudoc/sru-bd-tome-en-200h", "094733023")
    assert item.work.title.value == "Le tour de Gaule d'Astérix"
    assert item.work.series is not None and item.work.series.value == "Une aventure d'Astérix"
    assert item.work.volume is not None and (item.work.volume.value, item.work.volume.origin) == (5.0, "200$h")
    roman = candidate("sudoc/sru-bd-tome-en-200h", "010791833")
    assert roman.work.volume is not None and roman.work.volume.value == 5.0  # « V » en 200$h, « 5 » en 461$v
    assert "\x98" not in roman.work.series.value if roman.work.series else True


def test_one_isbn_two_titles_in_the_sudoc():
    items = candidates("sudoc/sru-isbn-quatre-notices-deux-titres")
    assert {item.edition.isbn13 for item in items} == {("9782203001015",)}
    assert {item.work.title.value for item in items} == {"Tintin au Congo", "Les cigares du pharaon"}


def test_mentions_name_the_closest_verb():
    item = candidate("bnf/meme-titre-plusieurs-editeurs", "ark:/12148/cb500080595")
    mentions = {p.surname + " " + p.forename: p.mention for p in item.work.contributors}
    assert mentions["Brizzi Paul"] == "drawing"
    assert mentions["Maupassant Guy de"] == "original"


def test_invalid_isbn_in_a_record_is_kept_apart():
    item = candidate("bnf/titre-auteur-330-resultats", "ark:/12148/cb40930802j")
    assert item.edition.isbn13 == ("9782848970769",)
    assert item.edition.invalid_isbn == ()  # le $z (ISBN erroné) n'est pas lu du tout


def test_explain_responses_hold_no_record():
    assert candidates("bnf/explain") == []
    assert list(unimarc.iter_records(load("sudoc/explain"))) == []


def test_quoted_summary_is_third_party():
    item = candidate("sudoc/sru-isbn-deux-notices", "139262636")
    assert item.work.summary is not None and item.work.summary.reuse == "third_party"
    assert item.work.summary.provider == "citation"
