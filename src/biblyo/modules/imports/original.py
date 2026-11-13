# SPDX-License-Identifier: EUPL-1.2
"""Export au format d'origine et comparaison cellule par cellule (lot L6, tâches T7 et T8).

**Règle de priorité** entre la valeur source conservée et la valeur de biblyO, cellule par cellule :

1. tant que la valeur de biblyO correspondant à une cellule est **celle que l'import a écrite**, la cellule exporte
   la **valeur d'origine, telle que lue** : même texte, même type (nombre ou texte), même casse, même article
   inversé (« Schtroumpfs (Les) ») — l'affichage de biblyO peut différer, l'export rend le fichier du Club ;
2. dès qu'une personne a **modifié** cette valeur dans biblyO (titre corrigé, cote changée, état revu…), la cellule
   exporte la **valeur modifiée**, et l'onglet « Modifications » donne la cellule, la valeur d'origine, la valeur
   exportée et la raison : une différence légitime est montrée, jamais présentée comme une égalité ;
3. une **proposition** (ISBN, année, forme d'un nom…) n'entre jamais dans les colonnes d'origine : l'onglet
   « Compléments et propositions » la donne avec son statut (proposée, confirmée, rejetée) ;
4. une colonne **conservée telle quelle** exporte toujours sa valeur d'origine.

La feuille d'origine garde ses colonnes dans leur ordre exact, leurs en-têtes exacts, sa ligne d'en-têtes et le
numéro de ligne de chaque exemplaire : la ligne 356 du classeur reste la ligne 356 de l'export. Les exemplaires
créés ensuite dans biblyO vont dans un onglet à part, pour ne pas altérer la structure d'origine. Les formats de
cellule (polices, largeurs, formats de nombre) ne sont pas restitués : seuls les valeurs et leurs types le sont.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell
from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.core.i18n import _
from biblyo.modules.catalogue.models import (
    Agent,
    Collection,
    Copy,
    Edition,
    EditionIdentifier,
    Publisher,
    Series,
    Work,
    WorkContributor,
    WorkSeries,
)
from biblyo.modules.catalogue.rules import format_number
from biblyo.modules.enrichment.models import EditionIsbnCandidate
from biblyo.modules.imports.models import ImportBatch, ImportLink, ImportProposal, ImportRow
from biblyo.modules.imports.profile import CONDITIONS, KEEP
from biblyo.modules.imports.reader import column_letter

STATUS_LABELS = {"proposed": _("proposée"), "confirmed": _("confirmée"), "rejected": _("rejetée")}
REASONS = {
    "series": _("Série renommée ou changée dans biblyO"),
    "volume": _("Tome modifié dans biblyO"),
    "title": _("Titre modifié dans biblyO"),
    "contributor": _("Contributeur modifié ou retiré dans biblyO"),
    "publisher": _("Éditeur modifié dans biblyO"),
    "collection": _("Collection modifiée dans biblyO"),
    "isbn": _("ISBN modifié dans biblyO"),
    "year": _("Année modifiée dans biblyO"),
    "inventory": _("Numéro d'inventaire modifié dans biblyO"),
    "call_number": _("Cote modifiée dans biblyO"),
    "condition": _("État modifié dans biblyO"),
    "acquisition": _("Acquisition modifiée dans biblyO"),
    "copy_note": _("Note de l'exemplaire modifiée dans biblyO"),
    "barcode": _("Code-barres modifié dans biblyO"),
    "deleted": _("Exemplaire supprimé de biblyO : valeur d'origine gardée"),
}


@dataclass
class Modification:
    cell: str
    row: int
    column: str
    original: Any
    exported: Any
    reason: str


@dataclass
class OriginalExport:
    data: bytes
    sheet: str
    rows: int
    modifications: list[Modification] = field(default_factory=list)
    added: int = 0


# --- État actuel de ce que l'import a écrit ---------------------------------------------------------------------------
@dataclass
class _Now:
    copy: Copy | None
    edition: Edition | None
    work: Work | None
    series_title: str | None
    volume: tuple[float | None, str | None] | None
    publisher: tuple[int | None, str | None]
    collection: tuple[int | None, str | None]
    isbns: list[str]
    contributors: dict[int, str]  # agent → nom actuel, s'il est encore contributeur de l'œuvre


def _now(session: Session, stored: dict[str, Any]) -> _Now:
    copy = session.get(Copy, stored["copy"]["id"])
    edition = session.get(Edition, copy.edition_id) if copy else None
    work = session.get(Work, edition.work_id) if edition else None
    series_title, volume = None, None
    if work is not None:
        link = session.scalar(
            select(WorkSeries).where(WorkSeries.work_id == work.id, WorkSeries.is_primary == True)  # noqa: E712
        )
        if link is not None:
            series_title = session.scalar(select(Series.title).where(Series.id == link.series_id))
            volume = (link.volume_number, link.volume_label)
    publisher = (None, None)
    collection: tuple[int | None, str | None] = (None, None)
    isbns: list[str] = []
    if edition is not None:
        if edition.publisher_id:
            publisher = (
                edition.publisher_id,
                session.scalar(select(Publisher.name).where(Publisher.id == edition.publisher_id)),
            )
        if edition.collection_id:
            collection = (
                edition.collection_id,
                session.scalar(select(Collection.name).where(Collection.id == edition.collection_id)),
            )
        isbns = list(
            session.scalars(
                select(EditionIdentifier.value).where(
                    EditionIdentifier.edition_id == edition.id, EditionIdentifier.scheme == "isbn13"
                )
            )
        )
    contributors: dict[int, str] = {}
    if work is not None:
        for agent_id, name in session.execute(
            select(Agent.id, Agent.display_name)
            .join(WorkContributor, WorkContributor.agent_id == Agent.id)
            .where(WorkContributor.work_id == work.id)
        ):
            contributors[agent_id] = name
    return _Now(copy, edition, work, series_title, volume, publisher, collection, isbns, contributors)


def _volume_text(number: float | None, label: str | None) -> str:
    return label or (format_number(number) if number is not None else "")


def _cell(code: str, position: int, raw: Any, stored: dict[str, Any], now: _Now) -> tuple[Any, str]:
    """(valeur exportée, raison) ; raison vide quand la valeur d'origine est restituée."""
    if code in (KEEP, "isbn_proposed", "year_proposed", "location_hint") or now.copy is None:
        return raw, ""
    copy, edition, work = now.copy, now.edition, now.work
    s_copy, s_edition = stored["copy"], stored["edition"]
    if code == "series" and stored.get("series"):
        current = now.series_title
        return (raw, "") if current == stored["series"]["title"] else (current or None, REASONS["series"])
    if code == "volume" and stored.get("volume"):
        before = stored["volume"]["label"] or ""
        after = _volume_text(*now.volume) if now.volume else ""
        if after == before:
            return raw, ""
        exported: Any = int(after) if after.isdigit() else (after or None)
        return exported, REASONS["volume"]
    if code == "title" and work is not None:
        return (raw, "") if work.title == stored["work"]["title"] else (work.title, REASONS["title"])
    if code == "contributor":
        entry = next((c for c in stored["contributors"] if c["column"] == position), None)
        if entry is None:
            return raw, ""
        current = now.contributors.get(entry["agent_id"])
        return (raw, "") if current == entry["written"] else (current, REASONS["contributor"])
    if code == "publisher" and edition is not None:
        before = (s_edition["publisher_id"], s_edition.get("publisher"))
        return (raw, "") if now.publisher == before else (now.publisher[1], REASONS["publisher"])
    if code == "collection" and edition is not None:
        kind = s_edition["collection_kind"]
        if kind == "mention":
            same = (edition.edition_statement or "") == (s_edition["collection"] or "")
            return (raw, "") if same else (edition.edition_statement, REASONS["collection"])
        if kind == "note":
            same = (edition.notes or "") == (s_edition["collection"] or "")
            return (raw, "") if same else (edition.notes, REASONS["collection"])
        before = (s_edition.get("collection_id"), s_edition.get("collection_name"))
        return (raw, "") if now.collection == before else (now.collection[1], REASONS["collection"])
    if code == "isbn" and edition is not None:
        before = s_edition.get("isbn")
        if (before in now.isbns) if before else not now.isbns:
            return raw, ""
        return (now.isbns[0] if now.isbns else None), REASONS["isbn"]
    if code == "year" and edition is not None:
        before = s_edition.get("year")
        after = str(edition.publication_year) if edition.publication_year else None
        return (raw, "") if after == before else (edition.publication_year, REASONS["year"])
    simple = {
        "inventory": ("inventory_number", "inventory"),
        "call_number": ("call_number", "call_number"),
        "acquisition": ("acquisition_note", "acquisition_note"),
        "copy_note": ("notes", "notes"),
        "barcode": ("barcode", "barcode"),
    }
    if code in simple:
        attribute, key = simple[code]
        current = getattr(copy, attribute)
        return (raw, "") if current == s_copy[key] else (current, REASONS[code])
    if code == "condition":
        if (copy.condition, copy.condition_note) == (s_copy["condition"], s_copy["condition_note"]):
            return raw, ""
        if copy.condition_note != s_copy["condition_note"]:
            return copy.condition_note, REASONS["condition"]
        return CONDITIONS.get(copy.condition or "", copy.condition), REASONS["condition"]
    return raw, ""


