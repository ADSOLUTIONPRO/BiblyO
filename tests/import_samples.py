# SPDX-License-Identifier: EUPL-1.2
"""Classeurs fabriqués pour les essais de l'import (lot L6) : les formes relevées par l'audit, sans aucune donnée du
Club. Séries, titres, noms et éditeurs sont inventés ; les ISBN sont fabriqués avec une clé de contrôle valide."""

from __future__ import annotations

import io
from functools import lru_cache
from typing import Any

from openpyxl import Workbook

HEADERS = (
    "Titre", "Tome", "Sous-Titre", "Auteur", "Auteur 2", "Date d'acquisition", "Edition", "Etat", "Cote",
    "Numéro d'inventaire", "Collection", "Auteur 3", "Auteur 4",
)  # fmt: skip
EXTRA = ("ISBN (référence à vérifier)", "Année édition (à vérifier)")
SOURCES = (
    "Ligne source", "N° inventaire", "Ouvrage", "ISBN proposé", "Année de référence", "URL de la source",
    "Remarque / vérification",
)  # fmt: skip


def isbn13(stem: str) -> str:
    """ISBN-13 à clé valide, à partir de 12 chiffres."""
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(stem))
    return stem + str((10 - total % 10) % 10)


ISBN_A = isbn13("978200000001")
ISBN_B = isbn13("978200000002")
ISBN_C = isbn13("978200000003")

# Titre, Tome, Sous-Titre, Auteur, Auteur 2, Acquisition, Edition, Etat, Cote, Inventaire, Collection, Auteur 3, 4
ROWS: list[tuple[Any, ...]] = [
    (
        "Marins (Les)",
        1,
        "La mer d'huile",
        "Jean Dupré",
        "Paul Marin",
        "inf. 2026",
        "DARGOT",
        "satisfaisant",
        "MAR/01",
        1,
        None,
        None,
        None,
    ),
    (
        "Marins (Les)",
        2,
        "Le vent tourne",
        "Jean Dupré ",
        "Paul Marin",
        "inf. 2026",
        "DARGOT",
        "leg. usure",
        "MAR/02",
        2,
        None,
        None,
        None,
    ),
    (
        "Marins (Les)",
        2,
        "Le vent tourne",
        "Jean Dupré",
        "Paul Marin",
        "inf. 2026",
        "DARGOT",
        "satisfaisant",
        "MAR/02",
        3,
        None,
        None,
        None,
    ),
    (
        "agent 99 (L')",
        4,
        "Coup de sifflet",
        "Gil Tournesol",
        None,
        "inf. 2026",
        "DUPONT",
        "usé",
        "AGE/04",
        4,
        None,
        None,
        None,
    ),
    (
        "histoire du phare (une)",
        None,
        None,
        "Anne Lune",
        None,
        "inf. 2026",
        "PHARE ÉDITIONS",
        "satisfaisant",
        "HIS/01",
        5,
        None,
        None,
        None,
    ),
    (
        "Comète (Le petit)",
        1,
        "Premier vol",
        "Rémi Astre",
        None,
        "inf. 2026",
        "DUPONT",
        "satisfaisant",
        "COM/01",
        6,
        None,
        None,
        None,
    ),
    (
        "Navire",
        20.21,
        "Deux voyages ; Le retour",
        "Hervé Cap",
        None,
        "inf. 2026",
        "DUPONT",
        "satisfaisant",
        "NAV/20,21",
        7,
        None,
        None,
        None,
    ),
    (
        "Feuilleton",
        "ep 1-4",
        "La guerre des encres",
        "Ida Plume",
        None,
        "inf. 2026",
        "PLUME",
        "nettoyable",
        "FEU/1-4",
        8,
        None,
        None,
        None,
    ),
    (
        "Bobine",
        1,
        "Le fil rouge",
        "Soria, Carlos R.",
        "Eric & Sam",
        "inf. 2026",
        "WALT DISNEY",
        "satisfaisant",
        "BOB/01",
        9,
        "double albums",
        None,
        None,
    ),
    (
        "Bobard",
        1,
        "Le grand mensonge",
        "J.C. Martin",
        "Leo",
        "inf. 2026",
        "DISNEY",
        "satisfaisant",
        "BOB/01",
        10,
        "Collection intégrale",
        None,
        "S.E.D.L.I.",
    ),
    (
        "Bobard",
        1,
        "Un autre mensonge",
        "J-C Martin",
        "Léo",
        "inf. 2026",
        "Dargot",
        "satisfaisant",
        "BOB/02",
        11,
        "hs",
        None,
        None,
    ),
    (
        "Jacques",
        8,
        "La huitième",
        "\tAtelier Crayon",
        None,
        "inf. 2026",
        "ALBUM DOUBLE",
        "satisfaisant",
        "JAC/04",
        12,
        "Banque Mutuelle",
        None,
        None,
    ),
    (
        "Chevalier Vaillant",
        8,
        "L'épée",
        "Max Fer",
        None,
        "inf. 2026",
        "DUPONT",
        "satisfaisant",
        "JUN/008",
        13,
        404,
        None,
        None,
    ),
    ("Chevalier vaillant", 2, None, "Max Fer", None, " ", "DUPONT", "à revoir", "CHE/02", 14, None, "Ana Fer", None),
    ("Donjon", 2, "Les rives", None, None, None, None, None, "DON/02", 15, None, None, None),
    ("Donjon", 3, "La mort", None, None, None, None, None, "DON/03", 16, None, None, None),
    ("Recueil", 5, "Cinq fois", "Ola Cinq", None, "inf. 2026", "CINQ", "satisfaisant", "REC/05", 17, None, None, None),
    ("Recueil", 5, "Cinq fois", "Ola Cinq", None, "inf. 2026", "CINQ", "satisfaisant", "REC/05", 18, None, None, None),
    ("Recueil", 5, "Cinq fois", "Ola Cinq", None, "inf. 2026", "CINQ", "satisfaisant", "REC/05", 19, None, None, None),
    ("Recueil", 5, "Cinq fois", "Ola Cinq", None, "inf. 2026", "CINQ", "satisfaisant", "REC/05", 20, None, None, None),
    ("Recueil", 5, "Cinq fois", "Ola Cinq", None, "inf. 2026", "CINQ", "satisfaisant", "REC/05", 21, None, None, None),
    ("Jumeaux", 1, "Le double", "Zoé Deux", None, "inf. 2026", "DEUX", "satisfaisant", "JUM/01", 22, None, None, None),
    ("Jumeaux", 1, "Le double", "Zoé Deux", None, "inf. 2026", "DEUX", "satisfaisant", "JUM/01", 23, None, None, None),
]
#: Ce que le fichier enrichi ajoute : inventaire → (ISBN, année), et les cellules qu'il remplit sans le dire.
ENRICHED: dict[int, tuple[str, int | None]] = {
    1: (ISBN_A, 1990),
    2: (ISBN_B, None),
    3: (ISBN_B, None),
    22: (ISBN_A, 1991),
    23: (ISBN_C, 1992),
}
SILENT: dict[int, dict[str, str]] = {
    15: {"Auteur": "Rita Rive", "Auteur 2": "Hugo Rive"},
    16: {"Auteur": "Rita Rive", "Auteur 2": "Hugo Rive", "Edition": "RIVE"},
}
DATE_CELL = "I14"  # « JUN/008 », texte au format date


