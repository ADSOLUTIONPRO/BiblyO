# SPDX-License-Identifier: EUPL-1.2
"""Ce que les dessins d'étagères affichent : dos, niveaux, meubles, arbre, plan de salle (plan de L2, section 3).

Rien ici ne lit la base : les pages et le catalogue des composants passent des faits, et reçoivent des vues prêtes à
rendre. Largeurs, hauteurs, couleurs et places sur le plan deviennent des **classes** : la CSP interdit tout style en
ligne. Les dimensions d'un dos sont une illustration (D6) : elles ne servent jamais à la capacité (D1).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from biblyo.core.i18n import _
from biblyo.modules.catalogue.rules import COPY_STATUS_BADGES, COPY_STATUSES, cover_color
from biblyo.modules.holdings import rules
from biblyo.modules.holdings.rules import Fill, GridBox

MAX_FREE_SLOTS = 6  # au-delà : « +N places »
#: Pictogramme qui double l'état d'un dos : l'état n'est jamais porté par la couleur seule.
STATE_ICONS: dict[str, str] = {"in_processing": "clock", "in_repair": "gear", "missing": "alert"}
STATE_CLASSES: dict[str, str] = {"in_processing": "st-prep", "in_repair": "st-repair", "missing": "st-missing"}


# --- Dos --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SpineFacts:
    """Un exemplaire rangé, tel que le dessin en a besoin."""

    copy_id: int
    position: int | None
    title: str
    status: str
    type_code: str
    href: str
    call_number: str | None = None
    pages: int | None = None
    series: str | None = None
    volume: str | None = None
    condition: str | None = None
    barcode: str = ""
    version: int = 1
    due_on: str | None = None  # en prêt : l'échéance seulement, jamais l'emprunteur
    overdue: bool = False  # échéance passée : le dos sorti porte en plus une bande de retard


@dataclass(frozen=True)
class Spine:
    copy_id: int
    position: int | None
    title: str
    call_number: str | None
    status: str
    href: str
    width: int
    height: str
    color: int
    volume: str | None
    label: str
    state_class: str
    state_icon: str | None
    group: str | None = None
    group_edge: str | None = None  # start, mid, end, solo : le bandeau de série court d'un dos à l'autre
    hit: bool = False
    condition_label: str = ""
    barcode: str = ""
    version: int = 1
    due_on: str | None = None
    overdue: bool = False

    @property
    def status_text(self) -> str:
        if self.due_on and self.overdue:
            return _("Sorti, en retard : retour prévu le {day}").format(day=_day(self.due_on))
        if self.due_on:
            return _("Sorti, retour le {day}").format(day=_day(self.due_on))
        return COPY_STATUSES.get(self.status, self.status)

    @property
    def status_badge(self) -> str:
        if self.due_on:
            return "late" if self.overdue else "info"
        return COPY_STATUS_BADGES.get(self.status, "neutral")

    @property
    def classes(self) -> str:
        parts = ["spn", f"w{self.width}", f"h-{self.height}", f"cv-{self.color}"]
        if self.state_class:
            parts.append(self.state_class)
        if self.hit:
            parts.append("hit")
        return " ".join(parts)


def status_label(status: str) -> str:
    return COPY_STATUSES.get(status, status).lower()


def _day(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}"


def spine_label(facts: SpineFacts) -> str:
    """Nom accessible d'un dos : « Position 3 : Astérix le Gaulois, tome 1, cote BD AST 3, en place »."""
    parts = [facts.title]
    if facts.volume:
        parts.append(_("tome {volume}").format(volume=facts.volume))
    if facts.call_number:
        parts.append(_("cote {call}").format(call=facts.call_number))
    if facts.due_on and facts.overdue:
        parts.append(_("sorti, en retard, retour prévu le {day}").format(day=_day(facts.due_on)))
    elif facts.due_on:
        parts.append(_("sorti, retour le {day}").format(day=_day(facts.due_on)))
    else:
        parts.append(status_label(facts.status))
    text = ", ".join(parts)
    if facts.position is not None:
        return _("Position {n} : {text}").format(n=facts.position, text=text)
    return text


