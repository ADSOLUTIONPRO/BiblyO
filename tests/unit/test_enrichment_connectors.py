# SPDX-License-Identifier: EUPL-1.2
"""Connecteurs BnF et Sudoc sur un réseau simulé : réponses enregistrées, pannes, refus, couvertures."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.base import CoverRequest, SearchQuery
from biblyo.modules.enrichment.connectors.bnf import BnfConnector
from biblyo.modules.enrichment.connectors.sudoc import SudocConnector
from biblyo.modules.enrichment.connectors.transport import HttpTransport
from tests.sources import read_bytes

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
NO_IMAGE = (
    b'<!doctype html><html lang="fr"><head><title>\xc3\x89tat HTTP 500</title></head><body>'
    b"<p><b>message</b> Request processing failed; nested exception is java.lang.IllegalArgumentException:"
    b" id to load is required for loading</p></body></html>"
)
Handler = Callable[[httpx.Request], httpx.Response]


def make_transport(handler: Handler) -> HttpTransport:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)
    return HttpTransport(registry.HOSTS, resolver=lambda host: ["194.199.8.183"], client=client)


def xml(name: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=read_bytes(name), headers={"Content-Type": "text/xml;charset=UTF-8"})

    return handler


def bnf(handler: Handler) -> BnfConnector:
    return BnfConnector(make_transport(handler))


def sudoc(handler: Handler) -> SudocConnector:
    return SudocConnector(make_transport(handler))


# --- BnF : notices ----------------------------------------------------------------------------------------------------
def test_bnf_isbn_search_sends_both_forms_and_reads_the_record():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return xml("bnf/isbn-exact-petit-prince")(request)

    result = bnf(handler).search(SearchQuery(level=1, isbn13="9782070612758", isbn10="2070612759"))
    assert result.status == "found" and result.total == 1 and len(result.candidates) == 1
    assert result.candidates[0].url == "https://catalogue.bnf.fr/ark:/12148/cb41023439w"
    query = parse_qs(urlsplit(str(seen[0].url)).query)
    assert query["query"] == ['bib.isbn any "9782070612758 2070612759"']
    assert query["recordSchema"] == ["unimarcXchange"] and seen[0].url.host == "catalogue.bnf.fr"


def test_bnf_no_result_is_answered_not_a_failure():
    result = bnf(xml("bnf/isbn-inconnu")).search(SearchQuery(level=1, isbn13="9782999999992"))
    assert result.status == "not_found" and result.answered and result.candidates == ()


def test_bnf_diagnostics_are_refusals_never_no_result():
    result = bnf(xml("bnf/diagnostic-index-inconnu")).search(SearchQuery(level=1, isbn13="9782070612758"))
    assert result.status == "refused" and not result.answered and "16" in result.detail


def test_bnf_impossible_query_is_refused_without_a_call():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    result = bnf(handler).search(SearchQuery(level=2, title="Seul"))
    assert result.status == "refused" and calls == []


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (503, "unavailable", True),
        (502, "unavailable", True),
        (500, "unavailable", False),
        (429, "throttled", True),
        (404, "refused", False),
    ],
)
def test_bnf_http_errors(status, expected, retryable):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"<html/>", headers={"Retry-After": "12"} if status == 429 else {})

    result = bnf(handler).search(SearchQuery(level=1, isbn13="9782070612758"))
    assert (result.status, result.retryable) == (expected, retryable)
    if status == 429:
        assert result.retry_after == 12.0


def test_bnf_timeout_and_connection_failure():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lent", request=request)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refusé", request=request)

    assert bnf(slow).search(SearchQuery(level=1, isbn13="9782070612758")).status == "timeout"
    result = bnf(down).search(SearchQuery(level=1, isbn13="9782070612758"))
    assert result.status == "unavailable" and result.retryable


@pytest.mark.parametrize(
    "body",
    [
        b"<srw:searchRetrieveResponse>",
        b"<!DOCTYPE x [<!ENTITY e 'x'>]><x>&e;</x>",
        b"",
        b"<html><body>Erreur</body></html>",
    ],
)
def test_bnf_invalid_xml_is_invalid_response(body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Type": "text/xml"})

    result = bnf(handler).search(SearchQuery(level=1, isbn13="9782070612758"))
    assert result.status == "invalid_response" and not result.answered and not result.retryable


def test_bnf_oversize_response_is_invalid():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<r>" + b"<e/>" * 600_000 + b"</r>")

    assert bnf(handler).search(SearchQuery(level=1, isbn13="9782070612758")).status == "invalid_response"


def test_bnf_fetch_by_ark():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return xml("bnf/bd-asterix-unimarc")(request)

    result = bnf(handler).fetch("ark:/12148/cb43404620t")
    assert result.status == "found" and result.candidates[0].external_id == "ark:/12148/cb43404620t"
    assert 'bib.persistentid any "ark:/12148/cb43404620t"' in parse_qs(urlsplit(str(seen[0].url)).query)["query"]


@pytest.mark.parametrize("ark", ["ark:/12148/cb434", "cb43404620t", "ark:/12148/cb43404620t/../x", "javascript:x"])
def test_bnf_fetch_refuses_a_malformed_ark(ark):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    assert bnf(handler).fetch(ark).status == "refused" and calls == []


# --- BnF : couvertures ------------------------------------------------------------------------------------------------
def test_cover_found_by_ark_builds_the_address_itself():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})

    result = bnf(handler).cover(CoverRequest(external_id="ark:/12148/cb41023439w", announced=True))
    assert result.status == "found" and result.data == JPEG
    url = seen[0].url
    assert url.host == "openapi.bnf.fr" and url.path == "/couverture/image/image/recupererImage"
    assert parse_qs(url.query.decode()) == {"idArk": ["ark:/12148/cb41023439w"], "couverture": ["1"]}


def test_cover_original_size_and_ean_and_isbn():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})

    connector = bnf(handler)
    connector.cover(CoverRequest(external_id="ark:/12148/cb41023439w", original=True, side=4))
    connector.cover(CoverRequest(ean="9782070612758"))
    connector.cover(CoverRequest(isbn="9782070612758"))
    queries = [parse_qs(request.url.query.decode()) for request in seen]
    assert queries[0] == {"idArk": ["ark:/12148/cb41023439w"], "couverture": ["4"], "taille": ["originale"]}
    assert queries[1] == {"EAN": ["9782070612758"], "couverture": ["1"]}
    assert queries[2] == {"ISBN": ["9782070612758"], "couverture": ["1"]}


def test_cover_http_500_is_classified_by_what_the_record_said():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=NO_IMAGE, headers={"Content-Type": "text/html;charset=utf-8"})

    connector = bnf(handler)
    announced = connector.cover(CoverRequest(external_id="ark:/12148/cb41023439w", announced=True))
    assert announced.status == "announced_not_served" and not announced.retryable
    silent = connector.cover(CoverRequest(external_id="ark:/12148/cb43404620t"))
    assert silent.status == "none_found"
    by_ean = connector.cover(CoverRequest(ean="9782012101333"))
    assert by_ean.status == "none_found"


def test_cover_other_errors_are_unavailable():
    def other(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>autre panne</html>")

    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lent", request=request)

    request = CoverRequest(external_id="ark:/12148/cb41023439w", announced=True)
    assert bnf(other).cover(request).status == "unavailable"
    assert bnf(gateway).cover(request).retryable
    assert bnf(slow).cover(request).status == "unavailable" and bnf(slow).cover(request).retryable


def test_cover_that_is_not_an_image_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>pas une image</html>", headers={"Content-Type": "image/jpeg"})

    assert bnf(handler).cover(CoverRequest(external_id="ark:/12148/cb41023439w")).status == "invalid_image"

    def mislabeled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "text/html"})

    assert bnf(mislabeled).cover(CoverRequest(external_id="ark:/12148/cb41023439w")).status == "invalid_image"


def test_cover_oversize_is_unavailable_not_stored():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG + b"\x00" * (3 * 1024 * 1024), headers={"Content-Type": "image/jpeg"})

    result = bnf(handler).cover(CoverRequest(external_id="ark:/12148/cb41023439w"))
    assert result.status == "unavailable" and result.data == b""


def test_cover_with_a_malformed_identifier_makes_no_call():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})

    connector = bnf(handler)
    assert connector.cover(CoverRequest(external_id="ark:/evil")).status == "unavailable"
    assert connector.cover(CoverRequest(ean="97820706")).status == "unavailable"
    assert connector.cover(CoverRequest()).status == "unavailable"
    assert calls == []


# --- Sudoc ------------------------------------------------------------------------------------------------------------
def test_sudoc_isbn_search():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return xml("sudoc/sru-isbn-petit-prince")(request)

    result = sudoc(handler).search(SearchQuery(level=1, isbn13="9782070612758"))
    assert result.status == "found" and result.candidates[0].external_id == "117074047"
    assert result.candidates[0].url == "https://www.sudoc.fr/117074047"
    query = parse_qs(urlsplit(str(seen[0].url)).query)
    assert query["query"] == ["isb=9782070612758"] and query["recordSchema"] == ["unimarc"]
    assert seen[0].url.host == "www.sudoc.abes.fr"
    assert "$5" not in repr(result.candidates[0])


def test_sudoc_no_result_with_its_harmless_diagnostic():
    result = sudoc(xml("sudoc/sru-isbn-inconnu")).search(SearchQuery(level=1, isbn13="9782999999992"))
    assert result.status == "not_found" and result.answered


def test_sudoc_fetch_by_ppn_and_unknown_ppn():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/117074047.xml":
            return xml("sudoc/notice-par-ppn")(request)
        return httpx.Response(404, content=b"<html>Not Found</html>")

    connector = sudoc(handler)
    found = connector.fetch("117074047")
    assert found.status == "found" and found.candidates[0].external_id == "117074047"
    assert connector.fetch("000000000").status == "not_found"
    assert connector.fetch("abc").status == "refused"


def test_sudoc_has_no_covers():
    assert sudoc(lambda r: httpx.Response(200)).cover(CoverRequest(ean="9782070612758")).status == "unsupported"


def test_registry_builds_the_declared_connectors():
    transport = make_transport(lambda r: httpx.Response(200))
    assert registry.build("bnf", transport, timeout_s=3.0) is not None
    assert registry.build("sudoc", transport, timeout_s=3.0) is not None
    assert registry.build("openlibrary", transport, timeout_s=3.0) is not None
    assert registry.build("inconnue", transport, timeout_s=3.0) is None
    assert {
        "catalogue.bnf.fr",
        "openapi.bnf.fr",
        "www.sudoc.abes.fr",
        "www.sudoc.fr",
        "openlibrary.org",
        "covers.openlibrary.org",
    } == registry.HOSTS  # archive.org n'est atteint que par une redirection d'image d'Open Library
