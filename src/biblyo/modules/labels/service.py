# SPDX-License-Identifier: EUPL-1.2
"""Étiquettes (lot L8) : choisir les exemplaires, régler la planche et la mise en page, préparer un lot et son PDF.

**Imprimer n'attribue jamais de code.** Seul un exemplaire qui a déjà un code-barres enregistré entre dans un lot ; un
aperçu ne lit que la base. Un lot garde, étiquette par étiquette, le code imprimé : une réimpression ou une reprise
relit le code enregistré de l'exemplaire et refuse de continuer s'il a changé depuis.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _
from biblyo.core.ids import new_public_id
from biblyo.core.settings_store import get_setting, set_setting
from biblyo.modules.catalogue import listing
from biblyo.modules.catalogue.models import Copy, Edition, Series, Work, WorkSeries
from biblyo.modules.catalogue.service import ConflictError
from biblyo.modules.holdings.models import Location
from biblyo.modules.labels import layout, sheets
from biblyo.modules.labels.layout import Appearance, LabelData, Plan
from biblyo.modules.labels.models import LabelBatch, LabelBatchItem
from biblyo.modules.labels.render import to_pdf

PREFERENCES = "labels.preferences"
MAX_LABELS_PER_BATCH = 2000
MAX_OFFSET = 5.0
EXCLUDED_STATUSES = ("withdrawn", "lost")  # un livre retiré ou perdu n'a pas d'étiquette à recevoir
ORDERS: dict[str, str] = {
    "cote": _("Par cote, puis série et tome"),
    "inventaire": _("Par numéro d’inventaire"),
    "rayon": _("Dans l’ordre des étagères"),
}
CODE_FILTERS: dict[str, str] = {
    "": _("Tous les exemplaires"),
    "avec": _("Avec un code-barres"),
    "sans": _("Sans code-barres"),
    "a-etiqueter": _("Avec un code, étiquette jamais confirmée"),
}


# --- Préférences ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Preferences:
    sheet: str = sheets.DEFAULT
    custom: dict[str, str] = field(default_factory=dict)
    model: str = "standard"
    elements: tuple[str, ...] = ()  # seulement pour le modèle personnalisé
    offset: tuple[float, float] = (0.0, 0.0)
    verified: dict[str, str] = field(default_factory=dict)  # gabarit → date de la feuille test jugée juste

    def current_sheet(self) -> sheets.Sheet:
        if self.sheet == sheets.CUSTOM and self.custom:
            try:
                return sheets.custom_sheet(sheets.CustomInput(**_custom_fields(self.custom)))
            except FormErrors:
                return sheets.KNOWN_BY_CODE[sheets.DEFAULT]
        return sheets.KNOWN_BY_CODE.get(self.sheet, sheets.KNOWN_BY_CODE[sheets.DEFAULT])

    def elements_for(self, model: str | None = None) -> frozenset[str]:
        chosen = model or self.model
        if chosen in layout.MODELS:
            return layout.MODELS[chosen][1]
        return frozenset(name for name in self.elements if name in layout.ELEMENTS)


def _custom_fields(values: dict[str, str]) -> dict[str, str]:
    names = {
        "colonnes": "columns",
        "lignes": "rows",
        "largeur": "width",
        "hauteur": "height",
        "marge_haut": "top",
        "marge_gauche": "left",
        "espace_horizontal": "gap_x",
        "espace_vertical": "gap_y",
    }
    return {target: str(values.get(source, "")) for source, target in names.items()}


def preferences(session: Session) -> Preferences:
    data = get_setting(session, PREFERENCES, None) or {}
    offset = data.get("offset") or [0, 0]
    return Preferences(
        sheet=str(data.get("sheet") or sheets.DEFAULT),
        custom={str(k): str(v) for k, v in (data.get("custom") or {}).items()},
        model=str(data.get("model") or "standard"),
        elements=tuple(str(item) for item in data.get("elements") or ()),
        offset=(float(offset[0]), float(offset[1])),
        verified={str(k): str(v) for k, v in (data.get("verified") or {}).items()},
    )


def _store(session: Session, prefs: Preferences, user_id: int | None) -> None:
    set_setting(
        session,
        PREFERENCES,
        {
            "sheet": prefs.sheet,
            "custom": prefs.custom,
            "model": prefs.model,
            "elements": list(prefs.elements),
            "offset": list(prefs.offset),
            "verified": prefs.verified,
        },
        user_id,
    )


def parse_offset(horizontal: str, vertical: str) -> tuple[float, float]:
    errors: dict[str, str] = {}
    values = []
    for name, text in (("decalage_horizontal", horizontal), ("decalage_vertical", vertical)):
        try:
            value = float((text or "0").strip().replace(",", ".").replace("+", ""))
        except ValueError:
            errors[name] = _("Indiquez un nombre de millimètres, par exemple 0,5 ou -1.")
            continue
        if abs(value) > MAX_OFFSET:
            errors[name] = _("Un décalage de plus de 5 mm vient d’un mauvais gabarit, pas de l’imprimante.")
        values.append(round(value, 2))
    if errors:
        raise FormErrors(errors)
    return values[0], values[1]


def save_preferences(
    database: Database,
    *,
    sheet: str,
    custom: sheets.CustomInput | None,
    model: str,
    elements: Sequence[str],
    offset: tuple[float, float],
    user_id: int | None,
) -> Preferences:
    """Planche, modèle et calibration par défaut de toute l'installation (une seule imprimante, en général). Un gabarit
    personnalisé est validé avant d'être gardé."""
    custom_values: dict[str, str] = {}
    if sheet == sheets.CUSTOM:
        if custom is None:
            raise FormErrors({"gabarit": _("Décrivez la planche personnalisée.")})
        custom_values = sheets.custom_values(sheets.custom_sheet(custom))
    elif sheet not in sheets.KNOWN_BY_CODE:
        raise FormErrors({"planche": _("Choisissez une planche de la liste.")})
    if model not in layout.MODELS and model != layout.CUSTOM_MODEL:
        raise FormErrors({"modele": _("Choisissez un modèle d’étiquette.")})
    with database.session(write=True) as session:
        current = preferences(session)
        updated = replace(
            current,
            sheet=sheet,
            custom=custom_values or current.custom,
            model=model,
            elements=tuple(name for name in elements if name in layout.ELEMENTS),
            offset=offset,
        )
        _store(session, updated, user_id)
        audit.record(
            session,
            "labels.preferences",
            "settings",
            None,
            user_id=user_id,
            details={"planche": sheet, "modele": model, "decalage": list(offset)},
        )
        return updated


