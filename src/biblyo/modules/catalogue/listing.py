# SPDX-License-Identifier: EUPL-1.2
"""Liste du catalogue : critères lus depuis l'adresse, filtres cumulables, tri stable, pagination.

Tout l'état tient dans l'adresse : une recherche se partage, se rejoue et s'enregistre telle quelle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from math import ceil
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import ColumnElement, Select, and_, column, func, select, table, text
from sqlalchemy.orm import Session, aliased

from biblyo.core.i18n import _
from biblyo.core.text import normalize
from biblyo.modules.catalogue import repository as repo
from biblyo.modules.catalogue import search
from biblyo.modules.catalogue.models import (
    Agent,
    Collection,
    Copy,
    DocumentType,
    Edition,
    EditionContributor,
    EditionIdentifier,
    Publisher,
    Section,
    Series,
    Tag,
    Work,
    WorkContributor,
    WorkSeries,
    WorkTag,
)
from biblyo.modules.catalogue.rules import ACTIVE_COPY_EXCLUDED, AUDIENCES, LANGUAGES
from biblyo.modules.holdings.models import Location

PAGE_SIZE = 48
MAX_VALUE = 200
LOW_COMPLETENESS = 50

VIEWS = ("grid", "list")
SORTS: dict[str, str] = {
    "titre": _("Titre A→Z"),
    "serie": _("Série et tome"),
    "recents": _("Ajoutés récemment"),
    "annee": _("Année de parution"),
    "completude": _("Complétude croissante"),
    "rayon": _("Ordre sur l’étagère"),
}
PHYSICAL_ORDER = "rayon"
RELEVANCE = "pertinence"
RELEVANCE_LABEL = _("Pertinence")
META: dict[str, str] = {
    "sans-isbn": _("Sans ISBN"),
    "incomplet": _("Complétude < 50 %"),
    "sans-couverture": _("Sans couverture"),
    "sans-resume": _("Sans résumé"),
    "sans-auteur": _("Sans auteur"),
}
UNSHELVED = "non-ranges"
MAX_PLACE_OPTIONS = 300
COPIES: dict[str, str] = {"avec": _("Avec exemplaire"), "sans": _("Sans exemplaire")}
STATUSES: dict[str, str] = {"archivees": _("Archivées"), "toutes": _("Actives et archivées")}
_FILTER_FIELDS = (
    "type",
    "section",
    "cat",
    "genre",
    "mot",
    "langue",
    "public",
    "editeur",
    "collection",
    "serie",
    "contributeur",
    "annee_min",
    "annee_max",
    "exemplaires",
    "meta",
    "statut",
    "emplacement",
)
_ADVANCED_FIELDS = tuple(name for name in _FILTER_FIELDS if name not in ("type", "cat", "meta"))
REF_FIELDS = ("editeur", "collection", "serie", "contributeur")

_FTS = table("works_search", column("rowid"))


def _enum(value: str, allowed: Mapping[str, Any] | tuple[str, ...], default: str = "") -> str:
    return value if value in allowed else default


def _place(value: str) -> str:
    return value if value == UNSHELVED or (value.isdigit() and len(value) < 12) else ""


def _year(value: str) -> str:
    return value if value.isdigit() and len(value) == 4 else ""


@dataclass(frozen=True)
class Criteria:
    """Critères du catalogue. Une valeur inconnue ou mal formée est ignorée : une adresse ne provoque pas d'erreur."""

    q: str = ""
    view: str = "grid"
    tri: str = ""
    page: int = 1
    type: str = ""
    section: str = ""
    cat: str = ""
    genre: str = ""
    mot: str = ""
    langue: str = ""
    public: str = ""
    editeur: str = ""
    collection: str = ""
    serie: str = ""
    contributeur: str = ""
    annee_min: str = ""
    annee_max: str = ""
    exemplaires: str = ""
    meta: str = ""
    statut: str = ""
    emplacement: str = ""

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> Criteria:
        def read(name: str) -> str:
            return str(params.get(name) or "").strip()[:MAX_VALUE]

        page = read("page")
        return cls(
            q=read("q"),
            view=_enum(read("view"), VIEWS, "grid"),
            tri=_enum(read("tri"), SORTS),
            page=max(int(page), 1) if page.isdigit() and len(page) < 9 else 1,
            type=read("type"),
            section=read("section"),
            cat=normalize(read("cat")),
            genre=normalize(read("genre")),
            mot=normalize(read("mot")),
            langue=_enum(read("langue"), LANGUAGES),
            public=_enum(read("public"), AUDIENCES),
            editeur=read("editeur"),
            collection=read("collection"),
            serie=read("serie"),
            contributeur=read("contributeur"),
            annee_min=_year(read("annee_min")),
            annee_max=_year(read("annee_max")),
            exemplaires=_enum(read("exemplaires"), COPIES),
            meta=_enum(read("meta"), META),
            statut=_enum(read("statut"), STATUSES),
            emplacement=_place(read("emplacement")),
        )

    @property
    def has_filters(self) -> bool:
        return bool(self.q or any(getattr(self, name) for name in _FILTER_FIELDS))

    @property
    def advanced_open(self) -> bool:
        return any(getattr(self, name) for name in _ADVANCED_FIELDS if name not in REF_FIELDS or not self.is_ref(name))

    def is_ref(self, name: str) -> bool:
        """Filtre précis sur une fiche (une personne parmi des homonymes), plutôt qu'un texte saisi."""
        return str(getattr(self, name)).isdigit()

    @property
    def effective_sort(self) -> str:
        return self.tri or (RELEVANCE if search.build_match(self.q) else "titre")

    def as_dict(self) -> dict[str, str]:
        """Critères qui s'enregistrent : ni la page ni les valeurs par défaut."""
        values = asdict(self)
        values.pop("page")
        if values["view"] == "grid":
            values.pop("view")
        return {key: str(value) for key, value in values.items() if value}

    def query_string(self, **changes: Any) -> str:
        current = replace(self, **changes)
        values = current.as_dict()
        if current.page > 1:
            values["page"] = str(current.page)
        return urlencode(values)

    def url(self, **changes: Any) -> str:
        query = self.query_string(**changes)
        return f"/catalogue?{query}" if query else "/catalogue"

    def without(self, name: str) -> str:
        return self.url(**{name: "", "page": 1})


