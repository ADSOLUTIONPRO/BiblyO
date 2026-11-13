# SPDX-License-Identifier: EUPL-1.2
"""Lecture d'une notice UNIMARC (MarcXML ou marcXchange) vers un `Candidate`.

Deux dialectes : la BnF (espace de noms marcXchange, identifiant ARK sur la notice) et le Sudoc (sans espace de
noms, PPN en 001, zones dans le désordre, marques de non-tri dans les titres). Toute zone `9xx` et toute zone
portant un sous-champ `$5` est écartée avant lecture : ce sont des données d'exemplaire, jamais des données de
l'ouvrage.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any
from xml.etree.ElementTree import Element  # nosec B405 : l'analyse est faite en amont, par xmlsafe

from biblyo.core.text import normalize
from biblyo.modules.enrichment.connectors.base import (
    Candidate,
    EditionData,
    Mention,
    Number,
    Person,
    RecordKind,
    RecordStatus,
    Summary,
    Text,
    WorkData,
)
from biblyo.modules.enrichment.normalize import (
    clean_text,
    is_open_date,
    isbn13_of,
    parse_pages,
    parse_volume,
    parse_year,
    person_tokens,
)

ARK = re.compile(r"ark:/12148/cb\d{8}[0-9a-z]")
PPN = re.compile(r"^\d{8}[\dX]$")
FRBNF = re.compile(r"^FRBNF\d+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")  # marques de non-tri comprises
_EAN = re.compile(r"^\d{13}$")
_THIRD_PARTY_SUMMARY = re.compile(r"^\s*(?:4e|4ème|4eme|quatri[eè]me)\s+de\s+couv", re.IGNORECASE)
_QUOTED_SUMMARY = re.compile(r"""^\s*["«“]""")
_NO_NAME = frozenset({"s.n.", "s.l.", "s.d.", "sn", "sl", "sd"})
_HOLDINGS_TAGS = frozenset({"316", "317", "318", "319"})  # notes d'exemplaire : jamais lues
_MENTIONS: tuple[tuple[re.Pattern[str], Mention], ...] = (
    (re.compile(r"\bd[ '’]?apr[eè]s\b|\bde la nouvelle\b|\bdu roman\b", re.IGNORECASE), "original"),
    (re.compile(r"\b(?:texte|textes|sc[eé]nario|r[eé]cit|histoire)\b", re.IGNORECASE), "text"),
    (re.compile(r"\b(?:dessins?|dessin[eé]|graphisme)\b", re.IGNORECASE), "drawing"),
    (re.compile(r"\b(?:couleurs?|colori\w*|mise en couleurs?)\b", re.IGNORECASE), "colour"),
    (re.compile(r"\b(?:ill(?:ustr\w*|\.)|aquarelles?|images?|photographi\w*)\b", re.IGNORECASE), "illustration"),
    (re.compile(r"\b(?:trad\w*|translat\w*)\b", re.IGNORECASE), "translation"),
    (re.compile(r"\b(?:adapt\w*)\b", re.IGNORECASE), "adaptation"),
    (re.compile(r"\b(?:pr[eé]fac\w*|postfac\w*|avant-propos|introduction)\b", re.IGNORECASE), "preface"),
)


@dataclass(frozen=True)
class Field:
    tag: str
    ind1: str
    ind2: str
    subfields: tuple[tuple[str, str], ...]

    def first(self, code: str) -> str:
        return next((value for key, value in self.subfields if key == code), "")

    def all(self, code: str) -> list[str]:
        return [value for key, value in self.subfields if key == code]

    def has(self, code: str) -> bool:
        return any(key == code for key, _value in self.subfields)

    @property
    def label(self) -> str:
        return self.tag


@dataclass(frozen=True)
class Record:
    """Une notice nettoyée : en-tête, zones de contrôle et zones de données retenues."""

    leader: str
    controls: tuple[tuple[str, str], ...]
    fields: tuple[Field, ...]
    record_id: str = ""  # attribut `id` de marcXchange (ARK à la BnF)

    def control(self, tag: str) -> str:
        return next((value for key, value in self.controls if key == tag), "")

    def get(self, tag: str) -> list[Field]:
        return [field for field in self.fields if field.tag == tag]

    def first(self, tag: str) -> Field | None:
        return next((field for field in self.fields if field.tag == tag), None)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(value: str | None) -> str:
    return clean_text(_CONTROL.sub("", value or ""))


def is_record(element: Element) -> bool:
    """Une notice MARC, en UNIMARC : une notice Intermarc ou Dublin Core n'est pas lue."""
    if _local(element.tag) != "record" or element.get("format", "UNIMARC").upper() != "UNIMARC":
        return False
    return any(_local(child.tag) in ("leader", "datafield") for child in element)