def spines(facts: Sequence[SpineFacts], *, hit: int | None = None, group_series: bool = True) -> list[Spine]:
    """Dos dans l'ordre réel. Des dos voisins d'une même série partagent un bandeau de sa couleur."""
    keys = [item.series if group_series and item.series else None for item in facts]
    built: list[Spine] = []
    for index, item in enumerate(facts):
        key = keys[index]
        edge = None
        if key is not None:
            joined_before = index > 0 and keys[index - 1] == key
            joined_after = index + 1 < len(facts) and keys[index + 1] == key
            edge = {(False, False): "solo", (False, True): "start", (True, True): "mid", (True, False): "end"}[
                (joined_before, joined_after)
            ]
        built.append(
            Spine(
                copy_id=item.copy_id,
                position=item.position,
                title=item.title,
                call_number=item.call_number,
                status=item.status,
                href=item.href,
                width=rules.spine_width(item.pages, item.type_code),
                height=rules.spine_height(item.type_code),
                color=cover_color(item.series or item.title),
                volume=item.volume,
                label=spine_label(item),
                state_class=("st-out st-late" if item.overdue else "st-out")
                if item.due_on
                else STATE_CLASSES.get(item.status, ""),
                state_icon="out" if item.due_on else STATE_ICONS.get(item.status),
                group=key,
                group_edge=edge,
                hit=item.copy_id == hit,
                condition_label=rules.CONDITIONS.get(item.condition or "", ""),
                barcode=item.barcode,
                version=item.version,
                due_on=item.due_on,
                overdue=item.overdue,
            )
        )
    return built


# --- Remplissage et niveaux -------------------------------------------------------------------------------------------
def fill_label(fill: Fill) -> str:
    """« 27 / 32 places », « 34 / 32 places, dépassée de 2 », « 12 exemplaires » sans capacité."""
    if fill.capacity is None:
        return _("{n} exemplaires").format(n=fill.count) if fill.count != 1 else _("1 exemplaire")
    text = _("{n} / {cap} places").format(n=fill.count, cap=fill.capacity)
    if fill.state == "over":
        text += _(", dépassée de {n}").format(n=fill.excess)
    elif fill.state == "full":
        text += _(", pleine")
    return text


@dataclass(frozen=True)
class LevelView:
    id: int
    name: str
    href: str
    spines: list[Spine]
    fill: Fill
    focus: bool = False
    hidden: int = 0  # dos non dessinés : un très long niveau se consulte dans la vue Liste
    version: int = 1

    @property
    def free_slots(self) -> int:
        return min(self.fill.free, MAX_FREE_SLOTS)

    @property
    def more_free(self) -> int:
        return max(0, self.fill.free - MAX_FREE_SLOTS)

    @property
    def more_free_text(self) -> str:
        return _("+1 place") if self.more_free == 1 else _("+{n} places").format(n=self.more_free)

    @property
    def caption(self) -> str:
        """Étiquette du niveau : « Étagère 2 · 27/32 »."""
        if self.fill.capacity is None:
            return f"{self.name} · {self.fill.count}"
        return f"{self.name} · {self.fill.count}/{self.fill.capacity}"

    @property
    def fill_text(self) -> str:
        return fill_label(self.fill)


@dataclass(frozen=True)
class UnitView:
    """Un meuble en élévation : ses niveaux de haut en bas."""

    id: int
    name: str
    href: str
    levels: list[LevelView]
    code: str | None = None
    hint: str | None = None

    @property
    def count(self) -> int:
        return sum(level.fill.count for level in self.levels)

    @property
    def count_text(self) -> str:
        return _("1 exemplaire") if self.count == 1 else _("{n} exemplaires").format(n=self.count)


