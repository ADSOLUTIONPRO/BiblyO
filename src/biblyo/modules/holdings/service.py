# SPDX-License-Identifier: EUPL-1.2
"""Écritures des emplacements et des exemplaires (lot L2, décisions D1 à D13).

**Une seule voie pour ranger** : `shelve`. Le formulaire, le clavier, le glisser-déposer et le scan l'appellent tous.
Chaque écriture est une transaction `BEGIN IMMEDIATE` : deux rangements simultanés se suivent, jamais ne se mêlent.
Les rangs d'un niveau restent continus (1 à n) : ils sont reposés en deux temps, pour que l'index unique
(niveau, rang) ne soit jamais violé en cours de route. **Rien n'est écrasé en silence** : un rang qui a changé entre
l'affichage et la demande, un code déjà attribué, une place occupée sur le plan sont refusés avec un message.

Un déplacement ne touche ni l'œuvre ni l'édition : ni leur version, ni leur date de modification, ni la cote (D2).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _, ngettext
from biblyo.core.ids import new_public_id
from biblyo.modules.catalogue import search
from biblyo.modules.catalogue.models import Copy, Edition
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.holdings import repository as repo
from biblyo.modules.holdings import rules
from biblyo.modules.holdings.models import Location, RetiredCode
from biblyo.modules.holdings.rules import Fill, GridBox, LocationError

#: Décalage des rangs pendant une renumérotation : au-delà de toute capacité réelle.
OFFSET = 1_000_000
MAX_COPIES_PER_MOVE = 200
MAX_LEVELS_AT_ONCE = 12
MAX_NOTE = 2000
MAX_SHORT = 200
MAX_HINT = 80
ROOM_COLUMNS = 8  # largeur du plan proposée pour placer un nouveau meuble ; la grille va jusqu'à 40
FURNITURE_BOX = (2, 1)  # largeur et hauteur d'un nouveau meuble sur le plan
#: Statuts qu'une annulation de rangement peut rendre : ceux qu'un rangement accepte.
UNDOABLE_STATUSES = frozenset({"in_processing", "available", "in_repair"})

LOCATION_CONFLICT = _(
    "Cet emplacement vient d’être modifié par une autre personne. Rien n’a été enregistré : rechargez la page, "
    "puis recommencez."
)
COPY_CONFLICT = _(
    "Cet exemplaire vient d’être modifié ou déplacé par une autre personne. Rien n’a été enregistré : rechargez la "
    "page, puis recommencez."
)
LEVEL_CHANGED = _(
    "L’étagère a changé depuis son affichage. Rien n’a été enregistré : rechargez la page, puis recommencez."
)


@contextmanager
def _business_rules() -> Iterator[None]:
    """Une règle pure refusée devient un refus métier : la transaction est annulée, le message est montré."""
    try:
        yield
    except LocationError as error:
        raise BusinessError(str(error)) from error


def _text(value: str | None, limit: int, field_name: str, errors: dict[str, str]) -> str | None:
    text = (value or "").strip()
    if len(text) > limit:
        errors[field_name] = _("Ce texte est trop long.")
    return text or None


def _line(value: str | None, limit: int, field_name: str, errors: dict[str, str]) -> str | None:
    return _text(" ".join((value or "").split()), limit, field_name, errors)


# --- Emplacements -----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LocationInput:
    name: str
    code: str = ""
    capacity: str = ""
    content_hint: str = ""
    notes: str = ""


def _get_location(session: Session, location_id: int | None) -> Location:
    location = session.get(Location, location_id) if location_id is not None else None
    if location is None:
        raise BusinessError(_("Cet emplacement n’existe plus."))
    return location


def _check_location_version(location: Location, expected: int | None) -> None:
    if expected is not None and location.version != expected:
        raise ConflictError(LOCATION_CONFLICT)


def _touch_location(location: Location, moment: str | None = None) -> None:
    location.version += 1
    location.updated_at = moment or now_iso()


def _clean_location(
    session: Session, kind: str, parent_id: int | None, data: LocationInput, exclude: int | None = None
) -> dict[str, object]:
    errors: dict[str, str] = {}
    values: dict[str, object] = {}
    try:
        values["name"] = name = rules.clean_name(data.name)
    except LocationError as error:
        errors["nom"] = str(error)
    else:
        if repo.sibling_name_taken(session, parent_id, name, exclude):
            errors["nom"] = _("Un emplacement porte déjà ce nom à cet endroit.")
    try:
        values["code"] = code = rules.clean_location_code(data.code)
    except LocationError as error:
        errors["code"] = str(error)
    else:
        if code is not None:
            holder = session.scalar(select(Location.path).where(Location.code == code, Location.id != (exclude or 0)))
            if holder is not None:
                errors["code"] = _("Ce code court est déjà celui de « {path} ».").format(path=holder)
    try:
        values["capacity"] = capacity = rules.clean_capacity(data.capacity)
    except LocationError as error:
        errors["capacite"] = str(error)
    else:
        if capacity is not None and not rules.is_level(kind):
            errors["capacite"] = _("La capacité se donne pour une étagère ou un bac.")
    values["content_hint"] = _line(data.content_hint, MAX_HINT, "indication", errors)
    values["notes"] = _text(data.notes, MAX_NOTE, "notes", errors)
    if errors:
        raise FormErrors(errors)
    return values


def _path(parent: Location | None, name: str) -> str:
    return rules.path_of([parent.path, name]) if parent is not None else name


def _next_sort_order(session: Session, parent_id: int | None) -> int:
    statement = select(func.max(Location.sort_order)).where(
        Location.parent_id == parent_id if parent_id is not None else Location.parent_id.is_(None)
    )
    return (session.scalar(statement) or 0) + 1


def _active_chain(session: Session, location: Location) -> bool:
    return all(item.is_active for item in repo.ancestors(session, location.id))


def _grid_of(location: Location) -> GridBox | None:
    if location.grid_row is None or location.grid_col is None:
        return None
    return GridBox(location.grid_row, location.grid_col, location.grid_width, location.grid_height)


def _furniture_boxes(session: Session, room_id: int, exclude: int | None) -> list[tuple[Location, GridBox]]:
    found = []
    for item in repo.children(session, room_id):
        box = _grid_of(item)
        if item.kind == "furniture" and item.id != exclude and box is not None:
            found.append((item, box))
    return found


def _free_box(session: Session, room_id: int, exclude: int | None = None) -> GridBox:
    """Première place libre du plan, ligne par ligne : un nouveau meuble est toujours visible sur le plan (D5)."""
    taken = [box for _item, box in _furniture_boxes(session, room_id, exclude)]
    width, height = FURNITURE_BOX
    for row in range(1, rules.GRID_MAX - height + 2):
        for col in range(1, ROOM_COLUMNS - width + 2):
            candidate = GridBox(row, col, width, height)
            if not rules.grid_overlaps(candidate, taken):
                return candidate
    raise BusinessError(_("Le plan de cette salle est plein : déplacez d’abord un meuble."))


def _check_box(session: Session, room_id: int, box: GridBox, exclude: int | None) -> None:
    rules.check_grid_box(box)
    for item, other in _furniture_boxes(session, room_id, exclude):
        if rules.grid_overlaps(box, [other]):
            raise BusinessError(_("Cette place est déjà occupée par « {name} ».").format(name=item.name))


def _set_box(location: Location, box: GridBox) -> None:
    location.grid_row, location.grid_col = box.row, box.col
    location.grid_width, location.grid_height = box.width, box.height


def _refresh_subtree(session: Session, root: Location) -> None:
    """Chemins et profondeurs des descendants, après un renommage ou un déplacement. Les exemplaires ne bougent pas."""
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        for child in repo.children(session, parent.id):
            child.path = _path(parent, child.name)
            child.depth = rules.depth_for(parent.depth)
            frontier.append(child)


def create_location(
    database: Database,
    kind: str,
    parent_id: int | None,
    data: LocationInput,
    user_id: int | None,
    *,
    grid: GridBox | None = None,
) -> int:
    with database.session(write=True) as session, _business_rules():
        parent = _get_location(session, parent_id) if parent_id is not None else None
        rules.check_parent(kind, parent.kind if parent else None)
        if parent is not None and not _active_chain(session, parent):
            raise BusinessError(_("Ce lieu est désactivé : réactivez-le d’abord."))
        depth = rules.depth_for(parent.depth if parent else None)
        values = _clean_location(session, kind, parent_id, data)
        moment = now_iso()
        location = Location(
            public_id=new_public_id(),
            parent_id=parent_id,
            kind=kind,
            path=_path(parent, str(values["name"])),
            depth=depth,
            sort_order=_next_sort_order(session, parent_id),
            created_at=moment,
            updated_at=moment,
            **values,
        )
        if kind == "furniture":
            assert parent is not None  # check_parent : un meuble est dans une salle
            box = grid or _free_box(session, parent.id)
            _check_box(session, parent.id, box, exclude=None)
            _set_box(location, box)
        elif grid is not None:
            raise BusinessError(_("Seul un meuble se place sur le plan d’une salle."))
        session.add(location)
        session.flush()
        audit.record(
            session,
            "location.create",
            "location",
            location.id,
            user_id=user_id,
            details={"type": kind, "chemin": location.path},
        )
        return location.id


def create_levels(database: Database, furniture_id: int, count: int, capacity: str, user_id: int | None) -> list[int]:
    """Ajoute d'un coup les niveaux d'un meuble, « Étagère N » à la suite des niveaux existants."""
    if not 1 <= count <= MAX_LEVELS_AT_ONCE:
        raise FormErrors({"niveaux": _("Indiquez entre 1 et 12 niveaux.")})
    with database.session(write=True) as session, _business_rules():
        furniture = _get_location(session, furniture_id)
        rules.check_parent("shelf", furniture.kind)
        if not _active_chain(session, furniture):
            raise BusinessError(_("Ce lieu est désactivé : réactivez-le d’abord."))
        moment = now_iso()
        created: list[int] = []
        number = len(repo.children(session, furniture.id))
        for _index in range(count):
            number += 1
            while repo.sibling_name_taken(session, furniture.id, f"Étagère {number}"):
                number += 1
            name = f"Étagère {number}"
            values = _clean_location(session, "shelf", furniture.id, LocationInput(name, capacity=capacity))
            level = Location(
                public_id=new_public_id(),
                parent_id=furniture.id,
                kind="shelf",
                path=_path(furniture, name),
                depth=rules.depth_for(furniture.depth),
                sort_order=_next_sort_order(session, furniture.id),
                created_at=moment,
                updated_at=moment,
                **values,
            )
            session.add(level)
            session.flush()
            created.append(level.id)
        audit.record(
            session,
            "location.create_levels",
            "location",
            furniture.id,
            user_id=user_id,
            details={"niveaux": count, "chemin": furniture.path},
        )
        return created


def update_location(
    database: Database, location_id: int, data: LocationInput, expected_version: int | None, user_id: int | None
) -> None:
    """Renommer, changer le code court, la capacité, l'indication de contenu. Une capacité réduite sous le nombre
    d'exemplaires présents est permise : le dépassement est signalé, jamais bloquant (D1)."""
    with database.session(write=True) as session, _business_rules():
        location = _get_location(session, location_id)
        _check_location_version(location, expected_version)
        values = _clean_location(session, location.kind, location.parent_id, data, exclude=location.id)
        changed = [key for key, value in values.items() if getattr(location, key) != value]
        if not changed:
            return
        renamed = values["name"] != location.name
        for key, value in values.items():
            setattr(location, key, value)
        if renamed:
            parent = session.get(Location, location.parent_id) if location.parent_id else None
            location.path = _path(parent, location.name)
            _refresh_subtree(session, location)
        _touch_location(location)
        audit.record(session, "location.update", "location", location.id, user_id=user_id, details={"champs": changed})


