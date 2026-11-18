# SPDX-License-Identifier: EUPL-1.2
"""Forme d'un code lu à la douchette ou tapé au clavier, avant toute recherche en base (lot L8).

Rien ici ne lit la base : on dit seulement à quoi ressemble le texte reçu. Un ISBN reste un ISBN, un code interne reste
un code interne ; ni l'un ni l'autre n'est jamais converti en l'autre. La recherche en base vient ensuite
(`modules/scan/resolver.py`), qui seule dit ce que ce code désigne réellement.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from biblyo.core.barcodes import is_valid_ean13
from biblyo.core.isbn import Isbn, parse_isbn

MAX_SCAN_LENGTH = 64

#: Ce qu'écrit un poste français (AZERTY) quand la douchette envoie les touches d'un clavier américain (QWERTY).
#: Les chiffres sont sur la rangée du haut sans majuscule : `0` devient `à`, `1` devient `&`… et le tiret, à droite du
#: zéro en QWERTY, devient `)`. Windows et macOS diffèrent pour le 6 (`-` ou `§`) et le 8 (`_` ou `!`).
AZERTY_FROM_QWERTY: dict[str, str] = {
    "à": "0", "&": "1", "é": "2", '"': "3", "'": "4", "(": "5", "§": "6", "-": "6", "è": "7", "_": "8", "!": "8",
    "ç": "9", ")": "-", "A": "Q", "Q": "A", "Z": "W", "W": "Z", "?": "M", "a": "q", "q": "a", "z": "w", "w": "z",
    ",": "m", ";": "m",
}  # fmt: skip
_QWERTY_SIGNS = frozenset("à&é\"'(§è_!çÀÉÈÇ)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_INTERNAL = re.compile(r"[A-Z0-9](?:[A-Z0-9-]{0,30}[A-Z0-9])?")
#: Codes de l'ancienne reconstruction du catalogue du Club (« CSN-000123 ») : renumérotés, ils ne correspondent à aucun
#: inventaire et ne sont jamais des codes de biblyO, qui s'écrivent sans tiret (« CSN000123 », D-L6-7 et L8).
RECONSTRUCTION_CODE = re.compile(r"^\s*CSN-\d+\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Shape:
    """Ce que le texte reçu semble être. `isbn` n'est renseigné que pour un ISBN dont la clé est juste."""

    text: str  # le texte nettoyé : sans espaces ni caractères de contrôle, en majuscules
    isbn: Isbn | None = None
    ean: str | None = None  # EAN-13 valide qui n'est pas un ISBN (978/979) : un autre produit
    internal: bool = False  # forme d'un code interne, d'un inventaire ou d'une carte (lettres, chiffres, tirets)
    keyboard: str | None = None  # même code relu comme une douchette réglée en QWERTY sur un poste AZERTY
    reconstruction: bool = False  # ancienne étiquette « CSN-… » : jamais un code de biblyO

    @property
    def is_book_number(self) -> bool:
        return self.isbn is not None or self.ean is not None


def clean(raw: str | None) -> str:
    """Retire les caractères de contrôle (suffixe Entrée ou Tab compris), les espaces, et passe en majuscules."""
    text = _CONTROL.sub("", unicodedata.normalize("NFC", raw or ""))[:MAX_SCAN_LENGTH]
    return "".join(text.split()).upper()


def from_qwerty(raw: str) -> str | None:
    """Le code qu'une douchette réglée en QWERTY voulait écrire, si le texte en porte la marque ; sinon `None`."""
    text = "".join((raw or "").split())
    if not any(character in _QWERTY_SIGNS for character in text):
        return None
    repaired = "".join(AZERTY_FROM_QWERTY.get(character, character) for character in text).upper()
    return repaired if _INTERNAL.fullmatch(repaired) or parse_isbn(repaired) else None


def shape(raw: str | None) -> Shape:
    text = clean(raw)
    keyboard = from_qwerty(raw or "")
    isbn = (
        parse_isbn(text) if text and not any(character.isalpha() and character != "X" for character in text) else None
    )
    digits = text.replace("-", "")
    ean = digits if isbn is None and is_valid_ean13(digits) else None
    if ean is None and isbn is None and len(digits) == 12 and digits.isdigit() and is_valid_ean13("0" + digits):
        ean = "0" + digits  # UPC-A, transmis sans son zéro initial par certaines douchettes
    legacy = RECONSTRUCTION_CODE.match(text) is not None
    return Shape(text, isbn, ean, bool(_INTERNAL.fullmatch(text)), keyboard, legacy)
