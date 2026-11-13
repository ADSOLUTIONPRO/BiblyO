# SPDX-License-Identifier: EUPL-1.2
"""Profil de correspondance d'un import (lot L6, tâche T3 ; conception 21).

Un profil dit, pour chaque colonne lue, ce qu'elle devient dans biblyO : un champ de l'œuvre, de l'édition ou de
l'exemplaire, une proposition « à vérifier », ou une colonne **conservée telle quelle**. Aucune colonne n'est
jamais ignorée : la valeur brute de chaque cellule reste attachée à sa ligne, et revient à l'export.

Deux sortes de règles :

- les **règles validées** (décision du 13 novembre 2026) ne sont pas des réglages : titre exact conservé, tome
  atypique conservé sans numéro choisi, noms et éditeurs gardés tels qu'écrits, cote jamais clé, aucun exemplaire
  fusionné ni renuméroté ;
- les **arbitrages du 13 novembre (PC2)** fixent la règle de D-L6-3, 4, 5 et 7 : libellés d'origine gardés,
  conversion visible et réversible, rôle jamais deviné, collections jamais fusionnées d'office, identifiants source
  conservés et codes-barres attribués plus tard. Les tables de valeurs (états, collections) restent des **réglages
  du profil**, à relire : la règle est approuvée, pas chaque ligne de la table.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from biblyo.core.i18n import _
from biblyo.core.text import normalize
from biblyo.modules.imports.reader import Column, Sheet, Value

PROFILE_VERSION = 1
KEEP = "keep"  # colonne conservée telle quelle : jamais ignorée

#: Niveaux, dans l'ordre de l'écran.
LEVELS: dict[str, str] = {
    "work": _("Œuvre"),
    "edition": _("Édition"),
    "copy": _("Exemplaire"),
    "proposal": _("Proposition à vérifier"),
    "kept": _("Conservée telle quelle"),
}


@dataclass(frozen=True)
class FieldSpec:
    code: str
    label: str
    level: str
    multiple: bool = False  # plusieurs colonnes peuvent y aller (contributeurs, colonnes conservées)
    help: str = ""


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("series", _("Série, ou titre de l'album isolé"), "work", help=_("Titre exact conservé.")),
    FieldSpec("volume", _("Tome"), "work", help=_("Un tome atypique est gardé tel quel, sans numéro choisi.")),
    FieldSpec("title", _("Titre de l'album"), "work"),
    FieldSpec(
        "contributor",
        _("Auteur ou contributeur"),
        "work",
        multiple=True,
        help=_("Dans l'ordre des colonnes ; nom gardé tel qu'écrit."),
    ),
    FieldSpec("publisher", _("Éditeur"), "edition", help=_("Libellé d'origine gardé ; comparaison sans casse.")),
    FieldSpec("collection", _("Collection"), "edition"),
    FieldSpec("isbn", _("ISBN établi (sur le livre)"), "edition"),
    FieldSpec("year", _("Année d'édition établie"), "edition"),
    FieldSpec("isbn_proposed", _("ISBN à vérifier"), "proposal", help=_("Jamais un fait : proposition.")),
    FieldSpec("year_proposed", _("Année à vérifier"), "proposal", help=_("Jamais un fait : proposition.")),
    FieldSpec(
        "location_hint",
        _("Emplacement indiqué (non vérifié)"),
        "proposal",
        help=_("Proposition de rangement : l'exemplaire reste non rangé tant que sa place n'est pas vérifiée."),
    ),
    FieldSpec("inventory", _("Numéro d'inventaire"), "copy", help=_("Identifiant d'origine de l'exemplaire.")),
    FieldSpec("call_number", _("Cote"), "copy", help=_("Gardée au caractère près ; jamais une clé.")),
    FieldSpec("condition", _("État"), "copy", help=_("La valeur d'origine reste en précision sur l'état.")),
    FieldSpec("acquisition", _("Date ou note d'acquisition"), "copy"),
    FieldSpec("barcode", _("Code-barres déjà attribué"), "copy", help=_("Repris tel quel, jamais réattribué.")),
    FieldSpec("copy_note", _("Note sur l'exemplaire"), "copy"),
    FieldSpec(KEEP, _("Conservée telle quelle"), "kept", multiple=True, help=_("Gardée, lisible et exportée.")),
)
FIELD_BY_CODE: dict[str, FieldSpec] = {spec.code: spec for spec in FIELDS}

#: États de biblyO (contrainte de `copies.condition`).
CONDITIONS: dict[str, str] = {
    "": _("Non renseigné"),
    "new": _("Neuf"),
    "good": _("Bon"),
    "fair": _("Correct"),
    "worn": _("Usé"),
    "damaged": _("Abîmé"),
}
COLLECTION_KINDS: dict[str, str] = {
    "collection": _("Collection"),
    "mention": _("Mention d'édition"),
    "note": _("Note sur l'édition"),
}
ROLE_MODES: dict[str, str] = {
    "unspecified": _("Rôle à préciser pour tous"),
    "first_author": _("Auteur, puis rôle à préciser"),
}
BARCODE_MODES: dict[str, str] = {
    "deferred": _("Inventaire gardé, code-barres plus tard"),
    "column": _("Étiquettes existantes"),
    "generate": _("Codes générés (non retenu)"),
}

#: Règle de chaque réglage, montrée à l'écran : (statut, décision, règle). « validated » : la règle est arbitrée
#: (13 novembre, PC2) ; la table de valeurs qu'elle encadre reste un réglage du profil, à relire.
RULE_STATUS: dict[str, tuple[str, str, str]] = {
    "condition_map": (
        "validated",
        "D-L6-3",
        _("Libellé d'origine toujours gardé en précision sur l'état ; une valeur non rangée reste sans état et "
          "devient une proposition à vérifier. Table à relire."),
    ),
    "roles": (
        "validated",
        "D-L6-4",
        _("Aucun rôle n'est deviné : chaque nom garde sa colonne et sa position ; le rôle reste à préciser."),
    ),
    "collection_map": (
        "validated",
        "D-L6-5",
        _("Chaque valeur reste distincte, même entre noms proches ; rapprochement manuel avec une collection existante "
          "possible. Table à relire."),
    ),
    "barcode": (
        "validated",
        "D-L6-7",
        _("Identifiants source conservés, étiquettes reportées : aucun code inventé, aucun code de la reconstruction "
          "de l’ancienne application, aucun code existant écrasé."),
    ),
}  # fmt: skip

#: Règles validées le 13 novembre 2026, rappelées sur l'écran de correspondance et dans le rapport.
VALIDATED_RULES: tuple[str, ...] = (
    _("Une cote n'est jamais une clé : deux livres peuvent la partager."),
    _("Un ISBN seul n'identifie pas une édition ; deux lignes semblables ne prouvent pas un doublon physique."),
    _("Chaque ligne garde un identifiant de source stable ; aucun exemplaire n'est supprimé, fusionné ni renuméroté."),
    _("Titre exact conservé ; un titre à article inversé reçoit en plus une forme de recherche."),
    _("Tome atypique conservé tel quel : aucun numéro n'est choisi."),
    _("Noms gardés tels qu'écrits ; nom inversé, noms multiples ou organisation : propositions à vérifier."),
    _("Éditeurs comparés sans casse ni accents ; libellés d'origine jamais modifiés ; éditions contradictoires "
      "gardées distinctes."),
    _("Le fichier complémentaire ne fait pas foi : ses écarts sont des différences de version, ses valeurs ajoutées "
      "des propositions."),
)  # fmt: skip

# --- Suggestions -----------------------------------------------------------------------------------------------------
_EXACT: dict[str, str] = {
    "titre": "series",
    "serie": "series",
    "series": "series",
    "titre de la serie": "series",
    "tome": "volume",
    "volume": "volume",
    "numero de tome": "volume",
    "no tome": "volume",
    "sous titre": "title",
    "titre de l album": "title",
    "titre album": "title",
    "album": "title",
    "auteur": "contributor",
    "auteurs": "contributor",
    "scenariste": "contributor",
    "scenario": "contributor",
    "dessinateur": "contributor",
    "dessin": "contributor",
    "illustrateur": "contributor",
    "coloriste": "contributor",
    "edition": "publisher",
    "editeur": "publisher",
    "editions": "publisher",
    "maison d edition": "publisher",
    "collection": "collection",
    "isbn": "isbn",
    "ean": "isbn",
    "annee": "year",
    "annee d edition": "year",
    "annee de publication": "year",
    "date de publication": "year",
    "numero d inventaire": "inventory",
    "n inventaire": "inventory",
    "no inventaire": "inventory",
    "inventaire": "inventory",
    "cote": "call_number",
    "etat": "condition",
    "date d acquisition": "acquisition",
    "acquisition": "acquisition",
    "code barres": "barcode",
    "code barre": "barcode",
    "note": "copy_note",
    "remarque": "copy_note",
    "emplacement": "location_hint",
    "localisation": "location_hint",
    "etagere": "location_hint",
    "rayon": "location_hint",
    "meuble": "location_hint",
}
_TO_CHECK = ("a verifier", "verifier", "reference a verifier", "propose", "proposition")


@dataclass(frozen=True)
class Suggestion:
    field: str
    confidence: str  # exact, probable, none
    reason: str


def suggest(header: str, headers: list[str]) -> Suggestion:
    """Champ suggéré pour un en-tête. « Titre » est la série quand le fichier a aussi un « Sous-titre » ou un tome."""
    key = normalize(header)
    keys = {normalize(h) for h in headers}
    if not key:
        return Suggestion(KEEP, "none", _("En-tête vide : colonne conservée telle quelle."))
    to_check = any(word in key for word in _TO_CHECK)
    if "isbn" in key or "ean" in key.split():
        if to_check:
            return Suggestion("isbn_proposed", "exact", _("ISBN marqué « à vérifier » : proposition."))
        return Suggestion("isbn", "probable", _("ISBN sans mention « à vérifier » : à confirmer."))
    if key.startswith("annee") or "date de publication" in key:
        if to_check:
            return Suggestion("year_proposed", "exact", _("Année marquée « à vérifier » : proposition."))
        return Suggestion("year", "probable", _("Année sans mention « à vérifier » : à confirmer."))
    numbered = key.rstrip("0123456789 ").strip()
    if numbered in ("auteur", "auteurs", "contributeur") and numbered != key:
        return Suggestion("contributor", "exact", _("Contributeur supplémentaire, dans l'ordre des colonnes."))
    if key == "titre":
        has_subtitle = bool(keys & {"sous titre", "titre de l album", "titre album", "album"})
        if has_subtitle or "tome" in keys:
            return Suggestion("series", "probable", _("« Titre » avec un tome ou un sous-titre : titre de la série."))
        return Suggestion("title", "probable", _("« Titre » seul : titre du livre."))
    found = _EXACT.get(key)
    if found:
        return Suggestion(found, "exact", _("En-tête reconnu."))
    for word, code in (("inventaire", "inventory"), ("cote", "call_number"), ("editeur", "publisher")):
        if word in key.split():
            return Suggestion(code, "probable", _("En-tête proche."))
    return Suggestion(KEEP, "none", _("Aucun champ reconnu : colonne conservée telle quelle, lisible et exportée."))


_CONDITION_WORDS: tuple[tuple[str, str], ...] = (
    ("neuf", "new"),
    ("tres bon", "good"),
    ("satisfaisant", "good"),
    ("bon", "good"),
    ("leg", "fair"),  # « leg. usure »
    ("leger", "fair"),
    ("nettoyable", "fair"),
    ("correct", "fair"),
    ("moyen", "fair"),
    ("use", "worn"),
    ("usure", "worn"),
    ("abime", "damaged"),
    ("degrade", "damaged"),
    ("mauvais", "damaged"),
)
_MENTION_WORDS = ("integrale", "double album", "double albums", "omnibus", "hors serie", "hs", "edition speciale")


#: Libellés qui **sont** un état de biblyO, mot pour mot (sans casse ni accents) : seule conversion faite d'office.
_CONDITION_EXACT: dict[str, str] = {
    "neuf": "new",
    "bon": "good",
    "bon etat": "good",
    "correct": "fair",
    "use": "worn",
    "abime": "damaged",
}


def suggest_condition(value: str) -> str:
    """État appliqué d'office à une valeur d'origine : seulement si elle **est** un état de biblyO (« usé »).

    Arbitrage du 13 novembre (« ne pas appliquer de transformation ambiguë par défaut ») : « satisfaisant »,
    « leg. usure » ou « nettoyable » restent sans état tant que la table du profil ne les range pas ; la
    recommandation (`recommend_condition`) est montrée à côté, jamais appliquée seule."""
    return _CONDITION_EXACT.get(normalize(value), "")


def recommend_condition(value: str) -> str:
    """Recommandation pour une valeur ambiguë (D-L6-3), affichée dans la correspondance, jamais appliquée seule."""
    key = normalize(value)
    for word, code in _CONDITION_WORDS:
        if key == word or key.startswith(f"{word} "):
            return code
    words = key.split()
    for word, code in _CONDITION_WORDS:
        if (" " in word and word in key) or word in words:
            return code
    return ""


def suggest_collection(value: str) -> str:
    """Rangement proposé d'une valeur de « Collection » (D-L6-5, provisoire) : mention d'édition ou collection."""
    key = normalize(value)
    words = key.split()
    if any((w in words) if " " not in w else (w in key) for w in _MENTION_WORDS):
        return "mention"
    return "collection"


# --- Profil ----------------------------------------------------------------------------------------------------------
@dataclass
class Profile:
    """Correspondance appliquée par la simulation. Sérialisable en JSON, stable (même profil, même empreinte)."""

    columns: dict[str, str] = field(default_factory=dict)  # libellé de colonne → code de champ
    condition_map: dict[str, str] = field(default_factory=dict)  # valeur d'origine → état
    collection_map: dict[str, str] = field(default_factory=dict)  # valeur d'origine → collection, mention, note
    collection_links: dict[str, int] = field(default_factory=dict)  # valeur d'origine → collection existante (D-L6-5)
    collection_canonical: dict[str, str] = field(default_factory=dict)  # valeur d'origine → forme retenue (D-L6-5)
    roles: str = "unspecified"
    barcode: str = "deferred"
    barcode_prefix: str = "EX-"
    document_type: str = "bd"
    language: str = "fre"
    name: str = ""

    def field_of(self, column: Column) -> str:
        return self.columns.get(column.label, KEEP)

    def columns_for(self, sheet: Sheet, code: str) -> list[Column]:
        return [column for column in sheet.columns if self.field_of(column) == code]

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": PROFILE_VERSION,
            "columns": self.columns,
            "condition_map": self.condition_map,
            "collection_map": self.collection_map,
            "collection_links": self.collection_links,
            "collection_canonical": self.collection_canonical,
            "roles": self.roles,
            "barcode": self.barcode,
            "barcode_prefix": self.barcode_prefix,
            "document_type": self.document_type,
            "language": self.language,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    @classmethod
    def from_json(cls, text: str | None, *, name: str = "") -> Profile:
        try:
            data = json.loads(text or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        def mapping(key: str, allowed: set[str] | None) -> dict[str, str]:
            raw = data.get(key) or {}
            if not isinstance(raw, dict):
                return {}
            return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and (allowed is None or v in allowed)}

        roles = str(data.get("roles") or "unspecified")
        barcode = str(data.get("barcode") or "deferred")
        barcode = "deferred" if barcode == "none" else barcode  # profils enregistrés avant les arbitrages de PC2
        links = data.get("collection_links") or {}
        return cls(
            columns=mapping("columns", set(FIELD_BY_CODE)),
            condition_map=mapping("condition_map", set(CONDITIONS)),
            collection_map=mapping("collection_map", set(COLLECTION_KINDS)),
            collection_links={
                str(k): int(v) for k, v in (links.items() if isinstance(links, dict) else ()) if isinstance(v, int)
            },
            collection_canonical=mapping("collection_canonical", None),
            roles=roles if roles in ROLE_MODES else "unspecified",
            barcode=barcode if barcode in BARCODE_MODES else "deferred",
            barcode_prefix=str(data.get("barcode_prefix") or "EX-")[:12],
            document_type=str(data.get("document_type") or "bd")[:40],
            language=str(data.get("language") or "fre")[:3],
            name=name,
        )


def distinct_values(sheet: Sheet, column: Column) -> list[tuple[str, int]]:
    """Valeurs distinctes d'une colonne (texte affiché, espaces de bord retirés) et leur nombre, les plus fréquentes
    d'abord."""
    counts: dict[str, int] = {}
    for row in sheet.rows:
        text = display(row.get(column)).strip()
        if text:
            counts[text] = counts.get(text, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], normalize(item[0]), item[0]))


