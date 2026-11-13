# SPDX-License-Identifier: EUPL-1.2
"""Règles pures des prêts et retours (plan de L4, § 3 et décisions D1 à D16). Ni base, ni horloge implicite.

Trois natures, jamais confondues. Une **interdiction** protège un fait ou un droit : personne n'y déroge, aucun réglage
ne l'assouplit. Un **contrôle dérogeable** refuse, sauf permission nommée et motif journalisé. Un **avertissement**
informe et laisse continuer. Chaque verdict dit pourquoi, et ce qu'il est possible de faire.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from biblyo.core.i18n import _
from biblyo.modules.catalogue.rules import COPY_STATUSES
from biblyo.modules.holdings.rules import CONDITIONS
from biblyo.modules.patrons import rules as patrons
from biblyo.modules.patrons.rules import GuardianFacts


class CirculationError(ValueError):
    """Demande impossible en elle-même : le message s'adresse à la personne qui l'a faite."""


class Nature(StrEnum):
    FORBIDDEN = "interdiction"
    OVERRIDABLE = "derogeable"
    WARNING = "avertissement"
    INFO = "information"


# --- Permissions ------------------------------------------------------------------------------------------------------
LEND = "circulation.lend"
RENEW = "circulation.renew"
OVERRIDE = "circulation.override"
OVERRIDE_LIMIT = "circulation.override_limit"
HISTORY = "circulation.history"
SETTINGS = "circulation.settings"


# --- Le tableau du § 3 ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    nature: Nature
    message: str
    permission: str | None = None


RULES: dict[str, Rule] = {
    "R1": Rule(Nature.FORBIDDEN, _("Cet exemplaire est déjà en prêt, retour prévu le {due}.")),
    "R2": Rule(Nature.FORBIDDEN, _("Cet exemplaire se consulte sur place : il ne peut pas être prêté.")),
    "R3": Rule(Nature.FORBIDDEN, _("Cet exemplaire est « {status} » : il ne peut pas être prêté.")),
    "R4": Rule(Nature.FORBIDDEN, _("Code inconnu. Cherchez plutôt le lecteur par son nom, ou l’ouvrage au catalogue.")),
    "R5": Rule(Nature.FORBIDDEN, _("Cette fiche est archivée ou anonymisée : aucun prêt n’est possible.")),
    "R6": Rule(Nature.FORBIDDEN, _("Ce lecteur est suspendu : aucun prêt n’est possible.")),
    "R7": Rule(
        Nature.FORBIDDEN,
        _(
            "Responsable à renseigner : ni adulte habilité et joignable, ni autorisation utilisable d’emprunter seul."
            " Aucun prêt n’est possible pour cet enfant."
        ),
    ),
    "R8": Rule(
        Nature.FORBIDDEN,
        _("Cette personne ne peut pas répondre de ce prêt : elle doit être habilitée, joignable et active."),
    ),
    "R9": Rule(Nature.WARNING, _("Majorité à confirmer : ce lecteur reste mineur tant qu’elle n’est pas confirmée.")),
    "R10": Rule(Nature.FORBIDDEN, _("Plusieurs résultats : choisissez explicitement avant de continuer.")),
    "R11": Rule(
        Nature.FORBIDDEN,
        _("Cette carte n’est plus en service. Vérifiez l’identité, puis retrouvez la fiche par le nom."),
    ),
    "R12": Rule(
        Nature.FORBIDDEN,
        _("La situation vient de changer : rien n’a été enregistré. Vérifiez la liste, puis validez à nouveau."),
    ),
    "R13": Rule(Nature.FORBIDDEN, _("Cette personne n’est pas autorisée à emprunter pour cette structure.")),
    "R14": Rule(Nature.WARNING, _("Plafond dépassé : {count} prêts pour un plafond de {maximum}.")),
    "R15": Rule(Nature.OVERRIDABLE, _("Nombre maximal de renouvellements atteint ({maximum})."), OVERRIDE),
    "R16": Rule(Nature.OVERRIDABLE, _("Date de prêt antérieure à aujourd’hui."), OVERRIDE),
    "R17": Rule(Nature.OVERRIDABLE, _("Date de retour au-delà de la durée prévue ({due})."), OVERRIDE),
    "R18": Rule(Nature.WARNING, _("Prêts en retard : {own} pour ce lecteur, {responsible} pour son responsable.")),
    "R19": Rule(Nature.WARNING, _("L’adhésion de ce lecteur est échue.")),
    "R20": Rule(Nature.WARNING, _("Plafond bientôt atteint : {count} prêts sur {maximum}.")),
    "R21": Rule(Nature.INFO, _("Emprunte seul, avec l’autorisation d’un responsable.")),
    "R22": Rule(Nature.WARNING, _("État signalé sur cet exemplaire : {condition}. Vérifiez-le avant la sortie.")),
    "R23": Rule(Nature.WARNING, _("Ce prêt est en retard.")),
    "R24": Rule(Nature.WARNING, _("L’échéance tombe un jour de fermeture.")),
    "R25": Rule(Nature.INFO, _("Cet exemplaire n’est pas en prêt : il n’y a rien à rendre.")),
    "R26": Rule(
        Nature.FORBIDDEN,
        _("Une opération ultérieure dépend de celle-ci : elle ne s’annule plus. Demandez une correction tracée."),
    ),
    "R27": Rule(Nature.FORBIDDEN, _("L’emprunteur d’un exemplaire n’est visible qu’avec le droit sur l’historique.")),
    "R28": Rule(Nature.FORBIDDEN, _("Les prêts en cours se consultent au comptoir, avec le droit de prêter.")),
    # D9 : hors de la fenêtre, ou opération d'un autre compte. Pas une ligne du § 3, mais un contrôle dérogeable
    "D9": Rule(
        Nature.OVERRIDABLE, _("Annulation hors délai ou d’un autre compte : un bibliothécaire habilité."), OVERRIDE
    ),
}


@dataclass(frozen=True)
class Verdict:
    code: str
    nature: Nature
    message: str
    permission: str | None = None


def verdict(code: str, *, nature: Nature | None = None, permission: str | None = None, **values: Any) -> Verdict:
    """Le verdict d'une règle. `nature` et `permission` ne servent qu'aux règles dont un réglage change la nature."""
    rule = RULES[code]
    return Verdict(code, nature or rule.nature, rule.message.format(**values), permission or rule.permission)


def forbidden(verdicts: Sequence[Verdict]) -> list[Verdict]:
    return [item for item in verdicts if item.nature is Nature.FORBIDDEN]


def overridable(verdicts: Sequence[Verdict]) -> list[Verdict]:
    return [item for item in verdicts if item.nature is Nature.OVERRIDABLE]


def warnings(verdicts: Sequence[Verdict]) -> list[Verdict]:
    return [item for item in verdicts if item.nature is Nature.WARNING]


def refusals(verdicts: Sequence[Verdict], permissions: Collection[str], *, has_reason: bool) -> list[Verdict]:
    """Ce qui empêche réellement l'opération : toute interdiction, et tout contrôle sans permission ou sans motif."""
    stopped = forbidden(verdicts)
    stopped += [item for item in overridable(verdicts) if item.permission not in permissions or not has_reason]
    return stopped


