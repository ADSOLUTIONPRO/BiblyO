# SPDX-License-Identifier: EUPL-1.2
"""Règles pures des exemplaires et des emplacements : aucune base, aucun réseau (13-PLAN-L2.md, décisions D1 à D8).

Hiérarchie physique (D8) : site facultatif → salle → meuble → niveau (étagère ou bac). La position d'un
exemplaire appartient au niveau : c'est son rang, de 1 à n, sans trou. Des lieux virtuels, sans position,
complètent l'arbre.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from biblyo.core.i18n import _
from biblyo.core.text import normalize

# --- Emplacements -----------------------------------------------------------------------------------------------------
KINDS: dict[str, str] = {
    "site": _("Site"),
    "room": _("Salle"),
    "furniture": _("Meuble"),
    "shelf": _("Étagère"),
    "bin": _("Bac"),
    "virtual": _("Lieu virtuel"),
}
LEVEL_KINDS = frozenset({"shelf", "bin"})
#: Parents permis pour chaque type. `None` : à la racine.
PARENT_KINDS: dict[str, tuple[str | None, ...]] = {
    "site": (None,),
    "room": (None, "site"),
    "furniture": ("room",),
    "shelf": ("furniture",),
    "bin": ("furniture",),
    "virtual": (None, "site", "room"),
}
MAX_DEPTH = 3  # site, salle, meuble, niveau
MAX_NAME = 80
MAX_CODE = 16
MAX_CAPACITY = 2000
GRID_MAX = 40


class LocationError(ValueError):
    """Organisation d'emplacements refusée : le message s'adresse à la personne qui l'a demandée."""


def check_parent(kind: str, parent_kind: str | None) -> None:
    """Refuse une relation incohérente : un meuble hors d'une salle, une étagère hors d'un meuble…"""
    if kind not in KINDS:
        raise LocationError(_("Ce type d’emplacement n’existe pas."))
    if parent_kind is not None and parent_kind not in KINDS:
        raise LocationError(_("Le lieu parent n’existe pas."))
    if parent_kind not in PARENT_KINDS[kind]:
        allowed = [KINDS[item] if item else _("la racine") for item in PARENT_KINDS[kind]]
        raise LocationError(
            _("Un emplacement de type « {kind} » se place sous : {allowed}.").format(
                kind=KINDS[kind], allowed=", ".join(allowed)
            )
        )


def depth_for(parent_depth: int | None) -> int:
    depth = 0 if parent_depth is None else parent_depth + 1
    if depth > MAX_DEPTH:
        raise LocationError(_("La hiérarchie est trop profonde."))
    return depth


def check_no_cycle(location_id: int, new_parent_chain: Sequence[int]) -> None:
    """`new_parent_chain` : le nouveau parent et tous ses ancêtres. Un lieu ne peut pas descendre de lui-même."""
    if location_id in new_parent_chain:
        raise LocationError(
            _("Un emplacement ne peut pas être placé à l’intérieur de lui-même ou de ses descendants.")
        )


def path_of(names: Sequence[str]) -> str:
    return " / ".join(names)


def is_level(kind: str) -> bool:
    return kind in LEVEL_KINDS


def accepts_copies(kind: str) -> bool:
    """Un exemplaire se range dans un niveau (avec position) ou dans un lieu virtuel (sans position)."""
    return kind in LEVEL_KINDS or kind == "virtual"


def clean_name(value: str | None) -> str:
    text = " ".join((value or "").split())
    if not text:
        raise LocationError(_("Indiquez un nom."))
    if len(text) > MAX_NAME:
        raise LocationError(_("Ce nom est trop long."))
    return text


def clean_location_code(value: str | None) -> str | None:
    """Code court imprimable (« BD-A ») : facultatif, en majuscules, lettres, chiffres et tirets."""
    text = (value or "").strip().upper().replace(" ", "-")
    if not text:
        return None
    if len(text) > MAX_CODE or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]*", text):
        raise LocationError(_("Le code court ne contient que des lettres, des chiffres et des tirets (16 au plus)."))
    return text


