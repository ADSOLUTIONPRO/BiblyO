# SPDX-License-Identifier: EUPL-1.2
from fastapi import APIRouter
from fastapi.testclient import TestClient

from biblyo.app import create_app
from biblyo.core.modules import Module


def test_health_is_public(client):
    response = client.get("/sante")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_about_shows_version_and_licence(client):
    response = client.get("/a-propos")
    assert response.status_code == 200
    assert "EUPL-1.2" in response.text
    assert "provisoire" in response.text
    assert "dépôt non encore publié" in response.text


def test_about_links_to_configured_source(settings):
    settings.source_url = "https://example.org/biblyo"
    with TestClient(create_app(settings)) as test_client:
        assert 'href="https://example.org/biblyo"' in test_client.get("/a-propos").text


def test_unknown_page_is_french_404(client):
    response = client.get("/nimporte-quoi")
    assert response.status_code == 404
    assert "Page introuvable" in response.text


def test_unexpected_error_reveals_nothing(settings):
    router = APIRouter()

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret interne : /data/biblyo.sqlite")

    module = Module("boom", "Boom", router=router, public_paths=("/boom",))
    with TestClient(create_app(settings, modules=[module]), raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")
    assert response.status_code == 500
    assert "Une erreur est survenue" in response.text
    assert "secret interne" not in response.text
    assert "Traceback" not in response.text
