# SPDX-License-Identifier: EUPL-1.2
"""Recherche de couvertures, indépendante des autres champs (conception 17 § 3 et 4).

Pour une édition : ses notices à la BnF (par ISBN), puis celles de l'œuvre (titre et auteur, ou titre et éditeur, ou
titre seul) ; les candidats non écartés qui **annoncent** une image (audit du 12 novembre 2026 : sans annonce, le
service n'en a jamais) ; l'image demandée par ARK, puis EAN, puis ISBN. Open Library, si l'administrateur l'a
activée, ajoute les images de ces ISBN et de l'œuvre. Les images identiques sont écartées.

Chaque proposition dit à quelle édition elle se rapporte et à quel point : même ISBN (« cette édition »), édition
présumée (aucun ISBN pour trancher, éditeur et année cohérents), autre édition, ou image de l'œuvre sans édition
déterminée. Rien n'est enregistré ici.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from biblyo.core.i18n import _
from biblyo.core.media import InvalidImage, preview_webp
from biblyo.modules.catalogue import covers as catalogue_covers
from biblyo.modules.enrichment import covers, matching
from biblyo.modules.enrichment.connectors.base import Candidate, CoverRequest, CoverResult, SearchQuery
from biblyo.modules.enrichment.connectors.bnf import ARK
from biblyo.modules.enrichment.connectors.openlibrary import COVER_ID, OpenLibraryConnector
from biblyo.modules.enrichment.connectors.openlibrary import LICENCE as OPEN_LIBRARY_LICENCE
from biblyo.modules.enrichment.normalize import isbn10_of, similarity
from biblyo.modules.enrichment.sources import SourceHub

MAX_BNF_NOTICES = 8
MAX_OPEN_LIBRARY = 6
SEARCH_LIMIT = 20
TITLE_POINTS_SAME_BOOK = 22  # critère « Titre » : identique (30) ou très proche (22), jamais « proche » (10)
THUMBNAIL_SIDE = 320
LARGE_SIDE = 900

MATCH_LABELS = catalogue_covers.MATCH_LABELS
NEEDS_CONFIRMATION = catalogue_covers.NEEDS_CONFIRMATION


@dataclass(frozen=True)
class CoverProposal:
    source: str  # « bnf », « openlibrary »
    reference: str  # ARK de la notice, ou numéro d'image d'Open Library
    ean: str  # repli de la BnF quand l'ARK ne sert pas l'image
    title: str
    edition: str  # « Hachette, DL 2015, ISBN 9782014001020 »
    match: str  # voir MATCH_LABELS
    notice_url: str
    digest: str  # empreinte de la vignette : écarte les doublons

    @property
    def key(self) -> str:
        return f"{self.source}:{self.reference}"


@dataclass(frozen=True)
class SourceLine:
    """Ce que l'écran dit d'une source après la recherche."""

    code: str
    status: str  # found, none, disabled, unavailable
    notices: int = 0
    images: int = 0
    detail: str = ""


@dataclass
class CoverSearch:
    proposals: list[CoverProposal] = field(default_factory=list)
    sources: list[SourceLine] = field(default_factory=list)
    without_image: int = 0  # notices retenues dont l'image annoncée n'a pas été servie
    duplicates: int = 0
    failures: int = 0
    other_works: int = 0  # notices ou œuvres d'un autre ouvrage (autre titre, autre tome) : jamais proposées

    @property
    def failed(self) -> bool:
        return not self.proposals and self.failures > 0


class Previews:
    """Images déjà demandées, gardées en mémoire (vignette et grand format) : réafficher ne rappelle pas la source."""

    def __init__(self, cache: covers.PreviewCache) -> None:
        self.cache = cache

    @staticmethod
    def key(source: str, reference: str, size: str) -> str:
        return f"couverture:{source}:{reference}:{size}"

    def get(self, source: str, reference: str, size: str) -> bytes | None:
        return self.cache.get(self.key(source, reference, size))

    def missing(self, source: str, reference: str) -> bool:
        return self.cache.get(self.key(source, reference, "absente")) is not None

    def mark_missing(self, source: str, reference: str) -> None:
        self.cache.put(self.key(source, reference, "absente"), b"")

    def keep(self, source: str, reference: str, original: bytes) -> bytes:
        """Contrôle et réencode l'image d'origine ; garde l'original, la vignette et le grand format."""
        thumbnail = preview_webp(original, THUMBNAIL_SIDE)
        self.cache.put(self.key(source, reference, "originale"), original)
        self.cache.put(self.key(source, reference, "vignette"), thumbnail)
        self.cache.put(self.key(source, reference, "grande"), preview_webp(original, LARGE_SIDE))
        return thumbnail


def valid_reference(source: str, reference: str) -> bool:
    if source == "bnf":
        return bool(ARK.match(reference))
    if source == "openlibrary":
        return bool(COVER_ID.match(reference))
    return False