def move_location(
    database: Database, location_id: int, parent_id: int | None, expected_version: int | None, user_id: int | None
) -> None:
    """Change un lieu de parent, avec tout ce qu'il contient. Cotes et rangs des exemplaires restent les mêmes."""
    with database.session(write=True) as session, _business_rules():
        location = _get_location(session, location_id)
        _check_location_version(location, expected_version)
        if parent_id == location.parent_id:
            return
        parent = _get_location(session, parent_id) if parent_id is not None else None
        rules.check_no_cycle(location.id, [item.id for item in repo.ancestors(session, parent_id)])
        rules.check_parent(location.kind, parent.kind if parent else None)
        if parent is not None and not _active_chain(session, parent):
            raise BusinessError(_("Ce lieu est désactivé : réactivez-le d’abord."))
        depth = rules.depth_for(parent.depth if parent else None)
        subtree = repo.descendant_ids(session, location.id)
        deepest = session.scalar(select(func.max(Location.depth)).where(Location.id.in_(subtree))) or location.depth
        if deepest - location.depth + depth > rules.MAX_DEPTH:
            raise BusinessError(_("La hiérarchie est trop profonde."))
        if repo.sibling_name_taken(session, parent_id, location.name, exclude=location.id):
            raise BusinessError(_("Un emplacement porte déjà ce nom à cet endroit : renommez-le d’abord."))
        before = location.path
        location.parent_id = parent_id
        location.depth = depth
        location.path = _path(parent, location.name)
        if location.kind == "furniture":
            assert parent is not None
            _set_box(location, _free_box(session, parent.id, exclude=location.id))
        _refresh_subtree(session, location)
        _touch_location(location)
        audit.record(
            session,
            "location.move",
            "location",
            location.id,
            user_id=user_id,
            details={"de": before, "vers": location.path},
        )


