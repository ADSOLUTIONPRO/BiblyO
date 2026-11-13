# SPDX-License-Identifier: EUPL-1.2
"""Numéros de page à afficher : la première, la dernière et les voisines de la page courante."""

from __future__ import annotations


def page_numbers(current: int, total: int, *, around: int = 1) -> list[int | None]:
    """`None` marque un saut. Liste vide s'il n'y a rien à paginer."""
    if total <= 0:
        return []
    wanted = {1, total, *range(max(1, current - around), min(total, current + around) + 1)}
    pages: list[int | None] = []
    previous = 0
    for page in sorted(wanted):
        if page - previous > 1:
            pages.append(None)
        pages.append(page)
        previous = page
    return pages