def workbook(*, enriched: bool = False, sources: bool | str = True, rows: list[tuple[Any, ...]] | None = None) -> bytes:
    """Mêmes arguments, mêmes octets : openpyxl date chaque fichier à la seconde, et deux classeurs fabriqués à une
    seconde d'écart auraient deux empreintes, donc deux plans. Le classeur est donc fabriqué une fois par processus."""
    return _workbook(enriched, sources, tuple(tuple(r) for r in rows) if rows is not None else None)


@lru_cache(maxsize=64)
def _workbook(enriched: bool, sources: bool | str, rows: tuple[tuple[Any, ...], ...] | None) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Feuil1"
    sheet.append(list(HEADERS) + (list(EXTRA) if enriched else []))
    for values in rows if rows is not None else ROWS:
        line = list(values)
        inventory = line[HEADERS.index("Numéro d'inventaire")]
        if enriched:
            for header, value in SILENT.get(inventory, {}).items():
                line[HEADERS.index(header)] = value
            isbn, year = ENRICHED.get(inventory, (None, None))
            line += [isbn, year]
        sheet.append(line)
    sheet[DATE_CELL].number_format = "mmm-yy"
    if enriched and sources:
        extra = book.create_sheet("Sources et vérifications")
        extra.append(list(SOURCES))
        for inventory, (isbn, year) in ENRICHED.items():
            line_number = inventory + 1 if sources is True else inventory + 5
            extra.append(
                [line_number, inventory, "ouvrage", isbn, year, f"https://exemple.invalid/{inventory}", "à vérifier"]
            )
        for _ in range(3):
            extra.append([None] * len(SOURCES))
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