def fetch_original(hub: SourceHub, source: str, reference: str, ean: str = "") -> CoverResult:
    """L'image d'origine d'une proposition, demandée à sa source. BnF : par ARK, puis par EAN si l'ARK ne la sert pas
    (One Piece 112, édition originale, relevé du 12 novembre 2026)."""
    config = next((c for c in hub.cover_configs() if c.code == source), None)
    if config is None or not valid_reference(source, reference):
        return CoverResult("unsupported")
    if not (config.enabled and config.covers_enabled):
        return CoverResult("unsupported", detail="source ou couvertures désactivées")
    if source == "bnf":
        result = hub.cover(config, CoverRequest(external_id=reference, side=1, announced=True, original=True))
        if result.status != "found" and len(ean) == 13 and ean.isdigit():
            result = hub.cover(config, CoverRequest(ean=ean, side=1, announced=True, original=True))
        return result
    return hub.cover(config, CoverRequest(external_id=reference, original=True))


def preview(hub: SourceHub, previews: Previews, source: str, reference: str, ean: str, size: str) -> bytes | None:
    """Une image de proposition, depuis la mémoire ou la source. Rien si elle n'existe pas ou n'est pas une image.

    Une absence constatée (pas d'image, réponse qui n'est pas une image) est gardée en mémoire : la même image
    n'est pas redemandée à chaque affichage. Une panne passagère, elle, n'est pas retenue."""
    cached = previews.get(source, reference, size)
    if cached is not None:
        return cached
    if previews.missing(source, reference):
        return None
    result = fetch_original(hub, source, reference, ean)
    if result.status != "found":
        if result.status in ("none_found", "announced_not_served", "invalid_image"):
            previews.mark_missing(source, reference)
        return None
    try:
        previews.keep(source, reference, result.data)
    except InvalidImage:
        previews.mark_missing(source, reference)
        return None
    return previews.get(source, reference, size)


def licence_note(source: str, retrieved_at: str) -> str:
    if source == "bnf":
        return covers.licence_note(retrieved_at)
    return f"{OPEN_LIBRARY_LICENCE.covers_note} Récupérée le {retrieved_at[:10]}. Règles : {OPEN_LIBRARY_LICENCE.url}"


def public_url(source: str, reference: str) -> str:
    if source == "bnf":
        from biblyo.modules.enrichment.connectors.bnf import BnfConnector

        return BnfConnector.public_url(reference)
    return OpenLibraryConnector.public_url(reference)


def _queries(facts: matching.LocalFiche) -> list[SearchQuery]:
    queries: list[SearchQuery] = []
    for isbn in facts.isbn13[:2]:
        queries.append(SearchQuery(level=1, isbn13=isbn, isbn10=isbn10_of(isbn) or ""))
    author = facts.contributors[0] if facts.contributors else ""
    if facts.title and author:
        queries.append(SearchQuery(level=2, title=facts.title, author=author))
    elif facts.title and facts.publisher:
        queries.append(SearchQuery(level=3, title=facts.title, publisher=facts.publisher))
    elif facts.title:
        queries.append(SearchQuery(level=5, title=facts.title))
    return queries


def _edition_text(candidate: Candidate) -> str:
    edition = candidate.edition
    parts = [
        edition.publisher.value if edition.publisher else "",
        edition.year.raw if edition.year else "",
        _("ISBN {isbn}").format(isbn=edition.isbn13[0]) if edition.isbn13 else "",
    ]
    return ", ".join(part for part in parts if part) or _("édition non précisée")


def _bnf_match(facts: matching.LocalFiche, assessment: matching.Assessment) -> str:
    if assessment.isbn_equal:
        return "same_edition"
    if facts.isbn13:
        return "other_edition"  # la fiche a un ISBN, la notice un autre ou aucun
    if not assessment.edition_usable:
        return "other_edition"  # sans ISBN, mais un autre éditeur, ou éditeur et année différents
    if assessment.match_class in ("exact", "strong"):
        return "presumed"
    return "undetermined"


def _same_book(assessment: matching.Assessment) -> bool:
    """Une image ne vaut que pour le même livre : titre identique ou très proche (ou titre de la série au même
    tome), et une correspondance retenue. « Je joue avec le Petit Prince », « Le P'tiot Prince » ou « Le Petit Prince
    et Okoda » ne sont pas « Le Petit Prince » (recette sur DEV, 12 novembre 2026)."""
    if assessment.match_class in ("excluded", "none"):
        return False
    title = next((c for c in assessment.criteria if c.key == "title"), None)
    return title is not None and title.points >= TITLE_POINTS_SAME_BOOK


def _same_title(local: str, theirs: str) -> bool:
    return similarity(local, theirs) >= matching.TITLE_CLOSE


_NUMBER = re.compile(r"(?<![0-9])([0-9]{1,4})(?![0-9])")


def _other_volume(facts: matching.LocalFiche, candidate: Candidate) -> bool:
    """Un tome de série : une notice dont le titre porte un autre numéro (« One Piece 113 » pour le tome 112) est
    écartée même quand son numéro de tome n'est pas lisible ailleurs (relevé du 12 novembre 2026)."""
    if facts.volume is None:
        return False
    text = candidate.work.title.value + " " + (candidate.work.subtitle.value if candidate.work.subtitle else "")
    numbers = {float(n) for n in _NUMBER.findall(text)}
    return bool(numbers) and all(abs(n - facts.volume) >= 0.01 for n in numbers)


