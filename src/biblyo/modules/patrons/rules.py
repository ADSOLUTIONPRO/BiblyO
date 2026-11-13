# SPDX-License-Identifier: EUPL-1.2
"""Règles pures des lecteurs, foyers et responsables (plan de L3, décisions D1 à D17).

**L'identité d'un côté, les relations de l'autre.** Un lecteur a un identifiant interne immuable (UUID), un numéro
permanent lisible (`P-000001`) et des cartes successives (`L-000001`) : trois choses distinctes. Ses foyers et ses
responsables sont des liens, et **un lien ne donne un droit que s'il le porte explicitement**.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from biblyo.core.i18n import _
from biblyo.core.text import normalize


class PatronError(ValueError):
    """Règle refusée : le message s'adresse à la personne qui a fait la demande."""


# --- Vocabulaires -----------------------------------------------------------------------------------------------------
CATEGORIES: dict[str, str] = {"adult": _("Adulte"), "minor": _("Mineur")}
STATUSES: dict[str, str] = {
    "active": _("Actif"),
    "suspended": _("Suspendu"),
    "archived": _("Archivé"),
    "anonymized": _("Anonymisé"),
}
HOUSEHOLD_KINDS: dict[str, str] = {"family": _("Famille"), "structure": _("Structure")}
#: Rôles **descriptifs** dans un foyer : ils ne donnent aucun droit (D4).
MEMBER_ROLES: dict[str, dict[str, str]] = {
    "family": {"responsible": _("Responsable"), "member": _("Membre"), "child": _("Enfant")},
    "structure": {"referent": _("Référent"), "member": _("Membre")},
}
RELATIONS: dict[str, str] = {
    "parent": _("Parent"),
    "legal_guardian": _("Tuteur légal"),
    "teacher": _("Enseignant"),
    "other": _("Autre"),
}
CARD_END_REASONS: dict[str, str] = {
    "lost": _("Perdue"),
    "damaged": _("Abîmée"),
    "replaced": _("Remplacée"),
    "revoked": _("Révoquée"),
    "anonymized": _("Lecteur anonymisé"),
}
CARD_ENDED_NOTE = _("trouvé par une carte qui n’est plus en service")
ANONYMIZE_REASONS: dict[str, str] = {
    "request": _("Demande de la personne"),
    "retention": _("Durée de conservation dépassée"),
    "other": _("Autre motif"),
}
#: Ce que dit l'historique d'une fiche : qui a fait quoi, quels champs, jamais les valeurs (D10).
HISTORY_ACTIONS: dict[str, str] = {
    "patron.register": _("Inscription"),
    "patron.update": _("Fiche modifiée"),
    "patron.contacts": _("Coordonnées modifiées"),
    "patron.contacts_shown": _("Coordonnées affichées"),
    "patron.majority": _("Majorité confirmée"),
    "patron.renew": _("Adhésion renouvelée"),
    "patron.suspend": _("Suspension"),
    "patron.reactivate": _("Réactivation"),
    "patron.archive": _("Archivage"),
    "patron.card": _("Carte changée"),
    "patron.household_joined": _("Rattachement à un foyer"),
    "patron.household_role": _("Rôle dans un foyer modifié"),
    "patron.household_right": _("Droit d’emprunter pour une structure modifié"),
    "patron.household_left": _("Départ d’un foyer"),
    "patron.guardian_added": _("Responsable ajouté"),
    "patron.guardian_updated": _("Droits d’un responsable modifiés"),
    "patron.guardian_removed": _("Responsable retiré"),
    "patron.hold": _("Mise en conservation"),
    "patron.hold_lifted": _("Conservation levée"),
    "patron.anonymize": _("Anonymisation"),
    "patron.export": _("Export des données"),
}
HISTORY_FIELDS: dict[str, str] = {
    "first_name": _("prénom"),
    "last_name": _("nom"),
    "birth": _("naissance"),
    "notes": _("observations"),
    "membership_expires_on": _("adhésion"),
    "courriel": _("courriel"),
    "telephone": _("téléphone"),
    "adresse": _("adresse"),
    "categorie": _("catégorie"),
    "adhesion": _("adhésion"),
}
CONTACT_LABELS: dict[str, str] = {
    "home": _("Domicile"),
    "mobile": _("Portable"),
    "work": _("Travail"),
    "other": _("Autre"),
}
MAJORITY_AGE = 18
MAX_NAME = 80
MAX_NOTE = 2000
MAX_SHORT = 200
ANONYMIZED_NAME = _("Lecteur anonymisé")