def mark_sheet_verified(database: Database, sheet_code: str, user_id: int | None) -> None:
    """La feuille test, posée sur la vraie planche, tombe juste : on le note, avec la date."""
    with database.session(write=True) as session:
        current = preferences(session)
        _store(session, replace(current, verified={**current.verified, sheet_code: now_iso()[:10]}), user_id)
        audit.record(
            session, "labels.sheet_verified", "settings", None, user_id=user_id,
            details={"planche": sheet_code, "feuille_test": "juste à 100 %", "douchette": "code d'essai lu"},
        )


# --- Sélection --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Selection:
    """Quels exemplaires : des filtres, ou une liste explicite (fiche d'un exemplaire, cases cochées)."""

    q: str = ""
    serie: str = ""
    tome_min: str = ""
    tome_max: str = ""
    cote: str = ""
    emplacement: str = ""
    code: str = ""
    ordre: str = "cote"
    ids: tuple[int, ...] = ()

    @classmethod
    def read(cls, values: Any) -> Selection:
        def text(name: str, limit: int = 200) -> str:
            value = values.get(name)
            return value.strip()[:limit] if isinstance(value, str) else ""

        raw = values.getlist("exemplaire") if hasattr(values, "getlist") else values.get("exemplaire", [])
        ids = tuple(
            dict.fromkeys(int(item) for item in raw if isinstance(item, str) and item.isdecimal() and len(item) < 10)
        )[:MAX_LABELS_PER_BATCH]
        number = text("tome_min", 6), text("tome_max", 6)
        return cls(
            q=text("q"),
            serie=text("serie"),
            tome_min=number[0] if number[0].isdecimal() else "",
            tome_max=number[1] if number[1].isdecimal() else "",
            cote=text("cote", 40),
            emplacement=listing._place(text("emplacement", 12)),
            code=text("code", 20) if text("code", 20) in CODE_FILTERS else "",
            ordre=text("ordre", 20) if text("ordre", 20) in ORDERS else "cote",
            ids=ids,
        )

    @property
    def has_filters(self) -> bool:
        return bool(self.q or self.serie or self.tome_min or self.tome_max or self.cote or self.emplacement)

    def hidden(self) -> list[tuple[str, str]]:
        """La sélection telle qu'un formulaire la renvoie à l'étape suivante."""
        pairs = [
            (name, getattr(self, name))
            for name in ("q", "serie", "tome_min", "tome_max", "cote", "emplacement", "code", "ordre")
            if getattr(self, name)
        ]
        return pairs + [("exemplaire", str(item)) for item in self.ids]