def place_furniture(
    database: Database, location_id: int, box: GridBox, expected_version: int | None, user_id: int | None
) -> None:
    """Pose un meuble sur le plan de sa salle (D5). Une place occupée est refusée, jamais partagée."""
    with database.session(write=True) as session, _business_rules():
        location = _get_location(session, location_id)
        _check_location_version(location, expected_version)
        if location.kind != "furniture" or location.parent_id is None:
            raise BusinessError(_("Seul un meuble se place sur le plan d’une salle."))
        if _grid_of(location) == box:
            return
        _check_box(session, location.parent_id, box, exclude=location.id)
        _set_box(location, box)
        _touch_location(location)
        audit.record(
            session,
            "location.place",
            "location",
            location.id,
            user_id=user_id,
            details={"ligne": box.row, "colonne": box.col, "largeur": box.width, "hauteur": box.height},
        )


def reorder_children(
    database: Database, parent_id: int, ordered_ids: Sequence[int], expected_version: int | None, user_id: int | None
) -> None:
    """Ordre des niveaux d'un meuble (de haut en bas) ou des meubles d'une salle."""
    with database.session(write=True) as session:
        parent = _get_location(session, parent_id)
        _check_location_version(parent, expected_version)
        current = repo.children(session, parent.id)
        if len(set(ordered_ids)) != len(ordered_ids) or {item.id for item in current} != set(ordered_ids):
            raise ConflictError(LOCATION_CONFLICT)
        by_id = {item.id: item for item in current}
        for rank, item_id in enumerate(ordered_ids, start=1):
            by_id[item_id].sort_order = rank
        _touch_location(parent)
        audit.record(session, "location.reorder_children", "location", parent.id, user_id=user_id)


def set_location_active(
    database: Database, location_id: int, active: bool, expected_version: int | None, user_id: int | None
) -> None:
    """Désactiver un lieu désactive tout ce qu'il contient, à condition qu'il soit vide d'exemplaires."""
    with database.session(write=True) as session:
        location = _get_location(session, location_id)
        _check_location_version(location, expected_version)
        subtree = repo.descendant_ids(session, location.id)
        if not active:
            count = repo.copies_in(session, subtree)
            if count:
                raise BusinessError(
                    ngettext(
                        "Ce lieu contient encore {n} exemplaire : déplacez-le d’abord.",
                        "Ce lieu contient encore {n} exemplaires : déplacez-les d’abord.",
                        count,
                    ).format(n=count)
                )
        elif location.parent_id is not None and not _active_chain(session, _get_location(session, location.parent_id)):
            raise BusinessError(_("Le lieu parent est désactivé : réactivez-le d’abord."))
        moment = now_iso()
        for item in session.scalars(select(Location).where(Location.id.in_(subtree))):
            if item.is_active != active:
                item.is_active = active
                _touch_location(item, moment)
        audit.record(
            session,
            "location.activate" if active else "location.deactivate",
            "location",
            location.id,
            user_id=user_id,
            details={"chemin": location.path, "lieux": len(subtree)},
        )


def delete_location(
    database: Database,
    location_id: int,
    expected_version: int | None,
    user_id: int | None,
    *,
    with_children: bool = False,
) -> int | None:
    """Supprime un lieu vide. Ses sous-lieux vides ne partent avec lui que sur confirmation explicite.

    Renvoie le parent, pour y revenir.
    """
    with database.session(write=True) as session:
        location = _get_location(session, location_id)
        _check_location_version(location, expected_version)
        subtree = repo.descendant_ids(session, location.id)
        count = repo.copies_in(session, subtree)
        if count:
            raise BusinessError(
                ngettext(
                    "« {name} » contient encore {n} exemplaire : déplacez-le ou retirez-le d’abord.",
                    "« {name} » contient encore {n} exemplaires : déplacez-les ou retirez-les d’abord.",
                    count,
                ).format(name=location.name, n=count)
            )
        children = len(subtree) - 1
        if children and not with_children:
            raise BusinessError(
                ngettext(
                    "« {name} » contient encore {n} emplacement : supprimez-le d’abord, ou confirmez la suppression "
                    "de l’ensemble.",
                    "« {name} » contient encore {n} emplacements : supprimez-les d’abord, ou confirmez la "
                    "suppression de l’ensemble.",
                    children,
                ).format(name=location.name, n=children)
            )
        audit.record(
            session,
            "location.delete",
            "location",
            location.id,
            user_id=user_id,
            details={"chemin": location.path, "sous_emplacements": children},
        )
        doomed = list(session.scalars(select(Location).where(Location.id.in_(subtree)).order_by(Location.depth.desc())))
        for item in doomed:
            session.delete(item)
            session.flush()  # les plus profonds d'abord : la clé étrangère vers le parent est RESTRICT
        return location.parent_id


# --- Rangement : la voie unique ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Shelving:
    """Ce qu'un rangement a fait, et ce qu'il faut dire à la personne qui l'a demandé."""

    location_id: int
    path: str
    copy_ids: tuple[int, ...]
    positions: dict[int, int | None]
    previous: dict[int, tuple[int | None, int | None]]
    statuses: dict[int, tuple[str, str]]
    fill: Fill
    location_version: int
    shifted: int
    already_there: bool = False
    changed: bool = True  # faux : déjà à cette place, rien n'a été écrit (A14)


def _get_copy(session: Session, copy_id: int) -> Copy:
    copy = session.get(Copy, copy_id)
    if copy is None:
        raise BusinessError(_("Cet exemplaire n’existe plus."))
    return copy


def _check_copy_version(copy: Copy, expected: int | None) -> None:
    if expected is not None and copy.version != expected:
        raise ConflictError(COPY_CONFLICT)


def _touch_copy(copy: Copy, user_id: int | None, moment: str | None = None) -> None:
    copy.version += 1
    copy.updated_at = moment or now_iso()
    copy.updated_by = user_id


def _level_copies(session: Session, location_id: int) -> list[Copy]:
    statement = (
        select(Copy)
        .where(Copy.location_id == location_id, Copy.shelf_position.is_not(None))
        .order_by(Copy.shelf_position)
    )
    return list(session.scalars(statement))


def _renumber(session: Session, orders: Mapping[int, Sequence[Copy]]) -> None:
    """Pose les rangs 1 à n de chaque niveau, en deux temps : d'abord tous décalés, puis tous à leur place.

    `orders` doit contenir **tous** les exemplaires rangés des niveaux concernés.
    """
    for order in orders.values():
        for copy in order:
            if copy.shelf_position is not None:
                copy.shelf_position += OFFSET
    session.flush()
    for location_id, order in orders.items():
        for rank, copy in enumerate(order, start=1):
            copy.location_id = location_id
            copy.shelf_position = rank
    session.flush()


