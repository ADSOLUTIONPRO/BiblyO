# SPDX-License-Identifier: EUPL-1.2
"""Client HTTP des connecteurs : hôtes permis, adresses privées, redirections, taille, délais, mandataires."""

from __future__ import annotations

import httpx
import pytest

from biblyo.modules.enrichment.connectors.transport import (
    USER_AGENT,
    HttpTransport,
    TransportError,
    is_public_address,
)

HOSTS = {"catalogue.bnf.fr", "openapi.bnf.fr"}


def public(host: str) -> list[str]:
    return ["194.199.8.183"]


def transport(handler, resolver=public) -> HttpTransport:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)
    return HttpTransport(HOSTS, resolver=resolver, client=client)


def test_a_plain_response_is_returned_with_its_timing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<x/>", headers={"Content-Type": "text/xml"})

    response = transport(handler).get("https://catalogue.bnf.fr/api/SRU?x=1")
    assert (response.status, response.content_type, response.body) == (200, "text/xml", b"<x/>")
    assert response.elapsed_ms >= 0 and response.retry_after is None


@pytest.mark.parametrize(
    "url",
    [
        "http://catalogue.bnf.fr/api/SRU",
        "https://example.org/",
        "https://catalogue.bnf.fr:8443/api/SRU",
        "https://user:pw@catalogue.bnf.fr/api/SRU",
        "https://catalogue.bnf.fr.evil.example/",
        "ftp://catalogue.bnf.fr/",
    ],
)
def test_urls_outside_the_allowed_list_are_refused(url):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    with pytest.raises(TransportError) as error:
        transport(handler).get(url)
    assert error.value.kind == "refused_host" and calls == []


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.0.10", "169.254.169.254", "::1", "fd00::1", "0.0.0.0"],  # noqa: S104
)
def test_names_resolving_to_private_addresses_are_refused(address):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    with pytest.raises(TransportError) as error:
        transport(handler, resolver=lambda host: [address]).get("https://catalogue.bnf.fr/api/SRU")
    assert error.value.kind == "private_address" and calls == []
    assert not is_public_address(address)


def test_a_mixed_resolution_is_refused():
    with pytest.raises(TransportError):
        transport(lambda r: httpx.Response(200), resolver=lambda h: ["194.199.8.183", "10.0.0.1"]).get(
            "https://catalogue.bnf.fr/"
        )


def test_redirections_are_never_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://catalogue.bnf.fr/elsewhere"})

    with pytest.raises(TransportError) as error:
        transport(handler).get("https://catalogue.bnf.fr/api/SRU")
    assert error.value.kind == "redirect"


def test_an_announced_oversize_response_is_refused_before_reading():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": str(50 * 1024 * 1024)}, content=b"")

    with pytest.raises(TransportError) as error:
        transport(handler).get("https://catalogue.bnf.fr/api/SRU")
    assert error.value.kind == "too_large"


def test_a_streamed_oversize_response_is_cut():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 3000)

    with pytest.raises(TransportError) as error:
        transport(handler).get("https://catalogue.bnf.fr/api/SRU", max_bytes=2048)
    assert error.value.kind == "too_large"


def test_a_timeout_is_reported_as_such():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lent", request=request)

    with pytest.raises(TransportError) as error:
        transport(handler).get("https://catalogue.bnf.fr/api/SRU")
    assert error.value.kind == "timeout"


def test_a_connection_failure_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refusé", request=request)

    with pytest.raises(TransportError) as error:
        transport(handler).get("https://catalogue.bnf.fr/api/SRU")
    assert error.value.kind == "unavailable"


def test_retry_after_is_read_and_http_errors_are_returned_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"}, content=b"trop")

    response = transport(handler).get("https://catalogue.bnf.fr/api/SRU")
    assert (response.status, response.retry_after) == (429, 30.0)


def test_the_default_client_ignores_proxies_and_declares_itself():
    client = HttpTransport(HOSTS, resolver=public)._client
    assert client.trust_env is False
    assert client.headers["User-Agent"] == USER_AGENT
    assert client.follow_redirects is False
    client.close()


def test_the_error_message_never_carries_a_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "9999999"}, content=b"SECRET")

    with pytest.raises(TransportError) as error:
        transport(handler).get("https://catalogue.bnf.fr/api/SRU")
    assert "SECRET" not in str(error.value)


def _archive(host: str) -> bool:
    return host == "archive.org" or host.endswith(".us.archive.org")


def test_a_connector_may_allow_redirections_to_named_hosts():
    """Open Library : couvertures servies après deux redirections vers archive.org (relevé du 12 novembre 2026)."""
    hops = {
        "catalogue.bnf.fr": "https://archive.org/download/x.zip/1-M.jpg",
        "archive.org": "https://ia800703.us.archive.org/view_archive.php?file=1-M.jpg",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in hops:
            return httpx.Response(302, headers={"Location": hops[request.url.host]})
        return httpx.Response(200, content=b"\xff\xd8", headers={"Content-Type": "image/jpeg"})

    response = transport(handler).get("https://catalogue.bnf.fr/b/id/1-M.jpg", follow=_archive)
    assert response.status == 200 and response.url.startswith("https://ia800703.us.archive.org/")


@pytest.mark.parametrize(
    "location", ["https://ailleurs.example.org/x.jpg", "http://archive.org/x.jpg", "https://archive.org:8080/x"]
)
def test_an_allowed_redirection_is_still_checked(location):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": location})

    with pytest.raises(TransportError) as error:
        transport(handler).get("https://catalogue.bnf.fr/b/id/1-M.jpg", follow=_archive)
    assert error.value.kind == "refused_host"


def test_an_allowed_redirection_to_a_private_address_is_refused():
    def resolver(host: str) -> list[str]:
        return ["10.0.0.8"] if host == "archive.org" else ["194.199.8.183"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://archive.org/x.jpg"})

    with pytest.raises(TransportError) as error:
        transport(handler, resolver).get("https://catalogue.bnf.fr/b/id/1-M.jpg", follow=_archive)
    assert error.value.kind == "private_address"


def test_allowed_redirections_are_bounded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://archive.org/encore"})

    with pytest.raises(TransportError) as error:
        transport(handler).get("https://catalogue.bnf.fr/b/id/1-M.jpg", follow=_archive, max_redirects=2)
    assert error.value.kind == "redirect"
