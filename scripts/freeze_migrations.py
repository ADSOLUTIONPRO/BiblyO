#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Fige l'empreinte des révisions : une révision publiée ne change plus (`--check` pour contrôler)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "src" / "biblyo" / "migrations" / "versions"
LEDGER = ROOT / "src" / "biblyo" / "migrations" / "checksums.json"


def digests(versions: Path = VERSIONS) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(versions.glob("*.py"))}


def check(versions: Path = VERSIONS, ledger: Path = LEDGER) -> list[str]:
    known = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}
    actual = digests(versions)
    problems = [f"{name}: révision publiée modifiée" for name, digest in known.items() if actual.get(name) != digest]
    problems += [f"{name}: révision non figée (scripts/freeze_migrations.py)" for name in actual if name not in known]
    return problems


def main() -> int:
    if "--check" in sys.argv:
        problems = check()
        for problem in problems:
            print(problem)
        if problems:
            return 1
        print("Révisions : conformes à leur empreinte.")
        return 0
    LEDGER.write_text(json.dumps(digests(), indent=2) + "\n", encoding="utf-8")
    print(f"Empreintes écrites : {LEDGER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
