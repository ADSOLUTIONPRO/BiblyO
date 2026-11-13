# SPDX-License-Identifier: EUPL-1.2
"""Recherche plein texte : table FTS5 `works_search`, remplie à partir des œuvres existantes.

Révision 0003. Elle n'écrit que dans l'index. Le texte est normalisé avant l'indexation : `remove_diacritics` ne
traite pas les ligatures. La normalisation et la composition des documents sont recopiées ici à dessein : une
révision figée ne dépend pas d'un code qui peut changer. `biblyo db check` vérifie la concordance avec
`modules/catalogue/search.py`, et `biblyo search reindex` reconstruit l'index à tout moment.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_NON_WORD = re.compile(r"[^a-z0-9]+")
_LIGATURES = str.maketrans({"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE"})

COLUMNS = (
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


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value.translate(_LIGATURES))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return _NON_WORD.sub(" ", stripped).strip()


def _join(values: Iterable[str | None]) -> str:
    return " ".join(sorted({text for value in values if (text := _normalize(value))}))


def _codes(values: Iterable[str | None]) -> str:
    forms: list[str] = []
    for value in values:
        text = _normalize(value)
        forms += [text, text.replace(" ", "")]
    return " ".join(sorted({form for form in forms if form}))


def _volume(number: float | None, label: str | None) -> str:
    if label:
        return label
    if number is None:
        return ""
    return str(int(number)) if float(number).is_integer() else str(number).replace(".", ",")


def _fill(bind) -> None:  # type: ignore[no-untyped-def]
    run = bind.exec_driver_sql
    values: dict[str, dict[int, list[str | None]]] = {column: defaultdict(list) for column in COLUMNS}
    works = run("SELECT id, title, subtitle, original_title FROM works").fetchall()
    for work_id, title, subtitle, original in works:
        values["title"][work_id] += [title, original]
        values["subtitle"][work_id].append(subtitle)
    for work_id, name in run(
        "SELECT wc.work_id, a.display_name FROM work_contributors wc JOIN agents a ON a.id = wc.agent_id"
    ):
        values["contributors"][work_id].append(name)
    for work_id, name in run(
        "SELECT e.work_id, a.display_name FROM edition_contributors ec JOIN agents a ON a.id = ec.agent_id "
        "JOIN editions e ON e.id = ec.edition_id"
    ):
        values["contributors"][work_id].append(name)
    for work_id, title, number, label in run(
        "SELECT ws.work_id, s.title, ws.volume_number, ws.volume_label FROM work_series ws "
        "JOIN series s ON s.id = ws.series_id"
    ):
        values["series"][work_id].append(f"{title} {_volume(number, label)}")
    for work_id, variant, year, publisher, collection in run(
        "SELECT e.work_id, e.title_variant, e.publication_year, p.name, c.name FROM editions e "
        "LEFT JOIN publishers p ON p.id = e.publisher_id LEFT JOIN collections c ON c.id = e.collection_id"
    ):
        values["title"][work_id].append(variant)
        values["years"][work_id].append(str(year) if year else None)
        values["publisher"][work_id].append(publisher)
        values["collection"][work_id].append(collection)
    for work_id, value in run(
        "SELECT e.work_id, i.value FROM edition_identifiers i JOIN editions e ON e.id = i.edition_id"
    ):
        values["identifiers"][work_id].append(value)
    for work_id, barcode in run("SELECT e.work_id, c.barcode FROM copies c JOIN editions e ON e.id = c.edition_id"):
        values["barcodes"][work_id].append(barcode)
    for work_id, label in run("SELECT wt.work_id, t.label FROM work_tags wt JOIN tags t ON t.id = wt.tag_id"):
        values["tags"][work_id].append(label)

    code_columns = {"identifiers", "barcodes"}
    insert = (
        f"INSERT INTO works_search(rowid, {', '.join(COLUMNS)}) "  # noqa: S608 : noms de colonnes constants
        f"VALUES ({', '.join('?' * (len(COLUMNS) + 1))})"
    )
    for row in works:
        work_id = row[0]
        document = [
            (_codes if column in code_columns else _join)(values[column].get(work_id, ())) for column in COLUMNS
        ]
        run(insert, (work_id, *document))


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        f"CREATE VIRTUAL TABLE works_search USING fts5({', '.join(COLUMNS)}, "
        "tokenize = 'unicode61 remove_diacritics 2')"
    )
    _fill(bind)


def downgrade() -> None:
    """L'index se reconstruit : le retirer ne perd aucune donnée."""
    op.get_bind().exec_driver_sql("DROP TABLE works_search")
