# SPDX-License-Identifier: EUPL-1.2
"""Mise en page des étiquettes : une seule description, en millimètres, pour le PDF et pour l'aperçu à l'écran.

Une étiquette empile, de haut en bas : l'en-tête (logo, nom de la bibliothèque), le titre, la série et le tome, le code
et son numéro lisible, puis une ligne de pied (cote, inventaire, emplacement). Seul le code est obligatoire. Quand la
place manque, les éléments facultatifs s'effacent dans un ordre fixe, et l'aperçu dit lesquels : rien ne disparaît en
silence, et le code n'est jamais réduit sous sa taille lisible (D-L8-6).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from biblyo.core.barcodes import Symbol, code128, ean13
from biblyo.core.i18n import _
from biblyo.core.pdf import POINTS_PER_MM, fit_text, text_width, wrap_text
from biblyo.modules.labels.sheets import CUT_TOLERANCE, Sheet

#: Largeurs de module permises, en millimètres : 5, 4 et 3 points d'une imprimante à 300 points par pouce.
MODULES = (0.4233, 0.3387, 0.254)
MIN_BAR_HEIGHT = 6.0
MAX_BAR_HEIGHT = 16.0
LINE_GAP = 0.35

ELEMENTS: dict[str, str] = {
    "logo": _("Logo"),
    "bibliotheque": _("Nom de la bibliothèque"),
    "titre": _("Titre du livre"),
    "serie": _("Série et tome"),
    "numero": _("Numéro lisible sous le code"),
    "cote": _("Cote"),
    "inventaire": _("Numéro d’inventaire"),
    "emplacement": _("Emplacement"),
}
#: Ordre d'effacement quand la place manque : le moins utile au rangement d'abord.
DROP_ORDER = ("logo", "emplacement", "bibliotheque", "inventaire", "serie", "titre", "cote", "numero")

MODELS: dict[str, tuple[str, frozenset[str]]] = {
    "compact": (_("Compact"), frozenset({"numero", "cote"})),
    "standard": (_("Standard"), frozenset({"bibliotheque", "titre", "numero", "cote"})),
    "detaille": (
        _("Détaillé"),
        frozenset({"logo", "bibliotheque", "titre", "serie", "numero", "cote", "inventaire", "emplacement"}),
    ),
}
CUSTOM_MODEL = "personnalise"
MODEL_HELP: dict[str, str] = {
    "compact": _("Le code, son numéro et la cote : pour les petites étiquettes."),
    "standard": _("Le nom de la bibliothèque, le titre, le code et la cote."),
    "detaille": _("Tout : logo, titre, série et tome, cote, inventaire et emplacement."),
    CUSTOM_MODEL: _("Vous choisissez chaque élément."),
}


@dataclass(frozen=True)
class LabelData:
    """Ce qu'une étiquette peut montrer d'un exemplaire. `code` est son code-barres enregistré, jamais un autre."""

    code: str
    title: str = ""
    series: str = ""
    volume: str = ""
    call_number: str = ""
    inventory: str = ""
    location: str = ""
    copy_id: int | None = None


@dataclass(frozen=True)
class Appearance:
    elements: frozenset[str]
    library: str = ""
    has_logo: bool = False


@dataclass(frozen=True)
class Text:
    x: float
    baseline: float
    size: float  # en points
    text: str
    bold: bool = False
    gray: float = 0.0


@dataclass(frozen=True)
class Bars:
    x: float  # bord gauche de la première barre (les zones blanches sont autour)
    y: float
    height: float
    module: float
    symbol: Symbol
    guard_extra: float = 0.0  # EAN-13 : les barres de garde descendent un peu plus bas

    @property
    def width(self) -> float:
        return self.symbol.width * self.module

    def rectangles(self) -> list[tuple[float, float, float, float]]:
        guards = set(self.symbol.guards)
        return [
            (self.x + start * self.module, self.y, width * self.module,
             self.height + (self.guard_extra if (start, width) in guards else 0))
            for start, width in self.symbol.bars
        ]  # fmt: skip


@dataclass(frozen=True)
class Logo:
    x: float
    y: float
    size: float


@dataclass(frozen=True)
class Frame:
    """Contour d'une étiquette ou d'une case, pour l'aperçu et la feuille test."""

    x: float
    y: float
    width: float
    height: float
    kind: str  # « label », « occupied », « test »
    number: int = 0


@dataclass
class Drawn:
    texts: list[Text] = field(default_factory=list)
    bars: list[Bars] = field(default_factory=list)
    logos: list[Logo] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


