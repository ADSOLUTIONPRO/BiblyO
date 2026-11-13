# SPDX-License-Identifier: EUPL-1.2
"""Export complet du catalogue (lot L6, tâche T6) : sauvegarde logique et réversibilité.

Un classeur xlsx, ou une archive de fichiers CSV, avec une feuille par table : œuvres, séries, éditions,
identifiants, exemplaires, emplacements, contributeurs, éditeurs, collections, catégories, ISBN proposés, lots,
lignes et propositions d'import. Chaque ligne porte ses identifiants (interne et public, stables d'un export à
l'autre) et sa provenance (lot, ligne du fichier, identifiant de source) ; les colonnes d'un fichier importé qui
n'ont pas de champ dans biblyO reviennent dans « Lignes d'import », une colonne par en-tête d'origine.

Confidentialité : ni lecteurs, ni prêts, ni comptes — l'export d'un lecteur a son propre chemin (L3). Les codes
(inventaire, code-barres, cote, ISBN) sont écrits **comme du texte** : un zéro initial n'est jamais perdu, une valeur
qui commence par = n'est jamais une formule. Dans les CSV (UTF-8 avec BOM, point-virgule), une telle valeur est
précédée d'une apostrophe (OWASP) ; le xlsx, lui, garde la valeur exacte dans une cellule de texte.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import text
from sqlalchemy.orm import Session

from biblyo import __version__
from biblyo.core.clock import now_iso
from biblyo.core.i18n import _
from biblyo.modules.imports.report import safe_csv


@dataclass(frozen=True)
class Table:
    name: str  # nom de la feuille, et du fichier CSV
    headers: tuple[str, ...]
    rows: list[Sequence[Any]]


_QUERIES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "oeuvres",
        ("id", "public_id", "titre", "sous_titre", "titre_de_tri", "titre_original", "type_document", "section",
         "public", "statut", "completude", "resume", "notes", "cree_le", "modifie_le", "version"),
        "SELECT w.id, w.public_id, w.title, w.subtitle, w.title_sort, w.original_title, d.code, s.code, w.audience,"
        " w.status, w.completeness, w.summary, w.notes, w.created_at, w.updated_at, w.version FROM works w"
        " JOIN document_types d ON d.id = w.document_type_id LEFT JOIN sections s ON s.id = w.section_id ORDER BY w.id",
    ),
    (
        "series",
        ("id", "public_id", "titre", "titre_de_tri", "serie_parente", "prefixe_de_cote", "statut", "notes", "cree_le"),
        "SELECT s.id, s.public_id, s.title, s.title_sort, p.public_id, s.call_prefix, s.status, s.notes, s.created_at"
        " FROM series s LEFT JOIN series p ON p.id = s.parent_id ORDER BY s.id",
    ),
    (
        "oeuvres_series",
        ("oeuvre", "serie", "tome_numero", "tome_libelle", "numero_interne", "principale"),
        "SELECT w.public_id, s.public_id, ws.volume_number, ws.volume_label, ws.call_sequence, ws.is_primary"
        " FROM work_series ws JOIN works w ON w.id = ws.work_id JOIN series s ON s.id = ws.series_id"
        " ORDER BY ws.work_id, ws.series_id",
    ),
    (
        "contributeurs",
        ("id", "nom_affiche", "nom_de_tri", "sorte", "ark_bnf", "dates", "cree_le"),
        "SELECT id, display_name, sort_name, kind, ark_bnf, dates, created_at FROM agents ORDER BY id",
    ),
    (
        "roles",
        ("niveau", "notice", "contributeur", "nom_affiche", "role", "position"),
        "SELECT 'oeuvre', w.public_id, a.id, a.display_name, wc.role, wc.position FROM work_contributors wc"
        " JOIN works w ON w.id = wc.work_id JOIN agents a ON a.id = wc.agent_id"
        " UNION ALL SELECT 'edition', e.public_id, a.id, a.display_name, ec.role, ec.position"
        " FROM edition_contributors ec JOIN editions e ON e.id = ec.edition_id JOIN agents a ON a.id = ec.agent_id"
        " ORDER BY 1, 2, 6",
    ),
    (
        "editeurs",
        ("id", "nom", "nom_compare", "cree_le"),
        "SELECT id, name, name_norm, created_at FROM publishers ORDER BY id",
    ),
    (
        "collections",
        ("id", "nom", "editeur", "issn", "cree_le"),
        "SELECT c.id, c.name, p.name, c.issn, c.created_at FROM collections c"
        " LEFT JOIN publishers p ON p.id = c.publisher_id ORDER BY c.id",
    ),
    (
        "editions",
        ("id", "public_id", "oeuvre", "principale", "titre_variante", "editeur", "collection", "numero_collection",
         "annee", "date", "mention", "langue", "pages", "note_physique", "notes", "origine", "statut", "cree_le",
         "modifie_le", "version"),
        "SELECT e.id, e.public_id, w.public_id, e.is_primary, e.title_variant, p.name, c.name, e.collection_number,"
        " e.publication_year, e.publication_date, e.edition_statement, e.language, e.page_count, e.physical_note,"
        " e.notes, e.origin, e.status, e.created_at, e.updated_at, e.version FROM editions e"
        " JOIN works w ON w.id = e.work_id LEFT JOIN publishers p ON p.id = e.publisher_id"
        " LEFT JOIN collections c ON c.id = e.collection_id ORDER BY e.id",
    ),
    (
        "identifiants",
        ("edition", "schema", "valeur"),
        "SELECT e.public_id, i.scheme, i.value FROM edition_identifiers i JOIN editions e ON e.id = i.edition_id"
        " ORDER BY i.edition_id, i.id",
    ),
    (
        "isbn_proposes",
        ("edition", "isbn", "statut", "source", "reference", "adresse", "cree_le", "decide_le"),
        "SELECT e.public_id, c.isbn13, c.status, c.source_code, c.external_id, c.external_url, c.created_at,"
        " c.decided_at FROM edition_isbn_candidates c JOIN editions e ON e.id = c.edition_id ORDER BY c.id",
    ),
    (
        "exemplaires",
        ("id", "public_id", "edition", "oeuvre", "code_barres", "numero_inventaire", "cote", "statut", "pretable",
         "etat", "precision_etat", "mode_acquisition", "date_acquisition", "note_acquisition", "prix_centimes",
         "provenance", "emplacement", "chemin_emplacement", "position", "notes", "lot_import", "ligne_source",
         "cree_le", "modifie_le", "version"),
        "SELECT c.id, c.public_id, e.public_id, w.public_id, c.barcode, c.inventory_number, c.call_number, c.status,"
        " c.is_loanable, c.condition, c.condition_note, c.acquisition_mode, c.acquired_on, c.acquisition_note,"
        " c.price_cents, c.provenance, l.public_id, l.path, c.shelf_position, c.notes, b.public_id, r.row_number,"
        " c.created_at, c.updated_at, c.version FROM copies c JOIN editions e ON e.id = c.edition_id"
        " JOIN works w ON w.id = e.work_id LEFT JOIN locations l ON l.id = c.location_id"
        " LEFT JOIN import_links k ON k.entity_type = 'copy' AND k.entity_id = c.id"
        " LEFT JOIN import_rows r ON r.id = k.row_id LEFT JOIN import_batches b ON b.id = r.batch_id ORDER BY c.id",
    ),
    (
        "emplacements",
        ("id", "public_id", "parent", "sorte", "nom", "code", "chemin", "profondeur", "ordre", "capacite",
         "contenu", "actif", "notes"),
        "SELECT l.id, l.public_id, p.public_id, l.kind, l.name, l.code, l.path, l.depth, l.sort_order, l.capacity,"
        " l.content_hint, l.is_active, l.notes FROM locations l LEFT JOIN locations p ON p.id = l.parent_id"
        " ORDER BY l.id",
    ),
    (
        "categories",
        ("oeuvre", "sorte", "libelle"),
        "SELECT w.public_id, t.kind, t.label FROM work_tags wt JOIN works w ON w.id = wt.work_id"
        " JOIN tags t ON t.id = wt.tag_id ORDER BY wt.work_id, t.kind, t.label",
    ),
    (
        "lots_import",
        ("id", "public_id", "fichier", "empreinte_fichier", "feuille", "fichier_complementaire",
         "empreinte_complementaire", "empreinte_plan", "statut", "sauvegarde_avant", "cree_le", "applique_le"),
        "SELECT id, public_id, filename, file_sha256, sheet, complement_filename, complement_sha256, plan_sha256,"
        " status, backup_name, created_at, applied_at FROM import_batches ORDER BY id",
    ),
    (
        "propositions_import",
        ("lot", "ligne", "edition", "champ", "valeur", "source", "note", "statut", "cree_le", "decide_le"),
        "SELECT b.public_id, r.row_number, e.public_id, p.field, p.value, p.source_url, p.note, p.status,"
        " p.created_at, p.decided_at FROM import_proposals p JOIN editions e ON e.id = p.edition_id"
        " LEFT JOIN import_rows r ON r.id = p.row_id LEFT JOIN import_batches b ON b.id = r.batch_id ORDER BY p.id",
    ),
)  # fmt: skip
#: Colonnes écrites comme du texte même quand elles ressemblent à un nombre.
CODES = frozenset({"code_barres", "numero_inventaire", "cote", "valeur", "isbn", "public_id", "reference"})


def tables(session: Session) -> list[Table]:
    found = [Table(name, headers, [tuple(r) for r in session.execute(text(sql))]) for name, headers, sql in _QUERIES]
    found.append(_import_rows(session))
    return found


def _import_rows(session: Session) -> Table:
    """Les lignes de chaque fichier importé, valeurs d'origine comprises : une colonne par en-tête rencontré."""
    rows = session.execute(
        text(
            "SELECT b.public_id, r.sheet, r.row_number, r.raw, r.normalized, r.severity FROM import_rows r"
            " JOIN import_batches b ON b.id = r.batch_id ORDER BY r.batch_id, r.row_number"
        )
    ).all()
    labels: list[str] = []
    parsed = []
    for batch, sheet, number, raw, normalized, severity in rows:
        values = json.loads(raw)
        for label in values:
            if label not in labels:
                labels.append(label)
        stored = json.loads(normalized or "{}")
        copy = stored.get("copy") or {}
        parsed.append((batch, sheet, number, stored.get("source_id"), copy.get("public_id"), severity, values))
    headers = ("lot", "feuille", "ligne", "identifiant_source", "exemplaire", "gravite", *labels)
    lines = [(b, s, n, src, c, sev, *(v.get(label) for label in labels)) for b, s, n, src, c, sev, v in parsed]
    return Table("lignes_import", headers, lines)


