# SPDX-License-Identifier: EUPL-1.2
"""Registre des modules : chaque module se déclare, `create_app` assemble."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter


class ModuleError(ValueError):
    """Déclaration de module incohérente : l'application refuse de démarrer."""


@dataclass(frozen=True)
class Permission:
    code: str
    label: str


@dataclass(frozen=True)
class NavEntry:
    """Entrée de navigation déclarée par un module."""

    label: str
    path: str
    icon: str
    color: str  # clé de couleur de module : cat, ser, she, loan, rdr, enr, inv, io, sta
    group: str  # pilotage, collection, circulation, maintenance, pied
    order: int = 0
    permission: str | None = None
    code: str = ""  # identifiant stable ; par défaut, le chemin
    shortcut: str = ""  # lettre du raccourci « G puis lettre »


@dataclass(frozen=True)
class Module:
    code: str
    label: str
    router: APIRouter | None = None
    depends_on: tuple[str, ...] = ()
    permissions: tuple[Permission, ...] = ()
    nav: tuple[NavEntry, ...] = ()
    public_paths: tuple[str, ...] = ()  # toute autre route doit passer par require() ou require_session()
    template_dir: Path | None = None
    can_be_disabled: bool = False
    extras: dict[str, object] = field(default_factory=dict)


def order_modules(modules: list[Module] | tuple[Module, ...]) -> list[Module]:
    """Vérifie les déclarations et rend les modules dans l'ordre de leurs dépendances."""
    by_code: dict[str, Module] = {}
    for module in modules:
        if module.code in by_code:
            raise ModuleError(f"Module déclaré deux fois : {module.code}")
        by_code[module.code] = module
    for module in modules:
        for dependency in module.depends_on:
            if dependency not in by_code:
                raise ModuleError(f"Le module {module.code} dépend d'un module absent : {dependency}")

    ordered: list[Module] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(code: str) -> None:
        if code in done:
            return
        if code in visiting:
            raise ModuleError(f"Cycle de dépendances entre modules autour de : {code}")
        visiting.add(code)
        for dependency in by_code[code].depends_on:
            visit(dependency)
        visiting.discard(code)
        done.add(code)
        ordered.append(by_code[code])

    for module in modules:
        visit(module.code)

    codes = [p.code for m in ordered for p in m.permissions]
    duplicates = {c for c in codes if codes.count(c) > 1}
    if duplicates:
        raise ModuleError(f"Permissions déclarées plusieurs fois : {sorted(duplicates)}")
    return ordered
