# SPDX-License-Identifier: EUPL-1.2
"""Lecture sûre d'un fichier à importer (lot L6, tâche T2 ; conception 21) : xlsx ou CSV, rien d'autre.

Le fichier n'est jamais écrit sur le disque par ce module ni exécuté : il est lu en mémoire, après des contrôles qui
précèdent toute analyse XML. Un xlsx est une archive zip de XML : nombre d'entrées, taille décompressée et taux de
compression sont bornés (archive piégée), les macros sont refusées (xlsm, `vbaProject.bin`), et le XML est lu par
openpyxl, qui passe par defusedxml quand il est installé (aucune entité externe). Les anciens .xls sont refusés.

Les valeurs sont rendues **telles quelles**, avec leur type (texte, entier, décimal, booléen, date en texte ISO) : la
normalisation est l'affaire du profil de correspondance (T3). Ce qui pourrait tromper est signalé, jamais corrigé :
cellule de texte au format date, formule, cellules fusionnées, en-tête vide ou en double, feuille masquée.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from biblyo.core.errors import BusinessError
from biblyo.core.i18n import _

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024
MAX_ENTRIES = 2_000
MAX_RATIO = 150  # taille décompressée / taille compressée, entrée par entrée
MAX_ROWS = 200_000
MAX_COLUMNS = 200
MAX_CELLS = 3_000_000
HEADER_SEARCH_ROWS = 20
#: Premiers caractères qui font d'une cellule une formule dans un tableur (injection de formules, OWASP).
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

Value = str | int | float | bool | None

_DATE_FORMAT = re.compile(r"(?<![\\\"])(d{1,4}|m{3,5}|y{2,4}|yy)", re.IGNORECASE)
_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


class ReadError(BusinessError):
    """Fichier refusé avant toute importation, avec une phrase pour la bibliothécaire."""


@dataclass(frozen=True)
class Column:
    index: int  # 0 pour la première colonne
    letter: str  # « A »
    header: str  # en-tête exact, espaces compris
    label: str  # en-tête affichable : « Colonne C » quand il est vide


@dataclass(frozen=True)
class Row:
    number: int  # numéro de ligne dans le fichier (les en-têtes sur la ligne 1 : première donnée en 2)
    values: tuple[Value, ...]

    def get(self, column: Column) -> Value:
        return self.values[column.index] if column.index < len(self.values) else None


@dataclass(frozen=True)
class Notice:
    """Ce qui mérite d'être signalé dans une feuille, sans rien changer à la lecture."""

    code: str  # date_format, formula, merged, empty_header, duplicate_header, hidden, trailing_empty
    message: str
    cells: tuple[str, ...] = ()  # « I458 », au plus quelques-unes


@dataclass(frozen=True)
class Sheet:
    name: str
    hidden: bool
    header_row: int
    columns: tuple[Column, ...]
    rows: tuple[Row, ...]
    notices: tuple[Notice, ...] = ()
    trailing_empty_rows: int = 0
    date_text_cells: frozenset[str] = frozenset()  # toutes les cellules de texte au format date (« I458 »)
    formula_like_cells: frozenset[str] = frozenset()  # CSV : texte qui commence comme une formule (« =… »)


@dataclass(frozen=True)
class Workbook:
    filename: str
    kind: str  # xlsx, csv
    sha256: str
    size: int
    sheets: tuple[Sheet, ...]
    notices: tuple[Notice, ...] = field(default_factory=tuple)

    def sheet(self, name: str) -> Sheet | None:
        return next((s for s in self.sheets if s.name == name), None)


