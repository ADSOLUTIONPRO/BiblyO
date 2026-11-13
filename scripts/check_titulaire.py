#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Mention de titulaire : la même dans NOTICE, README.md et l'application, suivie de « Licensed under the EUPL-1.2 ».

`--publication` refuse en plus une mention dont le nom n'est pas renseigné : une version ne part pas sans titulaire.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "[NOM CIVIL DU TITULAIRE]"
LICENSED = "Licensed under the EUPL-1.2"
_MENTION = re.compile(r"Copyright © (\d{4}) (.+)")


def declared(root: Path = ROOT) -> str:
    match = re.search(r'^__copyright__ = "(.+)"$', (root / "src" / "biblyo" / "__init__.py").read_text("utf-8"), re.M)
    return match.group(1) if match else ""


def check(root: Path = ROOT, publication: bool = False) -> list[str]:
    mention = declared(root)
    if not _MENTION.fullmatch(mention):
        return ["src/biblyo/__init__.py : __copyright__ absent ou mal formé (« Copyright © AAAA Nom »)"]
    problems = []
    for name in ("NOTICE", "README.md"):
        lines = (root / name).read_text("utf-8").splitlines()
        if mention not in lines:
            problems.append(f"{name} : mention « {mention} » absente")
        elif LICENSED not in lines[lines.index(mention) + 1 : lines.index(mention) + 3]:
            problems.append(f"{name} : « {LICENSED} » doit suivre la mention de copyright")
    if not (root / "LICENSE").read_text("utf-8").startswith("EUROPEAN UNION PUBLIC LICENCE v. 1.2"):
        problems.append("LICENSE : texte officiel de l'EUPL-1.2 attendu")
    if publication and PLACEHOLDER in mention:
        problems.append(f"Titulaire non renseigné ({PLACEHOLDER}) : publication refusée")
    return problems


def main(argv: list[str]) -> int:
    problems = check(publication="--publication" in argv)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(f"Titulaire : « {declared()} », {LICENSED}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