def _readme(found: list[Table]) -> Table:
    lines: list[Sequence[Any]] = [
        (_("Application"), f"biblyO {__version__}"),
        (_("Exporté le"), now_iso()),
        (_("Contenu"), _("Catalogue complet, emplacements et traces d'import. Ni lecteurs, ni prêts, ni comptes.")),
        (_("Codes"), _("Inventaires, codes-barres, cotes et ISBN en texte : aucun zéro initial perdu.")),
    ]
    lines += [(_("Feuille {name}").format(name=t.name), _("{n} ligne(s)").format(n=len(t.rows))) for t in found]
    return Table("lisez_moi", (_("rubrique"), _("valeur")), lines)


def to_xlsx(session: Session) -> bytes:
    found = tables(session)
    book = Workbook()
    book.remove(book.active)
    for table in [_readme(found), *found]:
        sheet = book.create_sheet(table.name[:31])
        sheet.append(list(table.headers))
        codes = {i for i, h in enumerate(table.headers) if h in CODES or (table.name == "lignes_import" and i >= 6)}
        for values in table.rows:
            sheet.append([None] * len(table.headers))
            line = sheet.max_row
            for index, value in enumerate(values):
                if value is None:
                    continue
                cell = sheet.cell(row=line, column=index + 1)
                if isinstance(value, str) or (index in codes and not isinstance(value, bool)):
                    if table.name == "lignes_import" and isinstance(value, int | float):
                        cell.value = value  # valeur d'origine : son type est gardé
                        continue
                    cell.value = str(value)
                    cell.data_type = "s"
                else:
                    cell.value = value
        sheet.freeze_panes = "A2"
        for index, header in enumerate(table.headers, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = min(max(len(str(header)) + 2, 10), 40)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def to_csv_zip(session: Session) -> bytes:
    found = tables(session)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for table in [_readme(found), *found]:
            buffer = io.StringIO()
            writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
            writer.writerow([safe_csv(h) for h in table.headers])
            for values in table.rows:
                writer.writerow(["" if v is None else safe_csv(v) for v in values])
            archive.writestr(f"{table.name}.csv", ("﻿" + buffer.getvalue()).encode("utf-8"))
    return out.getvalue()
