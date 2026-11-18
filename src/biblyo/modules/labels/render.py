# SPDX-License-Identifier: EUPL-1.2
"""Rendus d'une mise en page : PDF à imprimer, SVG pour l'aperçu à l'écran. Les deux lisent le même `Plan`.

Le SVG est en millimètres (`viewBox`) et chaque texte porte sa largeur calculée (`textLength`) : même si le navigateur
n'a pas Helvetica, l'aperçu garde exactement les coupures et les marges du PDF.
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape

from markupsafe import Markup

from biblyo.core.barcodes import Symbol, code128
from biblyo.core.i18n import _
from biblyo.core.pdf import POINTS_PER_MM, Document, Page, text_width
from biblyo.modules.labels.layout import Bars, Drawn, Plan, test_symbols
from biblyo.modules.labels.sheets import Sheet

RULER = 100.0  # longueur des règles de la feuille test, en millimètres


def _pdf_label(page: Page, drawn: Drawn, x: float, y: float, logo: int | None) -> None:
    for bars in drawn.bars:
        page.bars([(x + bx, y + by, w, h) for bx, by, w, h in bars.rectangles()])
    for text in drawn.texts:
        page.text(x + text.x, y + text.baseline, text.size, text.text, bold=text.bold, gray=text.gray)
    if logo is not None:
        for item in drawn.logos:
            page.image(logo, x + item.x, y + item.y, item.size, item.size)


def to_pdf(plan: Plan, *, title: str, logo: bytes | None = None) -> bytes:
    """Le PDF du lot : une page A4 par feuille, chaque étiquette dans sa case, décalée de la calibration."""
    document = Document(title)
    logo_number = document.add_image(logo) if logo else None
    dx, dy = plan.offset
    for placed_items in plan.pages:
        page = document.add_page()
        for placed in placed_items:
            _pdf_label(page, placed.drawn, placed.x + dx, placed.y + dy, logo_number)
    return document.render()


def test_sheet_pdf(sheet: Sheet, *, offset: tuple[float, float] = (0.0, 0.0)) -> bytes:
    """La feuille test : le contour et le numéro de chaque case, son centre, deux règles de 100 mm et trois codes à lire
    à la douchette (Code 128, ISBN, code inconnu). À imprimer d'abord sur du papier ordinaire, à 100 %, puis à poser
    sur la planche, par transparence ; enfin, à lire avec la douchette dans l'assistant « Configurer ma douchette »."""
    document = Document(_("biblyO, feuille test : {name}").format(name=sheet.name))
    page = document.add_page()
    dx, dy = offset
    for position in range(1, sheet.count + 1):
        x, y = sheet.slot(position)
        x, y = x + dx, y + dy
        page.rect(x, y, sheet.width, sheet.height, fill=None, stroke=0.45, line=0.15, dash=0.8)
        cx, cy = x + sheet.width / 2, y + sheet.height / 2
        page.line(cx - 2, cy, cx + 2, cy, gray=0.45, width=0.12)
        page.line(cx, cy - 2, cx, cy + 2, gray=0.45, width=0.12)
        page.text(x + 1.2, y + 3.2, 6, str(position), bold=True, gray=0.35)
    _rulers(page, dx, dy)
    _test_codes(page, dx, dy)
    return document.render()


def _test_codes(page: Page, dx: float, dy: float) -> None:
    """Les trois codes d’essai, sous la règle horizontale, à droite de la verticale : lisibles sur toute planche."""
    left, top = 62.0 + dx, 160.0 + dy
    captions = (_("Code 128 d’essai"), _("ISBN-13 d’essai"), _("Code inconnu d’essai"))
    for symbol, caption in zip(test_symbols(), captions, strict=True):
        module = 0.3387
        height = 10.0
        bars = Bars(left + symbol.quiet_left * module, top, height, module, symbol, 1.2 if symbol.guards else 0.0)
        page.bars(bars.rectangles())
        page.text(left, top + height + 4.2, 6.5, f"{caption} : {symbol.text}", bold=True)
        top += 24


def _rulers(page: Page, dx: float, dy: float) -> None:
    """Deux règles de 100 mm, graduées tous les 10 mm, au milieu de la page : imprimées à 100 %, elles mesurent
    exactement 100 mm."""
    x0, y0 = 55.0 + dx, 140.0 + dy
    page.line(x0, y0, x0 + RULER, y0, width=0.25)
    page.line(x0, y0, x0, y0 + RULER, width=0.25)
    for step in range(0, 11):
        tick = 2.5 if step % 5 == 0 else 1.5
        page.line(x0 + step * 10, y0 - tick, x0 + step * 10, y0, width=0.2)
        page.line(x0 - tick, y0 + step * 10, x0, y0 + step * 10, width=0.2)
    page.text(x0 + 3, y0 + 5, 7, _("Chaque trait mesure 100 mm s’il est imprimé à 100 %."), bold=True)
    page.text(x0 + 3, y0 + 9, 6.5, _("Sinon : désactivez « Ajuster à la page » dans la fenêtre d’impression."))
    page.text(x0 + 3, y0 + 13, 6.5, _("Douchette : lisez les trois codes ci-dessous dans « Configurer ma douchette »."))


