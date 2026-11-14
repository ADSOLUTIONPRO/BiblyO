# SPDX-License-Identifier: EUPL-1.2
"""Assistant d'enrichissement (L7) : une recherche donne des éditions proposées, chacune avec sa vraie couverture.

Une seule recherche par livre, celle du service (`service.search`), avec tous ses niveaux, série et tome compris. Ses
propositions, regroupées par édition, deviennent des cartes : une miniature réellement servie par la source (jamais
une image générique), une indication compréhensible (« Même édition », « Édition probable », « À vérifier »,
« Autre édition »), ce qui concorde, ce qui diffère, la source et ses conditions.

Couvertures, sans nouvelle recherche de notices :

- BnF : l'image d'une notice qui l'annonce (zone 856), demandée par ARK puis EAN ; une notice du Sudoc y mène par
  l'ARK qu'elle cite, si la notice BnF correspondante annonce une image.
- Open Library (si l'administrateur l'a activée, avec une adresse de contact) : un seul appel, pour les ISBN des
  éditions proposées et pour l'œuvre. Une image rattachée à l'ISBN d'une carte va sur cette carte ; une image
  « d'œuvre » est proposée à part, toujours comme « Autre édition — couverture à vérifier », jamais pour un autre tome
  ni pour une intégrale. Jamais en lot (`enrichment.batch` ne l'appelle pas).

Chaque image est demandée par biblyO, contrôlée et réencodée (`cover_search.preview`) avant d'être montrée : une image
indisponible est dite indisponible, sans vignette cassée. Rien n'est enregistré ici.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select

from biblyo.core.db import Database
from biblyo.core.i18n import _
from biblyo.modules.enrichment import cover_search, covers, matching
from biblyo.modules.enrichment.connectors.base import Candidate
from biblyo.modules.enrichment.models import EnrichmentProposal, EnrichmentSearch
from biblyo.modules.enrichment.normalize import similarity, title_key
from biblyo.modules.enrichment.service import candidate_of
from biblyo.modules.enrichment.sources import SourceHub

MAIN_CARDS = 3
MAX_CARDS = 12
MAX_BNF_IMAGES = 8
MAX_OPEN_LIBRARY_ISBNS = 4
MAX_OPEN_LIBRARY_EXTRA = 4

RELIABILITY: dict[str, tuple[str, str, str]] = {
    # code : (libellé, ton, explication en langage courant)
    "same_edition": (
        _("Même édition"),
        "ok",
        _("Même ISBN que votre livre : c'est votre édition."),
    ),
    "probable": (
        _("Édition probable"),
        "due",
        _("Titre, auteur, éditeur et tome concordent, mais aucun ISBN ne le confirme : vérifiez sur le livre."),
    ),
    "check": (
        _("À vérifier"),
        "due",
        _("Ressemble à votre livre, mais un élément manque ou plusieurs éditions sont possibles."),
    ),
    "other_edition": (
        _("Autre édition"),
        "late",
        _("Le même ouvrage, mais pas votre édition : autre éditeur, autre année ou autre ISBN."),
    ),
}
OTHER_EDITION_COVER = _("Autre édition — couverture à vérifier")
COVER_STATES: dict[str, str] = {
    "found": _("Couverture trouvée"),
    "announced_missing": _("La notice annonce une couverture, mais le service ne la fournit pas en ce moment."),
    "none": _("La source n'a pas d'image pour cette édition. Vous pourrez ajouter votre propre photo."),
}


@dataclass(frozen=True)
class CoverOffer:
    """Une image réellement servie, prête à être montrée, avec ce qu'elle représente."""

    source: str  # bnf, openlibrary
    reference: str  # ARK, numéro d'image Open Library
    ean: str
    match: str  # catalogue.covers.MATCH_LABELS
    edition_text: str
    notice_url: str
    title: str = ""

    @property
    def needs_check(self) -> bool:
        return self.match in cover_search.NEEDS_CONFIRMATION


