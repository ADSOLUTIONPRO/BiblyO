# SPDX-License-Identifier: EUPL-1.2
"""Image par adresse saisie : aucune adresse locale, privée ou réservée n'est jamais atteinte (mission 3, essais 14
et 15). Le réseau est simulé ; le résolveur aussi."""

from __future__ import annotations

import httpx
import pytest

from biblyo.core.remote import RemoteImageError, check, fetch_image, is_public

PUBLIC = "93.184.216.34"
NAMES = {
    "images.example.org": [PUBLIC],
    "autre.example.org": ["93.184.216.35"],
    "interne.example.org": ["10.0.0.5"],
    "mixte.example.org": [PUBLIC, "192.168.0.10"],
    "boucle.example.org": ["127.0.0.1"],
    "metadonnees.example.org": ["169.254.169.254"],
    "partage.example.org": ["100.64.0.1"],
    "v6.example.org": ["::ffff:127.0.0.1"],
}


def resolver(host: str) -> list[str]:
    return NAMES.get(host, [])


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000


@pytest.mark.parametrize(
    "url",
    [
        "http://images.example.org/a.jpg",
        "ftp://images.example.org/a.jpg",
        "https://images.example.org:8443/a.jpg",
        "https://moi:secret@images.example.org/a.jpg",
        "https://127.0.0.1/a.jpg",
        "https://[::1]/a.jpg",
        "https://169.254.169.254/latest/meta-data",
        "https://8.8.8.8/a.jpg",
        "https://localhost/a.jpg",
        "https://app/a.jpg",
        "https://interne.example.org/a.jpg",
        "https://mixte.example.org/a.jpg",
        "https://boucle.example.org/a.jpg",
        "https://metadonnees.example.org/a.jpg",
        "https://partage.example.org/a.jpg",
        "https://v6.example.org/a.jpg",
        "https://inconnu.example.org/a.jpg",
        "pas une adresse",
        "https://images.example.org/" + "a" * 3000,
    ],
)
def test_dangerous_or_invalid_addresses_are_refused(url):
    with pytest.raises(RemoteImageError):
        check(url, resolver)


def test_public_addresses():
    assert is_public(PUBLIC)
    unspecified = ".".join(["0"] * 4)
    private = ("10.1.2.3", "172.17.0.2", "192.168.0.1", "127.0.0.1", "::1", "fc00::1", "fe80::1", "100.64.0.1")
    assert not any(is_public(a) for a in (*private, "224.0.0.1", unspecified, "::ffff:10.0.0.1"))


def test_the_connection_goes_to_the_checked_address_with_the_name_for_tls():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(host=request.url.host, header=request.headers["Host"], sni=request.extensions.get("sni_hostname"))
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})

    image = fetch_image("https://images.example.org/couv.jpg?taille=l", resolver=resolver, client=client(handler))
    assert seen == {"host": PUBLIC, "header": "images.example.org", "sni": "images.example.org"}
    assert image.body == JPEG and image.url == "https://images.example.org/couv.jpg?taille=l"


def test_a_redirect_is_checked_like_the_first_address():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Host"] == "images.example.org":
            return httpx.Response(302, headers={"Location": "https://interne.example.org/secret"})
        return httpx.Response(200, content=JPEG)

    with pytest.raises(RemoteImageError, match="réseau privé"):
        fetch_image("https://images.example.org/a.jpg", resolver=resolver, client=client(handler))


def test_a_redirect_to_plain_http_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "http://autre.example.org/a.jpg"})

    with pytest.raises(RemoteImageError, match="https"):
        fetch_image("https://images.example.org/a.jpg", resolver=resolver, client=client(handler))


def test_an_allowed_redirect_is_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Host"] == "images.example.org":
            return httpx.Response(302, headers={"Location": "https://autre.example.org/vraie.jpg"})
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})

    image = fetch_image("https://images.example.org/a.jpg", resolver=resolver, client=client(handler))
    assert image.url == "https://autre.example.org/vraie.jpg"


def test_more_than_three_redirects_are_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/encore"})

    with pytest.raises(RemoteImageError, match="Trop de redirections"):
        fetch_image("https://images.example.org/a.jpg", resolver=resolver, client=client(handler))


def test_an_announced_or_actual_oversize_is_refused():
    def announced(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"Content-Length": str(9 * 1024 * 1024)})

    with pytest.raises(RemoteImageError, match="8 Mio"):
        fetch_image("https://images.example.org/a.jpg", resolver=resolver, client=client(announced))

    def streamed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    with pytest.raises(RemoteImageError, match="8 Mio"):
        fetch_image("https://images.example.org/a.jpg", resolver=resolver, client=client(streamed), max_bytes=4000)


def test_an_error_status_and_a_timeout_have_their_message():
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(RemoteImageError, match="404"):
        fetch_image("https://images.example.org/a.jpg", resolver=resolver, client=client(missing))

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lent", request=request)

    with pytest.raises(RemoteImageError, match="trop lent"):
        fetch_image("https://images.example.org/a.jpg", resolver=resolver, client=client(slow))


def test_the_total_duration_is_bounded():
    ticks = iter([0.0, 30.0, 31.0, 32.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG)

    with pytest.raises(RemoteImageError, match="trop lent"):
        fetch_image(
            "https://images.example.org/a.jpg", resolver=resolver, client=client(handler), clock=lambda: next(ticks)
        )


def test_the_user_agent_is_plain_ascii():
    from biblyo.core.remote import USER_AGENT

    USER_AGENT.encode("ascii")  # un en-tête HTTP n'accepte pas d'accent