def _check_target(session: Session, target: Location) -> None:
    if not rules.accepts_copies(target.kind):
        message = _("Un exemplaire se range sur une étagère, dans un bac ou dans un lieu virtuel, pas dans « {name} ».")
        raise BusinessError(message.format(name=target.name))
    if not _active_chain(session, target):
        raise BusinessError(_("Cet emplacement est désactivé : on ne peut plus y ranger."))


def _refuse_on_loan(session: Session, copies: Sequence[Copy], message: str) -> None:
    """Un exemplaire sorti garde sa place et son statut jusqu'à son retour (D10). Table du module des prêts."""
    ids = [copy.id for copy in copies]
    if not ids:
        return
    query = text("SELECT 1 FROM loans WHERE copy_id IN :ids AND returned_at IS NULL AND cancelled_at IS NULL LIMIT 1")
    if session.execute(query.bindparams(bindparam("ids", expanding=True)), {"ids": ids}).first():
        raise BusinessError(message)


ON_LOAN_KEEPS_PLACE = _("Cet exemplaire est en prêt : il garde sa place jusqu’à son retour.")
ON_LOAN_RETURN_FIRST = _("Cet exemplaire est en prêt : enregistrez d’abord son retour.")


def _named(brief: repo.CopyBrief) -> str:
    return _("« {title} » ({code})").format(title=brief.title, code=brief.barcode)


def _load_copies(session: Session, copy_ids: Sequence[int]) -> list[Copy]:
    ids = list(copy_ids)
    if not ids:
        raise BusinessError(_("Choisissez au moins un exemplaire."))
    if len(ids) > MAX_COPIES_PER_MOVE:
        raise BusinessError(_("Déplacez au plus 200 exemplaires à la fois."))
    if len(set(ids)) != len(ids):
        raise BusinessError(_("Un exemplaire figure deux fois dans la sélection."))
    return [_get_copy(session, copy_id) for copy_id in ids]


def _paths(session: Session, location_ids: set[int | None]) -> dict[int, str]:
    ids = [item for item in location_ids if item is not None]
    if not ids:
        return {}
    return dict(session.execute(select(Location.id, Location.path).where(Location.id.in_(ids))).all())


def _shelve(
    session: Session,
    copy_ids: Sequence[int],
    location_id: int,
    *,
    position: int | None,
    expected_versions: Mapping[int, int] | None,
    expected_location_version: int | None,
    keep_processing: bool,
    user_id: int | None,
    action: str = "copy.shelve",
    before: int | None = None,
) -> Shelving:
    target = _get_location(session, location_id)
    _check_target(session, target)
    _check_location_version(target, expected_location_version)
    copies = _load_copies(session, copy_ids)
    _refuse_on_loan(session, copies, ON_LOAN_KEEPS_PLACE)
    expected = expected_versions or {}
    for copy in copies:
        _check_copy_version(copy, expected.get(copy.id))
    on_level = rules.is_level(target.kind)
    if position is not None and not on_level:
        raise BusinessError(_("Un lieu virtuel n’a pas de rang : rangez sans position."))
    if before is not None:
        if not on_level:
            raise BusinessError(_("Un lieu virtuel n’a pas de rang : rangez sans position."))
        here = [copy.shelf_position for copy in copies if copy.location_id == target.id and copy.shelf_position]
        position = rules.rank_after_removal(before, here)
    if _already_in_place(session, target, copies, position, on_level):
        return _unchanged(session, target, copies)
    new_status: dict[int, str] = {}
    for copy in copies:
        try:
            new_status[copy.id] = rules.status_after_shelving(
                copy.status, first_time=copy.first_shelved_at is None, keep_processing=keep_processing
            )
        except LocationError as error:
            brief = repo.briefs(session, [copy.id])[copy.id]
            raise BusinessError(f"{_named(brief)} : {error}") from error

    previous = {copy.id: (copy.location_id, copy.shelf_position) for copy in copies}
    moving = {copy.id for copy in copies}
    sources = {copy.location_id for copy in copies if copy.shelf_position is not None} - {target.id}
    orders: dict[int, list[Copy]] = {
        source: [item for item in _level_copies(session, source) if item.id not in moving]
        for source in sources
        if source is not None
    }
    shifted = 0
    if on_level:
        present = _level_copies(session, target.id)
        by_id = {item.id: item for item in present} | {copy.id: copy for copy in copies}
        old_ranks = {item.id: item.shelf_position for item in present if item.id not in moving}
        new_order = rules.insert([item.id for item in present], [copy.id for copy in copies], position)
        orders[target.id] = [by_id[item_id] for item_id in new_order]
        shifted = sum(1 for rank, item_id in enumerate(new_order, start=1) if old_ranks.get(item_id, rank) != rank)
    else:
        for copy in copies:  # hors des niveaux décalés : sa place est libérée avant la renumérotation
            copy.location_id, copy.shelf_position = target.id, None
    _renumber(session, orders)

    moment = now_iso()
    statuses: dict[int, tuple[str, str]] = {}
    status_before = {copy.id: copy.status for copy in copies}
    for copy in copies:
        if copy.status != new_status[copy.id]:
            statuses[copy.id] = (copy.status, new_status[copy.id])
            copy.status = new_status[copy.id]
        if copy.first_shelved_at is None:
            copy.first_shelved_at = moment
        _touch_copy(copy, user_id, moment)
    touched = {source for source, _rank in previous.values() if source is not None} | {target.id}
    for location in session.scalars(select(Location).where(Location.id.in_(list(touched)))):
        _touch_location(location, moment)
    session.flush()
    paths = _paths(session, {source for source, _rank in previous.values()})
    for copy in copies:
        source, rank = previous[copy.id]
        details: dict[str, object] = {
            "de": paths.get(source) if source else None,
            "de_rang": rank,
            "vers": target.path,
            "rang": copy.shelf_position,
            "de_id": source,  # de quoi annuler sans croire ce que le navigateur renverrait
            "statut_avant": status_before[copy.id],
            "version": copy.version,
        }
        if copy.id in statuses:
            details["statut"] = statuses[copy.id][1]
        audit.record(session, action, "copy", copy.id, user_id=user_id, details=details)
    return Shelving(
        location_id=target.id,
        path=target.path,
        copy_ids=tuple(copy.id for copy in copies),
        positions={copy.id: copy.shelf_position for copy in copies},
        previous=previous,
        statuses=statuses,
        fill=repo.fill_of(session, target),
        location_version=target.version,
        shifted=shifted,
    )


def _already_in_place(
    session: Session, target: Location, copies: Sequence[Copy], position: int | None, on_level: bool
) -> bool:
    """Vrai si la demande laisse chaque exemplaire exactement où il est : même lieu, même rang pour tous (A14)."""
    if any(copy.location_id != target.id for copy in copies):
        return False
    if not on_level:
        return True
    current = [item.id for item in _level_copies(session, target.id)]
    return rules.insert(current, [copy.id for copy in copies], position) == current