@dataclass(frozen=True)
class Card:
    proposal_id: int
    reliability: str
    title: str
    subtitle: str
    authors: str
    publisher: str
    year: str
    isbn: str
    series: str
    pages: str
    sources: tuple[str, ...]
    notice_urls: tuple[tuple[str, str], ...]
    agreeing: tuple[str, ...]
    differing: tuple[str, ...]
    score: int
    evidence: int
    level: int
    cover: CoverOffer | None
    cover_state: str
    several: bool = False  # plusieurs éditions sont possibles (classe « multiple »)

    @property
    def label(self) -> tuple[str, str, str]:
        return RELIABILITY[self.reliability]


@dataclass
class Suggestions:
    search_id: int
    outcome: str
    cards: list[Card] = field(default_factory=list)
    extra_covers: list[CoverOffer] = field(default_factory=list)  # images d'autres éditions (Open Library)
    excluded: int = 0
    open_library: str = "disabled"  # disabled, found, none, unavailable

    @property
    def main(self) -> list[Card]:
        return self.cards[:MAIN_CARDS]

    @property
    def others(self) -> list[Card]:
        return self.cards[MAIN_CARDS:]


def reliability(match_class: str, explanation: dict[str, object]) -> str:
    """L'indication montrée à la place du score. Seul un ISBN identique fait « Même édition »."""
    if explanation.get("isbn_equal") and match_class in ("exact", "strong"):
        return "same_edition"
    if not explanation.get("edition_usable", True):
        return "other_edition"
    if match_class in ("exact", "strong"):
        return "probable"
    return "check"


def _people(candidate: Candidate) -> str:
    return ", ".join(p.name for p in candidate.work.contributors[:4])


def _series(candidate: Candidate) -> str:
    """Série et tome, jamais une collection d'éditeur : « Folio junior » n° 100 (zone 225) n'est pas un tome."""
    series = candidate.work.series.value if candidate.work.series else ""
    if candidate.work.series and candidate.work.series.origin.startswith("225"):
        series = ""
    volume = candidate.work.volume.value if candidate.work.volume else None
    if candidate.work.volume and candidate.work.volume.origin.startswith("225"):
        volume = None
    if volume is None:
        return series
    number = str(int(volume)) if float(volume).is_integer() else str(volume)
    return f"{series}, tome {number}" if series else _("tome {n}").format(n=number)


def _edition_text(candidate: Candidate) -> str:
    edition = candidate.edition
    parts = [
        edition.publisher.value if edition.publisher else "",
        edition.year.raw if edition.year else "",
        _("ISBN {isbn}").format(isbn=edition.isbn13[0]) if edition.isbn13 else "",
    ]
    return ", ".join(p for p in parts if p) or _("édition non précisée")


def _cover_match(reliability_code: str) -> str:
    return {
        "same_edition": "same_edition",
        "probable": "presumed",
        "check": "undetermined",
        "other_edition": "other_edition",
    }[reliability_code]


def _bnf_cover(
    hub: SourceHub, previews: cover_search.Previews, group: list[Candidate], announced_arks: set[str], code: str
) -> tuple[CoverOffer | None, str]:
    """L'image BnF d'une édition : celle d'une notice BnF du groupe qui l'annonce, ou de l'ARK cité par le Sudoc."""
    for candidate in group:
        ark = covers.ark_for(candidate)
        if not ark or not (candidate.has_cover or ark in announced_arks):
            continue
        ean = (candidate.edition.ean or candidate.edition.isbn13 or ("",))[0]
        if cover_search.preview(hub, previews, "bnf", ark, ean, "vignette") is None:
            return None, "announced_missing"
        return (
            CoverOffer(
                "bnf",
                ark,
                ean,
                _cover_match(code),
                _edition_text(candidate),
                covers.public_url(candidate),
                candidate.work.title.value,
            ),
            "found",
        )
    return None, "none"