# --- Échéances (D4, D7, D8, D12) --------------------------------------------------------------------------------------
DUE_SOON_DAYS = 3


def due_date(start: date, days: int) -> date:
    return start + timedelta(days=days)


def renewed_due(due_on: date, today: date, days: int) -> date:
    """Une prolongation part de l'échéance actuelle, ou d'aujourd'hui si le prêt est déjà en retard (D7)."""
    return max(due_on, today) + timedelta(days=days)


def is_overdue(due_on: date, today: date) -> bool:
    """En retard le lendemain de l'échéance : le jour même, le livre est encore à l'heure (D4)."""
    return today > due_on


def days_overdue(due_on: date, today: date) -> int:
    return max((today - due_on).days, 0)


def is_due_soon(due_on: date, today: date, within: int = DUE_SOON_DAYS) -> bool:
    return today <= due_on <= today + timedelta(days=within)


def check_dates(loaned_on: date, due_on: date) -> None:
    if due_on <= loaned_on:
        raise CirculationError(_("La date de retour doit être postérieure à la date de prêt."))


def date_verdicts(
    loaned_on: date, due_on: date, rule_due: date, today: date, closed_weekday: int | None
) -> list[Verdict]:
    """R16, R17, R24. Une échéance plus courte que la règle est toujours permise (D8)."""
    found: list[Verdict] = []
    if loaned_on < today:
        found.append(verdict("R16"))
    if due_on > rule_due:
        found.append(verdict("R17", due=patrons.french_date(rule_due.isoformat())))
    if closed_weekday is not None and due_on.weekday() == closed_weekday:
        found.append(verdict("R24"))
    return found


