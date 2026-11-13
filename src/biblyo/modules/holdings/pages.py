# SPDX-License-Identifier: EUPL-1.2
"""Vues prêtes à dessiner, lues dans la base : niveaux d'un meuble, plan d'une salle, arbre, exemplaires sans place."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from biblyo.modules.catalogue import search
from biblyo.modules.catalogue.models import COPY_CODE, Copy, DocumentType, Edition, Series, Work, WorkSeries
from biblyo.modules.catalogue.rules import ACTIVE_COPY_EXCLUDED, format_volume
from biblyo.modules.holdings import availability, drawing, rules
from biblyo.modules.holdings import repository as repo
from biblyo.modules.holdings.drawing import LevelView, RoomPlan, RoomTile, SpineFacts, TreeNode, UnitView
from biblyo.modules.holdings.models import Location
from biblyo.modules.holdings.rules import Fill, GridBox

#: Au-delà, un niveau se consulte dans la vue Liste : le dessin resterait lourd sans rien montrer de plus.
MAX_SPINES_DRAWN = 400
HREF = "/emplacements/{id}"
MAX_UNSHELVED = 200
MAX_FOUND = 30


def spine_facts(session: Session, location_ids: Sequence[int]) -> dict[int, list[SpineFacts]]:
    """Les exemplaires rangés de ces lieux, dans l'ordre réel, en une seule requête."""
    found_rows = session.execute(
        select(Copy.id, Copy.location_id, Copy.shelf_position, Copy.status, Copy.call_number)
        .add_columns(Work.title, DocumentType.code, Edition.page_count, Series.title)
        .add_columns(WorkSeries.volume_number, WorkSeries.volume_label)
        .add_columns(Copy.condition, Copy.barcode, Copy.version)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .join(DocumentType, DocumentType.id == Work.document_type_id)
        .outerjoin(WorkSeries, (WorkSeries.work_id == Work.id) & WorkSeries.is_primary.is_(True))
        .outerjoin(Series, Series.id == WorkSeries.series_id)
        .where(Copy.location_id.in_(list(location_ids)), Copy.shelf_position.is_not(None))
        .order_by(Copy.location_id, Copy.shelf_position)
    )
    found: dict[int, list[SpineFacts]] = defaultdict(list)
    rows = found_rows.all()
    out = repo.on_loan(session, [row[0] for row in rows])
    day = availability.today().isoformat()
    for row in rows:
        copy_id, location_id, position, status, call, title, kind, pages, series, number, label = row[:11]
        state, code, version = row[11:]
        found[location_id].append(
            SpineFacts(
                copy_id=copy_id,
                position=position,
                title=title,
                status=status,
                type_code=kind,
                href=f"/exemplaires/{copy_id}",
                call_number=call,
                pages=pages,
                series=series,
                volume=format_volume(number, label) or None if series else None,
                condition=state,
                barcode=code,
                version=version,
                due_on=out[copy_id].due_on if copy_id in out else None,
                overdue=copy_id in out and out[copy_id].due_on < day,
            )
        )
    return found


def levels_of(session: Session, furniture_id: int) -> list[Location]:
    return [item for item in repo.children(session, furniture_id) if rules.is_level(item.kind)]


def unit_view(
    session: Session,
    furniture: Location,
    *,
    focus: int | None = None,
    hit: int | None = None,
    group_series: bool = True,
    complete: bool = False,
) -> UnitView:
    """`complete` : tous les exemplaires, pour la vue Liste. Le dessin s'arrête à `MAX_SPINES_DRAWN`."""
    levels = levels_of(session, furniture.id)
    facts = spine_facts(session, [item.id for item in levels]) if levels else {}
    counts = repo.counts_by_location(session)
    views = []
    for level in levels:
        items = facts.get(level.id, [])
        drawn = items if complete else items[:MAX_SPINES_DRAWN]
        views.append(
            LevelView(
                id=level.id,
                name=level.name,
                href=HREF.format(id=level.id),
                spines=drawing.spines(drawn, hit=hit, group_series=group_series),
                fill=Fill(counts.get(level.id, 0), level.capacity),
                focus=level.id == focus,
                hidden=len(items) - len(drawn),
                version=level.version,
            )
        )
    href = HREF.format(id=furniture.id)
    return UnitView(furniture.id, furniture.name, href, views, furniture.code, furniture.content_hint)