_NUMBER = re.compile(r"(?<![0-9])([0-9]{1,4})(?![0-9])")
_INTEGRAL = re.compile(r"\bint[eé]grale?s?\b", re.IGNORECASE)


def _plausible_work_image(fiche: matching.LocalFiche, title: str) -> bool:
    """Une image « d'œuvre » d'Open Library n'est proposée que pour le même ouvrage : même titre (ou titre abrégé),
    jamais une intégrale si la fiche n'en est pas une, jamais un autre numéro de tome."""
    if _INTEGRAL.search(title) and not _INTEGRAL.search(fiche.title):
        return False
    if fiche.volume is not None:
        numbers = {float(n) for n in _NUMBER.findall(title)}
        if numbers and all(abs(n - fiche.volume) >= 0.01 for n in numbers):
            return False
    mine, theirs = title_key(fiche.title), title_key(title)
    if not mine or not theirs:
        return False
    if similarity(fiche.title, title) >= matching.TITLE_CLOSE:
        return True
    # titre de série suivi de celui de l'album : « Les idées d'Achille Talon cerveau-choc ! »
    return theirs.endswith(f" {mine}") and bool(fiche.series) and title_key(fiche.series) in theirs


def _open_library(
    hub: SourceHub,
    previews: cover_search.Previews,
    fiche: matching.LocalFiche,
    cards: list[Card],
    suggestions: Suggestions,
) -> list[Card]:
    config = next((c for c in hub.cover_configs() if c.code == "openlibrary"), None)
    if config is None or not (config.enabled and config.covers_enabled):
        return cards
    isbns: list[str] = list(fiche.isbn13)
    for card in cards:
        if card.isbn and card.isbn not in isbns:
            isbns.append(card.isbn)
    author = fiche.contributors[0] if fiche.contributors else ""
    found, status = hub.find_covers(
        "openlibrary", isbns=tuple(isbns[:MAX_OPEN_LIBRARY_ISBNS]), title=fiche.title, author=author
    )
    if status not in ("found", "not_found"):
        suggestions.open_library = "unavailable"
        return cards
    suggestions.open_library = "found" if found else "none"
    by_isbn = {item.isbn13: item for item in found if item.level == "isbn" and item.isbn13}
    updated: list[Card] = []
    seen: set[str] = set()
    for card in cards:
        item = by_isbn.get(card.isbn)
        if (
            card.cover is None
            and item is not None
            and cover_search.preview(hub, previews, "openlibrary", item.cover_id, "", "vignette") is not None
        ):
            seen.add(item.cover_id)
            offer = CoverOffer(
                "openlibrary",
                item.cover_id,
                "",
                _cover_match(card.reliability),
                ", ".join(p for p in (item.publisher, item.year, _("ISBN {isbn}").format(isbn=item.isbn13)) if p),
                item.url,
                item.title,
            )
            card = replace(card, cover=offer, cover_state="found")
        updated.append(card)
    for item in found:
        if len(suggestions.extra_covers) >= MAX_OPEN_LIBRARY_EXTRA or item.cover_id in seen:
            continue
        if item.level == "isbn" and item.isbn13 in fiche.isbn13:
            continue  # déjà sur la carte de cette édition
        if item.level != "isbn" and not _plausible_work_image(fiche, item.title):
            continue
        if cover_search.preview(hub, previews, "openlibrary", item.cover_id, "", "vignette") is None:
            continue
        seen.add(item.cover_id)
        parts = (item.publisher, item.year, _("ISBN {isbn}").format(isbn=item.isbn13) if item.isbn13 else "")
        suggestions.extra_covers.append(
            CoverOffer(
                "openlibrary",
                item.cover_id,
                "",
                "other_edition" if item.level == "isbn" else "work",
                ", ".join(p for p in parts if p) or _("édition non précisée"),
                item.url,
                item.title,
            )
        )
    return updated


