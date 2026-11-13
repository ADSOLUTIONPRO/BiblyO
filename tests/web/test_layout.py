# SPDX-License-Identifier: EUPL-1.2
"""Mises en page : langue, repères, titre unique, lien d'évitement, fragments HTMX, thème, formats français."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from biblyo.app import create_app
from biblyo.core import flash, i18n
from biblyo.core.modules import Module
from biblyo.core.security.csrf import COOKIE_NAME as CSRF_COOKIE
from biblyo.core.templating import STATIC_DIR, static_url
from biblyo.core.web import THEME_COOKIE

from ..factories import make_user


def _assert_page_structure(html: str) -> None:
    assert '<html lang="fr"' in html
    assert len(re.findall(r"<h1\b", html)) == 1
    assert len(re.findall(r"<main\b", html)) == 1
    assert 'href="#contenu"' in html
    assert 'id="contenu"' in html
    assert "<title>" in html


CONNECTED_PAGES = ("/", "/compte", "/a-propos", "/nexiste-pas", "/_composants", "/a-venir/statistiques")


@pytest.mark.parametrize("path", CONNECTED_PAGES)
def test_connected_pages_have_the_shell(client_as, path):
    html = client_as("admin").get(path).text
    _assert_page_structure(html)
    assert 'aria-label="Navigation principale"' in html
    assert 'aria-label="Navigation mobile"' in html
    assert "<footer" in html
    assert "<header" in html


def test_refused_page_has_the_shell(client_as):
    response = client_as("viewer").get("/admin/utilisateurs")
    assert response.status_code == 403
    _assert_page_structure(response.text)
    assert 'aria-label="Navigation principale"' in response.text


def test_login_page_has_no_shell(client, settings):
    make_user(settings, "camille")
    html = client.get("/connexion").text
    _assert_page_structure(html)
    assert "Navigation principale" not in html
    assert "<footer" in html


def test_install_page_structure(client):
    _assert_page_structure(client.get("/installation").text)


def test_anonymous_error_uses_the_simple_layout(client):
    response = client.get("/nexiste-pas")
    assert response.status_code == 404
    _assert_page_structure(response.text)
    assert "Navigation principale" not in response.text


def test_maintenance_page_is_french_and_uses_only_local_files(bare_settings):
    with TestClient(create_app(bare_settings), raise_server_exceptions=False) as test_client:
        html = test_client.get("/").text
    assert '<html lang="fr"' in html
    assert len(re.findall(r"<h1\b", html)) == 1
    assert 'href="/static/dist/app.css' in html
    assert "http://" not in html
    assert "https://" not in html


def test_htmx_request_gets_a_fragment(client_as):
    browser = client_as("admin")
    full = browser.get("/compte")
    fragment = browser.get("/compte", headers={"HX-Request": "true"})
    assert "<!doctype html>" in full.text
    assert "<!doctype html>" not in fragment.text
    assert "Navigation principale" not in fragment.text
    assert "Mon compte" in fragment.text
    assert "HX-Request" in fragment.headers["vary"]


def test_htmx_history_restore_gets_a_full_page(client_as):
    response = client_as("admin").get("/compte", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert "<!doctype html>" in response.text


def test_library_name_is_shown(client_as, settings):
    import sqlite3

    connection = sqlite3.connect(settings.database_path)
    connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('library.name', ?, '2026-01-01T00:00:00Z')",
        ('"Médiathèque d’essai"',),
    )
    connection.commit()
    connection.close()
    html = client_as("viewer").get("/").text
    assert "Médiathèque d’essai" in html
    assert ">MÉ<" in html or ">MD<" in html


def test_home_greets_with_real_data_only(client_as, frozen_clock):
    html = client_as("viewer", display_name="Camille Essai").get("/").text
    assert "Bonjour, Camille" in html  # 12 h UTC, 14 h à Paris
    assert "Lundi 1 juin" in html
    assert "Votre bibliothèque est prête" in html
    frozen_clock.advance(hours=8)
    assert "Bonsoir, Camille" in client_as("viewer", display_name="Camille Essai").get("/").text


def test_admin_figures_are_reserved(client_as):
    assert "Comptes actifs" in client_as("admin").get("/").text
    viewer_page = client_as("viewer").get("/").text
    assert "Comptes actifs" not in viewer_page
    assert "Sauvegardes" not in viewer_page


def test_theme_comes_from_the_cookie_when_valid(client_as):
    browser = client_as("viewer")
    assert 'data-theme="' not in browser.get("/").text.split("<head>")[0]
    browser.cookies.set(THEME_COOKIE, "dark")
    assert '<html lang="fr" data-theme="dark">' in browser.get("/").text
    browser.cookies.set(THEME_COOKIE, '"><script>')
    html = browser.get("/").text
    assert '<html lang="fr">' in html


def test_flash_appears_once(client_as, settings):
    browser = client_as("viewer")
    browser.cookies.set(flash.COOKIE_NAME, "profile_updated")
    response = browser.get("/")
    assert "Votre profil a été mis à jour." in response.text
    assert flash.COOKIE_NAME in "".join(response.headers.get_list("set-cookie"))


def test_unknown_flash_code_shows_nothing(client_as):
    browser = client_as("viewer")
    browser.cookies.set(flash.COOKIE_NAME, "<script>alert(1)</script>")
    assert "alert(1)" not in browser.get("/").text


def test_anonymous_page_sets_csrf_cookie_only_once(client, settings):
    make_user(settings, "camille")
    first = client.get("/connexion")
    assert CSRF_COOKIE in "".join(first.headers.get_list("set-cookie"))
    second = client.get("/connexion")
    assert CSRF_COOKIE not in "".join(second.headers.get_list("set-cookie"))


def test_csrf_meta_is_available_to_htmx(client_as):
    browser = client_as("viewer")
    token = browser.headers["X-CSRF-Token"]
    assert f'<meta name="csrf-token" content="{token}">' in browser.get("/").text


def test_htmx_is_configured_without_eval(client_as):
    html = client_as("viewer").get("/").text
    assert '"allowEval":false' in html
    assert '"includeIndicatorStyles":false' in html
    assert '"selfRequestsOnly":true' in html


def test_only_local_scripts_and_styles(client_as):
    html = client_as("admin").get("/").text
    # Seul lien externe admis : celui du code source (EUPL), un simple lien de navigation, jamais une ressource.
    source = '<a href="https://github.com/ADSOLUTIONPRO/BiblyO">'
    assert html.count(source) == 1
    for reference in re.findall(r'(?:src|href)="([^"]+)"', html.replace(source, "")):
        assert not reference.startswith(("http:", "https:", "//")), reference
    assert "<style" not in html
    assert " style=" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+=", html)


def test_static_files_are_served_with_long_cache_when_versioned(client):
    url = static_url("js/app.js")
    response = client.get(url)
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert "nosniff" in response.headers["x-content-type-options"]


def test_static_url_contains_a_digest():
    assert re.fullmatch(r"/static/js/app\.js\?v=[0-9a-f]{10}", static_url("js/app.js"))
    assert (STATIC_DIR / "js" / "app.js").is_file()


def test_french_formats():
    moment = datetime(2026, 11, 6, 13, 3, tzinfo=UTC)
    assert i18n.date_long(moment) == "6 novembre 2026"
    assert i18n.date_with_weekday(moment) == "Vendredi 6 novembre"
    assert i18n.datetime_short(moment) == "6 nov. 2026 à 14:03"
    assert i18n.datetime_short("2026-11-06T13:03:00Z") == "6 nov. 2026 à 14:03"
    assert i18n.number(12345) == "12 345"


def test_server_error_page_renders_for_a_connected_account(settings, client_as):
    router = APIRouter()

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret interne")

    module = Module("boom", "Boom", router=router, public_paths=("/boom",))
    from biblyo.modules.registry import all_modules

    browser = client_as("viewer")
    with TestClient(
        create_app(settings, modules=[*all_modules(), module]), raise_server_exceptions=False
    ) as test_client:
        test_client.cookies.update(browser.cookies)
        response = test_client.get("/boom")
    assert response.status_code == 500
    assert "Une erreur est survenue" in response.text
    assert "secret interne" not in response.text
