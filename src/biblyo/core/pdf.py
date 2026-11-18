# SPDX-License-Identifier: EUPL-1.2
"""Écriture de PDF, sans dépendance : pages, rectangles, traits, textes, images (lot L8, planches d'étiquettes).

Toutes les cotes sont en **millimètres**, mesurées depuis le coin supérieur gauche de la page, comme sur une planche
d'étiquettes ; la conversion en points PDF (1/72 de pouce, origine en bas à gauche) se fait ici seulement.

Le texte utilise les polices standard Helvetica et Helvetica-Bold (codage WinAnsi), que tout lecteur PDF fournit ; leurs
chasses, publiées par Adobe (`pdf_metrics`), servent à mesurer un texte avant de le couper. Le document demande au
lecteur d'imprimer **sans mise à l'échelle** (`/PrintScaling /None`) : une étiquette de 38,1 mm doit mesurer 38,1 mm.
"""

from __future__ import annotations

import io
import unicodedata
import zlib
from dataclasses import dataclass, field

from PIL import Image

from biblyo.core.pdf_metrics import FIRST_CHAR, HELVETICA, HELVETICA_BOLD

POINTS_PER_MM = 72 / 25.4
A4 = (210.0, 297.0)
_SUBSTITUTES = {" ": " ", " ": " ", " ": " ", "‐": "-", "‑": "-", "−": "-"}


def mm(value: float) -> float:
    return value * POINTS_PER_MM


def _number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def encode_text(text: str) -> bytes:
    """Le texte en WinAnsi. Un caractère absent du codage perd son accent (« ő » → « o »), sinon devient « ? »."""
    out = bytearray()
    for character in text:
        character = _SUBSTITUTES.get(character, character)
        try:
            encoded = character.encode("cp1252")
        except UnicodeEncodeError:
            base = unicodedata.normalize("NFKD", character).encode("cp1252", "ignore")
            encoded = base[:1] if base else b"?"
        if encoded[0] < FIRST_CHAR:
            encoded = b" "
        out += encoded
    return bytes(out)


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    """Largeur du texte, en millimètres, pour un corps en points."""
    widths = HELVETICA_BOLD if bold else HELVETICA
    units = sum(widths[byte - FIRST_CHAR] for byte in encode_text(text))
    return units * size / 1000 / POINTS_PER_MM


def fit_text(text: str, size: float, width: float, *, bold: bool = False) -> str:
    """Le texte tel qu'il tient dans `width` millimètres, coupé par « … » si besoin ; vide si rien ne tient."""
    text = " ".join(text.split())
    if text_width(text, size, bold=bold) <= width:
        return text
    words = text
    while words and text_width(words.rstrip(" ,;:-–") + "…", size, bold=bold) > width:
        words = words[:-1]
    words = words.rstrip(" ,;:-–")
    return f"{words}…" if words else ""


def wrap_text(text: str, size: float, width: float, lines: int, *, bold: bool = False) -> list[str]:
    """Au plus `lines` lignes coupées entre les mots ; la dernière finit par « … » si le texte ne tient pas."""
    words = " ".join(text.split()).split(" ")
    out: list[str] = []
    while words and len(out) < lines:
        line = words.pop(0)
        while words and text_width(f"{line} {words[0]}", size, bold=bold) <= width:
            line = f"{line} {words.pop(0)}"
        if len(out) == lines - 1 and words:
            line = f"{line} {' '.join(words)}"
            words = []
        out.append(fit_text(line, size, width, bold=bold))
    return [line for line in out if line]


def _escape(data: bytes) -> bytes:
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


@dataclass
class Page:
    width: float
    height: float
    operations: list[str] = field(default_factory=list)
    images: dict[str, int] = field(default_factory=dict)  # nom dans la page → numéro d'image du document

    def _y(self, top: float) -> float:
        return mm(self.height - top)

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        fill: float | None = 0.0,
        stroke: float | None = None,
        line: float = 0.2,
        dash: float | None = None,
    ) -> None:
        """Rectangle : `fill` et `stroke` sont des gris (0 noir, 1 blanc), `None` pour ne pas remplir ou tracer."""
        ops = ["q"]
        if fill is not None:
            ops.append(f"{_number(fill)} g")
        if stroke is not None:
            ops.append(f"{_number(stroke)} G {_number(mm(line))} w")
            ops.append(f"[{_number(mm(dash))}] 0 d" if dash else "[] 0 d")
        ops.append(f"{_number(mm(x))} {_number(self._y(y + height))} {_number(mm(width))} {_number(mm(height))} re")
        ops.append("B" if fill is not None and stroke is not None else "f" if fill is not None else "S")
        ops.append("Q")
        self.operations.append(" ".join(ops))

    def bars(self, rectangles: list[tuple[float, float, float, float]]) -> None:
        """Beaucoup de rectangles noirs en une seule opération : les barres d'un code."""
        body = " ".join(
            f"{_number(mm(x))} {_number(self._y(y + h))} {_number(mm(w))} {_number(mm(h))} re"
            for x, y, w, h in rectangles
        )
        self.operations.append(f"q 0 g {body} f Q")

    def line(self, x1: float, y1: float, x2: float, y2: float, *, gray: float = 0.0, width: float = 0.2) -> None:
        self.operations.append(
            f"q {_number(gray)} G {_number(mm(width))} w {_number(mm(x1))} {_number(self._y(y1))} m "
            f"{_number(mm(x2))} {_number(self._y(y2))} l S Q"
        )

    def text(self, x: float, baseline: float, size: float, text: str, *, bold: bool = False, gray: float = 0.0) -> None:
        """Texte posé sur sa ligne de base, à `baseline` millimètres du haut de la page."""
        data = _escape(encode_text(text)).decode("latin-1")
        font = "F2" if bold else "F1"
        self.operations.append(
            f"q {_number(gray)} g BT /{font} {_number(size)} Tf {_number(mm(x))} {_number(self._y(baseline))} Td "
            f"({data}) Tj ET Q"
        )

    def image(self, number: int, x: float, y: float, width: float, height: float) -> None:
        name = f"Im{number}"
        self.images[name] = number
        self.operations.append(
            f"q {_number(mm(width))} 0 0 {_number(mm(height))} {_number(mm(x))} {_number(self._y(y + height))} cm "
            f"/{name} Do Q"
        )


