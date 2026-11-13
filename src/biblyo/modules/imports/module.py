# SPDX-License-Identifier: EUPL-1.2
"""Module Import (lot L6) : dépôt d'un fichier, correspondance des colonnes, simulation sans écriture.

L'application au catalogue (T5) et l'export (T8) viendront ensuite ; la permission `imports.apply` est déclarée dès
maintenant pour que les rôles existants la portent déjà.
"""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry, Permission
from biblyo.modules.imports import models as models
from biblyo.modules.imports.routes import router

MODULE = Module(
    code="imports",
    label=_("Import et export"),
    router=router,
    depends_on=("catalogue", "holdings"),
    nav=(
        NavEntry(
            _("Import & export"),
            "/imports",
            "io",
            "io",
            "maintenance",
            order=30,
            permission="imports.simulate",
            code="imports",
            shortcut="x",
        ),
    ),
    permissions=(
        Permission("imports.simulate", _("Lire un fichier et simuler son import, sans rien écrire")),
        Permission("imports.apply", _("Appliquer un import simulé au catalogue")),
    ),
    template_dir=Path(__file__).parent / "templates",
)
