# SPDX-License-Identifier: EUPL-1.2
"""Sudoc (Abes) : service SRU et notice par PPN. Aucune couverture.

Le Sudoc joint à ses notices les exemplaires de tout son réseau : le lecteur UNIMARC les écarte avant toute lecture,
mise en cache ou journalisation. Une requête malformée revient en HTTP 200 avec zéro résultat et le diagnostic 61,
le même que pour un vrai « aucun résultat » : les requêtes sont construites ici seulement, à partir de termes
nettoyés.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode
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
)
from biblyo.modules.enrichment.connectors.bnf import status_for, status_for_http
from biblyo.modules.enrichment.connectors.transport import HttpTransport, Response, TransportError

CODE = "sudoc"
HOSTS = frozenset({"www.sudoc.abes.fr", "www.sudoc.fr"})
SRU_URL = "https://www.sudoc.abes.fr/cbs/sru/"
RECORD_URL = "https://www.sudoc.fr/{ppn}.xml"
PUBLIC_URL = "https://www.sudoc.fr/{ppn}"
PPN = re.compile(r"^\d{8}[\dX]$")
LICENCE = Licence(
    holder="Abes (Sudoc)",
    name="Licence Ouverte de l'État",
    url="https://abes.fr/reseau-sudoc/reutiliser-les-donnees-sudoc/formats-de-donnees-disponibles/",
)


class SudocConnector:
    code = CODE
    label = "Sudoc (Abes)"
    hosts = HOSTS
    capabilities = frozenset({Capability.ISBN, Capability.SEARCH, Capability.SERIES})
    licence = LICENCE

    def __init__(self, transport: HttpTransport, *, timeout_s: float = 15.0) -> None:
        self._transport = transport
        self._timeout = timeout_s

    @staticmethod
    def public_url(external_id: str) -> str:
        return PUBLIC_URL.format(ppn=external_id)

    @staticmethod
    def sru_url(cql: str, *, limit: int) -> str:
        params = {
            "operation": "searchRetrieve",
            "version": "1.1",
            "recordSchema": "unimarc",
            "maximumRecords": str(max(1, min(limit, 50))),
            "startRecord": "1",
            "query": cql,
        }
        return SRU_URL + "?" + urlencode(params, quote_via=quote)

    def from_record(self, record: unimarc.Record) -> Candidate | None:
        """Le candidat d'une notice nettoyée, reçue ou relue depuis le cache."""
        external_id = unimarc.ppn_of(record)
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

    def read_response(self, response: Response) -> SourceResult:
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

    def _get(self, url: str) -> SourceResult:
        try:
            response = self._transport.get(url, read_timeout=self._timeout)
        except TransportError as error:
            status, retryable = status_for(error)
            return SourceResult(status, detail=error.detail, retryable=retryable)
        return self.read_response(response)

    def search(self, query: SearchQuery, *, limit: int = 20) -> SourceResult:
        try:
            cql = sru.sudoc_query(query)
        except sru.QueryError as exc:
            return SourceResult("refused", detail=str(exc))
        return self._get(self.sru_url(cql, limit=limit))

    def fetch(self, external_id: str) -> SourceResult:
        if not PPN.match(external_id):
            return SourceResult("refused", detail="PPN mal formé")
        result = self._get(RECORD_URL.format(ppn=external_id))
        if result.status == "refused" and result.detail == "HTTP 404":
            return SourceResult("not_found", detail="PPN inconnu ou fusionné")
        return result

    def cover(self, request: CoverRequest) -> CoverResult:
        return CoverResult("unsupported", detail="le Sudoc ne fournit pas de couvertures")
