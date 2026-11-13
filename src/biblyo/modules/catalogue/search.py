# SPDX-License-Identifier: EUPL-1.2
"""Recherche plein texte du catalogue (révision 0003).

Une ligne par œuvre, rowid égal à `works.id`, regroupant toutes ses éditions et ses exemplaires. Le texte est
normalisé avant l'index : « coeur » trouve « cœur ». L'index suit l'écriture dans la même transaction.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from biblyo.core.isbn import clean as clean_code
from biblyo.core.isbn import parse_isbn
from biblyo.core.text import normalize
from biblyo.modules.catalogue.models import (
    Agent,
    Collection,
    Copy,
    Edition,
    EditionContributor,
    EditionIdentifier,
    Publisher,
    Series,
    Tag,
    Work,
    WorkContributor,
    WorkSeries,
    WorkTag,
)
from biblyo.modules.catalogue.rules import format_volume

COLUMNS: tuple[str, ...] = (
    "title",
    "subtitle",
    "contributors",
    "series",
    "collection",
    "publisher",
    "identifiers",
    "years",
    "call_numbers",
    "barcodes",
    "tags",
)
_CODE_COLUMNS = frozenset({"identifiers", "barcodes", "call_numbers"})
_COLUMN_LIST = ", ".join(COLUMNS)
_INSERT = text(
    f"INSERT INTO works_search(rowid, {_COLUMN_LIST}) "  # noqa: S608 : noms de colonnes constants
    f"VALUES (:id, {', '.join(':' + column for column in COLUMNS)})"
)
_BATCH = 500

#: Poids de chaque colonne dans le classement : le titre et les codes d'abord, dans l'ordre de COLUMNS.
RANK = "bm25(works_search, 10.0, 4.0, 6.0, 6.0, 2.0, 2.0, 8.0, 1.0, 1.0, 8.0, 2.0)"

#: Critères par champ que l'on peut écrire dans la recherche : « auteur:goscinny », « série:astérix ».
FIELDS: dict[str, tuple[str, ...]] = {
    "titre": ("title", "subtitle"),
    "auteur": ("contributors",),
    "contributeur": ("contributors",),
    "serie": ("series",),
    "collection": ("collection",),
    "editeur": ("publisher",),
    "isbn": ("identifiers",),
    "annee": ("years",),
    "code": ("barcodes",),
    "cote": ("call_numbers",),
    "tag": ("tags",),
    "genre": ("tags",),
    "categorie": ("tags",),
}
_CHUNK = re.compile(r'(?:(?P<field>[^\W\d_]+):)?(?P<value>"[^"]*"|\S+)')
_NUMERIC_CODE = re.compile(r"^[0-9Xx]+(?:[\-‐‑–—.][0-9Xx]+)+$")
_MAX_QUERY = 500
_MAX_TERMS = 32


def _value_terms(value: str) -> list[str]:
    """Formes à chercher pour un mot saisi. Un ISBN avec tirets se compacte, sinon le découpage le perdrait."""
    if _NUMERIC_CODE.match(value):
        compact = normalize(clean_code(value))
        return [compact] if compact else []
    return normalize(value).split()


def _scoped(expression: str, field: str | None) -> str:
    if field is None:
        return expression
    columns = FIELDS[field]
    scope = columns[0] if len(columns) == 1 else "{" + " ".join(columns) + "}"
    return f"{scope} : {expression}"


def build_match(query: str | None) -> str | None:
    """Unique constructeur de requête FTS5, et sa seule porte d'entrée.

    Chaque terme, réduit à des lettres et des chiffres, est placé entre guillemets : aucun opérateur saisi (`NEAR`,
    `OR`, `*`, parenthèses, guillemets) n'atteint la syntaxe de FTS5. Rend `None` s'il ne reste rien à chercher.
    """
    raw = (query or "").strip()[:_MAX_QUERY]
    if not raw:
        return None
    isbn = parse_isbn(raw)
    if isbn is not None:
        return f'identifiers : "{normalize(isbn.isbn13)}"'
    expressions: list[str] = []
    for found in _CHUNK.finditer(raw):
        field = normalize(found["field"]) if found["field"] else None
        value = found["value"]
        if field is not None and field not in FIELDS:  # « foo:bar » n'est pas un critère : deux mots
            value, field = f"{found['field']} {value}", None
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            phrase = normalize(value)
            if phrase:
                expressions.append(_scoped(f'"{phrase}"*', field))
            continue
        for chunk in value.split():
            field_isbn = parse_isbn(chunk) if field == "isbn" else None
            if field_isbn is not None:
                expressions.append(_scoped(f'"{normalize(field_isbn.isbn13)}"', field))
                continue
            expressions += [_scoped(f'"{term}"*', field) for term in _value_terms(chunk)]
    if not expressions:
        return None
    return " AND ".join(expressions[:_MAX_TERMS])


def match_ids(session: Session, query: str | None, limit: int = 50) -> list[int]:
    """Œuvres correspondant à la recherche, les plus pertinentes d'abord. Sans critère : aucune."""
    match = build_match(query)
    if match is None:
        return []
    rows = session.execute(
        text(f"SELECT rowid FROM works_search WHERE works_search MATCH :match ORDER BY {RANK}, rowid LIMIT :limit"),  # noqa: S608
        {"match": match, "limit": limit},
    )
    return [row[0] for row in rows]


def _join(values: Iterable[str | None]) -> str:
    return " ".join(sorted({normalized for value in values if (normalized := normalize(value))}))


def _codes(values: Iterable[str | None]) -> str:
    """Un code s'indexe en mots séparés et d'un bloc : « EX-000042 » se retrouve en « ex 000042 » et « ex000042 »."""
    forms: list[str] = []
    for value in values:
        normalized = normalize(value)
        forms += [normalized, normalized.replace(" ", "")]
    return " ".join(sorted({form for form in forms if form}))


