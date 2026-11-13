#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Contrôle d'indépendance par identifiants (docs/PROVENANCE.md ; complété par scripts/check_provenance.py).

Refuse dans `src/` et `tests/` les identifiants distinctifs d'un logiciel antérieur, le nom d'une
maquette de travail remplacée, et tout fichier portant un nom historique.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("src", "tests")
SKIPPED_DIRS = {"__pycache__", "vendor", "fonts", "dist", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
TEXT_SUFFIXES = {
    ".py",
    ".html",
    ".js",
    ".css",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".mako",
    ".json",
    ".svg",
}
# Les motifs sont assemblés pour que ce fichier ne se signale pas lui-même.
_OLD = "She" + "lf"
FORBIDDEN = {
    rf"\b{_OLD}\b": f"nom du logiciel antérieur ({_OLD})",
    rf"\b{_OLD.upper()}_\w+": "variable d'environnement du logiciel antérieur",
    r"\bdgaha" + "gan\\b": "dépôt du logiciel antérieur",
    r"\bitems_" + "live\\b": "identifiant interne du logiciel antérieur",
    r"\bcopies_" + "live\\b": "identifiant interne du logiciel antérieur",
    r"\bitem_" + "copies\\b": "identifiant interne du logiciel antérieur",
    r"\binsert_" + "item\\b": "identifiant interne du logiciel antérieur",
    r"\bupdate_item_" + "fields\\b": "identifiant interne du logiciel antérieur",
    r"\bprovider_" + "result\\b": "identifiant interne du logiciel antérieur",
    r"\bnav_" + "tabs\\b": "identifiant interne du logiciel antérieur",
    r"\bcheck" + "outs\\b": "table du logiciel antérieur",
    r"\bborr" + "owers\\b": "table du logiciel antérieur",
    r"\blent_" + "to\\b": "identifiant interne du logiciel antérieur",
    r"\bshelf[-_]" + "fr\\b": "ancien module de traduction",
    r"\b(?:advanced|loans|review)_" + "fr\\b": "ancien module français",
    r"\bsn-(?:btn|hero|nav|panel|brand)\b": "ancien thème",
    r"\bRay" + "on\\b": "nom d'une maquette de travail remplacée",
}
HISTORICAL_FILES = re.compile(
    r"^(patch_[a-z0-9_]+|boot" + r"strap(-old)?|seed_" + r"fresh|update_templates|upgrade_source)\.py$"
    r"|^" + _OLD.lower() + r"-fr\.js$"
)


def check(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    compiled = [(re.compile(pattern), why) for pattern, why in FORBIDDEN.items()]
    for top in SCANNED:
        for dirpath, dirnames, filenames in os.walk(root / top):
            dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS]
            for name in filenames:
                path = Path(dirpath, name)
                rel = path.relative_to(root)
                if HISTORICAL_FILES.match(name):
                    problems.append(f"{rel}: fichier au nom historique")
                if path.suffix not in TEXT_SUFFIXES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for lineno, line in enumerate(text.splitlines(), 1):
                    for pattern, why in compiled:
                        if pattern.search(line):
                            problems.append(f"{rel}:{lineno}: {why}")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        print("Contrôle d'indépendance : refusé (docs/PROVENANCE.md).")
        return 1
    print("Contrôle d'indépendance : aucun identifiant ni fichier hérité.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
