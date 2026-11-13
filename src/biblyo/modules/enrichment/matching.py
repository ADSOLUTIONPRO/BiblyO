# SPDX-License-Identifier: EUPL-1.2
"""Rapprochement d'une fiche locale et des candidats des sources : score explicable, classes, regroupement.

Règles du plan L5 (§ 6). Les seuils sont des paramètres provisoires, réunis en tête de fichier pour être
réétalonnés sur la simulation d'import de L6. Le score ne décide jamais seul : les conditions éliminatoires et les
déclassements s'appliquent avant lui, et une correspondance forte ne devient jamais exacte par son score.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Literal

from biblyo.core.i18n import _
from biblyo.modules.enrichment.connectors.base import Candidate
from biblyo.modules.enrichment.normalize import (
    publisher_key,
    publisher_relation,
    same_person,
    series_conflict,
    similarity,
    title_key,
)

# Seuils provisoires (D5)
STRONG_SCORE = 85
STRONG_EVIDENCE = 65
STRONG_LEAD = 10
UNCERTAIN_SCORE = 60
DISCARD_SCORE = 40
TITLE_EXACT = 0.95
TITLE_CLOSE = 0.85
TITLE_COHERENT = 0.70
TITLE_MINIMUM = 0.50
PAGES_TOLERANCE = 0.05

MatchClass = Literal["exact", "strong", "uncertain", "multiple", "none", "excluded"]
LEVELS: dict[int, str] = {
    1: "ISBN",
    2: "titre et auteur",
    3: "titre, éditeur et année",
    4: "série et tome",
    5: "recherche élargie",
}


@dataclass(frozen=True)
class LocalFiche:
    """Ce que le rapprochement sait de la fiche locale. Les clés absentes ne comptent pas dans la base de preuve."""

    title: str
    subtitle: str = ""
    contributors: tuple[str, ...] = ()  # noms affichés : « René Goscinny »
    isbn13: tuple[str, ...] = ()  # ISBN confirmés seulement
    publisher: str = ""
    year: int | None = None
    pages: int | None = None
    collection: str = ""
    series: str = ""
    volume: float | None = None
    language: str = "fre"  # ISO 639-2/B, comme `editions.language`
    medium: str = "text"


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    points: int
    possible: int
    note: str  # « Titre identique », « Éditeur différent : Dargaud contre Hachette »

    @property
    def agrees(self) -> bool:
        return self.points == self.possible

    @property
    def conflicts(self) -> bool:
        return self.points == 0


@dataclass(frozen=True)
class Assessment:
    candidate: Candidate
    score: int  # 0 à 100
    evidence: int  # base de preuve : points possibles, 0 à 100
    criteria: tuple[Criterion, ...]
    isbn_equal: bool
    excluded: str = ""  # motif d'élimination, vide si le candidat reste
    demoted: tuple[str, ...] = ()  # motifs de déclassement
    edition_usable: bool = True  # faux : le candidat sert à l'œuvre, jamais à l'édition
    match_class: MatchClass = "none"
    group: str = ""  # même édition vue par deux sources
    lead: int | None = field(default=None)  # avance sur le groupe suivant, posée par `classify`

    @property
    def agreeing(self) -> tuple[str, ...]:
        return tuple(c.note for c in self.criteria if c.agrees)

    @property
    def conflicting(self) -> tuple[str, ...]:
        return tuple(c.note for c in self.criteria if c.conflicts)

    @property
    def explanation(self) -> dict[str, object]:
        """Le détail affiché sous « Pourquoi ce score », conservé avec la proposition."""
        return {
            "score": self.score,
            "evidence": self.evidence,
            "criteria": [
                {"key": c.key, "label": c.label, "points": c.points, "possible": c.possible, "note": c.note}
                for c in self.criteria
            ],
            "isbn_equal": self.isbn_equal,
            "excluded": self.excluded,
            "demoted": list(self.demoted),
            "edition_usable": self.edition_usable,
            "class": self.match_class,
        }


# --- Score ------------------------------------------------------------------------------------------------------------
def _series_as_title(local: LocalFiche, candidate: Candidate) -> float:
    """Ressemblance du titre de la fiche au titre de la série du candidat, comptée seulement si le même livre est en
    vue : tome identique des deux côtés, ou même ISBN. Sans cette garde, tout album d'une série vaudrait la fiche."""
    series = candidate.work.series
    if series is None:
        return 0.0
    theirs = _candidate_volume(local, candidate)
    same_volume = local.volume is not None and theirs is not None and abs(theirs - local.volume) < 0.01
    same_isbn = bool(local.isbn13) and any(isbn in candidate.edition.isbn13 for isbn in local.isbn13)
    if not (same_volume or same_isbn):
        return 0.0
    return similarity(local.title, series.value)


