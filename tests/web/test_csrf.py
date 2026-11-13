# SPDX-License-Identifier: EUPL-1.2
"""CSRF : toute requête qui modifie quelque chose est refusée sans le bon jeton."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from biblyo.core.permissions import route_table

from ..factories import PASSWORD, make_user

FORM = {"identifiant": "camille", "mot_de_passe": PASSWORD}


def test_login_without_token_is_refused(client, settings):
    make_user(settings, "camille")
    response = client.post("/connexion", data=FORM)
    assert response.status_code == 403
    assert "Le formulaire a expiré" in response.text


def test_login_with_wrong_token_is_refused(client, settings, anonymous_form):
    make_user(settings, "camille")
    anonymous_form(client, "/connexion")  # pose le cookie
    assert client.post("/connexion", data={**FORM, "_csrf": "mauvais"}).status_code == 403


def test_login_with_token_but_no_cookie_is_refused(app, settings, anonymous_form):
    make_user(settings, "camille")
    with TestClient(app, follow_redirects=False) as first:
        token = anonymous_form(first, "/connexion")
    with TestClient(app, follow_redirects=False) as second:
        assert second.post("/connexion", data={**FORM, "_csrf": token}).status_code == 403


def test_authenticated_form_needs_the_session_token(client_as):
    browser = client_as("volunteer")
    token = browser.headers.pop("X-CSRF-Token")
    assert browser.post("/compte/profil", data={"nom": "Nouveau"}).status_code == 403
    assert browser.post("/compte/profil", data={"nom": "Nouveau", "_csrf": "mauvais"}).status_code == 403
    assert browser.post("/compte/profil", data={"nom": "Nouveau", "_csrf": token}).status_code == 303


def test_htmx_uses_the_header(client_as):
    browser = client_as("volunteer")
    token = browser.headers.pop("X-CSRF-Token")
    htmx = {"HX-Request": "true"}
    assert browser.post("/compte/profil", data={"nom": "N"}, headers=htmx).status_code == 403
    wrong = {**htmx, "X-CSRF-Token": "mauvais"}
    assert browser.post("/compte/profil", data={"nom": "N"}, headers=wrong).status_code == 403
    right = {**htmx, "X-CSRF-Token": token}
    assert browser.post("/compte/profil", data={"nom": "N"}, headers=right).status_code == 303


def test_anonymous_token_is_not_accepted_once_connected(client_as, app, anonymous_form, settings):
    browser = client_as("volunteer")
    browser.headers.pop("X-CSRF-Token")
    make_user(settings, "autre")
    with TestClient(app, follow_redirects=False) as anonymous:
        anonymous_token = anonymous_form(anonymous, "/connexion")
    browser.cookies.set("biblyo_csrf", anonymous_token)
    assert browser.post("/compte/profil", data={"nom": "N", "_csrf": anonymous_token}).status_code == 403


def test_page_contains_the_session_token(client_as):
    browser = client_as("volunteer")
    token = browser.headers["X-CSRF-Token"]
    assert re.search(rf'name="_csrf" value="{re.escape(token)}"', browser.get("/compte").text)


def test_every_modifying_route_refuses_a_request_without_token(app, client_as):
    """Fermé par défaut : ajouter une route d'écriture ne demande rien de plus pour qu'elle soit couverte."""
    browser = client_as("admin")
    browser.headers.pop("X-CSRF-Token")
    checked = 0
    for route, _ in route_table(app):
        for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            response = browser.request(method, path, data={})
            assert response.status_code == 403, f"{method} {path}: {response.status_code}"
            checked += 1
    assert checked >= 8
