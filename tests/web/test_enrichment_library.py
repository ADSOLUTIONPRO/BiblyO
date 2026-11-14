# SPDX-License-Identifier: EUPL-1.2
"""Écran « Enrichir la bibliothèque » (L7, T4) : tableau de travail, tâche contrôlée, validation par petits lots."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest

from biblyo.core.db import Database
from biblyo.modules.enrichment.connectors.transport import RecordedTransport
from biblyo.modules.enrichment.sources import SourceHub
from biblyo.modules.enrichment.throttle import Throttle
from tests.factories import make_notice
from tests.services.test_enrichment_batch import CERVEAU_CHOC, PETIT_PRINCE

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "enregistrees"


@pytest.fixture
def recorded(app, settings):
    clock = {"now": 0.0}

    def tick(seconds: float) -> None:
        clock["now"] += seconds

    transport = RecordedTransport(RECORDED)
    app.state.enrichment_hub = SourceHub(
        Database(settings.database_path),
        transport=transport,
        throttle=Throttle(clock=lambda: clock["now"], sleep=tick),
        sleep=tick,
    )
    yield transport
    runner = getattr(app.state, "enrichment_runner", None)
    if runner is not None:
        runner.stop()


def rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def wait_finished(settings, timeout=20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rows(settings, "SELECT status FROM enrichment_jobs ORDER BY id DESC LIMIT 1") == [("finished",)]:
            return
        time.sleep(0.05)
    raise AssertionError(rows(settings, "SELECT status, processed, total FROM enrichment_jobs"))


def test_the_board_shows_the_counts_and_the_launch_form(client_as, settings, recorded):
    make_notice(settings, **PETIT_PRINCE)
    page = client_as("librarian").get("/enrichissement").text
    assert "Enrichir la bibliothèque" in page and "Livres sans couverture" in page
    assert "Lancer la recherche" in page and "Toutes les éditions" in page
    assert recorded.calls == []


def test_the_catalogue_offers_the_library_action(client_as, settings, recorded):
    assert 'href="/enrichissement"' in client_as("librarian").get("/catalogue").text


def test_launch_runs_in_the_background_then_offers_reliable_books_in_small_batches(client_as, settings, recorded):
    petit = make_notice(settings, **PETIT_PRINCE)
    make_notice(settings, **CERVEAU_CHOC)
    browser = client_as("librarian")
    response = browser.post("/enrichissement/lancer", data={"livres": "all"})
    assert response.status_code == 303
    wait_finished(settings)
    page = browser.get("/enrichissement?vue=reliable").text
    assert "Le Petit Prince" in page and "Ajouter les informations manquantes aux livres cochés" in page
    assert "Cerveau-choc" not in page  # « à vérifier » : jamais dans la validation par lots
    assert "Cerveau-choc" in browser.get("/enrichissement?vue=plausible").text
    item = re.search(r'name="element" value="(\d+)"', page).group(1)
    done = browser.post("/enrichissement/valider", data={"element": item, "vue": "reliable"})
    assert done.status_code == 303 and "valides=1" in done.headers["location"]
    assert rows(settings, "SELECT COUNT(*) FROM enrichment_decisions WHERE decision = 'accepted'")[0][0] > 0
    assert rows(settings, "SELECT title FROM works WHERE id = ?", petit) == [("Le Petit Prince",)]


def test_pause_and_resume_are_reserved_to_the_batch_permission(client_as, settings, recorded):
    make_notice(settings, **PETIT_PRINCE)
    assert client_as("volunteer").post("/enrichissement/lancer", data={"livres": "all"}).status_code == 403
    browser = client_as("admin")
    browser.post("/enrichissement/lancer", data={"livres": "all"})
    job = rows(settings, "SELECT id FROM enrichment_jobs")[0][0]
    wait_finished(settings)
    refused = browser.post(f"/enrichissement/{job}/pause")
    assert refused.status_code == 303  # une tâche terminée ne se met pas en pause : message, aucune erreur
    assert rows(settings, "SELECT status FROM enrichment_jobs") == [("finished",)]


def test_viewers_cannot_open_the_board(client_as, settings, recorded):
    assert client_as("viewer").get("/enrichissement").status_code == 403


def test_the_progress_is_shown_while_the_search_runs(client_as, settings, recorded, app):
    from biblyo.modules.enrichment import batch

    make_notice(settings, **PETIT_PRINCE)
    batch.start(Database(settings.database_path), scope="all", user_id=None)  # en cours, pas encore avancée
    app.state.enrichment_runner = batch.Runner(app.state.database, lambda: app.state.enrichment_hub)
    app.state.enrichment_runner.ensure = lambda job_id: None  # la progression est lue telle quelle
    fragment = client_as("librarian").get("/enrichissement/progression")
    assert fragment.status_code == 200 and "0 livre(s) sur 1" in fragment.text and "pour cent" in fragment.text


def test_pause_resume_and_cancel_through_the_screen(client_as, settings, recorded, app):
    from biblyo.modules.enrichment import batch

    make_notice(settings, **PETIT_PRINCE)
    app.state.enrichment_runner = batch.Runner(app.state.database, lambda: app.state.enrichment_hub)
    app.state.enrichment_runner.ensure = lambda job_id: None  # la tâche reste « en cours » sans avancer
    admin = client_as("admin")
    admin.post("/enrichissement/lancer", data={"livres": "all"})
    job = rows(settings, "SELECT id FROM enrichment_jobs")[0][0]
    assert "Mettre en pause" in admin.get("/enrichissement").text
    assert admin.post(f"/enrichissement/{job}/pause").status_code == 303
    assert rows(settings, "SELECT status FROM enrichment_jobs") == [("paused",)]
    assert "Reprendre" in admin.get("/enrichissement").text
    admin.post(f"/enrichissement/{job}/reprendre")
    assert rows(settings, "SELECT status FROM enrichment_jobs") == [("running",)]
    admin.post(f"/enrichissement/{job}/annuler")
    assert rows(settings, "SELECT status FROM enrichment_jobs") == [("cancelled",)]
    assert "Lancer la recherche" in admin.get("/enrichissement").text
    assert admin.post(f"/enrichissement/{job}/inconnue").status_code == 404
    events = {a for (a,) in rows(settings, "SELECT action FROM audit_log WHERE action LIKE 'enrichment.batch%'")}
    assert events == {
        "enrichment.batch_started",
        "enrichment.batch_paused",
        "enrichment.batch_running",
        "enrichment.batch_cancelled",
    }