# --- Aperçu SVG -------------------------------------------------------------------------------------------------------
def _f(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _svg_texts(drawn: Drawn, x: float, y: float) -> Iterable[str]:
    for text in drawn.texts:
        size = text.size / POINTS_PER_MM
        width = text_width(text.text, text.size, bold=text.bold)
        weight = " lbl-b" if text.bold else ""
        tone = " lbl-soft" if text.gray > 0 else ""
        yield (
            f'<text class="lbl-t{weight}{tone}" x="{_f(x + text.x)}" y="{_f(y + text.baseline)}" '
            f'font-size="{_f(size)}" textLength="{_f(width)}" lengthAdjust="spacingAndGlyphs">'
            f"{escape(text.text)}</text>"
        )


def _svg_bars(bars: Bars, x: float, y: float) -> str:
    path = "".join(f"M{_f(x + bx)} {_f(y + by)}h{_f(w)}v{_f(h)}h-{_f(w)}z" for bx, by, w, h in bars.rectangles())
    return f'<path class="lbl-bars" d="{path}"/>'


def _svg_label(drawn: Drawn, x: float, y: float, logo_url: str) -> list[str]:
    parts = [_svg_bars(bars, x, y) for bars in drawn.bars]
    parts += list(_svg_texts(drawn, x, y))
    if logo_url:
        parts += [
            f'<image href="{escape(logo_url)}" x="{_f(x + item.x)}" y="{_f(y + item.y)}" width="{_f(item.size)}" '
            f'height="{_f(item.size)}" preserveAspectRatio="xMidYMid meet"/>'
            for item in drawn.logos
        ]
    return parts


def sheet_svg(plan: Plan, page_index: int, *, title: str, logo_url: str = "") -> Markup:
    """Une feuille A4 à l'échelle : cases déjà utilisées hachurées, étiquettes à imprimer dessinées, cases libres
    restantes en pointillés."""
    sheet = plan.sheet
    dx, dy = plan.offset
    placed = {item.position: item for item in plan.pages[page_index]} if plan.pages else {}
    parts = [
        f'<svg class="sheet-svg" viewBox="0 0 210 297" role="img" aria-labelledby="apercu-{page_index}" '
        f'xmlns="http://www.w3.org/2000/svg"><title id="apercu-{page_index}">{escape(title)}</title>',
        f'<defs><pattern id="hachures-{page_index}" width="2" height="2" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)"><line class="lbl-hatch" x1="0" y1="0" x2="0" y2="2"/></pattern></defs>',
        '<rect class="sheet-page" x="0" y="0" width="210" height="297"/>',
    ]
    for position in range(1, sheet.count + 1):
        x, y = sheet.slot(position)
        used = page_index == 0 and position in plan.occupied
        kind = "occupied" if used else "label" if position in placed else "free"
        parts.append(
            f'<rect class="lslot lslot-{kind}" x="{_f(x)}" y="{_f(y)}" width="{_f(sheet.width)}" '
            f'height="{_f(sheet.height)}" rx="{_f(sheet.radius)}"/>'
        )
        if used:
            parts.append(
                f'<rect class="lslot-hatch" x="{_f(x)}" y="{_f(y)}" width="{_f(sheet.width)}" '
                f'height="{_f(sheet.height)}" rx="{_f(sheet.radius)}" fill="url(#hachures-{page_index})"/>'
            )
        if position in placed:
            parts += _svg_label(placed[position].drawn, x + dx, y + dy, logo_url)
        else:
            size = min(4.0, sheet.height / 4)
            parts.append(
                f'<text class="lslot-n" x="{_f(x + sheet.width / 2)}" y="{_f(y + sheet.height / 2 + size / 3)}" '
                f'font-size="{_f(size)}" text-anchor="middle">{position}</text>'
            )
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 : tout texte venu des données passe par `escape`


def label_svg(plan: Plan, *, title: str, logo_url: str = "") -> Markup:
    """La première étiquette du lot, seule et agrandie : on y lit ce qui sera imprimé, au caractère près."""
    placed = plan.pages[0][0]
    sheet = plan.sheet
    parts = [
        f'<svg class="label-svg" viewBox="-1 -1 {_f(sheet.width + 2)} {_f(sheet.height + 2)}" role="img" '
        f'aria-label="{escape(title)}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect class="lslot lslot-label" x="0" y="0" width="{_f(sheet.width)}" height="{_f(sheet.height)}" '
        f'rx="{_f(sheet.radius)}"/>',
        *_svg_label(placed.drawn, 0, 0, logo_url),
        "</svg>",
    ]
    return Markup("".join(parts))  # noqa: S704 : tout texte venu des données passe par `escape`


def symbol_svg(symbol: Symbol, *, label: str = "") -> Markup:
    """Un code dessiné à l'écran (Code 128 ou EAN-13), lisible par une douchette ou une caméra : barres noires sur
    fond blanc, zones blanches comprises, numéro lisible dessous."""
    module = 1.0
    height = 30.0
    guard = 4.0 if symbol.guards else 0.0
    width = symbol.total_width * module
    bars = Bars(symbol.quiet_left * module, 0, height, module, symbol, guard)
    total = height + guard + 12
    parts = [
        f'<svg class="code-svg" viewBox="0 0 {_f(width)} {_f(total)}" role="img" '
        f'aria-label="{escape(label or _("Code-barres {code}").format(code=symbol.text))}" '
        'xmlns="http://www.w3.org/2000/svg">',
        f'<rect class="code-bg" x="0" y="0" width="{_f(width)}" height="{_f(total)}"/>',
        _svg_bars(bars, 0, 0),
        f'<text class="code-n" x="{_f(width / 2)}" y="{_f(height + guard + 9)}" font-size="8" text-anchor="middle">'
        f"{escape(symbol.text)}</text></svg>",
    ]
    return Markup("".join(parts))  # noqa: S704 : le texte du code est échappé


def barcode_svg(code: str, *, label: str = "") -> Markup:
    """Le code-barres d'un exemplaire, en Code 128."""
    return symbol_svg(code128(code), label=label)