def _confirmed_copies() -> Select[Any]:
    return (
        select(LabelBatchItem.copy_id)
        .join(LabelBatch, LabelBatch.id == LabelBatchItem.batch_id)
        .where(LabelBatch.status == "confirmed", LabelBatchItem.copy_id.is_not(None))
    )


def _statement(selection: Selection) -> Select[Any]:
    statement = (
        select(Copy.id)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .outerjoin(WorkSeries, (WorkSeries.work_id == Work.id) & (WorkSeries.is_primary.is_(True)))
        .where(Copy.status.not_in(EXCLUDED_STATUSES))
    )
    if selection.ids:
        return statement.where(Copy.id.in_(selection.ids))
    if selection.q or selection.serie:
        criteria = listing.Criteria(q=selection.q, serie=selection.serie, statut="toutes")
        statement = statement.where(Work.id.in_(listing.filtered_statement(criteria)))
    if selection.tome_min:
        statement = statement.where(WorkSeries.volume_number >= int(selection.tome_min))
    if selection.tome_max:
        statement = statement.where(WorkSeries.volume_number <= int(selection.tome_max))
    if selection.cote:
        statement = statement.where(Copy.call_number.startswith(selection.cote.upper(), autoescape=True))
    if selection.emplacement == listing.UNSHELVED:
        statement = statement.where(Copy.location_id.is_(None))
    elif selection.emplacement:
        places = listing._places_under(selection.emplacement, "lieux_etiquettes")
        statement = statement.where(Copy.location_id.in_(select(places.c.id)))
    if selection.code == "avec":
        statement = statement.where(Copy.barcode.is_not(None))
    elif selection.code == "sans":
        statement = statement.where(Copy.barcode.is_(None))
    elif selection.code == "a-etiqueter":
        statement = statement.where(Copy.barcode.is_not(None), Copy.id.not_in(_confirmed_copies()))
    return statement


def _ordered(statement: Select[Any], order: str) -> Select[Any]:
    if order == "inventaire":
        return statement.order_by(func.length(Copy.inventory_number), Copy.inventory_number, Copy.id)
    if order == "rayon":
        return statement.outerjoin(Location, Location.id == Copy.location_id).order_by(
            Location.path.is_(None), Location.path, Copy.shelf_position, Copy.call_number, Copy.id
        )
    return statement.order_by(
        Copy.call_number.is_(None),
        Copy.call_number,
        WorkSeries.series_id,
        WorkSeries.volume_number,
        Work.title_sort,
        Copy.id,
    )


def select_copies(session: Session, selection: Selection, *, limit: int | None = None) -> list[int]:
    statement = _ordered(_statement(selection), selection.ordre)
    if limit is not None:
        statement = statement.limit(limit)
    found = list(dict.fromkeys(session.scalars(statement)))
    if selection.ids:  # une liste explicite garde l'ordre choisi par la personne
        rank = {copy_id: index for index, copy_id in enumerate(selection.ids)}
        found.sort(key=lambda copy_id: rank.get(copy_id, len(rank)))
    return found