def clean_capacity(value: str | int | None) -> int | None:
    """Capacité en places physiques (D1). Facultative ; jamais une raison de refuser un rangement."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if not text.isdigit() or int(text) > MAX_CAPACITY:
        raise LocationError(_("Indiquez un nombre de places entre 0 et 2000."))
    return int(text)


@dataclass(frozen=True)
class GridBox:
    row: int
    col: int
    width: int = 1
    height: int = 1

    def cells(self) -> set[tuple[int, int]]:
        return {(r, c) for r in range(self.row, self.row + self.height) for c in range(self.col, self.col + self.width)}


def check_grid_box(box: GridBox) -> None:
    values = (box.row, box.col, box.width, box.height)
    if any(value < 1 for value in values) or box.row + box.height - 1 > GRID_MAX or box.col + box.width - 1 > GRID_MAX:
        raise LocationError(_("Cette place sort de la grille de la salle."))


def grid_overlaps(box: GridBox, others: Iterable[GridBox]) -> bool:
    cells = box.cells()
    return any(cells & other.cells() for other in others)


# --- Positions --------------------------------------------------------------------------------------------------------
def _bounded(position: int | None, length: int) -> int:
    """Rang demandé, borné à [1, n + 1]. Sans rang : à la fin."""
    if position is None:
        return length + 1
    return max(1, min(position, length + 1))


def remove[T: Hashable](order: Sequence[T], item: T) -> list[T]:
    return [value for value in order if value != item]


def insert[T: Hashable](order: Sequence[T], items: Sequence[T], position: int | None = None) -> list[T]:
    """Insère des exemplaires, dans leur ordre, à partir du rang demandé ; les suivants se décalent."""
    if len(set(items)) != len(items):
        raise ValueError("Exemplaire en double")
    rest = [value for value in order if value not in set(items)]
    index = _bounded(position, len(rest)) - 1
    return [*rest[:index], *items, *rest[index:]]


def move_to[T: Hashable](order: Sequence[T], item: T, position: int) -> list[T]:
    """Change le rang d'un exemplaire sur sa propre étagère."""
    if item not in order:
        raise ValueError("Exemplaire absent de l'étagère")
    return insert(order, [item], position)


def rank_after_removal(rank: int, removed_ranks: Iterable[int]) -> int:
    """« Placer avant le dos au rang N » : les dos déplacés qui le précèdent sur ce niveau libèrent leur place."""
    return rank - sum(1 for removed in removed_ranks if removed < rank)


def neighbours[T: Hashable](order: Sequence[T], item: T) -> tuple[T | None, T | None]:
    index = list(order).index(item)
    before = order[index - 1] if index > 0 else None
    after = order[index + 1] if index + 1 < len(order) else None
    return before, after


SORTS: dict[str, str] = {"serie": _("Série puis tome"), "titre": _("Titre"), "cote": _("Cote")}


@dataclass(frozen=True)
class SortFacts:
    """Ce qu'il faut savoir d'un exemplaire pour trier une étagère."""

    copy_id: int
    title_sort: str
    series_sort: str | None = None
    volume: float | None = None
    volume_label: str | None = None
    call_number: str | None = None


def sort_key(facts: SortFacts, mode: str) -> tuple[Any, ...]:
    if mode == "serie":
        return (
            facts.series_sort is None,
            facts.series_sort or "",
            facts.volume is None,
            facts.volume or 0.0,
            normalize(facts.volume_label),
            facts.title_sort,
            facts.copy_id,
        )
    if mode == "cote":
        return (facts.call_number is None, normalize(facts.call_number), facts.title_sort, facts.copy_id)
    if mode == "titre":
        return (facts.title_sort, facts.copy_id)
    raise ValueError(f"Tri inconnu : {mode}")


