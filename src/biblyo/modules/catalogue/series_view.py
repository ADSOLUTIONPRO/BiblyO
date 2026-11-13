# SPDX-License-Identifier: EUPL-1.2
"""Lecture des séries : frise à trois états, détail par tome, doublons physiques.

Un numéro absent n'est jamais un tome manquant : seul un tome confirmé l'est. Tout le reste reste « à vérifier ».
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biblyo.core.i18n import _
from biblyo.core.text import normalize
from biblyo.modules.catalogue.models import Copy, Edition, Publisher, Series, SeriesExpectedVolume, Work, WorkSeries
from biblyo.modules.catalogue.rules import (
    ACTIVE_COPY_EXCLUDED,
    SERIES_STATUSES,
    cover_color,
    series_strip,
)
from biblyo.modules.holdings.availability import for_works
from biblyo.modules.holdings.rules import WorkCounts

FILTERS: dict[str, str] = {
    "": _("Toutes"),
    "incompletes": _("Incomplètes"),
    "a-verifier": _("À vérifier"),
    "completes": _("Complètes"),
    "doublons": _("Doublons et éditions"),
}
PAGE_SIZE = 40


@dataclass(frozen=True)
class StripItem:
    """Une case de la frise, prête pour le gabarit `series_strip`."""

    number: float | None
    state: str
    label: str
    title: str
    href: str = ""
    badge: str = ""
    out: bool = False  # possédé, mais tous ses exemplaires sont sortis : jamais « manquant » pour autant


@dataclass(frozen=True)
class _Link:
    work_id: int
    number: float | None
    label: str | None
    title: str


@dataclass(frozen=True)
class Tally:
    """Ce qui se compte dans une série, sans construire sa frise : de quoi la classer pour un filtre.

    `has_duplicates` vaut `None` quand il n'a pas été calculé : il demande les éditions et les exemplaires.
    """

    present: int
    missing: int
    unverified: int
    last_number: int
    has_duplicates: bool | None = None

    @property
    def is_complete(self) -> bool:
        """Complète : aucun manque confirmé, **et aucun numéro incertain**."""
        return self.present > 0 and self.missing == 0 and self.unverified == 0

    @property
    def state(self) -> str:
        """complete, uncertain (des numéros restent à vérifier), missing (manques confirmés seulement), empty."""
        if self.is_complete:
            return "complete"
        if self.unverified:
            return "uncertain"
        if self.missing:
            return "missing"
        return "empty"

    def matches(self, filter_code: str) -> bool:
        if filter_code == "doublons":
            if self.has_duplicates is None:
                raise ValueError("Doublons non calculés pour ce classement")
            return self.has_duplicates
        return {
            "incompletes": self.missing > 0,
            "a-verifier": self.unverified > 0,
            "completes": self.is_complete,
        }.get(filter_code, True)


LinkFact = tuple[int, float | None, str | None]  # œuvre, numéro, libellé


def tally(links: Iterable[LinkFact], confirmed: Iterable[float], is_multi: Callable[[int], bool] | None) -> Tally:
    """Compte une série comme le fait sa frise (`rules.series_strip`), sans fabriquer ses cases.

    Les numéros entiers de 1 au plus grand numéro entier présent ou confirmé, absents des deux, sont « à vérifier ».
    """
    by_number: dict[float, list[int]] = defaultdict(list)
    labelled: dict[str, list[int]] = defaultdict(list)
    for work_id, number, label in links:
        if number is not None:
            by_number[number].append(work_id)
        elif label:
            labelled[label].append(work_id)
    known = set(by_number)
    expected = set(confirmed)
    whole = {number for number in known | expected if float(number).is_integer()}
    top = int(max(whole)) if whole else 0
    duplicates = None
    if is_multi is not None:
        groups = (*by_number.values(), *labelled.values())
        duplicates = any(len(group) > 1 or is_multi(group[0]) for group in groups)
    return Tally(
        present=len(known) + len(labelled),
        missing=len(expected - known),
        unverified=top - sum(1 for number in whole if number >= 1),
        last_number=top,
        has_duplicates=duplicates,
    )


@dataclass(frozen=True)
class SeriesCard:
    id: int
    title: str
    status: str
    status_label: str
    notes: str | None
    color: int
    title_count: int
    copies: int
    counts: Tally
    strip: tuple[StripItem, ...]

    @property
    def present(self) -> int:
        return self.counts.present

    @property
    def missing(self) -> int:
        return self.counts.missing

    @property
    def unverified(self) -> int:
        return self.counts.unverified

    @property
    def has_duplicates(self) -> bool:
        return bool(self.counts.has_duplicates)

    @property
    def is_complete(self) -> bool:
        return self.counts.is_complete

    @property
    def state(self) -> str:
        return self.counts.state

    @property
    def percent(self) -> int:
        """Part des tomes attendus présents. Un numéro seulement « à vérifier » n'y entre pas."""
        expected = self.present + self.missing
        return round(100 * self.present / expected) if expected else 0

    @property
    def ring_module(self) -> str:
        """Vert pour une série complète, bleu pour des manques confirmés, gris en pointillés s'il reste un doute."""
        return {"complete": "ok", "missing": "ser"}.get(self.state, "")

    @property
    def is_uncertain(self) -> bool:
        return self.state == "uncertain"

    @property
    def percent_caption(self) -> str:
        """Ce que mesure le pourcentage. Dit explicitement quand des numéros restent incertains."""
        if self.present + self.missing == 0:
            return _("Aucun tome numéroté confirmé")
        if self.unverified == 1:
            return _("{percent} % des tomes confirmés · 1 numéro à vérifier, non compté").format(percent=self.percent)
        if self.unverified:
            return _("{percent} % des tomes confirmés · {count} numéros à vérifier, non comptés").format(
                percent=self.percent, count=self.unverified
            )
        return _("{percent} % des tomes attendus").format(percent=self.percent)

    @property
    def complete_label(self) -> str:
        """Ce que « complète » veut dire ici : rien ne manque jusqu'au dernier tome connu. La suite peut exister."""
        if self.counts.last_number:
            return _("Complète jusqu’au t. {number}").format(number=self.counts.last_number)
        return _("Complète")

    @property
    def unverified_label(self) -> str:
        if self.unverified == 1:
            return _("1 numéro à vérifier")
        return _("{count} numéros à vérifier").format(count=self.unverified)

    def matches(self, filter_code: str) -> bool:
        return self.counts.matches(filter_code)