@dataclass(frozen=True)
class _Row:
    id: int
    group: str
    match_class: str
    level: int
    status: str
    candidate: Candidate
    explanation: dict[str, Any]

    @property
    def score(self) -> int:
        return int(self.explanation.get("score", 0))


_ORDER = {"exact": 0, "strong": 1, "multiple": 2, "uncertain": 3}


def _card(members: list[_Row], code: str, offer: CoverOffer | None, state: str) -> Card:
    lead = members[0].candidate
    explanation = members[0].explanation
    criteria = explanation.get("criteria", [])
    agreeing = tuple(c["note"] for c in criteria if c.get("possible") and c.get("points") == c.get("possible"))
    differing = tuple(c["note"] for c in criteria if c.get("points") == 0) + tuple(explanation.get("demoted", []))
    return Card(
        proposal_id=members[0].id,
        reliability=code,
        title=lead.work.title.value,
        subtitle=lead.work.subtitle.value if lead.work.subtitle else "",
        authors=_people(lead),
        publisher=lead.edition.publisher.value if lead.edition.publisher else "",
        year=lead.edition.year.raw if lead.edition.year else "",
        isbn=lead.edition.isbn13[0] if lead.edition.isbn13 else "",
        series=_series(lead),
        pages=lead.edition.pages.raw if lead.edition.pages else "",
        sources=tuple(dict.fromkeys(m.candidate.source for m in members)),
        notice_urls=tuple((m.candidate.source, m.candidate.url) for m in members if m.candidate.url),
        agreeing=agreeing,
        differing=differing,
        score=members[0].score,
        evidence=int(explanation.get("evidence", 0)),
        level=members[0].level,
        cover=offer,
        cover_state=state,
        several=members[0].match_class == "multiple",
    )


def suggestions(
    database: Database, hub: SourceHub, previews: cover_search.Previews, *, search_id: int, fiche: matching.LocalFiche
) -> Suggestions:
    """Les cartes d'une recherche déjà faite. Les images sont demandées ici (puis gardées en mémoire)."""
    with database.session() as session:
        search = session.get(EnrichmentSearch, search_id)
        if search is None:
            raise LookupError(search_id)
        records = session.scalars(select(EnrichmentProposal).where(EnrichmentProposal.search_id == search_id)).all()
        rows = [
            _Row(r.id, r.group_key, r.match_class, r.match_level, r.status, candidate_of(r), json.loads(r.explanation))
            for r in records
        ]
        result = Suggestions(search_id=search_id, outcome=search.outcome)
    kept = sorted(
        (r for r in rows if r.match_class in _ORDER and r.status == "pending"),
        key=lambda r: (_ORDER[r.match_class], -r.score, r.id),
    )
    result.excluded = sum(1 for r in rows if r.match_class not in _ORDER)
    groups: dict[str, list[_Row]] = {}
    for row in kept:
        groups.setdefault(row.group, []).append(row)
    announced = {r.candidate.external_id for r in rows if r.candidate.has_cover}  # ARK BnF qui annoncent une image
    cards: list[Card] = []
    images = 0
    for members in list(groups.values())[:MAX_CARDS]:
        code = reliability(members[0].match_class, members[0].explanation)
        offer: CoverOffer | None = None
        state = "none"
        if images < MAX_BNF_IMAGES:
            offer, state = _bnf_cover(hub, previews, [m.candidate for m in members], announced, code)
            images += offer is not None
        cards.append(_card(members, code, offer, state))
    result.cards = _open_library(hub, previews, fiche, cards, result)
    return result


def cover_for(
    hub: SourceHub, previews: cover_search.Previews, candidate: Candidate, explanation: dict[str, Any], match_class: str
) -> tuple[CoverOffer | None, str]:
    """L'image BnF d'une seule proposition (écran de comparaison), qualifiée comme sa carte."""
    code = reliability(match_class, explanation)
    announced = {candidate.external_id} if candidate.has_cover else set()
    return _bnf_cover(hub, previews, [candidate], announced, code)