def _same_book_by_series(x: Candidate, y: Candidate) -> bool:
    """Deux notices d'un même ISBN qui nomment la même série au même tome décrivent le même livre, l'une par le
    titre de la série, l'autre par celui du tome. Sans tome des deux côtés, rien n'est conclu."""
    if x.work.series is None or y.work.series is None or x.work.volume is None or y.work.volume is None:
        return False
    if abs(x.work.volume.value - y.work.volume.value) >= 0.01:
        return False
    if similarity(x.work.series.value, y.work.series.value) < TITLE_CLOSE:
        return False
    titles = (x.work.title.value, y.work.title.value)
    return any(similarity(t, s) >= TITLE_CLOSE for t in titles for s in (x.work.series.value, y.work.series.value))


def _title_criterion(local: LocalFiche, candidate: Candidate) -> tuple[Criterion, float]:
    local_title = local.title
    theirs = candidate.work.title.value
    ratio = similarity(local_title, theirs)
    if ratio < TITLE_EXACT and local.subtitle:
        ratio = max(ratio, similarity(f"{local_title} {local.subtitle}", theirs))
    if ratio < TITLE_EXACT and candidate.work.subtitle:
        ratio = max(ratio, similarity(local_title, f"{theirs} {candidate.work.subtitle.value}"))
    by_series = _series_as_title(local, candidate)
    if by_series > ratio and by_series >= TITLE_CLOSE and candidate.work.series:
        # « One Piece », tome 72, contre « Les oubliés de Dressrosa » : le titre de la fiche est celui de la série.
        note = _("Titre de la série, même livre : « {series} »").format(series=candidate.work.series.value)
        return Criterion("title", _("Titre"), 30 if by_series >= TITLE_EXACT else 22, 30, note), by_series
    if ratio >= TITLE_EXACT:
        points, note = 30, _("Titre identique")
    elif ratio >= TITLE_CLOSE:
        points, note = 22, _("Titre très proche")
    elif ratio >= TITLE_COHERENT:
        points, note = 10, _("Titre proche")
    else:
        points, note = 0, _("Titre différent : « {theirs} »").format(theirs=theirs)
    return Criterion("title", _("Titre"), points, 30, note), ratio


def _authors_criterion(local: LocalFiche, candidate: Candidate) -> Criterion | None:
    names = [name for name in local.contributors if name.strip()]
    if not names:
        return None
    people = candidate.work.contributors
    found = [name for name in names if any(same_person(name, p.surname, p.forename) for p in people)]
    if len(found) == len(names):
        return Criterion(
            "authors", _("Auteurs"), 20, 20, _("Auteurs retrouvés : {names}").format(names=", ".join(found))
        )
    if found:
        return Criterion("authors", _("Auteurs"), 14, 20, _("Auteur retrouvé : {names}").format(names=", ".join(found)))
    theirs = ", ".join(p.name for p in people) or _("aucun")
    return Criterion("authors", _("Auteurs"), 0, 20, _("Auteurs différents : {theirs}").format(theirs=theirs))


def _publisher_criterion(local: LocalFiche, candidate: Candidate) -> Criterion | None:
    if not publisher_key(local.publisher):
        return None
    theirs = candidate.edition.publisher.value if candidate.edition.publisher else ""
    relation = publisher_relation(local.publisher, theirs)
    if relation == "same":
        return Criterion("publisher", _("Éditeur"), 15, 15, _("Même éditeur"))
    if relation == "related":
        return Criterion("publisher", _("Éditeur"), 8, 15, _("Éditeur apparenté : {theirs}").format(theirs=theirs))
    label = theirs or _("non renseigné")
    return Criterion(
        "publisher",
        _("Éditeur"),
        0,
        15,
        _("Éditeur différent : {mine} contre {theirs}").format(mine=local.publisher, theirs=label),
    )


