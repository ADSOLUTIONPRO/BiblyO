#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Catalogue des textes à jour, et aucun terme d'interface en anglais (`--write` pour régénérer le catalogue)."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "locales" / "messages.pot"
ENGLISH = re.compile(
    r"\b(?:Save|Cancel|Delete|Edit|Login|Log in|Logout|Log out|Password|Username|Search|Settings|Users?|Home|Submit"
    r"|Error|Loading|Back|Next|Previous|Close|Yes|No|Please|Welcome|Dashboard|Account|Sign in|Sign out)\b"
)


def extract(target: Path) -> None:
    command = [
        sys.executable, "-m", "babel.messages.frontend", "extract", "-F", "babel.cfg", "--no-location", "--sort-output",
        "--project", "biblyO", "--copyright-holder", "Projet biblyO", "-k", "_", "-k", "gettext", "-k", "ngettext:1,2",
        "-o", str(target), "src",
    ]  # fmt: skip
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True)


def messages(path: Path) -> set[str]:
    with path.open("rb") as handle:
        catalogue = read_po(handle)
    found: set[str] = set()
    for message in catalogue:
        if message.id:
            found.update(message.id if isinstance(message.id, tuple) else (message.id,))
    return found


def english_terms(texts: set[str]) -> list[str]:
    return sorted(text for text in texts if ENGLISH.search(text))


def check() -> list[str]:
    if not CATALOGUE.exists():
        return [f"{CATALOGUE}: catalogue absent (make i18n)"]
    with tempfile.TemporaryDirectory() as workdir:
        fresh = Path(workdir) / "fresh.pot"
        extract(fresh)
        current = messages(fresh)
    if not current:
        return ["aucun texte extrait : la configuration de babel.cfg ne trouve plus les fichiers"]
    known = messages(CATALOGUE)
    problems = [f"texte absent du catalogue (make i18n) : {text}" for text in sorted(current - known)]
    problems += [f"texte disparu du code (make i18n) : {text}" for text in sorted(known - current)]
    problems += [f"terme anglais dans l'interface : {text}" for text in english_terms(current)]
    return problems


def main() -> int:
    if "--write" in sys.argv:
        CATALOGUE.parent.mkdir(exist_ok=True)
        extract(CATALOGUE)
        print(f"Catalogue écrit : {CATALOGUE} ({len(messages(CATALOGUE))} textes)")
        return 0
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(f"Catalogue : à jour, {len(messages(CATALOGUE))} textes en français.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
