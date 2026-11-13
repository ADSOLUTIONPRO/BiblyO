# SPDX-License-Identifier: EUPL-1.2
"""Nom lisible d'un navigateur, d'après sa signature. Sert à l'affichage et ne fonde aucune décision."""

from __future__ import annotations

from biblyo.core.i18n import _

# L'ordre compte : Edge et Opera s'annoncent aussi comme Chrome, Chrome comme Safari, Android comme Linux.
_BROWSERS = (
    ("Edg", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("FxiOS/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("CriOS/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Android", "Android"),
    ("Windows", "Windows"),
    ("CrOS", "ChromeOS"),
    ("Macintosh", "macOS"),
    ("Linux", "Linux"),
)


def _first(marks: tuple[tuple[str, str], ...], text: str) -> str | None:
    return next((name for mark, name in marks if mark in text), None)


def describe_agent(signature: str | None) -> str:
    """Exemple : « Chrome sur macOS ». Une signature inconnue n'est jamais recopiée telle quelle."""
    text = signature or ""
    browser, system = _first(_BROWSERS, text), _first(_SYSTEMS, text)
    if browser and system:
        return _("%(browser)s sur %(system)s") % {"browser": browser, "system": system}
    return browser or _("Navigateur inconnu")
