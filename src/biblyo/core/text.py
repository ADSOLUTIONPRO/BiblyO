# SPDX-License-Identifier: EUPL-1.2
"""Normalisation du texte : recherche et comparaison sans accents ni casse."""

from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r"[^a-z0-9]+")
_LIGATURES = str.maketrans({"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE"})
_LEADING_ARTICLE = re.compile(r"^(?:l|le|la|les|un|une|des|d) (?=\S)")


def normalize(value: str | None) -> str:
    """« L'Été à Noël » devient « l ete a noel »."""
    if not value:
        return ""
    expanded = value.translate(_LIGATURES)  # NFKD ne décompose pas ces ligatures, ni le tokeniseur de FTS5
    decomposed = unicodedata.normalize("NFKD", expanded)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return _NON_WORD.sub(" ", stripped).strip()


def title_sort(title: str | None) -> str:
    """Clé de tri d'un titre, sans article initial : « L'Agent 212 » est classé à « agent 212 »."""
    text = normalize(title)
    without_article = _LEADING_ARTICLE.sub("", text)
    return without_article or text


def sort_name(name: str | None) -> str:
    """Clé de tri d'une personne : le dernier mot d'abord. « René Goscinny » est classé à « goscinny rene »."""
    words = normalize(name).split()
    return " ".join([*words[-1:], *words[:-1]])


def split_terms(value: str | None) -> list[str]:
    """Liste saisie, séparée par des virgules ou des points-virgules : sans vide ni doublon, l'ordre conservé."""
    seen: set[str] = set()
    terms: list[str] = []
    for part in re.split(r"[,;\n]+", value or ""):
        term = " ".join(part.split())
        key = normalize(term)
        if key and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def initials(value: str, limit: int = 2) -> str:
    """Initiales affichées dans les pastilles : « Médiathèque Les Tilleuls » donne « ML »."""
    words = [w for w in re.split(r"[\s\-']+", value.strip()) if w]
    return "".join(w[0] for w in words[:limit]).upper()