def iter_records(root: Element) -> Iterator[Element]:
    """Les notices d'une réponse, quel que soit l'emballage (SRU, notice seule)."""
    for element in root.iter():
        if is_record(element):
            yield element


def read(element: Element) -> Record:
    """Nettoie une notice : zones d'exemplaire écartées, marques de non-tri et caractères de contrôle retirés."""
    leader = ""
    controls: list[tuple[str, str]] = []
    fields: list[Field] = []
    for child in element:
        name = _local(child.tag)
        tag = child.get("tag", "")
        if name == "leader":
            leader = child.text or ""
        elif name == "controlfield" and tag:
            controls.append((tag, _text(child.text)))
        elif name == "datafield" and tag:
            subfields = tuple((sub.get("code", "?"), _text(sub.text)) for sub in child if _local(sub.tag) == "subfield")
            if tag.startswith("9") or tag in _HOLDINGS_TAGS or any(code == "5" for code, _v in subfields):
                continue
            fields.append(Field(tag, child.get("ind1", " ") or " ", child.get("ind2", " ") or " ", subfields))
    return Record(leader, tuple(controls), tuple(fields), element.get("id", ""))


# --- Identifiants -----------------------------------------------------------------------------------------------------
def ark_of(record: Record) -> str:
    for text in (record.record_id, record.control("003"), record.control("001")):
        match = ARK.search(text)
        if match:
            return match.group(0)
    return ""


def ppn_of(record: Record) -> str:
    value = record.control("001")
    return value if PPN.match(value) else ""


def linked_ids(record: Record) -> tuple[str, ...]:
    """Identifiants d'autres sources cités par la notice : l'ARK de la BnF dans une notice du Sudoc."""
    found: list[str] = []
    for field in record.get("033") + record.get("035"):
        for value in field.all("a"):
            match = ARK.search(value)
            if match and match.group(0) not in found:
                found.append(match.group(0))
            elif FRBNF.match(value) and value not in found:
                found.append(value)
    return tuple(found)


# --- Nature de la notice ----------------------------------------------------------------------------------------------
def medium_of(record: Record) -> str:
    """`text` pour un texte imprimé ou manuscrit ; `other` pour un son, une vidéo, une partition, une image."""
    kind = record.leader[6] if len(record.leader) > 6 else " "
    if kind in "ab":
        return "text"
    if kind.strip():
        return "other"
    title = record.first("200")
    other = title is not None and any(
        word in title.first("b").casefold() for word in ("sonore", "animées", "animees", "musique", "électronique")
    )
    return "other" if other else "text"


def kind_of(record: Record) -> RecordKind:
    """`set` : une notice d'ensemble (série, collection, publication en cours), jamais une édition."""
    level = record.leader[7] if len(record.leader) > 7 else " "
    if level in "sc":
        return "set"
    general = record.first("100")
    date_type = general.first("a")[8] if general is not None and len(general.first("a")) > 8 else ""
    if date_type in "abcg":
        return "set"
    publication = record.first("210") or record.first("214")
    if publication is not None and is_open_date(publication.first("d")):
        return "set"
    return "edition"


def status_of(record: Record) -> RecordStatus:
    """`sparse` : notice élémentaire, dont les contributeurs n'ont ni lien d'autorité ni code de fonction.

    La BnF ne porte pas le statut dans la notice UNIMARC, et l'en-tête des notices passées par sa conversion
    retour est incomplet : il ne dit rien. Le connecteur peut connaître le statut par sa requête et corriger
    cette lecture.
    """
    people = [field for field in record.fields if field.tag in ("700", "701", "702", "710", "711", "712")]
    if people and (not any(f.has("3") for f in people) or not any(f.has("4") for f in people)):
        return "sparse"
    return "validated"


# --- Œuvre ------------------------------------------------------------------------------------------------------------
def _mentions(record: Record) -> list[str]:
    title = record.first("200")
    if title is None:
        return []
    segments: list[str] = []
    for value in title.all("f") + title.all("g"):
        segments.extend(part.strip() for part in re.split(r"[;,]", value) if part.strip())
    return segments


def _mention_in(text: str) -> tuple[int, Mention] | None:
    """La mention la plus proche de la fin du texte, avec sa position."""
    best: tuple[int, Mention] | None = None
    for pattern, mention in _MENTIONS:
        for match in pattern.finditer(text):
            if best is None or match.end() > best[0]:
                best = (match.end(), mention)
    return best


