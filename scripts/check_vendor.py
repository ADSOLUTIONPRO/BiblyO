#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Bibliothèques et polices tierces : empreintes vérifiées, aucun fichier en plus ni en moins (`--write` pour figer)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "biblyo" / "static"
LEDGER = STATIC / "vendor" / "checksums.sha256"
FOLDERS = ("vendor", "fonts")


def digests(static: Path = STATIC, ledger_name: str = LEDGER.name) -> dict[str, str]:
    found = {}
    for folder in FOLDERS:
        for path in sorted((static / folder).rglob("*")):
            if path.is_file() and path.name != ledger_name:
                found[path.relative_to(static).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def read_ledger(ledger: Path) -> dict[str, str]:
    entries = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            entries[name] = digest
    return entries


def check(static: Path = STATIC, ledger: Path = LEDGER) -> list[str]:
    if not ledger.exists():
        return [f"{ledger}: registre d'empreintes absent"]
    expected = read_ledger(ledger)
    actual = digests(static, ledger.name)
    problems = [
        f"{name}: empreinte incorrecte" for name in expected if name in actual and actual[name] != expected[name]
    ]
    problems += [f"{name}: fichier absent" for name in expected if name not in actual]
    problems += [f"{name}: fichier non enregistré" for name in actual if name not in expected]
    return problems


def main() -> int:
    if "--write" in sys.argv:
        LEDGER.write_text("".join(f"{d}  {n}\n" for n, d in digests().items()), encoding="utf-8")
        print(f"Empreintes écrites : {LEDGER}")
        return 0
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print("Bibliothèques et polices : conformes à leurs empreintes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