def _candidate_volume(local: LocalFiche, candidate: Candidate) -> float | None:
    """Le tome du candidat, s'il est digne de foi : un numéro lu en 225 n'est un tome que si la série locale porte
    ce nom (sinon c'est un numéro de collection : « Folio junior, 100 »)."""
    volume = candidate.work.volume
    if volume is None:
        return None
    if volume.origin.startswith("225"):
        series = candidate.work.series.value if candidate.work.series else ""
        if not local.series or similarity(local.series, series) < TITLE_COHERENT:
            return None
    return volume.value


def _volume_criterion(local: LocalFiche, candidate: Candidate) -> Criterion | None:
    if local.volume is None:
        return None
    theirs = _candidate_volume(local, candidate)
    if theirs is None:
        return Criterion("volume", _("Tome"), 0, 15, _("Tome non renseigné par la source"))
    if abs(theirs - local.volume) < 0.01:
        return Criterion("volume", _("Tome"), 15, 15, _("Tome {number} identique").format(number=_number(theirs)))
    return Criterion(
        "volume",
        _("Tome"),
        0,
        15,
        _("Tome différent : {mine} contre {theirs}").format(mine=_number(local.volume), theirs=_number(theirs)),
    )


def _year_criterion(local: LocalFiche, candidate: Candidate) -> Criterion | None:
    if local.year is None:
        return None
    theirs = candidate.edition.year.whole if candidate.edition.year else None
    if theirs is None:
        return Criterion("year", _("Année"), 0, 10, _("Année non renseignée par la source"))
    if theirs == local.year:
        return Criterion("year", _("Année"), 10, 10, _("Année {year} identique").format(year=theirs))
    if abs(theirs - local.year) == 1:
        return Criterion("year", _("Année"), 6, 10, _("Année voisine : {theirs}").format(theirs=theirs))
    return Criterion(
        "year", _("Année"), 0, 10, _("Année différente : {mine} contre {theirs}").format(mine=local.year, theirs=theirs)
    )


def _pages_criterion(local: LocalFiche, candidate: Candidate) -> Criterion | None:
    if not local.pages:
        return None
    theirs = candidate.edition.pages.whole if candidate.edition.pages else None
    if theirs is None:
        return Criterion("pages", _("Pages"), 0, 5, _("Pages non renseignées par la source"))
    if abs(theirs - local.pages) <= PAGES_TOLERANCE * max(local.pages, theirs):
        return Criterion("pages", _("Pages"), 5, 5, _("Même nombre de pages"))
    return Criterion(
        "pages",
        _("Pages"),
        0,
        5,
        _("Pages différentes : {mine} contre {theirs}").format(mine=local.pages, theirs=theirs),
    )


def _collection_criterion(local: LocalFiche, candidate: Candidate) -> Criterion | None:
    if not title_key(local.collection):
        return None
    theirs = candidate.edition.collection.value if candidate.edition.collection else ""
    if theirs and title_key(theirs) == title_key(local.collection):
        return Criterion("collection", _("Collection"), 5, 5, _("Même collection"))
    return Criterion(
        "collection", _("Collection"), 0, 5, _("Collection différente : {theirs}").format(theirs=theirs or _("aucune"))
    )


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value).replace(".", ",")


def _series_conflict(local: LocalFiche, candidate: Candidate) -> bool:
    """La fiche et le candidat nomment deux séries différentes. Une collection (225) ne contredit pas une série."""
    theirs = candidate.work.series
    if theirs is None or theirs.origin.startswith("225"):
        return False
    return series_conflict(local.series, theirs.value)


