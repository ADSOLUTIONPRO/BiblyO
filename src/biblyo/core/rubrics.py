# SPDX-License-Identifier: EUPL-1.2
"""Rubriques des paramètres (L7, T5) : sept entrées, chacune visible selon les droits du compte."""

from __future__ import annotations

from dataclasses import dataclass

from biblyo.core.i18n import _
from biblyo.core.security.sessions import AuthContext


@dataclass(frozen=True)
class Rubric:
    key: str
    label: str
    href: str
    icon: str
    description: str
    needs: tuple[str, ...]  # l'un de ces droits suffit


RUBRICS: tuple[Rubric, ...] = (
    Rubric(
        "bibliotheque",
        _("Ma bibliothèque"),
        "/parametres/bibliotheque",
        "building",
        _("Nom, logo et coordonnées affichés dans biblyO."),
        ("admin.identity", "admin.settings"),
    ),
    Rubric(
        "catalogue",
        _("Catalogue et affichage"),
        "/parametres/catalogue",
        "book",
        _("Genres, catégories, types de document et sections du catalogue."),
        ("catalogue.edit", "admin.settings"),
    ),
    Rubric(
        "enrichissement",
        _("Enrichissement et couvertures"),
        "/parametres/enrichissement",
        "enrich",
        _("Sources bibliographiques, ordre de recherche et couvertures."),
        ("admin.sources",),
    ),
    Rubric(
        "prets",
        _("Prêts et lecteurs"),
        "/parametres/prets",
        "loans",
        _("Durées de prêt, nombre de livres, rappels et catégories de lecteurs."),
        ("circulation.settings", "admin.settings"),
    ),
    Rubric(
        "sauvegardes",
        _("Sauvegardes"),
        "/parametres/sauvegardes",
        "archive",
        _("Créer et vérifier une sauvegarde, voir les précédentes."),
        ("admin.backups", "admin.settings"),
    ),
    Rubric(
        "utilisateurs",
        _("Utilisateurs et permissions"),
        "/parametres/utilisateurs",
        "users",
        _("Comptes, rôles et ce que chaque rôle peut faire."),
        ("admin.users", "admin.roles", "admin.settings"),
    ),
    Rubric(
        "avance",
        _("Paramètres avancés"),
        "/parametres/avance",
        "gear",
        _("Contrôle de chaque réglage, informations techniques, réglages techniques des sources."),
        ("admin.settings",),
    ),
)


def visible(auth: AuthContext) -> list[Rubric]:
    return [rubric for rubric in RUBRICS if any(auth.can(need) for need in rubric.needs)]


def allowed(auth: AuthContext, key: str) -> bool:
    return any(rubric.key == key for rubric in visible(auth))
