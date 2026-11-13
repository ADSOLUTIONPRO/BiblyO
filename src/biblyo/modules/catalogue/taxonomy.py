# SPDX-License-Identifier: EUPL-1.2
"""Listes du catalogue : types, sections, catégories, genres, mots-clés, éditeurs, collections.

Renommer, désactiver si la liste le permet, et fusionner **sans perdre aucune association** : chaque fusion se fait
dans une seule transaction, remet l'index de recherche à jour et laisse une trace dans le journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.core.i18n import _
from biblyo.core.text import normalize
from biblyo.modules.catalogue import search
from biblyo.modules.catalogue.models import Collection, DocumentType, Edition, Publisher, Section, Tag, Work, WorkTag

MAX_LABEL = 200
PAGE_SIZE = 100


@dataclass(frozen=True)
class Kind:
    code: str
    label: str
    singular: str
    tag_kind: str | None = None
    can_disable: bool = False
    can_create: bool = False


KINDS: dict[str, Kind] = {
    "types": Kind("types", _("Types de document"), _("ce type"), can_disable=True, can_create=True),
    "sections": Kind("sections", _("Sections"), _("cette section"), can_disable=True, can_create=True),
    "categories": Kind("categories", _("Catégories"), _("cette catégorie"), tag_kind="category"),
    "genres": Kind("genres", _("Genres"), _("ce genre"), tag_kind="genre"),
    "mots-cles": Kind("mots-cles", _("Mots-clés"), _("ce mot-clé"), tag_kind="keyword"),
    "editeurs": Kind("editeurs", _("Éditeurs"), _("cet éditeur")),
    "collections": Kind("collections", _("Collections"), _("cette collection")),
}


@dataclass(frozen=True)
class Row:
    id: int
    label: str
    count: int
    is_active: bool = True
    parent_id: int | None = None
    detail: str = ""


def rows(session: Session, kind: Kind) -> list[Row]:
    counts: dict[Any, int]
    statement: Any
    if kind.tag_kind:
        statement = select(WorkTag.tag_id, func.count()).group_by(WorkTag.tag_id)
        counts = dict(session.execute(statement).all())
        tags = session.scalars(select(Tag).where(Tag.kind == kind.tag_kind).order_by(Tag.label_norm))
        return [Row(tag.id, tag.label, counts.get(tag.id, 0)) for tag in tags]
    if kind.code == "types":
        statement = select(Work.document_type_id, func.count()).group_by(Work.document_type_id)
        counts = dict(session.execute(statement).all())
        types = session.scalars(select(DocumentType).order_by(DocumentType.sort_order, DocumentType.label))
        return [Row(item.id, item.label, counts.get(item.id, 0), item.is_active) for item in types]
    if kind.code == "sections":
        statement = select(Work.section_id, func.count()).group_by(Work.section_id)
        counts = dict(session.execute(statement).all())
        sections = list(session.scalars(select(Section).order_by(Section.sort_order, Section.label)))
        names = {item.id: item.label for item in sections}
        return [
            Row(
                item.id,
                item.label,
                counts.get(item.id, 0),
                item.is_active,
                item.parent_id,
                names.get(item.parent_id, "") if item.parent_id else "",
            )
            for item in sections
        ]
    if kind.code == "editeurs":
        works = func.count(func.distinct(Edition.work_id))
        statement = select(Edition.publisher_id, works).group_by(Edition.publisher_id)
        counts = dict(session.execute(statement).all())
        publishers = session.scalars(select(Publisher).order_by(Publisher.name_norm))
        return [Row(item.id, item.name, counts.get(item.id, 0)) for item in publishers]
    works = func.count(func.distinct(Edition.work_id))
    statement = select(Edition.collection_id, works).group_by(Edition.collection_id)
    counts = dict(session.execute(statement).all())
    collections = session.execute(
        select(Collection, Publisher.name)
        .outerjoin(Publisher, Publisher.id == Collection.publisher_id)
        .order_by(Collection.name_norm, Publisher.name_norm)
    )
    return [Row(item.id, item.name, counts.get(item.id, 0), detail=publisher or "") for item, publisher in collections]


def _clean(label: str) -> str:
    text = " ".join(label.split())
    if not text or not normalize(text):
        raise BusinessError(_("Indiquez un libellé."))
    if len(text) > MAX_LABEL:
        raise BusinessError(_("Ce libellé est trop long."))
    return text


def _works_with_tag(session: Session, tag_id: int) -> list[int]:
    return list(session.scalars(select(WorkTag.work_id).where(WorkTag.tag_id == tag_id)))


def _works_with_publisher(session: Session, publisher_id: int) -> list[int]:
    return list(session.scalars(select(Edition.work_id).where(Edition.publisher_id == publisher_id).distinct()))


def _works_with_collection(session: Session, collection_id: int) -> list[int]:
    return list(session.scalars(select(Edition.work_id).where(Edition.collection_id == collection_id).distinct()))


def _get(session: Session, model: Any, item_id: int) -> Any:
    item = session.get(model, item_id)
    if item is None:
        raise BusinessError(_("Cet élément n’existe plus."))
    return item


def _get_tag(session: Session, kind: Kind, item_id: int) -> Tag:
    tag: Tag | None = session.scalar(select(Tag).where(Tag.id == item_id, Tag.kind == kind.tag_kind))
    if tag is None:
        raise BusinessError(_("Cet élément n’existe plus."))
    return tag


def _reject_duplicate(exists_already: bool) -> None:
    if exists_already:
        raise BusinessError(_("Ce libellé existe déjà : fusionnez plutôt les deux éléments."))


def rename(database: Database, kind: Kind, item_id: int, label: str, user_id: int | None) -> None:
    text = _clean(label)
    key = normalize(text)
    with database.session(write=True) as session:
        affected: list[int] = []
        if kind.tag_kind:
            tag = _get_tag(session, kind, item_id)
            other = session.scalar(
                select(Tag.id).where(Tag.kind == kind.tag_kind, Tag.label_norm == key, Tag.id != item_id)
            )
            _reject_duplicate(other is not None)
            tag.label, tag.label_norm = text, key
            affected = _works_with_tag(session, item_id)
        elif kind.code == "editeurs":
            publisher = _get(session, Publisher, item_id)
            other = session.scalar(select(Publisher.id).where(Publisher.name_norm == key, Publisher.id != item_id))
            _reject_duplicate(other is not None)
            publisher.name, publisher.name_norm = text, key
            affected = _works_with_publisher(session, item_id)
        elif kind.code == "collections":
            collection = _get(session, Collection, item_id)
            scope = (
                Collection.publisher_id == collection.publisher_id
                if collection.publisher_id
                else Collection.publisher_id.is_(None)
            )
            other = session.scalar(
                select(Collection.id).where(Collection.name_norm == key, scope, Collection.id != item_id)
            )
            _reject_duplicate(other is not None)
            collection.name, collection.name_norm = text, key
            affected = _works_with_collection(session, item_id)
        else:
            model: Any = DocumentType if kind.code == "types" else Section
            item = _get(session, model, item_id)
            same = session.scalars(select(model).where(model.id != item_id))
            _reject_duplicate(any(normalize(row.label) == key for row in same))
            item.label = text
        session.flush()
        search.index_works(session, affected)
        audit.record(session, "taxonomy.rename", kind.code, item_id, user_id=user_id)


def _slug(session: Session, model: Any, label: str) -> str:
    base = normalize(label).replace(" ", "-")[:40] or "element"
    candidate, suffix = base, 2
    while session.scalar(select(model.id).where(model.code == candidate)) is not None:
        candidate, suffix = f"{base}-{suffix}", suffix + 1
    return candidate


def _section_chain(session: Session, section_id: int | None) -> set[int]:
    """La section et tous ses parents. Une chaîne déjà bouclée s'arrête au premier retour."""
    seen: set[int] = set()
    current = section_id
    while current is not None and current not in seen:
        seen.add(current)
        current = session.scalar(select(Section.parent_id).where(Section.id == current))
    return seen