def _unchanged(session: Session, target: Location, copies: Sequence[Copy]) -> Shelving:
    """Une demande sans effet n'écrit rien : ni rang, ni version, ni date, ni journal."""
    places = {copy.id: (copy.location_id, copy.shelf_position) for copy in copies}
    return Shelving(
        location_id=target.id,
        path=target.path,
        copy_ids=tuple(copy.id for copy in copies),
        positions={copy.id: copy.shelf_position for copy in copies},
        previous=places,
        statuses={},
        fill=repo.fill_of(session, target),
        location_version=target.version,
        shifted=0,
        already_there=True,
        changed=False,
    )


def shelve(
    database: Database,
    copy_ids: Sequence[int],
    location_id: int,
    *,
    position: int | None = None,
    expected_versions: Mapping[int, int] | None = None,
    expected_location_version: int | None = None,
    keep_processing: bool = False,
    user_id: int | None,
    before: int | None = None,
) -> Shelving:
    """Range ou déplace des exemplaires, dans l'ordre donné, à partir du rang `position` (à la fin sinon).

    `before` : à la place du dos affiché au rang N. Au rangement à l'écran, on désigne un dos, pas un rang final.

    Les versions attendues lient la demande à ce qui a été affiché : un exemplaire ou une étagère qui a changé
    entre-temps fait refuser le tout. Le premier rangement fait passer « En préparation » à « En place », sauf
    `keep_processing` ; les autres statuts sont conservés ; un exemplaire introuvable, perdu ou retiré est refusé (D7).
    La capacité n'est jamais une raison de refuser : le résultat dit si elle est dépassée (D1).
    """
    with database.session(write=True) as session, _business_rules():
        return _shelve(
            session,
            copy_ids,
            location_id,
            position=position,
            expected_versions=expected_versions,
            expected_location_version=expected_location_version,
            keep_processing=keep_processing,
            user_id=user_id,
            before=before,
        )


def _release(session: Session, copies: Sequence[Copy], moment: str) -> set[int]:
    """Retire des exemplaires de leur place et resserre les niveaux qu'ils quittent. Renvoie les lieux touchés."""
    leaving = {copy.id for copy in copies}
    sources = {copy.location_id for copy in copies if copy.location_id is not None}
    levels = {copy.location_id for copy in copies if copy.shelf_position is not None}
    orders = {
        level: [item for item in _level_copies(session, level) if item.id not in leaving]
        for level in levels
        if level is not None
    }
    for copy in copies:
        copy.location_id, copy.shelf_position = None, None
    _renumber(session, orders)
    for location in session.scalars(select(Location).where(Location.id.in_(list(sources)))):
        _touch_location(location, moment)
    return {item for item in sources if item is not None}


def unshelve(
    database: Database,
    copy_ids: Sequence[int],
    *,
    expected_versions: Mapping[int, int] | None = None,
    user_id: int | None,
) -> None:
    """« Non rangé » : l'exemplaire quitte sa place, garde son statut. Ses voisins se resserrent."""
    with database.session(write=True) as session:
        copies = _load_copies(session, copy_ids)
        _refuse_on_loan(session, copies, ON_LOAN_KEEPS_PLACE)
        expected = expected_versions or {}
        for copy in copies:
            _check_copy_version(copy, expected.get(copy.id))
        previous = {copy.id: (copy.location_id, copy.shelf_position) for copy in copies}
        paths = _paths(session, {source for source, _rank in previous.values()})
        moment = now_iso()
        _release(session, copies, moment)
        for copy in copies:
            source, rank = previous[copy.id]
            if source is None:
                continue
            _touch_copy(copy, user_id, moment)
            audit.record(
                session,
                "copy.unshelve",
                "copy",
                copy.id,
                user_id=user_id,
                details={"de": paths.get(source), "de_rang": rank},
            )


def reorder(
    database: Database,
    location_id: int,
    ordered_ids: Sequence[int],
    expected_location_version: int | None,
    user_id: int | None,
    *,
    action: str = "location.reorder",
    details: Mapping[str, object] | None = None,
) -> int:
    """Remet un niveau dans l'ordre donné, qui doit contenir exactement ses exemplaires. Renvoie le nombre déplacé."""
    with database.session(write=True) as session:
        return _reorder(session, location_id, ordered_ids, expected_location_version, user_id, action, details)


def _reorder(
    session: Session,
    location_id: int,
    ordered_ids: Sequence[int],
    expected_location_version: int | None,
    user_id: int | None,
    action: str,
    details: Mapping[str, object] | None,
) -> int:
    location = _get_location(session, location_id)
    if not rules.is_level(location.kind):
        raise BusinessError(_("Seuls les exemplaires d’une étagère ou d’un bac ont un ordre."))
    _check_location_version(location, expected_location_version)
    present = _level_copies(session, location.id)
    if len(set(ordered_ids)) != len(ordered_ids) or {item.id for item in present} != set(ordered_ids):
        raise ConflictError(LEVEL_CHANGED)
    moved = rules.moved_count([item.id for item in present], list(ordered_ids))
    if not moved:
        return 0
    by_id = {item.id: item for item in present}
    _renumber(session, {location.id: [by_id[item_id] for item_id in ordered_ids]})
    _touch_location(location)
    audit.record(
        session, action, "location", location.id, user_id=user_id, details={**(details or {}), "deplaces": moved}
    )
    return moved


@dataclass(frozen=True)
class SortPreview:
    location_id: int
    mode: str
    before: list[int]
    after: list[int]
    moved: int
    location_version: int


def _sort_preview(session: Session, location: Location, mode: str) -> SortPreview:
    if mode not in rules.SORTS:
        raise BusinessError(_("Ce tri n’existe pas."))
    before = repo.shelf_order(session, location.id)
    after = rules.sorted_order(repo.sort_facts(session, before), mode)
    return SortPreview(location.id, mode, before, after, rules.moved_count(before, after), location.version)


def sort_preview(database: Database, location_id: int, mode: str) -> SortPreview:
    """Ce que ferait un tri automatique : l'ordre proposé et le nombre d'exemplaires qui changeraient de rang."""
    with database.session() as session:
        location = _get_location(session, location_id)
        if not rules.is_level(location.kind):
            raise BusinessError(_("Seuls les exemplaires d’une étagère ou d’un bac ont un ordre."))
        return _sort_preview(session, location, mode)


def apply_sort(
    database: Database, location_id: int, mode: str, expected_location_version: int | None, user_id: int | None
) -> int:
    """Applique le tri tel qu'il a été prévisualisé : refusé si l'étagère a changé depuis l'aperçu."""
    with database.session(write=True) as session:
        location = _get_location(session, location_id)
        _check_location_version(location, expected_location_version)
        preview = _sort_preview(session, location, mode)
        return _reorder(
            session, location.id, preview.after, expected_location_version, user_id, "location.sort", {"tri": mode}
        )