def _mention_for(person_surname: str, segments: Iterable[str]) -> Mention:
    """Ce que la mention de responsabilité dit d'une personne : le verbe le plus proche avant son nom.

    « adaptation et dessin par Paul Brizzi de la nouvelle de Maupassant » : Brizzi dessine, Maupassant est
    l'auteur d'origine. Sans verbe avant le nom, le morceau entier est lu ; sans verbe du tout, rien.
    """
    tokens = person_tokens(person_surname)
    if not tokens:
        return ""
    key = " ".join(sorted(tokens, key=len, reverse=True)[:1])
    for piece in segments:
        plain = normalize(piece)
        if not tokens <= person_tokens(piece):
            continue
        position = plain.find(key)
        before = _mention_in(plain[:position]) if position > 0 else None
        if before is not None:
            return before[1]
        whole = _mention_in(plain)
        return whole[1] if whole is not None else ""
    return ""


def contributors_of(record: Record) -> tuple[Person, ...]:
    segments = _mentions(record)
    people: list[Person] = []
    for field in record.fields:
        if field.tag not in ("700", "701", "702", "710", "711", "712"):
            continue
        surname, forename = field.first("a"), field.first("b")
        if not surname:
            continue
        organization = field.tag.startswith("71")
        name = surname if organization or not forename else f"{forename} {surname}"
        people.append(
            Person(
                name=name,
                surname=surname,
                forename=forename,
                dates=field.first("f"),
                authority_id=field.first("3"),
                kind="organization" if organization else "person",
                function_code=field.first("4"),
                function_text=field.first("c") if not organization else "",
                mention=_mention_for(surname, segments),
                origin=field.tag,
            )
        )
    return tuple(people)


def _series_and_volume(record: Record) -> tuple[Text | None, Number | None]:
    """Série et tome, par ordre de confiance : lien d'ensemble (461), titre de tome (200$h, $i), collection (225).

    Un numéro lu en 225 peut être un numéro de collection (« Folio junior, 100 ») : son origine le dit, et le
    rapprochement ne le tient pour un tome que si la série locale porte ce nom.
    """
    link = record.first("461")
    if link is not None and link.first("t"):
        series = Text(link.first("t"), link.first("t"), "461$t")
        number = parse_volume(link.first("v"))
        return series, Number(number, link.first("v"), "461$v") if number is not None else None
    title = record.first("200")
    if title is not None and title.first("h"):
        number = parse_volume(title.first("h"))
        named = Text(title.first("a"), title.first("a"), "200$a") if title.first("i") else None
        return named, Number(number, title.first("h"), "200$h") if number is not None else None
    collection = record.first("225")
    if collection is not None and collection.first("a"):
        series = Text(collection.first("a"), collection.first("a"), "225$a")
        number = parse_volume(collection.first("v"))
        return series, Number(number, collection.first("v"), "225$v") if number is not None else None
    return None, None


def _summary(record: Record) -> Summary | None:
    field = record.first("330")
    if field is None or not field.first("a"):
        return None
    text, provider = field.first("a"), field.first("2")
    if not provider and _THIRD_PARTY_SUMMARY.match(text):
        provider = "quatrième de couverture"
    elif not provider and _QUOTED_SUMMARY.match(text):
        provider = "citation"
    return Summary(text, "330", provider, "third_party" if provider else "established")


def _work(record: Record) -> WorkData:
    title = record.first("200")
    main = title.first("a") if title is not None else ""
    if title is not None and title.first("i"):
        main = title.first("i")  # le titre du tome ; $a est la série
    subtitle = title.first("e") if title is not None else ""
    series, volume = _series_and_volume(record)
    responsibility = " ; ".join(_mentions(record))
    return WorkData(
        title=Text(main, main, "200$i" if title is not None and title.first("i") else "200$a"),
        subtitle=Text(subtitle, subtitle, "200$e") if subtitle else None,
        contributors=contributors_of(record),
        series=series,
        volume=volume,
        summary=_summary(record),
        subjects=tuple(
            Text(" -- ".join(f.all("a") + f.all("x")), f.first("a"), "606") for f in record.get("606") if f.first("a")
        ),
        genres=tuple(Text(f.first("a"), f.first("a"), "608") for f in record.get("608") if f.first("a")),
        responsibility=responsibility,
    )


