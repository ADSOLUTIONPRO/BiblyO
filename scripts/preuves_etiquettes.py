#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Preuves du lot L8 : un PDF par gabarit, une feuille entamée et les feuilles test, avec des données inventées.

    uv run --frozen python scripts/preuves_etiquettes.py <dossier>

Écrit les PDF dans le dossier, puis `mesures.txt` : format de chaque page, et pour chaque étiquette sa case attendue
(d'après le gabarit) et la position réellement mesurée de son code dans le PDF, relue par `tests/pdf_reader.py`. La
relecture par un décodeur indépendant (Vision, macOS) se fait ensuite avec `scripts/verifier_codes_vision.swift`.
Aucune donnée de bibliothèque réelle n'est lue.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import pdf_reader  # noqa: E402

from biblyo.modules.labels import layout, render, sheets  # noqa: E402
from biblyo.modules.labels.layout import Appearance, LabelData  # noqa: E402

SERIES = ("Les Tuniques bleues", "Spirou et Fantasio", "Lucky Luke", "Les Schtroumpfs", "Boule et Bill")


def books(count: int, start: int = 1) -> list[LabelData]:
    items = []
    for number in range(start, start + count):
        series = SERIES[number % len(SERIES)]
        volume = number % 40 + 1
        items.append(
            LabelData(
                f"CSN{number:06d}", f"{series}, tome {volume} : un titre inventé pour l'essai", series, str(volume),
                f"BD {series[:3].upper()} {volume}", str(100 + number), "Salle BD / Meuble 2 / Étagère 3", number,
            )
        )  # fmt: skip
    return items


CASES = (
    ("avery-l7160", "standard", 21, frozenset(), "Format principal du Club (BD), feuille neuve"),
    ("avery-l7160", "detaille", 18, frozenset(range(1, 8)), "Format principal, feuille entamée (cases 1 à 7)"),
    ("avery-l7651", "compact", 65, frozenset(), "Format compact du Club, feuille neuve"),
    ("avery-l7651", "standard", 80, frozenset({1, 2, 3, 9}), "Format compact, deux feuilles, la première entamée"),
    ("avery-l7159", "standard", 24, frozenset(), "Autre référence (calibration nécessaire)"),
    ("avery-l7654", "compact", 40, frozenset(), "Autre référence (calibration nécessaire)"),
)


def main() -> int:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for code, model, count, occupied, note in CASES:
        sheet = sheets.KNOWN_BY_CODE[code]
        look = Appearance(layout.MODELS[model][1], "Club d'essai", False)
        plan = layout.plan(books(count), look, sheet, occupied=occupied)
        name = f"etiquettes-{code}-{model}{'-entamee' if occupied else ''}.pdf"
        data = render.to_pdf(plan, title=f"Essai {sheet.name}")
        (out / name).write_bytes(data)
        pages = pdf_reader.pages(data)
        lines.append(f"== {name} : {sheet.name}, modèle {model}, {count} étiquettes. {note}.")
        lines.append(f"   gabarit : {sheet.summary} ; marges {sheet.top} / {sheet.left} mm ; pas {sheet.pitch_x} / "
                     f"{sheet.pitch_y} mm")  # fmt: skip
        placed = [item for page in plan.pages for item in page]
        codes = [found for page in pages for found in page.codes]
        worst = 0.0
        for item, found in zip(placed, codes, strict=True):
            assert found.text == item.data.code, (found.text, item.data.code)
            left_gap = found.x - item.x
            right_gap = item.x + sheet.width - (found.x + found.width)
            worst = max(worst, abs(left_gap - right_gap))
        for index, page in enumerate(pages, start=1):
            lines.append(f"   page {index} : {page.width:.3f} × {page.height:.3f} mm, {len(page.codes)} codes relus")
        first, last = codes[0], codes[-1]
        lines.append(f"   premier code {first.text} : case {placed[0].position}, barres à x = {first.x:.2f} mm, "
                     f"y = {first.y:.2f} mm, module {first.module:.4f} mm, hauteur {first.height:.2f} mm")  # fmt: skip
        lines.append(f"   dernier code {last.text} : feuille {placed[-1].sheet_number}, case {placed[-1].position}")
        lines.append(f"   écart de centrage horizontal le plus grand : {worst:.4f} mm ; tous les codes = ceux attendus")
        test = render.test_sheet_pdf(sheet)
        if (out / f"feuille-test-{code}.pdf").exists():
            continue
        (out / f"feuille-test-{code}.pdf").write_bytes(test)
        (page,) = pdf_reader.pages(test)
        found = [c.text or "EAN-13 (relu par Vision : vision.txt)" for c in page.codes]
        lines.append(f"   feuille test : {page.width:.3f} × {page.height:.3f} mm, codes {found}")
    (out / "mesures.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
