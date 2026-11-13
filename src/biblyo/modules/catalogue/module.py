# SPDX-License-Identifier: EUPL-1.2
"""Module Catalogue."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry, Permission
from biblyo.core.security.sessions import AuthContext
from biblyo.modules.catalogue import models as models
from biblyo.modules.catalogue import repository, stats
from biblyo.modules.catalogue.routes import router


def _nav_counts(session: Session, auth: AuthContext) -> dict[str, int]:
    return {"catalogue": repository.active_work_count(session)} if auth.can("catalogue.view") else {}


MODULE = Module(
    code="catalogue",
    label=_("Catalogue"),
    router=router,
    nav=(
        NavEntry(
            _("Catalogue"),
            "/catalogue",
            "book",
            "cat",
            "collection",
            order=10,
            permission="catalogue.view",
            code="catalogue",
            shortcut="c",
        ),
        NavEntry(
            _("Séries & collections"),
            "/catalogue/series",
            "series",
            "ser",
            "collection",
            order=20,
            permission="catalogue.view",
            code="series",
            shortcut="s",
        ),
    ),
    permissions=(
        Permission("catalogue.view", _("Consulter le catalogue")),
        Permission("catalogue.edit", _("Créer et modifier des notices, séries, contributeurs")),
        Permission("catalogue.archive", _("Archiver ou supprimer une notice sans historique")),
    ),
    template_dir=Path(__file__).parent / "templates",
    extras={"nav_counts": _nav_counts, "dashboard": stats.dashboard_context},
)
