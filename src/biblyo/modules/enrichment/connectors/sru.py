# SPDX-License-Identifier: EUPL-1.2
"""SRU sans réseau : construction des requêtes CQL et lecture de l'enveloppe d'une réponse.

Une erreur de requête revient en HTTP 200 avec un diagnostic : il est lu avant le nombre de résultats. Aucun texte
saisi n'entre dans une requête sans passer par `search_terms`, qui retire guillemets, opérateurs et ponctuation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree.ElementTree import Element  # nosec B405 : l'analyse est faite en amont, par xmlsafe

from biblyo.modules.enrichment.connectors.base import SearchQuery
from biblyo.modules.enrichment.normalize import search_terms

_DIGITS = re.compile(r"^\d+$")
_ISBN13 = re.compile(r"^\d{13}$")
_ISBN10 = re.compile(r"^\d{9}[\dX]$")


HARMLESS_DIAGNOSTICS = frozenset({"0", "61"})
PAYLOAD_ROOTS = frozenset({"searchRetrieveResponse", "record"})


def is_payload(root: Element) -> bool:
    """Une réponse SRU ou une notice seule : toute autre racine (page HTML bien formée) est une réponse invalide."""
    return _local(root.tag) in PAYLOAD_ROOTS


class QueryError(ValueError):
    """Les critères ne permettent aucune requête à ce niveau."""


@dataclass(frozen=True)
class Diagnostic:
    uri: str
    details: str
    message: str

    @property
    def code(self) -> str:
        return self.uri.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Envelope:
    total: int
    records: tuple[Element, ...]
    diagnostics: tuple[Diagnostic, ...]
    surrogates: int = 0  # notices remplacées par un diagnostic : écartées une à une, la réponse reste bonne

    @property
    def refused(self) -> bool:
        """Un vrai diagnostic : le Sudoc joint le code 0 à une réponse normale et le code 61 à « aucun résultat »."""
        return any(d.code not in HARMLESS_DIAGNOSTICS for d in self.diagnostics)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_envelope(root: Element) -> Envelope:
    from biblyo.modules.enrichment.connectors.unimarc import iter_records

    total = 0
    diagnostics: list[Diagnostic] = []
    surrogates = 0
    # La BnF remplace parfois une notice illisible par un diagnostic dans son <recordData> (1/131, « erreur de
    # traitement ») : il ne vise que cette notice, pas la requête.
    in_record = {id(e) for data in root.iter() if _local(data.tag) == "recordData" for e in data.iter()}
    for element in root.iter():
        name = _local(element.tag)
        if name == "numberOfRecords" and _DIGITS.match((element.text or "").strip()):
            total = int((element.text or "").strip())
        elif name in ("diagnostic", "diagnostics"):
            parts = {_local(child.tag): (child.text or "").strip() for child in element}
            if "uri" not in parts:  # le Sudoc met les éléments directement sous <diagnostics>
                continue
            if id(element) in in_record:
                surrogates += 1
            else:
                diagnostics.append(Diagnostic(parts["uri"], parts.get("details", ""), parts.get("message", "")))
    return Envelope(total, tuple(iter_records(root)), tuple(diagnostics), surrogates)


# --- BnF --------------------------------------------------------------------------------------------------------------
def _quoted(value: str) -> str:
    return '"' + value.replace('"', " ").replace("\\", " ").strip() + '"'


def _isbn_terms(query: SearchQuery) -> str:
    forms = [query.isbn13] if _ISBN13.match(query.isbn13) else []
    if _ISBN10.match(query.isbn10):
        forms.append(query.isbn10)
    if not forms:
        raise QueryError("aucun ISBN")
    return " ".join(forms)


def bnf_query(query: SearchQuery) -> str:
    """CQL de la BnF pour un niveau de recherche. L'ISBN est cherché sous ses deux formes : la BnF l'indexe tel
    qu'il a été saisi. `bib.fuzzyIsbn` n'est jamais utilisé : il accepte une clé de contrôle fausse."""
    title, author = search_terms(query.title), search_terms(query.author)
    publisher, series = search_terms(query.publisher, limit=4), search_terms(query.series)
    books = ' and bib.doctype any "a"'
    if query.level == 1:
        return f"bib.isbn any {_quoted(_isbn_terms(query))}"
    if query.level == 2:
        if not title or not author:
            raise QueryError("titre et auteur requis")
        return f"bib.title all {_quoted(title)} and bib.author all {_quoted(author)}{books}"
    if query.level == 3:
        if not title or not publisher:
            raise QueryError("titre et éditeur requis")
        return f"bib.title all {_quoted(title)} and bib.publisher all {_quoted(publisher)}{books}"
    if query.level == 4:
        if not series:
            raise QueryError("série requise")
        terms = " ".join(part for part in (series, search_terms(query.volume, limit=2), title) if part)
        clause = f"bib.title all {_quoted(terms)}"
        return clause + (f" and bib.author all {_quoted(author)}" if author else "") + books
    if query.level == 5:
        terms = " ".join(part for part in (title, author) if part)
        if not terms:
            raise QueryError("aucun terme")
        return f"bib.anywhere all {_quoted(terms)}"
    raise QueryError(f"niveau inconnu : {query.level}")


# --- Sudoc ------------------------------------------------------------------------------------------------------------
def _sudoc_clause(index: str, value: str) -> str:
    return f"{index}={value}"


def sudoc_query(query: SearchQuery) -> str:
    """Syntaxe du Sudoc : `index=termes`, `and` entre les clauses, termes sans accent ni apostrophe."""
    title, author = search_terms(query.title), search_terms(query.author)
    publisher, series = search_terms(query.publisher, limit=4), search_terms(query.series)
    if query.level == 1:
        if not _ISBN13.match(query.isbn13):
            raise QueryError("aucun ISBN")
        return _sudoc_clause("isb", query.isbn13)
    if query.level == 2:
        if not title or not author:
            raise QueryError("titre et auteur requis")
        return " and ".join((_sudoc_clause("mti", title), _sudoc_clause("aut", author)))
    if query.level == 3:
        if not title or not publisher:
            raise QueryError("titre et éditeur requis")
        clauses = [_sudoc_clause("mti", title), _sudoc_clause("edi", publisher)]
        if query.year:
            clauses.append(_sudoc_clause("apu", str(query.year)))
        return " and ".join(clauses)
    if query.level == 4:
        if not series:
            raise QueryError("série requise")
        terms = " ".join(part for part in (series, title) if part)
        clauses = [_sudoc_clause("mti", terms)]
        if author:
            clauses.append(_sudoc_clause("aut", author))
        return " and ".join(clauses)
    if query.level == 5:
        terms = " ".join(part for part in (title, author) if part)
        if not terms:
            raise QueryError("aucun terme")
        return _sudoc_clause("tou", terms)
    raise QueryError(f"niveau inconnu : {query.level}")