def _write(cell: Cell, value: Any) -> None:
    """Le type d'origine : un nombre reste un nombre, un texte reste un texte et ne devient jamais une formule."""
    if value is None:
        return
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


# --- Export --------------------------------------------------------------------------------------------------------
def original_workbook(session: Session, batch_id: int) -> OriginalExport:
    """Classeur au format d'origine d'un lot appliqué, suivant la règle de priorité du module."""
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.status != "applied" or not batch.summary:
        raise LookupError(batch_id)
    summary = json.loads(batch.summary)
    columns: list[list[Any]] = summary["columns"]
    fields: dict[str, str] = summary["fields"]
    header_row = int(summary["file"]["header_row"])
    contributor_position = {
        label: rank for rank, label in enumerate(c[3] for c in columns if fields.get(c[3]) == "contributor")
    }
    book = Workbook()
    sheet = book.active
    sheet.title = batch.sheet[:31] or "Feuil1"
    for index, _letter, header, _label in columns:
        _write(sheet.cell(row=header_row, column=index + 1), header if header != "" else None)
    modifications: list[Modification] = []
    rows = list(session.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_number)))
    complements: list[tuple[ImportRow, dict[str, Any], dict[str, Any], _Now]] = []
    for row in rows:
        raw = json.loads(row.raw)
        stored = json.loads(row.normalized or "{}")
        now = _now(session, stored)
        complements.append((row, raw, stored, now))
        if now.copy is None:
            first = next((c for c in columns if fields.get(c[3]) == "inventory"), columns[0])
            modifications.append(
                Modification(
                    f"{first[1]}{row.row_number}", row.row_number, first[3], raw.get(first[3]), raw.get(first[3]),
                    REASONS["deleted"],
                )
            )  # fmt: skip
        for index, letter, _header, label in columns:
            code = fields.get(label, KEEP)
            value, reason = _cell(code, contributor_position.get(label, -1), raw.get(label), stored, now)
            _write(sheet.cell(row=row.row_number, column=index + 1), value)
            if reason:
                modifications.append(
                    Modification(f"{letter}{row.row_number}", row.row_number, label, raw.get(label), value, reason)
                )
    _complements_sheet(book, session, batch, summary, complements)
    _modifications_sheet(book, modifications)
    _differences_sheet(book, summary)
    added = _added_sheet(book, session, batch, summary)
    _readme_sheet(book, batch, summary, len(rows), len(modifications), added)
    out = io.BytesIO()
    book.save(out)
    return OriginalExport(out.getvalue(), sheet.title, len(rows), modifications, added)


