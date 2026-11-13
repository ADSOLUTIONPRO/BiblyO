# SPDX-License-Identifier: EUPL-1.2
"""Différences entre la fiche locale et un candidat, champ par champ, avec l'action proposée et la case précochée.

Règles du plan L5 (§ 7) : un champ rempli n'est jamais remplacé sans une case cochée ; rôles, résumé, mots-clés,
genres, ISBN et couverture ne sont jamais précochés ; un résumé de fournisseur tiers n'est pas copiable tant que ses
conditions ne sont pas vérifiées ; la série et le tome sont montrés à part, jamais appliqués par l'enrichissement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from biblyo.core.i18n import _
from biblyo.modules.enrichment.connectors.base import Candidate, Person
from biblyo.modules.enrichment.normalize import publisher_key, same_person, title_key

Level = Literal["work", "edition", "aside"]
Action = Literal["fill", "replace", "add", "identical", "not_copyable", "info"]
#: Rôles de biblyO proposés d'après le code de fonction UNIMARC, puis la mention de responsabilité.
FUNCTION_ROLES: dict[str, str] = {
    "070": "author",
    "100": "author",  # auteur de l'œuvre d'origine (adaptations) : à valider
    "330": "author",  # scénariste, libellé ancien
    "440": "illustrator",
    "730": "translator",
    "080": "editor",  # préfacier : proposé comme directeur de publication faute de mieux, à valider
    "340": "editor",
    "090": "adapter",
    "650": "editor",
}
MENTION_ROLES: dict[str, str] = {
    "text": "scenarist",
    "drawing": "artist",
    "colour": "colorist",
    "illustration": "illustrator",
    "translation": "translator",
    "adaptation": "adapter",
    "preface": "editor",
    "original": "author",
}
TEXT_ROLES: dict[str, str] = {
    "auteur du texte": "author",
    "autrice du texte": "author",
    "traducteur": "translator",
    "traductrice": "translator",
    "illustrateur": "illustrator",
    "illustratrice": "illustrator",
    "scénariste": "scenarist",
    "dessinateur": "artist",
    "coloriste": "colorist",
}
NEVER_PRECHECKED = frozenset({"isbn", "cover", "contributors", "summary", "keywords", "genres"})


@dataclass(frozen=True)
class LocalValues:
    """Les valeurs de la fiche et de l'édition visées, telles que l'écran les montre."""

    title: str
    subtitle: str = ""
    contributors: tuple[tuple[str, str], ...] = ()  # (nom, rôle)
    series: str = ""
    volume: str = ""
    summary: str = ""
    keywords: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    isbn13: tuple[str, ...] = ()
    publisher: str = ""
    collection: str = ""
    collection_number: str = ""
    year: str = ""
    statement: str = ""
    language: str = "fre"
    pages: str = ""
    physical_note: str = ""
    has_cover: bool = False
    cover_sha256: str = ""  # empreinte de la couverture actuelle : détecte un changement avant une annulation
    known_genres: tuple[str, ...] = ()  # genres de la taxonomie de la bibliothèque


@dataclass(frozen=True)
class ContributorProposal:
    name: str
    role: str  # rôle de biblyO proposé
    role_origin: str  # « code 440 », « mention : dessins de »
    uncertain: bool  # code reconverti ou absent : rôle incertain
    already_present: bool
    present_role: str = ""
    authority_id: str = ""
    dates: str = ""


@dataclass(frozen=True)
class FieldDiff:
    level: Level
    field: str
    label: str
    local: str
    proposed: str
    origin: str
    action: Action
    prechecked: bool = False
    note: str = ""
    payload: tuple[ContributorProposal, ...] = ()

    @property
    def applicable(self) -> bool:
        return self.action in ("fill", "replace", "add")


def propose_role(person: Person, *, reconverted: bool = False) -> tuple[str, str, bool]:
    """Le rôle de biblyO proposé pour une personne, d'où il vient, et s'il est incertain.

    Le code de fonction l'emporte, sauf « 070 » (auteur), que la mention de responsabilité précise pour une bande
    dessinée : « texte de » devient scénariste, « dessins de » dessinateur. Le rôle est incertain quand le code
    et la mention se contredisent, quand le code manque, ou quand le connecteur sait la notice reconvertie.
    """
    code = person.function_code
    mention = MENTION_ROLES.get(person.mention, "")
    text = TEXT_ROLES.get(person.function_text.casefold(), "")
    if code and code != "070" and code in FUNCTION_ROLES:
        return FUNCTION_ROLES[code], _("code de fonction {code}").format(code=code), reconverted
    if code == "070":
        if mention and mention not in ("author",):
            return mention, _("mention « {text} »").format(text=person.mention), True
        return "author", _("code de fonction 070"), reconverted
    if mention:
        return mention, _("mention de responsabilité"), True
    if text:
        return text, _("rôle écrit : {text}").format(text=person.function_text), True
    return "author", _("sans code de fonction"), True


def contributors_diff(local: LocalValues, candidate: Candidate) -> FieldDiff:
    reconverted = False  # l'UNIMARC de la BnF ne porte pas la marque de la conversion retour (Intermarc 991)
    proposals: list[ContributorProposal] = []
    for person in candidate.work.contributors:
        if person.mention == "original" and person.function_code == "100":
            continue  # auteur de l'œuvre d'origine d'une adaptation : montré à part par la mention
        role, origin, uncertain = propose_role(person, reconverted=reconverted)
        present = next(
            (
                (name, local_role)
                for name, local_role in local.contributors
                if same_person(name, person.surname, person.forename)
            ),
            None,
        )
        proposals.append(
            ContributorProposal(
                name=person.name,
                role=role,
                role_origin=origin,
                uncertain=uncertain,
                already_present=present is not None,
                present_role=present[1] if present else "",
                authority_id=person.authority_id,
                dates=person.dates,
            )
        )
    new = [p for p in proposals if not p.already_present]
    action: Action = "add" if new else "identical"
    if not proposals:
        action = "identical"
    return FieldDiff(
        "work",
        "contributors",
        _("Contributeurs"),
        "; ".join(f"{name} ({role})" for name, role in local.contributors),
        "; ".join(f"{p.name} ({p.role})" for p in proposals),
        "7xx",
        action,
        False,
        _("Les rôles sont à valider un par un ; une personne déjà présente garde son rôle.") if proposals else "",
        tuple(proposals),
    )


def _simple(
    level: Level, field: str, label: str, local: str, proposed: str, origin: str, *, exact: bool, strong: bool
) -> FieldDiff | None:
    if not proposed:
        return None
    if not local.strip():
        return FieldDiff(
            level, field, label, local, proposed, origin, "fill", (exact or strong) and field not in NEVER_PRECHECKED
        )
    if _same(field, local, proposed):
        return FieldDiff(level, field, label, local, proposed, origin, "identical")
    return FieldDiff(
        level, field, label, local, proposed, origin, "replace", False, _("La valeur actuelle serait remplacée.")
    )


def _same(field: str, local: str, proposed: str) -> bool:
    if field == "publisher":
        return publisher_key(local) == publisher_key(proposed)
    if field in ("year", "pages", "language", "collection_number"):
        return local.strip().casefold() == proposed.strip().casefold()
    return title_key(local) == title_key(proposed)


def diff(local: LocalValues, candidate: Candidate, *, match_class: str, edition_usable: bool) -> list[FieldDiff]:
    """Toutes les différences, dans l'ordre d'affichage : œuvre, édition, puis les encarts à part."""
    exact, strong = match_class == "exact", match_class == "strong"
    work, edition = candidate.work, candidate.edition
    diffs: list[FieldDiff] = []

    def add(item: FieldDiff | None) -> None:
        if item is not None:
            diffs.append(item)

    add(
        _simple(
            "work", "title", _("Titre"), local.title, work.title.value, work.title.origin, exact=exact, strong=strong
        )
    )
    if work.subtitle:
        add(
            _simple(
                "work",
                "subtitle",
                _("Sous-titre"),
                local.subtitle,
                work.subtitle.value,
                work.subtitle.origin,
                exact=exact,
                strong=strong,
            )
        )
    add(contributors_diff(local, candidate))
    if work.summary:
        if work.summary.reuse == "third_party":
            diffs.append(
                FieldDiff(
                    "work",
                    "summary",
                    _("Résumé"),
                    local.summary,
                    work.summary.text,
                    work.summary.origin,
                    "not_copyable",
                    False,
                    _("Résumé fourni par {provider} : conditions de réutilisation à vérifier, texte non copié.").format(
                        provider=work.summary.provider
                    ),
                )
            )
        else:
            add(
                _simple(
                    "work",
                    "summary",
                    _("Résumé"),
                    local.summary,
                    work.summary.text,
                    work.summary.origin,
                    exact=exact,
                    strong=strong,
                )
            )
    keywords = [
        s.value for s in work.subjects if s.value and title_key(s.value) not in {title_key(k) for k in local.keywords}
    ]
    if keywords:
        diffs.append(
            FieldDiff("work", "keywords", _("Mots-clés"), ", ".join(local.keywords), ", ".join(keywords), "606", "add")
        )
    known = {title_key(g): g for g in local.known_genres}
    genres = [known[title_key(g.value)] for g in work.genres if title_key(g.value) in known]
    genres = [g for g in genres if title_key(g) not in {title_key(x) for x in local.genres}]
    if genres:
        diffs.append(
            FieldDiff(
                "work", "genres", _("Genres"), ", ".join(local.genres), ", ".join(dict.fromkeys(genres)), "608", "add"
            )
        )

    if edition_usable:
        new_isbn = [isbn for isbn in edition.isbn13 if isbn not in local.isbn13]
        if new_isbn:
            action: Action = "fill" if not local.isbn13 else "add"
            diffs.append(
                FieldDiff(
                    "edition",
                    "isbn",
                    _("ISBN"),
                    ", ".join(local.isbn13),
                    ", ".join(new_isbn),
                    "010$a",
                    action,
                    False,
                    _("Un ISBN n'est confirmé que si l'édition est établie ; sinon il reste proposé."),
                )
            )
        if edition.publisher:
            add(
                _simple(
                    "edition",
                    "publisher",
                    _("Éditeur"),
                    local.publisher,
                    edition.publisher.value,
                    edition.publisher.origin,
                    exact=exact,
                    strong=strong,
                )
            )
        if edition.collection:
            add(
                _simple(
                    "edition",
                    "collection",
                    _("Collection"),
                    local.collection,
                    edition.collection.value,
                    edition.collection.origin,
                    exact=exact,
                    strong=strong,
                )
            )
        if edition.collection_number:
            add(
                _simple(
                    "edition",
                    "collection_number",
                    _("Numéro dans la collection"),
                    local.collection_number,
                    edition.collection_number.value,
                    edition.collection_number.origin,
                    exact=exact,
                    strong=strong,
                )
            )
        if edition.year:
            add(
                _simple(
                    "edition",
                    "year",
                    _("Année"),
                    local.year,
                    str(edition.year.whole),
                    edition.year.origin,
                    exact=exact,
                    strong=strong,
                )
            )
        if edition.statement:
            add(
                _simple(
                    "edition",
                    "statement",
                    _("Mention d'édition"),
                    local.statement,
                    edition.statement.value,
                    edition.statement.origin,
                    exact=exact,
                    strong=strong,
                )
            )
        if edition.language:
            add(
                _simple(
                    "edition",
                    "language",
                    _("Langue"),
                    local.language,
                    edition.language.value,
                    edition.language.origin,
                    exact=exact,
                    strong=strong,
                )
            )
        if edition.pages:
            add(
                _simple(
                    "edition",
                    "pages",
                    _("Pages"),
                    local.pages,
                    str(edition.pages.whole),
                    edition.pages.origin,
                    exact=exact,
                    strong=strong,
                )
            )
        if edition.physical:
            add(
                _simple(
                    "edition",
                    "physical_note",
                    _("Description matérielle"),
                    local.physical_note,
                    edition.physical.value,
                    edition.physical.origin,
                    exact=exact,
                    strong=strong,
                )
            )
        if candidate.has_cover or _linked_bnf_ark(candidate):
            announced = candidate.has_cover
            diffs.append(
                FieldDiff(
                    "edition",
                    "cover",
                    _("Couverture"),
                    _("présente") if local.has_cover else "",
                    _("couverture signalée par la source") if announced else _("couverture de la notice BnF liée"),
                    "856" if announced else "033",
                    "replace" if local.has_cover else "fill",
                    False,
                    _("Jamais précochée ; remplacer une couverture demande une confirmation dédiée.")
                    if announced
                    else _("Non annoncée par cette notice : la BnF sera interrogée, sans garantie d’image."),
                )
            )

    from_collection = (work.volume is not None and work.volume.origin.startswith("225")) or (
        work.volume is None and work.series is not None and work.series.origin.startswith("225")
    )
    if (work.series or work.volume) and not from_collection:  # un numéro de collection n'est pas un tome
        series = work.series.value if work.series else ""
        volume = _volume_text(work.volume.value) if work.volume else ""
        proposed = " · ".join(part for part in (series, _("tome {n}").format(n=volume) if volume else "") if part)
        mine = " · ".join(
            part for part in (local.series, _("tome {n}").format(n=local.volume) if local.volume else "") if part
        )
        diffs.append(
            FieldDiff(
                "aside",
                "series",
                _("Série et tome"),
                mine,
                proposed,
                work.volume.origin if work.volume else work.series.origin if work.series else "",
                "info",
                False,
                _("L'enrichissement ne rattache aucune œuvre à une série et ne change ni numéro interne ni cote."),
            )
        )
    return diffs


def _linked_bnf_ark(candidate: Candidate) -> str:
    """ARK de la BnF cité par une notice d'une autre source : sa couverture peut être cherchée."""
    if candidate.source == "bnf":
        return ""
    return next((v for v in candidate.linked_ids if v.startswith("ark:/12148/cb")), "")


def _volume_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value).replace(".", ",")
