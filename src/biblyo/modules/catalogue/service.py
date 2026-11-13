# SPDX-License-Identifier: EUPL-1.2
"""Règles d'écriture du catalogue. Une notice est une œuvre et ses éditions (ADR 0003).

Rien ne rapproche automatiquement deux ouvrages : même titre, même tome ou même ISBN restent permis. Toute
écriture passe par `_after_change`, qui tient à jour ce qui se déduit de l'œuvre.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from biblyo.core import audit
from biblyo.core.clock import now_iso
from biblyo.core.db import Database
from biblyo.core.errors import BusinessError, FormErrors
from biblyo.core.i18n import _
from biblyo.core.ids import new_public_id
from biblyo.core.isbn import Isbn, parse_isbn, split_list
from biblyo.core.media import StoredImage, store_image
from biblyo.core.models import AuditEntry
from biblyo.core.text import normalize, sort_name, split_terms, title_sort
from biblyo.modules.catalogue import repository as repo
from biblyo.modules.catalogue import search
from biblyo.modules.catalogue.models import (
    Agent,
    Collection,
    Copy,
    CoverChange,
    DocumentType,
    Edition,
    EditionContributor,
    EditionIdentifier,
    MediaFile,
    NumberSequence,
    Publisher,
    SavedSearch,
    Section,
    Series,
    SeriesExpectedVolume,
    Tag,
    Work,
    WorkContributor,
    WorkSeries,
    WorkTag,
)
from biblyo.modules.catalogue.rules import (
    ACTIVE_COPY_EXCLUDED,
    AUDIENCES,
    CONTRIBUTOR_ROLES,
    LANGUAGES,
    SERIES_STATUSES,
    InvalidVolume,
    Volume,
    completeness,
    parse_volume,
)

MAX_TITLE = 500
MAX_NAME = 200
MAX_TEXT = 20_000
MAX_SHORT = 200
MIN_YEAR, MAX_YEAR = 1400, 2200
MAX_PAGES = 100_000
MAX_COPIES_AT_ONCE = 50
MAX_CONFIRMED_RANGE = 500
BARCODE_SEQUENCE = "copy_barcode"

CONFLICT_MESSAGE = _(
    "Cette fiche vient d’être modifiée par une autre personne. Vos changements n’ont pas été enregistrés : "
    "rechargez la fiche, puis recommencez."
)


class ConflictError(BusinessError):
    """Deux modifications concurrentes : la seconde est refusée, rien n'est écrasé."""


@dataclass(frozen=True)
class ContributorInput:
    name: str
    role: str = "author"
    choice: str = ""  # vide : automatique ; « new » : nouvelle personne ; sinon, l'identifiant d'une personne
    dates: str = ""


@dataclass(frozen=True)
class IdentityInput:
    title: str
    document_type_id: int | None
    subtitle: str = ""
    original_title: str = ""
    section_id: int | None = None
    audience: str = ""
    contributors: tuple[ContributorInput, ...] = ()


@dataclass(frozen=True)
class SeriesInput:
    title: str = ""
    volume: str = ""


@dataclass(frozen=True)
class ContentInput:
    summary: str = ""
    notes: str = ""
    categories: str = ""
    genres: str = ""
    keywords: str = ""


@dataclass(frozen=True)
class EditionInput:
    isbn: str = ""
    publisher: str = ""
    collection: str = ""
    collection_number: str = ""
    year: str = ""
    date: str = ""
    statement: str = ""
    language: str = "fre"
    pages: str = ""
    physical_note: str = ""
    title_variant: str = ""
    notes: str = ""
    contributors: tuple[ContributorInput, ...] = field(default=())


