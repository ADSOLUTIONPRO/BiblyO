# SPDX-License-Identifier: EUPL-1.2
"""Tests de contrat : appels réels, espacés, à la demande seulement (`make contract`). Jamais dans les barrières.

Ils vérifient que les services répondent encore comme les réponses enregistrées le disent. Une dizaine d'appels,
une seconde entre deux, en lecture seule, sous l'agent déclaré de biblyO.
"""

from __future__ import annotations

import time

import pytest

from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.base import CoverRequest, SearchQuery
from biblyo.modules.enrichment.connectors.bnf import BnfConnector
from biblyo.modules.enrichment.connectors.sudoc import SudocConnector
from biblyo.modules.enrichment.connectors.transport import HttpTransport

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def transport():
    instance = HttpTransport(registry.HOSTS)
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _pause():
    yield
    time.sleep(1.0)


def test_bnf_isbn_exact(transport):
    result = BnfConnector(transport).search(SearchQuery(level=1, isbn13="9782070612758", isbn10="2070612759"))
    assert result.status == "found"
    assert result.candidates[0].external_id == "ark:/12148/cb41023439w"
    assert result.candidates[0].edition.isbn13 == ("9782070612758",)


def test_bnf_unknown_isbn(transport):
    assert BnfConnector(transport).search(SearchQuery(level=1, isbn13="9782999999992")).status == "not_found"


def test_bnf_title_author(transport):
    result = BnfConnector(transport).search(
        SearchQuery(level=2, title="Le petit prince", author="Saint-Exupéry"), limit=5
    )
    assert result.status == "found" and result.total >= 100


def test_bnf_cover_present_and_absent(transport):
    connector = BnfConnector(transport)
    present = connector.cover(CoverRequest(external_id="ark:/12148/cb41023439w", announced=True))
    assert present.status == "found" and present.data[:3] == b"\xff\xd8\xff"
    absent = connector.cover(CoverRequest(external_id="ark:/12148/cb43404620t"))
    assert absent.status == "none_found"


def test_sudoc_isbn_exact_and_ppn(transport):
    connector = SudocConnector(transport)
    result = connector.search(SearchQuery(level=1, isbn13="9782070612758"))
    assert result.status == "found" and result.candidates[0].external_id == "117074047"
    assert connector.fetch("117074047").status == "found"


def test_sudoc_unknown_isbn(transport):
    assert SudocConnector(transport).search(SearchQuery(level=1, isbn13="9782999999992")).status == "not_found"