def sorted_order(facts: Iterable[SortFacts], mode: str) -> list[int]:
    return [item.copy_id for item in sorted(facts, key=lambda item: sort_key(item, mode))]


def moved_count[T: Hashable](before: Sequence[T], after: Sequence[T]) -> int:
    """Nombre d'exemplaires qui changent de rang : ce que l'aperçu d'un tri annonce."""
    return sum(1 for old, new in zip(before, after, strict=True) if old != new)


@dataclass(frozen=True)
class Fill:
    count: int
    capacity: int | None

    @property
    def state(self) -> str:
        """none (capacité non renseignée), ok, full (pleine), over (dépassée : signalée, jamais refusée)."""
        if self.capacity is None:
            return "none"
        if self.count > self.capacity:
            return "over"
        return "full" if self.count == self.capacity else "ok"

    @property
    def excess(self) -> int:
        return max(0, self.count - (self.capacity or 0)) if self.capacity is not None else 0

    @property
    def free(self) -> int:
        return max(0, (self.capacity or 0) - self.count) if self.capacity is not None else 0

    @property
    def percent(self) -> int:
        if not self.capacity:
            return 100 if self.count and self.capacity == 0 else 0
        return min(100, round(100 * self.count / self.capacity))


# --- Cote (D2) --------------------------------------------------------------------------------------------------------
TYPE_CODES: dict[str, str] = {
    "bd": "BD",
    "manga": "MAN",
    "livre": "R",
    "comics": "COM",
    "revue": "REV",
    "documentaire": "DOC",
    "album": "A",
}
MAX_CALL_NUMBER = 40


def letters_code(text: str | None, length: int = 3) -> str:
    return re.sub(r"[^a-z]", "", normalize(text))[:length].upper()


@dataclass(frozen=True)
class CallFacts:
    type_code: str
    title: str
    series_title: str | None = None
    series_prefix: str | None = None
    call_sequence: int | None = None
    author_sort_name: str | None = None


def propose_call_number(facts: CallFacts) -> str:
    """Cote proposée : type, code de série (ou d'auteur, sinon du titre), numéro interne de série.

    **Jamais le tome commercial, jamais l'emplacement.** La proposition n'est faite qu'une fois : une cote enregistrée
    n'est jamais recalculée automatiquement.
    """
    kind = TYPE_CODES.get(facts.type_code, "X")
    if facts.series_title:
        key = (facts.series_prefix or "").strip().upper() or letters_code(facts.series_title)
        number = str(facts.call_sequence) if facts.call_sequence else ""
    else:
        key = letters_code(facts.author_sort_name) or letters_code(facts.title)
        number = ""
    return " ".join(part for part in (kind, key, number) if part)


def clean_call_number(value: str | None) -> str | None:
    text = " ".join((value or "").split()).upper()
    if not text:
        return None
    if len(text) > MAX_CALL_NUMBER:
        raise LocationError(_("La cote est trop longue."))
    return text


# --- Numéro d'inventaire et code-barres (D3) --------------------------------------------------------------------------
_CODE = re.compile(r"[A-Z0-9](?:[A-Z0-9-]{1,30})[A-Z0-9]")


def normalize_code(value: str | None) -> str | None:
    """Majuscules, sans espaces : « ex 000042 » devient « EX000042 ». `None` si le code est invalide ou vide."""
    text = re.sub(r"\s+", "", value or "").upper()
    return text if _CODE.fullmatch(text) else None


# --- Statuts au rangement (D7) ----------------------------------------------------------------------------------------
NOT_SHELVABLE = frozenset({"missing", "lost", "withdrawn"})


def status_after_shelving(status: str, *, first_time: bool, keep_processing: bool = False) -> str:
    """Seul « En préparation » devient « En place », au premier rangement. Les autres statuts sont conservés."""
    if status in NOT_SHELVABLE:
        raise LocationError(
            _("Cet exemplaire est « {status} » : déclarez-le d’abord retrouvé.").format(status=_status_label(status))
        )
    if status == "in_processing" and first_time and not keep_processing:
        return "available"
    return status


