# SPDX-License-Identifier: EUPL-1.2
"""Code 128 et EAN-13 : table conforme à la norme, chemin le plus court, relecture par un décodeur écrit à part.

Le décodeur ci-dessous ne partage avec l'encodeur que la table des largeurs : il relit les barres comme le ferait une
douchette (largeurs des éléments, symboles, jeu courant, clé). Le rapport L8 consigne en plus une relecture par un
décodeur tiers (Vision, macOS) des codes et des PDF produits.
"""

from __future__ import annotations

import pytest

from biblyo.core.barcodes import (
    CODE128_PATTERNS,
    QUIET_ZONE_MODULES,
    BarcodeError,
    Symbol,
    code128,
    code128_values,
    ean13,
    ean13_check_digit,
    is_valid_ean13,
)


def _widths(symbol: Symbol) -> list[int]:
    """Largeurs alternées barre, espace… relues depuis les barres dessinées."""
    out: list[int] = []
    position = 0
    for start, width in symbol.bars:
        if start > position:
            out.append(start - position)
        out.append(width)
        position = start + width
    return out


def decode128(symbol: Symbol) -> str:
    widths = _widths(symbol)
    table = {pattern: value for value, pattern in enumerate(CODE128_PATTERNS)}
    values = [table["".join(map(str, widths[i : i + 6]))] for i in range(0, len(widths) - 7, 6)]
    assert "".join(map(str, widths[-7:])) == CODE128_PATTERNS[106]
    *data, checksum = values
    assert (data[0] + sum(i * v for i, v in enumerate(data[1:], start=1))) % 103 == checksum
    current = {104: "B", 105: "C"}[data[0]]
    text = ""
    for value in data[1:]:
        if current == "B" and value == 99:
            current = "C"
        elif current == "C" and value == 100:
            current = "B"
        else:
            text += f"{value:02d}" if current == "C" else chr(value + 32)
    return text


def test_table_conforme_a_la_norme() -> None:
    assert len(CODE128_PATTERNS) == 107
    assert len(set(CODE128_PATTERNS)) == 107
    for pattern in CODE128_PATTERNS[:106]:
        assert len(pattern) == 6 and sum(map(int, pattern)) == 11
        assert sum(int(pattern[i]) for i in (0, 2, 4)) % 2 == 0  # barres : nombre pair de modules
    assert CODE128_PATTERNS[106] == "2331112" and sum(map(int, CODE128_PATTERNS[106])) == 13


def test_vecteur_connu_ex_000001() -> None:
    # Départ B, E, X, -, passage en C, 00, 00, 01, clé 77 : relu tel quel par Vision (rapport L8).
    assert code128_values("EX-000001") == [104, 37, 56, 13, 99, 0, 0, 1, 77]
    symbol = code128("EX-000001")
    assert symbol.width == 112
    assert symbol.total_width == 112 + 2 * QUIET_ZONE_MODULES


@pytest.mark.parametrize(
    "text",
    ["EX-000001", "EX-999999", "CSN000001", "CSN000930", "BIBLYO-TEST", "354", "9", "P-000123", "EX-1234567",
     "A", "12", "123", "1234", "12345", "AB12CD3456EF", "ex-000042", "Z-1-2-3", "~ !"],
)  # fmt: skip
def test_relecture(text: str) -> None:
    assert decode128(code128(text)) == text


def test_chemin_le_plus_court() -> None:
    # Six chiffres en jeu C (3 symboles + 1 changement) plutôt que six symboles en jeu B.
    assert len(code128_values("EX-000001")) < len(code128_values("EX-0A0B0C"))
    assert code128_values("123456")[0] == 105  # que des chiffres en nombre pair : départ en jeu C


def test_refus() -> None:
    for text in ("", "é", "a\tb", "x" * 49):
        with pytest.raises(BarcodeError):
            code128(text)


def test_ean13() -> None:
    assert ean13_check_digit("978212345680") == "3"
    assert is_valid_ean13("9782123456803") and not is_valid_ean13("9782123456804")
    symbol = ean13("9782123456803")
    assert symbol.width == 95
    assert len(symbol.guards) == 6  # 2 + 2 + 2 barres de garde
    assert (symbol.quiet_left, symbol.quiet_right) == (11, 7)
    with pytest.raises(BarcodeError):
        ean13("9782123456804")


def test_ean13_relecture() -> None:
    l_codes = ("0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111",
               "0001011")  # fmt: skip
    g_codes = tuple(code.translate(str.maketrans("01", "10"))[::-1] for code in l_codes)
    r_codes = tuple(code.translate(str.maketrans("01", "10")) for code in l_codes)
    parities = ("LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL")
    for value in ("9782123456803", "9780000000002", "4006381333931", "0012345678905"):
        symbol = ean13(value)
        modules = ["0"] * 95
        for start, width in symbol.bars:
            modules[start : start + width] = ["1"] * width
        line = "".join(modules)
        assert line[:3] == "101" and line[45:50] == "01010" and line[92:] == "101"
        left = [line[3 + 7 * i : 10 + 7 * i] for i in range(6)]
        right = [line[50 + 7 * i : 57 + 7 * i] for i in range(6)]
        kinds = "".join("L" if code in l_codes else "G" for code in left)
        digits = [str(parities.index(kinds))]
        digits += [
            str((l_codes if kind == "L" else g_codes).index(code)) for kind, code in zip(kinds, left, strict=True)
        ]
        digits += [str(r_codes.index(code)) for code in right]
        assert "".join(digits) == value
