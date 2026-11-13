# SPDX-License-Identifier: EUPL-1.2
"""Simulation intégrale d'un import, sans aucune écriture (lot L6, tâche T4 ; conception 21).

La simulation lit une feuille et, si on le lui donne, un fichier complémentaire (une version enrichie du même
classeur), applique le profil de correspondance et rend, **ligne par ligne**, ce que l'import créerait : séries,
œuvres, éditions (les « livres »), exemplaires, contributeurs, éditeurs, propositions, et tout ce qui mérite un
regard humain. Elle ne touche ni à la base ni au disque : le catalogue existant n'est lu que pour repérer les
conflits et les correspondances probables (`Existing`).

Règles validées le 13 novembre 2026, appliquées ici sans réglage :

- une ligne est un exemplaire ; son identité est l'identifiant de source (empreinte du fichier, feuille, ligne) et
  son numéro d'inventaire, gardé tel quel ; aucun exemplaire n'est fusionné, supprimé ni renuméroté ;
- deux lignes qui décrivent le même livre (série, tome, titre de l'album, éditeur, comparés sans casse ni accents
  ni article) deviennent deux exemplaires d'une même édition ; si leurs propositions se contredisent (deux ISBN
  différents), les éditions restent distinctes jusqu'à vérification ;
- la cote n'est jamais une clé : une cote portée par plusieurs livres est signalée, rien de plus ;
- le titre, les noms et les éditeurs restent tels qu'écrits dans la ligne source et reviennent à l'export ; un titre
  à article inversé s'affiche dans l'ordre naturel (D-L6-11) ; un nom inversé, des noms multiples ou une
  organisation deviennent des propositions (D-L6-13) ;
- le fichier complémentaire ne fait pas foi : ses écarts sont des différences de version, ses ajouts des
  propositions.

Le plan a une empreinte (`plan_sha256`) : même fichier, même profil, même état du catalogue donnent la même
empreinte. L'application (T5) exigera cette empreinte exacte.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from biblyo.core.i18n import _
from biblyo.core.isbn import clean as isbn_clean
from biblyo.core.isbn import parse_isbn
from biblyo.core.text import normalize, title_sort
from biblyo.modules.imports.guard import is_artificial_code, legacy_reconstruction
from biblyo.modules.imports.profile import (
    COLLECTION_KINDS,
    CONDITIONS,
    FIELD_BY_CODE,
    KEEP,
    LEVELS,
    RULE_STATUS,
    Profile,
    display,
)
from biblyo.modules.imports.reader import Column, Row, Sheet, Value, Workbook

SIMULATION_VERSION = 1

#: Codes des signalements : gravité, libellé, famille du rapport.
CODES: dict[str, tuple[str, str, str]] = {
    "inventory_missing": ("error", _("Numéro d'inventaire absent"), "identity"),
    "inventory_duplicate": ("error", _("Numéro d'inventaire en double dans le fichier"), "identity"),
    "inventory_exists": ("error", _("Numéro d'inventaire déjà présent dans biblyO"), "identity"),
    "barcode_duplicate": ("error", _("Code-barres en double dans le fichier"), "identity"),
    "barcode_exists": ("error", _("Code-barres déjà présent dans biblyO"), "identity"),
    "barcode_artificial": (
        "error",
        _("Code artificiel de la reconstruction de l’ancienne application : jamais repris"),
        "identity",
    ),
    "barcode_deferred": ("info", _("Pas de code-barres : à attribuer à l'étiquetage (D-L6-7)"), "identity"),
    "inventory_text": ("info", _("Numéro d'inventaire non numérique, gardé tel quel"), "identity"),
    "inverted_article": (
        "info",
        _("Titre à article inversé : affiché dans l'ordre naturel, forme d'origine gardée et exportée"),
        "title",
    ),
    "title_complement": (
        "warning",
        _("Titre terminé par un complément entre parenthèses : gardé tel quel, forme de recherche proposée"),
        "title",
    ),
    "series_variants": ("info", _("Même série écrite de plusieurs façons"), "title"),
    "no_album_title": ("warning", _("Pas de titre d'album : titre construit, à remplacer"), "title"),
    "standalone": ("info", _("Album isolé : ni tome ni titre d'album, pas de série"), "title"),
    "spaces": ("info", _("Espaces en trop : retirés pour comparer, valeur brute gardée"), "format"),
    "date_format_cell": ("warning", _("Cellule de texte au format date dans Excel"), "format"),
    "formula_like": ("warning", _("Cellule qui commence comme une formule"), "format"),
    "atypical_volume": ("warning", _("Tome atypique : valeur gardée, aucun numéro choisi"), "interpretation"),
    "isbn_invalid": ("warning", _("ISBN à la clé de contrôle fausse"), "interpretation"),
    "year_invalid": ("warning", _("Année illisible"), "interpretation"),
    "condition_unmapped": ("warning", _("État non rangé dans l'échelle de biblyO"), "interpretation"),
    "name_inverted": ("warning", _("Nom inversé (« Nom, Prénom ») : forme à l'endroit proposée"), "names"),
    "name_multiple": ("warning", _("Plusieurs noms dans une cellule : séparation proposée"), "names"),
    "name_organization": ("warning", _("Nom d'organisation probable"), "names"),
    "name_variants": ("info", _("Même nom écrit de plusieurs façons : jamais fusionné d'office"), "names"),
    "publisher_variants": ("info", _("Même éditeur écrit de plusieurs façons"), "publishers"),
    "publisher_close": ("warning", _("Éditeurs aux libellés voisins : peut-être le même"), "publishers"),
    "publisher_mention": ("warning", _("Libellé d'éditeur qui ressemble à une mention d'édition"), "publishers"),
    "collection_reclassified": ("info", _("Collection rangée en mention ou en note (table du profil)"), "edition"),
    "collection_linked": ("info", _("Collection rattachée à une collection existante (choix manuel)"), "edition"),
    "collection_variants": (
        "warning",
        _("Collections écrites autrement, confondues par la comparaison : choisir la forme retenue"),
        "edition",
    ),
    "collection_canonical": (
        "info",
        _("Collection rapprochée sous la forme retenue dans le profil : forme d'origine gardée dans la ligne"),
        "edition",
    ),
    "multiple_copies": ("info", _("Livre en plusieurs exemplaires"), "grouping"),
    "edition_conflict": ("warning", _("Propositions contradictoires : éditions gardées distinctes"), "grouping"),
    "book_collection_differs": (
        "warning",
        _("Collection renseignée autrement selon les exemplaires d'un même livre : la première est retenue pour "
          "l'édition, chaque valeur reste dans sa ligne"),
        "grouping",
    ),
    "same_volume_other_title": ("warning", _("Même tome d'une série porté par des titres différents"), "grouping"),
    "shared_call_number": ("warning", _("Cote portée par plusieurs livres différents"), "call_numbers"),
    "call_volume_mismatch": ("warning", _("Le numéro de la cote contredit le tome"), "call_numbers"),
    "missing_values": ("warning", _("Ligne incomplète"), "identity"),
    "version_difference": ("warning", _("Le fichier complémentaire diffère du fichier principal"), "version"),
    "complement_absent": ("info", _("Ligne absente du fichier complémentaire"), "version"),
    "source_mismatch": ("warning", _("Feuille des sources incohérente avec la ligne"), "version"),
    "probable_match": ("warning", _("Correspondance probable avec une notice existante"), "catalogue"),
    "proposal": ("info", _("Proposition à vérifier"), "proposals"),
}
FAMILIES: dict[str, str] = {
    "identity": _("Identité des exemplaires"),
    "title": _("Titres et séries"),
    "format": _("Forme des cellules"),
    "interpretation": _("Données impossibles à interpréter automatiquement"),
    "names": _("Auteurs et contributeurs"),
    "publishers": _("Éditeurs"),
    "edition": _("Collections"),
    "grouping": _("Regroupement en livres"),
    "call_numbers": _("Cotes"),
    "version": _("Différences entre fichiers"),
    "catalogue": _("Catalogue existant"),
    "proposals": _("Propositions"),
}
SEVERITY_ORDER = {"ok": 0, "info": 1, "warning": 2, "error": 3}
PROPOSAL_KINDS: dict[str, str] = {
    "isbn": _("ISBN à vérifier"),
    "publication_year": _("Année à vérifier"),
    "contributor": _("Contributeur"),
    "publisher": _("Éditeur"),
    "name_form": _("Forme du nom"),
    "name_split": _("Séparation des noms"),
    "organization": _("Organisation"),
    "volume": _("Tome"),
    "search_form": _("Forme de recherche"),
    "condition": _("État à vérifier"),
    "location": _("Emplacement indiqué"),
    "other": _("Autre valeur"),
}

_INVERTED = re.compile(r"^(?P<base>.*\S)\s*\((?P<article>le|la|les|l'|l’|un|une|des|the)\)\s*$", re.IGNORECASE)
_COMPLEMENT = re.compile(r"^(?P<base>[^()]*\S)\s*\((?P<article>[^()]*\S)\s*\)\s*$")  # « Spirou (Le petit) »
_INVERTED_NAME = re.compile(r"^(?P<last>[^,]+),\s*(?P<first>[^,]+)$")
_MULTIPLE_NAMES = re.compile(r"\s+(?:&|et|and)\s+|\s*;\s*|\s+/\s+", re.IGNORECASE)
_ACRONYM = re.compile(r"^(?:[A-Z]\.){2,}[A-Z]?\.?$")
_ORGANIZATION_WORDS = ("studio", "studios", "atelier", "collectif", "editions", "association", "club", "sarl", "sa")
_CALL_NUMBER = re.compile(r"^[^/]+/0*(\d+)\s*$")  # « JAC/04 » ; pas « DAR/1/2 »
_YEAR = re.compile(r"^\s*(\d{4})\s*$")
_MENTION_PUBLISHER = ("album double", "double album", "integrale", "hors serie")
_ROW_FIELDS = ("series", "volume", "title", "publisher", "condition", "acquisition", "call_number", "inventory")


# --- Résultats -------------------------------------------------------------------------------------------------------
@dataclass
class Message:
    code: str
    text: str = ""
    column: str = ""  # lettre de la colonne concernée, s'il y en a une

    @property
    def severity(self) -> str:
        return CODES[self.code][0]

    @property
    def label(self) -> str:
        return CODES[self.code][1]


@dataclass
class Proposal:
    kind: str
    value: str
    column: str = ""
    origin: str = ""  # « fichier principal », nom du fichier complémentaire, « règle de biblyO »
    url: str = ""
    note: str = ""


@dataclass
class Difference:
    row: int
    inventory: str
    column: str  # libellé exact
    letter: str
    main: str
    complement: str
    complement_row: int
    origin: str


@dataclass
class Contributor:
    written: str  # tel qu'écrit, espaces de bord retirés
    role: str  # author, other
    position: int  # rang de la colonne (0 pour la première colonne de contributeur)
    to_specify: bool  # rôle à préciser


@dataclass
class RowPlan:
    number: int
    source_id: str
    inventory: str
    raw: dict[str, Value]
    fields: dict[str, Any] = field(default_factory=dict)
    series: str = ""
    work: str = ""
    book: str = ""
    copy: str = ""
    action: str = "create"  # create, error
    messages: list[Message] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    complement_row: int = 0  # ligne correspondante du fichier complémentaire, s'il y en a une
    complement_raw: dict[str, Value] = field(default_factory=dict)  # ses valeurs, telles que lues

    @property
    def severity(self) -> str:
        worst = "ok"
        for message in self.messages:
            if SEVERITY_ORDER[message.severity] > SEVERITY_ORDER[worst]:
                worst = message.severity
        return worst

    def add(self, code: str, text: str = "", column: str = "") -> None:
        if not any(m.code == code and m.text == text and m.column == column for m in self.messages):
            self.messages.append(Message(code, text, column))


@dataclass
class SeriesPlan:
    ref: str
    title: str  # première forme rencontrée, exacte
    key: str
    search_form: str  # forme naturelle si l'article est inversé, sinon vide
    forms: dict[str, int] = field(default_factory=dict)
    rows: list[int] = field(default_factory=list)
    works: list[str] = field(default_factory=list)
    call_prefixes: dict[str, int] = field(default_factory=dict)
    existing_id: int | None = None


@dataclass
class WorkPlan:
    ref: str
    series: str
    volume: str  # libellé tel qu'affiché
    volume_number: float | None
    title: str
    title_built: bool
    key: tuple[str, str, str]
    contributors: list[Contributor] = field(default_factory=list)
    books: list[str] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    existing_id: int | None = None


@dataclass
class BookPlan:
    ref: str
    work: str
    publisher: str
    publisher_key: str
    collection: str
    collection_kind: str
    rows: list[int] = field(default_factory=list)
    copies: list[str] = field(default_factory=list)
    call_numbers: list[str] = field(default_factory=list)
    isbn_proposed: list[str] = field(default_factory=list)
    year_proposed: list[str] = field(default_factory=list)
    existing_id: int | None = None
    collection_link: int | None = None
    collection_label: str = ""  # nom de la collection créée : forme retenue, sinon la valeur d'origine


@dataclass
class AgentPlan:
    written: str
    key: str
    rows: list[int] = field(default_factory=list)
    kind: str = "person"
    existing_id: int | None = None


@dataclass
class PublisherPlan:
    key: str
    forms: dict[str, int] = field(default_factory=dict)
    rows: list[int] = field(default_factory=list)
    existing_id: int | None = None
    usage_form: str = ""

    @property
    def label(self) -> str:
        return max(self.forms.items(), key=lambda item: (item[1], item[0]))[0] if self.forms else ""


@dataclass
class ColumnReport:
    letter: str
    header: str
    target: str
    field_label: str
    level: str
    filled: int
    kept_raw: int
    stored_as_is: int
    transformed: int
    proposals: int
    examples_transformed: list[tuple[int, str, str]] = field(default_factory=list)
    source: str = "main"  # main, complement


@dataclass
class Existing:
    """Ce que le catalogue contient déjà, lu sans rien écrire."""

    inventory: set[str] = field(default_factory=set)
    barcodes: set[str] = field(default_factory=set)
    series: dict[str, tuple[int, str]] = field(default_factory=dict)
    works: dict[tuple[str, str, str], int] = field(default_factory=dict)
    isbns: dict[str, int] = field(default_factory=dict)
    agents: dict[str, int] = field(default_factory=dict)  # forme exacte → id
    publishers: dict[str, int] = field(default_factory=dict)
    fingerprint: str = "vide"


@dataclass
class Simulation:
    filename: str
    file_sha256: str
    sheet: str
    header_row: int
    complement_filename: str
    complement_sha256: str
    complement_sheet: str
    sources_sheet: str
    profile: Profile
    rows: list[RowPlan]
    series: dict[str, SeriesPlan]
    works: dict[str, WorkPlan]
    books: dict[str, BookPlan]
    agents: dict[str, AgentPlan]
    publishers: dict[str, PublisherPlan]
    shared_call_numbers: dict[str, list[str]]
    differences: list[Difference]
    columns: list[ColumnReport]
    sheet_notices: list[tuple[str, str, tuple[str, ...]]]
    totals: dict[str, Any]
    checks: list[tuple[str, bool, str]]
    blockers: list[str]
    existing_fingerprint: str
    plan_sha256: str = ""
    #: Colonnes lues, dans l'ordre du fichier : (indice, lettre, en-tête exact, libellé). L'export les restitue.
    sheet_columns: list[tuple[int, str, str, str]] = field(default_factory=list)
    complement_columns: list[tuple[int, str, str, str]] = field(default_factory=list)
    complement_header_row: int = 0

    def row(self, number: int) -> RowPlan | None:
        return next((r for r in self.rows if r.number == number), None)

    def anomalies(self) -> dict[str, list[RowPlan]]:
        """Code → lignes concernées, dans l'ordre du fichier."""
        found: dict[str, list[RowPlan]] = defaultdict(list)
        for row in self.rows:
            for code in dict.fromkeys(m.code for m in row.messages):
                found[code].append(row)
        return dict(found)


