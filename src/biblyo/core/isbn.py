# SPDX-License-Identifier: EUPL-1.2
"""ISBN : clé de contrôle, forme normalisée, conversion entre ISBN-10 et ISBN-13."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEPARATORS = re.compile(r"[\s\-‐‑–—.]+")
_ISBN10 = re.compile(r"^\d{9}[\dX]$")
_ISBN13 = re.compile(r"^97[89]\d{10}$")
_LIST_SEPARATOR = re.compile(r"[,;/\n]+")


@dataclass(frozen=True)
class Isbn:
    """Un même livre sous ses deux formes. Un ISBN en 979 n'a pas de forme à 10 chiffres."""

    isbn13: str
    isbn10: str | None


def clean(value: str | None) -> str:
    """Retire tirets et espaces, passe le `x` final en majuscule."""
    return _SEPARATORS.sub("", value or "").upper()


def _check_digit_10(digits: str) -> str:
    remainder = (11 - sum((10 - i) * int(c) for i, c in enumerate(digits))) % 11
    return "X" if remainder == 10 else str(remainder)


def _check_digit_13(digits: str) -> str:
    return str((10 - sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(digits))) % 10)


def is_valid_isbn10(value: str) -> bool:
    return bool(_ISBN10.match(value)) and _check_digit_10(value[:9]) == value[9]


def is_valid_isbn13(value: str) -> bool:
    return bool(_ISBN13.match(value)) and _check_digit_13(value[:12]) == value[12]


def to_isbn13(isbn10: str) -> str:
    stem = "978" + isbn10[:9]
    return stem + _check_digit_13(stem)


def to_isbn10(isbn13: str) -> str | None:
    if not isbn13.startswith("978"):
        return None
    stem = isbn13[3:12]
    return stem + _check_digit_10(stem)


def parse_isbn(value: str | None) -> Isbn | None:
    """L'ISBN sous ses deux formes, ou `None` s'il est invalide."""
    text = clean(value)
    if is_valid_isbn13(text):
        return Isbn(text, to_isbn10(text))
    if is_valid_isbn10(text):
        return Isbn(to_isbn13(text), text)
    return None


def split_list(value: str | None) -> list[str]:
    """Une saisie peut porter plusieurs ISBN, séparés par une virgule, un point-virgule ou un saut de ligne."""
    return [part.strip() for part in _LIST_SEPARATOR.split(value or "") if part.strip()]