def _table(book: Workbook, title: str, headers: list[str], lines: list[list[Any]]) -> None:
    sheet = book.create_sheet(title[:31])
    for index, header in enumerate(headers, start=1):
        _write(sheet.cell(row=1, column=index), header)
    for number, values in enumerate(lines, start=2):
        for index, value in enumerate(values, start=1):
            _write(sheet.cell(row=number, column=index), value)
    sheet.freeze_panes = "A2"


def _complements_sheet(
    book: Workbook,
    session: Session,
    batch: ImportBatch,
    summary: dict[str, Any],
    rows: list[tuple[ImportRow, dict[str, Any], dict[str, Any], _Now]],
) -> None:
    """Colonnes que le fichier complémentaire ajoute (ISBN, années à vérifier…), et le statut de chaque
    proposition : rien de tout cela n'entre dans les colonnes d'origine."""
    main_labels = {c[3] for c in summary["columns"]}
    extra = [c for c in summary.get("complement_columns", []) if c[3] not in main_labels]
    headers = [
        _("Ligne"),
        _("Numéro d'inventaire"),
        _("Identifiant biblyO de l'exemplaire"),
        _("Code-barres"),
        _("Ligne du fichier complémentaire"),
    ]
    for _index, _letter, header, label in extra:
        headers += [header or label, _("Statut : {h}").format(h=header or label)]
    headers += [_("Propositions"), _("Champs gardés tels quels")]
    keep_labels = [label for label, code in summary["fields"].items() if code == KEEP]
    lines: list[list[Any]] = []
    for row, raw, stored, now in rows:
        proposals = session.execute(
            select(ImportProposal.field, ImportProposal.value, ImportProposal.status).where(
                ImportProposal.row_id == row.id
            )
        ).all()
        complement = (stored.get("complement") or {}).get("values", {})
        line: list[Any] = [
            row.row_number,
            stored["copy"]["inventory"],
            now.copy.public_id if now.copy else stored["copy"]["public_id"],
            now.copy.barcode if now.copy else None,
            (stored.get("complement") or {}).get("row") or None,
        ]
        for _index, _letter, _header, label in extra:
            value = complement.get(label)
            line += [value, _status_of(session, now, proposals, value) if value not in (None, "") else None]
        line.append(" | ".join(f"{f} : {v} ({STATUS_LABELS.get(s, s)})" for f, v, s in proposals) or None)
        line.append(" | ".join(f"{k} : {raw.get(k)}" for k in keep_labels if raw.get(k) not in (None, "")) or None)
        lines.append(line)
    _table(book, _("Compléments et propositions"), headers, lines)


