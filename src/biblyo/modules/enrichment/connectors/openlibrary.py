# SPDX-License-Identifier: EUPL-1.2
"""Open Library (Internet Archive) : couvertures seulement, jamais de notice (conception 17 § 4).

Règles relevées le 12 novembre 2026 (`openlibrary.org/dev/docs/api/covers`, `/developers/api`) : l'API sert à
afficher des couvertures, pas au téléchargement en masse ; 100 requêtes par 5 minutes pour une image demandée par
ISBN, aucune limite par numéro d'image ; une requête par seconde sans identification ; lien de courtoisie apprécié.
Open Library ne revendique et n'accorde aucun droit sur les images : elles restent celles des éditeurs. La source est
livrée désactivée ; chaque image retenue exige la confirmation du droit d'utilisation par le bibliothécaire.

Trois requêtes, toutes construites à partir d'identifiants contrôlés : la notice d'un ISBN (`/api/books`), une
recherche par titre et auteur (`/search.json`), une image par son numéro (`covers.openlibrary.org/b/id/…`). L'image
est servie après deux redirections, vers `archive.org` puis `ia*.us.archive.org` : ce sont les seules permises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from biblyo.modules.enrichment.connectors.base import (
    Capability,
    CoverRequest,
    CoverResult,
    Licence,
    SearchQuery,
    SourceResult,
)
from biblyo.modules.enrichment.connectors.transport import MAX_IMAGE_BYTES, HttpTransport, TransportError

BOOKS_URL = "https://openlibrary.org/api/books"
SEARCH_URL = "https://openlibrary.org/search.json"
COVER_URL = "https://covers.openlibrary.org/b/id/{cover_id}-{size}.jpg?default=false"
PUBLIC_URL = "https://openlibrary.org"
COVER_ID = re.compile(r"^[1-9][0-9]{0,11}$")
ISBN13 = re.compile(r"^97[89][0-9]{10}$")
MAX_JSON_BYTES = 1024 * 1024
SEARCH_LIMIT = 6
MAX_ISBNS = 4  # une seule requête /api/books pour les ISBN des éditions proposées

LICENCE = Licence(
    holder="Open Library (Internet Archive)",
    name="Aucune licence : les couvertures restent la propriété de leurs éditeurs",
    url="https://openlibrary.org/dev/docs/api/covers",
    covers_note=(
        "Image trouvée par Open Library (Internet Archive), qui n'accorde aucun droit sur les couvertures : elles "
        "restent celles des éditeurs. Utilisation confirmée par le bibliothécaire."
    ),
)


def image_host(host: str) -> bool:
    """Les seuls hôtes vers lesquels une image d'Open Library peut rediriger."""
    return host == "archive.org" or (
        host.endswith(".us.archive.org") and host.removesuffix(".us.archive.org").isalnum()
    )


@dataclass(frozen=True)
class FoundCover:
    """Une image signalée par Open Library, avec ce que l'on sait de l'édition qu'elle représente."""

    cover_id: str
    title: str
    publisher: str
    year: str
    isbn13: str  # renseigné si l'image vient de la notice de cet ISBN
    url: str  # page publique de l'édition ou de l'œuvre
    level: str  # « isbn » : notice de cet ISBN ; « work » : image de l'œuvre, édition non déterminée