# --- Édition ----------------------------------------------------------------------------------------------------------
def _edition(record: Record) -> EditionData:
    valid: list[str] = []
    invalid: list[str] = []
    for field in record.get("010"):
        for raw in field.all("a"):  # $z, l'ISBN erroné, n'est jamais lu
            isbn = isbn13_of(raw)
            if isbn and isbn not in valid:
                valid.append(isbn)
            elif not isbn and raw:
                invalid.append(raw)
    ean = tuple(dict.fromkeys(v for f in record.get("073") for v in f.all("a") if _EAN.match(v)))
    publication = record.first("210") or record.first("214")
    tag = publication.tag if publication is not None else "210"
    publisher = publication.first("c") if publication is not None else ""
    place = publication.first("a") if publication is not None else ""
    publisher = "" if publisher.casefold() in _NO_NAME else publisher
    place = "" if place.casefold() in _NO_NAME else place
    date = publication.first("d") if publication is not None else ""
    year = parse_year(date)
    statement = record.first("205")
    collection = record.first("225")
    authority = record.first("410")
    number_raw, number_origin = "", ""
    if collection is not None and collection.first("v"):
        number_raw, number_origin = collection.first("v"), "225$v"
    elif authority is not None and authority.first("v"):
        number_raw, number_origin = authority.first("v"), "410$v"
    physical = record.first("215")
    pages = parse_pages(physical.first("a")) if physical is not None else None
    language = record.first("101")
    return EditionData(
        isbn13=tuple(valid),
        invalid_isbn=tuple(invalid),
        ean=ean,
        publisher=Text(publisher, publisher, f"{tag}$c") if publisher else None,
        place=Text(place, place, f"{tag}$a") if place else None,
        year=Number(year, date, f"{tag}$d") if year is not None else None,
        statement=Text(statement.first("a"), statement.first("a"), "205$a")
        if statement and statement.first("a")
        else None,
        collection=Text(collection.first("a"), collection.first("a"), "225$a")
        if collection and collection.first("a")
        else None,
        collection_number=Text(number_raw, number_raw, number_origin) if number_raw else None,
        pages=Number(pages, physical.first("a"), "215$a") if pages is not None and physical is not None else None,
        physical=Text(
            " ; ".join(v for _c, v in physical.subfields), " ; ".join(v for _c, v in physical.subfields), "215"
        )
        if physical is not None
        else None,
        language=Text(language.first("a"), language.first("a"), "101$a") if language and language.first("a") else None,
    )


# --- Provenance -------------------------------------------------------------------------------------------------------
def agency_of(record: Record) -> tuple[str, str]:
    """Organisme et date de la notice (801$b, 801$c) : l'organisme diffuseur (ind2 = 3), sinon le créateur (0)."""
    fields = record.get("801")
    for wanted in ("3", "0"):
        for field in fields:
            if field.ind2 == wanted and field.first("b"):
                return field.first("b"), field.first("c")
    for field in fields:
        if field.first("b"):
            return field.first("b"), field.first("c")
    return "", ""


def cover_sides_of(record: Record) -> tuple[int, ...]:
    sides: list[int] = []
    for field in record.get("856"):
        label = field.first("b").casefold()
        if "couverture" not in label:
            continue
        side = 4 if "quatri" in label else 1 if "premi" in label else 0
        if side and side not in sides:
            sides.append(side)
    return tuple(sides)


def candidate(
    record: Record, *, source: str, url: str, external_id: str, status: RecordStatus | None = None
) -> Candidate:
    """Le candidat d'une notice nettoyée. Le connecteur fournit l'identifiant et l'adresse publique."""
    return Candidate(
        source=source,
        external_id=external_id,
        url=url,
        work=_work(record),
        edition=_edition(record),
        record_status=status or status_of(record),
        record_kind=kind_of(record),
        medium="text" if medium_of(record) == "text" else "other",
        source_agency=agency_of(record)[0],
        source_date=agency_of(record)[1],
        linked_ids=linked_ids(record),
        cover_sides=cover_sides_of(record),
        leader=record.leader,
        controls=record.controls,
        raw=tuple((f.tag, f.ind1 + f.ind2, f.subfields) for f in record.fields),
    )


# --- Conservation -----------------------------------------------------------------------------------------------------
def record_to_json(record: Record) -> dict[str, Any]:
    """La notice nettoyée, telle qu'elle se conserve (cache, proposition) et se relit par le lecteur courant."""
    return {
        "id": record.record_id,
        "leader": record.leader,
        "controls": [list(pair) for pair in record.controls],
        "fields": [[f.tag, f.ind1, f.ind2, [list(pair) for pair in f.subfields]] for f in record.fields],
    }


def record_from_json(data: dict[str, Any]) -> Record:
    raw_controls: list[list[str]] = list(data.get("controls", []))
    raw_fields: list[list[Any]] = list(data.get("fields", []))
    controls = tuple((str(k), str(v)) for k, v in raw_controls)
    fields = tuple(
        Field(str(tag), str(ind1), str(ind2), tuple((str(c), str(v)) for c, v in subs))
        for tag, ind1, ind2, subs in raw_fields
    )
    return Record(str(data.get("leader", "")), controls, fields, str(data.get("id", "")))


def record_of(candidate: Candidate) -> Record:
    """La notice nettoyée d'un candidat, pour la conserver avec la proposition."""
    fields = tuple(Field(tag, ind[:1] or " ", ind[1:2] or " ", subs) for tag, ind, subs in candidate.raw)
    record_id = candidate.external_id if candidate.external_id.startswith("ark:/") else ""
    return Record(candidate.leader, candidate.controls, fields, record_id)