# --- Durées et plafonds (D2 bis, D3) ----------------------------------------------------------------------------------
LIMIT_WARN = "avertir"
LIMIT_BLOCK = "bloquer"
LIMIT_MODES: dict[str, str] = {LIMIT_WARN: _("Avertir"), LIMIT_BLOCK: _("Bloquer, avec dérogation")}


RULE_CATEGORIES: dict[str, str] = {"adult": _("Adulte"), "minor": _("Mineur"), "structure": _("Structure")}
WEEKDAYS: tuple[tuple[str, str], ...] = (
    ("0", _("Lundi")),
    ("1", _("Mardi")),
    ("2", _("Mercredi")),
    ("3", _("Jeudi")),
    ("4", _("Vendredi")),
    ("5", _("Samedi")),
    ("6", _("Dimanche")),
)
MAX_DAYS = 365
MAX_RENEWALS = 20
MAX_LIMIT = 999
MAX_OVERDUE_BLOCK = 99
MAX_RETENTION_MONTHS = 120


def parse_bound(text: str | None, low: int, high: int, *, optional: bool = False) -> int | None:
    """Un entier borné, saisi dans un formulaire. Vide : `None` si la valeur est facultative."""
    value = (text or "").strip()
    if not value:
        if optional:
            return None
        raise CirculationError(_("Indiquez une valeur."))
    if not value.isdecimal() or len(value) > 4 or not low <= int(value) <= high:
        raise CirculationError(_("Indiquez un nombre entre {low} et {high}.").format(low=low, high=high))
    return int(value)


def parse_rule(
    loan_days: str, renewal_days: str, max_renewals: str, max_active: str
) -> tuple[int, int, int, int | None]:
    """Durée, prolongation, renouvellements et plafond (vide : aucun plafond)."""
    return (
        parse_bound(loan_days, 1, MAX_DAYS) or 1,
        parse_bound(renewal_days, 1, MAX_DAYS) or 1,
        parse_bound(max_renewals, 0, MAX_RENEWALS) or 0,
        parse_bound(max_active, 1, MAX_LIMIT, optional=True),
    )


@dataclass(frozen=True)
class LoanRule:
    document_type_id: int | None
    patron_category: str | None
    loan_days: int
    renewal_days: int
    max_renewals: int
    max_active_loans: int | None


def rule_category(category: str, mode: str) -> str:
    """Une structure a sa durée et son plafond propres, quelle que soit la personne qui emprunte pour elle (D2 bis)."""
    return "structure" if mode == MODE_STRUCTURE else category


def pick_rule(rules: Sequence[LoanRule], document_type_id: int | None, category: str) -> LoanRule:
    """La règle la plus précise : type et catégorie, puis type seul, puis catégorie seule, puis la règle générale."""
    scopes = ((document_type_id, category), (document_type_id, None), (None, category), (None, None))
    for wanted in scopes:
        for item in rules:
            if (item.document_type_id, item.patron_category) == wanted:
                return item
    raise CirculationError(_("Aucune règle de prêt ne couvre ce cas : complétez les réglages."))


def limit_verdicts(active: int, adding: int, maximum: int | None, mode: str) -> list[Verdict]:
    """R14 et R20. Le plafond n'est bloquant que sur réglage, et reste alors dérogeable : jamais une interdiction."""
    if maximum is None:
        return []
    count = active + adding
    if count > maximum:
        if mode == LIMIT_BLOCK:
            return [verdict("R14", nature=Nature.OVERRIDABLE, permission=OVERRIDE_LIMIT, count=count, maximum=maximum)]
        return [verdict("R14", count=count, maximum=maximum)]
    if maximum - count <= 1 and count >= 1:
        return [verdict("R20", count=count, maximum=maximum)]
    return []


# --- L'exemplaire (R1 à R3, R22) --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CopyFacts:
    status: str
    is_loanable: bool
    on_loan: bool = False
    due_on: str | None = None
    condition: str | None = None
    condition_note: str | None = None