# --- Arbre ------------------------------------------------------------------------------------------------------------
class LocationLike(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def parent_id(self) -> int | None: ...
    @property
    def kind(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def sort_order(self) -> int: ...
    @property
    def capacity(self) -> int | None: ...
    @property
    def is_active(self) -> bool: ...


KIND_ICONS: dict[str, str] = {
    "site": "home",
    "room": "building",
    "furniture": "shelf",
    "shelf": "list",
    "bin": "archive",
    "virtual": "pin",
}


@dataclass
class TreeNode:
    id: int
    name: str
    kind: str
    href: str
    count: int
    fill: Fill | None
    depth: int
    active: bool
    current: bool = False
    open: bool = False
    children: list[TreeNode] = field(default_factory=list)

    @property
    def icon(self) -> str:
        return KIND_ICONS.get(self.kind, "pin")

    @property
    def kind_label(self) -> str:
        return rules.KINDS.get(self.kind, self.kind)


def tree(
    locations: Iterable[LocationLike],
    direct_counts: Mapping[int, int],
    *,
    href: str = "/emplacements/{id}",
    current: int | None = None,
) -> list[TreeNode]:
    """Arbre des lieux, avec les exemplaires de chaque nœud (descendants compris) et le remplissage des niveaux.

    La branche du lieu courant est ouverte ; le reste aussi, jusqu'aux meubles : l'arbre reste lisible d'un coup d'œil.
    """
    items = list(locations)
    by_parent: dict[int | None, list[LocationLike]] = defaultdict(list)
    for item in items:
        by_parent[item.parent_id].append(item)
    for siblings in by_parent.values():
        siblings.sort(key=lambda item: (item.sort_order, item.name, item.id))
    parent_of = {item.id: item.parent_id for item in items}
    on_path: set[int] = set()
    cursor = current
    while cursor is not None and cursor not in on_path:
        on_path.add(cursor)
        cursor = parent_of.get(cursor)

    def build(item: LocationLike, depth: int) -> TreeNode:
        children = [build(child, depth + 1) for child in by_parent.get(item.id, [])]
        count = direct_counts.get(item.id, 0) + sum(child.count for child in children)
        level = rules.is_level(item.kind)
        return TreeNode(
            id=item.id,
            name=item.name,
            kind=item.kind,
            href=href.format(id=item.id),
            count=count,
            fill=Fill(direct_counts.get(item.id, 0), item.capacity) if level else None,
            depth=depth,
            active=item.is_active,
            current=item.id == current,
            open=item.id in on_path or item.kind in {"site", "room"},
            children=children,
        )

    return [build(root, 0) for root in by_parent.get(None, [])]


# --- Plan de salle (D5) -----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RoomTile:
    id: int
    name: str
    href: str
    count: int
    levels: list[int]  # remplissage de chaque niveau, en pour cent, de haut en bas
    box: GridBox | None
    hint: str | None = None
    current: bool = False

    @property
    def grid_classes(self) -> str:
        """Place sur la grille, en classes utilitaires, à partir de 768 px : aucun style en ligne. En dessous, le plan
        redevient la liste des meubles (D13)."""
        if self.box is None:
            return ""
        box = self.box
        return f"md:row-start-{box.row} md:col-start-{box.col} md:row-span-{box.height} md:col-span-{box.width}"

    @property
    def summary(self) -> str:
        parts = [self.hint] if self.hint else []
        parts.append(_("1 ex.") if self.count == 1 else _("{n} ex.").format(n=self.count))
        return " · ".join(parts)


@dataclass(frozen=True)
class RoomPlan:
    id: int
    name: str
    tiles: list[RoomTile]

    @property
    def placed(self) -> list[RoomTile]:
        return [tile for tile in self.tiles if tile.box is not None]

    @property
    def unplaced(self) -> list[RoomTile]:
        return [tile for tile in self.tiles if tile.box is None]

    @property
    def columns(self) -> int:
        """Largeur du plan : celle des meubles placés, 8 colonnes au moins, 40 au plus."""
        widest = max((tile.box.col + tile.box.width - 1 for tile in self.placed if tile.box), default=0)
        return max(8, min(rules.GRID_MAX, widest))

    @property
    def rows(self) -> int:
        deepest = max((tile.box.row + tile.box.height - 1 for tile in self.placed if tile.box), default=0)
        return max(1, deepest)

    @property
    def grid_classes(self) -> str:
        """Le nombre de colonnes passe par une propriété posée par une classe utilitaire : aucun style en ligne."""
        return f"is-plan [--room-cols:{self.columns}]"
