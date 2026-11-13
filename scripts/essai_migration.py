# SPDX-License-Identifier: EUPL-1.2
"""Essai d'une révision sur une base remplie : jeu synthétique, descente, remontée, empreintes comparées.

Le jeu est celui de `perf_catalogue.py` (même graine). La base est écrite dans un dossier jetable, jamais dans une
installation : aucune donnée réelle n'est lue ni modifiée.

    uv run python scripts/essai_migration.py /tmp/essai --notices 100000 --depuis 0005 --vers 0006
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import perf_catalogue


def fingerprint(path: Path) -> dict[str, Any]:
    """Comptes, empreinte du noyau des exemplaires, intégrité. Le noyau ne doit jamais changer."""
    connection = sqlite3.connect(path)
    try:
        digest = hashlib.sha256()
        for row in connection.execute(
            "SELECT id, public_id, edition_id, barcode, status, notes, created_at, updated_at FROM copies ORDER BY id"
        ):
            digest.update(repr(row).encode())

        def count(table: str) -> int:
            return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608 : constante

        return {
            "revision": connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            "oeuvres": count("works"),
            "editions": count("editions"),
            "exemplaires": count("copies"),
            "empreinte_exemplaires": digest.hexdigest(),
            "integrite": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "cles_etrangeres": connection.execute("PRAGMA foreign_key_check").fetchall(),
            "copies_strict": connection.execute(
                "SELECT strict FROM pragma_table_list WHERE name = 'copies'"
            ).fetchone()[0],
        }
    finally:
        connection.close()


def main() -> int:
    from alembic import command

    from biblyo.core.migrate import alembic_config, schema_differences

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path, help="dossier jetable, qui ne doit pas exister")
    parser.add_argument("--notices", type=int, default=100_000)
    parser.add_argument("--graine", type=int, default=20261108)
    parser.add_argument("--depuis", default="0005", help="révision où redescendre")
    parser.add_argument("--vers", default="head", help="révision où remonter")
    options = parser.parse_args()
    if options.dossier.exists():
        parser.error("le dossier existe déjà : choisissez un dossier neuf")

    started = time.perf_counter()
    report: dict[str, Any] = {"jeu": perf_catalogue.generate(options.dossier, options.notices, options.graine)}
    report["generation_s"] = round(time.perf_counter() - started, 1)
    settings = perf_catalogue._settings(options.dossier)
    config = alembic_config(settings)
    report["tete"] = fingerprint(settings.database_path)
    started = time.perf_counter()
    command.downgrade(config, options.depuis)
    report["descente_s"] = round(time.perf_counter() - started, 2)
    report["apres_descente"] = fingerprint(settings.database_path)
    started = time.perf_counter()
    command.upgrade(config, options.vers)
    report["montee_s"] = round(time.perf_counter() - started, 2)
    report["apres_montee"] = fingerprint(settings.database_path)
    report["ecarts_de_schema"] = [str(item) for item in schema_differences(settings)]
    report["taille_mio"] = round(settings.database_path.stat().st_size / 2**20, 1)
    prints = {report[key]["empreinte_exemplaires"] for key in ("tete", "apres_descente", "apres_montee")}
    report["empreintes_identiques"] = len(prints) == 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    healthy = all(report[key]["integrite"] == "ok" for key in ("tete", "apres_descente", "apres_montee"))
    return 0 if report["empreintes_identiques"] and healthy and not report["ecarts_de_schema"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