def room_plan(session: Session, room: Location, *, current: int | None = None) -> RoomPlan:
    """Les meubles de la salle, chacun avec la miniature de ses niveaux."""
    counts = repo.counts_by_location(session)
    locations = list(session.scalars(select(Location).where(Location.parent_id == room.id)))
    furniture = sorted(
        (item for item in locations if item.kind == "furniture"), key=lambda item: (item.sort_order, item.name)
    )
    levels: dict[int, list[Location]] = defaultdict(list)
    if furniture:
        rows = session.scalars(
            select(Location)
            .where(Location.parent_id.in_([item.id for item in furniture]))
            .order_by(Location.sort_order, Location.name)
        )
        for level in rows:
            levels[level.parent_id or 0].append(level)
    tiles = []
    for item in furniture:
        own = levels[item.id]
        box = (
            GridBox(item.grid_row, item.grid_col, item.grid_width, item.grid_height)
            if item.grid_row is not None and item.grid_col is not None
            else None
        )
        tiles.append(
            RoomTile(
                id=item.id,
                name=item.name,
                href=HREF.format(id=item.id),
                count=sum(counts.get(level.id, 0) for level in own),
                levels=[Fill(counts.get(level.id, 0), level.capacity).percent for level in own],
                box=box,
                hint=item.content_hint,
                current=item.id == current,
            )
        )
    return RoomPlan(room.id, room.name, tiles)


def free_cells(plan: RoomPlan) -> list[tuple[int, int]]:
    """Cases libres du plan, deux lignes de marge comprises : on y pose le meuble choisi, sans JavaScript (D5)."""
    taken: set[tuple[int, int]] = set()
    for tile in plan.placed:
        if tile.box is not None:
            taken |= tile.box.cells()
    rows = min(rules.GRID_MAX, plan.rows + 2)
    return [(row, col) for row in range(1, rows + 1) for col in range(1, plan.columns + 1) if (row, col) not in taken]


def location_tree(session: Session, *, current: int | None = None) -> list[TreeNode]:
    locations = list(session.scalars(select(Location)))
    return drawing.tree(locations, repo.counts_by_location(session), href=HREF, current=current)


@dataclass(frozen=True)
class UnshelvedCopy:
    id: int
    barcode: str
    call_number: str | None
    title: str
    status_label: str


