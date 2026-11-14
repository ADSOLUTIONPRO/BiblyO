# SPDX-License-Identifier: EUPL-1.2
"""Module Enrichissement (lot L5) : déclaration, permissions, modèles."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry, Permission
from biblyo.modules.enrichment import models as models
from biblyo.modules.enrichment.creation import notice_created, notice_prefill
from biblyo.modules.enrichment.routes import cover_search_ready, router
from biblyo.modules.enrichment.tab import tab_context

MODULE = Module(
    code="enrichment",
    label=_("Enrichissement"),
    router=router,
    depends_on=("catalogue",),
    nav=(
        NavEntry(
            _("Enrichissement"),
            "/enrichissement",
            "enrich",
            "enr",
            "maintenance",
            order=10,
            permission="enrichment.search",
            code="enrichissement",
            shortcut="n",
        ),
    ),
    permissions=(
        Permission("enrichment.search", _("Chercher dans les sources bibliographiques et comparer")),
        Permission("enrichment.review", _("Appliquer ou annuler un enrichissement")),
        Permission("enrichment.batch", _("Enrichir toute la bibliothèque")),
    ),
    template_dir=Path(__file__).parent / "templates",
    can_be_disabled=True,
    extras={
        "enrichment_tab": tab_context,
        "notice_prefill": notice_prefill,
        "notice_created": notice_created,
        "cover_search_ready": cover_search_ready,
    },
)