class OpenLibraryConnector:
    code = "openlibrary"
    label = "Open Library"
    hosts = frozenset({"openlibrary.org", "covers.openlibrary.org"})
    capabilities = frozenset({Capability.COVER})
    licence = LICENCE

    def __init__(self, transport: HttpTransport, *, timeout_s: float = 15.0) -> None:
        self._transport = transport
        self._timeout = timeout_s

    # --- Ce connecteur ne fournit aucune notice ----------------------------------------------------------------------
    def search(self, query: SearchQuery, *, limit: int = 20) -> SourceResult:
        return SourceResult("refused", detail="Open Library ne sert qu'aux couvertures")

    def fetch(self, external_id: str) -> SourceResult:
        return SourceResult("refused", detail="Open Library ne sert qu'aux couvertures")

    def from_records(self, records: tuple[Any, ...], *, total: int, cached_at: str) -> SourceResult:
        return SourceResult("not_found", total=0, from_cache_at=cached_at)

    # --- Couvertures -------------------------------------------------------------------------------------------------
    def cover(self, request: CoverRequest) -> CoverResult:
        if not COVER_ID.match(request.external_id):
            return CoverResult("none_found", detail="numéro d'image Open Library absent ou mal formé")
        url = COVER_URL.format(cover_id=request.external_id, size="L" if request.original else "M")
        try:
            response = self._transport.get(
                url, read_timeout=self._timeout, max_bytes=MAX_IMAGE_BYTES, follow=image_host, max_redirects=2
            )
        except TransportError as error:
            return CoverResult("unavailable", url=url, detail=error.detail, retryable=error.kind == "timeout")
        if response.status == 200 and response.content_type.lower().startswith("image/"):
            return CoverResult("found", response.body, url)
        if response.status == 404:
            return CoverResult("none_found", url=url)
        if response.status == 200:
            return CoverResult("invalid_image", url=url, detail="réponse qui n'est pas une image")
        return CoverResult("unavailable", url=url, detail=f"HTTP {response.status}")

    def find_covers(self, *, isbns: tuple[str, ...], title: str, author: str) -> tuple[list[FoundCover], str]:
        """Les images d'Open Library pour ces ISBN, puis pour ce titre et cet auteur. Rend aussi l'état de l'appel :
        « found », « not_found » ou la nature de la panne."""
        found: list[FoundCover] = []
        status = "not_found"
        keys = [isbn for isbn in isbns if ISBN13.match(isbn)][:MAX_ISBNS]
        if keys:
            data, status = self._json(
                BOOKS_URL
                + "?"
                + urlencode({"bibkeys": ",".join(f"ISBN:{k}" for k in keys), "format": "json", "jscmd": "data"})
            )
            if isinstance(data, Mapping):
                for key in keys:
                    entry = data.get(f"ISBN:{key}")
                    if isinstance(entry, Mapping):
                        cover = _cover_id(entry.get("cover"))
                        if cover:
                            found.append(_from_book(cover, entry, key))
        words = " ".join(title.split())[:200]
        if words:
            params = {
                "title": words,
                "fields": "key,title,author_name,first_publish_year,cover_i",
                "limit": SEARCH_LIMIT,
            }
            if author.strip():
                params["author"] = " ".join(author.split())[:120]
            data, work_status = self._json(SEARCH_URL + "?" + urlencode(params))
            status = work_status if status != "found" else status
            docs = data.get("docs") if isinstance(data, Mapping) else None
            for doc in docs if isinstance(docs, list) else []:
                if not isinstance(doc, Mapping):
                    continue
                cover = str(doc.get("cover_i") or "")
                if COVER_ID.match(cover):
                    found.append(
                        FoundCover(
                            cover_id=cover,
                            title=str(doc.get("title") or "")[:300],
                            publisher="",
                            year=str(doc.get("first_publish_year") or "")[:10],
                            isbn13="",
                            url=PUBLIC_URL + str(doc.get("key") or "")
                            if str(doc.get("key", "")).startswith("/")
                            else PUBLIC_URL,
                            level="work",
                        )
                    )
        if found:
            status = "found"
        return found, status

    def _json(self, url: str) -> tuple[Any, str]:
        try:
            response = self._transport.get(url, read_timeout=self._timeout, max_bytes=MAX_JSON_BYTES)
        except TransportError as error:
            return None, "timeout" if error.kind == "timeout" else "unavailable"
        if response.status != 200:
            return None, "unavailable" if response.status >= 500 or response.status == 429 else "refused"
        try:
            return json.loads(response.body.decode("utf-8")), "not_found"
        except (UnicodeDecodeError, ValueError):
            return None, "invalid_response"

    @staticmethod
    def public_url(cover_id: str) -> str:
        return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if COVER_ID.match(cover_id) else PUBLIC_URL


def _cover_id(cover: Any) -> str:
    """Le numéro d'image d'une notice : `{"large": "https://covers.openlibrary.org/b/id/123-L.jpg", …}`."""
    if not isinstance(cover, Mapping):
        return ""
    for size in ("large", "medium", "small"):
        match = re.search(r"/b/id/([1-9][0-9]{0,11})-[SML]\.jpg$", str(cover.get(size) or ""))
        if match:
            return match.group(1)
    return ""


def _from_book(cover: str, entry: Mapping[str, Any], isbn: str) -> FoundCover:
    publishers = entry.get("publishers")
    publisher = ""
    if isinstance(publishers, list) and publishers and isinstance(publishers[0], Mapping):
        publisher = str(publishers[0].get("name") or "")[:200]
    url = str(entry.get("url") or "")
    return FoundCover(
        cover_id=cover,
        title=str(entry.get("title") or "")[:300],
        publisher=publisher,
        year=str(entry.get("publish_date") or "")[:20],
        isbn13=isbn,
        url=url if url.startswith("https://openlibrary.org/") else PUBLIC_URL,
        level="isbn",
    )