def assess(local: LocalFiche, candidate: Candidate) -> Assessment:
    """Score, base de preuve, exclusions et déclassements d'un candidat pour une fiche. Sans regroupement."""
    has_title = bool(title_key(local.title))  # recherche libre par ISBN seul : aucun titre à comparer
    title, ratio = _title_criterion(local, candidate)
    criteria = [title] if has_title else []
    if not has_title:
        ratio = 1.0
    for maker in (
        _authors_criterion,
        _publisher_criterion,
        _volume_criterion,
        _year_criterion,
        _pages_criterion,
        _collection_criterion,
    ):
        criterion = maker(local, candidate)
        if criterion is not None:
            criteria.append(criterion)
    possible = sum(c.possible for c in criteria)
    points = sum(c.points for c in criteria)
    score = round(100 * points / possible) if possible else 0
    isbn_equal = bool(local.isbn13) and any(isbn in candidate.edition.isbn13 for isbn in local.isbn13)

    excluded = ""
    language = candidate.edition.language.value if candidate.edition.language else ""
    their_volume = _candidate_volume(local, candidate)
    if candidate.record_kind == "set":
        excluded = _("Notice d'ensemble : décrit la série entière, pas une édition")
    elif local.medium == "text" and candidate.medium != "text":
        excluded = _("Autre type de document : {kind}").format(kind=_medium_label(candidate))
    elif language and local.language and language != local.language and local.language != "und":
        excluded = _("Langue différente : {theirs}").format(theirs=language)
    elif ratio < TITLE_MINIMUM:
        excluded = _("Titre manifestement différent")
    elif local.volume is not None and their_volume is not None and abs(their_volume - local.volume) >= 0.01:
        excluded = _("Numéro de tome différent")

    demoted: list[str] = []
    usable = True
    if _series_conflict(local, candidate):
        demoted.append(
            _("Série contradictoire : {theirs}").format(
                theirs=candidate.work.series.value if candidate.work.series else ""
            )
        )
        usable = False
    if local.isbn13 and candidate.edition.isbn13 and not isbn_equal:
        demoted.append(_("Édition incompatible : un autre ISBN"))
        usable = False
    publisher = next((c for c in criteria if c.key == "publisher"), None)
    year = next((c for c in criteria if c.key == "year"), None)
    if publisher is not None and year is not None and publisher.conflicts and year.conflicts and not isbn_equal:
        demoted.append(_("Édition incompatible : éditeur et année différents"))
        usable = False
    elif publisher is not None and publisher.conflicts and candidate.edition.publisher and not isbn_equal:
        # Recette sur DEV : une fiche Gallimard sans année ne prend ni l'année ni les pages d'une édition Futuropolis.
        demoted.append(_("Édition incompatible : autre éditeur"))
        usable = False
    if candidate.record_status == "sparse" and (not candidate.work.contributors or candidate.edition.publisher is None):
        demoted.append(_("Notice élémentaire insuffisante"))
    if TITLE_MINIMUM <= ratio < TITLE_COHERENT:
        demoted.append(_("Titre seulement cohérent"))

    return Assessment(
        candidate=candidate,
        score=score,
        evidence=possible,
        criteria=tuple(criteria),
        isbn_equal=isbn_equal,
        excluded=excluded,
        demoted=tuple(demoted),
        edition_usable=usable and not excluded,
        group=group_key(candidate),
    )


def _medium_label(candidate: Candidate) -> str:
    return candidate.work.title.raw or _("document non textuel")


# --- Regroupement et classes ------------------------------------------------------------------------------------------
def group_key(candidate: Candidate) -> str:
    """Deux notices de la même édition (une par source) : l'ARK commun, sinon ISBN, éditeur et année."""
    arks = [candidate.external_id, *candidate.linked_ids]
    ark = next((value for value in arks if value.startswith("ark:/")), "")
    if ark:
        return f"ark:{ark}"
    if candidate.edition.isbn13:
        publisher = publisher_key(candidate.edition.publisher.value if candidate.edition.publisher else "")
        year = candidate.edition.year.whole if candidate.edition.year else ""
        return f"isbn:{candidate.edition.isbn13[0]}:{publisher}:{year}"
    return f"{candidate.source}:{candidate.external_id}"


def _exactness(assessment: Assessment) -> bool:
    return (
        assessment.isbn_equal
        and not assessment.excluded
        and assessment.edition_usable
        and not any(reason.startswith(("Notice élémentaire", "Titre seulement")) for reason in assessment.demoted)
    )