def _links_by_series(session: Session, series_ids: list[int] | None = None) -> dict[int, list[_Link]]:
    links: dict[int, list[_Link]] = defaultdict(list)
    statement = (
        select(WorkSeries.series_id, Work.id, WorkSeries.volume_number, WorkSeries.volume_label, Work.title)
        .join(Work, Work.id == WorkSeries.work_id)
        .where(Work.status == "active")
    )
    if series_ids is not None:
        statement = statement.where(WorkSeries.series_id.in_(series_ids))
    for series_id, work_id, number, label, title in session.execute(statement):
        links[series_id].append(_Link(work_id, number, label, title))
    return links


def _confirmed_by_series(session: Session, series_ids: list[int] | None = None) -> dict[int, dict[float, str | None]]:
    confirmed: dict[int, dict[float, str | None]] = defaultdict(dict)
    statement = select(
        SeriesExpectedVolume.series_id, SeriesExpectedVolume.volume_number, SeriesExpectedVolume.source_note
    )
    if series_ids is not None:
        statement = statement.where(SeriesExpectedVolume.series_id.in_(series_ids))
    for series_id, number, note in session.execute(statement):
        confirmed[series_id][number] = note
    return confirmed


def _in_series(series_ids: list[int] | None) -> Any:
    statement = select(WorkSeries.work_id)
    if series_ids is not None:
        statement = statement.where(WorkSeries.series_id.in_(series_ids))
    return statement


def _edition_counts(session: Session, series_ids: list[int] | None = None) -> dict[int, int]:
    statement = (
        select(Edition.work_id, func.count())
        .where(Edition.status == "active", Edition.work_id.in_(_in_series(series_ids)))
        .group_by(Edition.work_id)
    )
    return dict(session.execute(statement).all())


def _copy_counts(session: Session, series_ids: list[int] | None = None) -> dict[int, int]:
    statement = (
        select(Edition.work_id, func.count())
        .join(Copy, Copy.edition_id == Edition.id)
        .where(Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)), Edition.work_id.in_(_in_series(series_ids)))
        .group_by(Edition.work_id)
    )
    return dict(session.execute(statement).all())


