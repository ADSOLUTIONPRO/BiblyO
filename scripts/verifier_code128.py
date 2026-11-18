#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Vérification exhaustive de la table du Code 128 de biblyO : chacun des 107 motifs relu par un décodeur indépendant.

    uv run --frozen python scripts/verifier_code128.py <dossier>
    swift scripts/verifier_codes_vision.swift <dossier>/code128-107-motifs.pdf 600 > <dossier>/vision-107.txt
    uv run --frozen python scripts/verifier_code128.py <dossier> --comparer <dossier>/vision-107.txt

Un décodeur ne rend un code que si chaque symbole **et** la clé modulo 103 sont justes. Les codes d'essai sont choisis
pour que chaque valeur de 0 à 102 soit au moins une fois la clé d'un code (lettres seules, jeu B), et que les départs A,
B et C, les changements de jeu et l'arrêt y figurent : un motif faux ferait échouer au moins un code.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from biblyo.core.barcodes import CODE128_PATTERNS, STOP, Symbol, _bars, code128, code128_values
from biblyo.core.pdf import Document

LETTERS = "ABCDEFGHJKLMNPRSTUVWXY"  # jeu B sans chiffres : aucun passage au jeu C


def checksum_cases() -> dict[int, str]:
    """Pour chaque valeur v de 0 à 102, un texte de lettres dont la clé du Code 128 vaut v."""
    found: dict[int, str] = {}
    for first in LETTERS:
        for second in LETTERS:
            for third in LETTERS:
                text = f"{first}{second}{third}"
                key = code128_values(text)[-1]
                found.setdefault(key, text)
        if len(found) == 103:
            break
    missing = sorted(set(range(103)) - set(found))
    assert not missing, f"clés non couvertes : {missing}"
    return found


def start_a(text: str) -> Symbol:
    """Un code en jeu A (départ 103), construit à la main : le seul départ que l'encodeur de biblyO n'emploie pas."""
    values = [103, *(ord(character) - 32 for character in text)]
    key = (values[0] + sum(position * value for position, value in enumerate(values[1:], start=1))) % 103
    bars, width = _bars("".join(CODE128_PATTERNS[value] for value in [*values, key, STOP]))
    return Symbol(text, "code128", bars, width, 10, 10)


def cases() -> list[tuple[str, Symbol, str]]:
    out = [(f"clé {key}", code128(text), text) for key, text in sorted(checksum_cases().items())]
    out.append(("départ A (103)", start_a("ABC"), "ABC"))
    for first in range(0, 100, 20):  # jeu C : les cent paires 00 à 99, vingt par code (40 chiffres)
        digits = "".join(f"{n:02d}" for n in range(first, first + 20))
        out.append((f"départ C (105), paires {first:02d} à {first + 19:02d}", code128(digits), ""))
    out.append(("passages B → C → B (99, 100)", code128("AB123456CD"), "AB123456CD"))
    return [(name, symbol, expected or symbol.text) for name, symbol, expected in out]


def render(folder: Path) -> None:
    document = Document("Code 128 : les 107 motifs")
    page = None
    for index, (_name, symbol, _expected) in enumerate(cases()):
        if index % 8 == 0:
            page = document.add_page()
        assert page is not None
        module = 0.33
        top = 15 + (index % 8) * 34
        left = 15 + symbol.quiet_left * module
        page.bars([(left + start * module, top, width * module, 18) for start, width in symbol.bars])
    (folder / "code128-107-motifs.pdf").write_bytes(document.render())
    lines = [f"{name}\t{expected}" for name, _symbol, expected in cases()]
    (folder / "code128-attendus.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    covered = {STOP, 103}  # l'arrêt termine chaque code ; le départ A est construit à la main
    for name, symbol, _expected in cases():
        if not name.startswith("départ A"):
            covered.update(code128_values(symbol.text))
    missing = sorted(set(range(107)) - covered)
    assert not missing, f"valeurs non couvertes : {missing}"
    print(f"{len(lines)} codes d'essai écrits ; les 107 valeurs de la table y figurent.")


def compare(folder: Path, vision: Path) -> int:
    expected = [line.split("\t") for line in (folder / "code128-attendus.txt").read_text(encoding="utf-8").splitlines()]
    read = set(re.findall(r"Code128:(\S+)", vision.read_text(encoding="utf-8")))
    report = []
    failures = 0
    for name, text in expected:
        ok = text in read
        failures += not ok
        report.append(f"{'conforme' if ok else 'ÉCART'} : {name} → « {text} »")
    report.append(f"{len(expected) - failures}/{len(expected)} codes relus par Vision ; les 107 motifs de la table "
                  f"sont {'tous vérifiés' if not failures else 'PAS tous vérifiés'}.")  # fmt: skip
    (folder / "code128-107-motifs.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(report[-1])
    return 1 if failures else 0


if __name__ == "__main__":
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    if "--comparer" in sys.argv:
        sys.exit(compare(target, Path(sys.argv[sys.argv.index("--comparer") + 1])))
    render(target)
