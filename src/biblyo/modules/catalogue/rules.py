# SPDX-License-Identifier: EUPL-1.2
"""Règles pures du catalogue : aucune base, aucun réseau. Les libellés suivent la maquette biblyO."""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

from biblyo.core.i18n import _
from biblyo.core.text import normalize

CONTRIBUTOR_ROLES: dict[str, str] = {
    "author": _("Auteur"),
    "scenarist": _("Scénariste"),
    "artist": _("Dessinateur"),
    "colorist": _("Coloriste"),
    "illustrator": _("Illustrateur"),
    "translator": _("Traducteur"),
    "editor": _("Directeur de publication"),
    "adapter": _("Adaptateur"),
    "other": _("Autre"),
}
AUDIENCES: dict[str, str] = {
    "children": _("Enfants"),
    "teen": _("Adolescents"),
    "adult": _("Adultes"),
    "all": _("Tout public"),
}
LANGUAGES: dict[str, str] = {
    "fre": _("Français"),
    "eng": _("Anglais"),
    "ger": _("Allemand"),
    "spa": _("Espagnol"),
    "ita": _("Italien"),
    "dut": _("Néerlandais"),
    "por": _("Portugais"),
    "jpn": _("Japonais"),
    "lat": _("Latin"),
    "mul": _("Plusieurs langues"),
    "und": _("Non déterminée"),
}
SERIES_STATUSES: dict[str, str] = {
    "ongoing": _("En cours"),
    "complete": _("Terminée"),
    "stopped": _("Arrêtée"),
    "unknown": _("Non renseigné"),
}
COPY_STATUSES: dict[str, str] = {
    "in_processing": _("En préparation"),
    "available": _("En place"),
    "in_repair": _("En réparation"),
    "missing": _("Introuvable"),
    "lost": _("Perdu"),
    "withdrawn": _("Retiré"),
}
#: Variante de pastille de chaque statut d'exemplaire ; le libellé porte le sens, la couleur ne fait que le souligner.
COPY_STATUS_BADGES: dict[str, str] = {
    "in_processing": "info",
    "available": "ok",
    "in_repair": "due",
    "missing": "late",
    "lost": "late",
    "withdrawn": "neutral",
}


def code_of(barcode: str | None, inventory: str) -> str:
    """Code qui désigne un exemplaire à l'écran et au scan : son code-barres, sinon son numéro d'inventaire.

    Révision 0012, D-L6-7 : un exemplaire importé n'a pas encore de code-barres ; son inventaire d'origine suffit à le
    retrouver (le comptoir et « Localiser » cherchent aussi par inventaire)."""
    return barcode or inventory


TAG_KINDS: dict[str, str] = {"category": _("Catégories"), "genre": _("Genres"), "keyword": _("Mots-clés")}
ACTIVE_COPY_EXCLUDED = frozenset({"withdrawn", "lost"})
COVER_PALETTE_SIZE = 9  # les neuf couleurs de la maquette : classes .cv-0 à .cv-8

_NUMERIC_VOLUME = re.compile(r"^\d+(?:[.,]\d+)?$")
_VOLUME_PREFIX = re.compile(r"^(?:t(?:ome)?|vol(?:ume)?|n[°o]?)\s*\.?\s*(?=\d)", re.IGNORECASE)
_MAX_VOLUME_LABEL = 60


class InvalidVolume(ValueError):
    """Numéro de tome refusé."""


@dataclass(frozen=True)
class Volume:
    number: float | None = None
    label: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.number is None and self.label is None


def parse_volume(text: str | None) -> Volume:
    """« 12 », « T. 12 », « 12,5 » donnent un numéro. « HS 2 » reste un libellé. Rien de négatif."""
    raw = " ".join((text or "").split())
    if not raw:
        return Volume()
    if raw.startswith("-"):
        raise InvalidVolume(_("Le numéro de tome ne peut pas être négatif."))
    if len(raw) > _MAX_VOLUME_LABEL:
        raise InvalidVolume(_("Le numéro de tome est trop long."))
    bare = _VOLUME_PREFIX.sub("", raw)
    if _NUMERIC_VOLUME.match(bare):
        return Volume(number=float(bare.replace(",", ".")))
    return Volume(label=raw)


def format_number(number: float) -> str:
    """12.0 s'écrit « 12 », 12.5 s'écrit « 12,5 »."""
    return str(int(number)) if float(number).is_integer() else str(number).replace(".", ",")


def format_volume(number: float | None, label: str | None) -> str:
    if label:
        return label
    return format_number(number) if number is not None else ""


# Pondération reprise de la maquette : le total fait 100.
COMPLETENESS_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("isbn", _("ISBN"), 20),
    ("publisher", _("Éditeur"), 12),
    ("year", _("Année"), 10),
    ("summary", _("Résumé"), 14),
    ("cover", _("Couverture"), 14),
    ("categories", _("Catégories"), 8),
    ("contributors", _("Auteurs"), 10),
    ("pages", _("Pages"), 6),
    ("collection", _("Collection"), 6),
)


def completeness(facts: dict[str, bool]) -> tuple[int, list[str]]:
    """Pourcentage de complétude et libellés de ce qui manque."""
    score = sum(weight for key, _label, weight in COMPLETENESS_FIELDS if facts.get(key))
    missing = [label for key, label, _weight in COMPLETENESS_FIELDS if not facts.get(key)]
    return score, missing


def cover_color(key: str | None) -> int:
    """Couleur stable du visuel de remplacement : même série, même couleur, sur tous les postes."""
    return zlib.crc32(normalize(key).encode()) % COVER_PALETTE_SIZE


@dataclass(frozen=True)
class VolumeState:
    """Un emplacement de la frise d'une série."""

    number: float
    state: str  # present, missing (confirmé), unverified (trou non confirmé)
    label: str = ""


def series_strip(known: set[float], confirmed: set[float]) -> list[VolumeState]:
    """Frise d'une série. **Un numéro absent d'une suite n'est pas un tome manquant** : seul un tome confirmé l'est.

    Les trous entre 1 et le plus grand numéro connu restent « à vérifier » : rien ne dit qu'ils ont paru.
    Les numéros décimaux connus s'intercalent ; ils n'ouvrent aucun trou.
    """
    whole_known = [n for n in known | confirmed if float(n).is_integer()]
    top = int(max(whole_known)) if whole_known else 0
    numbers = {float(n) for n in range(1, top + 1)} | known | confirmed
    states: list[VolumeState] = []
    for number in sorted(numbers):
        if number in known:
            state = "present"
        elif number in confirmed:
            state = "missing"
        else:
            state = "unverified"
        states.append(VolumeState(number, state, format_number(number)))
    return states
