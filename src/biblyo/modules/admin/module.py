# SPDX-License-Identifier: EUPL-1.2
"""Module Administration : comptes, rôles, réglages, à propos."""

from __future__ import annotations

from pathlib import Path

from biblyo.core.i18n import _
from biblyo.core.modules import Module, NavEntry, Permission
from biblyo.modules.admin.parametres_routes import router as parametres_router
from biblyo.modules.admin.routes import router

router.include_router(parametres_router)

MODULE = Module(
    code="admin",
    label=_("Administration"),
    router=router,
    permissions=(
        Permission("admin.users", _("Gérer les utilisateurs")),
        Permission("admin.roles", _("Gérer les rôles")),
        Permission("admin.settings", _("Modifier les réglages")),
        Permission("admin.identity", _("Modifier l’identité de la bibliothèque")),
        Permission("admin.modules", _("Activer ou désactiver des modules")),
        Permission("admin.sources", _("Configurer les sources bibliographiques")),
        Permission("admin.backups", _("Gérer les sauvegardes")),
        Permission("admin.audit", _("Consulter le journal")),
    ),
    nav=(
        NavEntry(
            _("Utilisateurs"),
            "/admin/utilisateurs",
            "key",
            "io",
            "pied",
            order=10,
            permission="admin.users",
            code="utilisateurs",
            shortcut="u",
        ),
        NavEntry(
            _("Paramètres"),
            "/parametres",
            "gear",
            "io",
            "pied",
            order=20,
            permission="admin.settings",
            code="parametres",
        ),
        NavEntry(_("À propos"), "/a-propos", "info", "io", "pied", order=30, code="a-propos"),
    ),
    public_paths=("/a-propos", "/connexion", "/installation", "/identite/logo.webp"),
    template_dir=Path(__file__).parent / "templates",
)
