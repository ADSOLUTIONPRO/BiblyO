# SPDX-License-Identifier: EUPL-1.2
"""Navigation : réunit les entrées déclarées par les modules et les modules à venir.

Une entrée n'apparaît que si le compte a la permission voulue ; une entrée « à venir » mène à une page qui
le dit, sans donnée fictive, et disparaît en production.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from biblyo.config import Settings
from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry
from biblyo.core.security.sessions import AuthContext

#: Groupes de la barre latérale, dans leur ordre d'affichage.
GROUPS: tuple[tuple[str, str], ...] = (
    ("pilotage", _("Pilotage")),
    ("collection", _("Collection")),
    ("circulation", _("Circulation")),
    ("maintenance", _("Maintenance")),
)
FOOT_GROUP = "pied"
PLANNED_PREFIX = "/a-venir/"
SOON_LABEL = _("À venir")


@dataclass(frozen=True)
class PlannedModule:
    code: str
    label: str
    icon: str
    color: str
    group: str
    order: int
    permission: str
    summary: str
    shortcut: str = ""
    in_nav: bool = True

    @property
    def path(self) -> str:
        return f"{PLANNED_PREFIX}{self.code}"


PLANNED: tuple[PlannedModule, ...] = (
    PlannedModule(
        "statistiques",
        _("Statistiques"),
        "stats",
        "sta",
        "pilotage",
        20,
        "catalogue.view",
        _("Prêts, fréquentation et état du fonds."),
    ),
    PlannedModule(
        "inventaire",
        _("Inventaire"),
        "inventory",
        "inv",
        "maintenance",
        20,
        "inventory.run",
        _("Récolement du fonds, rayon par rayon."),
        "i",
    ),
    PlannedModule(
        "scanner",
        _("Scanner"),
        "scan",
        "cat",
        "",
        0,
        "catalogue.view",
        _("Lecture de codes-barres à la caméra ou à la douchette."),
        in_nav=False,
    ),
)
PLANNED_BY_CODE = {module.code: module for module in PLANNED}


@dataclass(frozen=True)
class NavItem:
    code: str
    label: str
    path: str
    icon: str
    color: str
    group: str
    order: int
    planned: bool = False
    current: bool = False
    shortcut: str = ""
    count: int | None = None


@dataclass(frozen=True)
class NavGroup:
    code: str
    label: str
    items: tuple[NavItem, ...]


@dataclass(frozen=True)
class QuickAction:
    label: str
    path: str
    icon: str
    primary: bool = False
    planned: bool = True
    key: str = ""  # raccourci direct, sans « G »


@dataclass(frozen=True)
class Navigation:
    groups: tuple[NavGroup, ...] = ()
    foot: tuple[NavItem, ...] = ()
    quick_actions: tuple[QuickAction, ...] = ()
    scanner: str = ""
    phone: tuple[NavItem, ...] = ()
    soon_label: str = SOON_LABEL
    all_items: tuple[NavItem, ...] = field(default=())


def _is_current(path: str, request_path: str) -> bool:
    if path == "/":
        return request_path == "/"
    return request_path == path or request_path.startswith(f"{path}/")


def _permitted(auth: AuthContext, permission: str | None) -> bool:
    return permission is None or auth.can(permission)


def _module_item(entry: NavEntry, request_path: str) -> NavItem:
    return NavItem(
        code=entry.code or entry.path,
        label=entry.label,
        path=entry.path,
        icon=entry.icon,
        color=entry.color,
        group=entry.group,
        order=entry.order,
        current=_is_current(entry.path, request_path),
        shortcut=entry.shortcut,
    )


def _planned_item(module: PlannedModule, request_path: str) -> NavItem:
    return NavItem(
        code=module.code,
        label=module.label,
        path=module.path,
        icon=module.icon,
        color=module.color,
        group=module.group,
        order=module.order,
        planned=True,
        current=_is_current(module.path, request_path),
        shortcut=module.shortcut,
    )


def _only_longest_current(items: list[NavItem]) -> list[NavItem]:
    """Sur /catalogue/series, seule « Séries » est l'entrée courante, pas « Catalogue »."""
    current = [item for item in items if item.current]
    if len(current) < 2:
        return items
    longest = max(current, key=lambda item: len(item.path))
    return [replace(item, current=False) if item.current and item is not longest else item for item in items]


def build_navigation(
    modules: list[Module],
    auth: AuthContext,
    request_path: str,
    settings: Settings,
    counts: Mapping[str, int] | None = None,
) -> Navigation:
    items = [
        replace(_module_item(entry, request_path), count=(counts or {}).get(entry.code))
        for module in modules
        for entry in module.nav
        if _permitted(auth, entry.permission)
    ]
    show_planned = settings.planned_modules_visible
    if show_planned:
        taken = {item.code for item in items}
        items.extend(
            _planned_item(module, request_path)
            for module in PLANNED
            if module.in_nav and module.code not in taken and auth.can(module.permission)
        )
    items.sort(key=lambda item: item.order)
    items = _only_longest_current(items)

    groups = tuple(
        NavGroup(code, label, tuple(item for item in items if item.group == code))
        for code, label in GROUPS
        if any(item.group == code for item in items)
    )
    by_code = {item.code: item for item in items}
    phone_codes = ("dashboard", "catalogue", "prets")
    return Navigation(
        groups=groups,
        foot=tuple(item for item in items if item.group == FOOT_GROUP),
        quick_actions=tuple(action for action in _quick_actions(auth) if show_planned or not action.planned),
        scanner=_scanner_path(auth) if show_planned else "",
        phone=tuple(by_code[code] for code in phone_codes if code in by_code),
        all_items=tuple(items),
    )


def _quick_actions(auth: AuthContext) -> tuple[QuickAction, ...]:
    actions: list[QuickAction] = []
    if auth.can("circulation.lend"):
        actions.append(QuickAction(_("Prêt"), "/comptoir", "out", planned=False, key="p"))
        actions.append(QuickAction(_("Retour"), "/comptoir/retours", "ret", planned=False, key="r"))
    if auth.can("catalogue.edit"):
        actions.append(QuickAction(_("Ajouter"), "/catalogue/notices/nouvelle", "plus", primary=True, planned=False))
    return tuple(actions)


def _scanner_path(auth: AuthContext) -> str:
    module = PLANNED_BY_CODE["scanner"]
    return module.path if auth.can(module.permission) else ""


def navigation_for(request: Request, auth: AuthContext | None) -> Navigation:
    if auth is None:
        return Navigation()
    app = request.app
    return build_navigation(app.state.modules, auth, request.url.path, app.state.settings, _nav_counts(request, auth))


def _nav_counts(request: Request, auth: AuthContext) -> dict[str, int]:
    """Compteurs de la barre latérale, fournis par les modules. Une panne de lecture ne gêne pas la page."""
    counts: dict[str, int] = {}
    try:
        with request.app.state.database.session() as session:
            for module in request.app.state.modules:
                provider = module.extras.get("nav_counts")
                if callable(provider):
                    counts.update(provider(session, auth))
    except SQLAlchemyError:
        return {}
    return counts
