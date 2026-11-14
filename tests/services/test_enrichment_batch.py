# SPDX-License-Identifier: EUPL-1.2
"""Enrichir toute la bibliothèque (L7, T4) : recherche par édition, trois niveaux, reprise, validation par lots.

Réponses enregistrées des parcours navigateur (`tests/fixtures/sources/enregistrees`) : Le Petit Prince (même ISBN
à la BnF, couverture annoncée), Cerveau-choc et Du scénario (relevés du 14 novembre 2026).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from biblyo.core.db import Database
from biblyo.core.errors import BusinessError
from biblyo.modules.enrichment import batch
from biblyo.modules.enrichment.connectors.transport import RecordedTransport
from biblyo.modules.enrichment.sources import SourceHub
from biblyo.modules.enrichment.throttle import Throttle
from tests.factories import make_notice

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "enregistrees"
PETIT_PRINCE = {"title": "Le Petit Prince", "isbn": "9782070612758", "publisher": "Galimard"}
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
UNKNOWN = {"title": "Un livre que personne ne connaît", "authors": ("Personne",)}


@pytest.fixture
def database(settings):
    instance = Database(settings.database_path)
    yield instance
    instance.dispose()


@pytest.fixture
def transport():
    return RecordedTransport(RECORDED)


@pytest.fixture
def hub(database, transport):
    clock = {"now": 0.0}

    def tick(seconds: float) -> None:
        clock["now"] += seconds

    return SourceHub(
        database, transport=transport, throttle=Throttle(clock=lambda: clock["now"], sleep=tick), sleep=tick
    )


def rows(settings, query, *params):
    connection = sqlite3.connect(settings.database_path)
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def run(database, hub, job_id) -> None:
    for _ in range(100):
        if not batch.step(database, hub, job_id):
            return
    raise AssertionError("la tâche ne finit pas")


def library(settings) -> dict[str, int]:
    return {
        "petit_prince": make_notice(settings, **PETIT_PRINCE),
        "cerveau_choc": make_notice(settings, **CERVEAU_CHOC),
        "du_scenario": make_notice(settings, **DU_SCENARIO),
        "inconnu": make_notice(settings, **UNKNOWN),
    }


def tiers(settings, job_id) -> dict[str, str | None]:
    found = rows(
        settings,
        "SELECT w.title, i.tier FROM enrichment_job_items i JOIN editions e ON e.id = i.edition_id"
        " JOIN works w ON w.id = e.work_id WHERE i.job_id = ?",
        job_id,
    )
    return dict(found)


def test_one_search_per_edition_and_three_levels(settings, database, hub):
    library(settings)
    job_id = batch.start(database, scope="all", user_id=None)
    run(database, hub, job_id)
    assert tiers(settings, job_id) == {
        "Le Petit Prince": "reliable",  # même ISBN
        "Cerveau-choc": "plausible",  # édition probable : jamais validée en lot
        "Du scénario à la réalisation": "plausible",  # 1981 ou 1990 : à choisir
        "Un livre que personne ne connaît": "insufficient",
    }
    assert rows(settings, "SELECT status, total, processed, errors FROM enrichment_jobs") == [("finished", 4, 4, 0)]
    assert rows(settings, "SELECT COUNT(*) FROM enrichment_searches") == [(4,)]


def test_the_batch_never_writes_in_the_records_and_never_asks_open_library(settings, database, hub, transport):
    ids = library(settings)
    rows(settings, "SELECT 1")
    connection = sqlite3.connect(settings.database_path)
    connection.execute("UPDATE metadata_sources SET is_enabled = 1, covers_enabled = 1 WHERE code = 'openlibrary'")
    connection.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at)"
        " VALUES ('enrichment.contact', '\"bibliotheque@exemple.invalid\"', '2026-11-14')"
    )
    connection.commit()
    connection.close()
    before = rows(settings, "SELECT id, title, version FROM works ORDER BY id")
    job_id = batch.start(database, scope="all", user_id=None)
    run(database, hub, job_id)
    assert rows(settings, "SELECT id, title, version FROM works ORDER BY id") == before
    assert rows(settings, "SELECT COUNT(*) FROM enrichment_decisions") == [(0,)]
    assert not [url for url in transport.calls if "openlibrary" in url or "recupererImage" in url]
    assert rows(
        settings,
        "SELECT cover_announced FROM enrichment_job_items i JOIN editions e ON e.id = i.edition_id WHERE e.work_id = ?",
        ids["petit_prince"],
    ) == [(1,)]


def test_a_recent_search_on_an_unchanged_record_is_reused(settings, database, hub, transport):
    library(settings)
    run(database, hub, batch.start(database, scope="all", user_id=None))
    calls = len(transport.calls)
    second = batch.start(database, scope="all", user_id=None)
    run(database, hub, second)
    assert len(transport.calls) == calls  # aucune nouvelle requête
    assert rows(settings, "SELECT COUNT(*) FROM enrichment_searches") == [(4,)]
    assert tiers(settings, second)["Le Petit Prince"] == "reliable"


def test_only_one_active_job_pause_resume_and_restart(settings, database, hub):
    library(settings)
    job_id = batch.start(database, scope="all", user_id=None)
    with pytest.raises(BusinessError):
        batch.start(database, scope="all", user_id=None)
    assert batch.step(database, hub, job_id)
    batch.pause(database, job_id, user_id=None)
    assert not batch.step(database, hub, job_id)  # en pause : rien ne bouge
    batch.resume(database, job_id, user_id=None)
    assert batch.recover_interrupted(database) == 1  # le service redémarre : la tâche est « interrompue »
    assert rows(settings, "SELECT status, processed FROM enrichment_jobs") == [("interrupted", 1)]
    batch.resume(database, job_id, user_id=None)
    run(database, hub, job_id)
    assert rows(settings, "SELECT status, processed FROM enrichment_jobs") == [("finished", 4)]
    assert rows(settings, "SELECT COUNT(*) FROM enrichment_job_items") == [(4,)]  # aucune édition en double


def test_scope_without_cover_and_editions_not_copies(settings, database, hub):
    ids = library(settings)
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO media_files (id, sha256, mime, bytes, width, height, origin, created_at)"
        " VALUES (900, 'x', 'image/webp', 1, 1, 1, 'upload', '2026-11-14')"
    )
    connection.execute("UPDATE editions SET cover_media_id = 900 WHERE work_id = ?", (ids["inconnu"],))
    connection.commit()
    connection.close()
    job_id = batch.start(database, scope="without_cover", user_id=None)
    assert rows(settings, "SELECT total FROM enrichment_jobs WHERE id = ?", job_id) == [(3,)]


def test_validation_adds_only_missing_fields_of_reliable_editions(settings, database, hub, tmp_path):
    ids = library(settings)
    job_id = batch.start(database, scope="all", user_id=None)
    run(database, hub, job_id)
    items = dict(
        rows(settings, "SELECT e.work_id, i.id FROM enrichment_job_items i JOIN editions e ON e.id = i.edition_id")
    )
    result = batch.validate(
        database,
        hub,
        tmp_path,
        item_ids=[items[ids["petit_prince"]], items[ids["cerveau_choc"]]],
        with_cover=True,
        user_id=None,
    )
    assert (result.applied, result.skipped) == (1, 1)  # Cerveau-choc « à vérifier » : jamais validé en lot
    fields = {f for (f,) in rows(settings, "SELECT field FROM enrichment_decisions WHERE decision = 'accepted'")}
    assert "cover" in fields and not fields & {"isbn", "contributors", "summary", "title", "publisher"}
    assert rows(
        settings,
        "SELECT name FROM publishers p JOIN editions e ON e.publisher_id = p.id WHERE e.work_id = ?",
        ids["petit_prince"],
    ) == [("Galimard",)]  # jamais remplacé
    assert rows(settings, "SELECT version FROM works WHERE id = ?", ids["cerveau_choc"]) == [(1,)]


def test_validation_is_limited_to_small_batches(database, hub, tmp_path):
    with pytest.raises(BusinessError):
        batch.validate(database, hub, tmp_path, item_ids=list(range(26)), with_cover=False, user_id=None)