# --- Formes ----------------------------------------------------------------------------------------------------------
def natural_title(title: str) -> str:
    """« Schtroumpfs (Les) » donne « Les Schtroumpfs » ; « agent 212 (L') » donne « L'agent 212 »."""
    match = _INVERTED.match(title)
    if not match:
        return title
    article = match.group("article")
    joiner = "" if article.endswith(("'", "’")) else " "
    return f"{article}{joiner}{match.group('base')}"


def comparison_key(title: str) -> str:
    """Clé de comparaison d'un titre : sans casse, accents, ponctuation ni article, quelle que soit sa place."""
    return title_sort(natural_title(" ".join(title.split())))


def name_proposals(written: str) -> list[tuple[str, str, str]]:
    """Propositions pour un nom tel qu'écrit : (code de signalement, sorte de proposition, valeur proposée)."""
    found: list[tuple[str, str, str]] = []
    parts = [p.strip() for p in _MULTIPLE_NAMES.split(written) if p.strip()]
    if len(parts) > 1:
        found.append(("name_multiple", "name_split", " | ".join(parts)))
    match = _INVERTED_NAME.match(written)
    if match and len(parts) == 1:
        found.append(("name_inverted", "name_form", f"{match.group('first').strip()} {match.group('last').strip()}"))
    words = normalize(written).split()
    if _ACRONYM.match(written.replace(" ", "")) or (words and words[0] in _ORGANIZATION_WORDS):
        found.append(("name_organization", "organization", written))
    return found


