# SPDX-License-Identifier: EUPL-1.2
"""Assistant d'enrichissement (L7, T2), sur les réponses réelles relevées le 14 novembre 2026 (fixtures `l7`).

Cas de production : « Cerveau-choc » (Achille Talon, Greg, Dargaud) et « Du scénario à la réalisation » (Duc, Glénat).
Les images sont simulées : la BnF n'en sert que pour les notices qui l'annoncent (comme le vrai service, relevé du
14 novembre) ; Open Library sert une image pour chaque numéro demandé, après ses deux redirections.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from biblyo.core.db import Database
from biblyo.modules.enrichment import assistant, cover_search, covers, service
from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.transport import HttpTransport
from biblyo.modules.enrichment.repository import load_target
from biblyo.modules.enrichment.sources import SourceHub
from biblyo.modules.enrichment.throttle import Throttle
from tests.factories import make_notice

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "l7"
MANIFEST: dict[str, str] = json.loads((FIXTURES / "manifeste.json").read_text(encoding="utf-8"))
BOOKS: dict[str, object] = json.loads((FIXTURES / "openlibrary-livres.json").read_text(encoding="utf-8"))
ANNOUNCED = "ark:/12148/cb41279773q"  # « Achille Talon » tome 1, Dargaud 2007 : la seule notice qui annonce une image
CONTACT_SQL = (
    "INSERT OR REPLACE INTO settings (key, value, updated_at) "
    "VALUES ('enrichment.contact', '\"bibliotheque@exemple.invalid\"', '2026-11-14T00:00:00+00:00')"
)
ENABLE_OPEN_LIBRARY = "UPDATE metadata_sources SET is_enabled = 1, covers_enabled = 1 WHERE code = 'openlibrary'"


def jpeg(seed: str) -> bytes:
    digest = hashlib.sha256(seed.encode()).digest()
    buffer = io.BytesIO()
    Image.new("RGB", (400, 583), (digest[0], digest[1], digest[2])).save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


class Network:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.covers_down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url in MANIFEST:
            kind = "application/json" if MANIFEST[url].endswith(".json") else "text/xml"
            return httpx.Response(200, content=(FIXTURES / MANIFEST[url]).read_bytes(), headers={"Content-Type": kind})
        if "recupererImage" in url:
            if self.covers_down:
                return httpx.Response(503, content=b"panne")
            query = parse_qs(urlsplit(url).query)
            if (query.get("idArk") or [""])[0] == ANNOUNCED:
                return httpx.Response(200, content=jpeg(ANNOUNCED), headers={"Content-Type": "image/jpeg"})
            return httpx.Response(
                500, content=b"id to load is required for loading", headers={"Content-Type": "text/html"}
            )
        if url.startswith("https://openlibrary.org/api/books"):
            keys = parse_qs(urlsplit(url).query)["bibkeys"][0].split(",")
            body = {key: BOOKS[key] for key in keys if key in BOOKS}
            return httpx.Response(200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        if request.url.host == "covers.openlibrary.org":
            return httpx.Response(302, headers={"Location": "https://archive.org/download/x.zip/" + request.url.path})
        if request.url.host == "archive.org":
            return httpx.Response(
                302, headers={"Location": "https://ia800703.us.archive.org/view?f=" + request.url.path}
            )
        if request.url.host.endswith(".us.archive.org"):
            return httpx.Response(200, content=jpeg(url), headers={"Content-Type": "image/jpeg"})
        if "sudoc" in url:  # requête non relevée : aucun résultat
            return httpx.Response(200, content=(FIXTURES.parent / "sudoc" / "sru-isbn-inconnu.xml").read_bytes())
        return httpx.Response(200, content=(FIXTURES.parent / "bnf" / "isbn-inconnu.xml").read_bytes())

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


def execute(settings, query: str) -> None:
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(query)
        connection.commit()
    finally:
        connection.close()


def suggest(settings, database, network, **notice) -> assistant.Suggestions:
    work_id = make_notice(settings, **notice)
    with database.session() as session:
        from biblyo.modules.catalogue import repository as catalogue_repo

        edition_id = catalogue_repo.load_notice(session, work_id).editions[0].id
        target = load_target(session, edition_id)
    hub = make_hub(database, network)
    outcome = service.search(database, hub, edition_id=edition_id, user_id=None)
    previews = cover_search.Previews(covers.PreviewCache())
    return assistant.suggestions(database, hub, previews, search_id=outcome.search_id, fiche=target.fiche)


CERVEAU_CHOC = {
    "title": "Cerveau-choc",
    "authors": ("Greg",),
    "publisher": "Dargaud",
    "series": "Achille Talon",
    "volume": "1",
}
DU_SCENARIO = {
    "title": "Du scénario à la réalisation",
    "authors": ("Bernard Duc",),
    "publisher": "Glénat",
    "document_type": "livre",
}


def test_cerveau_choc_proposes_the_1984_album_as_probable_never_another_volume(settings, database):
    found = suggest(settings, database, Network(), **CERVEAU_CHOC)
    first = found.main[0]
    assert first.title == "Les Idées d'Achille Talon, cerveau-choc" and first.year == "1984"
    assert first.reliability == "probable"  # titre abrégé reconnu ; sans ISBN confirmé, jamais « Même édition »
    shown = [card.title for card in found.cards]
    assert "Achille Talon" not in shown  # tome 1 de 2007, la seule image BnF : un autre ouvrage, jamais proposé
    assert all("cerveau-choc" in t.casefold() for t in shown)


def test_cerveau_choc_never_shows_the_2007_cover_or_a_broken_thumbnail(settings, database):
    network = Network()
    found = suggest(settings, database, network, **CERVEAU_CHOC)
    assert all(card.cover is None or card.cover.reference != ANNOUNCED for card in found.cards)
    assert {card.cover_state for card in found.cards} == {"none"}  # la BnF n'a pas d'image pour ces notices
    assert network.count("recupererImage") == 0  # aucune notice retenue n'en annonce : rien n'est demandé
    assert found.extra_covers == [] and found.open_library == "disabled"


def test_open_library_when_configured_offers_the_album_cover_as_another_edition_never_the_integral(settings, database):
    execute(settings, CONTACT_SQL)
    execute(settings, ENABLE_OPEN_LIBRARY)
    network = Network()
    found = suggest(settings, database, network, **CERVEAU_CHOC)
    assert found.open_library == "found"
    titles = [offer.title for offer in found.extra_covers]
    assert titles == ["Les idées d'Achille Talon cerveau-choc !"]  # l'intégrale (tome 1) n'est jamais proposée
    offer = found.extra_covers[0]
    assert offer.match == "work" and offer.needs_check
    assert network.count("openlibrary.org/api/books") <= 1 and network.count("search.json") == 1


def test_du_scenario_shows_both_editions_1981_and_1990_to_be_compared(settings, database):
    found = suggest(settings, database, Network(), **DU_SCENARIO)
    years = {card.year for card in found.cards}
    assert {"DL 1981", "1990"} <= years
    assert all(card.reliability == "check" for card in found.cards if card.year in ("DL 1981", "1990"))
    assert any(card.several for card in found.cards)


def test_unavailable_cover_service_is_said_unavailable_never_a_fake_image(settings, database):
    network = Network()
    network.covers_down = True
    found = suggest(settings, database, network, **CERVEAU_CHOC)
    assert all(card.cover is None for card in found.cards)


@pytest.mark.parametrize(
    ("match_class", "explanation", "expected"),
    [
        ("exact", {"isbn_equal": True, "edition_usable": True}, "same_edition"),
        ("strong", {"isbn_equal": True, "edition_usable": True}, "same_edition"),
        ("strong", {"isbn_equal": False, "edition_usable": True}, "probable"),
        ("exact", {"isbn_equal": False, "edition_usable": True}, "probable"),
        ("multiple", {"isbn_equal": False, "edition_usable": True}, "check"),
        ("uncertain", {"isbn_equal": True, "edition_usable": True}, "check"),
        ("strong", {"isbn_equal": False, "edition_usable": False}, "other_edition"),
    ],
)
def test_reliability_is_same_edition_only_with_an_identical_isbn(match_class, explanation, expected):
    assert assistant.reliability(match_class, explanation) == expected


@pytest.mark.parametrize(
    ("title", "plausible"),
    [
        ("Les idées d'Achille Talon cerveau-choc !", True),
        ("Intégrale d'Achille Talon, tome 1 (Les idées d'Achille Talon cerveau-choc)", False),
        ("Achille Talon, tome 25", False),
        ("Achille Talon... à bout portant !", False),
    ],
)
def test_open_library_work_images_are_kept_only_for_the_same_album(title, plausible):
    from biblyo.modules.enrichment.matching import LocalFiche

    fiche = LocalFiche(title="Cerveau-choc", series="Achille Talon", volume=1.0, contributors=("Greg",))
    assert assistant._plausible_work_image(fiche, title) is plausible
