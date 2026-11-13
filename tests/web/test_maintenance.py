# SPDX-License-Identifier: EUPL-1.2
"""Mode maintenance : l'application démarre, répond 503 et ne touche jamais au schéma."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from biblyo.app import create_app
from biblyo.core import schema_state
from biblyo.core.migrate import upgrade


@pytest.fixture
def maintenance_client(bare_settings):
    with TestClient(create_app(bare_settings), raise_server_exceptions=False) as test_client:
        yield test_client


def test_absent_database_gives_maintenance_and_creates_nothing(bare_settings, maintenance_client):
    health = maintenance_client.get("/sante")
    assert health.status_code == 503
    assert health.json()["status"] == "maintenance"
    assert health.json()["schema"] == "absent"

    page = maintenance_client.get("/")
    assert page.status_code == 503
    assert page.headers["retry-after"] == "60"
    assert "Base de données non initialisée" in page.text
    assert "biblyo db upgrade" in page.text
    assert list(bare_settings.data_dir.iterdir()) == []


def test_every_page_but_health_is_closed(maintenance_client):
    assert maintenance_client.get("/a-propos").status_code == 503
    assert maintenance_client.post("/connexion").status_code == 503
    assert maintenance_client.get("/nimporte-quoi").status_code == 503


def test_static_files_stay_available(maintenance_client):
    # Le fichier existe dès T5 ; d'ici là, la réponse vient au moins de StaticFiles et non de la maintenance.
    assert maintenance_client.get("/static/absent.css").status_code == 404


def test_maintenance_does_not_reveal_details(bare_settings):
    bare_settings.database_path.write_bytes(b"pas une base" * 200)
    with TestClient(create_app(bare_settings)) as test_client:
        page = test_client.get("/")
    assert page.status_code == 503
    assert str(bare_settings.data_dir) not in page.text
    assert "not a database" not in page.text


def test_up_to_date_database_serves_pages(client):
    assert client.get("/sante").status_code == 200
    assert client.get("/a-propos").status_code == 200


def test_app_leaves_maintenance_without_restart(bare_settings, maintenance_client):
    assert maintenance_client.get("/sante").status_code == 503
    upgrade(bare_settings)
    assert maintenance_client.get("/sante").status_code == 200


def test_pending_revision_gives_maintenance_with_schema_untouched(settings, tmp_path, monkeypatch):
    head = schema_state.script_directory().get_current_head()
    connection = sqlite3.connect(settings.database_path)
    before = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    connection.close()

    import shutil

    target = tmp_path / "migrations"
    shutil.copytree(schema_state.MIGRATIONS_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    (target / "versions" / "9001_essai.py").write_text(
        f"revision = '9001'\ndown_revision = '{head}'\nbranch_labels = None\ndepends_on = None\n"
        "def upgrade():\n    raise AssertionError('jamais au démarrage')\n"
        "def downgrade():\n    pass\n"
    )
    monkeypatch.setattr(schema_state, "MIGRATIONS_DIR", target)
    schema_state._load_scripts.cache_clear()
    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
            health = test_client.get("/sante")
            page = test_client.get("/")
    finally:
        schema_state._load_scripts.cache_clear()
    assert health.status_code == 503
    assert health.json()["schema"] == "behind"
    assert page.status_code == 503
    assert "Mise à jour de la base requise" in page.text
    assert head in page.text
    assert "9001" in page.text

    connection = sqlite3.connect(settings.database_path)
    after = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    connection.close()
    assert after == before
    assert version == head


def test_database_newer_than_code_gives_maintenance(settings):
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE alembic_version SET version_num = '9999'")
    connection.commit()
    connection.close()
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        assert test_client.get("/sante").json()["schema"] == "ahead"
        page = test_client.get("/")
    assert page.status_code == 503
    assert "trop ancienne" in page.text
    connection = sqlite3.connect(settings.database_path)
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "9999"
    connection.close()