def usage_form(label: str) -> str:
    """Forme d'usage d'un libellé en capitales (D-L6-14, proposition) : « VENTS D'OUEST » → « Vents d'Ouest »."""
    if label != label.upper():
        return label
    small = {"de", "du", "des", "la", "le", "les", "et", "d", "l", "en", "aux", "au"}
    words = re.split(r"(\s+|-)", label.lower())
    out: list[str] = []
    for i, word in enumerate(words):
        if not word.strip() or word == "-":
            out.append(word)
            continue
        if "'" in word or "’" in word:
            sep = "'" if "'" in word else "’"
            head, tail = word.split(sep, 1)
            out.append(f"{head}{sep}{tail[:1].upper()}{tail[1:]}")
        elif i and word in small:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return "".join(out)


def _text(value: Value) -> str:
    return display(value).strip()


def _volume(value: Value) -> tuple[str, float | None, bool]:
    """(libellé affiché, numéro, atypique). Seul un entier donne un numéro : rien n'est choisi pour les autres."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "", None, False
    if isinstance(value, bool):
        return display(value), None, True
    if isinstance(value, int):
        return str(value), float(value), False
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value)), value, False
        return display(value), None, True
    raw = " ".join(str(value).split())
    if raw.isdigit():
        return raw, float(raw), False
    return raw, None, True


# --- Catalogue existant ----------------------------------------------------------------------------------------------
def snapshot(session: Session) -> Existing:
    """Lit ce qui peut entrer en conflit avec l'import, ou lui correspondre. Lecture seule."""
    existing = Existing()
    for inventory, barcode in session.execute(sql("SELECT inventory_number, barcode FROM copies")):
        if inventory:
            existing.inventory.add(str(inventory))
        if barcode:
            existing.barcodes.add(str(barcode))
    series_rows = session.execute(sql("SELECT id, title FROM series")).all()
    by_id = {}
    for series_id, title in series_rows:
        key = comparison_key(title)
        existing.series.setdefault(key, (series_id, title))
        by_id[series_id] = key
    for work_id, title, series_id, number, label in session.execute(
        sql(
            "SELECT w.id, w.title, ws.series_id, ws.volume_number, ws.volume_label FROM works w "
            "LEFT JOIN work_series ws ON ws.work_id = w.id AND ws.is_primary = 1 WHERE w.status = 'active'"
        )
    ):
        volume = label or (display(number) if number is not None else "")
        work_key = (by_id.get(series_id, "") if series_id else "", normalize(volume), comparison_key(title))
        existing.works.setdefault(work_key, work_id)
    for edition_id, value in session.execute(
        sql("SELECT edition_id, value FROM edition_identifiers WHERE scheme = 'isbn13'")
    ):
        existing.isbns.setdefault(str(value), edition_id)
    for agent_id, name in session.execute(sql("SELECT id, display_name FROM agents")):
        existing.agents.setdefault(" ".join(str(name).split()), agent_id)
    for publisher_id, name_norm in session.execute(sql("SELECT id, name_norm FROM publishers")):
        existing.publishers.setdefault(str(name_norm), publisher_id)
    counts = session.execute(
        sql(
            "SELECT (SELECT COUNT(*) FROM copies), (SELECT COALESCE(MAX(id), 0) FROM copies), "
            "(SELECT COUNT(*) FROM works), (SELECT COALESCE(MAX(id), 0) FROM works), "
            "(SELECT COUNT(*) FROM series), (SELECT COUNT(*) FROM agents), (SELECT COUNT(*) FROM publishers), "
            "(SELECT COUNT(*) FROM edition_identifiers)"
        )
    ).one()
    existing.fingerprint = "-".join(str(c) for c in counts)
    return existing


# --- Fichier complémentaire et feuille des sources -------------------------------------------------------------------
@dataclass
class _SourceLine:
    row: int
    line: str
    isbn: str
    year: str
    url: str
    note: str


def _find_sources(workbooks: list[tuple[Workbook, str]]) -> tuple[str, dict[str, _SourceLine]]:
    """Feuille des sources d'une version enrichie : en-têtes « inventaire » et « URL » ou « source »."""
    for workbook, data_sheet in workbooks:
        for sheet in workbook.sheets:
            if sheet.name == data_sheet:
                continue
            keys = {normalize(c.label): c for c in sheet.columns}

            def pick(*words: str, sheet_keys: dict[str, Column] = keys) -> Column | None:
                return next((c for k, c in sheet_keys.items() if all(w in k for w in words)), None)

            inventory = pick("inventaire")
            url = pick("url") or pick("source", "adresse")
            if inventory is None or url is None:
                continue
            line, isbn, year = pick("ligne"), pick("isbn"), pick("annee")
            remark = pick("remarque") or pick("verification")
            found: dict[str, _SourceLine] = {}
            for row in sheet.rows:
                number = _inventory_text(row.get(inventory))
                if not number:
                    continue
                found[number] = _SourceLine(
                    row.number,
                    _text(row.get(line)) if line else "",
                    isbn_clean(_text(row.get(isbn))) if isbn else "",
                    _text(row.get(year)) if year else "",
                    _text(row.get(url)),
                    _text(row.get(remark)) if remark else "",
                )
            return f"{workbook.filename} › {sheet.name}", found
    return "", {}