@dataclass(frozen=True)
class SelectionCount:
    total: int
    with_code: int
    without_code: int


def count_selection(session: Session, selection: Selection) -> SelectionCount:
    ids = select_copies(session, selection)
    with_code = session.scalar(
        select(func.count()).select_from(Copy).where(Copy.id.in_(ids), Copy.barcode.is_not(None))
    )
    return SelectionCount(len(ids), int(with_code or 0), len(ids) - int(with_code or 0))


# --- Contenu des étiquettes -------------------------------------------------------------------------------------------
def _volume(number: float | None, label: str | None) -> str:
    if label:
        return label
    if number is None:
        return ""
    return str(int(number)) if float(number).is_integer() else str(number).replace(".", ",")


def label_data(session: Session, copy_ids: Sequence[int]) -> tuple[list[LabelData], list[int]]:
    """Ce que chaque étiquette montrera, dans l'ordre demandé ; et les exemplaires écartés faute de code."""
    rows = session.execute(
        select(Copy, Work.title, Series.title, WorkSeries.volume_number, WorkSeries.volume_label, Location.path)
        .join(Edition, Edition.id == Copy.edition_id)
        .join(Work, Work.id == Edition.work_id)
        .outerjoin(WorkSeries, (WorkSeries.work_id == Work.id) & (WorkSeries.is_primary.is_(True)))
        .outerjoin(Series, Series.id == WorkSeries.series_id)
        .outerjoin(Location, Location.id == Copy.location_id)
        .where(Copy.id.in_(list(copy_ids)))
    ).all()
    found = {row[0].id: row for row in rows}
    items: list[LabelData] = []
    missing: list[int] = []
    for copy_id in copy_ids:
        row = found.get(copy_id)
        if row is None:
            continue
        copy, title, series, number, volume_label, path = row
        if copy.barcode is None:
            missing.append(copy_id)
            continue
        items.append(
            LabelData(
                copy.barcode,
                title or "",
                series or "",
                _volume(number, volume_label),
                copy.call_number or "",
                copy.inventory_number,
                (path or "").split(" / ")[-1] if path else "",
                copy.id,
            )
        )
    return items, missing


def appearance(session: Session, elements: frozenset[str]) -> tuple[Appearance, bytes | None]:
    from biblyo.modules.admin.parametres import identity, logo_bytes

    found = logo_bytes(session) if "logo" in elements else None
    library = identity(session).name if "bibliotheque" in elements else ""
    return Appearance(elements, library, found is not None), found[0] if found else None


# --- Lots ------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Request:
    """Une demande d'impression complète, telle que l'assistant la construit à l'étape 3."""

    copy_ids: tuple[int, ...]
    sheet: sheets.Sheet
    model: str
    elements: frozenset[str]
    occupied: frozenset[int]
    offset: tuple[float, float]


def build_plan(session: Session, request: Request) -> tuple[Plan, bytes | None, list[int]]:
    """Mise en page de la demande, lue en base au moment même : codes enregistrés seulement."""
    if len(request.copy_ids) > MAX_LABELS_PER_BATCH:
        raise BusinessError(_("Au plus 2 000 étiquettes par lot : imprimez en plusieurs fois."))
    items, missing = label_data(session, request.copy_ids)
    if not items:
        raise BusinessError(
            _("Aucun de ces exemplaires n’a de code-barres : générez d’abord les codes, puis imprimez les étiquettes.")
        )
    look, logo = appearance(session, request.elements)
    try:
        plan = layout.plan(items, look, request.sheet, occupied=request.occupied, offset=request.offset)
    except layout.LayoutError as error:
        raise BusinessError(str(error)) from error
    return plan, logo, missing


def _model_data(request: Request) -> str:
    return json.dumps({"modele": request.model, "elements": sorted(request.elements)}, ensure_ascii=False)


