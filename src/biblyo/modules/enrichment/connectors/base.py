# SPDX-License-Identifier: EUPL-1.2
"""Contrat d'un connecteur et notice normalisée (`Candidate`), indépendante de la source.

Un candidat ne porte aucune donnée d'exemplaire : ni cote, ni localisation, ni note de bibliothèque. Chaque valeur
garde son texte d'origine et la zone d'où elle vient, pour la provenance et pour l'explication du score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

RecordStatus = Literal["validated", "sparse"]
RecordKind = Literal["edition", "set"]
Medium = Literal["text", "other"]
ReuseStatus = Literal["established", "third_party"]
#: Ce que dit la mention de responsabilité (« texte de », « dessins de »), avant tout choix d'un rôle de biblyO.
Mention = Literal[
    "text", "drawing", "colour", "illustration", "translation", "adaptation", "preface", "original", ""
]  # fmt: skip
SourceStatus = Literal["found", "not_found", "unavailable", "timeout", "throttled", "refused", "invalid_response"]
CoverStatus = Literal[
    "found", "not_announced", "announced_not_served", "none_found", "unavailable", "invalid_image", "unsupported"
]


class Capability(StrEnum):
    ISBN = "isbn"
    SEARCH = "search"
    SERIES = "series"
    COVER = "cover"


@dataclass(frozen=True)
class Text:
    """Une donnée textuelle : `value` est nettoyée, `raw` est le texte exact de la source, `origin` sa zone."""

    value: str
    raw: str
    origin: str


@dataclass(frozen=True)
class Number:
    """Une donnée numérique lue dans un texte : « DL 2007 » donne 2007, et garde « DL 2007 »."""

    value: float
    raw: str
    origin: str

    @property
    def whole(self) -> int:
        return int(self.value)


@dataclass(frozen=True)
class Person:
    """Un contributeur tel que la source le décrit. Le rôle de biblyO se décide plus tard, jamais ici."""

    name: str  # « René Goscinny »
    surname: str
    forename: str = ""
    dates: str = ""
    authority_id: str = ""  # identifiant d'autorité de la source, s'il existe
    kind: Literal["person", "organization"] = "person"
    function_code: str = ""  # code de fonction UNIMARC : « 070 »
    function_text: str = ""  # rôle écrit en clair par une notice élémentaire : « Traducteur »
    mention: Mention = ""  # ce que dit la mention de responsabilité à propos de cette personne
    origin: str = ""


@dataclass(frozen=True)
class Summary:
    text: str
    origin: str
    provider: str = ""  # « Electre », « éditeur » : fournisseur d'origine, d'après la source
    reuse: ReuseStatus = "established"


@dataclass(frozen=True)
class WorkData:
    title: Text
    subtitle: Text | None = None
    contributors: tuple[Person, ...] = ()
    series: Text | None = None  # série ou ensemble nommé par la notice
    volume: Number | None = None  # tome dans cette série
    summary: Summary | None = None
    subjects: tuple[Text, ...] = ()
    genres: tuple[Text, ...] = ()
    responsibility: str = ""  # mention de responsabilité, telle quelle


@dataclass(frozen=True)
class EditionData:
    isbn13: tuple[str, ...] = ()  # clés de contrôle vérifiées
    invalid_isbn: tuple[str, ...] = ()  # écrits dans la notice, refusés : jamais proposés
    ean: tuple[str, ...] = ()
    publisher: Text | None = None
    place: Text | None = None
    year: Number | None = None
    statement: Text | None = None
    collection: Text | None = None
    collection_number: Text | None = None
    pages: Number | None = None
    physical: Text | None = None
    language: Text | None = None


@dataclass(frozen=True)
class Candidate:
    source: str  # « bnf », « sudoc »
    external_id: str  # ARK, PPN
    url: str  # notice publique
    work: WorkData
    edition: EditionData
    record_status: RecordStatus = "validated"
    record_kind: RecordKind = "edition"
    medium: Medium = "text"
    source_agency: str = ""  # 801$b : mention exigée par la licence
    source_date: str = ""  # 801$c
    linked_ids: tuple[str, ...] = ()  # ARK de la BnF cité par une notice du Sudoc
    cover_sides: tuple[int, ...] = ()  # 1 : première de couverture ; 4 : quatrième
    leader: str = ""
    controls: tuple[tuple[str, str], ...] = ()  # zones de contrôle nettoyées
    raw: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = ()  # zones retenues, nettoyées

    @property
    def has_cover(self) -> bool:
        return 1 in self.cover_sides


@dataclass(frozen=True)
class SearchQuery:
    """Critères d'une recherche. Les connecteurs en tirent leurs requêtes ; aucun texte n'y entre sans nettoyage."""

    level: int  # 1 à 5, voir `matching.LEVELS`
    isbn13: str = ""
    isbn10: str = ""
    title: str = ""
    author: str = ""
    publisher: str = ""
    year: int | None = None
    series: str = ""
    volume: str = ""


@dataclass(frozen=True)
class SourceResult:
    status: SourceStatus
    candidates: tuple[Candidate, ...] = ()
    total: int = 0  # nombre annoncé par la source
    elapsed_ms: int = 0
    from_cache_at: str | None = None
    detail: str = ""  # diagnostic technique, jamais affiché tel quel
    retryable: bool = False  # délai dépassé, connexion perdue, HTTP 429, 502, 503, 504
    retry_after: float | None = None  # secondes demandées par la source

    @property
    def answered(self) -> bool:
        """La source a répondu : « aucun résultat » est une réponse, une panne n'en est pas une."""
        return self.status in ("found", "not_found")


@dataclass(frozen=True)
class CoverRequest:
    external_id: str = ""  # ARK d'une notice identifiée : la voie normale
    ean: str = ""
    isbn: str = ""
    side: int = 1
    announced: bool = False  # la notice signale cette couverture
    original: bool = False  # image d'origine, sinon la vignette


@dataclass(frozen=True)
class CoverResult:
    status: CoverStatus
    data: bytes = b""
    url: str = ""
    detail: str = ""
    retryable: bool = False


class Connector(Protocol):
    code: str
    label: str
    hosts: frozenset[str]
    capabilities: frozenset[Capability]

    def search(self, query: SearchQuery, *, limit: int) -> SourceResult: ...

    def fetch(self, external_id: str) -> SourceResult: ...

    def cover(self, request: CoverRequest) -> CoverResult: ...

    def from_records(self, records: tuple[Any, ...], *, total: int, cached_at: str) -> SourceResult: ...


@dataclass(frozen=True)
class Licence:
    """Mention exigée par les conditions de réutilisation d'une source."""

    holder: str  # « Bibliothèque nationale de France »
    name: str  # « Licence Ouverte de l'État 2.0 »
    url: str
    covers_note: str = field(default="")