# --- Aperçu d'un déplacement groupé -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class PlannedCopy:
    brief: repo.CopyBrief
    version: int
    from_path: str | None
    from_position: int | None
    status_after: str | None
    refusal: str | None
    already_there: bool


@dataclass(frozen=True)
class ShelvingPlan:
    """De quoi confirmer un déplacement : qui part d'où, ce qui sera refusé, la capacité après coup."""

    location_id: int
    path: str
    location_version: int
    copies: list[PlannedCopy]
    fill_after: Fill
    shifted: int
    refusals: list[str] = field(default_factory=list)
    unchanged: bool = False  # tous déjà à cette place, dans cet ordre : la confirmation n'écrirait rien

    @property
    def expected_versions(self) -> dict[int, int]:
        return {item.brief.id: item.version for item in self.copies}


def plan_shelving(
    database: Database,
    copy_ids: Sequence[int],
    location_id: int,
    *,
    position: int | None = None,
    keep_processing: bool = False,
) -> ShelvingPlan:
    with database.session() as session, _business_rules():
        target = _get_location(session, location_id)
        _check_target(session, target)
        copies = _load_copies(session, copy_ids)
        found = repo.briefs(session, [copy.id for copy in copies])
        paths = _paths(session, {copy.location_id for copy in copies})
        planned: list[PlannedCopy] = []
        refusals: list[str] = []
        for copy in copies:
            refusal = status = None
            try:
                status = rules.status_after_shelving(
                    copy.status, first_time=copy.first_shelved_at is None, keep_processing=keep_processing
                )
            except LocationError as error:
                refusal = str(error)
                refusals.append(f"{_named(found[copy.id])} : {error}")
            planned.append(
                PlannedCopy(
                    brief=found[copy.id],
                    version=copy.version,
                    from_path=paths.get(copy.location_id) if copy.location_id else None,
                    from_position=copy.shelf_position,
                    status_after=status,
                    refusal=refusal,
                    already_there=copy.location_id == target.id,
                )
            )
        present = repo.shelf_order(session, target.id) if rules.is_level(target.kind) else []
        incoming = [copy.id for copy in copies]
        count = repo.copies_in(session, [target.id]) + sum(1 for copy in copies if copy.location_id != target.id)
        ranks = {item: rank for rank, item in enumerate(present, start=1) if item not in set(incoming)}
        after = rules.insert(present, incoming, position) if present else []
        shifted = sum(1 for rank, item in enumerate(after, start=1) if ranks.get(item, rank) != rank)
        return ShelvingPlan(
            location_id=target.id,
            path=target.path,
            location_version=target.version,
            copies=planned,
            fill_after=Fill(count, target.capacity),
            shifted=shifted,
            refusals=refusals,
            unchanged=_already_in_place(session, target, copies, position, rules.is_level(target.kind)),
        )


# --- Ranger en scannant (D12) -----------------------------------------------------------------------------------------
def shelve_by_code(
    database: Database,
    code: str,
    location_id: int,
    *,
    expected_location_version: int | None = None,
    user_id: int | None,
) -> Shelving:
    """Range à la fin du niveau l'exemplaire scanné : les scans successifs reproduisent l'ordre physique.

    Un exemplaire déjà sur ce niveau ne bouge pas : son rang n'est jamais changé par un second scan.
    """
    with database.session(write=True) as session, _business_rules():
        copy = repo.find_copy_by_code(session, code)
        if copy is None:
            raise BusinessError(_("Aucun exemplaire ne porte le code « {code} ».").format(code=code.strip()[:40]))
        target = _get_location(session, location_id)
        if copy.location_id == target.id:
            _check_target(session, target)
            return Shelving(
                location_id=target.id,
                path=target.path,
                copy_ids=(copy.id,),
                positions={copy.id: copy.shelf_position},
                previous={copy.id: (copy.location_id, copy.shelf_position)},
                statuses={},
                fill=repo.fill_of(session, target),
                location_version=target.version,
                shifted=0,
                already_there=True,
            )
        return _shelve(
            session,
            [copy.id],
            target.id,
            position=None,
            expected_versions=None,
            expected_location_version=expected_location_version,
            keep_processing=False,
            user_id=user_id,
            action="copy.shelve_scan",
        )


def undo_shelving(
    database: Database,
    copy_id: int,
    *,
    previous_location_id: int | None,
    previous_position: int | None,
    previous_status: str,
    expected_version: int,
    user_id: int | None,
) -> None:
    """Annule le dernier rangement d'un exemplaire : il revient à sa place d'avant, avec son statut d'avant.

    Refusé si l'exemplaire a été modifié depuis : on n'annule jamais par-dessus le travail de quelqu'un d'autre.
    """
    with database.session(write=True) as session, _business_rules():
        copy = _get_copy(session, copy_id)
        _refuse_on_loan(session, [copy], ON_LOAN_KEEPS_PLACE)
        _check_copy_version(copy, expected_version)
        if previous_status not in UNDOABLE_STATUSES:
            raise BusinessError(_("Ce rangement ne peut pas être annulé."))
        moment = now_iso()
        source = copy.location_id
        source_path = _paths(session, {source}).get(source) if source else None
        changed = False
        if previous_location_id is None:
            if source is not None:
                _release(session, [copy], moment)
                changed = True
        else:
            changed = _shelve(
                session,
                [copy.id],
                previous_location_id,
                position=previous_position,
                expected_versions=None,
                expected_location_version=None,
                keep_processing=True,
                user_id=user_id,
                action="copy.shelve_undo",
            ).changed
        if copy.status != previous_status:
            copy.status = previous_status
            changed = True
        if previous_status == "in_processing" and previous_location_id is None and copy.first_shelved_at:
            copy.first_shelved_at = None
            changed = True
        if not changed:
            return  # déjà dans l'état d'avant : rien à écrire (A14)
        _touch_copy(copy, user_id, moment)
        if previous_location_id is None:
            audit.record(
                session,
                "copy.shelve_undo",
                "copy",
                copy.id,
                user_id=user_id,
                details={"de": source_path, "vers": None, "statut": previous_status},
            )


# --- Fiche d'exemplaire -----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CopyInput:
    barcode: str
    inventory_number: str = ""
    call_number: str = ""
    is_loanable: bool = True
    condition: str = ""
    condition_note: str = ""
    acquisition_mode: str = ""
    acquired_on: str = ""
    acquisition_note: str = ""
    price: str = ""
    provenance: str = ""
    notes: str = ""


