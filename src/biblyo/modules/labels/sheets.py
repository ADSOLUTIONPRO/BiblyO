# SPDX-License-Identifier: EUPL-1.2
"""Planches A4 prédécoupées : gabarits connus et gabarit personnalisé, en millimètres (lot L8, D-L8-5).

Les sites des fabricants refusent la lecture automatique : les cotes des gabarits connus viennent de bases de gabarits
publiques, recoupées entre elles et avec la géométrie de la page (une planche est presque toujours centrée). Chaque
gabarit dit d'où viennent ses cotes et à quel point elles sont sûres ; la feuille test, imprimée sur la vraie planche,
les confirme, et la calibration corrige un décalage de l'imprimante.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from biblyo.core.errors import FormErrors
from biblyo.core.i18n import _
from biblyo.core.pdf import A4

CUSTOM = "personnalise"
MIN_LABEL = (25.0, 12.0)  # en deçà, un code de 9 caractères et son numéro ne tiennent plus lisiblement
MAX_LABELS_PER_SHEET = 200
CUT_TOLERANCE = 0.5  # le découpage d'une planche varie d'un demi-millimètre : rien d'important si près du bord

VERIFIED_CROSS = "recoupe"
VERIFIED_SINGLE = "source_unique"
VERIFIED_USER = "saisi"
FEATURED_MAIN = "principal"
FEATURED_COMPACT = "compact"
VERIFICATION_LABELS: dict[str, str] = {
    VERIFIED_CROSS: _("Format confirmé ; placement à certifier par la feuille test"),
    VERIFIED_SINGLE: _("Calibration nécessaire"),
    VERIFIED_USER: _("Gabarit personnalisé : calibration nécessaire"),
}


@dataclass(frozen=True)
class Sheet:
    """Une planche : `columns` × `rows` étiquettes de `width` × `height`, la première à (`left`, `top`)."""

    code: str
    name: str
    columns: int
    rows: int
    width: float
    height: float
    top: float
    left: float
    pitch_x: float  # d'un bord gauche au suivant : largeur + espace
    pitch_y: float
    radius: float = 0.0
    verification: str = VERIFIED_USER
    sources: tuple[str, ...] = ()
    note: str = ""
    featured: str = ""  # « principal » ou « compact » : un des deux formats mis en avant

    @property
    def needs_calibration(self) -> bool:
        """Hors des deux formats mis en avant, des cotes d'une seule source ou saisies : calibration avant usage."""
        return not self.featured

    @property
    def count(self) -> int:
        return self.columns * self.rows

    @property
    def gap_x(self) -> float:
        return round(self.pitch_x - self.width, 3)

    @property
    def gap_y(self) -> float:
        return round(self.pitch_y - self.height, 3)

    @property
    def summary(self) -> str:
        return _("{n} étiquettes de {w} × {h} mm ({c} × {r})").format(
            n=self.count, w=_mm(self.width), h=_mm(self.height), c=self.columns, r=self.rows
        )

    def slot(self, position: int) -> tuple[float, float]:
        """Coin supérieur gauche de l'étiquette n° `position` (1 à `count`), ligne par ligne, de gauche à droite."""
        if not 1 <= position <= self.count:
            raise ValueError(position)
        row, column = divmod(position - 1, self.columns)
        return self.left + column * self.pitch_x, self.top + row * self.pitch_y

    def problems(self) -> list[str]:
        """Ce qui empêche ce gabarit de tenir sur une feuille A4 : une liste vide s'il est utilisable."""
        found: list[str] = []
        if self.columns < 1 or self.rows < 1 or self.count > MAX_LABELS_PER_SHEET:
            found.append(_("Entre 1 et 200 étiquettes par feuille."))
        if self.width < MIN_LABEL[0] or self.height < MIN_LABEL[1]:
            found.append(_("Une étiquette mesure au moins 25 × 12 mm : en dessous, le code ne se lit plus."))
        if self.gap_x < 0 or self.gap_y < 0:
            found.append(_("Les étiquettes se chevauchent : l’espacement ne peut pas être négatif."))
        if self.left < 0 or self.top < 0:
            found.append(_("Les marges ne peuvent pas être négatives."))
        right = self.left + (self.columns - 1) * self.pitch_x + self.width
        bottom = self.top + (self.rows - 1) * self.pitch_y + self.height
        if right > A4[0] + 0.01 or bottom > A4[1] + 0.01:
            found.append(_("Les étiquettes dépassent de la feuille A4 (210 × 297 mm)."))
        return found