def _status_of(session: Session, now: _Now, proposals: list[Any], value: Any) -> str:
    """Statut d'une valeur ajoutée : celui du candidat ISBN (relu en L5) s'il y en a un, sinon de la proposition."""
    text = str(value)
    if now.edition is not None:
        from biblyo.core.isbn import parse_isbn

        parsed = parse_isbn(text)
        if parsed is not None:
            status = session.scalar(
                select(EditionIsbnCandidate.status).where(
                    EditionIsbnCandidate.edition_id == now.edition.id, EditionIsbnCandidate.isbn13 == parsed.isbn13
                )
            )
            if status:
                return STATUS_LABELS.get(status, status)
    for _field, proposed, status in proposals:
        if proposed == text or proposed.replace("-", "") == text.replace("-", ""):
            return STATUS_LABELS.get(status, status)
    return STATUS_LABELS["proposed"]


def _modifications_sheet(book: Workbook, modifications: list[Modification]) -> None:
    _table(
        book,
        _("Modifications"),
        [_("Cellule"), _("Ligne"), _("Colonne"), _("Valeur d'origine"), _("Valeur exportée"), _("Raison")],
        [[m.cell, m.row, m.column, m.original, m.exported, m.reason] for m in modifications],
    )


def _differences_sheet(book: Workbook, summary: dict[str, Any]) -> None:
    _table(
        book,
        _("Différences entre fichiers"),
        [
            _("Ligne"),
            _("Numéro d'inventaire"),
            _("Colonne"),
            _("Cellule"),
            _("Fichier principal"),
            _("Fichier complémentaire"),
            _("Ligne complémentaire"),
            _("Provenance"),
        ],
        [
            [d["row"], d["inventory"], d["column"], f"{d['letter']}{d['row']}", d["main"], d["complement"],
             d["complement_row"], d["origin"]]
            for d in summary.get("differences", [])
        ],
    )  # fmt: skip


def _added_sheet(book: Workbook, session: Session, batch: ImportBatch, summary: dict[str, Any]) -> int:
    """Exemplaires créés dans biblyO après l'import : à part, pour ne pas altérer la feuille d'origine."""
    imported = select(ImportLink.entity_id).where(ImportLink.entity_type == "copy")
    copies = session.execute(
        select(Copy, Work.title, Edition.public_id)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .where(Copy.id.not_in(imported), Copy.created_at >= (batch.applied_at or ""))
        .order_by(Copy.id)
    ).all()
    _table(
        book,
        _("Ajouts biblyO"),
        [_("Identifiant biblyO"), _("Titre"), _("Numéro d'inventaire"), _("Code-barres"), _("Cote"), _("Édition")],
        [[c.public_id, title, c.inventory_number, c.barcode, c.call_number, edition] for c, title, edition in copies],
    )
    return len(copies)


def _readme_sheet(
    book: Workbook, batch: ImportBatch, summary: dict[str, Any], rows: int, modifications: int, added: int
) -> None:
    lines = [
        [_("Fichier d'origine"), batch.filename],
        [_("Empreinte du fichier"), batch.file_sha256],
        [_("Feuille"), batch.sheet],
        [_("Fichier complémentaire"), batch.complement_filename or "—"],
        [_("Lot d'import"), batch.public_id or str(batch.id)],
        [_("Appliqué le"), batch.applied_at or ""],
        [_("Empreinte du plan"), batch.plan_sha256 or ""],
        [_("Lignes restituées"), rows],
        [_("Cellules modifiées dans biblyO"), modifications],
        [_("Exemplaires ajoutés ensuite"), added],
        [
            _("Règle"),
            _("Chaque cellule rend la valeur d'origine tant que biblyO garde la valeur écrite par l'import ; une "
              "valeur modifiée dans biblyO est exportée et listée dans « Modifications ». Les propositions ne "
              "sont jamais dans les colonnes d'origine."),
        ],
        [_("Non restitué"), _("Polices, largeurs, couleurs et formats de nombre : seuls valeurs et types le sont.")],
    ]  # fmt: skip
    _table(book, _("Lisez-moi"), [_("Rubrique"), _("Valeur")], lines)


