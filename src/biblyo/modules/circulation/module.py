# SPDX-License-Identifier: EUPL-1.2
"""Module Prêts et retours (lot L4)."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry, Permission
from biblyo.modules.circulation import models as models
from biblyo.modules.circulation.routes import (
    book_loans_tab,
    dashboard_context,
    patron_export,
    patron_loans_card,
    router,
)

MODULE = Module(
    code="circulation",
    label=_("Prêts et retours"),
    router=router,
    depends_on=("catalogue", "holdings", "patrons"),
    nav=(
        NavEntry(
            _("Prêts & retours"),
            "/comptoir",
            "loans",
            "loan",
            "circulation",
            order=10,
            permission="circulation.lend",
            code="prets",
            shortcut="p",
        ),
    ),
    permissions=(
        Permission("circulation.lend", _("Prêter, rendre, annuler sa propre opération dans les 15 minutes")),
        Permission("circulation.renew", _("Renouveler un prêt")),
        Permission("circulation.override", _("Déroger avec motif, annuler au-delà du délai, corriger un prêt")),
        Permission("circulation.override_limit", _("Dépasser un plafond de prêts bloquant, avec motif")),
        Permission("circulation.history", _("Voir l’emprunteur, l’historique des prêts et les rappels")),
        Permission("circulation.settings", _("Régler les durées, les plafonds et les seuils")),
    ),
    template_dir=Path(__file__).parent / "templates",
    extras={
        "dashboard": dashboard_context,
        "patron_loans": patron_loans_card,
        "patron_export": patron_export,
        "book_loans": book_loans_tab,
    },
)