class LayoutError(ValueError):
    """Le code ne tient pas sur cette étiquette à une taille lisible."""


def _line_height(size: float) -> float:
    return size / POINTS_PER_MM * 1.18


def _sizes(height: float) -> tuple[float, float, float]:
    """Corps (points) du texte courant, du titre et du numéro lisible, selon la hauteur de l'étiquette."""
    if height < 25:
        return 5.5, 6.0, 6.5
    if height < 36:
        return 6.5, 7.0, 7.5
    return 7.0, 8.0, 8.5


def padding(sheet: Sheet) -> float:
    return max(1.2, min(3.0, min(sheet.width, sheet.height) * 0.06))


def choose_module(symbol: Symbol, available: float) -> float:
    """La plus grande largeur de module qui fait tenir le code et ses deux zones blanches dans `available` mm."""
    for module in MODULES:
        if symbol.total_width * module <= available:
            return module
    raise LayoutError(
        _("Le code « {code} » ne tient pas sur une étiquette de cette largeur à une taille lisible.").format(
            code=symbol.text
        )
    )


def _series_line(data: LabelData) -> str:
    if data.series and data.volume:
        return _("{series} · T. {volume}").format(series=data.series, volume=data.volume)
    return data.series or (_("T. {volume}").format(volume=data.volume) if data.volume else "")


def _footer(data: LabelData, elements: frozenset[str]) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    if "cote" in elements and data.call_number:
        parts.append((data.call_number, True))
    if "inventaire" in elements and data.inventory and data.inventory != data.code:
        parts.append((_("Inv. {n}").format(n=data.inventory), False))
    if "emplacement" in elements and data.location:
        parts.append((data.location, False))
    return parts


def _attempt(data: LabelData, look: Appearance, elements: frozenset[str], sheet: Sheet, symbol: Symbol) -> Drawn | None:
    pad = padding(sheet)
    inner = sheet.width - 2 * pad
    body, title_size, number_size = _sizes(sheet.height)
    drawn = Drawn()
    y = pad
    bottom = sheet.height - pad
    lines: list[tuple[str, float, bool, float]] = []  # texte, corps, gras, gris

    header = ""
    logo = 0.0
    if "logo" in elements and look.has_logo:
        logo = _line_height(body) * 1.6
    if "bibliotheque" in elements and look.library:
        header = fit_text(look.library, body, inner - (logo + 0.8 if logo else 0), bold=True)
    if logo or header:
        height = max(logo, _line_height(body))
        if logo:
            drawn.logos.append(Logo(pad, y, logo))
        if header:
            drawn.texts.append(
                Text(pad + (logo + 0.8 if logo else 0), y + height / 2 + body / POINTS_PER_MM * 0.35, body, header,
                     True, 0.25)
            )  # fmt: skip
        y += height + LINE_GAP

    if "titre" in elements and data.title:
        count = 2 if sheet.height >= 30 else 1
        for line in wrap_text(data.title, title_size, inner, count, bold=True):
            lines.append((line, title_size, True, 0.0))
    if "serie" in elements and (series := _series_line(data)):
        lines.append((fit_text(series, body, inner), body, False, 0.2))
    for text, size, bold, gray in lines:
        if not text:
            continue
        y += _line_height(size)
        drawn.texts.append(Text(pad, y - _line_height(size) * 0.22, size, text, bold, gray))
        y += LINE_GAP * 0.5
    y += LINE_GAP if lines else 0

    footer = _footer(data, elements)
    footer_height = _line_height(body) + LINE_GAP if footer else 0.0
    number_height = _line_height(number_size) + LINE_GAP * 0.5 if "numero" in elements else 0.0
    bar_height = min(MAX_BAR_HEIGHT, bottom - y - number_height - footer_height)
    if bar_height < MIN_BAR_HEIGHT:
        return None

    module = choose_module(symbol, sheet.width - 2 * CUT_TOLERANCE)
    width = symbol.width * module
    x = (sheet.width - width) / 2
    drawn.bars.append(Bars(x, y, bar_height, module, symbol))
    y += bar_height
    if "numero" in elements:
        size = number_size
        while size > 4.5 and text_width(data.code, size, bold=True) > inner:
            size -= 0.5
        y += _line_height(size) * 0.82
        drawn.texts.append(Text((sheet.width - text_width(data.code, size, bold=True)) / 2, y, size, data.code, True))
        y += LINE_GAP * 0.5
    if footer:
        _place_footer(drawn, footer, body, pad, inner, bottom - _line_height(body) * 0.22)
    return drawn