def code_holder(session: Session, value: str, copy: Copy | None = None) -> str | None:
    """Qui détient déjà ce code ? `None` s'il est libre pour cet exemplaire (D3 : unique, jamais réattribué).

    Un exemplaire peut reprendre un code qu'il a lui-même porté : corriger une faute de frappe reste possible.
    """
    own = copy.id if copy is not None else 0
    other = session.scalar(
        select(Copy).where((Copy.barcode == value) | (Copy.inventory_number == value), Copy.id != own).limit(1)
    )
    if other is not None:
        return _named(repo.briefs(session, [other.id])[other.id])
    retired = session.get(RetiredCode, value)
    if retired is not None and (copy is None or retired.copy_public_id != copy.public_id):
        return _("un exemplaire supprimé (un code n’est jamais réattribué)")
    # Une carte est citée sans son titulaire : gérer des exemplaires ne donne pas accès aux lecteurs.
    if session.execute(text("SELECT 1 FROM patron_cards WHERE number = :value"), {"value": value}).first() is not None:
        return _("une carte de lecteur (un même code ne peut pas désigner deux choses au scan)")
    return None


def _clean_codes(
    session: Session, copy: Copy, data: CopyInput, errors: dict[str, str]
) -> tuple[str | None, str]:
    """Codes saisis. Un exemplaire importé sans code-barres peut le rester (D-L6-7) ; un code-barres attribué ne
    s'efface pas. Un numéro d'inventaire inchangé est gardé tel quel, même hors du format de biblyO (« 9 »)."""
    if not data.barcode.strip() and copy.barcode is None:
        barcode = None
    else:
        barcode = rules.normalize_code(data.barcode)
        if barcode is None:
            errors["code_barres"] = _("Le code-barres compte de 3 à 32 caractères : lettres, chiffres et tirets.")
        elif barcode != copy.barcode and (holder := code_holder(session, barcode, copy)):
            errors["code_barres"] = _("Ce code est déjà attribué à {holder}.").format(holder=holder)
    if data.inventory_number.strip() and data.inventory_number.strip() == copy.inventory_number:
        inventory: str | None = copy.inventory_number
    elif data.inventory_number.strip():
        inventory = rules.normalize_code(data.inventory_number)
        if inventory is None:
            errors["inventaire"] = _(
                "Le numéro d’inventaire compte de 3 à 32 caractères : lettres, chiffres et tirets."
            )
        elif inventory != copy.inventory_number and (holder := code_holder(session, inventory, copy)):
            errors["inventaire"] = _("Ce numéro est déjà attribué à {holder}.").format(holder=holder)
    elif barcode is None:
        errors["inventaire"] = _("Indiquez un numéro d’inventaire : cet exemplaire n’a pas de code-barres.")
        inventory = None
    else:
        inventory = barcode  # égal au code-barres par défaut, mais distinct en base (D3)
    return barcode, inventory or ""


def _retire_codes(session: Session, copy: Copy, old: Sequence[tuple[str | None, str]], moment: str) -> None:
    """Un code abandonné est retiré pour toujours ; un code repris par son ancien exemplaire est libéré."""
    current = {code for code in (copy.barcode, copy.inventory_number) if code}
    for value, kind in old:
        if value and value not in current and session.get(RetiredCode, value) is None:
            session.add(RetiredCode(value=value, kind=kind, copy_public_id=copy.public_id, retired_at=moment))
    for value in current:
        retired = session.get(RetiredCode, value)
        if retired is not None and retired.copy_public_id == copy.public_id:
            session.delete(retired)


def update_copy(
    database: Database, copy_id: int, data: CopyInput, expected_version: int | None, user_id: int | None
) -> int:
    """Codes, cote, prêt, état, acquisition. Renvoie l'œuvre, pour revenir à sa fiche.

    La cote est celle de l'exemplaire : plusieurs exemplaires peuvent la partager, et elle ne change jamais d'elle-même.
    """
    with database.session(write=True) as session:
        copy = _get_copy(session, copy_id)
        _check_copy_version(copy, expected_version)
        errors: dict[str, str] = {}
        barcode, inventory = _clean_codes(session, copy, data, errors)
        try:
            call_number = rules.clean_call_number(data.call_number)
        except LocationError as error:
            errors["cote"] = str(error)
            call_number = None
        if data.condition and data.condition not in rules.CONDITIONS:
            errors["etat"] = _("Choisissez un état dans la liste.")
        if data.acquisition_mode and data.acquisition_mode not in rules.ACQUISITION_MODES:
            errors["mode_acquisition"] = _("Choisissez un mode d’acquisition dans la liste.")
        try:
            acquired_on = rules.clean_partial_date(data.acquired_on)
        except LocationError as error:
            errors["date_acquisition"] = str(error)
            acquired_on = None
        try:
            price = rules.parse_price(data.price)
        except LocationError as error:
            errors["prix"] = str(error)
            price = None
        values: dict[str, object] = {
            "barcode": barcode,
            "inventory_number": inventory,
            "call_number": call_number,
            "is_loanable": bool(data.is_loanable),
            "condition": data.condition or None,
            "condition_note": _line(data.condition_note, MAX_SHORT, "note_etat", errors),
            "acquisition_mode": data.acquisition_mode or None,
            "acquired_on": acquired_on,
            "acquisition_note": _line(data.acquisition_note, MAX_SHORT, "note_acquisition", errors),
            "price_cents": price,
            "provenance": _line(data.provenance, MAX_SHORT, "provenance", errors),
            "notes": _text(data.notes, MAX_NOTE, "notes", errors),
        }
        if errors:
            raise FormErrors(errors)
        changed = [key for key, value in values.items() if getattr(copy, key) != value]
        work_id = session.scalar(select(Edition.work_id).where(Edition.id == copy.edition_id))
        assert work_id is not None
        if not changed:
            return work_id
        old_codes = [(copy.barcode, "barcode"), (copy.inventory_number, "inventory")]
        details: dict[str, object] = {"champs": changed}
        if "barcode" in changed:
            details["code_barres"] = [copy.barcode, barcode]
        if "inventory_number" in changed:
            details["inventaire"] = [copy.inventory_number, inventory]
        if "call_number" in changed:
            details["cote"] = [copy.call_number, call_number]
        for key, value in values.items():
            setattr(copy, key, value)
        moment = now_iso()
        _touch_copy(copy, user_id, moment)
        session.flush()
        _retire_codes(session, copy, old_codes, moment)
        if {"barcode", "inventory_number", "call_number"} & set(changed):
            search.index_works(session, [work_id])
        audit.record(session, "copy.update", "copy", copy.id, user_id=user_id, details=details)
        return work_id


