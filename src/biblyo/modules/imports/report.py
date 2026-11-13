# SPDX-License-Identifier: EUPL-1.2
"""Rapport de simulation téléchargeable (lot L6, T4) : classeur xlsx en plusieurs feuilles, et CSV des lignes.

Chaque cellule est écrite **comme du texte** : une valeur qui commence par =, +, -, @, une tabulation ou un retour
chariot ne devient jamais une formule (injection de formules). Dans le CSV, une telle valeur est précédée d'une
apostrophe, comme le recommande l'OWASP ; dans le xlsx, la cellule est typée texte, sans rien ajouter.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.utils import get_column_letter

from biblyo.core.i18n import _
from biblyo.modules.imports.profile import COLLECTION_KINDS, CONDITIONS, FIELD_BY_CODE, display
from biblyo.modules.imports.reader import FORMULA_TRIGGERS
from biblyo.modules.imports.simulate import CODES, FAMILIES, PROPOSAL_KINDS, RowPlan, Simulation

SEVERITY_LABELS = {
    "ok": _("Sans remarque"),
    "info": _("Information"),
    "warning": _("À vérifier"),
    "error": _("Refusée"),
}


def safe_csv(value: Any) -> str:
    """Texte d'une cellule de CSV, neutralisé s'il pourrait être pris pour une formule."""
    text_value = display(value) if not isinstance(value, str) else value
    return f"'{text_value}" if text_value.startswith(FORMULA_TRIGGERS) else text_value


def _contributors(row: RowPlan) -> str:
    return " | ".join(
        f"{c.written} ({_('auteur') if c.role == 'author' else _('rôle à préciser')})"
        for c in row.fields["contributors"]
    )


def row_headers(sim: Simulation) -> list[str]:
    raw = [f"{c.letter} · {c.header}" for c in sim.columns if c.source == "main"]
    return [
        _("Ligne"),
        _("Identifiant de source"),
        _("Numéro d'inventaire"),
        _("Résultat"),
        _("Gravité"),
        _("Exemplaire"),
        _("Livre (édition)"),
        _("Œuvre"),
        _("Série"),
        _("Série comprise"),
        _("Tome compris"),
        _("Titre compris"),
        _("Contributeurs compris"),
        _("Éditeur compris"),
        _("Collection comprise"),
        _("État compris"),
        _("Précision sur l'état"),
        _("Acquisition"),
        _("Cote"),
        _("Code-barres"),
        _("Signalements"),
        _("Propositions"),
        *raw,
    ]


def row_values(sim: Simulation, row: RowPlan) -> list[Any]:
    f = row.fields
    kind = f.get("collection_kind", "")
    collection = f"{f['collection']} ({COLLECTION_KINDS.get(kind, kind)})" if f.get("collection") else ""
    raw = [display(row.raw.get(c.header)) for c in sim.columns if c.source == "main"]
    return [
        row.number,
        row.source_id,
        row.inventory,
        _("refusée") if row.action == "error" else _("créée"),
        SEVERITY_LABELS[row.severity],
        row.copy,
        row.book,
        row.work,
        row.series,
        f["series"],
        f["volume"],
        sim.works[row.work].title if row.work else f["title"],
        _contributors(row),
        f["publisher"],
        collection,
        CONDITIONS.get(f["condition"], "") if f["condition"] else "",
        f["condition_note"],
        f["acquisition"],
        f["call_number"],
        f.get("barcode", ""),
        " | ".join(f"{m.label}{' : ' + m.text if m.text else ''}" for m in row.messages),
        " | ".join(
            f"{PROPOSAL_KINDS.get(p.kind, p.kind)} : {p.value}{' (' + p.origin + ')' if p.origin else ''}"
            for p in row.proposals
        ),
        *raw,
    ]


def _sheets(sim: Simulation) -> list[tuple[str, list[str], Iterable[Sequence[Any]]]]:
    t = sim.totals
    summary: list[tuple[Any, Any]] = [
        (_("Fichier"), sim.filename),
        (_("Empreinte SHA-256"), sim.file_sha256),
        (_("Feuille"), sim.sheet),
        (_("Fichier complémentaire"), sim.complement_filename or "—"),
        (_("Empreinte du complémentaire"), sim.complement_sha256 or "—"),
        (_("Feuille des sources"), sim.sources_sheet or "—"),
        (_("Empreinte du plan"), sim.plan_sha256),
        (_("Lignes lues"), t["rows_read"]),
        (_("Lignes refusées"), t["rows_rejected"]),
        (_("Exemplaires prévus"), t["copies"]),
        (_("Livres (éditions)"), t["books"]),
        (_("Exemplaires supplémentaires"), t["extra_copies"]),
        (_("Œuvres"), t["works"]),
        (_("dont albums isolés"), t["standalone_works"]),
        (_("Séries"), t["series"]),
        (_("Contributeurs, formes écrites"), t["agents_written"]),
        (_("Contributeurs, formes normalisées"), t["agents_keys"]),
        (_("Éditeurs"), t["publishers"]),
        (_("Collections (valeurs)"), t["collections"]),
        (_("Cotes partagées"), t["shared_call_numbers"]),
        (_("Titres à article inversé (lignes)"), t["inverted_titles"]),
        (_("Cellules au format date"), t["date_cells"]),
        (_("Différences de version"), t["differences"]),
    ]
    summary += [(_("Livres à {n} exemplaire(s)").format(n=k), n) for k, n in t["distribution"].items()]
    summary += [(_("Propositions : {k}").format(k=PROPOSAL_KINDS.get(k, k)), n) for k, n in t["proposals"].items()]
    summary += [
        (label, (_("conforme") if ok else _("ÉCART")) + (f" — {detail}" if detail else ""))
        for label, ok, detail in sim.checks
    ]
    summary += [(_("Avant toute application"), blocker) for blocker in sim.blockers]

    anomalies = sim.anomalies()
    anomaly_rows = [
        (
            FAMILIES[CODES[code][2]],
            CODES[code][1],
            SEVERITY_LABELS[CODES[code][0]],
            row.number,
            row.inventory,
            m.column,
            m.text,
        )
        for code in sorted(anomalies, key=lambda c: (CODES[c][2], c))
        for row in anomalies[code]
        for m in row.messages
        if m.code == code
    ]
    books = [
        (
            b.ref,
            sim.works[b.work].title,
            sim.series[sim.works[b.work].series].title if sim.works[b.work].series else "",
            sim.works[b.work].volume,
            b.publisher,
            b.collection,
            len(b.rows),
            ", ".join(str(n) for n in b.rows),
            ", ".join(sorted(set(b.call_numbers))),
            ", ".join(b.isbn_proposed),
            ", ".join(b.year_proposed),
        )
        for b in sim.books.values()
    ]
    series = [
        (
            s.ref,
            s.title,
            " / ".join(sorted(s.forms)),
            s.search_form,
            len(s.rows),
            len(s.works),
            ", ".join(f"{k} ({n})" for k, n in sorted(s.call_prefixes.items())),
            s.existing_id or "",
        )
        for s in sim.series.values()
    ]
    shared = [
        (call, ref, sim.works[sim.books[ref].work].title, ", ".join(str(n) for n in sim.books[ref].rows))
        for call, refs in sorted(sim.shared_call_numbers.items())
        for ref in refs
    ]
    proposals = [
        (row.number, row.inventory, PROPOSAL_KINDS.get(p.kind, p.kind), p.value, p.column, p.origin, p.url, p.note)
        for row in sim.rows
        for p in row.proposals
    ]
    differences = [
        (d.row, d.inventory, d.letter, d.column, d.main, d.complement, d.complement_row, d.origin)
        for d in sim.differences
    ]
    columns = [
        (
            c.letter,
            c.header,
            _("principal") if c.source == "main" else _("complémentaire"),
            c.field_label,
            c.level,
            c.filled,
            c.kept_raw,
            c.stored_as_is,
            c.transformed,
            c.proposals,
            " | ".join(f"{n} : « {a} » → « {b} »" for n, a, b in c.examples_transformed),
        )
        for c in sim.columns
    ]
    agents = [
        (
            a.written,
            a.key,
            len(a.rows),
            _("organisation ?") if a.kind != "person" else "",
            ", ".join(str(n) for n in a.rows[:30]),
        )
        for a in sorted(sim.agents.values(), key=lambda a: (a.key, a.written))
    ]
    publishers = [
        (p.label, " / ".join(sorted(p.forms)), p.usage_form, len(p.rows), p.existing_id or "")
        for p in sorted(sim.publishers.values(), key=lambda p: p.key)
    ]
    profile = [(column, FIELD_BY_CODE[code].label) for column, code in sim.profile.columns.items()] + [
        (_("État « {v} »").format(v=v), CONDITIONS.get(c, c) or _("non rangé"))
        for v, c in sim.profile.condition_map.items()
    ]
    profile += [
        (_("Collection « {v} »").format(v=v), COLLECTION_KINDS.get(c, c)) for v, c in sim.profile.collection_map.items()
    ]
    head_0 = [_("Rubrique"), _("Valeur")]
    head_2 = [_("Famille"), _("Signalement"), _("Gravité"), _("Ligne"), _("Inventaire"), _("Colonne"), _("Détail")]
    head_3 = [
        _("Livre"),
        _("Titre"),
        _("Série"),
        _("Tome"),
        _("Éditeur"),
        _("Collection"),
        _("Exemplaires"),
        _("Lignes"),
        _("Cotes"),
        _("ISBN proposés"),
        _("Années proposées"),
    ]
    head_4 = [
        _("Série"),
        _("Titre gardé"),
        _("Formes écrites"),
        _("Forme de recherche"),
        _("Lignes"),
        _("Œuvres"),
        _("Préfixes de cote"),
        _("Série existante"),
    ]
    head_5 = [_("Cote"), _("Livre"), _("Titre"), _("Lignes")]
    head_6 = [
        _("Ligne"),
        _("Inventaire"),
        _("Sorte"),
        _("Valeur"),
        _("Colonne"),
        _("Origine"),
        _("Adresse"),
        _("Remarque"),
    ]
    head_7 = [
        _("Ligne"),
        _("Inventaire"),
        _("Colonne"),
        _("En-tête"),
        _("Fichier principal"),
        _("Fichier complémentaire"),
        _("Ligne du complémentaire"),
        _("Provenance"),
    ]
    head_8 = [
        _("Colonne"),
        _("En-tête exact"),
        _("Fichier"),
        _("Destination"),
        _("Niveau"),
        _("Remplies"),
        _("Valeurs brutes gardées"),
        _("Gardées à l'identique"),
        _("Transformées"),
        _("Propositions"),
        _("Exemples de transformation"),
    ]
    head_9 = [_("Forme écrite"), _("Forme normalisée"), _("Lignes"), _("Remarque"), _("Numéros de ligne")]
    head_10 = [_("Libellé"), _("Formes écrites"), _("Forme d'usage proposée"), _("Lignes"), _("Éditeur existant")]
    head_11 = [_("Colonne ou valeur"), _("Destination")]
    return [
        (_("Synthèse"), head_0, summary),
        (_("Lignes"), row_headers(sim), (row_values(sim, r) for r in sim.rows)),
        (_("Anomalies"), head_2, anomaly_rows),
        (_("Livres"), head_3, books),
        (_("Séries"), head_4, series),
        (_("Cotes partagées"), head_5, shared),
        (_("Propositions"), head_6, proposals),
        (_("Différences"), head_7, differences),
        (_("Colonnes"), head_8, columns),
        (_("Contributeurs"), head_9, agents),
        (_("Éditeurs"), head_10, publishers),
        (_("Profil"), head_11, profile),
    ]


def _text_cell(cell: Cell, value: Any) -> None:
    """Un nombre reste un nombre ; tout le reste est écrit comme texte, jamais comme formule."""
    if isinstance(value, bool) or value is None:
        cell.value = "" if value is None else display(value)
    elif isinstance(value, int | float):
        cell.value = value
        return
    else:
        cell.value = str(value)
    cell.data_type = "s"


def to_xlsx(sim: Simulation) -> bytes:
    book = Workbook()
    book.remove(book.active)
    for title, headers, rows in _sheets(sim):
        sheet = book.create_sheet(title[:31])
        sheet.append(headers)
        for values in rows:
            sheet.append([None] * len(values))
            line = sheet.max_row
            for index, value in enumerate(values, start=1):
                _text_cell(sheet.cell(row=line, column=index), value)
        sheet.freeze_panes = "A2"
        for index, header in enumerate(headers, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = min(max(len(str(header)) + 2, 10), 48)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def to_csv(sim: Simulation) -> bytes:
    """Les lignes, une par ligne du fichier : UTF-8 avec BOM, point-virgule, valeurs neutralisées."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([safe_csv(h) for h in row_headers(sim)])
    for row in sim.rows:
        writer.writerow([safe_csv(v) for v in row_values(sim, row)])
    return ("﻿" + out.getvalue()).encode("utf-8")
