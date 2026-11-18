# SPDX-License-Identifier: EUPL-1.2
"""Symbologies Code 128 (ISO/IEC 15417) et EAN-13 (ISO/IEC 15420), écrites pour biblyO à partir des normes publiques.

Un symbole est une suite de **modules** : chaque barre et chaque espace mesure un nombre entier de modules.
Le dessin (PDF, SVG) choisit la largeur d'un module en millimètres ; rien ici ne dépend d'une unité.

Code 128 n'emploie que les jeux B (caractères imprimables) et C (paires de chiffres) : le chemin le plus court entre les
deux est calculé, si bien qu'un code `EX-000123` tient en 112 modules au lieu de 134.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Largeurs des 107 symboles du Code 128 : barre, espace, barre, espace, barre, espace (11 modules), arrêt (13).
CODE128_PATTERNS: tuple[str, ...] = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
)  # fmt: skip
CODE_B, CODE_C = 100, 99  # changement de jeu, valeur du symbole dans le jeu de départ
START_B, START_C, STOP = 104, 105, 106
QUIET_ZONE_MODULES = 10  # ISO/IEC 15417 : au moins 10 modules de blanc de chaque côté
MAX_CODE128_LENGTH = 48


class BarcodeError(ValueError):
    """Le texte ne peut pas être représenté dans cette symbologie."""


@dataclass(frozen=True)
class Symbol:
    """Un code dessinable : barres (début, largeur) en modules, largeur totale sans les zones blanches."""

    text: str
    symbology: str
    bars: tuple[tuple[int, int], ...]
    width: int
    quiet_left: int
    quiet_right: int
    guards: tuple[tuple[int, int], ...] = ()  # EAN-13 : barres de garde, plus longues que les autres

    @property
    def total_width(self) -> int:
        """Largeur avec les zones blanches : la place que le code exige sur l'étiquette."""
        return self.quiet_left + self.width + self.quiet_right


def _bars(widths: str) -> tuple[tuple[tuple[int, int], ...], int]:
    """Barres d'une suite de largeurs qui commence par une barre et alterne barre et espace."""
    bars: list[tuple[int, int]] = []
    position = 0
    for index, character in enumerate(widths):
        width = int(character)
        if index % 2 == 0:
            bars.append((position, width))
        position += width
    return tuple(bars), position


def _plan_code128(text: str) -> list[int]:
    """Valeurs des symboles (départ, données, sans la clé ni l'arrêt) par le chemin le plus court entre B et C."""
    size = len(text)
    infinity = 10**9
    # cost[i][s] : symboles nécessaires pour coder text[i:] en étant dans le jeu s (0 = B, 1 = C)
    cost = [[infinity, infinity] for _ in range(size + 1)]
    step: list[list[tuple[int, int] | None]] = [[None, None] for _ in range(size + 1)]
    cost[size] = [0, 0]
    for i in range(size - 1, -1, -1):
        pair = text[i : i + 2]
        for current in (0, 1):
            options: list[tuple[int, int, int]] = []  # (coût, avance, jeu suivant)
            if current == 0:
                options.append((1 + cost[i + 1][0], 1, 0))
            elif len(pair) == 2 and pair.isdigit():
                options.append((1 + cost[i + 2][1], 2, 1))
            other = 1 - current
            if other == 0:
                options.append((2 + cost[i + 1][0], 1, 0))
            elif len(pair) == 2 and pair.isdigit():
                options.append((2 + cost[i + 2][1], 2, 1))
            if options:
                best = min(options)
                cost[i][current] = best[0]
                step[i][current] = (best[1], best[2])
    start = 0 if cost[0][0] <= cost[0][1] else 1
    if cost[0][start] >= infinity:
        raise BarcodeError("Texte impossible à coder en Code 128.")
    values = [START_B if start == 0 else START_C]
    i, current = 0, start
    while i < size:
        chosen = step[i][current]
        assert chosen is not None
        advance, following = chosen
        if following != current:
            values.append(CODE_C if following == 1 else CODE_B)
            current = following
        values.append(int(text[i : i + 2]) if current == 1 else ord(text[i]) - 32)
        i += advance
    return values


def code128(text: str) -> Symbol:
    """Le symbole Code 128 de `text` : caractères ASCII imprimables seulement (espace à `~`)."""
    if not text or len(text) > MAX_CODE128_LENGTH:
        raise BarcodeError("Un code compte de 1 à 48 caractères.")
    if any(not 32 <= ord(character) <= 126 for character in text):
        raise BarcodeError("Le Code 128 de biblyO n'accepte que des caractères ASCII imprimables.")
    values = _plan_code128(text)
    checksum = (values[0] + sum(position * value for position, value in enumerate(values[1:], start=1))) % 103
    widths = "".join(CODE128_PATTERNS[value] for value in [*values, checksum, STOP])
    bars, width = _bars(widths)
    return Symbol(text, "code128", bars, width, QUIET_ZONE_MODULES, QUIET_ZONE_MODULES)


def code128_values(text: str) -> list[int]:
    """Valeurs des symboles, clé comprise, arrêt exclu : pour les tests et le diagnostic."""
    values = _plan_code128(text)
    checksum = (values[0] + sum(position * value for position, value in enumerate(values[1:], start=1))) % 103
    return [*values, checksum]


# --- EAN-13 -------------------------------------------------------------------------------------------------------
_EAN_L = ("0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011")
_EAN_G = tuple(code[::-1].translate(str.maketrans("01", "10")) for code in _EAN_L)  # R inversé
_EAN_R = tuple(code.translate(str.maketrans("01", "10")) for code in _EAN_L)
_EAN_PARITY = ("LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL")
EAN_QUIET_LEFT, EAN_QUIET_RIGHT = 11, 7  # ISO/IEC 15420


def ean13_check_digit(twelve: str) -> str:
    return str((10 - sum((3 if index % 2 else 1) * int(digit) for index, digit in enumerate(twelve))) % 10)


def is_valid_ean13(value: str) -> bool:
    return len(value) == 13 and value.isdigit() and ean13_check_digit(value[:12]) == value[12]


def _runs(modules: str, offset: int) -> list[tuple[int, int]]:
    """Barres d'une suite de modules « 1 » (noir) et « 0 » (blanc)."""
    bars: list[tuple[int, int]] = []
    start = None
    for index, module in enumerate(modules + "0"):
        if module == "1" and start is None:
            start = index
        elif module == "0" and start is not None:
            bars.append((offset + start, index - start))
            start = None
    return bars


def ean13(value: str) -> Symbol:
    """Le symbole EAN-13 (ISBN-13 compris) : 13 chiffres dont la clé est juste, 95 modules."""
    if not is_valid_ean13(value):
        raise BarcodeError("Un EAN-13 compte 13 chiffres et sa clé doit être juste.")
    parity = _EAN_PARITY[int(value[0])]
    left = "".join(
        (_EAN_L if kind == "L" else _EAN_G)[int(digit)] for kind, digit in zip(parity, value[1:7], strict=True)
    )
    right = "".join(_EAN_R[int(digit)] for digit in value[7:])
    guards = [*_runs("101", 0), *_runs("01010", 45), *_runs("101", 92)]
    bars = sorted([*guards, *_runs(left, 3), *_runs(right, 50)])
    return Symbol(value, "ean13", tuple(bars), 95, EAN_QUIET_LEFT, EAN_QUIET_RIGHT, tuple(guards))