def _status_label(status: str) -> str:
    from biblyo.modules.catalogue.rules import COPY_STATUSES

    return COPY_STATUSES.get(status, status).lower()


#: Changements de statut demandés par une personne : statuts de départ permis, statut d'arrivée. « Retrouvé » et
#: « Réparé » sont des déclarations explicites : aucun statut ne devient « En place » de lui-même (D7).
STATUS_ACTIONS: dict[str, tuple[frozenset[str], str]] = {
    "repair": (frozenset({"in_processing", "available"}), "in_repair"),
    "repaired": (frozenset({"in_repair"}), "available"),
    "missing": (frozenset({"in_processing", "available", "in_repair"}), "missing"),
    "lost": (frozenset({"in_processing", "available", "in_repair", "missing"}), "lost"),
    "found": (frozenset({"missing", "lost"}), "available"),
    "withdraw": (frozenset({"in_processing", "available", "in_repair", "missing", "lost"}), "withdrawn"),
    "reinstate": (frozenset({"withdrawn"}), "in_processing"),
}
STATUS_ACTION_LABELS: dict[str, str] = {
    "repair": _("Envoyer en réparation"),
    "repaired": _("Déclarer réparé"),
    "missing": _("Déclarer introuvable"),
    "lost": _("Déclarer perdu"),
    "found": _("Déclarer retrouvé"),
    "withdraw": _("Retirer du fonds"),
    "reinstate": _("Réintégrer"),
}
#: Un exemplaire perdu ou retiré quitte sa place ; un exemplaire introuvable ou en réparation la garde.
UNSHELVING_ACTIONS = frozenset({"lost", "withdraw"})
#: Permission exigée par chaque changement de statut : désherber, déclarer perdu et réintégrer sont réservés à
#: `holdings.withdraw` ; le reste relève du travail courant sur les exemplaires (`holdings.edit`).
STATUS_PERMISSIONS: dict[str, str] = {
    action: "holdings.withdraw" if action in {"withdraw", "lost", "reinstate"} else "holdings.edit"
    for action in STATUS_ACTIONS
}


def status_after_action(status: str, action: str) -> str:
    if action not in STATUS_ACTIONS:
        raise LocationError(_("Ce changement de statut n’existe pas."))
    allowed, target = STATUS_ACTIONS[action]
    if status not in allowed:
        raise LocationError(
            _("Impossible depuis le statut « {status} ».").format(status=_status_label(status))
        )
    return target


def actions_for(status: str) -> list[str]:
    return [action for action, (allowed, _target) in STATUS_ACTIONS.items() if status in allowed]


