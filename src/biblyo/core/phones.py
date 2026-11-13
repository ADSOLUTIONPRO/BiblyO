# SPDX-License-Identifier: EUPL-1.2
"""Numéros de téléphone : France métropolitaine, outre-mer, étranger (plan de L3, D7).

On contrôle la **forme** d'un numéro selon le plan de numérotation applicable, jamais son existence : aucune requête
réseau, aucun prestataire. Le stockage suit E.164 (« +33612345678 ») et garde donc l'indicatif. L'affichage suit
l'usage français. Un numéro qui peut se lire de plusieurs façons n'est jamais deviné : on rend les lectures possibles,
et une personne choisit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from biblyo.core.i18n import _


@dataclass(frozen=True)
class Territory:
    code: str
    label: str
    country_code: str
    #: Préfixes du numéro national sans le 0 (« 690 » pour 0690…), pour les territoires du plan à dix chiffres.
    prefixes: tuple[str, ...] = ()
    #: Longueur du numéro local, pour les collectivités qui ont leur propre plan.
    local_length: int = 0


METROPOLE = Territory("metropole", _("France métropolitaine"), "33")
DOM: tuple[Territory, ...] = (
    Territory("guadeloupe", _("Guadeloupe, Saint-Martin, Saint-Barthélemy"), "590", ("590", "690", "691")),
    Territory("guyane", _("Guyane"), "594", ("594", "694")),
    Territory("martinique", _("Martinique"), "596", ("596", "696", "697")),
    Territory("reunion", _("La Réunion"), "262", ("262", "263", "692", "693")),
    Territory("mayotte", _("Mayotte"), "262", ("269", "639")),
)
PACIFIC_AND_SPM: tuple[Territory, ...] = (
    Territory("saint_pierre", _("Saint-Pierre-et-Miquelon"), "508", local_length=6),
    Territory("wallis", _("Wallis-et-Futuna"), "681", local_length=6),
    Territory("caledonie", _("Nouvelle-Calédonie"), "687", local_length=6),
    Territory("polynesie", _("Polynésie française"), "689", local_length=8),
)
TERRITORIES: dict[str, Territory] = {item.code: item for item in (METROPOLE, *DOM, *PACIFIC_AND_SPM)}
_DOM_BY_PREFIX = {prefix: territory for territory in DOM for prefix in territory.prefixes}
_LOCAL_BY_CODE = {territory.country_code: territory for territory in PACIFIC_AND_SPM}
#: Indicatifs à un ou deux chiffres (UIT) ; tous les autres en ont trois. Sert à séparer l'indicatif à l'affichage.
_ONE_DIGIT = {"1", "7"}
_TWO_DIGITS = {
    "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47", "48", "49",
    "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65", "66", "81", "82", "84",
    "86", "90", "91", "92", "93", "94", "95", "98",
}  # fmt: skip
_SEPARATORS = re.compile(r"[\s.\-/()  ]")


class PhoneError(ValueError):
    """Numéro refusé : le message dit comment l'écrire."""


@dataclass(frozen=True)
class Phone:
    e164: str
    territory: str | None  # None : étranger

    @property
    def display(self) -> str:
        return format_phone(self.e164)

    @property
    def territory_label(self) -> str:
        if self.territory is None:
            return _("Étranger")
        return TERRITORIES[self.territory].label


@dataclass(frozen=True)
class Ambiguous:
    """Plusieurs lectures possibles, ou une lecture à confirmer : rien n'est enregistré sans un choix explicite."""

    candidates: tuple[Phone, ...]
    reason: str


def _split_country(digits: str) -> tuple[str, str]:
    if digits[:1] in _ONE_DIGIT:
        return digits[:1], digits[1:]
    if digits[:2] in _TWO_DIGITS:
        return digits[:2], digits[2:]
    return digits[:3], digits[3:]


def _pairs(digits: str) -> str:
    """Groupes de deux depuis la fin : « 2079460958 » devient « 20 79 46 09 58 »."""
    head = len(digits) % 2
    groups = [digits[:head]] if head else []
    groups += [digits[index : index + 2] for index in range(head, len(digits), 2)]
    return " ".join(groups)


def format_phone(e164: str) -> str:
    """Affichage à la française : « 06 12 34 56 78 », « 0690 12 34 56 », « +687 12 34 56 », « +44 20 79 46 09 58 »."""
    digits = e164.lstrip("+")
    if digits.startswith("33") and len(digits) == 11:
        national = "0" + digits[2:]
        return " ".join(national[index : index + 2] for index in range(0, 10, 2))
    for territory in DOM:
        rest = digits[len(territory.country_code) :]
        if digits.startswith(territory.country_code) and rest[:3] in territory.prefixes:
            national = "0" + digits[len(territory.country_code) :]
            return f"{national[:4]} {national[4:6]} {national[6:8]} {national[8:10]}"
    country, national = _split_country(digits)
    return f"+{country} {_pairs(national)}"


def territory_of(e164: str) -> str | None:
    digits = e164.lstrip("+")
    if digits.startswith("33") and len(digits) == 11:
        return METROPOLE.code
    for territory in DOM:
        rest = digits[len(territory.country_code) :]
        if digits.startswith(territory.country_code) and rest[:3] in territory.prefixes:
            return territory.code
    for code, territory in _LOCAL_BY_CODE.items():
        if digits.startswith(code):
            return territory.code
    return None


