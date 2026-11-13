# SPDX-License-Identifier: EUPL-1.2
"""Recherche de couvertures (conception 17 § 4) et historique des changements, sur un réseau simulé.

Essais de la mission 7 : 1 (couverture BnF correcte), 2 (absence), 3 (HTTP 500), 4 et 5 (avec et sans ISBN),
6 et 7 (plusieurs éditions, plusieurs images), 18 (annulation d'un enrichissement consignée)."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.core.media import store_image
from biblyo.modules.catalogue import repository as catalogue_repo
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.enrichment import cover_search, covers
from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.base import Capability, CoverRequest
from biblyo.modules.enrichment.connectors.openlibrary import OpenLibraryConnector, image_host
from biblyo.modules.enrichment.connectors.transport import HttpTransport
from biblyo.modules.enrichment.matching import LocalFiche
from biblyo.modules.enrichment.sources import SourceHub
from biblyo.modules.enrichment.throttle import Throttle
from tests.factories import make_notice
from tests.sources import read_bytes

#: Open Library exige une adresse de contact de l'instance : sans elle, la source reste inactive.
CONTACT_SQL = (
    "INSERT OR REPLACE INTO settings (key, value, updated_at) "
    "VALUES ('enrichment.contact', '\"bibliotheque@exemple.invalid\"', '2026-11-13T00:00:00+00:00')"
)

NO_IMAGE = b"<html><body>id to load is required for loading</body></html>"


def jpeg(seed: str) -> bytes:
    digest = hashlib.sha256(seed.encode()).digest()
    buffer = io.BytesIO()
    Image.new("RGB", (400, 583), (digest[0], digest[1], digest[2])).save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


class Network:
    """BnF : SRU et Couvertures ; Open Library : notice d'ISBN, recherche, images après deux redirections."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.no_image: set[str] = set()  # ARK pour lesquels le service répond 500 « id to load »
        self.same_image = False
        self.down = False
        self.other_work = False  # Open Library rend aussi une œuvre d'un autre titre

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if self.down:
            return httpx.Response(503, content=b"panne")
        query = parse_qs(urlsplit(url).query)
        if "recupererImage" in url:
            key = (query.get("idArk") or query.get("EAN") or [""])[0]
            if key in self.no_image:
                return httpx.Response(500, content=NO_IMAGE, headers={"Content-Type": "text/html"})
            return httpx.Response(
                200, content=jpeg("même" if self.same_image else key), headers={"Content-Type": "image/jpeg"}
            )
        if "isbn%20any%20%229782070612758" in url:
            return self._xml("bnf/isbn-exact-petit-prince")
        if "bib.title" in url and "horla" in url:
            return self._xml("bnf/meme-titre-plusieurs-editeurs")
        if "bib.title" in url and "tour%20de%20gaule" in url:
            return self._xml("bnf/bd-tome-meme-titre")
        if "bib.title" in url and "asterix%20le%20gaulois" in url:
            return self._xml("bnf/bd-asterix-unimarc")
        if url.startswith("https://openlibrary.org/api/books"):
            body = {
                "ISBN:9782070612758": {
                    "title": "Le Petit Prince",
                    "publishers": [{"name": "Gallimard"}],
                    "publish_date": "2007",
                    "url": "https://openlibrary.org/books/OL1M/x",
                    "cover": {"large": "https://covers.openlibrary.org/b/id/2137711-L.jpg"},
                }
            }
            return httpx.Response(200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        if url.startswith("https://openlibrary.org/search.json"):
            docs = [{"key": "/works/OL2W", "title": "Le Petit Prince", "first_publish_year": 1943, "cover_i": 8400001}]
            if self.other_work:
                docs.append(
                    {"key": "/works/OL3W", "title": "Le Compagnon du Petit Prince Workbook", "cover_i": 8400002}
                )
            body = {"docs": docs}
            return httpx.Response(200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        if request.url.host == "covers.openlibrary.org":
            return httpx.Response(302, headers={"Location": "https://archive.org/download/x.zip/" + request.url.path})
        if request.url.host == "archive.org":
            return httpx.Response(
                302, headers={"Location": "https://ia800703.us.archive.org/view?f=" + request.url.path}
            )
        if request.url.host.endswith(".us.archive.org"):
            return httpx.Response(200, content=jpeg(url), headers={"Content-Type": "image/jpeg"})
        default = "sudoc/sru-isbn-inconnu" if "sudoc" in url else "bnf/isbn-inconnu"
        return self._xml(default)

    @staticmethod
    def _xml(name: str) -> httpx.Response:
        return httpx.Response(200, content=read_bytes(name), headers={"Content-Type": "text/xml"})

    def count(self, part: str) -> int:
        return sum(1 for url in self.calls if part in url)


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


def make_hub(database: Database, network: Network) -> SourceHub:
    client = httpx.Client(transport=httpx.MockTransport(network.handler), follow_redirects=False, trust_env=False)
    transport = HttpTransport(registry.HOSTS, resolver=lambda host: ["194.199.8.183"], client=client)
    clock = {"now": 0.0}

    def tick(seconds: float) -> None:
        clock["now"] += seconds

    return SourceHub(
        database, transport=transport, throttle=Throttle(clock=lambda: clock["now"], sleep=tick), sleep=tick
    )


def previews() -> cover_search.Previews:
    return cover_search.Previews(covers.PreviewCache())


def execute(settings, query: str, *params) -> None:
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


def rows(settings, query: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


PETIT_PRINCE = LocalFiche(
    title="Le Petit Prince",
    contributors=("Antoine de Saint-Exupéry",),
    isbn13=("9782070612758",),
    publisher="Gallimard",
)


def test_the_bnf_cover_of_the_same_isbn_is_proposed_as_this_edition(database):
    """Essais 1 et 4."""
    network = Network()
    outcome = cover_search.search(make_hub(database, network), previews(), PETIT_PRINCE)
    first = outcome.proposals[0]
    assert (first.source, first.reference, first.match) == ("bnf", "ark:/12148/cb41023439w", "same_edition")
    assert "Gallimard" in first.edition and first.notice_url.startswith("https://catalogue.bnf.fr/")
    assert {line.code: line.status for line in outcome.sources} == {"bnf": "found", "openlibrary": "disabled"}
    assert network.count("openlibrary") == 0  # désactivée : jamais appelée


def test_a_notice_without_announced_image_is_never_asked_and_gives_no_proposal(database):
    """Essai 2 : Astérix 1999 n'annonce aucune image ; le service n'est pas interrogé (audit du 12 novembre)."""
    network = Network()
    local = LocalFiche(title="Astérix le Gaulois", contributors=("René Goscinny",))
    outcome = cover_search.search(make_hub(database, network), previews(), local)
    assert outcome.proposals == [] and not outcome.failed
    assert network.count("recupererImage") == 0
    assert {line.code: line.status for line in outcome.sources}["bnf"] == "none"


def test_an_http_500_is_no_image_not_a_failure(database):
    """Essai 3."""
    network = Network()
    network.no_image = {"ark:/12148/cb41023439w", "9782070612758"}
    outcome = cover_search.search(make_hub(database, network), previews(), PETIT_PRINCE)
    assert outcome.proposals == [] and outcome.without_image == 1 and not outcome.failed


def test_an_image_refused_by_ark_is_asked_by_ean(database):
    """One Piece 112, édition originale (relevé du 12 novembre) : « annoncée, non servie » par ARK, servie par EAN."""
    network = Network()
    network.no_image = {"ark:/12148/cb41023439w"}
    outcome = cover_search.search(make_hub(database, network), previews(), PETIT_PRINCE)
    assert [p.reference for p in outcome.proposals] == ["ark:/12148/cb41023439w"]
    assert network.count("EAN=9782070612758") == 1


def test_without_isbn_the_covers_of_several_editions_are_offered_never_as_this_edition(database):
    """Essais 5, 6 et 7 : Le Horla, cinq éditions qui annoncent une image, aucune présentée comme certaine."""
    network = Network()
    local = LocalFiche(title="Le Horla", contributors=("Guy de Maupassant",))
    outcome = cover_search.search(make_hub(database, network), previews(), local)
    assert len(outcome.proposals) >= 3
    assert all(p.match in ("presumed", "undetermined") for p in outcome.proposals)  # la fiche n'a pas d'ISBN
    assert len({p.reference for p in outcome.proposals}) == len(outcome.proposals)


def test_identical_images_are_shown_once(database):
    network = Network()
    network.same_image = True
    local = LocalFiche(title="Le Horla", contributors=("Guy de Maupassant",))
    outcome = cover_search.search(make_hub(database, network), previews(), local)
    assert len(outcome.proposals) == 1 and outcome.duplicates >= 2


def test_another_edition_of_the_same_isbn_fiche_is_labelled_as_such(database):
    """Le Tour de Gaule d'Astérix, fiche Atlas 2013 (pas d'image) : les images Hachette sont « autre édition »."""
    network = Network()
    local = LocalFiche(title="Le tour de Gaule d'Astérix", contributors=("René Goscinny",), isbn13=("9782731250619",))
    outcome = cover_search.search(make_hub(database, network), previews(), local)
    assert outcome.proposals and {p.match for p in outcome.proposals} == {"other_edition"}


def test_a_source_outage_is_a_failure_with_no_proposal(database):
    network = Network()
    network.down = True
    outcome = cover_search.search(make_hub(database, network), previews(), PETIT_PRINCE)
    assert outcome.failed and {line.code: line.status for line in outcome.sources}["bnf"] == "unavailable"


def test_open_library_once_enabled_adds_its_images_through_archive_org(settings, database):
    execute(settings, "UPDATE metadata_sources SET is_enabled = 1, covers_enabled = 1 WHERE code = 'openlibrary'")
    execute(settings, CONTACT_SQL)
    network = Network()
    network.no_image = {"ark:/12148/cb41023439w", "9782070612758"}  # la BnF n'a rien : Open Library seule
    outcome = cover_search.search(make_hub(database, network), previews(), PETIT_PRINCE)
    matches = {p.reference: p.match for p in outcome.proposals}
    assert matches == {"2137711": "same_edition", "8400001": "work"}
    assert all(p.source == "openlibrary" for p in outcome.proposals)
    assert network.count("ia800703.us.archive.org") == 2


def test_another_work_is_never_proposed_but_counted(settings, database):
    """Arbitrage du 12 novembre 2026 : un ouvrage différent est exclu, et l'écran dit combien d'images l'ont été."""
    execute(settings, "UPDATE metadata_sources SET is_enabled = 1, covers_enabled = 1 WHERE code = 'openlibrary'")
    execute(settings, CONTACT_SQL)
    network = Network()
    network.other_work = True
    outcome = cover_search.search(make_hub(database, network), previews(), PETIT_PRINCE)
    assert "8400002" not in {p.reference for p in outcome.proposals}
    assert outcome.other_works == 1


def test_open_library_redirections_go_nowhere_else():
    assert image_host("archive.org") and image_host("ia800703.us.archive.org")
    assert not any(image_host(h) for h in ("evil.org", "archive.org.evil.org", "x.y.us.archive.org", "us.archive.org"))
    connector = OpenLibraryConnector(HttpTransport(registry.HOSTS, resolver=lambda h: ["194.199.8.183"]))
    assert connector.cover(CoverRequest(external_id="../../etc")).status == "none_found"
    assert connector.capabilities == frozenset({Capability.COVER})  # aucune notice


def test_a_cover_only_source_is_never_asked_for_records(settings, database):
    execute(settings, "UPDATE metadata_sources SET is_enabled = 1, covers_enabled = 1 WHERE code = 'openlibrary'")
    execute(settings, CONTACT_SQL)
    hub = make_hub(database, Network())
    assert [c.code for c in hub.metadata_configs()] == ["bnf", "sudoc"]
    assert [c.code for c in hub.cover_configs()] == ["bnf", "openlibrary"]


# --- Historique : la fonction unique de changement -------------------------------------------------------------------
def _edition(settings, work):
    database = Database(settings.database_path)
    with database.session() as session:
        edition = catalogue_repo.load_notice(session, work).primary
    database.dispose()
    return edition


def test_every_cover_change_is_recorded_with_the_previous_image(settings, database, tmp_path):
    work = make_notice(settings)
    edition = _edition(settings, work).id
    first = store_image(jpeg("a"), settings.media_dir)
    second = store_image(jpeg("b"), settings.media_dir)
    upload = catalogue.CoverChangeInput("upload", rights_note="Photo prise pour le Club")
    catalogue.save_cover(
        database, edition, first, None, provenance=None, change=upload, expected_media_id=None, replace=False
    )
    current = _edition(settings, work).cover.media_id
    catalogue.save_cover(
        database, edition, second, None, provenance=None, change=upload, expected_media_id=current, replace=True
    )
    catalogue.remove_cover(database, edition, None)
    assert rows(
        settings, "SELECT kind, previous_media_id IS NULL, media_id IS NULL FROM cover_changes ORDER BY id"
    ) == [("upload", 1, 0), ("upload", 0, 0), ("remove", 0, 1)]


def test_an_enrichment_cover_is_undone_by_the_enrichment_not_by_restore(settings, database):
    work = make_notice(settings)
    edition = _edition(settings, work).id
    stored = store_image(jpeg("bnf"), settings.media_dir)
    with database.session(write=True) as session:
        from biblyo.modules.catalogue.models import Edition

        record = session.get(Edition, edition)
        catalogue.attach_cover(
            session, record, stored, None, change=catalogue.CoverChangeInput("enrichment", "bnf", "ark:/12148/x")
        )
    with pytest.raises(BusinessError, match="onglet « Enrichissement »"):
        catalogue.restore_previous_cover(database, edition, None)


def test_the_purge_keeps_the_current_and_recent_images(settings, database):
    work = make_notice(settings)
    edition = _edition(settings, work).id
    old = store_image(jpeg("vieille"), settings.media_dir)
    new = store_image(jpeg("neuve"), settings.media_dir)
    change = catalogue.CoverChangeInput("upload")
    catalogue.save_cover(
        database, edition, old, None, provenance=None, change=change, expected_media_id=None, replace=False
    )
    current = _edition(settings, work).cover.media_id
    catalogue.save_cover(
        database, edition, new, None, provenance=None, change=change, expected_media_id=current, replace=True
    )
    assert catalogue.purge_media(database, settings.media_dir, apply=True).unused_media == ()
    later = catalogue.purge_media(database, settings.media_dir, apply=True, today="2027-06-01T00:00:00Z")
    assert later.unused_media == (old.sha256,)
    assert not (settings.media_dir / f"{old.sha256}.webp").exists()
    assert (settings.media_dir / f"{new.sha256}.webp").exists()
    assert rows(settings, "SELECT COUNT(*) FROM cover_changes") == [(2,)]  # l'historique reste, sans l'image


def test_another_volume_named_in_the_title_is_never_proposed():
    """One Piece, tome 112 : la notice « One Piece 113 », sans tome lisible, a été proposée lors de l'essai réel."""
    from tests.unit.test_enrichment_matching import make

    local = LocalFiche(title="One Piece", series="One Piece", volume=112.0)
    assert cover_search._other_volume(local, make("One Piece 113"))
    assert not cover_search._other_volume(local, make("One Piece"))
    assert not cover_search._other_volume(local, make("One Piece 112"))
    assert not cover_search._other_volume(LocalFiche(title="Le Horla"), make("Le Horla 1887"))


def test_a_different_title_is_never_proposed():
    """Recette sur DEV : « Je joue avec le Petit Prince » ou « Le Petit Prince et Okoda » ne sont pas ce livre."""
    from biblyo.modules.enrichment import matching
    from tests.unit.test_enrichment_matching import make

    people = (("Saint-Exupéry", "Antoine de"),)
    candidates = [
        make("Le Petit Prince", isbn=("9782070652099",), publisher="Gallimard jeunesse", year=2013, people=people),
        make(
            "Je joue avec le Petit Prince",
            isbn=("9782070650002",),
            publisher="Gallimard jeunesse",
            people=people,
            external_id="ark:/12148/cb2",
        ),
        make(
            "Le Petit Prince et Okoda",
            isbn=("9782070650019",),
            publisher="Gallimard jeunesse",
            people=people,
            external_id="ark:/12148/cb3",
        ),
    ]
    ranked = matching.classify(PETIT_PRINCE, [matching.assess(PETIT_PRINCE, c) for c in candidates])
    kept = {a.candidate.work.title.value for a in ranked if cover_search._same_book(a)}
    assert kept == {"Le Petit Prince"}


def test_without_isbn_another_publisher_is_another_edition_not_one_to_check():
    """L'écume des jours, fiche Gallimard sans ISBN : les albums Delcourt sont « autre édition »."""
    from biblyo.modules.enrichment import matching
    from tests.unit.test_enrichment_matching import make

    local = LocalFiche(title="L'écume des jours", contributors=("Boris Vian",), publisher="Gallimard")
    people = (("Vian", "Boris"),)
    delcourt = make("L'écume des jours", isbn=("9782413026396",), publisher="Delcourt", year=2020, people=people)
    gallimard = make(
        "L'écume des jours",
        isbn=("9782070360109",),
        publisher="Gallimard",
        year=1972,
        people=people,
        external_id="ark:/12148/cb4",
    )
    ranked = matching.classify(local, [matching.assess(local, c) for c in (delcourt, gallimard)])
    match = {a.candidate.edition.publisher.value: cover_search._bnf_match(local, a) for a in ranked}
    assert match["Delcourt"] == "other_edition"
    assert match["Gallimard"] in ("presumed", "undetermined")


def test_an_open_library_work_needs_the_same_title():
    assert cover_search._same_title("Le Petit Prince", "Le petit prince")
    assert not cover_search._same_title(
        "Œuvres romanesques 2", "Oeuvres romanesques complètes t.2 (Bibliotheque de la Pleiade) (French Edition)"
    )
