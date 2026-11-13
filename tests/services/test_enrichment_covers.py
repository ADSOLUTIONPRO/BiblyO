# SPDX-License-Identifier: EUPL-1.2
"""Couvertures de la BnF sur un réseau simulé : récupération, contrôle, provenance, remplacement, annulation, pannes."""

from __future__ import annotations

import io
import sqlite3
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from biblyo.core.db import Database
from biblyo.modules.catalogue import repository as catalogue_repo
from biblyo.modules.catalogue import service as catalogue
from biblyo.modules.catalogue.models import Edition
from biblyo.modules.enrichment import service
from biblyo.modules.enrichment.connectors import registry
from biblyo.modules.enrichment.connectors.base import CoverRequest
from biblyo.modules.enrichment.connectors.bnf import COVER_URL, BnfConnector, CoverSettings
from biblyo.modules.enrichment.connectors.transport import HttpTransport
from biblyo.modules.enrichment.service import Acceptance
from biblyo.modules.enrichment.sources import SourceHub
from biblyo.modules.enrichment.throttle import Throttle
from tests.factories import make_notice
from tests.sources import read_bytes

PETIT_PRINCE = "9782070612758"
ARK = "ark:/12148/cb41023439w"
NO_IMAGE = (
    b"<!doctype html><html><body><p>Request processing failed; nested exception is java.lang."
    b"IllegalArgumentException: id to load is required for loading</p></body></html>"
)


