# SPDX-License-Identifier: EUPL-1.2
"""Module Lecteurs et familles (lot L3)."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry, Permission
from biblyo.modules.patrons import models as models
from biblyo.modules.patrons.routes import router

MODULE = Module(
    code="patrons",
    label=_("Lecteurs et familles"),
    router=router,
    nav=(
        NavEntry(
            _("Lecteurs & familles"),
            "/lecteurs",
            "users",
            "rdr",
            "circulation",
            order=20,
            permission="patrons.view",
            code="lecteurs",
            shortcut="l",
        ),
    ),
    permissions=(
        Permission("patrons.view", _("Voir les lecteurs : nom, numéros, foyers, sans coordonnées")),
        Permission("patrons.register", _("Inscrire un lecteur ou une famille, et corriger pendant l’inscription")),
        Permission("patrons.view_personal", _("Voir les coordonnées, la date de naissance et les observations")),
        Permission("patrons.edit", _("Modifier les lecteurs, foyers, responsables et cartes ; renouveler l’adhésion")),
        Permission("patrons.archive", _("Suspendre, archiver, réactiver")),
        Permission("patrons.export", _("Exporter les données d’un lecteur")),
        Permission("patrons.anonymize", _("Anonymiser un lecteur, le mettre en conservation")),
    ),
    template_dir=Path(__file__).parent / "templates",
)