def _item_badge(works: list[_Link], editions: dict[int, int], copies: dict[int, int]) -> str:
    """Le chiffre posé sur un tome : notices en double, sinon exemplaires, sinon éditions."""
    if len(works) > 1:
        return str(len(works))
    only = works[0].work_id
    if copies.get(only, 0) > 1:
        return str(copies[only])
    if editions.get(only, 0) > 1:
        return str(editions[only])
    return ""


def _all_out(works: list[_Link], states: Mapping[int, WorkCounts]) -> bool:
    """Vrai si ce tome a des exemplaires et qu'aucun n'est sur place : tous sont sortis."""
    found = [states[link.work_id] for link in works if link.work_id in states]
    total = sum(item.total for item in found)
    return total > 0 and sum(item.out for item in found) == total


def _present_item(
    number: float | None, label: str, series_id: int, works: list[_Link], badge: str, out: bool = False
) -> StripItem:
    title = _("Tome {label} : présent").format(label=label)
    if out:
        title = _("Tome {label} : présent, tous ses exemplaires sont sortis").format(label=label)
    if len(works) > 1:
        title = _("Tome {label} : {count} notices").format(label=label, count=len(works))
        return StripItem(number, "present", label, title, f"/catalogue/series/{series_id}#tome-{label}", badge, out)
    return StripItem(number, "present", label, title, f"/catalogue/notices/{works[0].work_id}", badge, out)


def _build_card(
    series: Series,
    links: list[_Link],
    confirmed: dict[float, str | None],
    editions: dict[int, int],
    copies: dict[int, int],
    states: Mapping[int, WorkCounts] | None = None,
) -> SeriesCard:
    states = states or {}
    by_number: dict[float, list[_Link]] = defaultdict(list)
    labelled: dict[str, list[_Link]] = defaultdict(list)
    for link in links:
        if link.number is not None:
            by_number[link.number].append(link)
        elif link.label:
            labelled[link.label].append(link)
    strip_items: list[StripItem] = []
    for state in series_strip(set(by_number), set(confirmed)):
        if state.state == "present":
            works = by_number[state.number]
            badge = _item_badge(works, editions, copies)
            out = _all_out(works, states)
            strip_items.append(_present_item(state.number, state.label, series.id, works, badge, out))
        elif state.state == "missing":
            title = _("Tome {label} : manquant confirmé").format(label=state.label)
            strip_items.append(StripItem(state.number, "missing", state.label, title))
        else:
            title = _("Tome {label} : à vérifier, sa parution n’est pas confirmée").format(label=state.label)
            strip_items.append(StripItem(state.number, "unverified", state.label, title))
    for label, works in sorted(labelled.items()):
        badge = _item_badge(works, editions, copies)
        strip_items.append(_present_item(None, label, series.id, works, badge, _all_out(works, states)))
    counts = tally(
        ((link.work_id, link.number, link.label) for link in links),
        confirmed,
        lambda work_id: editions.get(work_id, 0) > 1 or copies.get(work_id, 0) > 1,
    )
    return SeriesCard(
        id=series.id,
        title=series.title,
        status=series.status,
        status_label=SERIES_STATUSES.get(series.status, series.status),
        notes=series.notes,
        color=cover_color(series.title),
        title_count=len(links),
        copies=sum(copies.get(link.work_id, 0) for link in links),
        counts=counts,
        strip=tuple(strip_items),
    )


@dataclass(frozen=True)
class SeriesListing:
    cards: list[SeriesCard]
    total: int
    incomplete: int
    page: int
    pages: int


def _cards_of(session: Session, series_ids: list[int]) -> list[SeriesCard]:
    """Cartes complètes, frise comprise, des séries demandées et dans leur ordre."""
    if not series_ids:
        return []
    found = {series.id: series for series in session.scalars(select(Series).where(Series.id.in_(series_ids)))}
    links = _links_by_series(session, series_ids)
    confirmed = _confirmed_by_series(session, series_ids)
    editions = _edition_counts(session, series_ids)
    copies = _copy_counts(session, series_ids)
    states = for_works(session, [link.work_id for items in links.values() for link in items])
    return [
        _build_card(found[series_id], links.get(series_id, []), confirmed.get(series_id, {}), editions, copies, states)
        for series_id in series_ids
        if series_id in found
    ]


@dataclass(frozen=True)
class SeriesFacts:
    """Ce qu'il faut lire pour classer toutes les séries : quelques colonnes, aucun titre, aucune frise."""

    links: dict[int, list[LinkFact]]
    confirmed: dict[int, set[float]]
    multi_works: set[int] | None