def copy_verdicts(facts: CopyFacts) -> list[Verdict]:
    """Sans jamais nommer le détenteur : indisponibilité et échéance seulement (D11 bis)."""
    if facts.on_loan:
        return [verdict("R1", due=patrons.french_date(facts.due_on))]
    if facts.status != "available":
        return [verdict("R3", status=COPY_STATUSES.get(facts.status, facts.status).lower())]
    if not facts.is_loanable:
        return [verdict("R2")]
    if facts.condition == "damaged" or facts.condition_note:
        label = CONDITIONS.get(facts.condition or "", "").lower() or facts.condition_note or ""
        return [verdict("R22", condition=label)]
    return []


# --- L'emprunteur (R5, R6, R9, R18, R19) ------------------------------------------------------------------------------
@dataclass(frozen=True)
class BorrowerFacts:
    category: str
    status: str
    birth: str | None = None
    membership_expires_on: str | None = None
    overdue: int = 0
    responsible_overdue: int = 0
    guardians: tuple[GuardianFacts, ...] = ()


def borrower_verdicts(facts: BorrowerFacts, today: date, overdue_block: int | None = None) -> list[Verdict]:
    """Ce que dit la fiche, avant de savoir dans quel cadre on emprunte.

    Le seuil de retards est désactivé par défaut (`None`). Atteint, il rend R18 dérogeable : jamais une interdiction.
    """
    if facts.status in ("archived", "anonymized"):
        return [verdict("R5")]
    if facts.status == "suspended":
        return [verdict("R6")]
    found: list[Verdict] = []
    if patrons.majority_to_confirm(facts.category, facts.birth, today):
        found.append(verdict("R9"))
    if facts.membership_expires_on and facts.membership_expires_on < today.isoformat():
        found.append(verdict("R19"))
    total = facts.overdue + facts.responsible_overdue
    if total:
        blocked = overdue_block is not None and total >= overdue_block
        found.append(
            verdict(
                "R18",
                nature=Nature.OVERRIDABLE if blocked else None,
                permission=OVERRIDE if blocked else None,
                own=facts.overdue,
                responsible=facts.responsible_overdue,
            )
        )
    return found


# --- Le cadre de l'emprunt (D1, D2, P1) -------------------------------------------------------------------------------
MODE_SELF = "self"
MODE_WITH_ADULT = "with_adult"
MODE_ALONE = "alone"
MODE_STRUCTURE = "structure"
BORROW_MODES: dict[str, str] = {
    MODE_SELF: _("Pour soi"),
    MODE_WITH_ADULT: _("Avec un adulte"),
    MODE_ALONE: _("Seul"),
    MODE_STRUCTURE: _("Pour une structure"),
}


@dataclass(frozen=True)
class StructureFacts:
    household_id: int
    is_structure: bool
    archived: bool
    can_borrow_for_structure: bool


@dataclass(frozen=True)
class Borrowing:
    mode: str
    responsible_id: int | None = None
    household_id: int | None = None


@dataclass(frozen=True)
class MinorOptions:
    """Ce que le comptoir propose pour un mineur. Un seul adulte est présélectionné **et affiché**, à confirmer."""

    adults: tuple[int, ...]
    alone_by: int | None

    @property
    def preselected(self) -> int | None:
        return self.adults[0] if len(self.adults) == 1 else None


def minor_options(guardians: Sequence[GuardianFacts]) -> MinorOptions:
    facts = list(guardians)
    allowing = patrons.guardians_allowing_alone(facts)
    return MinorOptions(tuple(patrons.eligible_guardians(facts)), allowing[0] if allowing else None)


def decide_borrowing(
    facts: BorrowerFacts,
    mode: str,
    *,
    chosen_guardian: int | None = None,
    structure: StructureFacts | None = None,
) -> tuple[Borrowing | None, list[Verdict]]:
    """Qui répond de ce prêt. Aucun droit ne se déduit d'un autre, ni de l'appartenance à un foyer."""
    if facts.category == "minor":
        return _decide_minor(facts, mode, chosen_guardian)
    if mode == MODE_SELF:
        return Borrowing(MODE_SELF), []
    if mode == MODE_STRUCTURE:
        if structure and structure.is_structure and not structure.archived and structure.can_borrow_for_structure:
            return Borrowing(MODE_STRUCTURE, household_id=structure.household_id), []
        return None, [verdict("R13")]
    return None, [verdict("R8")]