def column_letter(index: int) -> str:
    letters = ""
    number = index + 1
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def read(data: bytes, filename: str, *, max_bytes: int = MAX_FILE_BYTES, max_cells: int = MAX_CELLS) -> Workbook:
    """Lit un xlsx ou un CSV, ou refuse avec un message clair (ReadError)."""
    if not data:
        raise ReadError(_("Le fichier est vide."))
    if len(data) > max_bytes:
        raise ReadError(_("Le fichier dépasse {n} Mio.").format(n=max_bytes // (1024 * 1024)))
    digest = hashlib.sha256(data).hexdigest()
    name = filename.strip() or "fichier"
    if data.startswith(_XLS_MAGIC):
        raise ReadError(_("Ancien format Excel (.xls) : ouvrez-le et enregistrez-le en .xlsx, puis recommencez."))
    if data.startswith(_ZIP_MAGIC):
        sheets, notices = _read_xlsx(data, max_cells=max_cells)
        return Workbook(name, "xlsx", digest, len(data), sheets, notices)
    if name.lower().endswith((".csv", ".txt")):
        return Workbook(name, "csv", digest, len(data), (_read_csv(data, max_cells=max_cells),))
    raise ReadError(_("Ce fichier n'est ni un classeur .xlsx ni un fichier .csv."))


# --- xlsx ------------------------------------------------------------------------------------------------------------
def _check_archive(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ReadError(_("Le classeur est abîmé : il ne s'ouvre pas.")) from exc
    if len(entries) > MAX_ENTRIES:
        raise ReadError(_("Le classeur contient trop d'éléments pour être un catalogue."))
    total = 0
    for entry in entries:
        total += entry.file_size
        if entry.compress_size and entry.file_size / entry.compress_size > MAX_RATIO and entry.file_size > 1_000_000:
            raise ReadError(_("Le classeur est anormalement compressé : il est refusé par précaution."))
        if entry.flag_bits & 0x1:
            raise ReadError(_("Le classeur est protégé par un mot de passe : enregistrez-le sans protection."))
    if total > MAX_UNCOMPRESSED_BYTES:
        raise ReadError(_("Le classeur est trop volumineux une fois décompressé."))
    names = {entry.filename for entry in entries}
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        raise ReadError(_("Le classeur contient des macros : enregistrez-le en .xlsx sans macros, puis recommencez."))
    if "xl/workbook.xml" not in names or "[Content_Types].xml" not in names:
        raise ReadError(_("Ce fichier n'est pas un classeur .xlsx."))
    return archive


def _is_date_format(number_format: str | None) -> bool:
    if not number_format or number_format in ("General", "@"):
        return False
    stripped = re.sub(r"\[[^\]]*\]|\"[^\"]*\"", "", number_format)
    return bool(_DATE_FORMAT.search(stripped))


def _value(raw: object) -> Value:
    if raw is None or isinstance(raw, (str, bool, int, float)):
        return raw
    if isinstance(raw, dt.datetime):
        return raw.isoformat(sep=" ") if raw.time() != dt.time() else raw.date().isoformat()
    if isinstance(raw, (dt.date, dt.time)):
        return raw.isoformat()
    if isinstance(raw, dt.timedelta):
        return str(raw)
    return str(raw)


def _empty(value: Value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _read_xlsx(data: bytes, *, max_cells: int) -> tuple[tuple[Sheet, ...], tuple[Notice, ...]]:
    import openpyxl
    from openpyxl.xml import DEFUSEDXML

    if not DEFUSEDXML:  # pragma: no cover : defusedxml est une dépendance de biblyO
        raise ReadError(_("Lecture des classeurs indisponible : composant de sécurité absent."))
    archive = _check_archive(data)
    try:
        values = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        formulas = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    except Exception as exc:
        raise ReadError(_("Le classeur est abîmé ou d'un format inattendu : il ne se lit pas.")) from exc
    sheets: list[Sheet] = []
    budget = max_cells
    try:
        paths = _sheet_paths(archive)
        for name in values.sheetnames:
            sheet, used = _read_sheet(values[name], formulas[name], archive, paths.get(name), budget)
            budget -= used
            sheets.append(sheet)
    except ReadError:
        raise
    except Exception as exc:
        raise ReadError(_("Le classeur est abîmé ou d'un format inattendu : il ne se lit pas.")) from exc
    finally:
        values.close()
        formulas.close()
    if not sheets:
        raise ReadError(_("Le classeur ne contient aucune feuille."))
    return tuple(sheets), ()


def _sheet_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    """Nom de feuille → chemin de son XML, pour compter les cellules fusionnées sans tout charger."""
    from defusedxml import ElementTree

    main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ElementTree.ParseError):
        return {}
    targets = {r.get("Id"): r.get("Target", "") for r in relations.iter(f"{package}Relationship")}
    paths: dict[str, str] = {}
    for sheet in workbook.iter(f"{main}sheet"):
        target = targets.get(sheet.get(f"{rel}id"), "")
        if target:
            paths[sheet.get("name", "")] = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    return paths


def _merged(archive: zipfile.ZipFile, path: str | None) -> tuple[str, ...]:
    if not path:
        return ()
    try:
        xml = archive.read(path)
    except KeyError:
        return ()
    return tuple(m.decode() for m in re.findall(rb'<mergeCell ref="([A-Z]+[0-9]+:[A-Z]+[0-9]+)"', xml)[:20])


def _read_sheet(ws: Any, wf: Any, archive: zipfile.ZipFile, path: str | None, budget: int) -> tuple[Sheet, int]:
    rows_raw: list[tuple[int, tuple[Value, ...], tuple[str, ...]]] = []
    used = 0
    for row in ws.iter_rows():
        cells = tuple(c for c in row if getattr(c, "column", None) is not None)
        if not cells:
            continue
        number = cells[0].row
        if number > MAX_ROWS + HEADER_SEARCH_ROWS:
            raise ReadError(_("La feuille « {name} » dépasse {n} lignes.").format(name=ws.title, n=MAX_ROWS))
        width = max(c.column for c in cells)
        if width > MAX_COLUMNS:
            raise ReadError(_("La feuille « {name} » dépasse {n} colonnes.").format(name=ws.title, n=MAX_COLUMNS))
        line: list[Value] = [None] * width
        formats = [""] * width
        for cell in cells:
            line[cell.column - 1] = _value(cell.value)
            formats[cell.column - 1] = cell.number_format or ""
        used += width
        if used > budget:
            raise ReadError(_("Le classeur contient trop de cellules pour être importé en une fois."))
        rows_raw.append((number, tuple(line), tuple(formats)))
    formula_cells = [
        f"{column_letter(c.column - 1)}{c.row}"
        for row in wf.iter_rows()
        for c in row
        if getattr(c, "data_type", None) == "f" or (isinstance(c.value, str) and c.value.startswith("="))
    ]
    header_at = _header_index(rows_raw)
    if header_at is None:
        return Sheet(ws.title, ws.sheet_state != "visible", 0, (), ()), used
    header_number, header_values, _formats = rows_raw[header_at]
    width = max(len(values) for _n, values, _f in rows_raw[header_at:])
    columns = []
    notices: list[Notice] = []
    seen: dict[str, str] = {}
    for index in range(width):
        raw = header_values[index] if index < len(header_values) else None
        header = "" if raw is None else str(raw)
        letter = column_letter(index)
        label = header.strip() or _("Colonne {letter}").format(letter=letter)
        if not header.strip():
            notices.append(Notice("empty_header", _("Colonne {letter} sans en-tête.").format(letter=letter)))
        elif header.strip().casefold() in seen:
            notices.append(
                Notice(
                    "duplicate_header",
                    _("En-tête « {h} » en double (colonnes {a} et {b}).").format(
                        h=header.strip(), a=seen[header.strip().casefold()], b=letter
                    ),
                )
            )
        else:
            seen[header.strip().casefold()] = letter
        columns.append(Column(index, letter, header, label))
    data = [(n, v, f) for n, v, f in rows_raw[header_at + 1 :]]
    trailing = 0
    while data and all(_empty(v) for v in data[-1][1]):
        data.pop()
        trailing += 1
    # Les lignes vides absentes du XML (jamais écrites) ne comptent pas : seules les lignes présentes sont rendues.
    date_text = [
        f"{column_letter(i)}{n}"
        for n, values, formats in data
        for i, (value, number_format) in enumerate(zip(values, formats, strict=False))
        if isinstance(value, str) and _is_date_format(number_format)
    ]
    if date_text:
        notices.append(
            Notice(
                "date_format",
                _(
                    "{n} cellule(s) de texte au format date : Excel pourrait les convertir si elles sont ressaisies."
                ).format(n=len(date_text)),
                tuple(date_text[:10]),
            )
        )
    if formula_cells:
        notices.append(
            Notice(
                "formula",
                _("{n} formule(s) : la valeur calculée enregistrée dans le fichier est lue.").format(
                    n=len(formula_cells)
                ),
                tuple(formula_cells[:10]),
            )
        )
    merged = _merged(archive, path)
    if merged:
        notices.append(Notice("merged", _("Cellules fusionnées : seule la première porte la valeur."), merged))
    if ws.sheet_state != "visible":
        notices.append(Notice("hidden", _("Feuille masquée dans Excel.")))
    if trailing:
        notices.append(
            Notice("trailing_empty", _("{n} ligne(s) vide(s) en fin de feuille, ignorée(s).").format(n=trailing))
        )
    rows = tuple(Row(n, v + (None,) * (width - len(v))) for n, v, _f in data)
    return Sheet(
        ws.title,
        ws.sheet_state != "visible",
        header_number,
        tuple(columns),
        rows,
        tuple(notices),
        trailing,
        date_text_cells=frozenset(date_text),
    ), used


def _header_index(rows: list[tuple[int, tuple[Value, ...], tuple[str, ...]]]) -> int | None:
    """La ligne d'en-têtes : la première, parmi les vingt premières, dont toutes les cellules remplies sont du texte et
    qui en a au moins deux ; à défaut, la première ligne non vide."""
    first: int | None = None
    for position, (_number, values, _formats) in enumerate(rows[:HEADER_SEARCH_ROWS]):
        filled = [v for v in values if not _empty(v)]
        if not filled:
            continue
        if first is None:
            first = position
        if len(filled) >= 2 and all(isinstance(v, str) for v in filled):
            return position
    return first


# --- CSV -------------------------------------------------------------------------------------------------------------
def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ReadError(_("Encodage du fichier CSV non reconnu : enregistrez-le en UTF-8."))  # pragma: no cover


def _read_csv(data: bytes, *, max_cells: int) -> Sheet:
    text = _decode(data)
    sample = text[:20_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    rows_raw: list[tuple[int, tuple[Value, ...], tuple[str, ...]]] = []
    used = 0
    for number, line in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), start=1):
        if len(line) > MAX_COLUMNS:
            raise ReadError(_("Le fichier dépasse {n} colonnes.").format(n=MAX_COLUMNS))
        if number > MAX_ROWS + HEADER_SEARCH_ROWS:
            raise ReadError(_("Le fichier dépasse {n} lignes.").format(n=MAX_ROWS))
        used += len(line)
        if used > max_cells:
            raise ReadError(_("Le fichier contient trop de cellules pour être importé en une fois."))
        values: tuple[Value, ...] = tuple(cell if cell != "" else None for cell in line)
        rows_raw.append((number, values, ("",) * len(line)))
    header_at = _header_index(rows_raw)
    if header_at is None:
        raise ReadError(_("Le fichier ne contient aucune ligne."))
    header_number, header_values, _formats = rows_raw[header_at]
    width = max(len(v) for _n, v, _f in rows_raw[header_at:])
    columns = tuple(
        Column(
            i,
            column_letter(i),
            str(header_values[i] or "") if i < len(header_values) else "",
            (str(header_values[i] or "").strip() if i < len(header_values) else "")
            or _("Colonne {letter}").format(letter=column_letter(i)),
        )
        for i in range(width)
    )
    data_rows = rows_raw[header_at + 1 :]
    trailing = 0
    while data_rows and all(_empty(v) for v in data_rows[-1][1]):
        data_rows.pop()
        trailing += 1
    rows = tuple(Row(n, v + (None,) * (width - len(v))) for n, v, _f in data_rows)
    notices: list[Notice] = []
    if trailing:
        notices.append(
            Notice("trailing_empty", _("{n} ligne(s) vide(s) en fin de fichier, ignorée(s).").format(n=trailing))
        )
    formula_like = [
        f"{column_letter(i)}{row.number}"
        for row in rows
        for i, value in enumerate(row.values)
        if isinstance(value, str) and value.startswith(FORMULA_TRIGGERS)
    ]
    if formula_like:
        notices.append(
            Notice(
                "formula_like",
                _(
                    "{n} cellule(s) commencent par =, +, - ou @ : lues comme du texte, jamais calculées ; "
                    "neutralisées dans les rapports exportés."
                ).format(n=len(formula_like)),
                tuple(formula_like[:10]),
            )
        )
    return Sheet(
        "CSV",
        False,
        header_number,
        columns,
        rows,
        tuple(notices),
        trailing,
        formula_like_cells=frozenset(formula_like),
    )