def change_status(
    database: Database,
    copy_id: int,
    action: str,
    *,
    reason: str = "",
    expected_version: int | None = None,
    user_id: int | None,
) -> int:
    """Réparation, introuvable, perdu, retrouvé, retrait, réintégration (D7). Renvoie l'œuvre.

    Perdu et retiré libèrent la place, et les voisins se resserrent ; introuvable et en réparation la gardent. Un
    retrait garde l'exemplaire et tout son historique : seul un exemplaire sans historique peut être supprimé.
    """
    with database.session(write=True) as session, _business_rules():
        copy = _get_copy(session, copy_id)
        _refuse_on_loan(session, [copy], ON_LOAN_RETURN_FIRST)
        _check_copy_version(copy, expected_version)
        target = rules.status_after_action(copy.status, action)
        errors: dict[str, str] = {}
        motive = _line(reason, MAX_SHORT, "motif", errors)
        if action == "withdraw" and not motive:
            errors["motif"] = _("Indiquez le motif du retrait.")
        if errors:
            raise FormErrors(errors)
        moment = now_iso()
        source, rank = copy.location_id, copy.shelf_position
        source_path = _paths(session, {source}).get(source) if source else None
        if action in rules.UNSHELVING_ACTIONS and source is not None:
            _release(session, [copy], moment)
        today = moment[:10]
        if target == "missing":
            copy.missing_on = today
        elif action == "found":
            copy.missing_on = None
        if target == "withdrawn":
            copy.withdrawn_on, copy.withdrawal_reason = today, motive
        elif action == "reinstate":
            copy.withdrawn_on, copy.withdrawal_reason = None, None
        before = copy.status
        copy.status = target
        _touch_copy(copy, user_id, moment)
        audit.record(
            session,
            "copy.status",
            "copy",
            copy.id,
            user_id=user_id,
            details={
                "action": action,
                "de": before,
                "vers": target,
                "motif": motive,
                "emplacement": source_path,
                "rang": rank,
            },
        )
        work_id = session.scalar(select(Edition.work_id).where(Edition.id == copy.edition_id))
        assert work_id is not None
        return work_id


# --- Lot L6 : mise en rayon des exemplaires importés, présence vérifiée, codes-barres attribués en lot ------------
@dataclass(frozen=True)
class PresenceLine:
    brief: repo.CopyBrief
    version: int
    path: str | None
    refusal: str | None


def presence_plan(database: Database, copy_ids: Sequence[int]) -> list[PresenceLine]:
    """Aperçu de « Confirmer la présence » : seul un exemplaire **rangé** et encore « En préparation » passe
    « En place ». Un exemplaire sans place, déjà en place ou dans un autre statut est montré, jamais changé."""
    with database.session() as session:
        copies = _load_copies(session, copy_ids)
        found = repo.briefs(session, [copy.id for copy in copies])
        paths = _paths(session, {copy.location_id for copy in copies})
        lines = []
        for copy in copies:
            refusal = None
            if copy.location_id is None:
                refusal = _("Sans place : rangez-le d’abord.")
            elif copy.status != "in_processing":
                refusal = _("Déjà « {s} » : rien à confirmer.").format(s=found[copy.id].status)
            lines.append(PresenceLine(found[copy.id], copy.version, paths.get(copy.location_id or 0), refusal))
        return lines


def confirm_presence(
    database: Database, expected_versions: Mapping[int, int], user_id: int | None
) -> list[int]:
    """Passe « En place » les exemplaires rangés dont la présence a été vérifiée sur l'étagère (arbitrage du
    13 novembre : aucune fausse disponibilité). Tout ou rien ; journalisé comme un changement de statut."""
    with database.session(write=True) as session, _business_rules():
        copies = _load_copies(session, list(expected_versions))
        _refuse_on_loan(session, copies, ON_LOAN_KEEPS_PLACE)
        for copy in copies:
            _check_copy_version(copy, expected_versions[copy.id])
            if copy.location_id is None or copy.status != "in_processing":
                raise BusinessError(_("Seul un exemplaire rangé et « En préparation » peut être confirmé en place."))
        moment = now_iso()
        for copy in copies:
            copy.status = "available"
            if copy.first_shelved_at is None:
                copy.first_shelved_at = moment
            _touch_copy(copy, user_id, moment)
            audit.record(
                session, "copy.status", "copy", copy.id, user_id=user_id,
                details={"action": "verify", "de": "in_processing", "vers": "available", "emplacement": None},
            )  # fmt: skip
        return [copy.id for copy in copies]


@dataclass(frozen=True)
class BarcodeLine:
    brief: repo.CopyBrief
    version: int
    inventory: str
    current: str | None
    proposed: str | None  # code qui serait attribué ; aucun si l'exemplaire en a déjà un


def _free_codes(session: Session, count: int) -> list[str]:
    """Les prochains codes libres de la séquence, **sans la faire avancer** : un aperçu ne consomme rien."""
    from biblyo.modules.catalogue.models import NumberSequence
    from biblyo.modules.catalogue.service import BARCODE_SEQUENCE, code_in_use

    sequence = session.get(NumberSequence, BARCODE_SEQUENCE)
    assert sequence is not None
    found: list[str] = []
    value = sequence.last_value
    while len(found) < count:
        value += 1
        code = f"{sequence.prefix}{value:0{sequence.padding}d}"
        if not code_in_use(session, code):
            found.append(code)
    return found


def barcode_plan(database: Database, copy_ids: Sequence[int]) -> list[BarcodeLine]:
    """Aperçu de l'attribution en lot (D-L6-7) : un code de la séquence pour chaque exemplaire **sans** code-barres ;
    un code existant n'est jamais remplacé, l'inventaire n'est jamais touché, rien n'est imprimé."""
    with database.session() as session:
        copies = _load_copies(session, copy_ids)
        found = repo.briefs(session, [copy.id for copy in copies])
        codes = iter(_free_codes(session, sum(1 for copy in copies if copy.barcode is None)))
        return [
            BarcodeLine(
                found[copy.id], copy.version, copy.inventory_number, copy.barcode,
                next(codes) if copy.barcode is None else None,
            )
            for copy in copies
        ]  # fmt: skip


def assign_barcodes(
    database: Database, expected_versions: Mapping[int, int], user_id: int | None
) -> dict[int, str]:
    """Attribue un code de la séquence à chaque exemplaire choisi qui n'en a pas. Tout ou rien, journalisé."""
    from biblyo.modules.catalogue.service import _next_barcode

    with database.session(write=True) as session, _business_rules():
        copies = _load_copies(session, list(expected_versions))
        for copy in copies:
            _check_copy_version(copy, expected_versions[copy.id])
        given: dict[int, str] = {}
        moment = now_iso()
        for copy in copies:
            if copy.barcode is not None:
                continue
            copy.barcode = _next_barcode(session)
            given[copy.id] = copy.barcode
            _touch_copy(copy, user_id, moment)
            session.flush()
            audit.record(
                session, "copy.update", "copy", copy.id, user_id=user_id,
                details={"champs": ["barcode"], "code_barres": [None, copy.barcode], "motif": "attribution_en_lot"},
            )  # fmt: skip
        if given:
            work_ids = list(
                session.scalars(
                    select(Edition.work_id).join(Copy, Copy.edition_id == Edition.id).where(Copy.id.in_(list(given)))
                )
            )
            search.index_works(session, sorted(set(work_ids)))
        return given
