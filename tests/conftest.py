# SPDX-License-Identifier: EUPL-1.2
"""Fixtures communes : chaque test travaille dans un répertoire temporaire, sur une base créée par les migrations."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from biblyo.app import create_app
from biblyo.cli import cli
from biblyo.config import Settings
from biblyo.core import clock
from biblyo.core.migrate import upgrade
from biblyo.core.security import passwords
from biblyo.core.security.sessions import COOKIE_NAME

from .factories import make_user, open_session_for


def _make_settings(root: Path) -> Settings:
    (root / "data").mkdir()
    (root / "backups").mkdir()
    return Settings(env="test", data_dir=root / "data", backup_dir=root / "backups")


@pytest.fixture(scope="session", autouse=True)
def _fast_password_hashing():
    """Le coût réel d'argon2id n'apporte rien aux tests, sauf à les ralentir."""
    passwords.configure(time_cost=1, memory_cost=8, parallelism=1)


@pytest.fixture(scope="session")
def template_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Base à la dernière révision, obtenue en appliquant les migrations une fois par processus de test."""
    root = tmp_path_factory.mktemp("modele")
    settings = _make_settings(root)
    upgrade(settings)
    template = root / "modele.sqlite"
    connection = sqlite3.connect(settings.database_path)
    connection.execute("VACUUM INTO ?", (str(template),))
    connection.close()
    return template


@pytest.fixture
def bare_settings(tmp_path: Path) -> Settings:
    """Installation sans base de données."""
    return _make_settings(tmp_path)


@pytest.fixture
def settings(bare_settings: Settings, template_database: Path) -> Settings:
    """Installation dont la base est à jour."""
    bare_settings.database_path.write_bytes(template_database.read_bytes())
    return bare_settings


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


def _test_client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


@pytest.fixture
def client(app):
    with _test_client(app) as test_client:
        yield test_client


@pytest.fixture
def client_as(app, settings: Settings):
    """Fabrique de clients connectés : un compte et une session par appel, jeton CSRF déjà joint."""
    counter = iter(range(1, 1000))

    def _make(role: str = "volunteer", username: str | None = None, **options) -> TestClient:
        user_id = make_user(settings, username or f"{role}{next(counter)}", role, **options)
        raw, csrf_token = open_session_for(settings, user_id)
        test_client = _test_client(app)
        test_client.cookies.set(COOKIE_NAME, raw)
        test_client.headers["X-CSRF-Token"] = csrf_token
        test_client.user_id = user_id
        return test_client

    return _make


@pytest.fixture
def anonymous_form(settings: Settings):
    """Prépare un envoi de formulaire anonyme : lit le jeton CSRF de la page, comme le ferait un navigateur."""

    def _token(test_client: TestClient, page: str) -> str:
        response = test_client.get(page)
        assert response.status_code == 200, response.text
        return re.search(r'name="_csrf" value="([^"]+)"', response.text).group(1)

    return _token


class FrozenClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    def advance(self, **delta: float) -> None:
        self.moment += timedelta(**delta)

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture
def frozen_clock():
    instance = FrozenClock()
    clock.set_clock(instance)
    yield instance
    clock.set_clock(None)


@pytest.fixture
def run_cli(monkeypatch: pytest.MonkeyPatch):
    """Appelle la commande `biblyo` sur l'installation de test, depuis une ligne telle qu'elle est affichée."""

    def _run(settings: Settings, line: str, *, input: str | None = None):
        monkeypatch.setenv("BIBLYO_ENV", settings.env)
        monkeypatch.setenv("BIBLYO_DATA_DIR", str(settings.data_dir))
        monkeypatch.setenv("BIBLYO_BACKUP_DIR", str(settings.backup_dir))
        assert line.startswith("biblyo ")
        return CliRunner().invoke(cli, line.split()[1:], input=input)

    return _run