def detect(sheet: Sheet, base: Profile | None = None) -> Profile:
    """Profil proposé pour une feuille : suggestions pour les colonnes absentes du profil de base, tables de valeurs
    complétées pour les valeurs nouvelles. Un profil enregistré n'est jamais modifié ici : on en part."""
    profile = Profile.from_json(base.to_json(), name=base.name) if base else Profile()
    headers = [column.label for column in sheet.columns]
    for column in sheet.columns:
        if column.label not in profile.columns:
            profile.columns[column.label] = suggest(column.label, headers).field
    for column in profile.columns_for(sheet, "condition"):
        for value, _count in distinct_values(sheet, column):
            profile.condition_map.setdefault(value, suggest_condition(value))
    for column in profile.columns_for(sheet, "collection"):
        for value, _count in distinct_values(sheet, column):
            profile.collection_map.setdefault(value, suggest_collection(value))
    return profile


def display(value: Value) -> str:
    """Texte d'une cellule tel qu'un tableur l'affiche à peu près : 12.0 donne « 12 », 20.21 donne « 20,21 »."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "VRAI" if value else "FAUX"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value).replace(".", ",")
    return str(value)


def risky(sheet: Sheet, column: Column, code: str) -> list[str]:
    """Conversions risquées d'une colonne vers son champ, dites avant la simulation."""
    notes: list[str] = []
    values = [row.get(column) for row in sheet.rows]
    filled = [v for v in values if v is not None and str(v).strip()]
    kinds = {type(v).__name__ for v in filled}
    if code == "volume":
        odd = [display(v) for v in filled if not (isinstance(v, int) or (isinstance(v, float) and v.is_integer()))]
        if odd:
            notes.append(
                _("{n} tome(s) non entier(s) ou en texte (« {ex} ») : gardés tels quels, sans numéro choisi.").format(
                    n=len(odd), ex="», «".join(odd[:3])
                )
            )
    if code in ("inventory", "barcode"):
        texts = [display(v).strip() for v in filled]
        if len(set(texts)) != len(texts):
            notes.append(_("Valeurs en double : chaque doublon sera refusé à la simulation."))
        if len(filled) != len(values):
            notes.append(_("{n} ligne(s) sans valeur.").format(n=len(values) - len(filled)))
    if code == "call_number" and kinds & {"int", "float"}:
        notes.append(_("Cotes lues comme des nombres : le texte affiché est gardé."))
    if code in ("isbn", "isbn_proposed") and kinds & {"int", "float"}:
        notes.append(_("ISBN lus comme des nombres : un zéro initial a pu être perdu par le tableur."))
    if code in ("year", "year_proposed"):
        bad = [v for v in filled if not (isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()))]
        if bad:
            notes.append(_("{n} valeur(s) qui ne sont pas une année.").format(n=len(bad)))
    if code == "series" and not filled:
        notes.append(_("Colonne vide."))
    if len(kinds) > 1 and code not in ("volume", KEEP):
        notes.append(_("Types mêlés ({kinds}) : le texte affiché est gardé.").format(kinds=", ".join(sorted(kinds))))
    return notes
