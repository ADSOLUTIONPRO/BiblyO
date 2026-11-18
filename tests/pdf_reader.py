# SPDX-License-Identifier: EUPL-1.2
"""Relecture des PDF de biblyO pour les tests : format des pages, textes, et codes-barres relus depuis leurs barres.

Écrit à part du moteur (`biblyo.core.pdf`) : il ne s'appuie que sur la syntaxe PDF. Les barres d'un code sont les
rectangles d'une même opération « q 0 g … f Q » ; leurs largeurs, divisées par la plus fine, donnent les modules.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

from biblyo.core.barcodes import CODE128_PATTERNS

POINTS_PER_MM = 72 / 25.4
_OBJECT = re.compile(rb"(\d+) 0 obj\n(.*?)\nendobj\n", re.S)
_STREAM = re.compile(rb"<<(.*?)>>\nstream\n(.*)\nendstream", re.S)
_BARS = re.compile(r"q 0 g ((?:[-\d.]+ [-\d.]+ [-\d.]+ [-\d.]+ re ?)+) f Q")
_TEXT = re.compile(r"BT /(F\d) ([\d.]+) Tf ([-\d.]+) ([-\d.]+) Td \(((?:\\.|[^\\)])*)\) Tj ET")


@dataclass(frozen=True)
class Code:
    text: str
    x: float  # mm depuis la gauche de la page, première barre
    y: float  # mm depuis le haut de la page, haut des barres
    width: float
    height: float
    module: float


@dataclass(frozen=True)
class PageContent:
    width: float  # mm
    height: float
    codes: list[Code]
    texts: list[tuple[float, float, str]]  # x, ligne de base (mm depuis le haut), texte


def objects(data: bytes) -> dict[int, bytes]:
    return {int(number): body for number, body in _OBJECT.findall(data)}


def check_xref(data: bytes) -> None:
    """Chaque décalage de la table de références désigne bien le début de son objet."""
    start = int(re.search(rb"startxref\n(\d+)", data).group(1))  # type: ignore[union-attr]
    table = data[start:].split(b"\n")
    assert table[0] == b"xref"
    count = int(table[1].split()[1])
    for number in range(1, count):
        offset = int(table[2 + number].split()[0])
        assert data[offset:].startswith(f"{number} 0 obj".encode())


def _decode(widths: list[int]) -> str:
    table = {pattern: value for value, pattern in enumerate(CODE128_PATTERNS)}
    values = [table["".join(map(str, widths[i : i + 6]))] for i in range(0, len(widths) - 7, 6)]
    assert "".join(map(str, widths[-7:])) == CODE128_PATTERNS[106], "pas de motif d'arrêt"
    *data, checksum = values
    assert (data[0] + sum(i * v for i, v in enumerate(data[1:], start=1))) % 103 == checksum, "clé fausse"
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


def _code(numbers: list[float], page_height: float) -> Code:
    rects = sorted((numbers[i], numbers[i + 1], numbers[i + 2], numbers[i + 3]) for i in range(0, len(numbers), 4))
    module = min(width for _, _, width, _ in rects)
    widths: list[int] = []
    position = rects[0][0]
    for x, _y, width, _h in rects:
        if x - position > module / 4:
            widths.append(round((x - position) / module))
        widths.append(round(width / module))
        position = x + width
    x0, y0, _w, h = rects[0]
    try:
        text = _decode(widths)
    except (KeyError, AssertionError):
        text = ""  # pas un Code 128 (l'EAN-13 de la feuille test) ou un code abîmé : le test qui l'attend échoue
    return Code(
        text,
        x0 / POINTS_PER_MM,
        page_height - (y0 + h) / POINTS_PER_MM,
        (position - x0) / POINTS_PER_MM,
        h / POINTS_PER_MM,
        module / POINTS_PER_MM,
    )


def pages(data: bytes) -> list[PageContent]:
    found = objects(data)
    out: list[PageContent] = []
    for body in found.values():
        if b"/Type /Page " not in body:
            continue
        box = [float(v) for v in re.search(rb"/MediaBox \[([^\]]+)\]", body).group(1).split()]  # type: ignore[union-attr]
        width, height = box[2] / POINTS_PER_MM, box[3] / POINTS_PER_MM
        content_ref = int(re.search(rb"/Contents (\d+) 0 R", body).group(1))  # type: ignore[union-attr]
        match = _STREAM.search(found[content_ref])
        assert match is not None
        content = zlib.decompress(match.group(2)).decode("latin-1")
        codes = [
            _code([float(v) for v in m.group(1).replace("re", "").split()], height) for m in _BARS.finditer(content)
        ]
        texts = [
            (float(m.group(3)) / POINTS_PER_MM, height - float(m.group(4)) / POINTS_PER_MM,
             re.sub(r"\\(.)", r"\1", m.group(5)))
            for m in _TEXT.finditer(content)
        ]  # fmt: skip
        out.append(PageContent(width, height, codes, texts))
    return out
