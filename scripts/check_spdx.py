#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Chaque fichier source porte son en-tête SPDX ; un seul identifiant, modifiable par script."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = "SPDX-License-Identifier: EUPL-1.2"
PATTERNS = (
    "src/**/*.py",
    "tests/**/*.py",
    "scripts/*.py",
    "scripts/*.sh",
    "scripts/hooks/*",
    "src/biblyo/static/js/**/*.js",
    "src/biblyo/static/src/*.css",
)


def check(root: Path = ROOT) -> list[str]:
    problems = []
    for pattern in PATTERNS:
        for path in root.glob(pattern):
            head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:3])
            if HEADER not in head:
                problems.append(f"{path.relative_to(root)}: en-tête SPDX absent")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print("En-têtes SPDX : complets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
