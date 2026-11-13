# SPDX-License-Identifier: EUPL-1.2
"""Clés de comparaison bibliographiques : titres, personnes, éditeurs, années, pages, tomes.

Aucune base, aucun réseau. Ces règles servent au rapprochement avec les sources, et serviront au contrôle des
doublons de l'import : deux moteurs ne doivent pas juger différemment un même titre.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from biblyo.core.isbn import parse_isbn
from biblyo.core.text import normalize, title_sort

MIN_YEAR, MAX_YEAR = 1400, 2200
MAX_ROMAN_VOLUME = 200  # au-delà, des lettres comme « M » ou « D » ne sont pas un numéro de tome
_YEAR = re.compile(r"(?<!\d)(1[4-9]\d\d|2[01]\d\d)(?!\d)")
_PAGES = re.compile(r"\[?\s*(?:ca\.?\s*)?(\d{1,5})\s*\]?\s*(?:p\b|pages?\b)", re.IGNORECASE)
_BRACKETED = re.compile(r"\[\s*(?:ca\.?\s*)?(\d{1,5})\s*(?:p\.?)?\s*\]", re.IGNORECASE)
_VOLUME_PREFIX = re.compile(r"^(?:t(?:ome)?|vol(?:ume)?|n[°o]?|bd|band|livre|#)\s*\.?\s*", re.IGNORECASE)
_ROMAN = re.compile(r"^(?=[ivxlcdm]+$)m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
#: Mots sans valeur dans un nom d'éditeur : « Éditions Albert René », « les Éditions Albert-René », « Albert René ».
_PUBLISHER_NOISE = frozenset(
    {"editions", "edition", "ed", "eds", "editeur", "editeurs", "les", "le", "la", "l", "librairie"}
)
_PARTICLES = frozenset({"de", "du", "des", "d", "la", "le", "von", "van", "der", "den", "di", "da"})


def title_key(title: str | None) -> str:
    """Clé d'un titre : sans accent, sans casse, sans ponctuation, sans article initial."""
    return title_sort(title)


def similarity(left: str | None, right: str | None) -> float:
    """Ressemblance de deux titres, de 0 à 1. Deux titres vides ne se ressemblent pas."""
    a, b = title_key(left), title_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def person_tokens(name: str | None) -> frozenset[str]:
    """Mots porteurs d'un nom : « Antoine de Saint-Exupéry » et « Saint-Exupéry, Antoine de » donnent le même jeu."""
    return frozenset(word for word in normalize(name).split() if word not in _PARTICLES)


def same_person(local: str | None, surname: str, forename: str = "") -> bool:
    """La personne saisie localement est-elle celle de la source ?

    Le nom de famille doit s'y retrouver en entier. Le prénom, s'il est donné des deux côtés, doit concorder :
    en toutes lettres, ou par son initiale (« R. Goscinny »). Deux prénoms différents sont deux personnes.
    """
    mine = person_tokens(local)
    family = person_tokens(surname)
    if not mine or not family or not family <= mine:
        return False
    given_here = mine - family
    given_there = person_tokens(forename)
    if not given_here or not given_there:
        return True
    if given_here & given_there:
        return True
    initials_here = {word[0] for word in given_here}
    initials_there = {word[0] for word in given_there}
    short = all(len(word) == 1 for word in given_here) or all(len(word) == 1 for word in given_there)
    return short and bool(initials_here & initials_there)


_SERIES_STOPWORDS = frozenset({"une", "un", "les", "le", "la", "l", "de", "d", "des", "du", "the", "a", "an"})


def series_conflict(mine: str | None, theirs: str | None) -> bool:
    """Deux noms de série qui ne peuvent pas désigner la même : « Astérix » et « Une aventure d'Astérix » le peuvent,
    « Les aventures de Tintin » et « Une aventure d'Astérix » non. Un nom vide ne contredit rien."""
    a = {w for w in normalize(mine).split() if w not in _SERIES_STOPWORDS}
    b = {w for w in normalize(theirs).split() if w not in _SERIES_STOPWORDS}
    if not a or not b or a <= b or b <= a:
        return False
    return similarity(mine, theirs) < 0.70