def _place_footer(
    drawn: Drawn, parts: list[tuple[str, bool]], size: float, pad: float, inner: float, baseline: float
) -> None:
    """La cote à gauche, en gras ; le reste à droite, coupé s'il le faut."""
    first, bold = parts[0]
    first = fit_text(first, size, inner * (0.55 if len(parts) > 1 else 1), bold=bold)
    drawn.texts.append(Text(pad, baseline, size, first, bold))
    if len(parts) > 1:
        rest = fit_text(" · ".join(text for text, _bold in parts[1:]), size,
                        inner - text_width(first, size, bold=bold) - 1.5)  # fmt: skip
        if rest:
            drawn.texts.append(Text(pad + inner - text_width(rest, size), baseline, size, rest, False, 0.25))


def layout(data: LabelData, look: Appearance, sheet: Sheet) -> Drawn:
    """L'étiquette dessinée dans son repère (0, 0 en haut à gauche). Lève `LayoutError` si le code ne tient pas."""
    symbol = code128(data.code)
    choose_module(symbol, sheet.width - 2 * CUT_TOLERANCE)
    elements = frozenset(look.elements)
    dropped: list[str] = []
    for name in (None, *DROP_ORDER):
        if name is not None and name in elements:
            elements = elements - {name}
            dropped.append(name)
        drawn = _attempt(data, look, elements, sheet, symbol)
        if drawn is not None:
            drawn.dropped = [item for item in dropped if item in look.elements]
            return drawn
    raise LayoutError(_("Cette étiquette est trop basse pour un code lisible (6 mm de barres au moins)."))


def test_symbols() -> tuple[Symbol, Symbol, Symbol]:
    """Les codes de la feuille test et de l'assistant de la douchette : un Code 128, un EAN-13 (ISBN) et un code
    inconnu."""
    return code128(TEST_CODE128), ean13(TEST_EAN13), code128(TEST_UNKNOWN)


TEST_CODE128 = "BIBLYO-TEST"
TEST_EAN13 = "9782123456803"  # ISBN d'exemple, dont la clé est juste ; il ne désigne aucun livre du catalogue
TEST_UNKNOWN = "BIBLYO-INCONNU"  # code d'essai qu'aucun livre ne porte : biblyO doit répondre « Code inconnu »


@dataclass(frozen=True)
class Placed:
    """Une étiquette sur une feuille : sa case, son contenu, et ce qu'elle a dû laisser de côté."""

    sheet_number: int
    position: int
    x: float
    y: float
    data: LabelData
    drawn: Drawn


@dataclass(frozen=True)
class Plan:
    sheet: Sheet
    pages: tuple[tuple[Placed, ...], ...]
    occupied: frozenset[int]  # cases déjà utilisées de la première feuille
    offset: tuple[float, float] = (0.0, 0.0)  # calibration : décalage horizontal et vertical, en millimètres

    @property
    def count(self) -> int:
        return sum(len(page) for page in self.pages)

    @property
    def dropped(self) -> list[str]:
        names = {name for page in self.pages for item in page for name in item.drawn.dropped}
        return [name for name in DROP_ORDER if name in names]


def free_positions(sheet: Sheet, occupied: frozenset[int]) -> list[int]:
    return [position for position in range(1, sheet.count + 1) if position not in occupied]


def plan(
    items: Sequence[LabelData],
    look: Appearance,
    sheet: Sheet,
    *,
    occupied: frozenset[int] = frozenset(),
    offset: tuple[float, float] = (0.0, 0.0),
) -> Plan:
    """Répartit les étiquettes sur les feuilles : la première saute les cases déjà utilisées, les suivantes sont
    pleines. Chaque étiquette garde le code de son exemplaire, dans l'ordre donné."""
    first = free_positions(sheet, occupied)
    if not first:
        raise LayoutError(
            _("Toutes les cases de cette feuille sont marquées utilisées : choisissez une feuille neuve.")
        )
    pages: list[list[Placed]] = []
    queue = list(items)
    positions = first
    number = 1
    while queue:
        page: list[Placed] = []
        for position in positions:
            if not queue:
                break
            data = queue.pop(0)
            x, y = sheet.slot(position)
            page.append(Placed(number, position, x, y, data, layout(data, look, sheet)))
        pages.append(page)
        positions = list(range(1, sheet.count + 1))
        number += 1
    return Plan(sheet, tuple(tuple(page) for page in pages), occupied, offset)