def _decide_minor(facts: BorrowerFacts, mode: str, chosen: int | None) -> tuple[Borrowing | None, list[Verdict]]:
    options = minor_options(facts.guardians)
    if mode == MODE_ALONE:
        if options.alone_by is None:
            return None, [verdict("R7")]
        return Borrowing(MODE_ALONE, responsible_id=options.alone_by), [verdict("R21")]
    if mode == MODE_STRUCTURE:
        return None, [verdict("R13")]  # une structure emprunte par un adulte (D2)
    if mode != MODE_WITH_ADULT or not options.adults:
        return None, [verdict("R7")]
    if chosen is None or chosen not in options.adults:
        return None, [verdict("R8")]
    return Borrowing(MODE_WITH_ADULT, responsible_id=chosen), []


LOAN_STATES: dict[str, str] = {
    "active": _("En cours"),
    "returned": _("Rendu"),
    "cancelled": _("Annulé"),
}
EVENT_KINDS: dict[str, str] = {
    "created": _("Prêt enregistré"),
    "renewed": _("Renouvellement"),
    "due_changed": _("Échéance modifiée"),
    "returned": _("Retour"),
    "cancelled": _("Annulation"),
    "corrected": _("Correction"),
    "override": _("Dérogation"),
}


# --- Résolution d'un code (D5) ----------------------------------------------------------------------------------------
KIND_CARD = "card"
KIND_ENDED_CARD = "ended_card"
KIND_PATRON = "patron"
KIND_COPY = "copy"
KIND_ISBN = "isbn"
KIND_NAME = "name"
CANDIDATE_KINDS: dict[str, str] = {
    KIND_CARD: _("Carte"),
    KIND_ENDED_CARD: _("Carte hors service"),
    KIND_PATRON: _("Numéro de lecteur"),
    KIND_COPY: _("Exemplaire"),
    KIND_ISBN: _("ISBN"),
    KIND_NAME: _("Nom"),
}


@dataclass(frozen=True)
class Candidate:
    kind: str
    target_id: int
    label: str = ""


@dataclass(frozen=True)
class Resolution:
    picked: Candidate | None
    choices: tuple[Candidate, ...] = ()
    verdict: Verdict | None = None


def resolve(found: Sequence[Candidate]) -> Resolution:
    """Un seul résultat dans un seul type : il est pris. Toute autre situation demande une sélection explicite.

    Deux types qui mènent à la même fiche restent une ambiguïté : rien n'est résolu en silence entre types (D5).
    """
    unique = tuple(dict.fromkeys((item.kind, item.target_id) for item in found))
    candidates = tuple(next(item for item in found if (item.kind, item.target_id) == key) for key in unique)
    if not candidates:
        return Resolution(None, (), verdict("R4"))
    if len(candidates) > 1:
        return Resolution(None, candidates, verdict("R10"))
    only = candidates[0]
    return Resolution(None, (), verdict("R11")) if only.kind == KIND_ENDED_CARD else Resolution(only)


# --- Renouvellement et retour (D7, R25) -------------------------------------------------------------------------------
def renewal_verdicts(renewals: int, max_renewals: int, due_on: date, today: date) -> list[Verdict]:
    found: list[Verdict] = []
    if renewals >= max_renewals:
        found.append(verdict("R15", maximum=max_renewals))
    if is_overdue(due_on, today):
        found.append(verdict("R23"))
    return found


def nothing_to_return() -> Verdict:
    return verdict("R25")


# --- Annulation (D9) --------------------------------------------------------------------------------------------------
CANCEL_WINDOW = timedelta(minutes=15)


@dataclass(frozen=True)
class CancelFacts:
    elapsed: timedelta
    own: bool
    relent: bool = False
    returned: bool = False
    renewed: bool = False
    reminded: bool = False

    @property
    def has_dependent(self) -> bool:
        """Une opération ultérieure s'appuie sur celle-ci : l'annuler réécrirait l'histoire."""
        return self.relent or self.returned or self.renewed or self.reminded


@dataclass(frozen=True)
class CancelDecision:
    allowed: bool
    needs_reason: bool = False
    verdict: Verdict | None = None


