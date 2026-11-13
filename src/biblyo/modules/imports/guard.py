# SPDX-License-Identifier: EUPL-1.2
"""Garde contre la reconstruction de l’ancienne application (décision D-L6-1, arbitrage du 13 novembre 2026).

La reconstruction classée produite par l’ancienne application n'est pas le fichier du Club : sa colonne
« Ligne Excel » ne désigne pas la bonne ligne, ses numéros d'inventaire ont été renumérotés, et ses codes `CSN-…`
sont artificiels. Elle ne doit servir **ni** à créer des exemplaires, **ni** à attribuer des identifiants, **ni** de
fichier complémentaire (ses inventaires renumérotés produiraient de fausses différences). Le dépôt la refuse, la
simulation la bloque, l'application la refuse encore : trois verrous, pour qu'un fichier pris par erreur ne passe
jamais.

La reconnaissance repose sur la forme du fichier (en-têtes, feuilles, codes), jamais sur des données du Club.
"""

from __future__ import annotations

import re

from biblyo.core.i18n import _
from biblyo.core.text import normalize
from biblyo.modules.imports.reader import Workbook

#: En-têtes propres à la reconstruction (comparés sans casse, accents ni ponctuation).
HEADERS = frozenset({"ligne excel", "code barres futur", "etagere virtuelle", "position virtuelle", "type shelf"})
#: Feuilles propres à la reconstruction.
SHEETS = frozenset({"etageres virtuelles"})
#: Codes artificiels de la reconstruction : jamais des codes-barres officiels (D-L6-7, règle 4).
ARTIFICIAL_CODE = re.compile(r"^\s*CSN-\d+\s*$", re.IGNORECASE)


def is_artificial_code(value: object) -> bool:
    return isinstance(value, str) and ARTIFICIAL_CODE.match(value) is not None


def legacy_reconstruction(workbook: Workbook) -> list[str]:
    """Signes qui font reconnaître la reconstruction de l’ancienne application ; vide pour un autre fichier."""
    signs: list[str] = []
    for sheet in workbook.sheets:
        if normalize(sheet.name) in SHEETS:
            signs.append(_("feuille « {name} »").format(name=sheet.name))
        for column in sheet.columns:
            if normalize(column.header) in HEADERS:
                signs.append(_("colonne « {header} » ({sheet})").format(header=column.header, sheet=sheet.name))
        codes = sum(1 for row in sheet.rows for value in row.values if is_artificial_code(value))
        if codes:
            signs.append(_("{n} code(s) « CSN-… » ({sheet})").format(n=codes, sheet=sheet.name))
    return signs


def refusal(workbook: Workbook) -> str:
    """Message de refus, vide si le fichier n'est pas la reconstruction."""
    signs = legacy_reconstruction(workbook)
    if not signs:
        return ""
    return _(
        "« {file} » ressemble à la reconstruction de l’ancienne application ({signs}). Elle ne peut servir ni de "
        "source, ni de fichier complémentaire : déposez le classeur original du Club (décision D-L6-1)."
    ).format(file=workbook.filename, signs=", ".join(signs[:4]))