def _clean(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    return text or None


def _clean_block(value: str | None) -> str | None:
    """Texte long : les sauts de ligne sont conservés."""
    text = (value or "").replace("\r\n", "\n").strip()
    return text or None


# --- Validation -------------------------------------------------------------------------------------------------------
def _validate_identity(session: Session, data: IdentityInput, *, keep_type: int | None = None) -> dict[str, str]:
    errors: dict[str, str] = {}
    title = _clean(data.title)
    if not title:
        errors["titre"] = _("Indiquez un titre.")
    elif len(title) > MAX_TITLE:
        errors["titre"] = _("Le titre est trop long.")
    document_type = session.get(DocumentType, data.document_type_id) if data.document_type_id else None
    if document_type is None or (not document_type.is_active and document_type.id != keep_type):
        errors["type"] = _("Choisissez un type de document.")
    if data.section_id is not None and session.get(Section, data.section_id) is None:
        errors["section"] = _("Cette section n’existe pas.")
    if data.audience and data.audience not in AUDIENCES:
        errors["public"] = _("Ce public n’existe pas.")
    return errors


def _validate_contributors(session: Session, rows: tuple[ContributorInput, ...], prefix: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    for index, row in enumerate(rows):
        name = _clean(row.name)
        if not name:
            continue
        key = f"{prefix}_{index}"
        if len(name) > MAX_NAME:
            errors[key] = _("Ce nom est trop long.")
        elif row.role not in CONTRIBUTOR_ROLES:
            errors[key] = _("Choisissez un rôle.")
        elif row.choice == "new":
            continue
        elif row.choice:
            agent = session.get(Agent, int(row.choice)) if row.choice.isdigit() else None
            if agent is None or agent.name_norm != normalize(name):
                errors[key] = _("Choisissez à nouveau la personne : elle ne correspond plus au nom saisi.")
        elif len(repo.agents_named(session, name)) > 1:
            errors[key] = _("Plusieurs personnes portent ce nom : indiquez laquelle, ou créez-en une nouvelle.")
    return errors


def _validate_isbn(text: str) -> tuple[list[Isbn], dict[str, str]]:
    parsed: list[Isbn] = []
    for part in split_list(text):
        isbn = parse_isbn(part)
        if isbn is None:
            return [], {
                "isbn": _("« {valeur} » n’est pas un ISBN valide (longueur ou clé de contrôle).").format(valeur=part)
            }
        if isbn not in parsed:
            parsed.append(isbn)
    return parsed, {}


def _validate_edition(data: EditionInput) -> tuple[list[Isbn], dict[str, str]]:
    parsed, errors = _validate_isbn(data.isbn)
    year = data.year.strip()
    if year and not (year.isdigit() and MIN_YEAR <= int(year) <= MAX_YEAR):
        errors["annee"] = _("Indiquez une année sur quatre chiffres.")
    pages = data.pages.strip()
    if pages and not (pages.isdigit() and 0 < int(pages) <= MAX_PAGES):
        errors["pages"] = _("Indiquez un nombre de pages.")
    if data.language not in LANGUAGES:
        errors["langue"] = _("Cette langue n’existe pas dans la liste.")
    for key, value in (("editeur", data.publisher), ("collection", data.collection)):
        if len(value) > MAX_NAME:
            errors[key] = _("Ce nom est trop long.")
    return parsed, errors


def _validate_series(data: SeriesInput) -> tuple[Volume, dict[str, str]]:
    errors: dict[str, str] = {}
    try:
        volume = parse_volume(data.volume)
    except InvalidVolume as exc:
        return Volume(), {"tome": str(exc)}
    title = _clean(data.title)
    if title and len(title) > MAX_TITLE:
        errors["serie"] = _("Le titre de la série est trop long.")
    elif title and not normalize(title):
        errors["serie"] = _("Le titre de la série doit contenir des lettres ou des chiffres.")
    if not title and not volume.is_empty:
        errors["serie"] = _("Indiquez la série de ce tome.")
    return volume, errors


def _validate_content(data: ContentInput) -> dict[str, str]:
    errors: dict[str, str] = {}
    if len(data.summary) > MAX_TEXT:
        errors["resume"] = _("Le résumé est trop long.")
    if len(data.notes) > MAX_TEXT:
        errors["notes"] = _("Les notes sont trop longues.")
    return errors


# --- Écritures communes -----------------------------------------------------------------------------------------------
def _get_work(session: Session, work_id: int) -> Work:
    work = session.get(Work, work_id)
    if work is None:
        raise BusinessError(_("Cette notice n’existe plus."))
    return work


def _get_edition(session: Session, edition_id: int) -> Edition:
    edition = session.get(Edition, edition_id)
    if edition is None:
        raise BusinessError(_("Cette édition n’existe plus."))
    return edition


def _check_version(current: int, expected: int | None) -> None:
    if expected is not None and current != expected:
        raise ConflictError(CONFLICT_MESSAGE)


def _touch_work(work: Work, user_id: int | None) -> None:
    work.version += 1
    work.updated_at = now_iso()
    work.updated_by = user_id


def after_change(session: Session, work: Work) -> None:
    """Tient à jour ce qui se déduit d'une œuvre, dans la transaction de l'écriture."""
    session.flush()
    work.completeness, _missing = completeness(repo.completeness_facts(session, work))
    session.flush()
    search.index_works(session, [work.id])


def _agent(session: Session, row: ContributorInput) -> int:
    name = " ".join(row.name.split())
    if row.choice.isdigit():
        return int(row.choice)
    if row.choice != "new":
        existing = repo.agents_named(session, name)
        if existing:  # plusieurs homonymes sans choix : refusé par la validation
            return existing[0].id
    agent = Agent(
        kind="person",
        display_name=name,
        sort_name=sort_name(name),
        name_norm=normalize(name),
        dates=_clean(row.dates),
        created_at=now_iso(),
    )
    session.add(agent)
    session.flush()
    return agent.id


def _resolved_contributors(session: Session, rows: tuple[ContributorInput, ...]) -> list[tuple[int, str]]:
    resolved: list[tuple[int, str]] = []
    for row in rows:
        if _clean(row.name):
            pair = (_agent(session, row), row.role)
            if pair not in resolved:
                resolved.append(pair)
    return resolved


def _replace_work_contributors(session: Session, work_id: int, rows: tuple[ContributorInput, ...]) -> None:
    resolved = _resolved_contributors(session, rows)
    session.execute(delete(WorkContributor).where(WorkContributor.work_id == work_id))
    for position, (agent_id, role) in enumerate(resolved):
        session.add(WorkContributor(work_id=work_id, agent_id=agent_id, role=role, position=position))


def _replace_edition_contributors(session: Session, edition_id: int, rows: tuple[ContributorInput, ...]) -> None:
    resolved = _resolved_contributors(session, rows)
    session.execute(delete(EditionContributor).where(EditionContributor.edition_id == edition_id))
    for position, (agent_id, role) in enumerate(resolved):
        session.add(EditionContributor(edition_id=edition_id, agent_id=agent_id, role=role, position=position))


def _publisher_id(session: Session, name: str) -> int | None:
    label = _clean(name)
    key = normalize(label)
    if not label or not key:
        return None
    publisher = session.scalar(select(Publisher).where(Publisher.name_norm == key))
    if publisher is None:
        publisher = Publisher(name=label, name_norm=key, created_at=now_iso())
        session.add(publisher)
        session.flush()
    return publisher.id


def _collection_id(session: Session, name: str, publisher_id: int | None) -> int | None:
    label = _clean(name)
    key = normalize(label)
    if not label or not key:
        return None
    match = Collection.publisher_id == publisher_id if publisher_id else Collection.publisher_id.is_(None)
    collection = session.scalar(select(Collection).where(Collection.name_norm == key, match))
    if collection is None:
        collection = Collection(publisher_id=publisher_id, name=label, name_norm=key, created_at=now_iso())
        session.add(collection)
        session.flush()
    return collection.id


def _replace_tags(session: Session, work_id: int, kind: str, text: str) -> None:
    current = select(Tag.id).where(Tag.kind == kind)
    session.execute(delete(WorkTag).where(WorkTag.work_id == work_id, WorkTag.tag_id.in_(current)))
    for label in split_terms(text):
        key = normalize(label)
        if not key:
            continue
        tag = session.scalar(select(Tag).where(Tag.kind == kind, Tag.label_norm == key))
        if tag is None:
            tag = Tag(kind=kind, label=label, label_norm=key, created_at=now_iso())
            session.add(tag)
            session.flush()
        session.add(WorkTag(work_id=work_id, tag_id=tag.id))


def _set_identifiers(session: Session, edition_id: int, parsed: list[Isbn]) -> None:
    schemes = ("isbn13", "isbn10")
    session.execute(
        delete(EditionIdentifier).where(
            EditionIdentifier.edition_id == edition_id, EditionIdentifier.scheme.in_(schemes)
        )
    )
    for isbn in parsed:
        session.add(EditionIdentifier(edition_id=edition_id, scheme="isbn13", value=isbn.isbn13))
        if isbn.isbn10:
            session.add(EditionIdentifier(edition_id=edition_id, scheme="isbn10", value=isbn.isbn10))


def _apply_edition(session: Session, edition: Edition, data: EditionInput, parsed: list[Isbn]) -> None:
    publisher_id = _publisher_id(session, data.publisher)
    edition.publisher_id = publisher_id
    edition.collection_id = _collection_id(session, data.collection, publisher_id)
    edition.collection_number = _clean(data.collection_number)
    edition.publication_year = int(data.year) if data.year.strip() else None
    edition.publication_date = _clean(data.date)
    edition.edition_statement = _clean(data.statement)
    edition.language = data.language
    edition.page_count = int(data.pages) if data.pages.strip() else None
    edition.physical_note = _clean(data.physical_note)
    edition.title_variant = _clean(data.title_variant)
    edition.notes = _clean_block(data.notes)
    session.flush()
    _set_identifiers(session, edition.id, parsed)
    _replace_edition_contributors(session, edition.id, data.contributors)


def _attach_series(session: Session, work_id: int, title: str, volume: Volume) -> None:
    """Le numéro interne de cote n'est jamais réattribué : il suit la notice dans sa série, et ne redescend pas."""
    label = _clean(title)
    current = session.scalar(select(WorkSeries).where(WorkSeries.work_id == work_id, WorkSeries.is_primary == True))  # noqa: E712
    if not label:
        if current is not None:
            session.delete(current)
        return
    key = normalize(label)
    series = session.scalar(select(Series).where(Series.title_norm == key))
    if series is None:
        moment = now_iso()
        series = Series(
            public_id=new_public_id(),
            title=label,
            title_sort=title_sort(label),
            title_norm=key,
            next_call_sequence=1,
            status="unknown",
            created_at=moment,
            updated_at=moment,
        )
        session.add(series)
        session.flush()
    if current is not None and current.series_id != series.id:
        session.delete(current)
        session.flush()
        current = None
    if current is None:
        current = WorkSeries(
            work_id=work_id,
            series_id=series.id,
            call_sequence=series.next_call_sequence,
            is_primary=True,
        )
        series.next_call_sequence += 1
        session.add(current)
    current.volume_number = volume.number
    current.volume_label = volume.label


# --- Notices ----------------------------------------------------------------------------------------------------------
def create_notice(
    database: Database,
    identity: IdentityInput,
    series: SeriesInput,
    content: ContentInput,
    edition: EditionInput,
    user_id: int | None,
) -> int:
    """Crée en une fois l'œuvre et sa première édition, qui devient l'édition principale."""
    with database.session(write=True) as session:
        errors = _validate_identity(session, identity)
        errors.update(_validate_contributors(session, identity.contributors, "contributeur"))
        volume, series_errors = _validate_series(series)
        errors.update(series_errors)
        errors.update(_validate_content(content))
        parsed, edition_errors = _validate_edition(edition)
        errors.update(edition_errors)
        errors.update(_validate_contributors(session, edition.contributors, "edition_contributeur"))
        if errors:
            raise FormErrors(errors)

        moment = now_iso()
        title = " ".join(identity.title.split())
        work = Work(
            public_id=new_public_id(),
            document_type_id=identity.document_type_id,
            section_id=identity.section_id,
            title=title,
            subtitle=_clean(identity.subtitle),
            title_sort=title_sort(title),
            title_norm=normalize(title),
            original_title=_clean(identity.original_title),
            audience=identity.audience or None,
            summary=_clean_block(content.summary),
            notes=_clean_block(content.notes),
            status="active",
            completeness=0,
            version=1,
            created_at=moment,
            updated_at=moment,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(work)
        session.flush()
        _replace_work_contributors(session, work.id, identity.contributors)
        _attach_series(session, work.id, series.title, volume)
        _write_tags(session, work.id, content)
        record = Edition(
            public_id=new_public_id(),
            work_id=work.id,
            is_primary=True,
            language="fre",
            status="active",
            origin="manual",
            version=1,
            created_at=moment,
            updated_at=moment,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(record)
        session.flush()
        _apply_edition(session, record, edition, parsed)
        after_change(session, work)
        audit.record(session, "work.create", "work", work.id, user_id=user_id)
        return work.id


def _write_tags(session: Session, work_id: int, content: ContentInput) -> None:
    _replace_tags(session, work_id, "category", content.categories)
    _replace_tags(session, work_id, "genre", content.genres)
    _replace_tags(session, work_id, "keyword", content.keywords)


def apply_identity(session: Session, work: Work, data: IdentityInput, user_id: int | None) -> None:
    """Valide et écrit l'identification d'une œuvre dans la session de l'appelant. Sert aussi à l'enrichissement."""
    errors = _validate_identity(session, data, keep_type=work.document_type_id)
    errors.update(_validate_contributors(session, data.contributors, "contributeur"))
    if errors:
        raise FormErrors(errors)
    title = " ".join(data.title.split())
    work.title = title
    work.title_sort = title_sort(title)
    work.title_norm = normalize(title)
    work.subtitle = _clean(data.subtitle)
    work.original_title = _clean(data.original_title)
    work.document_type_id = data.document_type_id  # type: ignore[assignment]  # validé ci-dessus
    work.section_id = data.section_id
    work.audience = data.audience or None
    _replace_work_contributors(session, work.id, data.contributors)
    _touch_work(work, user_id)
    after_change(session, work)


def update_identity(
    database: Database, work_id: int, data: IdentityInput, expected_version: int | None, user_id: int | None
) -> None:
    with database.session(write=True) as session:
        work = _get_work(session, work_id)
        _check_version(work.version, expected_version)
        apply_identity(session, work, data, user_id)
        audit.record(session, "work.update", "work", work.id, user_id=user_id, details={"section": "identification"})


def update_series(
    database: Database, work_id: int, data: SeriesInput, expected_version: int | None, user_id: int | None
) -> None:
    with database.session(write=True) as session:
        work = _get_work(session, work_id)
        _check_version(work.version, expected_version)
        volume, errors = _validate_series(data)
        if errors:
            raise FormErrors(errors)
        _attach_series(session, work.id, data.title, volume)
        _touch_work(work, user_id)
        after_change(session, work)
        audit.record(session, "work.update", "work", work.id, user_id=user_id, details={"section": "serie"})


def apply_content(session: Session, work: Work, data: ContentInput, user_id: int | None) -> None:
    """Valide et écrit résumé, notes et étiquettes dans la session de l'appelant. Sert aussi à l'enrichissement."""
    errors = _validate_content(data)
    if errors:
        raise FormErrors(errors)
    work.summary = _clean_block(data.summary)
    work.notes = _clean_block(data.notes)
    _write_tags(session, work.id, data)
    _touch_work(work, user_id)
    after_change(session, work)


def update_content(
    database: Database, work_id: int, data: ContentInput, expected_version: int | None, user_id: int | None
) -> None:
    with database.session(write=True) as session:
        work = _get_work(session, work_id)
        _check_version(work.version, expected_version)
        apply_content(session, work, data, user_id)
        audit.record(session, "work.update", "work", work.id, user_id=user_id, details={"section": "contenu"})


def _active_copies(session: Session, edition_ids: list[int]) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Copy)
            .where(Copy.edition_id.in_(edition_ids), Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
        )
        or 0
    )


def _all_copies(session: Session, edition_ids: list[int]) -> int:
    return session.scalar(select(func.count()).select_from(Copy).where(Copy.edition_id.in_(edition_ids))) or 0


def _edition_ids(session: Session, work_id: int) -> list[int]:
    return list(session.scalars(select(Edition.id).where(Edition.work_id == work_id)))


def archive_work(database: Database, work_id: int, user_id: int | None) -> None:
    """Archivage contrôlé : une notice dont des exemplaires sont encore au fonds ne disparaît pas du catalogue."""
    with database.session(write=True) as session:
        work = _get_work(session, work_id)
        if _active_copies(session, _edition_ids(session, work.id)):
            raise BusinessError(_("Cette notice a encore des exemplaires au fonds : retirez-les d’abord."))
        work.status = "archived"
        _touch_work(work, user_id)
        after_change(session, work)
        audit.record(session, "work.archive", "work", work.id, user_id=user_id)


def unarchive_work(database: Database, work_id: int, user_id: int | None) -> None:
    with database.session(write=True) as session:
        work = _get_work(session, work_id)
        work.status = "active"
        _touch_work(work, user_id)
        after_change(session, work)
        audit.record(session, "work.unarchive", "work", work.id, user_id=user_id)


def delete_work(database: Database, work_id: int, user_id: int | None) -> None:
    """Suppression seulement sans exemplaire : l'histoire d'un exemplaire ne s'efface pas. Sinon, archiver."""
    with database.session(write=True) as session:
        work = _get_work(session, work_id)
        if _all_copies(session, _edition_ids(session, work.id)):
            raise BusinessError(
                _("Cette notice a des exemplaires : elle ne peut pas être supprimée, seulement archivée.")
            )
        audit.record(session, "work.delete", "work", work.id, user_id=user_id)
        search.remove_works(session, [work.id])
        session.delete(work)


# --- Éditions ---------------------------------------------------------------------------------------------------------
def add_edition(database: Database, work_id: int, data: EditionInput, user_id: int | None) -> int:
    with database.session(write=True) as session:
        work = _get_work(session, work_id)
        parsed, errors = _validate_edition(data)
        errors.update(_validate_contributors(session, data.contributors, "edition_contributeur"))
        if errors:
            raise FormErrors(errors)
        moment = now_iso()
        record = Edition(
            public_id=new_public_id(),
            work_id=work.id,
            is_primary=False,
            language="fre",
            status="active",
            origin="manual",
            version=1,
            created_at=moment,
            updated_at=moment,
            created_by=user_id,
            updated_by=user_id,
        )
        session.add(record)
        session.flush()
        _apply_edition(session, record, data, parsed)
        _touch_work(work, user_id)
        after_change(session, work)
        audit.record(session, "edition.create", "edition", record.id, user_id=user_id, details={"work": work.id})
        return record.id


def update_edition(
    database: Database, edition_id: int, data: EditionInput, expected_version: int | None, user_id: int | None
) -> int:
    """Rend l'œuvre concernée. Une édition a sa propre version : deux personnes peuvent travailler en parallèle."""
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        _check_version(record.version, expected_version)
        apply_edition_fields(session, record, data, user_id)
        audit.record(session, "edition.update", "edition", record.id, user_id=user_id)
        return record.work_id


def apply_edition_fields(session: Session, record: Edition, data: EditionInput, user_id: int | None) -> Work:
    """Valide et écrit les champs d'une édition dans la session de l'appelant. Sert aussi à l'enrichissement."""
    parsed, errors = _validate_edition(data)
    errors.update(_validate_contributors(session, data.contributors, "edition_contributeur"))
    if errors:
        raise FormErrors(errors)
    _apply_edition(session, record, data, parsed)
    record.version += 1
    record.updated_at = now_iso()
    record.updated_by = user_id
    work = _get_work(session, record.work_id)
    after_change(session, work)
    return work


def set_primary_edition(database: Database, edition_id: int, user_id: int | None) -> int:
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        if record.status != "active":
            raise BusinessError(_("Une édition archivée ne peut pas être l’édition principale."))
        primary = select(Edition).where(Edition.work_id == record.work_id, Edition.is_primary.is_(True))
        for other in session.scalars(primary):
            other.is_primary = False
        session.flush()  # l'index partiel n'admet qu'une édition principale à la fois
        record.is_primary = True
        work = _get_work(session, record.work_id)
        _touch_work(work, user_id)
        after_change(session, work)
        audit.record(session, "edition.primary", "edition", record.id, user_id=user_id)
        return record.work_id


def archive_edition(database: Database, edition_id: int, user_id: int | None) -> int:
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        if record.is_primary:
            raise BusinessError(_("Choisissez d’abord une autre édition principale."))
        if _active_copies(session, [record.id]):
            raise BusinessError(_("Cette édition a encore des exemplaires au fonds : retirez-les d’abord."))
        record.status = "archived"
        record.version += 1
        work = _get_work(session, record.work_id)
        after_change(session, work)
        audit.record(session, "edition.archive", "edition", record.id, user_id=user_id)
        return record.work_id


def unarchive_edition(database: Database, edition_id: int, user_id: int | None) -> int:
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        record.status = "active"
        record.version += 1
        work = _get_work(session, record.work_id)
        after_change(session, work)
        audit.record(session, "edition.unarchive", "edition", record.id, user_id=user_id)
        return record.work_id


def delete_edition(database: Database, edition_id: int, user_id: int | None) -> int:
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        if record.is_primary:
            raise BusinessError(_("Choisissez d’abord une autre édition principale."))
        if _all_copies(session, [record.id]):
            raise BusinessError(
                _("Cette édition a des exemplaires : elle ne peut pas être supprimée, seulement archivée.")
            )
        work = _get_work(session, record.work_id)
        audit.record(session, "edition.delete", "edition", record.id, user_id=user_id, details={"work": work.id})
        session.delete(record)
        session.flush()
        _touch_work(work, user_id)
        after_change(session, work)
        return work.id


# --- Couvertures ------------------------------------------------------------------------------------------------------
# Une couverture appartient à une édition. Tout changement passe par `change_cover`, qui l'inscrit dans
# `cover_changes` (révision 0010) : ajout, remplacement, retrait, enrichissement, annulation, rétablissement.
COVER_HISTORY_DAYS = 90
STAGED_FILE_HOURS = 2


@dataclass(frozen=True)
class CoverOrigin:
    """Provenance d'une couverture : source (« bnf », « openlibrary »), adresse (« url ») ou fichier (« upload »)."""

    origin: str
    origin_url: str
    source_code: str
    external_id: str
    license_note: str
    retrieved_at: str


@dataclass(frozen=True)
class CoverChangeInput:
    """Ce que le changement consigne, en plus des deux images."""

    kind: str  # voir COVER_CHANGE_KINDS
    source_code: str | None = None
    reference: str | None = None  # ARK, numéro d'image, adresse
    rights_note: str | None = None


def change_cover(
    session: Session, record: Edition, media_id: int | None, user_id: int | None, change: CoverChangeInput
) -> CoverChange:
    """Change la couverture d'une édition et le consigne, dans la session de l'appelant."""
    entry = CoverChange(
        edition_id=record.id,
        previous_media_id=record.cover_media_id,
        media_id=media_id,
        kind=change.kind,
        source_code=change.source_code,
        reference=change.reference,
        rights_note=change.rights_note,
        user_id=user_id,
        created_at=now_iso(),
    )
    record.cover_media_id = media_id
    record.version += 1
    record.updated_at = now_iso()
    record.updated_by = user_id
    after_change(session, _get_work(session, record.work_id))
    session.add(entry)
    session.flush()
    return entry


def media_for(session: Session, stored: StoredImage, provenance: CoverOrigin | None) -> MediaFile:
    """La ligne d'une image rangée. Une image identique déjà connue est réutilisée : sa provenance d'origine reste."""
    media = session.scalar(select(MediaFile).where(MediaFile.sha256 == stored.sha256))
    if media is None:
        media = MediaFile(
            sha256=stored.sha256,
            mime=stored.mime,
            bytes=stored.bytes,
            width=stored.width,
            height=stored.height,
            origin=provenance.origin if provenance else "upload",
            origin_url=(provenance.origin_url or None) if provenance else None,
            source_code=(provenance.source_code or None) if provenance else None,
            external_id=(provenance.external_id or None) if provenance else None,
            license_note=(provenance.license_note or None) if provenance else None,
            retrieved_at=(provenance.retrieved_at or None) if provenance else None,
            created_at=now_iso(),
        )
        session.add(media)
        session.flush()
    return media


def attach_cover(
    session: Session,
    record: Edition,
    stored: StoredImage,
    user_id: int | None,
    *,
    provenance: CoverOrigin | None = None,
    change: CoverChangeInput | None = None,
) -> MediaFile:
    """Rattache une image déjà rangée à une édition, dans la session de l'appelant. Sert aussi à l'enrichissement."""
    media = media_for(session, stored, provenance)
    default = CoverChangeInput(
        kind="source" if provenance and provenance.source_code else "upload",
        source_code=provenance.source_code if provenance else None,
        reference=provenance.external_id if provenance else None,
    )
    change_cover(session, record, media.id, user_id, change or default)
    return media


def set_cover_media(
    session: Session,
    record: Edition,
    media_id: int | None,
    user_id: int | None,
    change: CoverChangeInput | None = None,
) -> None:
    """Change la couverture d'une édition (ou la retire), dans la session de l'appelant, et le consigne."""
    fallback = CoverChangeInput("remove" if media_id is None else "restore")
    change_cover(session, record, media_id, user_id, change or fallback)


def set_cover(database: Database, edition_id: int, data: bytes, media_dir: Path, user_id: int | None) -> int:
    """Le fichier est contrôlé et rangé d'abord, hors transaction : nommé par son empreinte, il reste réutilisable."""
    stored = store_image(data, media_dir)
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        attach_cover(session, record, stored, user_id)
        audit.record(session, "edition.cover", "edition", record.id, user_id=user_id)
        return record.work_id


class CoverConflict(BusinessError):
    """La couverture a changé entre l'aperçu et l'enregistrement : rien n'est écrit."""


def save_cover(
    database: Database,
    edition_id: int,
    stored: StoredImage,
    user_id: int | None,
    *,
    provenance: CoverOrigin | None,
    change: CoverChangeInput,
    expected_media_id: int | None,
    replace: bool,
) -> int:
    """Enregistre la couverture vue à l'aperçu. Remplacer demande une case ; une couverture changée entre-temps
    n'est jamais écrasée."""
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        if record.cover_media_id != expected_media_id:
            raise CoverConflict(
                _("La couverture de cette édition a changé depuis l’aperçu : rien n’a été enregistré. Recommencez.")
            )
        if record.cover_media_id is not None and not replace:
            raise BusinessError(_("Cette édition a déjà une couverture : cochez « Remplacer la couverture actuelle »."))
        attach_cover(session, record, stored, user_id, provenance=provenance, change=change)
        audit.record(
            session,
            "edition.cover",
            "edition",
            record.id,
            user_id=user_id,
            details={"type": change.kind, "source": change.source_code, "reference": change.reference},
        )
        return record.work_id


def remove_cover(database: Database, edition_id: int, user_id: int | None) -> int:
    """Le fichier reste : la couverture se rétablit depuis l'historique, et seul le nettoyage le supprime."""
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        if record.cover_media_id is None:
            raise BusinessError(_("Cette édition n’a pas de couverture."))
        change_cover(session, record, None, user_id, CoverChangeInput("remove"))
        audit.record(session, "edition.cover_removed", "edition", record.id, user_id=user_id)
        return record.work_id


def restore_previous_cover(database: Database, edition_id: int, user_id: int | None) -> int:
    """Défait le dernier changement de couverture, s'il est encore celui en place et si l'image précédente existe."""
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        last = session.scalar(
            select(CoverChange).where(CoverChange.edition_id == edition_id).order_by(CoverChange.id.desc()).limit(1)
        )
        if last is None or last.undone_at is not None or last.kind in ("restore", "revert"):
            raise BusinessError(_("Aucun changement de couverture à défaire."))
        if record.cover_media_id != last.media_id:
            raise CoverConflict(
                _("La couverture a changé depuis ce changement : elle n’est pas remplacée sans votre choix.")
            )
        if last.kind == "enrichment":
            raise BusinessError(
                _("Cette couverture vient d’un enrichissement : annulez-le depuis l’onglet « Enrichissement ».")
            )
        # Sans couverture avant le changement, rétablir revient à la retirer ; une image retirée puis purgée est perdue
        if last.kind == "remove" and last.previous_media_id is None:
            raise BusinessError(_("L’image retirée n’est plus conservée : elle ne peut pas être rétablie."))
        moment = now_iso()
        last.undone_at = moment
        last.undone_by = user_id
        restore = CoverChangeInput("restore", reference=str(last.id))
        change_cover(session, record, last.previous_media_id, user_id, restore)
        audit.record(
            session, "edition.cover_restored", "edition", record.id, user_id=user_id, details={"change": last.id}
        )
        return record.work_id


@dataclass(frozen=True)
class MediaPurge:
    staged_files: tuple[str, ...]  # fichiers d'aperçus jamais confirmés
    unused_media: tuple[str, ...]  # images que plus rien ne désigne
    applied: bool


def purge_media(
    database: Database, media_dir: Path, *, apply: bool, now: float | None = None, today: str | None = None
) -> MediaPurge:
    """Fichiers devenus inutiles : aperçus non confirmés depuis plus de deux heures, images que plus aucune édition ni
    aucun changement des 90 derniers jours ne désignent. Sans `apply`, rien n'est supprimé."""
    import time
    from datetime import UTC, datetime, timedelta

    moment = time.time() if now is None else now
    reference = datetime.fromisoformat((today or now_iso()).replace("Z", "+00:00")).astimezone(UTC)
    horizon = (reference - timedelta(days=COVER_HISTORY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with database.session(write=apply) as session:
        known = set(session.scalars(select(MediaFile.sha256)))
        used: set[int] = {i for i in session.scalars(select(Edition.cover_media_id)) if i is not None}
        for current, previous in session.execute(
            select(CoverChange.media_id, CoverChange.previous_media_id).where(CoverChange.created_at >= horizon)
        ):
            used.update(i for i in (current, previous) if i is not None)
        unused = (
            list(session.scalars(select(MediaFile).where(MediaFile.id.not_in(used))))
            if used
            else list(session.scalars(select(MediaFile)))
        )
        staged: list[str] = []
        if media_dir.is_dir():
            for path in sorted(media_dir.glob("*.webp")):
                digest = path.name.split(".")[0].removesuffix("-v")
                if digest not in known and moment - path.stat().st_mtime > STAGED_FILE_HOURS * 3600:
                    staged.append(path.name)
        removed = [m.sha256 for m in unused]
        if apply:
            for media in unused:
                session.delete(media)
            for digest in removed:
                for name in (f"{digest}.webp", f"{digest}-v.webp"):
                    (media_dir / name).unlink(missing_ok=True)
            for name in staged:
                (media_dir / name).unlink(missing_ok=True)
            if removed or staged:
                audit.record(
                    session,
                    "media.purged",
                    "media_file",
                    None,
                    user_id=None,
                    details={"images": len(removed), "apercus": len(staged)},
                )
        return MediaPurge(tuple(staged), tuple(removed), apply)


@dataclass(frozen=True)
class MediaWithdrawal:
    sha256: str
    history: int  # changements d'historique qui la désignaient : ils ne la rétablissent plus
    applied: bool


def withdraw_media(
    database: Database, media_dir: Path, digest: str, *, reason: str, apply: bool, user_id: int | None = None
) -> MediaWithdrawal:
    """Retrait anticipé d'une image (droits contestés, demande de l'ayant droit) : sans attendre les 90 jours. Refusé
    pour une couverture actuelle, qui se supprime d'abord depuis sa page ; sans `apply`, rien ne change."""
    digest = digest.strip().lower()
    reason = " ".join(reason.split())[:300]
    if not reason:
        raise BusinessError(_("Indiquez le motif du retrait."))
    with database.session(write=apply) as session:
        media = session.scalar(select(MediaFile).where(MediaFile.sha256 == digest))
        if media is None:
            raise BusinessError(_("Aucune image enregistrée ne porte cette empreinte."))
        current = (
            session.scalar(select(func.count()).select_from(Edition).where(Edition.cover_media_id == media.id)) or 0
        )
        if current:
            raise BusinessError(
                _(
                    "Cette image est la couverture actuelle de {n} édition(s) : supprimez-la d’abord depuis sa page "
                    "Couverture."
                ).format(n=current)
            )
        history = (
            session.scalar(
                select(func.count())
                .select_from(CoverChange)
                .where((CoverChange.media_id == media.id) | (CoverChange.previous_media_id == media.id))
            )
            or 0
        )
        if apply:
            session.delete(media)  # l'historique garde ses lignes ; leurs liens vers l'image deviennent vides
            for name in (f"{digest}.webp", f"{digest}-v.webp"):
                (media_dir / name).unlink(missing_ok=True)
            audit.record(
                session,
                "media.withdrawn",
                "media_file",
                None,
                user_id=user_id,
                details={"sha256": digest, "motif": reason, "historique": history},
            )
        return MediaWithdrawal(digest, history, apply)


# --- Exemplaires : création et suppression. Fiche, rangement et statuts : modules/holdings/service.py ----------
def code_in_use(session: Session, value: str) -> bool:
    """Un code est pris s'il sert déjà de code-barres, de numéro d'inventaire ou de carte, ou s'il a été retiré (D3)."""
    from biblyo.modules.holdings.models import RetiredCode

    taken = session.scalar(
        select(func.count()).select_from(Copy).where((Copy.barcode == value) | (Copy.inventory_number == value))
    )
    if taken or session.get(RetiredCode, value) is not None:
        return True
    return (
        session.execute(text("SELECT 1 FROM patron_cards WHERE number = :value"), {"value": value}).first() is not None
    )


def _next_barcode(session: Session) -> str:
    """Sans trou ni réemploi : la transaction d'écriture sérialise les appels. Un code saisi à la main n'est jamais
    écrasé : le numéroteur passe au suivant libre."""
    sequence = session.get(NumberSequence, BARCODE_SEQUENCE)
    assert sequence is not None  # amorcée par la révision 0002
    while True:
        sequence.last_value += 1
        value = f"{sequence.prefix}{sequence.last_value:0{sequence.padding}d}"
        if not code_in_use(session, value):
            return value


def _call_number_for(session: Session, edition: Edition) -> str | None:
    """Cote d'un nouvel exemplaire : celle que partagent déjà les exemplaires de l'œuvre, sinon une proposition (D2).

    Elle est enregistrée une fois pour toutes : ni un déplacement ni un changement de tome ne la recalculent.
    """
    from biblyo.modules.holdings import repository as holdings
    from biblyo.modules.holdings.rules import propose_call_number

    existing = session.scalar(
        select(Copy.call_number)
        .join(Edition, Edition.id == Copy.edition_id)
        .where(Edition.work_id == edition.work_id, Copy.call_number.is_not(None))
        .order_by(Copy.id)
        .limit(1)
    )
    return existing or propose_call_number(holdings.call_facts(session, edition.id)) or None


def add_copies(database: Database, edition_id: int, quantity: int, note: str, user_id: int | None) -> list[int]:
    if not 1 <= quantity <= MAX_COPIES_AT_ONCE:
        raise FormErrors({"quantite": _("Indiquez un nombre d’exemplaires entre 1 et 50.")})
    with database.session(write=True) as session:
        record = _get_edition(session, edition_id)
        if record.status != "active":
            raise BusinessError(_("Cette édition est archivée : réactivez-la d’abord."))
        work = _get_work(session, record.work_id)
        if work.status != "active":
            raise BusinessError(_("Cette notice est archivée : réactivez-la d’abord."))
        moment = now_iso()
        call_number = _call_number_for(session, record)
        created: list[int] = []
        for _index in range(quantity):
            copy = Copy(
                public_id=new_public_id(),
                edition_id=record.id,
                barcode=(code := _next_barcode(session)),
                inventory_number=code,
                call_number=call_number,
                status="in_processing",
                notes=_clean(note),
                created_at=moment,
                updated_at=moment,
                created_by=user_id,
                updated_by=user_id,
            )
            session.add(copy)
            session.flush()
            created.append(copy.id)
        _touch_work(work, user_id)
        after_change(session, work)
        audit.record(session, "copy.create", "edition", record.id, user_id=user_id, details={"quantite": quantity})
        return created


#: Ce qui fait l'historique d'un exemplaire : il ne peut plus être supprimé, seulement retiré du fonds.
COPY_HISTORY_ACTIONS = ("copy.shelve", "copy.shelve_scan", "copy.shelve_undo", "copy.unshelve", "copy.status")


class CopyNotFound(BusinessError):
    """L'exemplaire demandé n'existe pas ou plus."""


def copy_has_history(session: Session, copy: Copy) -> bool:
    """Un exemplaire a un historique dès qu'il a été rangé, a changé de statut, ou a été prêté, même une seule fois."""
    if copy.status != "in_processing" or copy.first_shelved_at is not None or copy.location_id is not None:
        return True
    if session.execute(text("SELECT 1 FROM loans WHERE copy_id = :id LIMIT 1"), {"id": copy.id}).first():
        return True
    return bool(
        session.scalar(
            select(func.count())
            .select_from(AuditEntry)
            .where(
                AuditEntry.entity_type == "copy",
                AuditEntry.entity_id == copy.id,
                AuditEntry.action.in_(COPY_HISTORY_ACTIONS),
            )
        )
    )


def delete_copy(database: Database, copy_id: int, user_id: int | None) -> int:
    """Supprime un exemplaire créé par erreur, **sans historique**. Sinon, il se retire du fonds et garde son histoire.

    Ses codes sont retirés pour toujours : ils ne seront jamais réattribués (D3).
    """
    from biblyo.modules.holdings.models import RetiredCode

    with database.session(write=True) as session:
        copy = session.get(Copy, copy_id)
        if copy is None:
            raise CopyNotFound(_("Cet exemplaire n’existe plus."))
        if copy_has_history(session, copy):
            raise BusinessError(
                _("Cet exemplaire a déjà un historique : il ne peut pas être supprimé, seulement retiré du fonds.")
            )
        record = _get_edition(session, copy.edition_id)
        work = _get_work(session, record.work_id)
        moment = now_iso()
        for value, kind in ((copy.barcode, "barcode"), (copy.inventory_number, "inventory")):
            if value and session.get(RetiredCode, value) is None:
                session.add(RetiredCode(value=value, kind=kind, copy_public_id=copy.public_id, retired_at=moment))
                session.flush()
        audit.record(
            session,
            "copy.delete",
            "copy",
            copy.id,
            user_id=user_id,
            details={"code": copy.barcode, "inventaire": copy.inventory_number},
        )
        session.delete(copy)
        session.flush()
        _touch_work(work, user_id)
        after_change(session, work)
        return work.id


# --- Séries -----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SeriesEdit:
    title: str
    status: str = "unknown"
    notes: str = ""


def _validate_series_edit(session: Session, data: SeriesEdit, series_id: int | None = None) -> dict[str, str]:
    errors: dict[str, str] = {}
    label = _clean(data.title)
    key = normalize(label)
    if not label or not key:
        errors["titre"] = _("Indiquez le titre de la série.")
    elif len(label) > MAX_TITLE:
        errors["titre"] = _("Le titre de la série est trop long.")
    else:
        other = session.scalar(select(Series).where(Series.title_norm == key, Series.id != (series_id or 0)))
        if other is not None:
            errors["titre"] = _(
                "Une série porte déjà ce titre. Distinguez-la, par exemple « {titre} (2e série) »."
            ).format(titre=label)
    if data.status not in SERIES_STATUSES:
        errors["statut"] = _("Choisissez un état.")
    return errors


def create_series(database: Database, data: SeriesEdit, user_id: int | None) -> int:
    with database.session(write=True) as session:
        errors = _validate_series_edit(session, data)
        if errors:
            raise FormErrors(errors)
        label = " ".join(data.title.split())
        moment = now_iso()
        series = Series(
            public_id=new_public_id(),
            title=label,
            title_sort=title_sort(label),
            title_norm=normalize(label),
            next_call_sequence=1,
            status=data.status,
            notes=_clean_block(data.notes),
            created_at=moment,
            updated_at=moment,
        )
        session.add(series)
        session.flush()
        audit.record(session, "series.create", "series", series.id, user_id=user_id)
        return series.id


def update_series_record(database: Database, series_id: int, data: SeriesEdit, user_id: int | None) -> None:
    with database.session(write=True) as session:
        series = session.get(Series, series_id)
        if series is None:
            raise BusinessError(_("Cette série n’existe plus."))
        errors = _validate_series_edit(session, data, series_id)
        if errors:
            raise FormErrors(errors)
        label = " ".join(data.title.split())
        series.title = label
        series.title_sort = title_sort(label)
        series.title_norm = normalize(label)
        series.status = data.status
        series.notes = _clean_block(data.notes)
        series.updated_at = now_iso()
        session.flush()
        for work_id in session.scalars(select(WorkSeries.work_id).where(WorkSeries.series_id == series.id)):
            after_change(session, _get_work(session, work_id))
        audit.record(session, "series.update", "series", series.id, user_id=user_id)


def delete_series(database: Database, series_id: int, user_id: int | None) -> None:
    with database.session(write=True) as session:
        series = session.get(Series, series_id)
        if series is None:
            raise BusinessError(_("Cette série n’existe plus."))
        used = session.scalar(select(func.count()).select_from(WorkSeries).where(WorkSeries.series_id == series.id))
        if used:
            raise BusinessError(_("Des notices appartiennent à cette série : elle ne peut pas être supprimée."))
        audit.record(session, "series.delete", "series", series.id, user_id=user_id)
        session.delete(series)


def confirm_volumes(database: Database, series_id: int, first: str, last: str, note: str, user_id: int | None) -> int:
    """Confirme qu'un tome ou une plage de tomes a bien paru. Rend le nombre de tomes nouvellement confirmés."""
    start, end = _parse_range(first, last)
    with database.session(write=True) as session:
        series = session.get(Series, series_id)
        if series is None:
            raise BusinessError(_("Cette série n’existe plus."))
        known = set(
            session.scalars(
                select(SeriesExpectedVolume.volume_number).where(SeriesExpectedVolume.series_id == series.id)
            )
        )
        moment = now_iso()
        added = 0
        number = start
        while number <= end:
            if number not in known:
                session.add(
                    SeriesExpectedVolume(
                        series_id=series.id,
                        volume_number=number,
                        source_note=_clean(note),
                        confirmed_by=user_id,
                        confirmed_at=moment,
                    )
                )
                added += 1
            number += 1
        audit.record(session, "series.confirm", "series", series.id, user_id=user_id, details={"tomes": added})
        return added


def _parse_range(first: str, last: str) -> tuple[float, float]:
    try:
        start = parse_volume(first).number
        end = parse_volume(last).number if last.strip() else start
    except InvalidVolume as exc:
        raise FormErrors({"premier": str(exc)}) from exc
    if start is None or end is None:
        raise FormErrors({"premier": _("Indiquez un numéro de tome.")})
    if end < start:
        raise FormErrors({"dernier": _("Le dernier tome doit suivre le premier.")})
    if end - start > MAX_CONFIRMED_RANGE:
        raise FormErrors({"dernier": _("Cette plage est trop grande.")})
    return start, end


def unconfirm_volume(database: Database, series_id: int, number: float, user_id: int | None) -> None:
    with database.session(write=True) as session:
        session.execute(
            delete(SeriesExpectedVolume).where(
                SeriesExpectedVolume.series_id == series_id, SeriesExpectedVolume.volume_number == number
            )
        )
        audit.record(session, "series.unconfirm", "series", series_id, user_id=user_id)


# --- Recherches enregistrées, propres à un compte ------------------------------------------------------------------
MAX_SAVED_SEARCHES = 50
MAX_SEARCH_NAME = 60


def save_search(database: Database, user_id: int, name: str, criteria: dict[str, str]) -> None:
    """Un nom déjà pris par ce compte voit ses critères remplacés : enregistrer à nouveau, c'est mettre à jour."""
    label = _clean(name)
    if not label:
        raise BusinessError(_("Indiquez un nom pour cette recherche."))
    if len(label) > MAX_SEARCH_NAME:
        raise BusinessError(_("Ce nom est trop long."))
    with database.session(write=True) as session:
        existing = session.scalar(select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.name == label))
        if existing is not None:
            existing.criteria = json.dumps(criteria, ensure_ascii=False, sort_keys=True)
            return
        count = session.scalar(select(func.count()).select_from(SavedSearch).where(SavedSearch.user_id == user_id))
        if (count or 0) >= MAX_SAVED_SEARCHES:
            raise BusinessError(_("Vous avez atteint le nombre maximum de recherches enregistrées."))
        session.add(
            SavedSearch(
                user_id=user_id,
                name=label,
                criteria=json.dumps(criteria, ensure_ascii=False, sort_keys=True),
                created_at=now_iso(),
            )
        )


def delete_search(database: Database, user_id: int, search_id: int) -> bool:
    """Rend `False` si la recherche n'existe pas ou appartient à un autre compte : aucune différence visible."""
    with database.session(write=True) as session:
        saved = session.scalar(select(SavedSearch).where(SavedSearch.id == search_id, SavedSearch.user_id == user_id))
        if saved is None:
            return False
        session.delete(saved)
        return True
