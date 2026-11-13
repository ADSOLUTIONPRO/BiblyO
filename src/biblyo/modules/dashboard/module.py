# SPDX-License-Identifier: EUPL-1.2
"""Module Accueil."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry
from biblyo.modules.dashboard.routes import router

MODULE = Module(
    code="dashboard",
    label=_("Tableau de bord"),
    router=router,
    nav=(NavEntry(_("Tableau de bord"), "/", "home", "loan", "pilotage", order=10, code="dashboard", shortcut="t"),),
    template_dir=Path(__file__).parent / "templates",
)
