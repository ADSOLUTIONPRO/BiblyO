# SPDX-License-Identifier: EUPL-1.2
"""BnF : service SRU du catalogue général et service Couvertures.

Adresses construites ici seulement, à partir d'identifiants contrôlés (ARK, ISBN, EAN). L'ISBN est cherché sous
ses deux formes : la BnF l'indexe tel qu'il a été saisi. Une erreur de requête revient en HTTP 200 avec un
diagnostic. Le service Couvertures répond HTTP 500 quand il n'a pas d'image : ce code n'est jamais classé d'office.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from xml.etree.ElementTree import Element  # nosec B405 : analyse par xmlsafe

from biblyo.modules.enrichment.connectors import sru, unimarc, xmlsafe
from biblyo.modules.enrichment.connectors.base import (
    Candidate,
    Capability,
    CoverRequest,
    CoverResult,
    Licence,
    SearchQuery,
    SourceResult,
    SourceStatus,
)
from biblyo.modules.enrichment.connectors.transport import MAX_IMAGE_BYTES, HttpTransport, Response, TransportError

CODE = "bnf"
HOSTS = frozenset({"catalogue.bnf.fr", "openapi.bnf.fr"})
SRU_URL = "https://catalogue.bnf.fr/api/SRU"
COVER_URL = "https://openapi.bnf.fr/couverture/image/image/recupererImage"
PUBLIC_URL = "https://catalogue.bnf.fr/{ark}"
ARK = re.compile(r"^ark:/12148/cb\d{8}[0-9a-z]$")
EAN = re.compile(r"^\d{13}$")
_NO_IMAGE = b"id to load is required for loading"
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")
LICENCE = Licence(
    holder="Bibliothèque nationale de France",
    name="Licence Ouverte de l'État 2.0",
    url="https://www.bnf.fr/fr/conditions-de-reutilisations-des-donnees-de-la-bnf",
    covers_note=(
        "Image de couverture fournie par le service Couvertures du catalogue général de la BnF, réutilisable "
        "selon les conditions de réutilisation des données de la BnF, avec mention de la source et de la date "
        "de récupération."
    ),
)


_PARAM = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,30}$")


@dataclass(frozen=True)
class CoverSettings:
    """Adresse et paramètres du service Couvertures, en version bêta : réglables sans toucher au code.

    Un réglage invalide (autre hôte, autre schéma, nom de paramètre douteux) est ignoré au profit de la valeur connue.
    """

    url: str = COVER_URL
    ark_param: str = "idArk"
    ean_param: str = "EAN"
    isbn_param: str = "ISBN"
    side_param: str = "couverture"
    size_param: str = "taille"
    original_size: str = "originale"

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> CoverSettings:
        covers = config.get("covers")
        if not isinstance(covers, Mapping):
            return cls()
        values: dict[str, str] = {}
        url = covers.get("url")
        if isinstance(url, str):
            parts = urlsplit(url)
            if parts.scheme == "https" and parts.hostname in HOSTS and not parts.query and parts.port is None:
                values["url"] = url
        for key in ("ark_param", "ean_param", "isbn_param", "side_param", "size_param", "original_size"):
            value = covers.get(key)
            if isinstance(value, str) and _PARAM.match(value):
                values[key] = value
        return cls(**values)


def status_for(error: TransportError) -> tuple[SourceStatus, bool]:
    """État d'une source d'après la nature de l'échec, et s'il vaut un réessai."""
    if error.kind == "timeout":
        return "timeout", True
    if error.kind == "too_large":
        return "invalid_response", False
    return "unavailable", error.kind == "unavailable"


def status_for_http(status: int) -> tuple[SourceStatus, bool]:
    if status == 429:
        return "throttled", True
    if status in (502, 503, 504):
        return "unavailable", True
    if status >= 500:
        return "unavailable", False
    return "refused", False


class BnfConnector:
    code = CODE
    label = "BnF, catalogue général"
    hosts = HOSTS
    capabilities = frozenset({Capability.ISBN, Capability.SEARCH, Capability.SERIES, Capability.COVER})
    licence = LICENCE

    def __init__(
        self, transport: HttpTransport, *, timeout_s: float = 10.0, config: Mapping[str, Any] | None = None
    ) -> None:
        self._transport = transport
        self._timeout = timeout_s
        self.covers = CoverSettings.from_config(config or {})

    # --- Adresses ---------------------------------------------------------------------------------------------------
    @staticmethod
    def public_url(external_id: str) -> str:
        return PUBLIC_URL.format(ark=external_id)

    @staticmethod
    def sru_url(cql: str, *, limit: int) -> str:
        params = {
            "version": "1.2",
            "operation": "searchRetrieve",
            "query": cql,
            "recordSchema": "unimarcXchange",
            "maximumRecords": str(max(1, min(limit, 50))),
            "startRecord": "1",
        }
        return SRU_URL + "?" + urlencode(params, quote_via=quote)

    def cover_url(self, request: CoverRequest) -> str | None:
        """Adresse construite ici seulement, à partir d'un ARK, d'un EAN ou d'un ISBN contrôlés."""
        names = self.covers
        params: dict[str, str] = {}
        if request.external_id:
            if not ARK.match(request.external_id):
                return None
            params[names.ark_param] = request.external_id
        elif request.ean and EAN.match(request.ean):
            params[names.ean_param] = request.ean
        elif request.isbn and EAN.match(request.isbn):
            params[names.isbn_param] = request.isbn
        else:
            return None
        params[names.side_param] = "4" if request.side == 4 else "1"
        if request.original:
            params[names.size_param] = names.original_size
        return names.url + "?" + urlencode(params, quote_via=quote)

    # --- Notices ----------------------------------------------------------------------------------------------------
    def from_record(self, record: unimarc.Record) -> Candidate | None:
        """Le candidat d'une notice nettoyée, reçue ou relue depuis le cache."""
        external_id = unimarc.ark_of(record)
        if not external_id:
            return None
        return unimarc.candidate(record, source=CODE, url=self.public_url(external_id), external_id=external_id)

    def from_records(self, records: tuple[unimarc.Record, ...], *, total: int, cached_at: str) -> SourceResult:
        candidates = tuple(c for c in (self.from_record(r) for r in records) if c is not None)
        if not candidates:
            return SourceResult("not_found", total=total, from_cache_at=cached_at)
        return SourceResult("found", candidates, total=total, from_cache_at=cached_at)

    def _candidates(self, root: Element) -> tuple[Candidate, ...]:
        found = (self.from_record(unimarc.read(element)) for element in unimarc.iter_records(root))
        return tuple(c for c in found if c is not None)

    def _retrieve(self, cql: str, *, limit: int) -> SourceResult:
        try:
            response = self._transport.get(self.sru_url(cql, limit=limit), read_timeout=self._timeout)
        except TransportError as error:
            status, retryable = status_for(error)
            return SourceResult(status, detail=error.detail, retryable=retryable)
        return self.read_response(response)

    def read_response(self, response: Response) -> SourceResult:
        """Lit une réponse SRU (reçue ou relue depuis le cache)."""
        if response.status != 200:
            status, retryable = status_for_http(response.status)
            return SourceResult(
                status, detail=f"HTTP {response.status}", retryable=retryable, retry_after=response.retry_after
            )
        try:
            root = xmlsafe.parse(response.body)
        except xmlsafe.InvalidXml as exc:
            return SourceResult("invalid_response", detail=str(exc))
        if not sru.is_payload(root):
            return SourceResult("invalid_response", detail="réponse qui n'est pas du SRU")
        envelope = sru.read_envelope(root)
        if envelope.refused:
            diagnostic = envelope.diagnostics[0]
            return SourceResult("refused", detail=f"diagnostic {diagnostic.code} : {diagnostic.message}")
        candidates = self._candidates(root)
        if not candidates:
            return SourceResult("not_found", total=envelope.total, elapsed_ms=response.elapsed_ms)
        return SourceResult("found", candidates, total=envelope.total, elapsed_ms=response.elapsed_ms)

    def search(self, query: SearchQuery, *, limit: int = 20) -> SourceResult:
        try:
            cql = sru.bnf_query(query)
        except sru.QueryError as exc:
            return SourceResult("refused", detail=str(exc))
        return self._retrieve(cql, limit=limit)

    def fetch(self, external_id: str) -> SourceResult:
        if not ARK.match(external_id):
            return SourceResult("refused", detail="identifiant ARK mal formé")
        return self._retrieve(f'bib.persistentid any "{external_id}"', limit=1)

    # --- Couvertures ------------------------------------------------------------------------------------------------
    def cover(self, request: CoverRequest) -> CoverResult:
        url = self.cover_url(request)
        if url is None:
            return CoverResult("unavailable", detail="identifiant de couverture mal formé")
        try:
            response = self._transport.get(url, read_timeout=self._timeout, max_bytes=MAX_IMAGE_BYTES)
        except TransportError as error:
            status, retryable = status_for(error)
            return CoverResult("unavailable", url=url, detail=error.detail, retryable=retryable and status == "timeout")
        return self.read_cover(response, request)

    @staticmethod
    def read_cover(response: Response, request: CoverRequest) -> CoverResult:
        if response.status == 200:
            if response.body.startswith(_IMAGE_MAGIC) and response.content_type.startswith("image/"):
                return CoverResult("found", response.body, response.url)
            return CoverResult("invalid_image", url=response.url, detail="réponse qui n'est pas une image")
        if response.status == 500 and _NO_IMAGE in response.body:
            if request.announced:
                return CoverResult("announced_not_served", url=response.url, detail="image annoncée, non servie")
            return CoverResult("none_found", url=response.url)
        retryable = response.status in (429, 502, 503, 504)
        return CoverResult("unavailable", url=response.url, detail=f"HTTP {response.status}", retryable=retryable)