def _tag_filter(kind: str, label_norm: str) -> ColumnElement[bool]:
    return Work.id.in_(
        select(WorkTag.work_id)
        .join(Tag, Tag.id == WorkTag.tag_id)
        .where(Tag.kind == kind, Tag.label_norm == label_norm)
    )


def _named(value: str, id_column: Any, name_column: Any) -> ColumnElement[bool]:
    """Un identifiant désigne une fiche précise ; un texte cherche dans les noms, sans accents ni casse."""
    term = normalize(value)
    condition: ColumnElement[bool]
    if value.isdigit():
        condition = id_column == int(value)
    elif term:
        condition = name_column.contains(term, autoescape=True)
    else:  # que de la ponctuation : rien à chercher, et surtout pas « tout »
        condition = id_column < 0
    return condition


def _copies_at(place: str) -> Select[Any]:
    """Œuvres ayant un exemplaire dans ce lieu ou l'un de ses descendants, ou un exemplaire sans place."""
    statement = select(Edition.work_id).join(Copy, Copy.edition_id == Edition.id)
    if place == UNSHELVED:
        return statement.where(Copy.location_id.is_(None), Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
    return statement.where(Copy.location_id.in_(select(_places_under(place, "lieux_filtre").c.id)))


def filtered_statement(criteria: Criteria) -> Select[Any]:
    statement = select(Work.id)
    if criteria.statut == "archivees":
        statement = statement.where(Work.status == "archived")
    elif criteria.statut != "toutes":
        statement = statement.where(Work.status == "active")

    match = search.build_match(criteria.q)
    if match is not None:
        statement = statement.join(_FTS, _FTS.c.rowid == Work.id).where(
            text("works_search MATCH :fts_match").bindparams(fts_match=match)
        )

    # Des sous-requêtes IN plutôt que des EXISTS corrélés : SQLite part alors de l'index du critère le plus sélectif,
    # au lieu d'évaluer chaque notice du catalogue une à une.
    conditions: list[ColumnElement[bool]] = []
    if criteria.type:
        wanted = select(DocumentType.id).where(DocumentType.code == criteria.type).scalar_subquery()
        conditions.append(Work.document_type_id == wanted)  # une égalité : SQLite retient alors l'index du type
    if criteria.section:
        section = select(Section.id).where(Section.code == criteria.section).scalar_subquery()
        conditions.append(Work.section_id == section)
    if criteria.cat:
        conditions.append(_tag_filter("category", criteria.cat))
    if criteria.genre:
        conditions.append(_tag_filter("genre", criteria.genre))
    if criteria.mot:
        conditions.append(_tag_filter("keyword", criteria.mot))
    if criteria.langue:
        conditions.append(Work.id.in_(select(Edition.work_id).where(Edition.language == criteria.langue)))
    if criteria.public:
        conditions.append(Work.audience == criteria.public)
    if criteria.editeur:
        conditions.append(
            Work.id.in_(
                select(Edition.work_id)
                .join(Publisher, Publisher.id == Edition.publisher_id)
                .where(_named(criteria.editeur, Publisher.id, Publisher.name_norm))
            )
        )
    if criteria.collection:
        conditions.append(
            Work.id.in_(
                select(Edition.work_id)
                .join(Collection, Collection.id == Edition.collection_id)
                .where(_named(criteria.collection, Collection.id, Collection.name_norm))
            )
        )
    if criteria.serie:
        conditions.append(
            Work.id.in_(
                select(WorkSeries.work_id)
                .join(Series, Series.id == WorkSeries.series_id)
                .where(_named(criteria.serie, Series.id, Series.title_norm))
            )
        )
    if criteria.contributeur:
        person = _named(criteria.contributeur, Agent.id, Agent.name_norm)
        on_work = select(WorkContributor.work_id).join(Agent, Agent.id == WorkContributor.agent_id).where(person)
        on_edition = (
            select(Edition.work_id)
            .join(EditionContributor, EditionContributor.edition_id == Edition.id)
            .join(Agent, Agent.id == EditionContributor.agent_id)
            .where(person)
        )
        conditions.append(Work.id.in_(on_work.union(on_edition)))
    if criteria.annee_min or criteria.annee_max:
        years: list[ColumnElement[bool]] = []
        if criteria.annee_min:
            years.append(Edition.publication_year >= int(criteria.annee_min))
        if criteria.annee_max:
            years.append(Edition.publication_year <= int(criteria.annee_max))
        # La même édition satisfait les deux bornes.
        conditions.append(Work.id.in_(select(Edition.work_id).where(*years)))
    if criteria.exemplaires:
        with_copy = select(Edition.work_id).join(Copy, Copy.edition_id == Edition.id)
        conditions.append(Work.id.in_(with_copy) if criteria.exemplaires == "avec" else Work.id.not_in(with_copy))
    if criteria.emplacement:
        conditions.append(Work.id.in_(_copies_at(criteria.emplacement)))
    if criteria.meta == "sans-isbn":
        with_isbn = (
            select(Edition.work_id)
            .join(EditionIdentifier, EditionIdentifier.edition_id == Edition.id)
            .where(EditionIdentifier.scheme.in_(("isbn13", "isbn10")))
        )
        conditions.append(Work.id.not_in(with_isbn))
    elif criteria.meta == "incomplet":
        conditions.append(Work.completeness < LOW_COMPLETENESS)
    elif criteria.meta == "sans-couverture":
        conditions.append(Work.id.not_in(select(Edition.work_id).where(Edition.cover_media_id.is_not(None))))
    elif criteria.meta == "sans-resume":
        conditions.append(Work.summary.is_(None))
    elif criteria.meta == "sans-auteur":
        conditions.append(Work.id.not_in(select(WorkContributor.work_id)))
    return statement.where(*conditions)


def _places_under(place: str, name: str) -> Any:
    """Le lieu et tous ses descendants. Le nom distingue le filtre du tri, réunis dans une même requête."""
    tree = select(Location.id).where(Location.id == int(place)).cte(name, recursive=True)
    return tree.union_all(select(Location.id).where(Location.parent_id == tree.c.id))


def _shelf_rank(place: str) -> Any:
    """Pour chaque œuvre, la place physique de son premier exemplaire dans ce lieu : meuble, niveau, rang (CAT-13)."""
    furniture = aliased(Location)
    key = func.printf(
        "%06d-%06d-%06d", func.coalesce(furniture.sort_order, 0), Location.sort_order, Copy.shelf_position
    )
    return (
        select(Edition.work_id.label("work_id"), func.min(key).label("rang"))
        .join(Copy, Copy.edition_id == Edition.id)
        .join(Location, Location.id == Copy.location_id)
        .outerjoin(furniture, furniture.id == Location.parent_id)
        .where(Copy.location_id.in_(select(_places_under(place, "lieux_tri").c.id)), Copy.shelf_position.is_not(None))
        .group_by(Edition.work_id)
        .subquery("rayon")
    )


def _sorted(statement: Select[Any], sort: str, place: str = "") -> Select[Any]:
    """Tri total : l'identifiant départage toujours, une page ne répète ni n'oublie aucune notice."""
    if sort == PHYSICAL_ORDER and place.isdigit():
        rank = _shelf_rank(place)
        return statement.outerjoin(rank, rank.c.work_id == Work.id).order_by(
            rank.c.rang.is_(None), rank.c.rang, Work.title_sort, Work.id
        )
    if sort == RELEVANCE:
        return statement.order_by(text(search.RANK), Work.title_sort, Work.id)
    if sort == "recents":
        return statement.order_by(Work.created_at.desc(), Work.id.desc())
    if sort == "completude":
        return statement.order_by(Work.completeness, Work.title_sort, Work.id)
    if sort == "serie":
        return (
            statement.outerjoin(WorkSeries, and_(WorkSeries.work_id == Work.id, WorkSeries.is_primary.is_(True)))
            .outerjoin(Series, Series.id == WorkSeries.series_id)
            .order_by(
                Series.title_sort.is_(None), Series.title_sort, WorkSeries.volume_number, Work.title_sort, Work.id
            )
        )
    if sort == "annee":
        return statement.outerjoin(Edition, and_(Edition.work_id == Work.id, Edition.is_primary.is_(True))).order_by(
            Edition.publication_year.is_(None), Edition.publication_year.desc(), Work.title_sort, Work.id
        )
    return statement.order_by(Work.title_sort, Work.id)


def page_window(page: int, pages: int) -> list[int | None]:
    """Numéros de page à afficher, `None` marquant un saut."""
    keep = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    result: list[int | None] = []
    for previous, number in zip([0, *keep], keep, strict=False):
        if number - previous > 1:
            result.append(None)
        result.append(number)
    return result


@dataclass(frozen=True)
class Listing:
    cards: list[repo.CardView]
    total: int
    copies: int
    page: int
    pages: int
    sort: str

    @property
    def window(self) -> list[int | None]:
        return page_window(self.page, self.pages)


def run(session: Session, criteria: Criteria, *, page_size: int = PAGE_SIZE) -> Listing:
    statement = filtered_statement(criteria)
    unordered = statement.order_by(None)
    total = session.scalar(unordered.with_only_columns(func.count(), maintain_column_froms=True)) or 0
    copies = (
        session.scalar(
            unordered.with_only_columns(func.count(), maintain_column_froms=True)
            .join(Edition, Edition.work_id == Work.id)
            .join(Copy, Copy.edition_id == Edition.id)
        )
        or 0
    )
    pages = max(1, ceil(total / page_size))
    page = min(criteria.page, pages)
    sort = criteria.effective_sort
    ids: list[int] = list(
        session.scalars(_sorted(statement, sort, criteria.emplacement).limit(page_size).offset((page - 1) * page_size))
    )
    return Listing(repo.load_cards(session, ids), total, copies, page, pages, sort)


@dataclass(frozen=True)
class FilterOptions:
    types: list[tuple[str, str]]
    sections: list[tuple[str, str]]
    categories: list[tuple[str, str]]
    genres: list[tuple[str, str]]
    refs: dict[str, str]
    places: list[tuple[str, str]]


def _tag_options(session: Session, kind: str) -> list[tuple[str, str]]:
    statement = select(Tag.label_norm, Tag.label).where(Tag.kind == kind).order_by(Tag.label_norm).limit(500)
    rows = session.execute(statement)
    return [(key, label) for key, label in rows]


def _ref_label(session: Session, field: str, value: int) -> str | None:
    model_column: dict[str, Any] = {
        "editeur": Publisher.name,
        "collection": Collection.name,
        "serie": Series.title,
        "contributeur": Agent.display_name,
    }
    label_column = model_column[field]
    label: str | None = session.scalar(select(label_column).where(label_column.class_.id == value))
    return label


def _place_options(session: Session, selected: str) -> list[tuple[str, str]]:
    """Salles, meubles et niveaux. Au-delà de 300 lieux, les niveaux s'atteignent depuis leur meuble, pas d'ici."""
    rows = session.execute(select(Location.id, Location.path, Location.kind).order_by(Location.path)).all()
    if len(rows) > MAX_PLACE_OPTIONS:
        rows = [row for row in rows if row[2] not in ("shelf", "bin") or str(row[0]) == selected]
    return [(UNSHELVED, _("Non rangés")), *((str(item), path) for item, path, _kind in rows)]


def filter_options(session: Session, criteria: Criteria) -> FilterOptions:
    types = repo.document_types(session)
    sections = repo.sections(session)
    refs: dict[str, str] = {}
    for field in REF_FIELDS:
        if criteria.is_ref(field):
            label = _ref_label(session, field, int(getattr(criteria, field)))
            refs[field] = label or _("Fiche introuvable")
    return FilterOptions(
        types=[(item.code, item.label) for item in types],
        sections=[(item.code, item.label) for item in sections],
        categories=_tag_options(session, "category"),
        genres=_tag_options(session, "genre"),
        refs=refs,
        places=_place_options(session, criteria.emplacement),
    )