def cancel_decision(facts: CancelFacts, permissions: Collection[str]) -> CancelDecision:
    if facts.has_dependent:
        return CancelDecision(False, verdict=verdict("R26"))
    if facts.own and facts.elapsed < CANCEL_WINDOW and LEND in permissions:
        return CancelDecision(True)
    if OVERRIDE in permissions:
        return CancelDecision(True, needs_reason=True)
    return CancelDecision(False, verdict=verdict("D9"))


# --- Motifs (D3, D8, D9) : un code, et un texte court seulement pour « Autre » ----------------------------------------
REASON_OTHER = "other"
OVERRIDE_REASONS: dict[str, str] = {
    "late_entry": _("Saisie différée"),
    "exceptional_duration": _("Durée exceptionnelle"),
    "librarian_decision": _("Décision du bibliothécaire"),
    REASON_OTHER: _("Autre motif"),
}
CANCEL_REASONS: dict[str, str] = {
    "scan_error": _("Erreur de scan"),
    "wrong_reader": _("Mauvais lecteur"),
    "reader_gave_up": _("Le lecteur a renoncé"),
    REASON_OTHER: _("Autre motif"),
}
CORRECTION_REASONS: dict[str, str] = {
    "wrong_due": _("Échéance erronée"),
    "wrong_return_date": _("Date de retour erronée"),
    "wrong_reader": _("Mauvais lecteur"),
    "wrong_responsible": _("Mauvais responsable"),
    REASON_OTHER: _("Autre motif"),
}


def reason_label(code: str | None) -> str | None:
    """Le libellé d'un motif enregistré, tel que la personne l'a choisi."""
    if code is None:
        return None
    return {**OVERRIDE_REASONS, **CANCEL_REASONS, **CORRECTION_REASONS}.get(code, code)


MAX_REASON = 120
_MAX_DIGITS = 6


def clean_reason(
    code: str | None, text: str | None, allowed: dict[str, str], names: Sequence[str] = ()
) -> tuple[str, str | None]:
    """Un motif reste au journal : un code de la liste, et pour « Autre » un texte sans nom, courriel ni numéro."""
    if code not in allowed:
        raise CirculationError(_("Choisissez un motif."))
    typed = " ".join((text or "").split())
    if code != REASON_OTHER:
        return code, None
    if not typed:
        raise CirculationError(_("Précisez le motif, sans donnée personnelle."))
    if len(typed) > MAX_REASON:
        raise CirculationError(_("Ce motif est trop long."))
    words = set(patrons.name_norm(typed, "").split())
    named = {part for name in names for part in patrons.name_norm(name, "").split() if len(part) > 2}
    if words & named or "@" in typed or sum(char.isdigit() for char in typed) >= _MAX_DIGITS:
        raise CirculationError(_("Le motif reste au journal : n’y écrivez ni nom, ni coordonnée, ni numéro."))
    return code, typed


# --- Rappels manuels (D15) : valeurs structurées seulement ------------------------------------------------------------
REMINDER_CHANNELS: dict[str, str] = {
    "phone": _("Téléphone"),
    "in_person": _("En personne"),
    "email_outside": _("Courriel envoyé hors de biblyO"),
    "letter": _("Courrier"),
}
REMINDER_RESULTS: dict[str, str] = {
    "reached": _("Joint"),
    "message_left": _("Message laissé"),
    "no_answer": _("Pas de réponse"),
    "wrong_contact": _("Coordonnée erronée"),
    "return_promised": _("Retour promis"),
}


def check_reminder(channel: str | None, result: str | None) -> None:
    if channel not in REMINDER_CHANNELS:
        raise CirculationError(_("Choisissez le canal du rappel."))
    if result not in REMINDER_RESULTS:
        raise CirculationError(_("Choisissez le résultat du rappel."))


# --- Ce qui se voit (D11 bis, P2) -------------------------------------------------------------------------------------
def can_see_borrower(permissions: Collection[str]) -> bool:
    """R27 : l'emprunteur d'un exemplaire n'est visible qu'avec `circulation.history`, sur toutes les voies."""
    return HISTORY in permissions


def can_view_counter_loans(permissions: Collection[str]) -> bool:
    """R28 : les prêts en cours du lecteur sélectionné, au comptoir, limités à quatre champs (P2)."""
    return LEND in permissions or RENEW in permissions