def _national(nine: str) -> Phone:
    """Neuf chiffres après le 0 du plan français : métropole ou département d'outre-mer, selon le préfixe."""
    territory = _DOM_BY_PREFIX.get(nine[:3])
    if territory is not None:
        return Phone(f"+{territory.country_code}{nine}", territory.code)
    return Phone(f"+33{nine}", METROPOLE.code)


def _plausible_national(nine: str) -> bool:
    return len(nine) == 9 and nine[0] in "123456789"


def _international(digits: str) -> Phone | Ambiguous:
    if digits.startswith("33"):
        nine = digits[2:]
        if not _plausible_national(nine):
            raise PhoneError(_("Un numéro français compte neuf chiffres après +33 : +33 6 12 34 56 78."))
        territory = _DOM_BY_PREFIX.get(nine[:3])
        if territory is not None:  # +33 6 90… : un numéro d'outre-mer composé avec l'indicatif de la métropole
            return Ambiguous(
                (Phone(f"+{territory.country_code}{nine}", territory.code),),
                _("Ce numéro relève de l’outre-mer ({label}) : confirmez son indicatif.").format(label=territory.label),
            )
        return Phone(f"+33{nine}", METROPOLE.code)
    for territory in DOM:
        if digits.startswith(territory.country_code) and territory.country_code != "262":
            nine = digits[len(territory.country_code) :]
            if len(nine) != 9 or nine[:3] not in territory.prefixes:
                message = _("Ce numéro ne suit pas le plan de numérotation de {label}.")
                raise PhoneError(message.format(label=territory.label))
            return Phone(f"+{digits}", territory.code)
    if digits.startswith("262"):
        nine = digits[3:]
        territory = _DOM_BY_PREFIX.get(nine[:3]) if len(nine) == 9 else None
        if territory is None or territory.country_code != "262":
            raise PhoneError(_("Ce numéro ne suit pas le plan de La Réunion ni celui de Mayotte."))
        return Phone(f"+{digits}", territory.code)
    for code, territory in _LOCAL_BY_CODE.items():
        if digits.startswith(code):
            if len(digits) - len(code) != territory.local_length:
                raise PhoneError(
                    _("Un numéro de {label} compte {n} chiffres après +{code}.").format(
                        label=territory.label, n=territory.local_length, code=code
                    )
                )
            return Phone(f"+{digits}", territory.code)
    if not 8 <= len(digits) <= 15 or digits[0] == "0":
        raise PhoneError(_("Un numéro international compte de 8 à 15 chiffres, indicatif compris."))
    return Phone(f"+{digits}", None)


def _for_territory(digits: str, territory: Territory) -> Phone:
    """Un numéro local, interprété dans le territoire choisi explicitement par la personne."""
    if territory.local_length:
        if len(digits) != territory.local_length:
            raise PhoneError(
                _("Un numéro de {label} compte {n} chiffres.").format(label=territory.label, n=territory.local_length)
            )
        return Phone(f"+{territory.country_code}{digits}", territory.code)
    nine = digits[1:] if len(digits) == 10 and digits.startswith("0") else digits
    if not _plausible_national(nine):
        raise PhoneError(_("Écrivez le numéro à dix chiffres : 06 12 34 56 78."))
    phone = _national(nine)
    if phone.territory != territory.code:
        raise PhoneError(
            _("Ce numéro appartient à {label}, pas au territoire choisi.").format(
                label=TERRITORIES[phone.territory or METROPOLE.code].label
            )
        )
    return phone


def parse_phone(text: str | None, territory: str | None = None) -> Phone | Ambiguous | None:
    """Lit une saisie. `None` si elle est vide. `territory` : le territoire choisi pour lever une ambiguïté."""
    raw = _SEPARATORS.sub("", text or "")
    if not raw:
        return None
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    international = raw.startswith("+")
    digits = raw[1:] if international else raw
    if not digits.isdigit():
        raise PhoneError(_("Un numéro ne contient que des chiffres, et éventuellement + au début."))
    if international:
        return _international(digits)
    if territory:
        if territory not in TERRITORIES:
            raise PhoneError(_("Territoire inconnu."))
        return _for_territory(digits, TERRITORIES[territory])
    if len(digits) == 10 and digits.startswith("0"):
        if not _plausible_national(digits[1:]):
            raise PhoneError(_("Écrivez le numéro à dix chiffres : 06 12 34 56 78."))
        return _national(digits[1:])
    if len(digits) == 9 and _plausible_national(digits):
        return Ambiguous(
            (_national(digits),),
            _("Il manque peut-être le 0 initial : confirmez le numéro, ou écrivez-le avec son indicatif."),
        )
    if len(digits) == 6 and digits[0] != "0":
        return Ambiguous(
            tuple(
                Phone(f"+{item.country_code}{digits}", item.code) for item in PACIFIC_AND_SPM if item.local_length == 6
            ),
            _("Six chiffres : Saint-Pierre-et-Miquelon, Wallis-et-Futuna ou Nouvelle-Calédonie ? Choisissez."),
        )
    if len(digits) == 8 and digits[0] in "48":  # Polynésie : 40…, 87…, 88…, 89…
        polynesia = TERRITORIES["polynesie"]
        return Ambiguous(
            (Phone(f"+{polynesia.country_code}{digits}", polynesia.code),),
            _("Huit chiffres : un numéro de Polynésie française ? Pour l’étranger, écrivez l’indicatif (+44 …)."),
        )
    raise PhoneError(
        _("Numéro non reconnu : écrivez-le à dix chiffres (06 12 34 56 78), ou avec son indicatif (+44 …).")
    )


TERRITORY_CHOICES: list[tuple[str, str]] = [(item.code, item.label) for item in TERRITORIES.values()]