def _mm(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


#: Gabarits connus. D'abord les deux formats retenus par le Club (arbitrage du 18 novembre 2026) : L7160, format
#: principal des BD, et L7651, format compact ; formats et quantités confirmés auprès d'Avery, marges et espacements
#: à certifier par la feuille test posée sur une vraie planche. Les autres références portent la mention
#: « calibration nécessaire ». Les sources de chaque cote sont dans le champ `sources` du gabarit.
KNOWN: tuple[Sheet, ...] = (
    Sheet(
        "avery-l7160", "Avery L7160 · J8160", 3, 7, 63.5, 38.1, 15.15, 7.21, 66.04, 38.1, 1.5,
        VERIFIED_CROSS,
        ("Format et quantité (A4, 21 étiquettes de 63,5 × 38,1 mm) confirmés auprès d'Avery (18 novembre 2026)",
         "labelstudio.io : marges 15,2 et 7,3 mm, pas 66 et 38,1 mm",
         "sheetstolabels.com (d'après gLabels) : pas 66,04 et 38,1 mm",
         "page centrée : (297 − 7 × 38,1) / 2 = 15,15 mm ; (210 − 2 × 66,04 − 63,5) / 2 = 7,21 mm"),
        _("Format principal, par exemple pour les BD."),
        FEATURED_MAIN,
    ),
    Sheet(
        "avery-l7651", "Avery L7651 · J8651", 5, 13, 38.1, 21.2, 10.7, 4.75, 40.6, 21.2, 1.0,
        VERIFIED_CROSS,
        ("Format et quantité (A4, 65 étiquettes de 38,1 × 21,2 mm) confirmés auprès d'Avery (18 novembre 2026)",
         "labelstudio.io : marges 10,7 et 4,8 mm, pas 40,6 et 21,2 mm",
         "gLabels (avery-iso-templates, 8651 = L7651) : marges 10,9 et 4,7 mm, pas 40,6 et 21,2 mm",
         "page centrée : (297 − 13 × 21,2) / 2 = 10,7 mm ; (210 − 4 × 40,6 − 38,1) / 2 = 4,75 mm"),
        _("Format compact."),
        FEATURED_COMPACT,
    ),
    Sheet(
        "avery-l7159", "Avery L7159 · J8159", 3, 8, 63.5, 33.9, 12.9, 6.5, 66.0, 33.9, 1.5,
        VERIFIED_SINGLE,
        ("labelstudio.io : marges 12,9 et 6,5 mm, pas 66 et 33,9 mm",
         "page centrée en hauteur : (297 − 8 × 33,9) / 2 = 12,9 mm"),
        _("Calibration nécessaire : cotes non vérifiées (une seule source)."),
    ),
    Sheet(
        "avery-l7654", "Avery L7654 · J8654", 4, 10, 45.7, 25.4, 21.5, 9.4, 48.5, 25.4, 1.5,
        VERIFIED_SINGLE,
        ("gLabels (avery-iso-templates, L4770 = L7654) : pas 48,5 et 25,4 mm, origine 10 et 22 mm",
         "page centrée : (297 − 10 × 25,4) / 2 = 21,5 mm ; (210 − 3 × 48,5 − 45,7) / 2 = 9,4 mm"),
        _("Calibration nécessaire : cotes non vérifiées (une seule source, origine non centrée)."),
    ),
)  # fmt: skip
KNOWN_BY_CODE = {sheet.code: sheet for sheet in KNOWN}
DEFAULT = "avery-l7160"


@dataclass(frozen=True)
class CustomInput:
    columns: str = ""
    rows: str = ""
    width: str = ""
    height: str = ""
    top: str = ""
    left: str = ""
    gap_x: str = "0"
    gap_y: str = "0"


def _decimal(value: str, name: str, errors: dict[str, str], *, low: float, high: float) -> float:
    text = value.strip().replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        errors[name] = _("Indiquez un nombre de millimètres, par exemple 38,1.")
        return 0.0
    if not low <= number <= high:
        errors[name] = _("Entre {low} et {high} mm.").format(low=_mm(low), high=_mm(high))
    return round(number, 2)


def _count(value: str, name: str, errors: dict[str, str]) -> int:
    text = value.strip()
    if not text.isdigit() or not 1 <= int(text) <= 20:
        errors[name] = _("Entre 1 et 20.")
        return 1
    return int(text)


def custom_sheet(data: CustomInput) -> Sheet:
    """Le gabarit décrit par la personne, refusé s'il ne tient pas sur une feuille A4 ou s'il est trop petit."""
    errors: dict[str, str] = {}
    columns, rows = _count(data.columns, "colonnes", errors), _count(data.rows, "lignes", errors)
    width = _decimal(data.width, "largeur", errors, low=MIN_LABEL[0], high=A4[0])
    height = _decimal(data.height, "hauteur", errors, low=MIN_LABEL[1], high=A4[1])
    top = _decimal(data.top, "marge_haut", errors, low=0, high=100)
    left = _decimal(data.left, "marge_gauche", errors, low=0, high=100)
    gap_x = _decimal(data.gap_x or "0", "espace_horizontal", errors, low=0, high=50)
    gap_y = _decimal(data.gap_y or "0", "espace_vertical", errors, low=0, high=50)
    if errors:
        raise FormErrors(errors)
    sheet = Sheet(CUSTOM, _("Gabarit personnalisé"), columns, rows, width, height, top, left, width + gap_x,
                  height + gap_y)  # fmt: skip
    problems = sheet.problems()
    if problems:
        raise FormErrors({"gabarit": " ".join(problems)})
    return sheet


def custom_values(sheet: Sheet) -> dict[str, str]:
    return {
        "colonnes": str(sheet.columns),
        "lignes": str(sheet.rows),
        "largeur": _mm(sheet.width),
        "hauteur": _mm(sheet.height),
        "marge_haut": _mm(sheet.top),
        "marge_gauche": _mm(sheet.left),
        "espace_horizontal": _mm(sheet.gap_x),
        "espace_vertical": _mm(sheet.gap_y),
    }


def as_data(sheet: Sheet) -> dict[str, object]:
    """Ce que garde un lot d'impression : la géométrie employée, même si le gabarit change ensuite."""
    return {
        "code": sheet.code,
        "name": sheet.name,
        "columns": sheet.columns,
        "rows": sheet.rows,
        "width": sheet.width,
        "height": sheet.height,
        "top": sheet.top,
        "left": sheet.left,
        "pitch_x": sheet.pitch_x,
        "pitch_y": sheet.pitch_y,
        "radius": sheet.radius,
    }


def from_data(data: dict[str, object]) -> Sheet:
    known = KNOWN_BY_CODE.get(str(data.get("code")))
    values = {name: data[name] for name in ("columns", "rows") if name in data}
    numbers = {
        name: float(data[name])  # type: ignore[arg-type]
        for name in ("width", "height", "top", "left", "pitch_x", "pitch_y", "radius")
        if name in data
    }
    base = known or Sheet(CUSTOM, _("Gabarit personnalisé"), 1, 1, 25, 12, 0, 0, 25, 12)
    return replace(base, name=str(data.get("name") or base.name), **values, **numbers)  # type: ignore[arg-type]