def _single(assessment: Assessment) -> MatchClass:
    """La classe d'un candidat pris seul, avant le regard sur les autres."""
    if assessment.excluded:
        return "excluded"
    if _exactness(assessment):
        return "exact"
    if assessment.score < DISCARD_SCORE:
        return "none"
    strong = (
        not assessment.isbn_equal
        and assessment.score >= STRONG_SCORE
        and assessment.evidence >= STRONG_EVIDENCE
        and not assessment.demoted
    )
    if strong:
        return "strong"
    if assessment.score >= UNCERTAIN_SCORE or assessment.isbn_equal or assessment.score >= STRONG_SCORE:
        return "uncertain"
    return "none"


def classify(local: LocalFiche, assessments: list[Assessment]) -> list[Assessment]:
    """Classe chaque candidat en regardant les autres : avance sur le suivant, ISBN partagé par des titres différents.

    Rend la liste triée : exacts, forts, incertains, multiples, puis le reste ; à classe égale, score décroissant,
    notices validées avant les élémentaires.
    """
    kept = [a for a in assessments if not a.excluded]
    excluded = [replace(a, match_class="excluded") for a in assessments if a.excluded]

    # Un même ISBN porté par des titres différents : tous « plusieurs candidats », aucun « exact ».
    by_isbn: dict[str, list[Assessment]] = defaultdict(list)
    for a in assessments:  # les exclus comptent : un titre différent sous le même ISBN est le cas visé
        for isbn in a.candidate.edition.isbn13:
            by_isbn[isbn].append(a)
    shared: set[int] = set()
    for members in by_isbn.values():
        titles = {title_key(m.candidate.work.title.value) for m in members}
        if len(members) > 1 and any(
            similarity(x.candidate.work.title.value, y.candidate.work.title.value) < TITLE_COHERENT
            and not _same_book_by_series(x.candidate, y.candidate)
            for x in members
            for y in members
            if x is not y
        ):
            shared.update(id(m) for m in members)
        del titles

    # Meilleur score par groupe d'édition, pour l'avance d'un groupe sur le suivant.
    best_by_group: dict[str, int] = {}
    for a in kept:
        best_by_group[a.group] = max(best_by_group.get(a.group, -1), a.score)
    ranked = sorted(best_by_group.values(), reverse=True)

    result: list[Assessment] = []
    for a in kept:
        single = _single(a)
        others = [s for g, s in best_by_group.items() if g != a.group]
        lead = (a.score - max(others)) if others else 100
        if id(a) in shared:
            match_class: MatchClass = "multiple" if single != "none" else "none"
        elif (single == "strong" and lead < STRONG_LEAD) or (
            single in ("uncertain",) and lead < STRONG_LEAD and ranked and a.score == ranked[0] and len(ranked) > 1
        ):
            match_class = "multiple"
        else:
            match_class = single
        result.append(replace(a, match_class=match_class, lead=lead))

    order = {"exact": 0, "strong": 1, "uncertain": 2, "multiple": 3, "none": 4, "excluded": 5}
    result.extend(excluded)
    result.sort(key=lambda a: (order[a.match_class], a.candidate.record_status == "sparse", -a.score))
    return result


def outcome(assessments: list[Assessment], *, complete: bool) -> str:
    """Le résultat d'une recherche : exact, strong, uncertain, multiple, none ; incomplete si une source manque."""
    classes = [a.match_class for a in assessments]
    exacts = [a for a in assessments if a.match_class == "exact"]
    # Plusieurs notices d'un ISBN comptent pour une : même titre, ou même série au même tome (titre de la série d'un
    # côté, titre du tome de l'autre).
    editions: list[Assessment] = []
    for a in exacts:
        if not any(
            a.candidate.edition.isbn13[:1] == e.candidate.edition.isbn13[:1]
            and (
                title_key(a.candidate.work.title.value) == title_key(e.candidate.work.title.value)
                or _same_book_by_series(a.candidate, e.candidate)
            )
            for e in editions
        ):
            editions.append(a)
    if len(editions) > 1:
        return "multiple"
    for name in ("exact", "strong", "multiple", "uncertain"):
        if name in classes:
            return name
    return "none" if complete else "incomplete"
