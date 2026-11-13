# SPDX-License-Identifier: EUPL-1.2
"""Du formulaire au service, et du service au formulaire. Les noms de champs sont ceux des gabarits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from biblyo.modules.catalogue import repository as repo
from biblyo.modules.catalogue.repository import ContributorView, EditionView, NoticeView
from biblyo.modules.catalogue.service import (
    ContentInput,
    ContributorInput,
    EditionInput,
    IdentityInput,
    SeriesInput,
)

WORK_PREFIX = "contributeur"
EDITION_PREFIX = "edition_contributeur"
PREFIXES = frozenset({WORK_PREFIX, EDITION_PREFIX})


@dataclass(frozen=True)
class PersonChoice:
    id: int
    label: str


def first(form: FormData, name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


def strings(form: FormData, name: str) -> list[str]:
    return [value for value in form.getlist(name) if isinstance(value, str)]


def optional_int(text: str) -> int | None:
    return int(text) if text.strip().isdigit() else None


def contributors_from(form: FormData, prefix: str) -> tuple[ContributorInput, ...]:
    names = strings(form, f"{prefix}_nom")
    roles = strings(form, f"{prefix}_role")
    choices = strings(form, f"{prefix}_choix")
    dates = strings(form, f"{prefix}_dates")

    def at(values: list[str], index: int, default: str = "") -> str:
        return values[index] if index < len(values) else default

    return tuple(
        ContributorInput(name=name, role=at(roles, i, "author"), choice=at(choices, i), dates=at(dates, i))
        for i, name in enumerate(names)
    )


def identity_from(form: FormData) -> IdentityInput:
    return IdentityInput(
        title=first(form, "titre"),
        subtitle=first(form, "sous_titre"),
        original_title=first(form, "titre_original"),
        document_type_id=optional_int(first(form, "type")),
        section_id=optional_int(first(form, "section")),
        audience=first(form, "public"),
        contributors=contributors_from(form, WORK_PREFIX),
    )


def series_from(form: FormData) -> SeriesInput:
    return SeriesInput(title=first(form, "serie"), volume=first(form, "tome"))


def content_from(form: FormData) -> ContentInput:
    return ContentInput(
        summary=first(form, "resume"),
        notes=first(form, "notes"),
        categories=first(form, "categories"),
        genres=first(form, "genres"),
        keywords=first(form, "mots_cles"),
    )


def edition_from(form: FormData) -> EditionInput:
    return EditionInput(
        isbn=first(form, "isbn"),
        publisher=first(form, "editeur"),
        collection=first(form, "collection"),
        collection_number=first(form, "numero_collection"),
        year=first(form, "annee"),
        date=first(form, "date_publication"),
        statement=first(form, "mention_edition"),
        language=first(form, "langue") or "fre",
        pages=first(form, "pages"),
        physical_note=first(form, "description_physique"),
        title_variant=first(form, "titre_edition"),
        notes=first(form, "notes_edition"),
        contributors=contributors_from(form, EDITION_PREFIX),
    )


def form_version(form: FormData) -> int | None:
    return optional_int(first(form, "version"))


def person_choices(session: Session, name: str) -> list[PersonChoice]:
    """Les personnes de ce nom, avec de quoi les distinguer. Vide si le nom est nouveau."""
    people = repo.agents_named(session, name) if name.strip() else []
    counts = repo.agent_work_counts(session, [person.id for person in people])
    choices = []
    for person in people:
        details = [person.dates] if person.dates else []
        details.append(f"{counts.get(person.id, 0)} notice(s)")
        choices.append(PersonChoice(person.id, f"{person.display_name} ({', '.join(details)})"))
    return choices


def rows_from_form(session: Session, form: FormData, prefix: str, *, spare: int = 1) -> list[dict[str, Any]]:
    """Lignes à réafficher après une erreur : la saisie, et le choix entre homonymes s'il y a lieu."""
    rows: list[dict[str, Any]] = []
    for row in contributors_from(form, prefix):
        rows.append(
            {
                "nom": row.name,
                "role": row.role,
                "choix": row.choice,
                "dates": row.dates,
                "matches": person_choices(session, row.name),
            }
        )
    return _with_spare(rows, spare)


def rows_from_people(session: Session, people: tuple[ContributorView, ...], *, spare: int = 1) -> list[dict[str, Any]]:
    rows = [
        {
            "nom": person.name,
            "role": person.role,
            "choix": str(person.agent_id),
            "dates": person.dates or "",
            "matches": person_choices(session, person.name),
        }
        for person in people
    ]
    return _with_spare(rows, spare)


def _with_spare(rows: list[dict[str, Any]], spare: int) -> list[dict[str, Any]]:
    blank = {"nom": "", "role": "author", "choix": "", "dates": "", "matches": []}
    named = [row for row in rows if row["nom"].strip()]
    return named + [dict(blank) for _index in range(spare)]


def form_values(form: FormData) -> dict[str, str]:
    """Saisie à réafficher : champs simples seulement, les listes passent par `rows_from_form`."""
    return {key: value for key, value in form.items() if isinstance(value, str)}


def identity_values(notice: NoticeView) -> dict[str, str]:
    return {
        "titre": notice.title,
        "sous_titre": notice.subtitle or "",
        "titre_original": notice.original_title or "",
        "type": str(notice.document_type.id),
        "section": str(notice.section.id) if notice.section else "",
        "public": notice.audience or "",
    }


def series_values(notice: NoticeView) -> dict[str, str]:
    return {
        "serie": notice.series.title if notice.series else "",
        "tome": notice.series.volume_text if notice.series else "",
    }


def content_values(notice: NoticeView) -> dict[str, str]:
    return {
        "resume": notice.summary or "",
        "notes": notice.notes or "",
        "categories": ", ".join(notice.categories),
        "genres": ", ".join(notice.genres),
        "mots_cles": ", ".join(notice.keywords),
    }


def edition_values(edition: EditionView) -> dict[str, str]:
    return {
        "isbn": edition.isbn_text,
        "editeur": edition.publisher.label if edition.publisher else "",
        "collection": edition.collection.label if edition.collection else "",
        "numero_collection": edition.collection_number or "",
        "annee": str(edition.year) if edition.year else "",
        "date_publication": edition.date or "",
        "mention_edition": edition.statement or "",
        "langue": edition.language,
        "pages": str(edition.pages) if edition.pages else "",
        "description_physique": edition.physical_note or "",
        "titre_edition": edition.title_variant or "",
        "notes_edition": edition.notes or "",
    }