def create(database: Database, kind: Kind, label: str, user_id: int | None, parent_id: int | None = None) -> int:
    if not kind.can_create:
        raise BusinessError(_("Ces éléments se créent depuis les notices."))
    text = _clean(label)
    key = normalize(text)
    with database.session(write=True) as session:
        model: Any = DocumentType if kind.code == "types" else Section
        _reject_duplicate(any(normalize(row.label) == key for row in session.scalars(select(model))))
        code = _slug(session, model, text)
        if kind.code == "types":
            item: Any = DocumentType(code=code, label=text, icon="book", sort_order=100, is_active=True)
        else:
            if parent_id is not None:
                _get(session, Section, parent_id)
            item = Section(code=code, label=text, parent_id=parent_id, sort_order=100, is_active=True)
        session.add(item)
        session.flush()
        audit.record(session, "taxonomy.create", kind.code, item.id, user_id=user_id)
        return int(item.id)


def set_parent(database: Database, section_id: int, parent_id: int | None, user_id: int | None) -> None:
    with database.session(write=True) as session:
        section = _get(session, Section, section_id)
        if parent_id is not None:
            _get(session, Section, parent_id)
            if section_id in _section_chain(session, parent_id):
                raise BusinessError(_("Cette section ne peut pas être rangée dans une de ses propres sous-sections."))
        section.parent_id = parent_id
        audit.record(session, "taxonomy.parent", "sections", section_id, user_id=user_id)