@dataclass(frozen=True)
class _Image:
    width: int
    height: int
    rgb: bytes
    alpha: bytes | None


class Document:
    """Un PDF en mémoire. `render` donne des octets identiques pour un même contenu : rien ne dépend de l'heure."""

    def __init__(self, title: str = "", size: tuple[float, float] = A4) -> None:
        self.title = title
        self.size = size
        self.pages: list[Page] = []
        self._images: list[_Image] = []

    def add_page(self) -> Page:
        page = Page(*self.size)
        self.pages.append(page)
        return page

    def add_image(self, data: bytes, *, max_side: int = 600) -> int:
        """Une image (logo) réduite à `max_side` pixels, aplatie en RVB avec sa transparence. Renvoie son numéro."""
        with Image.open(io.BytesIO(data)) as source:
            picture = source.convert("RGBA")
        picture.thumbnail((max_side, max_side))
        alpha = picture.getchannel("A")
        has_alpha = alpha.getextrema() != (255, 255)
        self._images.append(
            _Image(
                picture.width, picture.height, picture.convert("RGB").tobytes(), alpha.tobytes() if has_alpha else None
            )
        )
        return len(self._images) - 1

    def render(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        def stream(dictionary: str, data: bytes) -> bytes:
            packed = zlib.compress(data, 9)
            return (
                f"<< {dictionary} /Filter /FlateDecode /Length {len(packed)} >>\nstream\n".encode()
                + packed
                + (b"\nendstream")
            )

        catalog = add(b"")  # réservé : rempli quand l'arbre des pages est connu
        pages_root = add(b"")
        fonts = {}
        for name, base, widths in (("F1", "Helvetica", HELVETICA), ("F2", "Helvetica-Bold", HELVETICA_BOLD)):
            fonts[name] = add(
                (
                    f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding "
                    f"/FirstChar {FIRST_CHAR} /LastChar 255 /Widths [{' '.join(map(str, widths))}] >>"
                ).encode()
            )
        image_refs: list[int] = []
        for picture in self._images:
            mask = ""
            if picture.alpha is not None:
                smask = add(
                    stream(
                        f"/Type /XObject /Subtype /Image /Width {picture.width} /Height {picture.height} "
                        "/ColorSpace /DeviceGray /BitsPerComponent 8",
                        picture.alpha,
                    )
                )
                mask = f" /SMask {smask} 0 R"
            image_refs.append(
                add(
                    stream(
                        f"/Type /XObject /Subtype /Image /Width {picture.width} /Height {picture.height} "
                        f"/ColorSpace /DeviceRGB /BitsPerComponent 8{mask}",
                        picture.rgb,
                    )
                )
            )
        kids: list[int] = []
        for page in self.pages:
            content = add(stream("", "\n".join(page.operations).encode("latin-1")))
            xobjects = " ".join(f"/{name} {image_refs[number]} 0 R" for name, number in sorted(page.images.items()))
            resources = f"/Font << /F1 {fonts['F1']} 0 R /F2 {fonts['F2']} 0 R >>"
            if xobjects:
                resources += f" /XObject << {xobjects} >>"
            kids.append(
                add(
                    (
                        f"<< /Type /Page /Parent {pages_root} 0 R /MediaBox [0 0 {_number(mm(page.width))} "
                        f"{_number(mm(page.height))}] /Resources << {resources} >> /Contents {content} 0 R >>"
                    ).encode()
                )
            )
        objects[pages_root - 1] = (
            f"<< /Type /Pages /Kids [{' '.join(f'{kid} 0 R' for kid in kids)}] /Count {len(kids)} >>".encode()
        )
        objects[catalog - 1] = (
            f"<< /Type /Catalog /Pages {pages_root} 0 R "
            "/ViewerPreferences << /PrintScaling /None /Duplex /Simplex >> >>"
        ).encode()
        title = _escape(encode_text(self.title)).decode("latin-1")
        info = add(f"<< /Producer (biblyO) /Title ({title}) >>".encode("latin-1"))

        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
        out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n".encode()
        out += f"startxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)
