# SPDX-License-Identifier: EUPL-1.2
"""Requêtes CQL des deux sources et lecture de l'enveloppe SRU : diagnostics lus avant le nombre de résultats."""

from __future__ import annotations

import pytest

from biblyo.modules.enrichment.connectors.base import SearchQuery
from biblyo.modules.enrichment.connectors.sru import QueryError, bnf_query, read_envelope, sudoc_query
from tests.sources import load


def test_isbn_query_sends_both_forms_to_the_bnf():
    query = SearchQuery(level=1, isbn13="9782070612758", isbn10="2070612759")
    assert bnf_query(query) == 'bib.isbn any "9782070612758 2070612759"'
    assert sudoc_query(query) == "isb=9782070612758"


def test_isbn_979_has_one_form():
    assert bnf_query(SearchQuery(level=1, isbn13="9791090636071")) == 'bib.isbn any "9791090636071"'


def test_fuzzy_isbn_is_never_used():
    for level in range(1, 6):
        query = SearchQuery(
            level=level, isbn13="9782070612758", title="Titre", author="Auteur", publisher="Editeur", series="Serie"
        )
        assert "fuzzy" not in bnf_query(query).casefold()


def test_title_and_author_queries_are_cleaned():
    query = SearchQuery(level=2, title='L\'écume "des" jours and (', author="Vian, Boris")
    assert bnf_query(query) == 'bib.title all "ecume des jours" and bib.author all "vian boris" and bib.doctype any "a"'
    assert sudoc_query(query) == "mti=ecume des jours and aut=vian boris"


def test_publisher_query():
    query = SearchQuery(level=3, title="Le Horla", publisher="Hatier", year=2012)
    assert bnf_query(query) == 'bib.title all "le horla" and bib.publisher all "hatier" and bib.doctype any "a"'
    assert sudoc_query(query) == "mti=le horla and edi=hatier and apu=2012"


def test_series_query():
    query = SearchQuery(
        level=4, series="Une aventure d'Astérix", volume="5", title="Le tour de Gaule", author="Goscinny"
    )
    assert bnf_query(query) == (
        'bib.title all "une aventure asterix 5 le tour de gaule" and bib.author all "goscinny" and bib.doctype any "a"'
    )
    assert sudoc_query(query) == "mti=une aventure asterix le tour de gaule and aut=goscinny"


def test_broad_query_has_no_document_type():
    query = SearchQuery(level=5, title="Le grand Meaulnes", author="Alain-Fournier")
    assert bnf_query(query) == 'bib.anywhere all "le grand meaulnes alain fournier"'
    assert sudoc_query(query) == "tou=le grand meaulnes alain fournier"


@pytest.mark.parametrize(
    "query",
    [
        SearchQuery(level=1),
        SearchQuery(level=1, isbn13="978207061275X"),
        SearchQuery(level=2, title="Seul"),
        SearchQuery(level=3, title="Seul"),
        SearchQuery(level=4, title="Seul"),
        SearchQuery(level=5),
        SearchQuery(level=6, title="x", author="y"),
    ],
)
def test_impossible_queries_are_refused(query):
    with pytest.raises(QueryError):
        bnf_query(query)
    with pytest.raises(QueryError):
        sudoc_query(query)


def test_envelope_of_a_found_record():
    envelope = read_envelope(load("bnf/isbn-exact-petit-prince"))
    assert (envelope.total, len(envelope.records), envelope.diagnostics, envelope.refused) == (1, 1, (), False)


def test_envelope_of_no_result():
    envelope = read_envelope(load("bnf/isbn-inconnu"))
    assert (envelope.total, envelope.records, envelope.refused) == (0, (), False)


def test_sudoc_found_carries_a_zero_diagnostic():
    envelope = read_envelope(load("sudoc/sru-isbn-petit-prince"))
    assert [d.code for d in envelope.diagnostics] == ["0"] and envelope.refused is False


def test_sudoc_no_result_carries_a_harmless_diagnostic():
    envelope = read_envelope(load("sudoc/sru-isbn-inconnu"))
    assert envelope.total == 0 and envelope.records == ()
    assert [d.code for d in envelope.diagnostics] == ["61"]
    assert envelope.refused is False


def test_bnf_diagnostics_are_refusals():
    malformed = read_envelope(load("bnf/diagnostic-requete-malformee"))
    assert malformed.refused and malformed.diagnostics[0].code == "10"
    unknown = read_envelope(load("bnf/diagnostic-index-inconnu"))
    assert unknown.refused and unknown.diagnostics[0].message == "Index non supporté"


def test_envelope_announces_more_than_it_carries():
    envelope = read_envelope(load("bnf/titre-auteur-330-resultats"))
    assert envelope.total == 330 and len(envelope.records) == 5


SURROGATE = """<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
<srw:version>1.2</srw:version><srw:numberOfRecords>2</srw:numberOfRecords><srw:records>
<srw:record><srw:recordSchema>marcxchange</srw:recordSchema><srw:recordPacking>xml</srw:recordPacking>
<srw:recordData><mxc:record xmlns:mxc="info:lc/xmlns/marcxchange-v2" format="Unimarc" type="Bibliographic"
 id="ark:/12148/cb1">
<mxc:leader>00000cam  2200000   450 </mxc:leader><mxc:controlfield tag="001">FRBNF1</mxc:controlfield>
<mxc:datafield tag="200" ind1="1" ind2=" "><mxc:subfield code="a">L'écume des jours</mxc:subfield></mxc:datafield>
</mxc:record></srw:recordData><srw:recordPosition>1</srw:recordPosition></srw:record>
<srw:record><srw:recordSchema>marcxchange</srw:recordSchema><srw:recordPacking>xml</srw:recordPacking>
<srw:recordData><srw:diagnostics><sd:diagnostic xmlns:sd="http://www.loc.gov/zing/srw/diagnostic/">
<sd:uri>info:srw/diagnostic/1/131</sd:uri><sd:details>-20;</sd:details><sd:message>erreur de traitement</sd:message>
</sd:diagnostic></srw:diagnostics></srw:recordData><srw:recordPosition>2</srw:recordPosition></srw:record>
</srw:records></srw:searchRetrieveResponse>"""


def test_a_record_replaced_by_a_diagnostic_is_skipped_not_a_refusal():
    """Recette sur DEV, « L'écume des jours » : la BnF remplace une notice illisible par un diagnostic 1/131 dans son
    recordData. Seule cette notice manque ; la réponse n'est pas refusée."""
    from biblyo.modules.enrichment.connectors.xmlsafe import parse

    envelope = read_envelope(parse(SURROGATE.encode()))
    assert (envelope.refused, envelope.surrogates, len(envelope.records), envelope.total) == (False, 1, 1, 2)