def set_active(database: Database, kind: Kind, item_id: int, active: bool, user_id: int | None) -> None:
    if not kind.can_disable:
        raise BusinessError(_("Cette liste ne se désactive pas."))
    with database.session(write=True) as session:
        model: Any = DocumentType if kind.code == "types" else Section
        item = _get(session, model, item_id)
        if not active and kind.code == "types":
            remaining = session.scalar(
                select(func.count())
                .select_from(DocumentType)
                .where(DocumentType.is_active.is_(True), DocumentType.id != item_id)
            )
            if not remaining:
                raise BusinessError(_("Il faut garder au moins un type de document actif."))
        item.is_active = active
        audit.record(session, "taxonomy.active", kind.code, item_id, user_id=user_id, details={"actif": active})


def _merge_tags(session: Session, source_id: int, target_id: int) -> tuple[list[int], int]:
    affected = _works_with_tag(session, source_id)
    both = select(WorkTag.work_id).where(WorkTag.tag_id == target_id)
    # Une œuvre qui porte déjà les deux étiquettes n'en garde qu'une : l'association demeure, sans doublon.
    session.execute(delete(WorkTag).where(WorkTag.tag_id == source_id, WorkTag.work_id.in_(both)))
    session.execute(update(WorkTag).where(WorkTag.tag_id == source_id).values(tag_id=target_id))
    session.delete(session.get(Tag, source_id))
    return affected, len(affected)


def _merge_collections(session: Session, source_id: int, target_id: int) -> list[int]:
    affected = _works_with_collection(session, source_id)
    session.execute(update(Edition).where(Edition.collection_id == source_id).values(collection_id=target_id))
    session.delete(session.get(Collection, source_id))
    return affected


def _merge_publishers(session: Session, source_id: int, target_id: int) -> list[int]:
    affected = set(_works_with_publisher(session, source_id))
    session.execute(update(Edition).where(Edition.publisher_id == source_id).values(publisher_id=target_id))
    target_collections = dict(
        session.execute(select(Collection.name_norm, Collection.id).where(Collection.publisher_id == target_id)).all()
    )
    for collection in list(session.scalars(select(Collection).where(Collection.publisher_id == source_id))):
        twin = target_collections.get(collection.name_norm)
        if twin is not None:  # les deux éditeurs avaient une collection de ce nom : elles n'en font plus qu'une
            affected.update(_merge_collections(session, collection.id, twin))
        else:
            collection.publisher_id = target_id
    session.flush()
    session.delete(session.get(Publisher, source_id))
    return sorted(affected)


def merge(database: Database, kind: Kind, source_id: int, target_id: int, user_id: int | None) -> None:
    """Déplace toutes les associations de la source vers la cible, puis supprime la source."""
    if source_id == target_id:
        raise BusinessError(_("Choisissez un autre élément pour la fusion."))
    with database.session(write=True) as session:
        affected: list[int] = []
        moved: int
        if kind.tag_kind:
            _get_tag(session, kind, source_id)
            _get_tag(session, kind, target_id)
            affected, moved = _merge_tags(session, source_id, target_id)
        elif kind.code == "editeurs":
            _get(session, Publisher, source_id)
            _get(session, Publisher, target_id)
            affected = _merge_publishers(session, source_id, target_id)
            moved = len(affected)
        elif kind.code == "collections":
            _get(session, Collection, source_id)
            _get(session, Collection, target_id)
            affected = _merge_collections(session, source_id, target_id)
            moved = len(affected)
        elif kind.code == "types":
            _get(session, DocumentType, source_id)
            target = _get(session, DocumentType, target_id)
            if not target.is_active:
                raise BusinessError(_("Choisissez un type actif comme cible de la fusion."))
            result = session.execute(
                update(Work).where(Work.document_type_id == source_id).values(document_type_id=target_id)
            )
            moved = result.rowcount  # type: ignore[attr-defined]
            session.delete(session.get(DocumentType, source_id))
        else:
            _get(session, Section, source_id)
            _get(session, Section, target_id)
            if source_id in _section_chain(session, target_id):
                raise BusinessError(_("Une section ne peut pas être fusionnée dans une de ses sous-sections."))
            result = session.execute(update(Work).where(Work.section_id == source_id).values(section_id=target_id))
            moved = result.rowcount  # type: ignore[attr-defined]
            session.execute(update(Section).where(Section.parent_id == source_id).values(parent_id=target_id))
            session.delete(session.get(Section, source_id))
        session.flush()
        search.index_works(session, affected)
        audit.record(
            session,
            "taxonomy.merge",
            kind.code,
            target_id,
            user_id=user_id,
            details={"source": source_id, "associations": moved},
        )
