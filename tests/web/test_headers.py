# SPDX-License-Identifier: EUPL-1.2
"""En-têtes de sécurité : présents sur toutes les réponses, y compris d'erreur et de maintenance."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.testclient import TestClient

from biblyo.app import create_app
from biblyo.core.modules import Module
from biblyo.core.security.headers import CSP
from biblyo.core.security.limits import MAX_BODY_BYTES


def _assert_headers(response) -> None:
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "camera=(self)" in response.headers["permissions-policy"]


def test_csp_is_strict():
    assert "unsafe-inline" not in CSP
    assert "unsafe-eval" not in CSP
    assert "script-src 'self'" in CSP
    assert "frame-ancestors 'none'" in CSP
    assert "base-uri 'none'" in CSP


def test_headers_on_every_kind_of_response(client, client_as):
    responses = [
        client.get("/sante"),  # JSON public
        client.get("/a-propos"),  # page publique
        client.get("/compte"),  # redirection
        client.get("/nimporte-quoi"),  # 404
        client.post("/connexion"),  # 403 CSRF
        client.get("/static/absent.css"),  # fichier statique absent
        client_as("viewer").get("/admin/utilisateurs"),  # 403
    ]
    assert [r.status_code for r in responses] == [200, 200, 303, 404, 403, 404, 403]
    for response in responses:
        _assert_headers(response)


def test_headers_in_maintenance(bare_settings):
    with TestClient(create_app(bare_settings), raise_server_exceptions=False) as test_client:
        for path in ("/", "/sante"):
            response = test_client.get(path)
            assert response.status_code == 503
            _assert_headers(response)


def test_headers_on_server_error(settings):
    router = APIRouter()

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError("panne")

    module = Module("boom", "Boom", router=router, public_paths=("/boom",))
    with TestClient(create_app(settings, modules=[module]), raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")
    assert response.status_code == 500
    _assert_headers(response)
    assert response.headers["cache-control"] == "no-store"


def test_html_pages_are_not_stored(client_as):
    assert client_as("volunteer").get("/compte").headers["cache-control"] == "no-store"


def test_oversized_body_is_refused_with_its_size_announced(client):
    response = client.post("/connexion", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    _assert_headers(response)


def test_oversized_body_is_refused_when_sent_in_chunks(client):
    def chunks():
        for _ in range(3):
            yield b"x" * (MAX_BODY_BYTES // 2 + 1)

    response = client.post(
        "/connexion", content=chunks(), headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 413
    _assert_headers(response)


def test_normal_form_passes_the_limit(client):
    assert client.post("/connexion", data={"identifiant": "x"}).status_code == 403  # jeton manquant, pas de taille


def test_invalid_form_gets_a_french_error_without_detail(client_as):
    response = client_as("admin").post("/admin/utilisateurs/pas-un-nombre", data={"nom": "x"})
    assert response.status_code == 400
    assert response.text == "Requête invalide."
