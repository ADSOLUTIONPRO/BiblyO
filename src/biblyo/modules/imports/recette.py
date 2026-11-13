# SPDX-License-Identifier: EUPL-1.2
"""Recette aller-retour d'un import (lot L6, tâche T8), dans une instance **indépendante et temporaire**.

Excel → correspondance → simulation → coupure en plein import (rien ne doit rester) → import → second import
(refusé, rien ne doit changer) → export au format d'origine → comparaison cellule par cellule avec le fichier lu →
export complet → restauration de la sauvegarde prise avant l'import. Jamais la base DEV permanente, jamais
l’ancienne application :
l'instance naît dans un dossier temporaire réservé à son propriétaire (0700) et en est effacée à la fin, sauf
demande contraire. Le rapport ne contient que des comptes et des cellules ; il est écrit là où on le demande.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from biblyo.config import Settings
from biblyo.core.backup import restore_backup
from biblyo.core.db import Database
from biblyo.core.migrate import upgrade
from biblyo.modules.imports import apply as importer
from biblyo.modules.imports import export, guard, original, reader, simulate, store
from biblyo.modules.imports import profile as profiles

CATALOGUE = ("works", "editions", "copies", "series", "agents", "publishers", "collections")


@dataclass
class Step:
    label: str
    ok: bool
    detail: str = ""


@dataclass
class Recette:
    filename: str
    sha256: str
    complement: str = ""
    totals: dict[str, Any] = field(default_factory=dict)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    plan: str = ""
    steps: list[Step] = field(default_factory=list)
    reconciliation: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] = field(default_factory=dict)
    complement_check: dict[str, Any] = field(default_factory=dict)
    export_counts: dict[str, int] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    workdir: str = ""

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)


def _counts(settings: Settings, tables: tuple[str, ...] = CATALOGUE) -> dict[str, int]:
    connection = sqlite3.connect(settings.database_path)
    try:
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}  # noqa: S608
    finally:
        connection.close()


def run(
    main_bytes: bytes,
    main_name: str,
    complement_bytes: bytes | None = None,
    complement_name: str = "",
    *,
    profile_json: str | None = None,
    keep: bool = False,
) -> Recette:
    import time

    main = reader.read(main_bytes, main_name)
    other = reader.read(complement_bytes, complement_name) if complement_bytes is not None else None
    result = Recette(main.filename, main.sha256, other.filename if other else "")
    for workbook in (main, other):
        if workbook is not None and guard.refusal(workbook):
            result.steps.append(Step("Fichier accepté comme source", False, guard.refusal(workbook)))
            return result
    root = Path(tempfile.mkdtemp(prefix="biblyo-recette-"))
    os.chmod(root, 0o700)
    result.workdir = str(root)
    try:
        (root / "data").mkdir(mode=0o700)
        (root / "backups").mkdir(mode=0o700)
        settings = Settings(env="test", data_dir=root / "data", backup_dir=root / "backups")
        upgrade(settings)
        database = Database(settings.database_path)
        try:
            with database.session(write=True) as session:
                from biblyo.core.settings_store import set_setting

                set_setting(session, importer.ENABLED_SETTING, True)
            sheet = main.sheet(store.data_sheet(main))
            assert sheet is not None
            other_sheet = other.sheet(store.data_sheet(other)) if other else None
            if profile_json:
                chosen = profiles.Profile.from_json(profile_json)
            else:
                chosen = profiles.detect(sheet)
                if other_sheet is not None:
                    chosen = profiles.detect(other_sheet, chosen)
            started = time.monotonic()
            with database.session() as session:
                sim = simulate.simulate(
                    main,
                    sheet.name,
                    chosen,
                    complement=other,
                    complement_sheet=other_sheet.name if other_sheet else "",
                    existing=simulate.snapshot(session),
                )
                session.rollback()
            result.seconds["simulation"] = round(time.monotonic() - started, 2)
            result.totals, result.checks, result.blockers, result.plan = (
                sim.totals,
                sim.checks,
                sim.blockers,
                sim.plan_sha256,
            )
            request = importer.ApplyRequest(
                main, sheet.name, chosen, sim.plan_sha256, None, other, other_sheet.name if other_sheet else ""
            )
            initial = _counts(settings)
            _cut(result, database, settings, request, len(sim.rows))
            started = time.monotonic()
            try:
                applied = importer.apply(database, settings, request)
            except (importer.ApplyRefused, importer.ApplyFailed) as exc:
                result.steps.append(Step("Import appliqué", False, f"{exc.message} {getattr(exc, 'details', '')}"))
                return result
            result.seconds["application"] = round(time.monotonic() - started, 2)
            result.reconciliation = applied.summary["reconciliation"]
            result.steps.append(
                Step(
                    "Import appliqué, réconcilié avant validation",
                    all(line["ok"] for line in applied.summary["reconciliation"]),
                    json.dumps(applied.summary["created"], ensure_ascii=False),
                )
            )
            before = _counts(settings)
            try:
                importer.apply(database, settings, request)
                result.steps.append(Step("Second import refusé", False, "accepté : défaut"))
            except importer.ApplyRefused as refused:
                result.steps.append(Step("Second import refusé", _counts(settings) == before, refused.message))
            with database.session(write=True) as session:
                exported = original.original_workbook(session, applied.batch_id)
            comparison = original.compare(
                main_bytes, exported.data, sheet.name, {m.cell: m.reason for m in exported.modifications}
            )
            result.comparison = {
                "sheet": comparison.sheet,
                "compared": comparison.compared,
                "identical": comparison.identical,
                "explained": [asdict(d) for d in comparison.explained],
                "unexplained": [asdict(d) for d in comparison.unexplained],
                "rows_initial": comparison.rows_initial,
                "rows_final": comparison.rows_final,
            }
            result.steps.append(
                Step(
                    "Aller-retour : format d'origine identique, cellule par cellule",
                    comparison.faithful,
                    f"{comparison.identical}/{comparison.compared} identiques, {len(comparison.explained)} expliquées, "
                    f"{len(comparison.unexplained)} inexpliquées",
                )
            )
            if other is not None and complement_bytes is not None and other_sheet is not None:
                checked, problems = original.compare_complement(
                    complement_bytes, other_sheet.name, exported.data, applied.summary
                )
                result.complement_check = {"checked": checked, "problems": [asdict(p) for p in problems]}
                result.steps.append(
                    Step(
                        "Colonnes ajoutées par le fichier complémentaire, restituées à l'identique",
                        not problems,
                        f"{checked} valeur(s), {len(problems)} écart(s)",
                    )
                )
            with database.session() as session:
                workbook_bytes = export.to_xlsx(session)
                archive = export.to_csv_zip(session)
            files = zipfile.ZipFile(io.BytesIO(archive))
            names = files.namelist()
            result.export_counts = {
                name[:-4]: max(
                    0, len(list(csv.reader(io.StringIO(files.read(name).decode("utf-8-sig")), delimiter=";"))) - 1
                )
                for name in names
            }
            copies = result.export_counts.get("exemplaires", 0)
            result.steps.append(
                Step(
                    "Export complet : un exemplaire par ligne importée",
                    copies == sim.totals["copies"] and len(workbook_bytes) > 0,
                    f"{copies} exemplaires, {len(names)} fichiers CSV",
                )
            )
        finally:
            database.dispose()
        restore_backup(settings, settings.backup_dir / applied.backup_name)
        restored = _counts(settings)
        result.steps.append(
            Step("Restauration de la sauvegarde prise avant l'import", restored == initial, str(restored))
        )
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    return result


def _cut(result: Recette, database: Database, settings: Settings, request: importer.ApplyRequest, rows: int) -> None:
    """Une coupure au milieu des lignes : rien ne doit rester, l'échec doit être consigné."""
    middle = max(1, rows // 2)

    def cut(stage: str, done: int) -> None:
        if stage == "row" and done == middle:
            raise ConnectionResetError("coupure simulée")

    before = _counts(settings)
    try:
        importer.apply(database, settings, request, checkpoint=cut)
        result.steps.append(Step("Coupure en plein import : tout annulé", False, "aucune coupure : défaut"))
    except importer.ApplyFailed:
        connection = sqlite3.connect(settings.database_path)
        failed = connection.execute("SELECT COUNT(*) FROM import_batches WHERE status = 'failed'").fetchone()[0]
        traces = connection.execute("SELECT COUNT(*) FROM import_rows").fetchone()[0]
        connection.close()
        result.steps.append(
            Step(
                "Coupure en plein import : tout annulé",
                _counts(settings) == before and failed == 1 and traces == 0,
                f"coupure à la ligne {middle} sur {rows} ; lot consigné « échoué »",
            )
        )


def markdown(result: Recette) -> str:
    t = result.totals
    lines = [
        f"# Recette aller-retour : {result.filename}",
        "",
        f"- Empreinte : `{result.sha256}`",
        f"- Complémentaire : {result.complement or '—'}",
        f"- Empreinte du plan : `{result.plan}`",
        f"- Durées : {result.seconds}",
        "",
        "## Totaux de la simulation",
        "",
    ]
    for key in ("rows_read", "rows_rejected", "copies", "books", "works", "series", "shared_call_numbers",
                "inverted_titles", "date_cells", "differences", "proposals", "distribution"):  # fmt: skip
        lines.append(f"- {key} : {t.get(key)}")
    lines += ["", "## Étapes", ""]
    lines += [f"- {'conforme' if s.ok else 'ÉCART'} : {s.label} — {s.detail}" for s in result.steps]
    lines += ["", "## Réconciliation", ""]
    lines += [
        f"- {'conforme' if r['ok'] else 'ÉCART'} : {r['label']} ({r['expected']} / {r['found']})"
        for r in result.reconciliation
    ]
    comparison = result.comparison
    if comparison:
        lines += ["", "## Comparaison cellule par cellule", ""]
        c = comparison
        lines.append(
            f"Feuille « {c['sheet']} » : {c['compared']} cellules comparées, {c['identical']} identiques "
            f"(valeur et type), {len(c['explained'])} différences expliquées, {len(c['unexplained'])} inexpliquées ; "
            f"lignes {c['rows_initial']} → {c['rows_final']}."
        )
        for kind in ("explained", "unexplained"):
            for d in comparison[kind]:
                lines.append(f"- {d['cell']} : « {d['initial']} » → « {d['final']} » ({d['reason'] or 'inexpliquée'})")
    if result.complement_check:
        lines += ["", f"Fichier complémentaire : {result.complement_check['checked']} valeur(s) vérifiée(s), "
                  f"{len(result.complement_check['problems'])} écart(s)."]  # fmt: skip
    lines += ["", "## Export complet", ""]
    lines += [f"- {name} : {count}" for name, count in result.export_counts.items()]
    lines += ["", f"Résultat : {'CONFORME' if result.ok else 'ÉCARTS'}", ""]
    return "\n".join(lines)
