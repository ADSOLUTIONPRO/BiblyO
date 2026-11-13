# SPDX-License-Identifier: EUPL-1.2
"""Client HTTP des connecteurs : liste fermée d'hôtes, HTTPS seul, redirections refusées sauf permission, réponses
bornées.

Aucune adresse n'est lue dans une réponse ni saisie par un utilisateur : chaque connecteur construit ses adresses à
partir d'identifiants contrôlés. Un nom qui se résout en adresse privée, locale ou de lien local est refusé, et les
variables de mandataire de l'environnement sont ignorées. Un connecteur peut permettre quelques redirections vers des
hôtes qu'il nomme (les images d'Open Library sont servies par `archive.org`) : chaque étape est contrôlée comme la
première.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "biblyO/0.1 (+https://biblyo.invalid ; enrichissement bibliographique)"


def user_agent(contact: str = "") -> str:
    """Identification des requêtes ; avec l'adresse de contact de l'instance quand elle est renseignée."""
    if not contact:
        return USER_AGENT
    return f"biblyO/0.1 (+https://biblyo.invalid ; enrichissement bibliographique ; contact : {contact})"


REDIRECT_STATUSES = (301, 302, 303, 307, 308)
CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 10.0
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
CHUNK = 64 * 1024

Failure = Literal["refused_host", "private_address", "timeout", "unavailable", "redirect", "too_large", "http_error"]
Resolver = Callable[[str], Iterable[str]]


class TransportError(Exception):
    """Un appel qui n'a pas abouti, avec sa nature. Le message ne contient jamais le corps d'une réponse."""

    def __init__(self, kind: Failure, detail: str = "", status: int | None = None) -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes
    elapsed_ms: int
    url: str
    retry_after: float | None = None


def system_resolver(host: str) -> list[str]:
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except OSError as exc:
        raise TransportError("unavailable", f"résolution impossible : {host}") from exc


def is_public_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_global and not parsed.is_multicast


class HttpTransport:
    """Un client par application. `hosts` est la réunion des hôtes déclarés par les connecteurs."""

    def __init__(
        self,
        hosts: Iterable[str],
        *,
        resolver: Resolver = system_resolver,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hosts = frozenset(h.casefold() for h in hosts)
        self._resolver = resolver
        self._clock = clock
        self._client = client or httpx.Client(
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml, image/jpeg, */*;q=0.1"},
            timeout=httpx.Timeout(DEFAULT_READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        )

    def set_contact(self, contact: str) -> None:
        """Place l'adresse de contact dans l'en-tête User-Agent des requêtes suivantes."""
        self._client.headers["User-Agent"] = user_agent(contact)

    def check_url(self, url: str, *, allowed: Callable[[str], bool] | None = None) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        if parts.scheme != "https" or not host or parts.port not in (None, 443) or parts.username or parts.password:
            raise TransportError("refused_host", "adresse hors de la liste permise")
        if host not in self.hosts and not (allowed is not None and allowed(host)):
            raise TransportError("refused_host", f"hôte non déclaré : {host}")
        addresses = list(self._resolver(host))
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise TransportError("private_address", f"adresse non publique pour {host}")
        return host

    def get(
        self,
        url: str,
        *,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_bytes: int = MAX_XML_BYTES,
        follow: Callable[[str], bool] | None = None,
        max_redirects: int = 2,
    ) -> Response:
        """GET borné : la lecture s'arrête dès que `max_bytes` est dépassé, sans attendre la fin de la réponse.

        Sans `follow`, toute redirection est refusée. Avec, une redirection est suivie si l'hôte visé est déclaré ou
        accepté par `follow`, dans la limite de `max_redirects`.
        """
        start = self._clock()
        for _hop in range(max_redirects + 1 if follow else 1):
            self.check_url(url, allowed=follow)
            target = self._read(url, read_timeout=read_timeout, max_bytes=max_bytes, start=start, follow=follow)
            if isinstance(target, Response):
                return target
            url = target
        raise TransportError("redirect", "trop de redirections")

    def _read(
        self, url: str, *, read_timeout: float, max_bytes: int, start: float, follow: Callable[[str], bool] | None
    ) -> Response | str:
        """Une étape : la réponse, ou l'adresse de la redirection permise."""
        timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT)
        try:
            with self._client.stream("GET", url, timeout=timeout) as response:
                if follow is not None and response.status_code in REDIRECT_STATUSES:
                    location = str(response.headers.get("Location", ""))
                    if not location:
                        raise TransportError("redirect", "redirection sans adresse", response.status_code)
                    return str(urljoin(url, location))
                if 300 <= response.status_code < 400:
                    raise TransportError("redirect", "redirection refusée", response.status_code)
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise TransportError("too_large", f"réponse annoncée à {declared} octets", response.status_code)
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise TransportError(
                            "too_large", f"réponse au-delà de {max_bytes} octets", response.status_code
                        )
                    chunks.append(chunk)
                retry = response.headers.get("Retry-After")
                return Response(
                    status=response.status_code,
                    content_type=response.headers.get("Content-Type", ""),
                    body=b"".join(chunks),
                    elapsed_ms=int((self._clock() - start) * 1000),
                    url=url,
                    retry_after=float(retry) if retry and retry.replace(".", "", 1).isdigit() else None,
                )
        except httpx.TimeoutException as exc:
            raise TransportError("timeout", "délai dépassé") from exc
        except httpx.HTTPError as exc:
            raise TransportError("unavailable", type(exc).__name__) from exc

    def close(self) -> None:
        self._client.close()


class RecordedTransport:
    """Réponses enregistrées servies à la place des sources, pour les essais de bout en bout. Jamais en production.

    Le dossier porte un `index.json` : une liste de règles `{"contains": "...", "file": "...", "status": 200}`,
    appliquées dans l'ordre à l'adresse demandée ; sans règle, une réponse « aucun résultat » de la source visée.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.rules: list[dict[str, Any]] = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        self.calls: list[str] = []

    def check_url(self, url: str, *, allowed: Callable[[str], bool] | None = None) -> str:
        return urlsplit(url).hostname or ""

    def get(
        self,
        url: str,
        *,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_bytes: int = MAX_XML_BYTES,
        follow: Callable[[str], bool] | None = None,
        max_redirects: int = 2,
    ) -> Response:
        self.calls.append(url)
        for rule in self.rules:
            if str(rule.get("contains", "")) in url:
                status = int(rule.get("status", 200))
                body = (self.directory / str(rule["file"])).read_bytes() if rule.get("file") else b""
                kind = str(rule.get("content_type", "text/xml;charset=UTF-8"))
                return Response(status, kind, body, 1, url)
        default = "sudoc-vide.xml" if "sudoc" in url else "bnf-vide.xml"
        return Response(200, "text/xml;charset=UTF-8", (self.directory / default).read_bytes(), 1, url)

    def close(self) -> None:
        return None