def documents_for(session: Session, work_ids: Sequence[int]) -> dict[int, dict[str, str]]:
    """Documents d'index des œuvres demandées, en quelques requêtes quel que soit leur nombre."""
    ids = list(work_ids)
    values: dict[str, dict[int, list[str | None]]] = {column: defaultdict(list) for column in COLUMNS}
    existing: set[int] = set()
    for work_id, title, subtitle, original in session.execute(
        select(Work.id, Work.title, Work.subtitle, Work.original_title).where(Work.id.in_(ids))
    ):
        existing.add(work_id)
        values["title"][work_id] += [title, original]
        values["subtitle"][work_id].append(subtitle)
    for work_id, name in session.execute(
        select(WorkContributor.work_id, Agent.display_name)
        .join(Agent, Agent.id == WorkContributor.agent_id)
        .where(WorkContributor.work_id.in_(ids))
    ):
        values["contributors"][work_id].append(name)
    for work_id, name in session.execute(
        select(Edition.work_id, Agent.display_name)
        .join(EditionContributor, EditionContributor.edition_id == Edition.id)
        .join(Agent, Agent.id == EditionContributor.agent_id)
        .where(Edition.work_id.in_(ids))
    ):
        values["contributors"][work_id].append(name)
    for work_id, title, number, label in session.execute(
        select(WorkSeries.work_id, Series.title, WorkSeries.volume_number, WorkSeries.volume_label)
        .join(Series, Series.id == WorkSeries.series_id)
        .where(WorkSeries.work_id.in_(ids))
    ):
        values["series"][work_id].append(f"{title} {format_volume(number, label)}")
    for work_id, variant, year, publisher, collection in session.execute(
        select(Edition.work_id, Edition.title_variant, Edition.publication_year, Publisher.name, Collection.name)
        .outerjoin(Publisher, Publisher.id == Edition.publisher_id)
        .outerjoin(Collection, Collection.id == Edition.collection_id)
        .where(Edition.work_id.in_(ids))
    ):
        values["title"][work_id].append(variant)
        values["years"][work_id].append(str(year) if year else None)
        values["publisher"][work_id].append(publisher)
        values["collection"][work_id].append(collection)
    for work_id, value in session.execute(
        select(Edition.work_id, EditionIdentifier.value)
        .join(EditionIdentifier, EditionIdentifier.edition_id == Edition.id)
        .where(Edition.work_id.in_(ids))
    ):
        values["identifiers"][work_id].append(value)
    for work_id, barcode, inventory, call_number in session.execute(
        select(Edition.work_id, Copy.barcode, Copy.inventory_number, Copy.call_number)
        .join(Copy, Copy.edition_id == Edition.id)
        .where(Edition.work_id.in_(ids))
    ):
        values["barcodes"][work_id] += [barcode, inventory]
        values["call_numbers"][work_id].append(call_number)
    for work_id, label in session.execute(
        select(WorkTag.work_id, Tag.label).join(Tag, Tag.id == WorkTag.tag_id).where(WorkTag.work_id.in_(ids))
    ):
        values["tags"][work_id].append(label)
    return {
        work_id: {
            column: (_codes if column in _CODE_COLUMNS else _join)(values[column].get(work_id, ()))
            for column in COLUMNS
        }
        for work_id in existing
    }


def index_works(session: Session, work_ids: Sequence[int]) -> None:
    """Remplace les lignes d'index de ces œuvres, dans la transaction en cours."""
    for start in range(0, len(work_ids), _BATCH):
        batch = list(work_ids[start : start + _BATCH])
        remove_works(session, batch)
        documents = documents_for(session, batch)
        if documents:
            session.execute(_INSERT, [{"id": work_id, **document} for work_id, document in documents.items()])


def remove_works(session: Session, work_ids: Sequence[int]) -> None:
    if work_ids:
        session.execute(text("DELETE FROM works_search WHERE rowid = :id"), [{"id": work_id} for work_id in work_ids])


def reindex_all(session: Session) -> int:
    """Reconstruit tout l'index dans une seule transaction : un échec laisse l'ancien en place."""
    session.execute(text("DELETE FROM works_search"))
    ids = list(session.scalars(select(Work.id).order_by(Work.id)))
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        documents = documents_for(session, batch)
        session.execute(_INSERT, [{"id": work_id, **document} for work_id, document in documents.items()])
    return len(ids)


def inconsistencies(session: Session) -> list[str]:
    """Écarts entre l'index et le catalogue : œuvres absentes, lignes orphelines, contenus différents."""
    works = set(session.scalars(select(Work.id)))
    indexed: dict[int, dict[str, str]] = {}
    for row in session.execute(text(f"SELECT rowid, {_COLUMN_LIST} FROM works_search")):  # noqa: S608
        indexed[row[0]] = dict(zip(COLUMNS, row[1:], strict=True))
    problems: list[str] = []
    missing = works - set(indexed)
    orphans = set(indexed) - works
    if missing:
        problems.append(f"index de recherche : {len(missing)} œuvre(s) absente(s), dont {sorted(missing)[:5]}")
    if orphans:
        problems.append(f"index de recherche : {len(orphans)} ligne(s) orpheline(s), dont {sorted(orphans)[:5]}")
    common = sorted(works & set(indexed))
    different: list[int] = []
    for start in range(0, len(common), _BATCH):
        batch = common[start : start + _BATCH]
        documents = documents_for(session, batch)
        different += [work_id for work_id in batch if documents[work_id] != indexed[work_id]]
    if different:
        problems.append(f"index de recherche : {len(different)} œuvre(s) à réindexer, dont {different[:5]}")
    return problems