# --- Noms -------------------------------------------------------------------------------------------------------------
def clean_name(value: str | None, field: str) -> str:
    text = " ".join((value or "").split())
    if not text:
        raise PatronError(_("Indiquez le {field}.").format(field=field))
    if len(text) > MAX_NAME:
        raise PatronError(_("Ce {field} est trop long.").format(field=field))
    return text


def name_norm(first: str, last: str) -> str:
    """Clé de recherche et d'homonymie : sans accents ni casse, prénom puis nom."""
    return normalize(f"{first} {last}")


def sort_key(first: str, last: str) -> str:
    """Clé de tri de l'annuaire : nom puis prénom."""
    return normalize(f"{last} {first}")


def initials(first: str, last: str) -> str:
    letters = [part[:1] for part in (first, last) if part]
    return "".join(letters).upper() or "?"


# --- Naissance et majorité (D2, D6) -----------------------------------------------------------------------------------
_YEAR = re.compile(r"\d{4}")
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_FRENCH = re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})")
MIN_BIRTH_YEAR = 1900


def parse_birth(value: str | None, today: date) -> str | None:
    """« 2014 », « 2014-05-03 » ou « 03/05/2014 », rendue en ISO, aussi précise que la saisie. Vide : inconnue."""
    text = (value or "").strip()
    if not text:
        return None
    if _YEAR.fullmatch(text):
        year = int(text)
        if not MIN_BIRTH_YEAR <= year <= today.year:
            raise PatronError(_("Cette année de naissance n’est pas plausible."))
        return text
    match = _ISO.fullmatch(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
    elif match := _FRENCH.fullmatch(text):
        day, month, year = (int(part) for part in match.groups())
    else:
        raise PatronError(_("Écrivez l’année (2014) ou la date complète (03/05/2014)."))
    try:
        born = date(year, month, day)
    except ValueError:
        raise PatronError(_("Cette date n’existe pas.")) from None
    if born > today or year < MIN_BIRTH_YEAR:
        raise PatronError(_("Cette date de naissance n’est pas plausible."))
    return born.isoformat()


def birth_year(birth: str | None) -> int | None:
    return int(birth[:4]) if birth else None


def is_full_date(birth: str | None) -> bool:
    return bool(birth) and len(birth or "") == 10


def age_range(birth: str | None, today: date) -> tuple[int, int] | None:
    """Âge le plus bas et le plus haut possibles. Égaux quand la date est complète."""
    if not birth:
        return None
    if is_full_date(birth):
        born = date.fromisoformat(birth)
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        return age, age
    years = today.year - int(birth)
    return max(0, years - 1), years


def majority_to_confirm(category: str, birth: str | None, today: date) -> bool:
    """Un mineur qui a peut-être 18 ans : proposé à la confirmation, **jamais basculé seul**."""
    ages = age_range(birth, today)
    return category == "minor" and ages is not None and ages[1] >= MAJORITY_AGE


def category_issue(category: str, birth: str | None, today: date) -> str | None:
    """Une incohérence certaine entre la catégorie et la naissance, signalée sans rien corriger."""
    ages = age_range(birth, today)
    if ages is None:
        return None
    if category == "adult" and ages[1] < MAJORITY_AGE:
        return _("Inscrit comme adulte, mais a moins de 18 ans d’après sa naissance.")
    return None


# --- Identifiants : numéro de lecteur et cartes (D1, P1) --------------------------------------------------------------
PATRON_NUMBER = re.compile(r"P-?0*(\d{1,9})")
_CODE = re.compile(r"[A-Z0-9](?:[A-Z0-9-]{1,30})[A-Z0-9]")


def format_patron_number(value: int) -> str:
    return f"P-{value:06d}"


def parse_patron_number(value: str | None) -> str | None:
    """« p-123 », « P000123 » ou « P-000123 » désignent le même lecteur. `None` si ce n'est pas un numéro de lecteur."""
    text = re.sub(r"\s+", "", value or "").upper()
    match = PATRON_NUMBER.fullmatch(text)
    return format_patron_number(int(match.group(1))) if match else None


def normalize_card(value: str | None) -> str | None:
    """Numéro de carte, généré ou repris d'une carte existante : majuscules, sans espaces, 3 à 32 caractères."""
    text = re.sub(r"\s+", "", value or "").upper()
    return text if _CODE.fullmatch(text) else None


def check_new_card(value: str | None) -> str:
    """Un numéro de carte qu'on attribue : bien formé, et impossible à prendre pour un numéro de lecteur au scan."""
    card = normalize_card(value)
    if card is None:
        raise PatronError(_("Un numéro de carte compte de 3 à 32 lettres, chiffres ou tirets."))
    if parse_patron_number(card) is not None:
        raise PatronError(_("Ce numéro se lirait comme un numéro de lecteur (P-…) : choisissez-en un autre."))
    return card


# --- Courriels --------------------------------------------------------------------------------------------------------
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[^@\s.]{2,}")


def clean_email(value: str | None) -> str | None:
    """Forme seulement : rien ne vérifie que l'adresse existe."""
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > 254 or not _EMAIL.fullmatch(text):
        raise PatronError(_("Cette adresse électronique n’a pas une forme valide."))
    local, _sep, domain = text.rpartition("@")
    return f"{local}@{domain.lower()}"


# --- Adresses ---------------------------------------------------------------------------------------------------------
_POSTAL_FR = re.compile(r"\d{5}")


@dataclass(frozen=True)
class Address:
    line1: str
    line2: str | None
    postal_code: str | None
    city: str
    country: str = "FR"


def clean_address(line1: str, line2: str, postal_code: str, city: str, country: str = "FR") -> Address | None:
    """Une adresse est tout ou rien : la rue et la commune au moins. Vide : pas d'adresse."""
    parts = [" ".join((item or "").split()) for item in (line1, line2, postal_code, city)]
    if not any(parts):
        return None
    street, extra, postal, town = parts
    if not street or not town:
        raise PatronError(_("Une adresse demande au moins la rue et la commune."))
    code = (country or "FR").strip().upper()[:2] or "FR"
    if code == "FR" and postal and not _POSTAL_FR.fullmatch(postal):
        raise PatronError(_("Un code postal français compte cinq chiffres."))
    if any(len(item) > MAX_SHORT for item in parts):
        raise PatronError(_("Une ligne d’adresse est trop longue."))
    return Address(street, extra or None, postal or None, town, code)


# --- Liens et droits (D4, D5) -----------------------------------------------------------------------------------------
def check_member_role(kind: str, role: str) -> None:
    if kind not in MEMBER_ROLES or role not in MEMBER_ROLES[kind]:
        raise PatronError(_("Ce rôle n’existe pas pour ce type de foyer."))


def check_guardianship(minor_category: str, guardian_category: str, same_person: bool, guardian_status: str) -> None:
    """Refuse : soi-même, un responsable mineur, un responsable anonymisé, un « mineur » adulte."""
    if same_person:
        raise PatronError(_("Une personne ne peut pas être son propre responsable."))
    if minor_category != "minor":
        raise PatronError(_("Seul un mineur a des responsables."))
    if guardian_category != "adult":
        raise PatronError(_("Un responsable doit être un adulte."))
    if guardian_status == "anonymized":
        raise PatronError(_("Un lecteur anonymisé ne peut pas être responsable."))


@dataclass(frozen=True)
class GuardianFacts:
    guardian_id: int
    can_borrow_for: bool
    is_contact: bool
    reachable: bool  # au moins un téléphone ou un courriel, propre ou de l'un de ses foyers
    active: bool
    can_borrow_alone: bool = False  # droit distinct de `can_borrow_for`, jamais déduit de lui (D1 et P1 de L4)


def eligible_guardians(facts: list[GuardianFacts]) -> list[int]:
    """Préparation de L4 : un prêt à un mineur n'est possible qu'avec un responsable **habilité et joignable**.

    Habilité : le lien porte explicitement le droit d'emprunter pour lui. Joignable : ce même responsable a un moyen
    d'être contacté. Appartenir au même foyer ne compte pour rien.
    """
    return [item.guardian_id for item in facts if item.can_borrow_for and item.reachable and item.active]


def guardians_allowing_alone(facts: list[GuardianFacts]) -> list[int]:
    """Responsables dont l'accord permet à l'enfant d'emprunter seul, leur présence n'étant pas exigée (P1).

    Quatre conditions, sur ce lien et ce responsable : le droit est coché, il accepte d'être contacté, il a une
    coordonnée utilisable, et il est actif. Ni `can_borrow_for` ni le foyer ne remplacent l'une d'elles.
    """
    return [
        item.guardian_id
        for item in facts
        if item.can_borrow_alone and item.is_contact and item.reachable and item.active
    ]


def needs_guardian(facts: list[GuardianFacts]) -> bool:
    """« Responsable à renseigner » : ni adulte habilité et joignable, ni autorisation utilisable d'emprunter seul."""
    return not eligible_guardians(facts) and not guardians_allowing_alone(facts)


DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def page_size(text: str | None) -> int:
    """Le nombre de fiches par page, décidé au serveur : 25 par défaut, 100 au plus, quoi que demande l'adresse."""
    value = (text or "").strip()
    if not value.isdecimal() or len(value) > 9 or int(value) < 1:
        return DEFAULT_PAGE_SIZE
    return min(int(value), MAX_PAGE_SIZE)


def page_number(text: str | None) -> int:
    """Un numéro de page valide, ou 1. Une page au-delà de la dernière est ramenée à la dernière par la lecture."""
    value = (text or "").strip()
    return int(value) if value.isdecimal() and 0 < len(value) < 10 and int(value) > 0 else 1


def french_date(value: str | None) -> str:
    """« 2027-08-31 » devient « 31/08/2027 » ; une année seule, ou rien, reste telle quelle."""
    if value and len(value) >= 10:
        return f"{value[8:10]}/{value[5:7]}/{value[:4]}"
    return value or ""


# --- Désambiguïsation (D8) --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Marks:
    number: str
    card: str | None
    birth_year: int | None
    city: str | None
    household: str | None
    guardian: str | None


def marks_text(marks: Marks) -> str:
    """« P-000123 · carte L-000045 · naissance 2014 · Lyon · Famille Martin · resp. Paul Martin »."""
    parts = [marks.number]
    if marks.card:
        parts.append(_("carte {card}").format(card=marks.card))
    if marks.birth_year:
        parts.append(_("naissance {year}").format(year=marks.birth_year))
    parts += [item for item in (marks.city, marks.household) if item]
    if marks.guardian:
        parts.append(_("resp. {name}").format(name=marks.guardian))
    return " · ".join(parts)


def display_name(first: str, last: str, status: str) -> str:
    if status == "anonymized":
        return ANONYMIZED_NAME
    return f"{first} {last}".strip()