def load_facts(session: Session, *, with_duplicates: bool) -> SeriesFacts:
    """Requêtes du classement. Les éditions et les exemplaires ne sont lus que pour le filtre « Doublons »."""
    archived = select(Work.id).where(Work.status == "archived")  # une œuvre est active ou archivée
    links: dict[int, list[LinkFact]] = defaultdict(list)
    for series_id, work_id, number, label in session.execute(
        select(WorkSeries.series_id, WorkSeries.work_id, WorkSeries.volume_number, WorkSeries.volume_label).where(
            WorkSeries.work_id.not_in(archived)
        )
    ):
        links[series_id].append((work_id, number, label))
    confirmed: dict[int, set[float]] = defaultdict(set)
    for series_id, number in session.execute(
        select(SeriesExpectedVolume.series_id, SeriesExpectedVolume.volume_number)
    ):
        confirmed[series_id].add(number)
    multi: set[int] | None = None
    if with_duplicates:
        several_editions = (
            select(Edition.work_id).where(Edition.status == "active").group_by(Edition.work_id).having(func.count() > 1)
        )
        several_copies = (
            select(Edition.work_id)
            .join(Copy, Copy.edition_id == Edition.id)
            .where(Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
            .group_by(Edition.work_id)
            .having(func.count() > 1)
        )
        multi = set(session.execute(several_editions.union(several_copies)).scalars())
    return SeriesFacts(links, confirmed, multi)


def classify(series_ids: list[int], facts: SeriesFacts) -> dict[int, Tally]:
    """Calcul seul, sans requête : les compteurs de chaque série."""
    multi = facts.multi_works
    is_multi = None if multi is None else multi.__contains__
    return {
        series_id: tally(facts.links.get(series_id, ()), facts.confirmed.get(series_id, ()), is_multi)
        for series_id in series_ids
    }


def ordered_series_ids(session: Session, term: str = "") -> list[int]:
    statement = select(Series.id).order_by(Series.title_sort, Series.id)
    if term:
        statement = statement.where(Series.title_norm.contains(term, autoescape=True))
    return list(session.scalars(statement))


def list_series(session: Session, query: str = "", filter_code: str = "", page: int = 1) -> SeriesListing:
    term = normalize(query)
    if not filter_code:  # sans filtre, seule la page affichée est lue
        ordered = select(Series.id).order_by(Series.title_sort, Series.id)
        if term:
            ordered = ordered.where(Series.title_norm.contains(term, autoescape=True))
        total = session.scalar(select(func.count()).select_from(ordered.order_by(None).subquery())) or 0
        pages = max(1, -(-total // PAGE_SIZE))
        current = min(max(page, 1), pages)
        shown = list(session.scalars(ordered.limit(PAGE_SIZE).offset((current - 1) * PAGE_SIZE)))
        return SeriesListing(_cards_of(session, shown), total, incomplete_count(session, term), current, pages)
    series_ids = ordered_series_ids(session, term)
    counts = classify(series_ids, load_facts(session, with_duplicates=filter_code == "doublons"))
    incomplete = sum(1 for item in counts.values() if item.missing > 0)
    matching = [series_id for series_id in series_ids if counts[series_id].matches(filter_code)]
    pages = max(1, -(-len(matching) // PAGE_SIZE))
    current = min(max(page, 1), pages)
    start = (current - 1) * PAGE_SIZE
    cards = _cards_of(session, matching[start : start + PAGE_SIZE])
    return SeriesListing(cards, len(matching), incomplete, current, pages)


@dataclass(frozen=True)
class EditionLine:
    label: str
    is_primary: bool
    copies: int


@dataclass(frozen=True)
class VolumeRow:
    """Une ligne du détail : un tome connu, ou un tome manquant confirmé, ou un numéro à vérifier."""

    number: float | None
    label: str
    state: str
    anchor: str
    note: str | None = None
    work_id: int | None = None
    title: str = ""
    editions: tuple[EditionLine, ...] = ()
    copies: int = 0
    duplicate: bool = False


def _editions_of(session: Session, work_ids: list[int]) -> tuple[dict[int, list[EditionLine]], dict[int, int]]:
    copy_counts: dict[int, int] = dict(
        session.execute(
            select(Copy.edition_id, func.count())
            .join(Edition, Edition.id == Copy.edition_id)
            .where(Edition.work_id.in_(work_ids), Copy.status.not_in(tuple(ACTIVE_COPY_EXCLUDED)))
            .group_by(Copy.edition_id)
        ).all()
    )
    lines: dict[int, list[EditionLine]] = defaultdict(list)
    totals: dict[int, int] = defaultdict(int)
    for work_id, edition_id, is_primary, year, statement, publisher in session.execute(
        select(
            Edition.work_id,
            Edition.id,
            Edition.is_primary,
            Edition.publication_year,
            Edition.edition_statement,
            Publisher.name,
        )
        .outerjoin(Publisher, Publisher.id == Edition.publisher_id)
        .where(Edition.work_id.in_(work_ids), Edition.status == "active")
        .order_by(Edition.is_primary.desc(), Edition.publication_year.desc(), Edition.id)
    ):
        parts = [part for part in (publisher, str(year) if year else "", statement) if part]
        count = copy_counts.get(edition_id, 0)
        lines[work_id].append(EditionLine(", ".join(parts) or _("Édition sans détail"), is_primary, count))
        totals[work_id] += count
    return lines, totals


def _detail_rows(
    links: list[_Link],
    confirmed: dict[float, str | None],
    editions: dict[int, list[EditionLine]],
    totals: dict[int, int],
) -> list[VolumeRow]:
    by_number: dict[float, list[_Link]] = defaultdict(list)
    labelled: dict[str, list[_Link]] = defaultdict(list)
    unnumbered: list[_Link] = []
    for link in links:
        if link.number is not None:
            by_number[link.number].append(link)
        elif link.label:
            labelled[link.label].append(link)
        else:
            unnumbered.append(link)

    def row(number: float | None, label: str, link: _Link, siblings: int) -> VolumeRow:
        return VolumeRow(
            number=number,
            label=label,
            state="present",
            anchor=f"tome-{label}" if label else "",
            work_id=link.work_id,
            title=link.title,
            editions=tuple(editions.get(link.work_id, ())),
            copies=totals.get(link.work_id, 0),
            duplicate=siblings > 1,
        )

    rows: list[VolumeRow] = []
    for state in series_strip(set(by_number), set(confirmed)):
        if state.state == "present":
            siblings = by_number[state.number]
            rows += [row(state.number, state.label, link, len(siblings)) for link in siblings]
        elif state.state == "missing":
            rows.append(
                VolumeRow(state.number, state.label, "missing", f"tome-{state.label}", confirmed.get(state.number))
            )
        else:
            rows.append(VolumeRow(state.number, state.label, "unverified", f"tome-{state.label}"))
    for label, works in sorted(labelled.items()):
        rows += [row(None, label, link, len(works)) for link in works]
    rows += [row(None, "", link, 1) for link in unnumbered]
    return rows


@dataclass(frozen=True)
class SeriesDetail:
    card: SeriesCard
    rows: list[VolumeRow]
    in_use: bool


def detail(session: Session, series_id: int) -> SeriesDetail | None:
    series = session.get(Series, series_id)
    if series is None:
        return None
    only = [series_id]
    links = _links_by_series(session, only).get(series_id, [])
    confirmed = _confirmed_by_series(session, only).get(series_id, {})
    work_ids = [link.work_id for link in links]
    states = for_works(session, work_ids)
    card = _build_card(series, links, confirmed, _edition_counts(session, only), _copy_counts(session, only), states)
    editions, totals = _editions_of(session, work_ids)
    in_use = bool(
        session.scalar(select(func.count()).select_from(WorkSeries).where(WorkSeries.series_id == series_id))
    )
    return SeriesDetail(card, _detail_rows(links, confirmed, editions, totals), in_use)


def incomplete_count(session: Session, term: str = "") -> int:
    """Séries ayant au moins un tome confirmé absent du fonds actif : l'indicateur du tableau de bord."""
    present = (
        select(WorkSeries.work_id)
        .join(Work, Work.id == WorkSeries.work_id)
        .where(
            WorkSeries.series_id == SeriesExpectedVolume.series_id,
            WorkSeries.volume_number == SeriesExpectedVolume.volume_number,
            Work.status == "active",
        )
        .exists()
    )
    statement = select(func.count(func.distinct(SeriesExpectedVolume.series_id))).where(~present)
    if term:
        statement = statement.join(Series, Series.id == SeriesExpectedVolume.series_id).where(
            Series.title_norm.contains(term, autoescape=True)
        )
    return session.scalar(statement) or 0