def jpeg(color=(200, 90, 60), size=(400, 583)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


class Network:
    def __init__(self, cover: tuple[int, bytes, str] | None = None) -> None:
        self.cover = cover or (200, jpeg(), "image/jpeg")
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if "recupererImage" in url:
            status, body, kind = self.cover
            return httpx.Response(status, content=body, headers={"Content-Type": kind})
        if f"bib.isbn%20any%20%22{PETIT_PRINCE}" in url:
            return httpx.Response(
                200, content=read_bytes("bnf/isbn-exact-petit-prince"), headers={"Content-Type": "text/xml"}
            )
        if f"isb%3D{PETIT_PRINCE}" in url:
            return httpx.Response(
                200, content=read_bytes("sudoc/sru-isbn-petit-prince"), headers={"Content-Type": "text/xml"}
            )
        default = "sudoc/sru-isbn-inconnu" if "sudoc" in url else "bnf/isbn-inconnu"
        return httpx.Response(200, content=read_bytes(default), headers={"Content-Type": "text/xml"})

    @property
    def cover_calls(self) -> list[str]:
        return [url for url in self.calls if "recupererImage" in url]


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


def rows(settings, query: str, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def execute(settings, query: str, *params) -> None:
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


def edition_of(database: Database, work_id: int) -> int:
    with database.session() as session:
        return session.scalar(select(Edition.id).where(Edition.work_id == work_id, Edition.is_primary.is_(True)))


def cover_of(database: Database, work_id: int):
    with database.session() as session:
        return catalogue_repo.load_notice(session, work_id).primary.cover


def searched(settings, database, network: Network, **fields):
    work = make_notice(settings, "Le Petit Prince", isbn=PETIT_PRINCE, **fields)
    edition = edition_of(database, work)
    hub = make_hub(database, network)
    outcome = service.search(database, hub, edition_id=edition, user_id=None)
    return work, edition, hub, outcome


def proposal_of(outcome, source: str = "bnf") -> int:
    return next(pid for key, pid in outcome.proposals.items() if key.startswith(source + ":"))


def apply(settings, database, hub, proposal: int, fields: set[str], **acceptance):
    return service.apply(
        database,
        proposal_id=proposal,
        acceptance=Acceptance(fields=frozenset(fields), **acceptance),
        user_id=None,
        hub=hub,
        media_dir=settings.media_dir,
    )


def test_cover_is_fetched_checked_stored_and_credited(settings, database):
    """V11 : couverture disponible, par l'ARK, en taille d'origine, avec fournisseur, notice, date et conditions."""
    network = Network()
    work, _edition, hub, outcome = searched(settings, database, network)
    result = apply(settings, database, hub, proposal_of(outcome), {"cover", "year"})
    assert result.cover_status == "found" and set(result.applied) == {"cover", "year"}
    [url] = network.cover_calls
    assert urlsplit(url).hostname == "openapi.bnf.fr"
    assert parse_qs(urlsplit(url).query) == {"idArk": [ARK], "couverture": ["1"], "taille": ["originale"]}
    cover = cover_of(database, work)
    assert cover is not None and (settings.media_dir / f"{cover.sha256}.webp").is_file()
    media = rows(
        settings,
        "SELECT mime, origin, origin_url, source_code, external_id, license_note, retrieved_at FROM media_files",
    )
    [(mime, origin, origin_url, source, external, licence, retrieved)] = media
    assert (mime, origin, source, external) == ("image/webp", "bnf", "bnf", ARK)
    assert origin_url == f"https://catalogue.bnf.fr/{ARK}"
    assert "service Couvertures du catalogue général de la BnF" in licence
    assert "conditions de réutilisation des données de la BnF" in licence
    assert f"Récupérée le {retrieved[:10]}" in licence
    provenance = rows(
        settings, "SELECT source_code, external_id, license_note FROM field_provenance WHERE field = 'cover'"
    )
    assert provenance and provenance[0][:2] == ("bnf", ARK) and "Couvertures" in provenance[0][2]
    assert rows(settings, "SELECT action FROM enrichment_decisions WHERE field = 'cover'") == [("fill",)]


@pytest.mark.parametrize(
    ("cover", "status"),
    [
        ((503, b"", "text/html"), "unavailable"),
        ((500, b"<html>autre panne</html>", "text/html"), "unavailable"),
        ((500, NO_IMAGE, "text/html;charset=utf-8"), "announced_not_served"),
        ((200, b"<html>pas une image</html>", "image/jpeg"), "invalid_image"),
        ((200, b"\xff\xd8\xff\xe0" + b"\x00" * 200, "image/jpeg"), "invalid_image"),
    ],
)
def test_a_cover_failure_never_blocks_the_other_fields(settings, database, cover, status):
    """V12 et image piégée : rien n'est rangé, les autres champs cochés sont appliqués."""
    work, _edition, hub, outcome = searched(settings, database, Network(cover))
    result = apply(settings, database, hub, proposal_of(outcome), {"cover", "year", "pages"})
    assert result.cover_status == status
    assert set(result.applied) == {"year", "pages"}
    assert any(name == "cover" for name, _reason in result.skipped)
    assert cover_of(database, work) is None
    assert rows(settings, "SELECT COUNT(*) FROM media_files") == [(0,)]
    assert rows(settings, "SELECT COUNT(*) FROM enrichment_decisions WHERE field = 'cover'") == [(0,)]


def test_http_500_without_announcement_is_no_image_not_a_failure():
    connector = BnfConnector(
        HttpTransport(
            registry.HOSTS,
            resolver=lambda h: ["194.199.8.183"],
            client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, content=NO_IMAGE))),
        )
    )
    assert connector.cover(CoverRequest(external_id="ark:/12148/cb43404620t")).status == "none_found"
    assert (
        connector.cover(CoverRequest(external_id="ark:/12148/cb43404620t", announced=True)).status
        == "announced_not_served"
    )