def prepare_batch(
    database: Database,
    request: Request,
    *,
    origin: str,
    user_id: int | None,
    parent_id: int | None = None,
    title: str = "",
) -> tuple[int, bytes]:
    """Enregistre le lot (« préparé ») et rend son PDF, produit de la même mise en page. Rien n'est attribué."""
    with database.session(write=True) as session:
        plan, logo, _missing = build_plan(session, request)
        moment = now_iso()
        batch = LabelBatch(
            public_id=new_public_id(),
            status="prepared",
            origin=origin,
            parent_id=parent_id,
            sheet=json.dumps(sheets.as_data(request.sheet), ensure_ascii=False),
            model=_model_data(request),
            start_position=min(layout.free_positions(request.sheet, request.occupied)),
            occupied=json.dumps(sorted(request.occupied)),
            offset_x=request.offset[0],
            offset_y=request.offset[1],
            total=plan.count,
            sheets=len(plan.pages),
            created_by=user_id,
            created_at=moment,
            updated_at=moment,
        )
        session.add(batch)
        session.flush()
        rank = 0
        for page in plan.pages:
            for placed in page:
                rank += 1
                session.add(
                    LabelBatchItem(
                        batch_id=batch.id,
                        rank=rank,
                        copy_id=placed.data.copy_id,
                        barcode=placed.data.code,
                        sheet_number=placed.sheet_number,
                        position=placed.position,
                    )
                )
        audit.record(
            session,
            "labels.batch",
            "label_batch",
            batch.id,
            user_id=user_id,
            details={
                "origine": origin,
                "etiquettes": plan.count,
                "feuilles": len(plan.pages),
                "planche": request.sheet.code,
                "depart": batch.start_position,
                "parent": parent_id,
            },
        )
        pdf = to_pdf(plan, title=title or _("Étiquettes biblyO, lot {n}").format(n=batch.id), logo=logo)
        return batch.id, pdf


@dataclass(frozen=True)
class BatchItemView:
    rank: int
    copy_id: int | None
    printed: str
    current: str | None  # code enregistré aujourd'hui : il doit être le même
    title: str
    sheet_number: int
    position: int


@dataclass(frozen=True)
class BatchView:
    batch: LabelBatch
    sheet: sheets.Sheet
    model: str
    elements: frozenset[str]
    occupied: frozenset[int]
    items: tuple[BatchItemView, ...]
    author: str

    @property
    def changed(self) -> list[BatchItemView]:
        return [item for item in self.items if item.copy_id is not None and item.current != item.printed]


def _load_batch(session: Session, batch_id: int) -> BatchView:
    from biblyo.core.models import User

    batch = session.get(LabelBatch, batch_id)
    if batch is None:
        raise BusinessError(_("Ce lot d’étiquettes n’existe pas."))
    rows = session.execute(
        select(LabelBatchItem, Copy.barcode, Work.title)
        .outerjoin(Copy, Copy.id == LabelBatchItem.copy_id)
        .outerjoin(Edition, Edition.id == Copy.edition_id)
        .outerjoin(Work, Work.id == Edition.work_id)
        .where(LabelBatchItem.batch_id == batch_id)
        .order_by(LabelBatchItem.rank)
    ).all()
    model = json.loads(batch.model)
    author = session.get(User, batch.created_by) if batch.created_by else None
    return BatchView(
        batch,
        sheets.from_data(json.loads(batch.sheet)),
        str(model.get("modele", "standard")),
        frozenset(model.get("elements", ())),
        frozenset(int(item) for item in json.loads(batch.occupied)),
        tuple(
            BatchItemView(
                item.rank,
                item.copy_id,
                item.barcode,
                current,
                title or _("Exemplaire supprimé"),
                item.sheet_number,
                item.position,
            )
            for item, current, title in rows
        ),
        author.display_name if author is not None else "",
    )


def batch_view(session: Session, batch_id: int) -> BatchView:
    return _load_batch(session, batch_id)


