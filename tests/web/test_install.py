# SPDX-License-Identifier: EUPL-1.2
"""Première installation : jeton à usage unique, puis fermeture définitive."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from biblyo.app import create_app
from biblyo.core.db import Database
from biblyo.core.security import install

from ..factories import PASSWORD, make_user

GOOD = {
    "bibliotheque": "Bibliothèque d’essai",
    "nom": "Camille Essai",
    "identifiant": "camille",
    "mot_de_passe": PASSWORD,
    "confirmation": PASSWORD,
}


def _scalar(settings, query):
    connection = sqlite3.connect(settings.database_path)
    try:
        row = connection.execute(query).fetchone()
        return row[0] if row else None
    finally:
        connection.close()


@pytest.fixture
def token(settings):
    database = Database(settings.database_path)
    value = install.regenerate_token(database)
    database.dispose()
    return value


def _submit(client, anonymous_form, **fields):
    csrf = anonymous_form(client, "/installation")
    return client.post("/installation", data={**GOOD, "_csrf": csrf, **fields})


def test_startup_writes_a_token_in_the_journal(settings, caplog):
    caplog.set_level(logging.WARNING, logger="biblyo.install")
    with TestClient(create_app(settings)):
        pass
    found = re.search(r"Jeton à usage unique : (\S+)", caplog.text)
    assert found
    assert _scalar(settings, "SELECT value FROM settings WHERE key = 'install.token_hash'") == (
        f'"{hashlib.sha256(found.group(1).encode()).hexdigest()}"'
    )


def test_startup_keeps_the_token_already_announced(settings, caplog):
    caplog.set_level(logging.WARNING, logger="biblyo.install")
    with TestClient(create_app(settings)):
        pass
    caplog.clear()
    with TestClient(create_app(settings)):
        pass
    assert "Jeton" not in caplog.text


def test_startup_without_database_announces_nothing_and_creates_nothing(bare_settings, caplog):
    caplog.set_level(logging.WARNING, logger="biblyo.install")
    with TestClient(create_app(bare_settings)) as test_client:
        assert test_client.get("/installation").status_code == 503
    assert "Jeton" not in caplog.text
    assert not bare_settings.database_path.exists()


def test_installation_page_after_upgrade_announces_a_token(settings, caplog):
    """`db upgrade` sans redémarrage : le jeton sort dans le journal à la première visite."""
    caplog.set_level(logging.WARNING, logger="biblyo.install")
    app = create_app(settings)
    with TestClient(app, follow_redirects=False) as test_client:
        database = Database(settings.database_path)
        with database.session(write=True) as session:
            install.consume_token(session)
        database.dispose()
        caplog.clear()
        assert test_client.get("/installation").status_code == 200
    assert "Jeton à usage unique" in caplog.text


def test_good_token_creates_admin_and_library_name(client, settings, token, anonymous_form):
    response = _submit(client, anonymous_form, jeton=token)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/"
    assert _scalar(settings, "SELECT COUNT(*) FROM users") == 1
    assert _scalar(settings, "SELECT value FROM settings WHERE key = 'library.name'") == '"Bibliothèque d’essai"'
    assert _scalar(settings, "SELECT code FROM roles JOIN user_roles ON roles.id = role_id") == "admin"
    assert _scalar(settings, "SELECT value FROM settings WHERE key = 'install.token_hash'") is None
    assert client.get("/admin/utilisateurs").status_code == 200  # connecté et administrateur


def test_without_token_is_refused(client, settings, token, anonymous_form):
    response = _submit(client, anonymous_form, jeton="")
    assert response.status_code == 400
    assert "Jeton incorrect" in response.text
    assert _scalar(settings, "SELECT COUNT(*) FROM users") == 0


def test_wrong_token_is_refused(client, settings, token, anonymous_form):
    response = _submit(client, anonymous_form, jeton="mauvais")
    assert response.status_code == 400
    assert _scalar(settings, "SELECT COUNT(*) FROM users") == 0
    assert PASSWORD not in response.text  # le mot de passe saisi n'est jamais réaffiché


def test_reused_token_is_refused(app, settings, token, anonymous_form):
    with TestClient(app, follow_redirects=False) as first:
        assert _submit(first, anonymous_form, jeton=token).status_code == 303
    with TestClient(app, follow_redirects=False) as second:
        # La page est fermée : le navigateur n'obtient même plus de formulaire.
        assert second.get("/installation").headers["location"] == "/connexion"
        csrf = _anonymous_csrf(second, settings)
        response = second.post("/installation", data={**GOOD, "identifiant": "intrus", "jeton": token, "_csrf": csrf})
    assert response.status_code == 303
    assert response.headers["location"] == "/connexion"
    assert _scalar(settings, "SELECT COUNT(*) FROM users") == 1


def _anonymous_csrf(test_client, _settings):
    page = test_client.get("/connexion").text
    return re.search(r'name="_csrf" value="([^"]+)"', page).group(1)


def test_closed_as_soon_as_a_user_exists(client, settings, token):
    make_user(settings, "camille")
    assert client.get("/installation").headers["location"] == "/connexion"
    csrf = _anonymous_csrf(client, settings)
    response = client.post("/installation", data={**GOOD, "identifiant": "intrus", "jeton": token, "_csrf": csrf})
    assert response.headers["location"] == "/connexion"
    assert _scalar(settings, "SELECT COUNT(*) FROM users") == 1


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("bibliotheque", " ", "obligatoire"),
        ("nom", "", "obligatoire"),
        ("identifiant", "a", "identifiant compte"),
        ("mot_de_passe", "court", "au moins 10"),
        ("confirmation", "autre chose", "diffèrent"),
    ],
)
def test_invalid_fields_are_reported_and_token_survives(
    client, settings, token, anonymous_form, field, value, fragment
):
    response = _submit(client, anonymous_form, jeton=token, **{field: value})
    assert response.status_code == 400
    assert fragment in response.text
    assert _scalar(settings, "SELECT COUNT(*) FROM users") == 0
    assert _submit(client, anonymous_form, jeton=token).status_code == 303


def test_wrong_tokens_are_limited(client, token, anonymous_form):
    for _ in range(10):
        assert _submit(client, anonymous_form, jeton="mauvais").status_code == 400
    assert _submit(client, anonymous_form, jeton=token).status_code == 429


def test_cli_token_replaces_the_previous_one(client, settings, token, anonymous_form, run_cli):
    result = run_cli(settings, "biblyo install token")
    assert result.exit_code == 0, result.output
    fresh = re.search(r"Jeton d’installation : (\S+)", result.output).group(1)
    assert fresh != token
    assert _submit(client, anonymous_form, jeton=token).status_code == 400
    assert _submit(client, anonymous_form, jeton=fresh).status_code == 303


def test_cli_token_is_refused_once_installed(settings, run_cli):
    make_user(settings, "camille")
    result = run_cli(settings, "biblyo install token")
    assert result.exit_code == 1
    assert "déjà terminée" in result.stderr


def test_cli_token_is_refused_without_database(bare_settings, run_cli):
    result = run_cli(bare_settings, "biblyo install token")
    assert result.exit_code == 1
    assert "non initialisée" in result.stderr
    assert not bare_settings.database_path.exists()