def _bnf(hub: SourceHub, previews: Previews, facts: matching.LocalFiche, outcome: CoverSearch, seen: set[str]) -> None:
    config = next((c for c in hub.cover_configs() if c.code == "bnf"), None)
    if config is None or not (config.enabled and config.covers_enabled):
        outcome.sources.append(SourceLine("bnf", "disabled"))
        return
    found: dict[str, Candidate] = {}
    failures = 0
    for query in _queries(facts):
        result, _state = hub.search(config, query, limit=SEARCH_LIMIT)
        if not result.answered:
            failures += 1
        for candidate in result.candidates:
            found.setdefault(candidate.external_id, candidate)
    ranked = matching.classify(facts, [matching.assess(facts, c) for c in found.values()])
    illustrated = [a for a in ranked if a.candidate.has_cover]
    usable = [a for a in illustrated if _same_book(a) and not _other_volume(facts, a.candidate)]
    outcome.other_works += len(illustrated) - len(usable)
    usable.sort(key=lambda a: (not a.isbn_equal, -a.score))
    images = 0
    for assessment in usable[:MAX_BNF_NOTICES]:
        candidate = assessment.candidate
        ark = covers.ark_for(candidate)
        if not ark:
            continue
        ean = (candidate.edition.ean or candidate.edition.isbn13 or ("",))[0]
        thumbnail = preview(hub, previews, "bnf", ark, ean, "vignette")
        if thumbnail is None:
            outcome.without_image += 1
            continue
        digest = hashlib.sha256(thumbnail).hexdigest()
        if digest in seen:
            outcome.duplicates += 1
            continue
        seen.add(digest)
        images += 1
        outcome.proposals.append(
            CoverProposal(
                source="bnf",
                reference=ark,
                ean=ean,
                title=candidate.work.title.value,
                edition=_edition_text(candidate),
                match=_bnf_match(facts, assessment),
                notice_url=covers.public_url(candidate),
                digest=digest,
            )
        )
    outcome.failures += failures
    status = "unavailable" if failures and not found else ("found" if images else "none")
    outcome.sources.append(SourceLine("bnf", status, notices=len(usable), images=images))


def _open_library(
    hub: SourceHub, previews: Previews, facts: matching.LocalFiche, outcome: CoverSearch, seen: set[str]
) -> None:
    config = next((c for c in hub.cover_configs() if c.code == "openlibrary"), None)
    if config is None or not (config.enabled and config.covers_enabled):
        outcome.sources.append(SourceLine("openlibrary", "disabled"))
        return
    author = facts.contributors[0] if facts.contributors else ""
    found, status = hub.find_covers("openlibrary", isbns=facts.isbn13, title=facts.title, author=author)
    if status not in ("found", "not_found"):
        outcome.failures += 1
        outcome.sources.append(SourceLine("openlibrary", "unavailable", detail=status))
        return
    images = 0
    if facts.volume is not None:  # un tome : l'image « de l'œuvre » montre le plus souvent un autre tome
        kept = [item for item in found if item.level == "isbn"]
        outcome.other_works += len(found) - len(kept)
        found = kept
    # Une œuvre trouvée par titre et auteur doit porter le même titre : « Oeuvres romanesques complètes t.2
    # (Bibliotheque de la Pleiade) » n'est pas « Œuvres romanesques 2 » (recette sur DEV, 12 novembre 2026).
    kept = [item for item in found if item.level == "isbn" or _same_title(facts.title, item.title)]
    outcome.other_works += len(found) - len(kept)
    found = kept
    for item in found[:MAX_OPEN_LIBRARY]:
        thumbnail = preview(hub, previews, "openlibrary", item.cover_id, "", "vignette")
        if thumbnail is None:
            outcome.without_image += 1
            continue
        digest = hashlib.sha256(thumbnail).hexdigest()
        if digest in seen:
            outcome.duplicates += 1
            continue
        seen.add(digest)
        images += 1
        same = item.level == "isbn" and item.isbn13 in facts.isbn13
        parts = [item.publisher, item.year, _("ISBN {isbn}").format(isbn=item.isbn13) if item.isbn13 else ""]
        outcome.proposals.append(
            CoverProposal(
                source="openlibrary",
                reference=item.cover_id,
                ean="",
                title=item.title,
                edition=", ".join(p for p in parts if p) or _("édition non précisée"),
                match="same_edition" if same else "work",
                notice_url=item.url,
                digest=digest,
            )
        )
    outcome.sources.append(SourceLine("openlibrary", "found" if images else "none", notices=len(found), images=images))


ORDER = {"same_edition": 0, "presumed": 1, "undetermined": 2, "other_edition": 3, "work": 4}


def search(hub: SourceHub, previews: Previews, facts: matching.LocalFiche) -> CoverSearch:
    outcome = CoverSearch()
    seen: set[str] = set()
    _bnf(hub, previews, facts, outcome, seen)
    _open_library(hub, previews, facts, outcome, seen)
    outcome.proposals.sort(key=lambda p: (ORDER.get(p.match, 9), p.source != "bnf"))
    return outcome
