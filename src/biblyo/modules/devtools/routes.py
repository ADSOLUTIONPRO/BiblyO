# SPDX-License-Identifier: EUPL-1.2
"""Catalogue vivant du design system. Il n'existe pas en production."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from biblyo.core.pagination import page_numbers
from biblyo.core.permissions import require_session
from biblyo.modules.holdings import drawing
from biblyo.modules.holdings.drawing import LevelView, RoomPlan, RoomTile, SpineFacts, UnitView
from biblyo.modules.holdings.rules import Fill, GridBox

router = APIRouter()
HREF = "/_composants#c-etageres"


def development_only(request: Request) -> None:
    if request.app.state.settings.is_production:
        raise HTTPException(status_code=404)


@dataclass(frozen=True)
class _Place:
    """Un lieu fictif pour l'arbre de démonstration."""

    id: int
    parent_id: int | None
    kind: str
    name: str
    sort_order: int = 0
    capacity: int | None = None
    is_active: bool = True


def _facts(start: int, rows: list[tuple[str, str, str, str | None, str | None, int | None]]) -> list[SpineFacts]:
    return [
        SpineFacts(
            copy_id=start + index,
            position=index + 1,
            title=title,
            status=status,
            type_code=kind,
            href="/_composants#c-etageres",
            call_number=call,
            series=series,
            volume=str(index + 1) if series else None,
            pages=pages,
        )
        for index, (title, kind, status, call, series, pages) in enumerate(rows)
    ]


def shelf_demo() -> dict[str, Any]:
    """Jeu fictif : chaque état d'un dos, d'un niveau, de l'arbre et du plan. Aucune donnée réelle."""
    first = _facts(
        100,
        [
            ("Le Village des brumes", "bd", "available", "BD VIL 1", "Les Brumes", 48),
            ("La Forêt des brumes", "bd", "available", "BD VIL 1", "Les Brumes", 52),
            ("Le Lac des brumes", "bd", "in_repair", "BD VIL 1", "Les Brumes", 46),
            ("Atlas des marées", "documentaire", "available", "DOC MAR", None, 320),
            ("Petit traité du vent", "livre", "in_processing", "R VEN", None, 180),
            ("Kumo, volume un", "manga", "available", "MAN KUM 2", "Kumo", 192),
        ],
    )
    second = _facts(
        200,
        [
            ("Les Sentiers oubliés", "livre", "available", "R SEN", None, 420),
            ("Carnet du jardinier", "album", "available", "A JAR", None, 32),
            ("Revue des étoiles", "revue", "missing", "REV ETO", None, 60),
            ("Le Phare aux mouettes", "bd", "available", "BD PHA", None, 56),
        ],
    )
    third = _facts(
        300,
        [(f"Chroniques d'Orsay, tome {n}", "bd", "available", "BD ORS 3", "Chroniques d'Orsay", 54) for n in range(1, 7)],  # noqa: E501
    )
    levels = [
        LevelView(11, "Étagère 1", "/_composants#c-etageres", drawing.spines(first), Fill(6, 12)),
        LevelView(12, "Étagère 2", "/_composants#c-etageres", drawing.spines(second, hit=203), Fill(4, 4), focus=True),
        LevelView(13, "Étagère 3", "/_composants#c-etageres", drawing.spines(third), Fill(6, 4)),
        LevelView(14, "Bac 4", "/_composants#c-etageres", [], Fill(0, 20)),
    ]
    unit = UnitView(1, "Meuble BD 1", "/_composants#c-etageres", levels, code="BD1", hint="A–L")
    places = [
        _Place(1, None, "room", "Salle jeunesse"),
        _Place(2, 1, "furniture", "Meuble BD 1", 1),
        _Place(11, 2, "shelf", "Étagère 1", 1, 12),
        _Place(12, 2, "shelf", "Étagère 2", 2, 4),
        _Place(13, 2, "shelf", "Étagère 3", 3, 4),
        _Place(14, 2, "bin", "Bac 4", 4, 20),
        _Place(3, 1, "furniture", "Meuble romans", 2),
        _Place(4, 1, "virtual", "Exposition", 3),
        _Place(5, None, "room", "Réserve", 2, None, False),
    ]
    tree = drawing.tree(places, {11: 6, 12: 4, 13: 6, 4: 2}, href="/_composants?lieu={id}#c-etageres", current=12)
    plan = RoomPlan(
        1,
        "Salle jeunesse",
        [
            RoomTile(2, "Meuble BD 1", HREF, 16, [50, 100, 100, 0], GridBox(1, 1, 2, 1), "A–L", True),
            RoomTile(3, "Meuble romans", HREF, 0, [0, 0, 0], GridBox(1, 4, 3, 1), "Romans"),
            RoomTile(6, "Bacs albums", HREF, 0, [10, 0], GridBox(3, 1, 1, 2), "Albums"),
            RoomTile(7, "Présentoir", HREF, 0, [], None, "Nouveautés"),
        ],
    )
    return {"demo_unit": unit, "demo_tree": tree, "demo_room": plan}


@router.get("/_composants", include_in_schema=False, dependencies=[Depends(development_only), require_session()])
def components(request: Request) -> HTMLResponse:
    context = {"pages_short": page_numbers(1, 3), "pages_long": page_numbers(6, 12), **shelf_demo()}
    return request.app.state.templates.render(request, "devtools/pages/components.html", context)