def unshelved(session: Session) -> list[UnshelvedCopy]:
    """Exemplaires sans place. Perdus et retirés n'en font pas partie : ce n'est pas un oubli de rangement."""
    from biblyo.modules.catalogue.rules import COPY_STATUSES

    rows = session.execute(
        select(Copy.id, COPY_CODE, Copy.call_number, Work.title, Copy.status)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .where(Copy.location_id.is_(None), Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
        .order_by(Work.title_sort, Copy.id)
        .limit(MAX_UNSHELVED)
    )
    return [
        UnshelvedCopy(copy_id, barcode, call, title, COPY_STATUSES.get(status, status))
        for copy_id, barcode, call, title, status in rows
    ]


#: Vues de « À ranger » : sans place, ou encore « En préparation » (rangés mais pas encore vérifiés).
TO_SHELVE_STATES = ("non_ranges", "en_preparation")
TO_SHELVE_PER_PAGE = 100


@dataclass(frozen=True)
class ShelveItem:
    """Un exemplaire à ranger ou à vérifier, avec ce qui aide à le retrouver : série, tome, inventaire, cote."""

    id: int
    title: str
    code: str
    inventory: str
    has_barcode: bool
    call_number: str | None
    series: str | None
    volume: str
    path: str | None
    status: str
    status_label: str
    imported: bool


@dataclass(frozen=True)
class ShelveFilter:
    state: str = "non_ranges"
    origin: str = ""  # vide : tous ; « importes » : venus d'un import
    query: str = ""
    page: int = 1


def to_shelve(session: Session, chosen: ShelveFilter) -> tuple[list[ShelveItem], int]:
    """Exemplaires à ranger (sans place) ou à vérifier (« En préparation »), dans l'ordre des séries et des tomes.

    Perdus et retirés n'en font jamais partie. Rien n'est modifié : la page ne fait que lire."""
    from sqlalchemy import column, func, or_, table

    from biblyo.core.text import normalize
    from biblyo.modules.catalogue.rules import COPY_STATUSES

    links = table("import_links")
    imported: Any = select(column("entity_id")).select_from(links).where(column("entity_type") == "copy")
    statement = (
        select(
            Copy.id, Work.title, COPY_CODE, Copy.inventory_number, Copy.barcode, Copy.call_number, Series.title,
            WorkSeries.volume_number, WorkSeries.volume_label, Location.path, Copy.status,
            Copy.id.in_(imported),
        )
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .outerjoin(WorkSeries, (WorkSeries.work_id == Work.id) & (WorkSeries.is_primary == True))  # noqa: E712
        .outerjoin(Series, Series.id == WorkSeries.series_id)
        .outerjoin(Location, Location.id == Copy.location_id)
        .where(Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
    )  # fmt: skip
    if chosen.state == "en_preparation":
        statement = statement.where(Copy.status == "in_processing")
    else:
        statement = statement.where(Copy.location_id.is_(None))
    if chosen.origin == "importes":
        statement = statement.where(Copy.id.in_(imported))
    text = chosen.query.strip()
    if text:
        key = normalize(text)
        statement = statement.where(
            or_(
                Work.title_norm.contains(key),
                Series.title_norm.contains(key),
                Copy.inventory_number == text,
                Copy.barcode == text.upper(),
                func.lower(Copy.call_number).contains(text.lower()),
            )
        )
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = session.execute(
        statement.order_by(Series.title_sort, WorkSeries.volume_number, Work.title_sort, Copy.id)
        .limit(TO_SHELVE_PER_PAGE)
        .offset((max(chosen.page, 1) - 1) * TO_SHELVE_PER_PAGE)
    ).all()
    items = [
        ShelveItem(
            copy_id, title, code, inventory, barcode is not None, call, series, format_volume(number, label) if series
            else "", path, status, str(COPY_STATUSES.get(status, status)), bool(from_import),
        )
        for copy_id, title, code, inventory, barcode, call, series, number, label, path, status, from_import in rows
    ]  # fmt: skip
    return items, int(total)


def copies_at(session: Session, location_id: int) -> list[UnshelvedCopy]:
    """Exemplaires d'un lieu virtuel : ils y sont sans rang."""
    from biblyo.modules.catalogue.rules import COPY_STATUSES

    rows = session.execute(
        select(Copy.id, COPY_CODE, Copy.call_number, Work.title, Copy.status)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .where(Copy.location_id == location_id)
        .order_by(Work.title_sort, Copy.id)
    )
    return [
        UnshelvedCopy(copy_id, barcode, call, title, COPY_STATUSES.get(status, status))
        for copy_id, barcode, call, title, status in rows
    ]


@dataclass(frozen=True)
class Found:
    """Un exemplaire trouvé par « Localiser », et où aller pour le voir."""

    id: int
    barcode: str
    call_number: str | None
    title: str
    path: str | None
    href: str


def find_copies(session: Session, query: str) -> list[Found]:
    """« Localiser un ouvrage » : un code, sinon une cote, sinon la recherche du catalogue."""
    text = query.strip()
    if not text:
        return []
    exact = repo.find_copy_by_code(session, text)
    copy_ids = [exact.id] if exact is not None else []
    if not copy_ids:
        wanted = rules.clean_call_number(text[:rules.MAX_CALL_NUMBER]) or ""
        copy_ids = list(session.scalars(select(Copy.id).where(Copy.call_number == wanted).limit(MAX_FOUND)))
    if not copy_ids:
        works = search.match_ids(session, text, limit=MAX_FOUND)
        if works:
            statement = (
                select(Copy.id)
                .join(Edition, Edition.id == Copy.edition_id)
                .where(Edition.work_id.in_(works), Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
                .limit(MAX_FOUND)
            )
            copy_ids = list(session.scalars(statement))
    rows = session.execute(
        select(Copy.id, COPY_CODE, Copy.call_number, Work.title, Location.path, Location.id)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .outerjoin(Location, Location.id == Copy.location_id)
        .where(Copy.id.in_(copy_ids))
        .order_by(Work.title_sort, Copy.id)
    )
    return [
        Found(
            copy_id,
            barcode,
            call,
            title,
            path,
            f"/emplacements/{place}?localiser={copy_id}#dos-{copy_id}" if place else f"/exemplaires/{copy_id}",
        )
        for copy_id, barcode, call, title, path, place in rows
    ]


def parent_choices(session: Session, kind: str, *, exclude: int | None = None) -> list[tuple[str, str]]:
    """Les lieux qui peuvent accueillir un lieu de ce type. Exclut le lieu lui-même et ses descendants."""
    excluded = set(repo.descendant_ids(session, exclude)) if exclude is not None else set()
    allowed = rules.PARENT_KINDS.get(kind, ())
    rows = session.scalars(select(Location).order_by(Location.path))
    choices = [(str(item.id), item.path) for item in rows if item.kind in allowed and item.id not in excluded]
    return [("", _root_label()), *choices] if None in allowed else choices


def _root_label() -> str:
    from biblyo.core.i18n import _

    return _("À la racine")


def titles_of(session: Session, copy_ids: Sequence[int]) -> list[repo.CopyBrief]:
    """Exemplaires désignés, dans l'ordre donné : l'aperçu d'un tri montre la suite réelle des titres."""
    found = repo.briefs(session, copy_ids)
    return [found[item] for item in copy_ids if item in found]