# --- Comparaison cellule par cellule (T8) ---------------------------------------------------------------------------
@dataclass
class CellDifference:
    cell: str
    initial: Any
    final: Any
    reason: str


@dataclass
class Comparison:
    sheet: str
    compared: int = 0
    identical: int = 0
    explained: list[CellDifference] = field(default_factory=list)
    unexplained: list[CellDifference] = field(default_factory=list)
    rows_initial: int = 0
    rows_final: int = 0

    @property
    def faithful(self) -> bool:
        return not self.unexplained and self.rows_initial == self.rows_final


def _cells(data: bytes, sheet_name: str) -> tuple[dict[str, Any], int, int]:
    """Valeurs de la feuille, lues telles quelles par openpyxl (types compris), cellule → valeur ; et l'étendue."""
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = book[sheet_name] if sheet_name in book.sheetnames else book.worksheets[0]
        values: dict[str, Any] = {}
        last_row = last_col = 0
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value is None or (isinstance(cell.value, str) and cell.value == ""):
                    continue
                coordinate = f"{column_letter(cell.column - 1)}{cell.row}"
                values[coordinate] = cell.value
                last_row, last_col = max(last_row, cell.row), max(last_col, cell.column)
        return values, last_row, last_col
    finally:
        book.close()


def compare(initial: bytes, final: bytes, sheet_name: str, explanations: dict[str, str]) -> Comparison:
    """Compare la feuille d'origine et la feuille exportée, cellule par cellule, valeur et type. Une différence est
    « expliquée » quand l'export l'a déclarée (onglet « Modifications ») ; toute autre est un défaut."""
    before, rows_before, cols_before = _cells(initial, sheet_name)
    after, rows_after, cols_after = _cells(final, sheet_name)
    result = Comparison(sheet_name, rows_initial=rows_before, rows_final=rows_after)
    for coordinate in sorted(set(before) | set(after), key=_order):
        a, b = before.get(coordinate), after.get(coordinate)
        if a == b and type(a) is type(b):
            continue
        difference = CellDifference(coordinate, a, b, explanations.get(coordinate, ""))
        (result.explained if difference.reason else result.unexplained).append(difference)
    # Toutes les cellules du rectangle des deux feuilles, vides comprises : une cellule vidée est une différence.
    result.compared = max(rows_before, rows_after) * max(cols_before, cols_after)
    result.identical = result.compared - len(result.explained) - len(result.unexplained)
    return result


def _order(coordinate: str) -> tuple[int, int]:
    letters = "".join(c for c in coordinate if c.isalpha())
    number = int("".join(c for c in coordinate if c.isdigit()))
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return number, index


def compare_complement(
    complement: bytes, complement_sheet: str, export: bytes, summary: dict[str, Any]
) -> tuple[int, list[CellDifference]]:
    """Colonnes ajoutées par le fichier complémentaire : chaque valeur doit se retrouver, à l'identique, dans l'onglet
    « Compléments et propositions » de l'export, à la ligne de son exemplaire."""
    main_labels = {c[3] for c in summary["columns"]}
    extra = [c for c in summary.get("complement_columns", []) if c[3] not in main_labels]
    source, _rows, _cols = _cells(complement, complement_sheet)
    header_row = int((summary.get("complement") or {}).get("header_row") or 1)
    book = load_workbook(io.BytesIO(export), read_only=True, data_only=True)
    try:
        sheet = book[_("Compléments et propositions")[:31]]
        lines = list(sheet.iter_rows(values_only=True))
    finally:
        book.close()
    headers = list(lines[0])
    by_row = {line[4]: line for line in lines[1:] if line[4] is not None}
    checked = 0
    problems: list[CellDifference] = []
    for _index, letter, header, label in extra:
        position = headers.index(header or label)
        for coordinate, value in source.items():
            if "".join(c for c in coordinate if c.isalpha()) != letter or _order(coordinate)[0] <= header_row:
                continue
            checked += 1
            line = by_row.get(_order(coordinate)[0])
            found = line[position] if line else None
            if found != value or type(found) is not type(value):
                problems.append(CellDifference(coordinate, value, found, ""))
    return checked, problems