# --- Acquisition (EXE-03) ---------------------------------------------------------------------------------------------
_PARTIAL_DATE = re.compile(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?")
_FRENCH_DATE = re.compile(r"(?:(\d{1,2})[/.])?(\d{1,2})[/.](\d{4})")
MIN_ACQUISITION_YEAR, MAX_ACQUISITION_YEAR = 1800, 2200
MAX_PRICE_CENTS = 10_000_000  # 100 000 €


def clean_partial_date(value: str | None) -> str | None:
    """Date d'acquisition, précise ou non : « 2026 », « 2026-03 », « 2026-03-14 », ou « 14/03/2026 », « 03/2026 ».

    Rendue au format ISO, aussi précise que la saisie. Une date floue (« inf. 2026 ») va dans la note d'acquisition.
    """
    text = (value or "").strip()
    if not text:
        return None
    if match := _PARTIAL_DATE.fullmatch(text):
        year, month, day = match.groups()
    elif match := _FRENCH_DATE.fullmatch(text):
        day, month, year = match.groups()
    else:
        raise LocationError(_("Écrivez la date sous la forme 2026, 03/2026 ou 14/03/2026."))
    if not MIN_ACQUISITION_YEAR <= int(year) <= MAX_ACQUISITION_YEAR:
        raise LocationError(_("Cette année n’est pas plausible."))
    if month is None:
        return year
    if not 1 <= int(month) <= 12:
        raise LocationError(_("Ce mois n’existe pas."))
    if day is None:
        return f"{year}-{int(month):02d}"
    try:
        moment = date(int(year), int(month), int(day))
    except ValueError:
        raise LocationError(_("Cette date n’existe pas.")) from None
    return moment.isoformat()


def parse_price(value: str | None) -> int | None:
    """Prix en centimes : « 12,50 », « 12.5 », « 12 € ». Vide : prix inconnu."""
    text = re.sub(r"[\s  €]", "", value or "").replace(",", ".")
    if not text:
        return None
    if not re.fullmatch(r"\d{1,6}(?:\.\d{1,2})?", text):
        raise LocationError(_("Écrivez le prix en euros, par exemple 12,50."))
    euros, _dot, cents = text.partition(".")
    total = int(euros) * 100 + int((cents + "00")[:2])
    if total > MAX_PRICE_CENTS:
        raise LocationError(_("Ce prix n’est pas plausible."))
    return total


def format_price(cents: int | None) -> str:
    if cents is None:
        return ""
    return f"{cents // 100},{cents % 100:02d}"


def availability(status: str, is_loanable: bool, on_loan: bool = False) -> str:
    """Disponibilité déduite, jamais saisie (EXE-02) : un prêt en cours l'emporte sur tout le reste."""
    if on_loan:
        return "on_loan"
    if status != "available":
        return "unavailable"
    return "available" if is_loanable else "on_site"


# --- Disponibilité de circulation (UX-Circulation 2.0) ----------------------------------------------------------------
#: Quatre états exclusifs, déduits du statut physique et du prêt en cours ; plus « hors fonds » (perdu, retiré), qui
#: n'entre dans aucun compteur. Aucun champ « disponible » n'est stocké : rien n'est à synchroniser avec les prêts.
STATE_AVAILABLE = "available"
STATE_ON_LOAN = "on_loan"
STATE_OVERDUE = "overdue"
STATE_UNAVAILABLE = "unavailable"
STATE_GONE = "gone"
STATE_BADGES: dict[str, str] = {
    STATE_AVAILABLE: "ok",
    STATE_ON_LOAN: "info",  # bleu : bien distinct du vert « Disponible » (consigne 8.2)
    STATE_OVERDUE: "late",
    STATE_UNAVAILABLE: "neutral",
    STATE_GONE: "neutral",
}
GONE_STATUSES = frozenset({"lost", "withdrawn"})


@dataclass(frozen=True)
class CopyState:
    """Ce qu'on peut dire d'un exemplaire au guichet : l'état, sa raison, l'échéance. **Jamais l'emprunteur.**"""

    state: str
    reason: str = ""  # pour un exemplaire indisponible ou hors fonds : « en réparation », « consultation sur place »
    due_on: str | None = None
    days_late: int = 0

    @property
    def badge(self) -> str:
        return STATE_BADGES[self.state]

    @property
    def is_loanable_now(self) -> bool:
        return self.state == STATE_AVAILABLE

    @property
    def is_out(self) -> bool:
        return self.state in (STATE_ON_LOAN, STATE_OVERDUE)

    @property
    def due_text(self) -> str:
        return f"{self.due_on[8:10]}/{self.due_on[5:7]}/{self.due_on[:4]}" if self.due_on else ""

    @property
    def label(self) -> str:
        """« Disponible », « Emprunté · retour le 01/12/2026 », « En retard · 3 jours », « Indisponible · … »."""
        if self.state == STATE_AVAILABLE:
            return _("Disponible")
        if self.state == STATE_ON_LOAN:
            return _("Emprunté · retour le {day}").format(day=self.due_text)
        if self.state == STATE_OVERDUE:
            unit = _("jour") if self.days_late == 1 else _("jours")
            return _("En retard · {count} {unit}").format(count=self.days_late, unit=unit)
        if self.state == STATE_GONE:
            return self.reason[:1].upper() + self.reason[1:]
        if self.reason == ON_SITE_REASON:
            return _("Consultation sur place")
        return _("Indisponible · {reason}").format(reason=self.reason)

    @property
    def short(self) -> str:
        """Le mot seul, pour une pastille étroite (dos d'étagère, frise)."""
        return {
            STATE_AVAILABLE: _("Disponible"),
            STATE_ON_LOAN: _("Emprunté"),
            STATE_OVERDUE: _("En retard"),
            STATE_UNAVAILABLE: _("Indisponible"),
            STATE_GONE: _("Hors fonds"),
        }[self.state]


ON_SITE_REASON = _("consultation sur place")


def copy_state(
    status: str, is_loanable: bool, due_on: str | None, today: date, status_label: str | None = None
) -> CopyState:
    """Un prêt en cours l'emporte sur tout ; puis le statut physique ; puis la consultation sur place (EXE-02).

    `status_label` : le libellé du statut physique (« En réparation »), fourni par l'appelant qui le connaît."""
    if due_on is not None:
        late = (today - date.fromisoformat(due_on[:10])).days
        if late > 0:
            return CopyState(STATE_OVERDUE, due_on=due_on, days_late=late)
        return CopyState(STATE_ON_LOAN, due_on=due_on)
    label = (status_label or status).lower()
    if status in GONE_STATUSES:
        return CopyState(STATE_GONE, reason=label)
    if status != "available":
        return CopyState(STATE_UNAVAILABLE, reason=label)
    if not is_loanable:
        return CopyState(STATE_UNAVAILABLE, reason=ON_SITE_REASON)
    return CopyState(STATE_AVAILABLE)


@dataclass(frozen=True)
class WorkCounts:
    """Les exemplaires d'une œuvre par état, **sans double compte** : « en retard » est retiré de « emprunté ».

    available + on_loan + overdue + unavailable = exemplaires du fonds (tous sauf perdus et retirés)."""

    available: int = 0
    on_loan: int = 0
    overdue: int = 0
    unavailable: int = 0
    gone: int = 0
    on_site: int = 0  # parmi les indisponibles : consultation sur place seulement
    next_due: str | None = None  # la plus proche échéance d'un exemplaire sorti

    @property
    def total(self) -> int:
        return self.available + self.on_loan + self.overdue + self.unavailable

    @property
    def out(self) -> int:
        return self.on_loan + self.overdue

    @property
    def headline(self) -> tuple[str, str]:
        """(libellé, pastille) : ce qu'une carte du catalogue montre d'abord."""
        if self.total == 0:
            return (_("Aucun exemplaire"), "neutral")
        if self.available:
            word = _("disponible") if self.available == 1 else _("disponibles")
            return (f"{self.available} {word}", "ok")
        if self.overdue and not self.on_loan:
            return (_("En retard") if self.total == 1 else _("Tous sortis, en retard"), "late")
        if self.out:
            if self.total == 1 and self.next_due:
                day = f"{self.next_due[8:10]}/{self.next_due[5:7]}"
                return (_("Emprunté · retour le {day}").format(day=day), "info")
            return (_("Aucun disponible"), "info")
        if self.on_site == self.unavailable:
            return (_("Consultation sur place"), "neutral")
        return (_("Indisponible"), "neutral")

    @property
    def segments(self) -> list[tuple[str, str]]:
        """Les états présents, dans l'ordre utile : « 2 disponibles · 1 emprunté · 1 en retard · 1 indisponible »."""
        found: list[tuple[str, str]] = []
        if self.available:
            found.append((f"{self.available} {_('disponible') if self.available == 1 else _('disponibles')}", "ok"))
        if self.on_loan:
            found.append((f"{self.on_loan} {_('emprunté') if self.on_loan == 1 else _('empruntés')}", "info"))
        if self.overdue:
            found.append((f"{self.overdue} {_('en retard')}", "late"))
        if self.unavailable:
            word = _("indisponible") if self.unavailable == 1 else _("indisponibles")
            found.append((f"{self.unavailable} {word}", "neutral"))
        return found


def count_states(states: Iterable[CopyState]) -> WorkCounts:
    """Le décompte d'une liste d'états : la même règle pour une fiche et pour une page entière du catalogue."""
    counts = {STATE_AVAILABLE: 0, STATE_ON_LOAN: 0, STATE_OVERDUE: 0, STATE_UNAVAILABLE: 0, STATE_GONE: 0}
    on_site = 0
    next_due: str | None = None
    for item in states:
        counts[item.state] += 1
        on_site += item.state == STATE_UNAVAILABLE and item.reason == ON_SITE_REASON
        if item.due_on and (next_due is None or item.due_on < next_due):
            next_due = item.due_on
    return WorkCounts(
        counts[STATE_AVAILABLE],
        counts[STATE_ON_LOAN],
        counts[STATE_OVERDUE],
        counts[STATE_UNAVAILABLE],
        counts[STATE_GONE],
        on_site,
        next_due,
    )


def availability_text(status: str, is_loanable: bool, due_on: str | None = None) -> str:
    """« En prêt · retour le 07/12/2026 » : l'échéance, **jamais l'emprunteur** (D11 bis du plan de L4)."""
    label = AVAILABILITY[availability(status, is_loanable, due_on is not None)]
    if due_on is None:
        return label
    return _("{label} · retour le {day}").format(label=label, day=f"{due_on[8:10]}/{due_on[5:7]}/{due_on[:4]}")


AVAILABILITY: dict[str, str] = {
    "available": _("Disponible"),
    "on_site": _("Consultation sur place"),
    "unavailable": _("Indisponible"),
    "on_loan": _("En prêt"),
}
CONDITIONS: dict[str, str] = {
    "new": _("Neuf"),
    "good": _("Bon"),
    "fair": _("Correct"),
    "worn": _("Usé"),
    "damaged": _("Abîmé"),
}
ACQUISITION_MODES: dict[str, str] = {
    "purchase": _("Achat"),
    "donation": _("Don"),
    "exchange": _("Échange"),
    "legacy": _("Fonds existant"),
    "unknown": _("Inconnu"),
}

# --- Dessin des dos (D6) : illustratif, jamais une règle de capacité --------------------------------------------------
SPINE_WIDTHS = 8
_TYPE_THICKNESS = {"bd": 4, "manga": 3, "livre": 4, "comics": 3, "revue": 1, "documentaire": 4, "album": 4}
#: Épaisseur minimale : un album cartonné de 48 pages reste plus épais qu'une revue du même nombre de pages.
_TYPE_MIN_THICKNESS = {"bd": 4, "album": 4, "comics": 3, "manga": 3, "livre": 2, "documentaire": 2, "revue": 1}
_TYPE_HEIGHT = {"bd": "tall", "manga": "short", "livre": "mid", "comics": "mid", "revue": "tall", "album": "tall"}


def spine_width(pages: int | None, type_code: str) -> int:
    """Classe de largeur de 1 à 8 : d'après le nombre de pages s'il est connu, sinon d'après le type ; jamais sous
    l'épaisseur minimale du type."""
    if not pages:
        return _TYPE_THICKNESS.get(type_code, 3)
    minimum = _TYPE_MIN_THICKNESS.get(type_code, 1)
    for width, limit in enumerate((48, 96, 160, 240, 340, 480, 700), start=1):
        if pages <= limit:
            return max(width, minimum)
    return SPINE_WIDTHS


def spine_height(type_code: str) -> str:
    return _TYPE_HEIGHT.get(type_code, "mid")