def recent_batches(session: Session, limit: int = 8) -> list[LabelBatch]:
    return list(session.scalars(select(LabelBatch).order_by(LabelBatch.id.desc()).limit(limit)))


def _checked_items(view: BatchView, from_rank: int = 1) -> list[int]:
    """Les exemplaires du lot à partir d'une étiquette, si leur code enregistré est toujours celui imprimé."""
    if view.changed:
        raise ConflictError(
            _("Le code de {n} exemplaire(s) a changé depuis ce lot : préparez une nouvelle impression.").format(
                n=len(view.changed)
            )
        )
    chosen = [item.copy_id for item in view.items if item.rank >= from_rank and item.copy_id is not None]
    if not chosen:
        raise BusinessError(_("Il ne reste aucune étiquette à imprimer dans ce lot."))
    return chosen


def batch_pdf(database: Database, batch_id: int) -> bytes:
    """Le PDF d'un lot, refait à l'identique : mêmes cases, mêmes codes. Refusé si un code a changé depuis."""
    with database.session() as session:
        view = _load_batch(session, batch_id)
        request = Request(
            tuple(_checked_items(view)),
            view.sheet,
            view.model,
            view.elements,
            view.occupied,
            (view.batch.offset_x, view.batch.offset_y),
        )
        plan, logo, _missing = build_plan(session, request)
        printed = [(placed.data.code, placed.sheet_number, placed.position) for page in plan.pages for placed in page]
        expected = [(item.printed, item.sheet_number, item.position) for item in view.items if item.copy_id]
        if printed != expected:
            raise ConflictError(_("Ce lot ne peut plus être refait à l’identique : préparez une nouvelle impression."))
        return to_pdf(plan, title=_("Étiquettes biblyO, lot {n}").format(n=batch_id), logo=logo)


def resume_request(database: Database, batch_id: int, from_rank: int) -> tuple[Request, BatchView]:
    """La suite d'un lot interrompu, à partir de l'étiquette `from_rank`, sur une feuille à choisir."""
    with database.session() as session:
        view = _load_batch(session, batch_id)
        chosen = _checked_items(view, from_rank)
    return Request(
        tuple(chosen), view.sheet, view.model, view.elements, frozenset(), (view.batch.offset_x, view.batch.offset_y)
    ), view


def set_batch_status(database: Database, batch_id: int, status: str, user_id: int | None) -> None:
    """« Imprimé et collé » ou « Abandonné » : dit par la personne, jamais déduit. Journalisé."""
    if status not in ("confirmed", "abandoned"):
        raise BusinessError(_("État inconnu."))
    with database.session(write=True) as session:
        batch = session.get(LabelBatch, batch_id)
        if batch is None:
            raise BusinessError(_("Ce lot d’étiquettes n’existe pas."))
        if batch.status != "prepared":
            raise BusinessError(_("Ce lot est déjà marqué : son état ne change plus."))
        moment = now_iso()
        batch.status = status
        batch.updated_at = moment
        if status == "confirmed":
            batch.confirmed_at, batch.confirmed_by = moment, user_id
        audit.record(
            session, f"labels.{status}", "label_batch", batch.id, user_id=user_id, details={"etiquettes": batch.total}
        )


# --- Vue d'ensemble ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Overview:
    totals: Any  # holdings.service.BarcodeTotals
    sequence: Any  # holdings.service.SequenceState
    never_confirmed: int
    pending_batches: int


def overview(session: Session) -> Overview:
    from biblyo.modules.holdings import service as holdings

    never = session.scalar(
        select(func.count())
        .select_from(Copy)
        .where(Copy.barcode.is_not(None), Copy.status.not_in(EXCLUDED_STATUSES), Copy.id.not_in(_confirmed_copies()))
    )
    pending = session.scalar(select(func.count()).select_from(LabelBatch).where(LabelBatch.status == "prepared"))
    return Overview(
        holdings.barcode_totals(session), holdings.sequence_state(session), int(never or 0), int(pending or 0)
    )
