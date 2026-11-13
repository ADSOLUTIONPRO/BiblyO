# SPDX-License-Identifier: EUPL-1.2
"""Module Exemplaires et emplacements (lot L2)."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry, Permission
from biblyo.modules.holdings import models as models
from biblyo.modules.holdings.routes import router

MODULE = Module(
    code="holdings",
    label=_("Exemplaires et emplacements"),
    router=router,
    depends_on=("catalogue",),
    nav=(
        NavEntry(
            _("Étagères"),
            "/emplacements",
            "shelf",
            "she",
            "collection",
            order=30,
            permission="catalogue.view",
            code="etageres",
            shortcut="e",
        ),
    ),
    permissions=(
        Permission("holdings.edit", _("Créer et modifier des exemplaires, cotes, codes-barres, et les ranger")),
        Permission("holdings.withdraw", _("Retirer (désherber), déclarer perdu")),
        Permission("locations.manage", _("Gérer les emplacements et le rangement")),
    ),
    template_dir=Path(__file__).parent / "templates",
)