def test_replacing_an_existing_cover_needs_its_own_confirmation(settings, database):
    network = Network()
    work, edition, hub, outcome = searched(settings, database, network)
    catalogue.set_cover(database, edition, jpeg((10, 120, 200)), settings.media_dir, None)
    before = cover_of(database, work).sha256
    outcome = service.search(database, hub, edition_id=edition, user_id=None)
    result = apply(settings, database, hub, proposal_of(outcome), {"cover", "year"})
    assert result.cover_status == "not_confirmed" and network.cover_calls == []
    assert cover_of(database, work).sha256 == before
    outcome = service.search(database, hub, edition_id=edition, user_id=None)
    result = apply(settings, database, hub, proposal_of(outcome), {"cover"}, replace_cover=True)
    assert result.cover_status == "found" and "cover" in result.applied
    after = cover_of(database, work).sha256
    assert after != before
    assert rows(settings, "SELECT action, old_value FROM enrichment_decisions WHERE field = 'cover'") == [
        ("replace", f'"{before}"')
    ]
    # Annulation : l'ancienne couverture revient, la nouvelle reste dans les médias
    reverted = service.revert(database, operation_id=result.operation_id, user_id=None)
    assert reverted.restored == ("cover",)
    assert cover_of(database, work).sha256 == before
    assert rows(settings, "SELECT COUNT(*) FROM media_files") == [(2,)]
    assert rows(settings, "SELECT COUNT(*) FROM field_provenance WHERE field = 'cover'") == [(0,)]


def test_revert_does_not_remove_a_cover_changed_since(settings, database):
    network = Network()
    work, edition, hub, outcome = searched(settings, database, network)
    result = apply(settings, database, hub, proposal_of(outcome), {"cover"})
    catalogue.set_cover(database, edition, jpeg((0, 200, 0)), settings.media_dir, None)
    changed = cover_of(database, work).sha256
    reverted = service.revert(database, operation_id=result.operation_id, user_id=None)
    assert reverted.restored == () and [c[0] for c in reverted.conflicts] == ["cover"]
    assert cover_of(database, work).sha256 == changed


def test_a_sudoc_record_citing_the_bnf_offers_its_cover(settings, database):
    network = Network()
    _work, _edition, hub, outcome = searched(settings, database, network)
    result = apply(settings, database, hub, proposal_of(outcome, "sudoc"), {"cover"})
    assert result.cover_status == "found"
    assert parse_qs(urlsplit(network.cover_calls[0]).query)["idArk"] == [ARK]
    assert rows(settings, "SELECT source_code, external_id FROM media_files") == [("bnf", ARK)]


def test_disabled_covers_are_never_requested(settings, database):
    execute(settings, "UPDATE metadata_sources SET covers_enabled = 0 WHERE code = 'bnf'")
    network = Network()
    _work, _edition, hub, outcome = searched(settings, database, network)
    result = apply(settings, database, hub, proposal_of(outcome), {"cover", "year"})
    assert result.cover_status == "unsupported" and result.applied == ("year",)
    assert network.cover_calls == []


def test_without_hub_or_media_dir_the_cover_is_skipped(settings, database):
    network = Network()
    _work, _edition, _hub, outcome = searched(settings, database, network)
    result = service.apply(
        database,
        proposal_id=proposal_of(outcome),
        acceptance=Acceptance(fields=frozenset({"cover", "year"})),
        user_id=None,
    )
    assert result.cover_status == "unsupported" and result.applied == ("year",)


def test_cover_settings_follow_the_beta_api_within_declared_hosts():
    assert CoverSettings.from_config({}) == CoverSettings()
    moved = CoverSettings.from_config(
        {"covers": {"url": "https://openapi.bnf.fr/couverture/v2/image", "ark_param": "ark", "size_param": "size"}}
    )
    assert (moved.url, moved.ark_param, moved.size_param) == (
        "https://openapi.bnf.fr/couverture/v2/image",
        "ark",
        "size",
    )
    for url in (
        "https://evil.example/image",
        "http://openapi.bnf.fr/x",
        "https://openapi.bnf.fr/x?y=1",
        "https://openapi.bnf.fr:8443/x",
    ):
        assert CoverSettings.from_config({"covers": {"url": url}}).url == COVER_URL
    assert CoverSettings.from_config({"covers": {"ark_param": "id&evil=1"}}).ark_param == "idArk"
    connector = BnfConnector(
        HttpTransport(registry.HOSTS, resolver=lambda h: ["194.199.8.183"]), config={"covers": {"ark_param": "ark"}}
    )
    assert "ark=ark%3A%2F12148%2Fcb41023439w" in connector.cover_url(CoverRequest(external_id=ARK))