def publisher_key(name: str | None) -> str:
    """« Les Éditions Albert-René » et « Éditions Albert René » ont la même clé : « albert rene »."""
    words = [word for word in normalize(name).split() if word not in _PUBLISHER_NOISE]
    return " ".join(words)


def publisher_relation(left: str | None, right: str | None) -> str:
    """`same`, `related` (« Gallimard » et « Gallimard jeunesse ») ou `different`. Vide : `unknown`."""
    a, b = publisher_key(left), publisher_key(right)
    if not a or not b:
        return "unknown"
    if a == b:
        return "same"
    words_a, words_b = a.split(), b.split()
    shorter, longer = sorted((words_a, words_b), key=len)
    if longer[: len(shorter)] == shorter:
        return "related"
    return "different"


def parse_year(raw: str | None) -> int | None:
    """Première année lisible : « DL 2007 », « impr. 2014 », « cop. 2006- », « [1980 (DL)] ». « [s.d.?] » : rien."""
    match = _YEAR.search(raw or "")
    if match is None:
        return None
    year = int(match.group(1))
    return year if MIN_YEAR <= year <= MAX_YEAR else None


def is_open_date(raw: str | None) -> bool:
    """« 1962- », « cop. 2006- » : une publication qui se poursuit, donc une notice d'ensemble."""
    return bool(re.search(r"\d{4}\s*-\s*(?:\.{2,}|\]|$)", (raw or "").strip()))


def parse_pages(raw: str | None) -> int | None:
    """Nombre de pages d'un volume, ou rien si la description en donne plusieurs.

    « 1 vol. (113 p.) » : 113 ; « non paginé [ca 208] p. » : 208 ; « 1 vol. (297 p.-[2] p. dépl.) » : 297 ;
    « 46 p., 48 p., 47 p., 48 p » ou « 4 vol. » : rien, car ce n'est pas un volume simple.
    """
    text = raw or ""
    counts = {int(value) for value in _PAGES.findall(text)} | {int(value) for value in _BRACKETED.findall(text)}
    counts.discard(0)
    if not counts:
        return None
    ordered = sorted(counts, reverse=True)
    if len(ordered) == 1 or ordered[0] >= 5 * ordered[1]:
        return ordered[0]
    return None


def roman_to_int(text: str) -> int | None:
    word = text.strip().casefold()
    if not word or not _ROMAN.match(word):
        return None
    total = 0
    for current, following in zip(word, [*word[1:], ""], strict=True):
        value = _ROMAN_VALUES[current]
        total += -value if following and _ROMAN_VALUES[following] > value else value
    return total or None


def parse_volume(raw: str | None) -> float | None:
    """Numéro de tome : « 5 », « T. 5 », « [1] », « Bd 6 », « V », « 12,5 ». Un libellé (« HS 2 ») ne donne rien."""
    text = (raw or "").strip().strip("[]() .")
    text = _VOLUME_PREFIX.sub("", text).strip("[]() .")
    if not text:
        return None
    if re.fullmatch(r"\d{1,4}(?:[.,]\d)?", text):
        return float(text.replace(",", "."))
    roman = roman_to_int(text)
    return float(roman) if roman is not None and roman <= MAX_ROMAN_VOLUME else None


def isbn13_of(raw: str | None) -> str | None:
    """ISBN-13 normalisé, clé de contrôle recalculée. Un ISBN mal formé ne donne rien : il n'est jamais corrigé."""
    parsed = parse_isbn(raw)
    return parsed.isbn13 if parsed else None


def isbn10_of(isbn13: str) -> str | None:
    parsed = parse_isbn(isbn13)
    return parsed.isbn10 if parsed else None


def clean_text(value: str | None) -> str:
    """Espaces réduits, crochets de restitution retirés : « [Paris] » devient « Paris »."""
    text = " ".join((value or "").split())
    return text.strip().strip("[]").strip()


def search_terms(value: str | None, *, limit: int = 8) -> str:
    """Termes sûrs pour une requête : ni guillemet, ni opérateur, ni accent, ni article élidé.

    Les deux sources ignorent les accents quand ils sont absents ; le Sudoc recommande de ne pas les saisir.
    """
    words = [
        word
        for word in normalize(value).split()
        if word not in {"and", "or", "not"} and (len(word) > 1 or word.isdigit())
    ]
    return " ".join(words[:limit])