def _inventory_text(value: Value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _text(value)


# --- Simulation ------------------------------------------------------------------------------------------------------
def simulate(
    workbook: Workbook,
    sheet_name: str,
    profile: Profile,
    *,
    complement: Workbook | None = None,
    complement_sheet: str = "",
    existing: Existing | None = None,
) -> Simulation:
    sheet = workbook.sheet(sheet_name)
    if sheet is None:
        raise ValueError(sheet_name)
    existing = existing or Existing()
    other = None
    if complement is not None:
        other = complement.sheet(complement_sheet) or (complement.sheets[0] if complement.sheets else None)
    by_field: dict[str, list[Column]] = defaultdict(list)
    for column in sheet.columns:
        by_field[profile.field_of(column)].append(column)

    def first(code: str) -> Column | None:
        return by_field[code][0] if by_field.get(code) else None

    prefix = workbook.sha256[:12]
    rows: list[RowPlan] = []
    for row in sheet.rows:
        raw = {column.label: row.get(column) for column in sheet.columns}
        rows.append(RowPlan(row.number, f"{prefix}:{sheet.name}:{row.number}", "", raw))
    by_number = {plan.number: plan for plan in rows}

    # 1. Lecture des champs, ligne par ligne.
    for row, plan in zip(sheet.rows, rows, strict=True):
        _read_fields(row, plan, sheet, profile, by_field, first)

    # 2. Identité des exemplaires.
    _identity(rows, existing, profile, first("barcode") is not None)

    # 3. Fichier complémentaire : différences de version et propositions.
    differences: list[Difference] = []
    complement_columns: list[ColumnReport] = []
    if other is not None and complement is not None:
        differences, complement_columns = _compare(sheet, other, complement, profile, rows, first)
    sources_label, sources = _find_sources(
        [(workbook, sheet.name)] + ([(complement, other.name)] if complement is not None and other else [])
    )
    if sources:
        _attach_sources(rows, sources, sources_label)

    # 4. Regroupement : séries, œuvres, livres, exemplaires.
    series, works, books = _group(rows, existing, profile)

    # 5. Contributeurs et éditeurs.
    agents = _agents(rows, works, existing)
    publishers = _publishers(rows, existing)

    # 6. Cotes.
    shared = _call_numbers(rows, books, series, by_number)

    # 7. Couverture des colonnes.
    columns = _coverage(sheet, rows, profile) + complement_columns
    notices = [(n.code, n.message, n.cells) for n in sheet.notices]
    if other is not None:
        notices += [
            (n.code, f"{complement.filename if complement else ''} : {n.message}", n.cells) for n in other.notices
        ]

    simulation = Simulation(
        filename=workbook.filename,
        file_sha256=workbook.sha256,
        sheet=sheet.name,
        header_row=sheet.header_row,
        complement_filename=complement.filename if complement else "",
        complement_sha256=complement.sha256 if complement else "",
        complement_sheet=other.name if other else "",
        sources_sheet=sources_label,
        profile=profile,
        rows=rows,
        series=series,
        works=works,
        books=books,
        agents=agents,
        publishers=publishers,
        shared_call_numbers=shared,
        differences=differences,
        columns=columns,
        sheet_notices=notices,
        totals={},
        checks=[],
        blockers=[
            _(
                "{file} ressemble à la reconstruction de l’ancienne application ({signs}) : jamais une source "
                "d'import (D-L6-1)."
            ).format(
                file=book.filename, signs=", ".join(signs[:4])
            )
            for book in [workbook] + ([complement] if complement is not None else [])
            if (signs := legacy_reconstruction(book))
        ],
        existing_fingerprint=existing.fingerprint,
        sheet_columns=[(c.index, c.letter, c.header, c.label) for c in sheet.columns],
        complement_columns=[(c.index, c.letter, c.header, c.label) for c in other.columns] if other else [],
        complement_header_row=other.header_row if other else 0,
    )
    _totals(simulation)
    simulation.plan_sha256 = plan_digest(simulation)
    return simulation


def _read_fields(
    row: Row,
    plan: RowPlan,
    sheet: Sheet,
    profile: Profile,
    by_field: dict[str, list[Column]],
    first: Any,
) -> None:
    fields: dict[str, Any] = {}
    for column in sheet.columns:
        value = row.get(column)
        cell = f"{column.letter}{row.number}"
        if cell in sheet.date_text_cells:
            plan.add(
                "date_format_cell",
                _("Cellule {cell} : texte « {v} » gardé tel quel.").format(cell=cell, v=_text(value)),
                column.letter,
            )
        if cell in sheet.formula_like_cells:
            plan.add("formula_like", _("Cellule {cell} gardée comme texte.").format(cell=cell), column.letter)
        if isinstance(value, str) and value and value != " ".join(value.split()) and value.strip():
            plan.add("spaces", column.label, column.letter)

    series_col, volume_col, title_col = first("series"), first("volume"), first("title")
    series_text = _text(row.get(series_col)) if series_col else ""
    volume_label, volume_number, atypical = _volume(row.get(volume_col)) if volume_col else ("", None, False)
    title_text = _text(row.get(title_col)) if title_col else ""
    if atypical and volume_col:
        plan.add("atypical_volume", _("Tome « {v} » gardé tel quel.").format(v=volume_label), volume_col.letter)
        plan.proposals.append(
            Proposal(
                "volume",
                volume_label,
                volume_col.letter,
                _("règle de biblyO"),
                note=_("Numéro à établir livre en main."),
            )
        )
    for text_value, column in ((series_text, series_col), (title_text, title_col)):
        if column and _INVERTED.match(text_value):
            plan.add(
                "inverted_article",
                _("« {t} » ; recherche aussi sous « {n} ».").format(t=text_value, n=natural_title(text_value)),
                column.letter,
            )
        elif column and (complement := _COMPLEMENT.match(text_value)):
            proposed = f"{complement.group('article')} {complement.group('base')}"
            plan.add("title_complement", f"« {text_value} » → « {proposed} »", column.letter)
            plan.proposals.append(
                Proposal("search_form", proposed, column.letter, _("règle de biblyO"), note=text_value)
            )
    fields.update(series=series_text, volume=volume_label, volume_number=volume_number, title=title_text)

    contributors: list[Contributor] = []
    for position, column in enumerate(by_field.get("contributor", [])):
        written = " ".join(_text(row.get(column)).split())
        if not written:
            continue
        author = profile.roles == "first_author" and position == 0
        contributors.append(Contributor(written, "author" if author else "other", position, not author))
        for code, kind, value in name_proposals(written):
            plan.add(code, f"« {written} » → « {value} »", column.letter)
            plan.proposals.append(Proposal(kind, value, column.letter, _("règle de biblyO"), note=written))
    fields["contributors"] = contributors

    publisher_col = first("publisher")
    fields["publisher"] = _text(row.get(publisher_col)) if publisher_col else ""
    collection_col = first("collection")
    collection = _text(row.get(collection_col)) if collection_col else ""
    kind = profile.collection_map.get(collection, "collection") if collection else ""
    linked = profile.collection_links.get(collection) if collection and kind == "collection" else None
    canonical = profile.collection_canonical.get(collection, "") if collection and kind == "collection" else ""
    fields.update(collection=collection, collection_kind=kind, collection_link=linked, collection_canonical=canonical)
    if collection and kind != "collection" and collection_col:
        plan.add("collection_reclassified", f"« {collection} » → {COLLECTION_KINDS[kind]}", collection_col.letter)
    if linked and collection_col:
        linked_text = _("« {v} » → collection n° {id}").format(v=collection, id=linked)
        plan.add("collection_linked", linked_text, collection_col.letter)

    condition_col = first("condition")
    condition_text = _text(row.get(condition_col)) if condition_col else ""
    condition = profile.condition_map.get(condition_text, "") if condition_text else ""
    fields.update(condition=condition, condition_note=condition_text)
    if condition_text and not condition and condition_col:
        plan.add("condition_unmapped", f"« {condition_text} »", condition_col.letter)
        plan.proposals.append(
            Proposal(
                "condition",
                condition_text,
                condition_col.letter,
                _("règle de biblyO"),
                note=_("Valeur d'origine sans état de biblyO : gardée en précision, état à établir livre en main."),
            )
        )

    acquisition_col = first("acquisition")
    fields["acquisition"] = _text(row.get(acquisition_col)) if acquisition_col else ""
    call_col = first("call_number")
    fields["call_number"] = _text(row.get(call_col)) if call_col else ""
    fields["copy_note"] = " ; ".join(t for c in by_field.get("copy_note", []) if (t := _text(row.get(c))))
    fields["kept"] = {c.label: display(row.get(c)) for c in by_field.get(KEEP, []) if row.get(c) is not None}

    isbn_col = first("isbn")
    isbn_text = _text(row.get(isbn_col)) if isbn_col else ""
    fields["isbn"] = ""
    if isbn_text and isbn_col:
        parsed = parse_isbn(isbn_text)
        if parsed is None:
            plan.add("isbn_invalid", f"« {isbn_text} »", isbn_col.letter)
        else:
            fields["isbn"] = parsed.isbn13
    year_col = first("year")
    year_text = _text(row.get(year_col)) if year_col else ""
    fields["year"] = ""
    if year_text and year_col:
        if _YEAR.match(year_text):
            fields["year"] = year_text.strip()
        else:
            plan.add("year_invalid", f"« {year_text} »", year_col.letter)
    for column in by_field.get("isbn_proposed", []):
        _propose_isbn(plan, _text(row.get(column)), column.letter, _("fichier principal"))
    for column in by_field.get("year_proposed", []):
        _propose_year(plan, _text(row.get(column)), column.letter, _("fichier principal"))
    for column in by_field.get("location_hint", []):
        if hint := _text(row.get(column)):
            plan.proposals.append(
                Proposal(
                    "location",
                    hint,
                    column.letter,
                    _("fichier principal"),
                    note=_("Place indiquée par le fichier, non vérifiée : l'exemplaire reste non rangé."),
                )
            )

    inventory_col = first("inventory")
    plan.inventory = _inventory_text(row.get(inventory_col)) if inventory_col else ""
    barcode_col = first("barcode")
    fields["barcode_given"] = _text(row.get(barcode_col)) if barcode_col else ""
    plan.fields = fields

    missing = [
        FIELD_BY_CODE[code].label
        for code, value in (
            ("contributor", contributors),
            ("publisher", fields["publisher"]),
            ("condition", condition_text),
            ("acquisition", fields["acquisition"]),
        )
        if by_field.get(code) and not value
    ]
    if len(missing) >= 2:
        plan.add("missing_values", ", ".join(missing))


def _propose_isbn(plan: RowPlan, value: str, letter: str, origin: str) -> None:
    if not value:
        return
    parsed = parse_isbn(value)
    if parsed is None:
        plan.add("isbn_invalid", f"« {value} »", letter)
    plan.proposals.append(Proposal("isbn", parsed.isbn13 if parsed else value, letter, origin))
    plan.add("proposal", _("ISBN {v}").format(v=value), letter)


def _propose_year(plan: RowPlan, value: str, letter: str, origin: str) -> None:
    if not value:
        return
    if not _YEAR.match(value):
        plan.add("year_invalid", f"« {value} »", letter)
    plan.proposals.append(Proposal("publication_year", value.strip(), letter, origin))
    plan.add("proposal", _("Année {v}").format(v=value), letter)


def _identity(rows: list[RowPlan], existing: Existing, profile: Profile, has_barcode_column: bool) -> None:
    counts = Counter(r.inventory for r in rows if r.inventory)
    for plan in rows:
        if not plan.inventory:
            plan.add("inventory_missing")
        elif counts[plan.inventory] > 1:
            plan.add("inventory_duplicate", plan.inventory)
        elif plan.inventory in existing.inventory:
            plan.add("inventory_exists", plan.inventory)
        elif not plan.inventory.isdigit():
            plan.add("inventory_text", plan.inventory)
    barcodes = Counter(r.fields.get("barcode_given") for r in rows if r.fields.get("barcode_given"))
    sequence = _barcode_start(existing, profile.barcode_prefix)
    for plan in rows:
        given = plan.fields.get("barcode_given", "")
        barcode = ""
        if profile.barcode == "column" and has_barcode_column:
            barcode = given
            if given and is_artificial_code(given):
                plan.add("barcode_artificial", given)
            elif given and barcodes[given] > 1:
                plan.add("barcode_duplicate", given)
            elif given and given in existing.barcodes:
                plan.add("barcode_exists", given)
        elif profile.barcode == "generate" and not any(m.severity == "error" for m in plan.messages):
            while True:
                candidate = f"{profile.barcode_prefix}{sequence:06d}"
                sequence += 1
                if candidate not in existing.barcodes and candidate not in barcodes:
                    barcode = candidate
                    break
        plan.fields["barcode"] = barcode
        if not barcode and not any(m.severity == "error" for m in plan.messages):
            plan.add("barcode_deferred")
        if any(m.severity == "error" for m in plan.messages):
            plan.action = "error"


def _barcode_start(existing: Existing, prefix: str) -> int:
    highest = 0
    for code in existing.barcodes:
        if code.startswith(prefix) and code[len(prefix) :].isdigit():
            highest = max(highest, int(code[len(prefix) :]))
    return highest + 1


def _compare(
    sheet: Sheet,
    other: Sheet,
    complement: Workbook,
    profile: Profile,
    rows: list[RowPlan],
    first: Any,
) -> tuple[list[Difference], list[ColumnReport]]:
    """Compare la version complémentaire, cellule par cellule, et en tire des propositions. Elle ne fait pas foi."""
    origin = complement.filename
    other_inventory = next((c for c in other.columns if profile.field_of(c) == "inventory"), None)
    main_inventory = first("inventory")
    index: dict[str, Row] = {}
    if other_inventory is not None:
        for row in other.rows:
            key = _inventory_text(row.get(other_inventory))
            if key:
                index[key] = row
    by_row = {row.number: row for row in other.rows}
    shared = [(c, next((o for o in other.columns if o.label == c.label), None)) for c in sheet.columns]
    extra = [c for c in other.columns if c.label not in {s.label for s in sheet.columns}]
    differences: list[Difference] = []
    extra_counts: Counter[str] = Counter()
    for plan in rows:
        match = index.get(plan.inventory) if main_inventory is not None and plan.inventory else None
        if match is None and main_inventory is None:
            match = by_row.get(plan.number)
        if match is None:
            plan.add("complement_absent", origin)
            continue
        plan.complement_row = match.number
        plan.complement_raw = {column.label: match.get(column) for column in other.columns}
        for column, twin in shared:
            if twin is None:
                continue
            main_value, other_value = display(plan.raw.get(column.label)), display(match.get(twin))
            if main_value == other_value:
                continue
            differences.append(
                Difference(
                    plan.number,
                    plan.inventory,
                    column.label,
                    column.letter,
                    main_value,
                    other_value,
                    match.number,
                    origin,
                )
            )
            plan.add("version_difference", f"{column.label} : « {main_value} » → « {other_value} »", column.letter)
            code = profile.field_of(column)
            kind = {"contributor": "contributor", "publisher": "publisher"}.get(code, "other")
            plan.proposals.append(
                Proposal(
                    kind,
                    other_value,
                    column.letter,
                    origin,
                    note=_("Absent du fichier principal")
                    if not main_value.strip()
                    else _("Valeur différente du fichier principal"),
                )
            )
        for column in extra:
            value = _text(match.get(column))
            if not value:
                continue
            extra_counts[column.label] += 1
            code = profile.field_of(column)
            if code == "isbn_proposed" or code == "isbn":
                _propose_isbn(plan, value, column.letter, origin)
            elif code == "year_proposed" or code == "year":
                _propose_year(plan, value, column.letter, origin)
            else:
                plan.proposals.append(Proposal("other", value, column.letter, origin, note=column.label))
    reports = [
        ColumnReport(
            column.letter,
            column.label,
            profile.field_of(column),
            FIELD_BY_CODE[profile.field_of(column)].label,
            LEVELS[FIELD_BY_CODE[profile.field_of(column)].level],
            extra_counts[column.label],
            extra_counts[column.label],
            0,
            0,
            extra_counts[column.label],
            source="complement",
        )
        for column in extra
    ]
    return differences, reports


def _attach_sources(rows: list[RowPlan], sources: dict[str, _SourceLine], label: str) -> None:
    for plan in rows:
        line = sources.get(plan.inventory)
        if line is None:
            continue
        for proposal in plan.proposals:
            if proposal.kind in ("isbn", "publication_year") and not proposal.url:
                proposal.url, proposal.note = line.url, line.note
        isbns = {p.value for p in plan.proposals if p.kind == "isbn"}
        years = {p.value for p in plan.proposals if p.kind == "publication_year"}
        problems = []
        if line.line and line.line != str(plan.number):
            problems.append(_("ligne source {a} au lieu de {b}").format(a=line.line, b=plan.number))
        parsed = parse_isbn(line.isbn) if line.isbn else None
        if line.isbn and (parsed.isbn13 if parsed else line.isbn) not in isbns:
            problems.append(_("ISBN {v} absent de la ligne").format(v=line.isbn))
        if line.year and line.year not in years:
            problems.append(_("année {v} absente de la ligne").format(v=line.year))
        if problems:
            plan.add("source_mismatch", f"{label} : " + " ; ".join(problems))


def _group(
    rows: list[RowPlan], existing: Existing, profile: Profile
) -> tuple[dict[str, SeriesPlan], dict[str, WorkPlan], dict[str, BookPlan]]:
    by_number = {plan.number: plan for plan in rows}
    series: dict[str, SeriesPlan] = {}
    series_by_key: dict[str, SeriesPlan] = {}
    works: dict[str, WorkPlan] = {}
    works_by_key: dict[tuple[str, str, str], WorkPlan] = {}
    groups: dict[tuple[tuple[str, str, str], str], list[RowPlan]] = defaultdict(list)
    for plan in rows:
        if plan.action == "error":
            continue
        f = plan.fields
        series_text, volume, title = f["series"], f["volume"], f["title"]
        standalone = not volume and not title
        series_key = "" if standalone else comparison_key(series_text)
        if standalone:
            plan.add("standalone")
        elif series_text:
            item = series_by_key.get(series_key)
            if item is None:
                item = SeriesPlan(
                    f"S-{len(series) + 1:04d}",
                    series_text,
                    series_key,
                    natural_title(series_text) if _INVERTED.match(series_text) else "",
                )
                if series_key in existing.series:
                    item.existing_id = existing.series[series_key][0]
                series[item.ref] = item
                series_by_key[series_key] = item
            item.forms[series_text] = item.forms.get(series_text, 0) + 1
            item.rows.append(plan.number)
            plan.series = item.ref
        title_built = False
        work_title = title
        if not title:
            work_title = series_text if not volume else f"{series_text}, tome {volume}"
            title_built = bool(volume)
            if title_built:
                plan.add("no_album_title", _("Titre construit : « {t} ».").format(t=work_title))
        key = (series_key, normalize(volume), comparison_key(work_title))
        work = works_by_key.get(key)
        if work is None:
            work = WorkPlan(
                f"O-{len(works) + 1:04d}", plan.series, volume, f["volume_number"], work_title, title_built, key
            )
            work.contributors = list(f["contributors"])
            if key in existing.works:
                work.existing_id = existing.works[key]
                plan.add(
                    "probable_match",
                    _("Notice n° {id} : même série, même tome, même titre.").format(id=work.existing_id),
                )
            works[work.ref] = work
            works_by_key[key] = work
            if plan.series:
                series[plan.series].works.append(work.ref)
        elif work.existing_id:
            plan.add(
                "probable_match", _("Notice n° {id} : même série, même tome, même titre.").format(id=work.existing_id)
            )
        work.rows.append(plan.number)
        plan.work = work.ref
        groups[(key, normalize(f["publisher"]))].append(plan)

    books: dict[str, BookPlan] = {}
    for (key, publisher_key), members in groups.items():
        work = works_by_key[key]
        subgroups = _split_contradictions(members)
        for subgroup in subgroups:
            head = subgroup[0]
            # Champ d'édition : le premier renseigné parmi les exemplaires, jamais une valeur perdue en silence
            carrier = next((p for p in subgroup if p.fields["collection"]), head)
            book = BookPlan(
                f"L-{len(books) + 1:04d}",
                work.ref,
                head.fields["publisher"],
                publisher_key,
                carrier.fields["collection"],
                carrier.fields["collection_kind"],
                collection_link=carrier.fields.get("collection_link"),
                collection_label=carrier.fields.get("collection_canonical") or carrier.fields["collection"],
            )
            written = {p.fields["collection"] for p in subgroup}
            if len(written) > 1:
                for plan in subgroup:
                    plan.add("book_collection_differs", " / ".join(sorted(v or "—" for v in written)))
            books[book.ref] = book
            work.books.append(book.ref)
            for plan in subgroup:
                plan.book = book.ref
                book.rows.append(plan.number)
                book.call_numbers.append(plan.fields["call_number"])
                for proposal in plan.proposals:
                    if proposal.kind == "isbn" and proposal.value not in book.isbn_proposed:
                        book.isbn_proposed.append(proposal.value)
                        if proposal.value in existing.isbns:
                            plan.add(
                                "probable_match",
                                _("ISBN proposé {v} déjà porté par l'édition n° {id}.").format(
                                    v=proposal.value, id=existing.isbns[proposal.value]
                                ),
                            )
                    if proposal.kind == "publication_year" and proposal.value not in book.year_proposed:
                        book.year_proposed.append(proposal.value)
                if plan.fields.get("isbn") and plan.fields["isbn"] in existing.isbns:
                    plan.add(
                        "probable_match",
                        _("ISBN {v} déjà porté par l'édition n° {id}.").format(
                            v=plan.fields["isbn"], id=existing.isbns[plan.fields["isbn"]]
                        ),
                    )
            if len(subgroups) > 1:
                for plan in subgroup:
                    plan.add(
                        "edition_conflict",
                        _("{n} lignes du même livre, {k} éditions distinctes.").format(
                            n=len(members), k=len(subgroups)
                        ),
                    )
    # Exemplaires : un par ligne, dans l'ordre du fichier.
    number = 0
    for plan in rows:
        if plan.action == "error":
            continue
        number += 1
        plan.copy = f"X-{number:04d}"
        books[plan.book].copies.append(plan.copy)
    for book in books.values():
        if len(book.rows) > 1:
            for row_number in book.rows:
                plan = by_number[row_number]
                others = ", ".join(str(n) for n in book.rows if n != row_number)
                plan.add(
                    "multiple_copies",
                    _("{n} exemplaires ; autres lignes : {rows}.").format(n=len(book.rows), rows=others),
                )
    # Même tome, titres différents.
    by_volume: dict[tuple[str, str], set[str]] = defaultdict(set)
    for work in works.values():
        if work.series and work.volume:
            by_volume[(work.series, normalize(work.volume))].add(work.ref)
    for (_series_ref, _volume), refs in by_volume.items():
        if len(refs) > 1:
            titles = " / ".join(sorted(works[r].title for r in refs))
            for ref in refs:
                for row_number in works[ref].rows:
                    by_number[row_number].add("same_volume_other_title", titles)
    for item in series.values():
        if len(item.forms) > 1:
            forms = " / ".join(sorted(item.forms))
            for row_number in item.rows:
                by_number[row_number].add("series_variants", forms)
    # Collections que la base confondrait (même éditeur, même nom sans casse ni accents) : signalées (D-L6-5).
    # Une forme retenue dans le profil (« collection canonique ») les rapproche explicitement ; sans elle, l'écart
    # bloque l'application : la base ne garde qu'une collection par nom comparé et par éditeur.
    collection_forms: dict[tuple[str, str], set[str]] = defaultdict(set)
    labels: dict[tuple[str, str], set[str]] = defaultdict(set)
    for book in books.values():
        if book.collection and book.collection_kind == "collection" and not book.collection_link:
            key = (book.publisher_key, normalize(book.collection_label))
            collection_forms[key].add(book.collection)
            labels[key].add(book.collection_label)
    for book in books.values():
        if not book.collection or book.collection_kind != "collection" or book.collection_link:
            continue
        key = (book.publisher_key, normalize(book.collection_label))
        forms = collection_forms.get(key, set())
        if len(forms) < 2:
            continue
        settled = len(labels[key]) == 1 and all(
            by_number[n].fields.get("collection_canonical") for b in books.values() if b.collection in forms
            for n in b.rows
        )  # fmt: skip
        code = "collection_canonical" if settled else "collection_variants"
        text_value = " / ".join(sorted(forms)) + (f" → « {book.collection_label} »" if settled else "")
        for row_number in book.rows:
            by_number[row_number].add(code, text_value)
    for work in works.values():
        twin = series_by_key.get(work.key[2]) if not work.series else None
        if twin is None:
            continue
        for row_number in work.rows:
            for message in by_number[row_number].messages:
                if message.code == "standalone":
                    message.text = _(
                        "Titre aussi porté par la série {ref} (« {t} ») : rattachement à vérifier."
                    ).format(ref=twin.ref, t=twin.title)
    return series, works, books


def _split_contradictions(members: list[RowPlan]) -> list[list[RowPlan]]:
    """Un groupe de lignes reste une édition, sauf si deux ISBN proposés (ou deux années) diffèrent : chaque valeur a
    alors son édition, et les lignes sans proposition la leur. Rien n'est tranché à la place d'un humain."""

    def values(plan: RowPlan, kind: str) -> frozenset[str]:
        return frozenset(p.value for p in plan.proposals if p.kind == kind)

    for kind in ("isbn", "publication_year"):
        distinct = {v for plan in members for v in values(plan, kind)}
        if len(distinct) > 1:
            parts: dict[frozenset[str], list[RowPlan]] = defaultdict(list)
            for plan in members:
                parts[values(plan, kind)].append(plan)
            return list(parts.values())
    return [members]


def _agents(rows: list[RowPlan], works: dict[str, WorkPlan], existing: Existing) -> dict[str, AgentPlan]:
    agents: dict[str, AgentPlan] = {}
    for plan in rows:
        if plan.action == "error":
            continue
        for contributor in plan.fields["contributors"]:
            agent = agents.get(contributor.written)
            if agent is None:
                agent = AgentPlan(contributor.written, normalize(contributor.written))
                agent.existing_id = existing.agents.get(contributor.written)
                if any(code == "name_organization" for code, _k, _v in name_proposals(contributor.written)):
                    agent.kind = "organization?"
                agents[contributor.written] = agent
            agent.rows.append(plan.number)
    by_key: dict[str, list[AgentPlan]] = defaultdict(list)
    for agent in agents.values():
        by_key[agent.key].append(agent)
    for group in by_key.values():
        if len(group) < 2:
            continue
        forms = " / ".join(sorted(a.written for a in group))
        numbers = {n for a in group for n in a.rows}
        for plan in rows:
            if plan.number in numbers:
                plan.add("name_variants", forms)
    return agents


def _publishers(rows: list[RowPlan], existing: Existing) -> dict[str, PublisherPlan]:
    publishers: dict[str, PublisherPlan] = {}
    for plan in rows:
        label = plan.fields.get("publisher", "")
        if plan.action == "error" or not label:
            continue
        key = normalize(label)
        item = publishers.setdefault(key, PublisherPlan(key, existing_id=existing.publishers.get(key)))
        item.forms[label] = item.forms.get(label, 0) + 1
        item.rows.append(plan.number)
    keys = sorted(publishers)
    flagged: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for item in publishers.values():
        item.usage_form = usage_form(item.label)
        if len(item.forms) > 1:
            for n in item.rows:
                flagged[n].append(("publisher_variants", " / ".join(sorted(item.forms))))
        if any(word in item.key for word in _MENTION_PUBLISHER):
            for n in item.rows:
                flagged[n].append(("publisher_mention", item.label))
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            words_a, words_b = set(a.split()), set(b.split())
            contained = (words_a < words_b or words_b < words_a) and min(len(words_a), len(words_b)) >= 1
            close = len(a) > 4 and len(b) > 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.88
            if contained or close:
                text_value = f"{publishers[a].label} / {publishers[b].label}"
                for n in publishers[a].rows + publishers[b].rows:
                    flagged[n].append(("publisher_close", text_value))
    for plan in rows:
        for code, text_value in flagged.get(plan.number, []):
            plan.add(code, text_value)
    return publishers


def _call_numbers(
    rows: list[RowPlan], books: dict[str, BookPlan], series: dict[str, SeriesPlan], by_number: dict[int, RowPlan]
) -> dict[str, list[str]]:
    books_by_call: dict[str, list[str]] = defaultdict(list)
    for plan in rows:
        call = plan.fields.get("call_number", "")
        if plan.action == "error" or not call:
            continue
        if plan.book not in books_by_call[call]:
            books_by_call[call].append(plan.book)
        if plan.series and "/" in call:
            prefix = call.split("/", 1)[0].strip()
            prefixes = series[plan.series].call_prefixes
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
        match = _CALL_NUMBER.search(call)
        number = plan.fields.get("volume_number")
        if match and number is not None and float(match.group(1)) != number:
            plan.add("call_volume_mismatch", _("Cote {c}, tome {t}.").format(c=call, t=plan.fields["volume"]))
    # Partagée : portée par des œuvres différentes. Deux éditions d'une même œuvre peuvent garder la même cote.
    shared = {call: refs for call, refs in books_by_call.items() if len({books[r].work for r in refs}) > 1}
    for call, refs in shared.items():
        for ref in refs:
            for row_number in books[ref].rows:
                titles = " / ".join(sorted({_book_title(by_number[books[r].rows[0]]) for r in refs}))
                by_number[row_number].add("shared_call_number", f"{call} : {titles}")
    return shared


def _book_title(plan: RowPlan) -> str:
    f = plan.fields
    parts = [f["series"], f["volume"] and f"t. {f['volume']}", f["title"]]
    return " — ".join(p for p in parts if p)


def _stored(code: str, plan: RowPlan, column: Column) -> str | None:
    """Ce que biblyO garderait dans son champ pour cette cellule ; None quand la colonne n'a pas de champ propre."""
    f = plan.fields
    if code == "series":
        return str(f["series"])
    if code == "volume":
        return str(f["volume"])
    if code == "title":
        return str(f["title"])
    if code == "contributor":
        return next(
            (c.written for c in f["contributors"] if c.written == " ".join(_text(plan.raw.get(column.label)).split())),
            "",
        )
    if code in ("publisher", "collection", "acquisition", "call_number"):
        return str(f[code])
    if code == "condition":
        return str(f["condition_note"])
    if code == "inventory":
        return plan.inventory
    if code in ("isbn_proposed", "year_proposed", "location_hint"):
        kind = {"isbn_proposed": "isbn", "year_proposed": "publication_year"}.get(code, "location")
        found = [p.value for p in plan.proposals if p.kind == kind and p.column == column.letter]
        return found[0] if found else ""
    return None


def _coverage(sheet: Sheet, rows: list[RowPlan], profile: Profile) -> list[ColumnReport]:
    """Pour chaque colonne : remplies, gardées brutes, gardées à l'identique dans leur champ, transformées."""
    reports: list[ColumnReport] = []
    for column in sheet.columns:
        code = profile.field_of(column)
        spec = FIELD_BY_CODE[code]
        filled = kept = same = changed = proposals = 0
        examples: list[tuple[int, str, str]] = []
        for plan in rows:
            value = plan.raw.get(column.label)
            shown = display(value)
            if value is None or not shown.strip():
                continue
            filled += 1
            kept += 1  # la valeur brute reste attachée à la ligne, quoi qu'il arrive
            if code in ("isbn_proposed", "year_proposed", "location_hint"):
                proposals += 1
            stored = _stored(code, plan, column)
            if stored is None or stored == shown:
                same += 1
            else:
                changed += 1
                if len(examples) < 5:
                    examples.append((plan.number, shown, stored))
        reports.append(
            ColumnReport(
                column.letter,
                column.label,
                code,
                spec.label,
                LEVELS[spec.level],
                filled,
                kept,
                same,
                changed,
                proposals,
                examples,
            )
        )
    return reports


def _totals(sim: Simulation) -> None:
    rows = sim.rows
    read = len(rows)
    rejected = [r for r in rows if r.action == "error"]
    copies = read - len(rejected)
    distribution = Counter(len(b.rows) for b in sim.books.values())
    proposals = Counter(p.kind for r in rows for p in r.proposals)
    severities = Counter(r.severity for r in rows)
    codes = sim.anomalies()
    keys = {a.key for a in sim.agents.values()}
    standalone = sum(1 for w in sim.works.values() if not w.series)
    sim.totals = {
        "rows_read": read,
        "rows_rejected": len(rejected),
        "copies": copies,
        "books": len(sim.books),
        "extra_copies": copies - len(sim.books),
        "distribution": dict(sorted(distribution.items())),
        "works": len(sim.works),
        "standalone_works": standalone,
        "series": len(sim.series),
        "series_forms": sum(len(s.forms) for s in sim.series.values()),
        "agents_written": len(sim.agents),
        "agents_keys": len(keys),
        "publishers": len(sim.publishers),
        "publisher_forms": sum(len(p.forms) for p in sim.publishers.values()),
        "collections": len({r.fields["collection"] for r in rows if r.fields.get("collection")}),
        "shared_call_numbers": len(sim.shared_call_numbers),
        "inverted_titles": len(codes.get("inverted_article", [])),
        "date_cells": sum(1 for r in rows for m in r.messages if m.code == "date_format_cell"),
        "differences": len(sim.differences),
        "proposals": dict(sorted(proposals.items())),
        "severities": {k: severities.get(k, 0) for k in ("ok", "info", "warning", "error")},
        "anomaly_rows": {code: len(found) for code, found in sorted(codes.items())},
    }
    t = sim.totals
    sim.checks = [
        (
            _("Lignes lues = exemplaires prévus + lignes refusées"),
            read == copies + len(rejected),
            f"{read} = {copies} + {len(rejected)}",
        ),
        (
            _("Exemplaires = somme des exemplaires de chaque livre"),
            copies == sum(k * n for k, n in distribution.items()),
            " + ".join(f"{n} × {k}" for k, n in sorted(distribution.items())) + f" = {copies}",
        ),
        (
            _("Livres = exemplaires − exemplaires supplémentaires"),
            t["books"] == copies - t["extra_copies"],
            f"{t['books']} = {copies} − {t['extra_copies']}",
        ),
        (
            _("Chaque exemplaire prévu a son livre, son œuvre et son identifiant de source"),
            all(r.book and r.work and r.copy and r.source_id for r in rows if r.action != "error"),
            "",
        ),
        (_("Aucune cellule remplie sans valeur brute gardée"), all(c.kept_raw == c.filled for c in sim.columns), ""),
        (
            _("Aucune colonne sans destination"),
            all(c.target in FIELD_BY_CODE for c in sim.columns),
            f"{len(sim.columns)} colonnes",
        ),
    ]
    blockers = list(sim.blockers)
    if rejected:
        blockers.append(_("{n} ligne(s) refusée(s) : à corriger dans le fichier.").format(n=len(rejected)))
    if codes.get("collection_variants"):
        blockers.append(
            _("{n} ligne(s) portent des collections écrites autrement que la base confondrait : choisissez la forme "
              "retenue, ou rangez une valeur autrement, dans la correspondance (D-L6-5).").format(
                n=len(codes["collection_variants"])
            )
        )  # fmt: skip
    if codes.get("probable_match"):
        blockers.append(
            _("{n} ligne(s) correspondent probablement à des notices existantes : à confirmer.").format(
                n=len(codes["probable_match"])
            )
        )
    pending = sorted({decision for status, decision, _rule in RULE_STATUS.values() if status == "provisional"})
    if pending:
        blockers.append(
            _("Réglages provisoires ({d}) : recommandations non approuvées, à valider avant l'application.").format(
                d=", ".join(pending)
            )
        )
    sim.blockers = blockers


def plan_digest(sim: Simulation) -> str:
    """Empreinte du plan : fichier(s), profil, état du catalogue, et ce que chaque ligne deviendrait."""
    payload = {
        "version": SIMULATION_VERSION,
        "file": sim.file_sha256,
        "sheet": sim.sheet,
        "complement": sim.complement_sha256,
        "complement_sheet": sim.complement_sheet,
        "profile": sim.profile.sha256,
        "catalogue": sim.existing_fingerprint,
        "rows": [
            {
                "n": r.number,
                "id": r.source_id,
                "inv": r.inventory,
                "action": r.action,
                "series": r.series,
                "work": r.work,
                "book": r.book,
                "copy": r.copy,
                "fields": _jsonable(r.fields),
                "messages": [(m.code, m.text, m.column) for m in r.messages],
                "proposals": [asdict(p) for p in r.proposals],
            }
            for r in sim.rows
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _jsonable(fields: dict[str, Any]) -> dict[str, Any]:
    out = dict(fields)
    out["contributors"] = [asdict(c) for c in fields.get("contributors", [])]
    return out


def condition_label(code: str) -> str:
    return CONDITIONS.get(code, code)
